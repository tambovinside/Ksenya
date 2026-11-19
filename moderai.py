
"""
Ksenya AI Security — многоступенчатая AI-модерация Telegram.

Возможности:
- Каждое текстовое сообщение и подпись проверяются Harmony.
- Локального фильтра-кандидата нет.
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
import secrets
import time

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

# Засчитываем нарушение только при высокой уверенности Harmony.
# Низкая уверенность часто соответствует отдельному мату без оскорбления/травли.
MIN_TOXIC_SCORE = 0.80

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
# MESSAGE PREPARATION
# ============================================================

# Локального фильтра-кандидата больше нет: каждое текстовое сообщение
# и каждая подпись к медиа отправляются в Harmony.


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

    # Не считаем class=1 достаточным основанием: модель может пометить
    # отдельное ругательство как токсичное без реального оскорбления.
    # Нарушение засчитывается только при class=1 и уверенности >= порога.
    if classification != 1 or toxic_score < MIN_TOXIC_SCORE:
        logger.info(
            "Harmony ignored as low-confidence/non-violation: "
            "user=%s message=%s class=%s score=%.2f%% threshold=%.2f%%",
            message.from_user.id if message.from_user else None,
            message.message_id,
            classification,
            toxic_score * 100,
            MIN_TOXIC_SCORE * 100,
        )
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
    Каждое текстовое сообщение и подпись отправляются в очередь Harmony.
    Команды также проверяются, если они попадают в этот handler.
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

    text = (
        message.text
        or message.caption
        or ""
    ).strip()

    if not text:
        return

    if not API_TOKEN:
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
            "Message queued for Harmony: user=%s message=%s queue=%s",
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
