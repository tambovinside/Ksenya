"""AI-модерация чата через внешний Harmony API.

Проверяет только сообщения, которые прошли локальный фильтр. Классификации
"class=1" накапливаются в БД за последние 2 часа. На пороге пользователь
получает автоматический мут на 24 часа, а в чат отправляется карточка для
решения администратора.
"""

import asyncio
import html
import logging
import os
import re
import secrets
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import aiohttp
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatType, ParseMode
from telegram.error import TelegramError
from telegram.ext import ContextTypes
from telegram import ChatPermissions

import db

logger = logging.getLogger("moderai")

API_URL = "https://tambovinside.onrender.com/check"
API_TOKEN = os.getenv("AImoder", "").strip()
ADMIN_ID = int(os.getenv("ADMIN_ID", "0") or 0)
CHAT_ID = int(os.getenv("CHAT_ID", "0") or 0)
ADMIN_MENTION = "@YouWarm"

# Пять классификаций class=1 за последние два часа.
VIOLATION_THRESHOLD = 5
WINDOW_SECONDS = 2 * 60 * 60
MUTE_SECONDS = 24 * 60 * 60
REQUEST_TIMEOUT_SECONDS = 10
MAX_PARALLEL_REQUESTS = 3

# Лёгкий локальный фильтр: модель не вызывается для обычных сообщений.
# Отдельные короткие оскорбления тоже проходят фильтр.
_SUSPICIOUS_RE = re.compile(
    r"(?iu)(?<![а-яё])(?:"
    r"бля\w*|бляд\w*|сука|суки|сукой|сучк\w*|"
    r"уеб\w*|уёб\w*|ебан\w*|ебл\w*|заеб\w*|"
    r"пизд\w*|ху[йеяи]\w*|нахуй|нахуя|хуй\w*|"
    r"мраз\w*|урод\w*|дебил\w*|идиот\w*|"
    r"тупиц\w*|туп(?:ой|ая|ое|ые|ица|ица)\b|"
    r"муд[ао]\w*|гандон\w*|чмо\b|шлюх\w*|"
    r"коз[её]л\w*|ублюд\w*|говн\w*|сволоч\w*"
    r")(?![а-яё])"
)

_session: aiohttp.ClientSession | None = None
_request_semaphore = asyncio.Semaphore(MAX_PARALLEL_REQUESTS)
_background_tasks: set[asyncio.Task] = set()
_user_locks: dict[tuple[int, int], asyncio.Lock] = {}


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


async def init_db() -> None:
    """Создаёт таблицы модерации в той же Turso БД, что и основной бот."""
    await db._exec_required(
        """CREATE TABLE IF NOT EXISTS ai_moderation_violations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            message_id INTEGER NOT NULL,
            message_text TEXT,
            toxic_score REAL,
            created_at INTEGER NOT NULL,
            UNIQUE(chat_id, message_id)
        )"""
    )
    await db._exec_required(
        """CREATE INDEX IF NOT EXISTS idx_ai_mod_user_window
           ON ai_moderation_violations(chat_id, user_id, created_at)"""
    )
    await db._exec_required(
        """CREATE TABLE IF NOT EXISTS ai_moderation_alerts (
            alert_id TEXT PRIMARY KEY,
            chat_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            violation_count INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            auto_muted INTEGER NOT NULL DEFAULT 0,
            created_at INTEGER NOT NULL,
            muted_until INTEGER
        )"""
    )
    await db._exec_required(
        """CREATE UNIQUE INDEX IF NOT EXISTS uq_ai_mod_pending_alert
           ON ai_moderation_alerts(chat_id, user_id)
           WHERE status = 'pending'"""
    )
    if not API_TOKEN:
        logger.warning("AImoder не задан в Render Environment — AI-модерация выключена")
    else:
        logger.info(
            "AI moderation initialized: chat_id=%s threshold=%s window=%ss",
            CHAT_ID, VIOLATION_THRESHOLD, WINDOW_SECONDS,
        )


async def _get_session() -> aiohttp.ClientSession:
    global _session
    if _session is None or _session.closed:
        timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_SECONDS, connect=4)
        connector = aiohttp.TCPConnector(limit=MAX_PARALLEL_REQUESTS, ttl_dns_cache=300)
        _session = aiohttp.ClientSession(timeout=timeout, connector=connector)
    return _session


async def close() -> None:
    global _session
    tasks = list(_background_tasks)
    if tasks:
        try:
            await asyncio.wait_for(
                asyncio.gather(*tasks, return_exceptions=True), timeout=12
            )
        except asyncio.TimeoutError:
            for task in tasks:
                task.cancel()
    if _session and not _session.closed:
        await _session.close()
    _session = None


def _is_candidate(text: str) -> bool:
    return bool(text and _SUSPICIOUS_RE.search(text))


async def _classify(text: str) -> dict[str, Any] | None:
    if not API_TOKEN:
        return None
    session = await _get_session()
    headers = {"X-API-Token": API_TOKEN}
    # Сервер принимает максимум 1000 символов.
    payload = {"text": text[:1000]}
    async with _request_semaphore:
        try:
            async with session.post(API_URL, json=payload, headers=headers) as response:
                if response.status != 200:
                    body = (await response.text())[:300]
                    logger.warning("Harmony API HTTP %s: %s", response.status, body)
                    return None
                result = await response.json(content_type=None)
                if not isinstance(result, dict) or "class" not in result:
                    logger.warning("Harmony API вернул неожиданный ответ: %r", result)
                    return None
                return result
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
            logger.exception("Не удалось обратиться к Harmony API")
            return None


def _user_lock(chat_id: int, user_id: int) -> asyncio.Lock:
    key = (chat_id, user_id)
    lock = _user_locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _user_locks[key] = lock
    return lock


async def _try_mute(bot, chat_id: int, user_id: int) -> bool:
    until = datetime.now(timezone.utc) + timedelta(seconds=MUTE_SECONDS)
    try:
        await bot.restrict_chat_member(
            chat_id=chat_id,
            user_id=user_id,
            permissions=_permissions_muted(),
            until_date=until,
        )
        return True
    except TelegramError:
        logger.exception("Не удалось автоматически замутить user_id=%s", user_id)
        return False


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
    shown_name = f"@{username}" if username else (first_name or "Пользователь")
    user_link = (
        f'<a href="tg://user?id={user_id}">{html.escape(shown_name)}</a>'
    )
    mute_line = (
        "🔇 Автоматический мут на 24 часа уже применён."
        if auto_muted
        else "⚠️ Автоматический мут не применился — проверьте права бота."
    )
    text = (
        "⚠️ <b>Система безопасности обнаружила нарушителя</b>\n\n"
        f"Пользователь: {user_link}\n"
        f"Нарушений за 2 часа: <b>{count}</b> из {VIOLATION_THRESHOLD}\n"
        f"Последняя токсичность: <b>{toxic_score:.2f}%</b>\n"
        f"Модель: <code>Ksenya AI Security</code>\n"
        f"{mute_line}\n\n"
        f"Ожидаем решение {ADMIN_MENTION}"
    )
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("Ошибка", callback_data=f"modai:wrong:{alert_id}")],
        [InlineKeyboardButton("Бан", callback_data=f"modai:ban:{alert_id}")],
        [InlineKeyboardButton("Оставить мут", callback_data=f"modai:mute:{alert_id}")],
    ])
    await bot.send_message(
        chat_id=chat_id,
        text=text,
        parse_mode=ParseMode.HTML,
        reply_markup=keyboard,
        disable_web_page_preview=True,
    )


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
            """INSERT OR IGNORE INTO ai_moderation_violations
               (chat_id, user_id, message_id, message_text, toxic_score, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            [chat_id, user_id, message_id, message_text[:1000], toxic_score, now],
        )
        if getattr(inserted, "error", False):
            logger.error("Не удалось сохранить AI-нарушение user_id=%s", user_id)
            return
        if int(getattr(inserted, "rows_affected", 0) or 0) == 0:
            return  # повторная обработка одного и того же Telegram message_id

        await db._exec(
            "DELETE FROM ai_moderation_violations WHERE created_at < ?",
            [cutoff],
        )
        count_rs = await db._exec(
            """SELECT COUNT(*) FROM ai_moderation_violations
               WHERE chat_id = ? AND user_id = ? AND created_at >= ?""",
            [chat_id, user_id, cutoff],
        )
        if getattr(count_rs, "error", False) or not count_rs.rows:
            return
        count = int(count_rs.rows[0][0] or 0)
        if count < VIOLATION_THRESHOLD:
            logger.info(
                "AI violation %s/%s: user_id=%s chat_id=%s",
                count, VIOLATION_THRESHOLD, user_id, chat_id,
            )
            return

        pending_rs = await db._exec(
            """SELECT alert_id FROM ai_moderation_alerts
               WHERE chat_id = ? AND user_id = ? AND status = 'pending' LIMIT 1""",
            [chat_id, user_id],
        )
        if pending_rs.rows:
            await db._exec(
                "UPDATE ai_moderation_alerts SET violation_count = ? WHERE alert_id = ?",
                [count, pending_rs.rows[0][0]],
            )
            return

        alert_id = secrets.token_hex(6)
        created = await db._exec(
            """INSERT INTO ai_moderation_alerts
               (alert_id, chat_id, user_id, violation_count, status, auto_muted, created_at, muted_until)
               VALUES (?, ?, ?, ?, 'pending', 0, ?, NULL)""",
            [alert_id, chat_id, user_id, count, now],
        )
        if getattr(created, "error", False):
            logger.exception("Не удалось создать карточку AI-модерации")
            return

        # Сначала сохраняем карточку, и только затем применяем мут: даже если
        # Telegram не даст ограничить пользователя, у администратора останутся кнопки.
        auto_muted = await _try_mute(bot, chat_id, user_id)
        muted_until = now + MUTE_SECONDS if auto_muted else None
        await db._exec(
            "UPDATE ai_moderation_alerts SET auto_muted = ?, muted_until = ? WHERE alert_id = ?",
            [1 if auto_muted else 0, muted_until, alert_id],
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
            logger.exception("Не удалось отправить уведомление AI-модерации")


async def _process_message(message, bot) -> None:
    text = (message.text or message.caption or "").strip()
    result = await _classify(text)
    if not result:
        return

    try:
        is_toxic = int(result.get("class", 0)) == 1
        toxic_score = float(result.get("toxic_score", 0.0))
    except (TypeError, ValueError):
        logger.warning("Некорректные поля ответа Harmony: %r", result)
        return

    logger.info(
        "Harmony check user_id=%s message_id=%s class=%s toxic=%.2f%% latency=%s ms",
        message.from_user.id if message.from_user else None,
        message.message_id,
        result.get("class"),
        toxic_score * 100,
        result.get("latency_ms"),
    )
    if not is_toxic or not message.from_user:
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


async def check_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handler: быстро отбирает кандидатов и проверяет их в фоне."""
    message = update.effective_message
    chat = update.effective_chat
    user = update.effective_user
    if not message or not chat or not user or user.is_bot:
        return
    if chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        return
    if CHAT_ID and chat.id != CHAT_ID:
        return
    if message.text and message.text.startswith("/"):
        return

    text = (message.text or message.caption or "").strip()
    if not text or not _is_candidate(text):
        return
    if not API_TOKEN:
        return

    task = asyncio.create_task(_process_message(message, context.bot))
    _background_tasks.add(task)

    def _task_done(done: asyncio.Task) -> None:
        _background_tasks.discard(done)
        try:
            done.result()
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("Фоновая задача AI-модерации завершилась с ошибкой")

    task.add_done_callback(_task_done)


async def moderation_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    query = update.callback_query
    if not query or not query.data:
        return
    if not ADMIN_ID or query.from_user.id != ADMIN_ID:
        await query.answer("Эта кнопка доступна только администратору.", show_alert=True)
        return

    try:
        _, action, alert_id = query.data.split(":", 2)
    except ValueError:
        await query.answer("Некорректная кнопка.", show_alert=True)
        return

    rs = await db._exec(
        """SELECT chat_id, user_id, status, auto_muted
           FROM ai_moderation_alerts WHERE alert_id = ? LIMIT 1""",
        [alert_id],
    )
    if getattr(rs, "error", False) or not rs.rows:
        await query.answer("Карточка не найдена.", show_alert=True)
        return

    chat_id, user_id, status, auto_muted = rs.rows[0]
    chat_id, user_id = int(chat_id), int(user_id)
    if status != "pending":
        await query.answer("Это предупреждение уже обработано.", show_alert=True)
        return

    try:
        if action == "wrong":
            if int(auto_muted or 0):
                await context.bot.restrict_chat_member(
                    chat_id=chat_id,
                    user_id=user_id,
                    permissions=_permissions_unmuted(),
                )
            await db._exec(
                "DELETE FROM ai_moderation_violations WHERE chat_id = ? AND user_id = ? AND created_at >= ?",
                [chat_id, user_id, int(time.time()) - WINDOW_SECONDS],
            )
            await db._exec(
                "UPDATE ai_moderation_alerts SET status = 'false_positive' WHERE alert_id = ?",
                [alert_id],
            )
            result_text = "❌ Администратор отменил срабатывание. Нарушения за последние 2 часа очищены."
        elif action == "ban":
            await context.bot.ban_chat_member(chat_id=chat_id, user_id=user_id)
            await db._exec(
                "UPDATE ai_moderation_alerts SET status = 'banned' WHERE alert_id = ?",
                [alert_id],
            )
            result_text = "🔨 Пользователь заблокирован администратором."
        elif action == "mute":
            until = datetime.now(timezone.utc) + timedelta(seconds=MUTE_SECONDS)
            await context.bot.restrict_chat_member(
                chat_id=chat_id,
                user_id=user_id,
                permissions=_permissions_muted(),
                until_date=until,
            )
            await db._exec(
                "UPDATE ai_moderation_alerts SET status = 'muted', auto_muted = 1, muted_until = ? WHERE alert_id = ?",
                [int(time.time()) + MUTE_SECONDS, alert_id],
            )
            result_text = "🔇 Мут на 24 часа подтверждён администратором."
        else:
            await query.answer("Неизвестное действие.", show_alert=True)
            return

        await query.answer("Решение сохранено.")
        if query.message:
            old_text = query.message.text_html or query.message.text or ""
            await query.edit_message_text(
                f"{old_text}\n\n<b>{html.escape(result_text)}</b>",
                parse_mode=ParseMode.HTML,
                reply_markup=None,
                disable_web_page_preview=True,
            )
    except TelegramError:
        logger.exception("Ошибка выполнения действия AI-модерации: %s", action)
        await query.answer("Telegram не выполнил действие. Проверь права бота.", show_alert=True)
