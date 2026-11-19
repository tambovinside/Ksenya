
"""
Ksenya AI Security — многоступенчатая AI-модерация Telegram.

Возможности:
- Расширенный локальный фильтр токсичности.
- Обнаружение мата, оскорблений, угроз и травли.
- Обнаружение транслита, цифр и обхода символами.
- Обнаружение слов, разделённых пробелами.
- Контекстные выражения.
- Ограниченная очередь сообщений.
- Один одновременный запрос Harmony, чтобы не перегружать API.
- Увеличенный таймаут для холодного старта Render и защита от зависших запросов.
- Сохранение нарушений в Turso.
- 5 нарушений за 2 часа = мут на 24 часа.
- Карточки администратору с кнопками.
"""

import asyncio
import html
import logging
import os
import re
import secrets
import time
import unicodedata

from datetime import datetime, timedelta, timezone
from typing import Any

import aiohttp

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ChatPermissions,
)

from telegram.constants import ChatType, ParseMode
from telegram.error import TelegramError
from telegram.ext import ContextTypes

import db


logger = logging.getLogger("moderai")


# ============================================================
# CONFIGURATION
# ============================================================

API_URL = "https://tambovinside.onrender.com/check"

API_TOKEN = os.getenv("AImoder", "").strip()

ADMIN_ID = int(os.getenv("ADMIN_ID", "0") or 0)
CHAT_ID = int(os.getenv("CHAT_ID", "0") or 0)

ADMIN_MENTION = "@YouWarm"

VIOLATION_THRESHOLD = 5

WINDOW_SECONDS = 2 * 60 * 60
MUTE_SECONDS = 24 * 60 * 60

# Render free-сервис может просыпаться после простоя. Даем API время
# на холодный старт, но не создаём много параллельных запросов.
REQUEST_TIMEOUT_SECONDS = 70
CONNECT_TIMEOUT_SECONDS = 12
SOCK_READ_TIMEOUT_SECONDS = 60

# Один запрос одновременно: не перегружаем небольшой сервер Harmony.
MAX_PARALLEL_REQUESTS = 1

# Ограниченная очередь. При перегрузке бот не блокирует обработку чата.
MAX_QUEUE_SIZE = 250

# Максимальная длина текста для Harmony.
MAX_TEXT_LENGTH = 1000

# Очередь не позволяет создавать бесконечное количество задач.
_queue: asyncio.Queue | None = None

_workers: set[asyncio.Task] = set()

_session: aiohttp.ClientSession | None = None

_request_semaphore = asyncio.Semaphore(MAX_PARALLEL_REQUESTS)

_user_locks: dict[tuple[int, int], asyncio.Lock] = {}

# Защита от повторного добавления одного сообщения.
_queued_messages: set[tuple[int, int]] = set()


# ============================================================
# NORMALIZATION
# ============================================================

# Нормализация распространённых замен букв цифрами и латинскими
# символами-двойниками. Это только вспомогательный вариант текста:
# исходное сообщение не изменяется.
_TRANSLATION = str.maketrans({
    "0": "о", "1": "и", "3": "з", "4": "ч",
    "5": "с", "6": "б", "8": "в",
    "@": "а", "$": "с", "ё": "е",
    "a": "а", "c": "с", "e": "е", "o": "о",
    "p": "р", "x": "х", "y": "у", "k": "к",
    "m": "м", "t": "т", "b": "в", "h": "н",
})


def _normalize_for_filter(text: str) -> list[str]:
    """Создаёт варианты текста для поиска замаскированных нарушений."""
    if not text:
        return []

    value = unicodedata.normalize("NFKC", text).lower()
    value = re.sub(r"[\u200b-\u200f\u202a-\u202e\ufeff\u2060]", "", value)
    value = value.replace("ё", "е")

    # Оригинал с нормализацией регистра и невидимых символов.
    normalized = value

    # Вариант с цифрами и визуально похожими латинскими буквами.
    homoglyphs = value.translate(_TRANSLATION)

    # Убираем знаки между буквами: п-и-з-д-а, п.и.з.д.а, п и з д а.
    # Сохраняем отдельный compact-variant исходника, чтобы не терять
    # транслит вроде b l y a t при нормализации похожих латинских букв.
    compact_original = re.sub(
        r"(?<=[a-zа-я0-9])[\W_]{1,4}(?=[a-zа-я0-9])",
        "",
        normalized,
        flags=re.IGNORECASE,
    )
    compact = re.sub(
        r"(?<=[a-zа-я0-9])[\W_]{1,4}(?=[a-zа-я0-9])",
        "",
        homoglyphs,
        flags=re.IGNORECASE,
    )

    # Более агрессивный вариант для обходов через несколько символов.
    compact_all = re.sub(r"[\W_]+", "", homoglyphs, flags=re.UNICODE)
    compact_original_all = re.sub(r"[\W_]+", "", normalized, flags=re.UNICODE)

    # Схлопываем длинные повторы: бляяяя -> бляя, суууука -> суука.
    compact_repeat = re.sub(r"(.)\1{2,}", r"\1\1", compact, flags=re.DOTALL)
    compact_original_repeat = re.sub(
        r"(.)\1{2,}", r"\1\1", compact_original, flags=re.DOTALL
    )

    # Убираем повторные пробелы, сохраняя границы слов.
    spaced = re.sub(r"\s+", " ", homoglyphs).strip()

    # Уникальные варианты в исходном порядке.
    return list(dict.fromkeys(
        item for item in (
            normalized, homoglyphs, spaced, compact_original, compact,
            compact_all, compact_original_all, compact_repeat,
            compact_original_repeat
        )
        if item
    ))


# ============================================================
# LOCAL FILTER
# ============================================================

# Это предварительный фильтр, а не окончательный классификатор.
# Совпадение означает только необходимость проверки Harmony.

_SUSPICIOUS_RE = re.compile(
    r"(?iu)("

    # --------------------------------------------------------
    # Русский мат
    # --------------------------------------------------------

    r"бля\w*|бляд\w*|блят\w*|"
    r"сука|суки|сукой|суку|сучк\w*|"
    r"еб\w*|ёб\w*|йоб\w*|"
    r"пизд\w*|пезд\w*|"
    r"ху[йеяиюи]\w*|хуй\w*|"
    r"нахуй|нахуя|"
    r"муд[ао]\w*|"
    r"гандон\w*|"
    r"залуп\w*|"
    r"шлюх\w*|"
    r"манда\w*|"
    r"говн\w*|"
    r"дерьм\w*|"
    r"уеб\w*|уёб\w*|"
    r"заеб\w*|заёб\w*|"
    r"долбо[её]б\w*|"
    r"выеб\w*|въеб\w*|"
    r"оху\w*|"
    r"хер\w*|"
    r"ссан\w*|"
    r"ссык\w*|"
    r"еблан\w*|"
    r"ебуч\w*|"
    r"ебучк\w*|"
    r"поху\w*|"
    r"хуле\b|"
    r"хули\b|"
    r"бзд\w*|"

    # --------------------------------------------------------
    # Оскорбления
    # --------------------------------------------------------

    r"мраз\w*|"
    r"урод\w*|"
    r"дебил\w*|"
    r"идиот\w*|"
    r"туп\w*|"
    r"кретин\w*|"
    r"ничтож\w*|"
    r"ублюд\w*|"
    r"сволоч\w*|"
    r"мерзав\w*|"
    r"чмош\w*|"
    r"чмо\b|"
    r"коз[её]л\w*|"
    r"лох\w*|"
    r"лохуш\w*|"
    r"гнид\w*|"
    r"падл\w*|"
    r"твар\w*|"
    r"мудак\w*|"
    r"петух\w*|"
    r"свинь\w*|"
    r"пидор\w*|"
    r"пидар\w*|"
    r"пидр\w*|"
    r"пидорас\w*|"
    r"ссыкло\w*|"
    r"безмозгл\w*|"
    r"бестолоч\w*|"
    r"недоумок\w*|"
    r"ничтожество\w*|"
    r"позорищ\w*|"

    # --------------------------------------------------------
    # Угрозы и агрессия
    # --------------------------------------------------------

    r"убью|убить|убей|"
    r"сдохни|сдохнет|"
    r"зарежу|пристрелю|"
    r"сломаю|изобью|"
    r"уничтожу|"
    r"ненавижу|"
    r"заткнись|"
    r"закрой\s+рот|"
    r"пасть\s+закрой|"
    r"отвали|"
    r"вали\s+отсюда|"
    r"пош[её]л\s+на|"
    r"иди\s+нах|"
    r"получишь\s+пизд|"
    r"я\s+тебя\s+убью|"
    r"тебе\s+конец|"
    r"найду\s+тебя|"
    r"разобью\s+тебе|"
    r"перережу|"

    # --------------------------------------------------------
    # Сексуальная лексика
    # --------------------------------------------------------

    r"секс\w*|"
    r"порно\w*|"
    r"дроч\w*|"
    r"минет\w*|"
    r"куни\w*|"
    r"трах\w*|"
    r"конча\w*|"
    r"сперм\w*|"
    r"член\w*|"
    r"вагин\w*|"
    r"пенис\w*|"
    r"нюдс\w*|"
    r"голыш\w*|"
    r"сос[иа]\w*|"

    # --------------------------------------------------------
    # Травля и унижение
    # --------------------------------------------------------

    r"никому\s+не\s+нужен|"
    r"ты\s+никто|"
    r"ты\s+ничтожество|"
    r"тебя\s+ненавидят|"
    r"всем\s+на\s+тебя\s+плевать|"
    r"лучше\s+бы\s+тебя\s+не\s+было|"
    r"ты\s+позор|"
    r"ты\s+жалок|"
    r"ты\s+жалкая|"
    r"ты\s+мерзкий|"
    r"ты\s+мерзкая|"
    r"ты\s+отвратителен|"
    r"ты\s+отвратительна|"
    r"никто\s+тебя\s+не\s+любит|"
    r"всем\s+на\s+тебя\s+плевать|"
    r"лучше\s+исчезни|"
    r"вали\s+из\s+чата|"
    r"опущенн\w*|"

    # --------------------------------------------------------
    # Транслит
    # --------------------------------------------------------

    r"blya\w*|"
    r"blyat\w*|"
    r"suka\w*|"
    r"pizd\w*|"
    r"huy\w*|"
    r"hui\w*|"
    r"ebat\w*|"
    r"mudak\w*|"
    r"pidor\w*|"
    r"nahuy\w*|"
    r"zaeb\w*|"
    r"yob\w*|"
    r"hernya\w*|"
    r"debil\w*|"
    r"idiot\w*"

    r")"
)


# Дополнительные признаки: угрозы, унижение, домогательства и транслит.
# Они только направляют сообщение на Harmony, а не приводят к автоматическому муту.
_EXTRA_SUSPICIOUS_RE = re.compile(
    r"(?iu)("
    r"сдохн(и|ите|ет)|"
    r"чтоб\s+ты\s+(сдох|умер|сдохла)|"
    r"лучше\s+бы\s+ты\s+(умер|сдох)|"
    r"закрой\s+(свой\s+)?(рот|пасть)|"
    r"заткнись|"
    r"пош[её]л\s+в\sон|"
    r"иди\s+в\sон|"
    r"тебя\s+надо\s+(убить|избить|уничтожить)|"
    r"я\s+тебя\s+(найду|достану|прибью|покалечу)|"
    r"скинь\s+(нюдс|голые\s+фото|обнаженн\w*)|"
    r"покажи\s+(сиськи|грудь|письку|член)|"
    r"разденься|отсоси|"
    r"ты\s+(кончен\w*|ебан\w*|долбан\w*|тупиц\w*|кретин\w*)|"
    r"сборище\s+(дебил\w*|идиот\w*)|"
    r"никч[её]мн\w*|"
    r"fuck\w*|b[i1]tch\w*|asshole\w*|motherfucker\w*|retard\w*"
    r")"
)

# Дополнительные контекстные признаки.
_CONTEXT_RE = re.compile(
    r"(?iu)("
    r"ты\s+(?:туп|дебил|идиот|урод|мраз|чмо|"
    r"ничтож|мудак|пидор|лох|свинь)|"
    r"сам\s+(?:ты\s+)?(?:дебил|идиот|урод|мраз)|"
    r"твоя\s+(?:мать|мамаша)|"
    r"твою\s+мать|"
    r"мать\s+твою|"
    r"пош[её]л\s+ты|"
    r"иди\s+ты\s+на|"
    r"закрой\s+свой\s+рот|"
    r"заткни\s+свою\s+пасть|"
    r"сдохни\s+уже|"
    r"ты\s+мне\s+противен|"
    r"ты\s+мне\s+противна"
    r")"
)


def _is_candidate(text: str) -> bool:
    """
    Быстрая локальная проверка.
    Harmony вызывается только при наличии подозрительных признаков.
    """

    if not text:
        return False

    variants = _normalize_for_filter(text)

    for variant in variants:
        if _SUSPICIOUS_RE.search(variant):
            return True

        if _EXTRA_SUSPICIOUS_RE.search(variant):
            return True

        if _CONTEXT_RE.search(variant):
            return True

    return False


# ============================================================
# TELEGRAM PERMISSIONS
# ============================================================

def _permissions_muted() -> ChatPermissions:
    return ChatPermissions(
        can_send_messages=False,
        can_send_audios=False,
        can_send_documents=False,
        can_send_photos=False,
        can_send_videos=False,
        can_send_video_notes=False,
        can_send_voice_notes=False,
        can_send_polls=False,
        can_send_other_messages=False,
        can_add_web_page_previews=False,
    )


def _permissions_unmuted() -> ChatPermissions:
    return ChatPermissions(
        can_send_messages=True,
        can_send_audios=True,
        can_send_documents=True,
        can_send_photos=True,
        can_send_videos=True,
        can_send_video_notes=True,
        can_send_voice_notes=True,
        can_send_polls=True,
        can_send_other_messages=True,
        can_add_web_page_previews=True,
    )


# ============================================================
# DATABASE
# ============================================================

async def init_db() -> None:
    """
    Создание таблиц и запуск очереди.
    Вызывай при запуске бота.
    """

    await db._exec_required(
        """
        CREATE TABLE IF NOT EXISTS ai_moderation_violations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            message_id INTEGER NOT NULL,
            message_text TEXT,
            toxic_score REAL,
            created_at INTEGER NOT NULL,
            UNIQUE(chat_id, message_id)
        )
        """
    )

    await db._exec_required(
        """
        CREATE INDEX IF NOT EXISTS idx_ai_mod_user_window
        ON ai_moderation_violations(
            chat_id, user_id, created_at
        )
        """
    )

    await db._exec_required(
        """
        CREATE TABLE IF NOT EXISTS ai_moderation_alerts (
            alert_id TEXT PRIMARY KEY,
            chat_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            violation_count INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            auto_muted INTEGER NOT NULL DEFAULT 0,
            created_at INTEGER NOT NULL,
            muted_until INTEGER
        )
        """
    )

    await db._exec_required(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_ai_mod_pending_alert
        ON ai_moderation_alerts(chat_id, user_id)
        WHERE status = 'pending'
        """
    )

    _start_workers()

    if not API_TOKEN:
        logger.warning(
            "AImoder отсутствует в Render Environment. "
            "AI-модерация выключена."
        )
    else:
        logger.info(
            "Ksenya AI Security initialized: "
            "chat=%s threshold=%s window=%s workers=%s",
            CHAT_ID,
            VIOLATION_THRESHOLD,
            WINDOW_SECONDS,
            MAX_PARALLEL_REQUESTS,
        )


# ============================================================
# QUEUE
# ============================================================

def _start_workers() -> None:
    global _queue

    if _queue is None:
        _queue = asyncio.Queue(maxsize=MAX_QUEUE_SIZE)

    active_workers = sum(not task.done() for task in _workers)

    for _ in range(MAX_PARALLEL_REQUESTS - active_workers):
        task = asyncio.create_task(_worker_loop())
        _workers.add(task)
        task.add_done_callback(_workers.discard)


async def _worker_loop() -> None:
    while True:
        item = await _queue.get()

        try:
            message, bot = item
            await _process_message(message, bot)

        except asyncio.CancelledError:
            raise

        except Exception:
            logger.exception("Ошибка обработки сообщения AI-модерацией")

        finally:
            if item:
                message, _ = item
                _queued_messages.discard(
                    (message.chat_id, message.message_id)
                )

            _queue.task_done()


# ============================================================
# HTTP SESSION
# ============================================================

async def _get_session() -> aiohttp.ClientSession:
    global _session

    if _session is None or _session.closed:
        timeout = aiohttp.ClientTimeout(
            total=REQUEST_TIMEOUT_SECONDS,
            connect=CONNECT_TIMEOUT_SECONDS,
            sock_read=SOCK_READ_TIMEOUT_SECONDS,
        )

        connector = aiohttp.TCPConnector(
            limit=MAX_PARALLEL_REQUESTS,
            ttl_dns_cache=300,
            enable_cleanup_closed=True,
        )

        _session = aiohttp.ClientSession(
            timeout=timeout,
            connector=connector,
        )

    return _session


async def close() -> None:
    """
    Вызывать при завершении приложения.
    """

    global _session

    if _queue is not None:
        try:
            await asyncio.wait_for(
                _queue.join(),
                timeout=5,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "Очередь AI-модерации не успела полностью обработаться"
            )

    for task in list(_workers):
        task.cancel()

    if _workers:
        await asyncio.gather(
            *_workers,
            return_exceptions=True,
        )

    _workers.clear()

    if _session and not _session.closed:
        await _session.close()

    _session = None

    logger.info("AI moderation stopped")


# ============================================================
# HARMONY API
# ============================================================

def _parse_score(value: Any) -> float:
    """
    Поддерживает значения как 0.6639, так и 66.39.
    Возвращает значение в диапазоне 0..1.
    """

    try:
        score = float(value)
    except (TypeError, ValueError):
        return 0.0

    if score > 1:
        score /= 100

    return max(0.0, min(score, 1.0))


async def _classify(text: str) -> dict[str, Any] | None:
    if not API_TOKEN:
        return None

    session = await _get_session()

    headers = {
        "X-API-Token": API_TOKEN,
    }

    payload = {
        "text": text[:MAX_TEXT_LENGTH],
    }

    async with _request_semaphore:
        try:
            async with session.post(
                API_URL,
                json=payload,
                headers=headers,
            ) as response:

                if response.status != 200:
                    body = (await response.text())[:300]

                    logger.warning(
                        "Harmony HTTP %s: %s",
                        response.status,
                        body,
                    )

                    return None

                result = await response.json(
                    content_type=None,
                )

                if not isinstance(result, dict):
                    logger.warning(
                        "Harmony returned invalid JSON: %r",
                        result,
                    )
                    return None

                if "class" not in result:
                    logger.warning(
                        "Harmony response has no class: %r",
                        result,
                    )
                    return None

                return result

        except asyncio.TimeoutError:
            logger.warning("Harmony request timeout")

        except aiohttp.ClientError:
            logger.exception("Harmony connection error")

        except ValueError:
            logger.exception("Harmony JSON parsing error")

        except Exception:
            logger.exception("Unexpected Harmony error")

    return None


# ============================================================
# USER LOCKS
# ============================================================

def _user_lock(
    chat_id: int,
    user_id: int,
) -> asyncio.Lock:

    key = (chat_id, user_id)

    lock = _user_locks.get(key)

    if lock is None:
        lock = asyncio.Lock()
        _user_locks[key] = lock

    return lock


# ============================================================
# AUTOMATIC MUTE
# ============================================================

async def _try_mute(
    bot,
    chat_id: int,
    user_id: int,
) -> bool:

    until = (
        datetime.now(timezone.utc)
        + timedelta(seconds=MUTE_SECONDS)
    )

    try:
        await bot.restrict_chat_member(
            chat_id=chat_id,
            user_id=user_id,
            permissions=_permissions_muted(),
            until_date=until,
        )

        logger.info(
            "Automatic mute applied: user=%s",
            user_id,
        )

        return True

    except TelegramError:
        logger.exception(
            "Unable to mute user_id=%s",
            user_id,
        )

        return False


# ============================================================
# ADMIN NOTIFICATION
# ============================================================

async def _notify_admin(
    bot,
    *,
    alert_id: str,
    chat_id: int,
    user_id: int,
    first_name: str,
    username: str | None,
    count: int,
    auto_muted: bool,
    toxic_score: float,
) -> None:

    shown_name = (
        f"@{username}"
        if username
        else (first_name or "Пользователь")
    )

    user_link = (
        f'<a href="tg://user?id={user_id}">'
        f'{html.escape(shown_name)}'
        f'</a>'
    )

    mute_line = (
        "🔇 Автоматический мут на 24 часа применён."
        if auto_muted
        else "⚠️ Мут не применился. Проверьте права бота."
    )

    text = (
        "⚠️ <b>Ksenya AI Security обнаружила нарушителя</b>\n\n"
        f"Пользователь: {user_link}\n"
        f"Нарушений за 2 часа: <b>{count}</b> "
        f"из {VIOLATION_THRESHOLD}\n"
        f"Последняя токсичность: "
        f"<b>{toxic_score * 100:.2f}%</b>\n"
        "Модель: <code>floxoris/harmony-v1.2</code>\n"
        f"{mute_line}\n\n"
        f"Ожидается решение {ADMIN_MENTION}"
    )

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "❌ Ошибка",
                callback_data=f"modai:wrong:{alert_id}",
            )
        ],
        [
            InlineKeyboardButton(
                "🔨 Бан",
                callback_data=f"modai:ban:{alert_id}",
            )
        ],
        [
            InlineKeyboardButton(
                "🔇 Оставить мут",
                callback_data=f"modai:mute:{alert_id}",
            )
        ],
    ])

    await bot.send_message(
        chat_id=chat_id,
        text=text,
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard,
        disable_web_page_preview=True,
    )


# ============================================================
# SAVE VIOLATION
# ============================================================

async def _save_violation(
    *,
    bot,
    chat_id: int,
    user_id: int,
    message_id: int,
    message_text: str,
    toxic_score: float,
    first_name: str,
    username: str | None,
) -> None:

    now = int(time.time())
    cutoff = now - WINDOW_SECONDS

    lock = _user_lock(chat_id, user_id)

    async with lock:

        inserted = await db._exec(
            """
            INSERT OR IGNORE INTO ai_moderation_violations
            (
                chat_id,
                user_id,
                message_id,
                message_text,
                toxic_score,
                created_at
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            [
                chat_id,
                user_id,
                message_id,
                message_text[:MAX_TEXT_LENGTH],
                toxic_score,
                now,
            ],
        )

        if getattr(inserted, "error", False):
            logger.error(
                "Failed to save violation user=%s",
                user_id,
            )
            return

        if int(
            getattr(inserted, "rows_affected", 0) or 0
        ) == 0:
            return

        # Удаляем старые нарушения.
        await db._exec(
            """
            DELETE FROM ai_moderation_violations
            WHERE created_at < ?
            """,
            [cutoff],
        )

        count_rs = await db._exec(
            """
            SELECT COUNT(*)
            FROM ai_moderation_violations
            WHERE chat_id = ?
              AND user_id = ?
              AND created_at >= ?
            """,
            [chat_id, user_id, cutoff],
        )

        if (
            getattr(count_rs, "error", False)
            or not count_rs.rows
        ):
            return

        count = int(count_rs.rows[0][0] or 0)

        logger.info(
            "AI violation %s/%s user=%s score=%.2f%%",
            count,
            VIOLATION_THRESHOLD,
            user_id,
            toxic_score * 100,
        )

        if count < VIOLATION_THRESHOLD:
            return

        # Проверяем, нет ли уже открытой карточки.
        pending_rs = await db._exec(
            """
            SELECT alert_id
            FROM ai_moderation_alerts
            WHERE chat_id = ?
              AND user_id = ?
              AND status = 'pending'
            LIMIT 1
            """,
            [chat_id, user_id],
        )

        if pending_rs.rows:
            await db._exec(
                """
                UPDATE ai_moderation_alerts
                SET violation_count = ?
                WHERE alert_id = ?
                """,
                [
                    count,
                    pending_rs.rows[0][0],
                ],
            )

            return

        alert_id = secrets.token_hex(6)

        created = await db._exec(
            """
            INSERT INTO ai_moderation_alerts
            (
                alert_id,
                chat_id,
                user_id,
                violation_count,
                status,
                auto_muted,
                created_at,
                muted_until
            )
            VALUES (?, ?, ?, ?, 'pending', 0, ?, NULL)
            """,
            [
                alert_id,
                chat_id,
                user_id,
                count,
                now,
            ],
        )

        if getattr(created, "error", False):
            logger.error(
                "Failed to create moderation alert user=%s",
                user_id,
            )
            return

        # Сначала карточка в БД, затем ограничение пользователя.
        auto_muted = await _try_mute(
            bot,
            chat_id,
            user_id,
        )

        muted_until = (
            now + MUTE_SECONDS
            if auto_muted
            else None
        )

        await db._exec(
            """
            UPDATE ai_moderation_alerts
            SET auto_muted = ?,
                muted_until = ?
            WHERE alert_id = ?
            """,
            [
                1 if auto_muted else 0,
                muted_until,
                alert_id,
            ],
        )

        try:
            await _notify_admin(
                bot,
                alert_id=alert_id,
                chat_id=chat_id,
                user_id=user_id,
                first_name=first_name,
                username=username,
                count=count,
                auto_muted=auto_muted,
                toxic_score=toxic_score,
            )

        except TelegramError:
            logger.exception(
                "Failed to send moderation notification"
            )


# ============================================================
# PROCESS MESSAGE
# ============================================================

async def _process_message(
    message,
    bot,
) -> None:

    text = (
        message.text
        or message.caption
        or ""
    ).strip()

    if not text:
        return

    result = await _classify(text)

    if not result:
        return

    try:
        classification = int(result.get("class", 0))

        if classification not in (0, 1):
            logger.warning(
                "Unknown Harmony class: %r",
                classification,
            )
            return

        toxic_score = _parse_score(
            result.get("toxic_score", 0)
        )

    except (TypeError, ValueError):
        logger.warning(
            "Invalid Harmony result: %r",
            result,
        )
        return

    logger.info(
        "Harmony: user=%s message=%s class=%s "
        "toxic=%.2f%% latency=%s",
        message.from_user.id if message.from_user else None,
        message.message_id,
        classification,
        toxic_score * 100,
        result.get("latency_ms"),
    )

    if classification != 1:
        return

    if not message.from_user:
        return

    await _save_violation(
        bot=bot,
        chat_id=message.chat_id,
        user_id=message.from_user.id,
        message_id=message.message_id,
        message_text=text,
        toxic_score=toxic_score,
        first_name=message.from_user.first_name or "Пользователь",
        username=message.from_user.username,
    )


# ============================================================
# MAIN MESSAGE HANDLER
# ============================================================

async def check_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """
    Регистрируется как обычный message handler.
    Никаких запросов к Harmony при каждом сообщении нет.
    """

    message = update.effective_message
    chat = update.effective_chat
    user = update.effective_user

    if not message or not chat or not user:
        return

    if user.is_bot:
        return

    if chat.type not in (
        ChatType.GROUP,
        ChatType.SUPERGROUP,
    ):
        return

    if CHAT_ID and chat.id != CHAT_ID:
        return

    # Не проверяем команды.
    if message.text and message.text.startswith("/"):
        return

    text = (
        message.text
        or message.caption
        or ""
    ).strip()

    if not text:
        return

    if not API_TOKEN:
        return

    # Самое важное: локальная проверка.
    # Обычные сообщения вообще не отправляются модели.
    if not _is_candidate(text):
        return

    # Запускаем очередь, если она ещё не запущена.
    if _queue is None or not _workers:
        _start_workers()

    key = (message.chat_id, message.message_id)

    if key in _queued_messages:
        return

    _queued_messages.add(key)

    try:
        _queue.put_nowait(
            (message, context.bot)
        )

        logger.debug(
            "Candidate queued: user=%s message=%s queue=%s",
            user.id,
            message.message_id,
            _queue.qsize(),
        )

    except asyncio.QueueFull:
        _queued_messages.discard(key)

        logger.warning(
            "AI moderation queue full. "
            "Message skipped: user=%s message=%s",
            user.id,
            message.message_id,
        )


# ============================================================
# ADMIN CALLBACKS
# ============================================================

async def moderation_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:

    query = update.callback_query

    if not query or not query.data:
        return

    if not ADMIN_ID or query.from_user.id != ADMIN_ID:
        await query.answer(
            "Эта кнопка доступна только администратору.",
            show_alert=True,
        )
        return

    try:
        _, action, alert_id = query.data.split(":", 2)

    except ValueError:
        await query.answer(
            "Некорректная кнопка.",
            show_alert=True,
        )
        return

    if action not in ("wrong", "ban", "mute"):
        await query.answer(
            "Неизвестное действие.",
            show_alert=True,
        )
        return

    rs = await db._exec(
        """
        SELECT chat_id, user_id, status, auto_muted
        FROM ai_moderation_alerts
        WHERE alert_id = ?
        LIMIT 1
        """,
        [alert_id],
    )

    if (
        getattr(rs, "error", False)
        or not rs.rows
    ):
        await query.answer(
            "Карточка не найдена.",
            show_alert=True,
        )
        return

    chat_id, user_id, status, auto_muted = rs.rows[0]

    chat_id = int(chat_id)
    user_id = int(user_id)

    if status != "pending":
        await query.answer(
            "Это предупреждение уже обработано.",
            show_alert=True,
        )
        return

    lock = _user_lock(chat_id, user_id)

    async with lock:
        # Повторно проверяем статус под блокировкой.
        current = await db._exec(
            """
            SELECT status, auto_muted
            FROM ai_moderation_alerts
            WHERE alert_id = ?
            LIMIT 1
            """,
            [alert_id],
        )

        if not current.rows or current.rows[0][0] != "pending":
            await query.answer(
                "Карточка уже обработана.",
                show_alert=True,
            )
            return

        auto_muted = int(current.rows[0][1] or 0)

        try:
            if action == "wrong":

                if auto_muted:
                    await context.bot.restrict_chat_member(
                        chat_id=chat_id,
                        user_id=user_id,
                        permissions=_permissions_unmuted(),
                    )

                await db._exec(
                    """
                    DELETE FROM ai_moderation_violations
                    WHERE chat_id = ?
                      AND user_id = ?
                      AND created_at >= ?
                    """,
                    [
                        chat_id,
                        user_id,
                        int(time.time()) - WINDOW_SECONDS,
                    ],
                )

                await db._exec(
                    """
                    UPDATE ai_moderation_alerts
                    SET status = 'false_positive'
                    WHERE alert_id = ?
                    """,
                    [alert_id],
                )

                result_text = (
                    "❌ Администратор отменил срабатывание. "
                    "Нарушения за последние 2 часа очищены."
                )

            elif action == "ban":

                await context.bot.ban_chat_member(
                    chat_id=chat_id,
                    user_id=user_id,
                )

                await db._exec(
                    """
                    UPDATE ai_moderation_alerts
                    SET status = 'banned'
                    WHERE alert_id = ?
                    """,
                    [alert_id],
                )

                result_text = (
                    "🔨 Пользователь заблокирован администратором."
                )

            elif action == "mute":

                until = (
                    datetime.now(timezone.utc)
                    + timedelta(seconds=MUTE_SECONDS)
                )

                await context.bot.restrict_chat_member(
                    chat_id=chat_id,
                    user_id=user_id,
                    permissions=_permissions_muted(),
                    until_date=until,
                )

                await db._exec(
                    """
                    UPDATE ai_moderation_alerts
                    SET status = 'muted',
                        auto_muted = 1,
                        muted_until = ?
                    WHERE alert_id = ?
                    """,
                    [
                        int(time.time()) + MUTE_SECONDS,
                        alert_id,
                    ],
                )

                result_text = (
                    "🔇 Мут на 24 часа подтверждён администратором."
                )

            await query.answer(
                "Решение сохранено."
            )

            if query.message:
                old_text = (
                    query.message.text_html
                    or query.message.text
                    or ""
                )

                await query.edit_message_text(
                    f"{old_text}\n\n"
                    f"<b>{html.escape(result_text)}</b>",
                    parse_mode=ParseMode.HTML,
                    reply_markup=None,
                    disable_web_page_preview=True,
                )

        except TelegramError:
            logger.exception(
                "Moderation action failed: %s",
                action,
            )

            await query.answer(
                "Telegram не выполнил действие. "
                "Проверь права бота.",
                show_alert=True,
            )
