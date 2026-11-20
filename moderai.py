
"""
Ksenya AI Security — многоступенчатая AI-модерация Telegram.

Возможности:
- Текст и подписи проверяются Harmony с коротким контекстом переписки.
- Мат и взрослые шутки сами по себе не должны считаться нарушением.
- Ограниченная очередь сообщений.
- Один одновременный запрос Harmony, чтобы не перегружать API.
- Увеличенный таймаут для холодного старта Render и защита от зависших запросов.
- Сохранение нарушений в Turso.
- 3 нарушения за 1 час = мут на 24 часа.
- Спокойные служебные карточки отправляются администратору в личные сообщения.
"""

import asyncio
import html
from collections import defaultdict, deque
import logging
import os
import secrets
import time
import io
import re
import subprocess
import tempfile
from pathlib import Path

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
IMAGE_API_URL = "https://tambovinside.onrender.com/check-image"

API_TOKEN = os.getenv("AImoder", "").strip()

ADMIN_ID = int(os.getenv("ADMIN_ID", "0") or 0)
CHAT_ID = int(os.getenv("CHAT_ID", "0") or 0)

ADMIN_MENTION = "@YouWarm"

VIOLATION_THRESHOLD = 3

# Засчитываем нарушение только при высокой уверенности Harmony.
# Низкая уверенность часто соответствует отдельному мату без оскорбления/травли.
MIN_TOXIC_SCORE = 0.80

WINDOW_SECONDS = 60 * 60
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

# Сохраняем небольшой контекст последних сообщений группы в памяти процесса.
# Он передаётся модели вместе с текущим сообщением, чтобы отличать ответную
# шутку/подкол от направленной травли или эскалации конфликта.
CONTEXT_MESSAGES = 4
_recent_messages: dict[int, deque] = defaultdict(
    lambda: deque(maxlen=CONTEXT_MESSAGES)
)

# Очередь не позволяет создавать бесконечное количество задач.
_queue: asyncio.Queue | None = None

_workers: set[asyncio.Task] = set()

_session: aiohttp.ClientSession | None = None

_request_semaphore = asyncio.Semaphore(MAX_PARALLEL_REQUESTS)

_user_locks: dict[tuple[int, int], asyncio.Lock] = {}

# Защита от повторного добавления одного сообщения.
_queued_messages: set[tuple[int, int]] = set()
_queued_media: set[tuple[int, int]] = set()
MAX_MEDIA_QUEUE_SIZE = 30
_media_queue: asyncio.Queue | None = None
_media_worker: asyncio.Task | None = None


# ============================================================
# MESSAGE PREPARATION
# ============================================================

# Проверяем текст и подписи с небольшим контекстом предыдущих сообщений.


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
            category TEXT NOT NULL DEFAULT 'TOXIC',
            media_type TEXT,
            UNIQUE(chat_id, message_id)
        )
        """
    )

    # Миграция уже существующей таблицы без потери старых нарушений.
    columns_rs = await db._exec("PRAGMA table_info(ai_moderation_violations)")
    existing_columns = {str(row[1]) for row in (columns_rs.rows or []) if len(row) > 1}
    if "category" not in existing_columns:
        await db._exec_required(
            "ALTER TABLE ai_moderation_violations ADD COLUMN category TEXT NOT NULL DEFAULT 'TOXIC'"
        )
    if "media_type" not in existing_columns:
        await db._exec_required(
            "ALTER TABLE ai_moderation_violations ADD COLUMN media_type TEXT"
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
    _start_media_worker()

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


def _start_media_worker() -> None:
    global _media_queue, _media_worker
    if _media_queue is None:
        _media_queue = asyncio.Queue(maxsize=MAX_MEDIA_QUEUE_SIZE)
    if _media_worker is None or _media_worker.done():
        _media_worker = asyncio.create_task(_media_worker_loop())


async def _media_worker_loop() -> None:
    while True:
        item = await _media_queue.get()
        try:
            message, bot = item
            await _process_media_message(message, bot)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Ошибка проверки изображения/видео AI-модерацией")
        finally:
            if item:
                message = item[0]
                _queued_media.discard((message.chat_id, message.message_id))
            _media_queue.task_done()


async def _worker_loop() -> None:
    while True:
        item = await _queue.get()

        try:
            message, bot, context_text = item
            await _process_message(message, bot, context_text)

        except asyncio.CancelledError:
            raise

        except Exception:
            logger.exception("Ошибка обработки сообщения AI-модерацией")

        finally:
            if item:
                message = item[0]
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
            await asyncio.wait_for(_queue.join(), timeout=5)
        except asyncio.TimeoutError:
            logger.warning("Очередь AI-модерации не успела полностью обработаться")

    if _media_queue is not None:
        try:
            await asyncio.wait_for(_media_queue.join(), timeout=5)
        except asyncio.TimeoutError:
            logger.warning("Очередь медиа-модерации не успела обработаться")

    global _media_worker
    if _media_worker is not None:
        _media_worker.cancel()
        await asyncio.gather(_media_worker, return_exceptions=True)
        _media_worker = None

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


async def _classify_images(images: list[tuple[str, bytes]]) -> dict[str, Any] | None:
    """Отправляет один или несколько кадров на отдельный endpoint классификатора."""
    if not API_TOKEN or not images:
        return None
    session = await _get_session()
    headers = {"X-API-Token": API_TOKEN}
    form = aiohttp.FormData()
    for filename, content in images:
        form.add_field("files", content, filename=filename, content_type="image/jpeg")
    async with _request_semaphore:
        try:
            image_timeout = aiohttp.ClientTimeout(total=180, connect=80, sock_read=150)
            async with session.post(IMAGE_API_URL, data=form, headers=headers, timeout=image_timeout) as response:
                if response.status != 200:
                    body = (await response.text())[:300]
                    logger.warning("Image classifier HTTP %s: %s", response.status, body)
                    return None
                result = await response.json(content_type=None)
                return result if isinstance(result, dict) else None
        except asyncio.TimeoutError:
            logger.warning("Image classifier request timeout")
        except (aiohttp.ClientError, ValueError):
            logger.exception("Image classifier request failed")
        except Exception:
            logger.exception("Unexpected image classifier error")
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
    """Применяет мут одним основным запросом без предварительных сетевых проверок.

    Это не задерживает модерацию двумя дополнительными get_chat_member.
    Временные сетевые ошибки и RetryAfter повторяются до трёх раз.
    """
    until = datetime.now(timezone.utc) + timedelta(seconds=MUTE_SECONDS)

    for attempt in range(1, 4):
        try:
            result = await bot.restrict_chat_member(
                chat_id=chat_id,
                user_id=user_id,
                permissions=_permissions_muted(),
                until_date=until,
            )
            if result is False:
                logger.error(
                    "Telegram returned false for mute | chat=%s user=%s attempt=%s",
                    chat_id, user_id, attempt,
                )
                return False

            logger.info(
                "Automatic mute applied successfully | chat=%s user=%s duration=%ss attempt=%s",
                chat_id, user_id, MUTE_SECONDS, attempt,
            )
            return True

        except TelegramError as error:
            error_name = type(error).__name__
            retry_after = getattr(error, "retry_after", None)
            is_temporary = (
                error_name in ("TimedOut", "NetworkError", "RetryAfter", "TelegramRetryAfter")
                or retry_after is not None
            )
            logger.error(
                "Mute request failed | chat=%s user=%s attempt=%s/3 error_type=%s error=%s",
                chat_id, user_id, attempt, error_name, error,
                exc_info=True,
            )
            if not is_temporary or attempt >= 3:
                return False
            delay = float(retry_after) if retry_after is not None else min(2 ** (attempt - 1), 5)
            await asyncio.sleep(max(0.5, min(delay, 30)))

        except Exception:
            logger.exception(
                "Unexpected error while applying mute | chat=%s user=%s attempt=%s/3",
                chat_id, user_id, attempt,
            )
            if attempt >= 3:
                return False
            await asyncio.sleep(min(2 ** (attempt - 1), 5))

    return False

# ============================================================
# CONFIRMED CONTENT REMOVAL + GROUP NOTICE
# ============================================================

async def _delete_confirmed_violations(
    bot,
    chat_id: int,
    user_id: int,
) -> tuple[int, int]:
    """Удаляет только сообщения, которые ИИ записал как нарушения.

    Вызывается исключительно после успешного подтверждения админом мута
    или бана. Сообщения за пределами текущего окна не затрагиваются.
    """
    cutoff = int(time.time()) - WINDOW_SECONDS
    rs = await db._exec(
        """
        SELECT message_id
        FROM ai_moderation_violations
        WHERE chat_id = ?
          AND user_id = ?
          AND created_at >= ?
          AND media_type IN ('photo', 'sticker', 'video')
        ORDER BY created_at DESC
        """,
        [chat_id, user_id, cutoff],
    )

    if getattr(rs, "error", False):
        logger.error(
            "Could not load confirmed violation messages for deletion: user=%s",
            user_id,
        )
        return 0, 0

    message_ids = list(dict.fromkeys(
        int(row[0]) for row in (rs.rows or []) if row and row[0] is not None
    ))
    deleted = 0
    failed = 0

    for message_id in message_ids:
        try:
            await bot.delete_message(
                chat_id=chat_id,
                message_id=message_id,
            )
            deleted += 1
        except TelegramError:
            failed += 1
            logger.warning(
                "Could not delete confirmed violating message chat=%s message=%s",
                chat_id,
                message_id,
                exc_info=True,
            )

    logger.info(
        "Confirmed violating messages cleanup: user=%s deleted=%s failed=%s",
        user_id,
        deleted,
        failed,
    )
    return deleted, failed


async def _get_violation_reason(chat_id: int, user_id: int) -> str:
    rs = await db._exec(
        """
        SELECT DISTINCT category
        FROM ai_moderation_violations
        WHERE chat_id = ? AND user_id = ? AND created_at >= ?
        """,
        [chat_id, user_id, int(time.time()) - WINDOW_SECONDS],
    )
    categories = {
        part
        for row in (rs.rows or []) if row and row[0]
        for part in str(row[0]).upper().split("+")
    }
    labels = []
    if "NSFL" in categories:
        labels.append("NSFL — шокирующий контент и жестокость")
    if "NSFW" in categories:
        labels.append("NSFW — сексуальный контент или нагота")
    if "TOXIC" in categories:
        labels.append("оскорбления или травля")
    return "; ".join(labels) if labels else "нарушение правил сообщества"


async def _notify_group_action(
    bot,
    *,
    chat_id: int,
    user_id: int,
    action: str,
    deleted: int,
    failed: int,
    reason: str,
    include_cleanup: bool = True,
) -> None:
    """Публично сообщает группе о применённой мере модерации."""
    user_link = f'<a href="tg://user?id={user_id}">Участник</a>'

    if action == "ban":
        heading = f"🚫 {user_link} заблокирован. Причина: {html.escape(reason)}."
    else:
        heading = f"🔇 {user_link} получил мут на 24 часа. Причина: {html.escape(reason)}."

    cleanup = ""
    if include_cleanup:
        if deleted and not failed:
            cleanup = f"\nУдалено медиа-сообщений с нарушениями: {deleted}."
        elif deleted and failed:
            cleanup = (
                f"\nУдалось удалить сообщений: {deleted}; "
                f"не удалось удалить медиа-сообщений: {failed}."
            )
        elif failed:
            cleanup = (
                "\nНе удалось удалить отмеченные сообщения. "
                "Проверьте, есть ли у бота право удалять сообщения."
            )
        else:
            cleanup = "\nОтмеченные сообщения для удаления не найдены."

    try:
        await bot.send_message(
            chat_id=chat_id,
            text=f"{heading}{cleanup}",
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
    except TelegramError:
        logger.exception(
            "Could not send moderation action notice to group chat=%s",
            chat_id,
        )


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
    message_text: str,
) -> None:
    """Отправляет спокойную служебную карточку только администратору в ЛС."""
    if not ADMIN_ID:
        logger.warning("ADMIN_ID не задан: уведомление AI-модерации не отправлено")
        return

    shown_name = f"@{username}" if username else (first_name or "Пользователь")
    user_link = (
        f'<a href="tg://user?id={user_id}">'
        f'{html.escape(shown_name)}'
        f'</a>'
    )
    excerpt = html.escape((message_text or "")[:700]) or "(пустое сообщение)"
    status = "Мут на 24 часа применён." if auto_muted else (
        "Автоматический мут не применился — проверьте права бота."
    )

    text = (
        "<b>Проверка модерации</b>\n"
        f"Участник: {user_link}\n"
        f"Нарушений за 1 час: <b>{count}/{VIOLATION_THRESHOLD}</b>\n"
        f"Оценка модели: <b>{toxic_score * 100:.1f}%</b>\n"
        f"{html.escape(status)}\n\n"
        f"<b>Последнее сообщение:</b>\n<blockquote>{excerpt}</blockquote>"
    )

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("Это ошибка", callback_data=f"modai:wrong:{alert_id}")],
        [InlineKeyboardButton("Заблокировать", callback_data=f"modai:ban:{alert_id}")],
        [InlineKeyboardButton("Продлить мут на 24 часа", callback_data=f"modai:mute:{alert_id}")],
    ])

    try:
        await bot.send_message(
            chat_id=ADMIN_ID,
            text=text,
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard,
            disable_web_page_preview=True,
        )
    except TelegramError:
        logger.exception(
            "Не удалось отправить личное уведомление AI-модерации админу=%s",
            ADMIN_ID,
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
    category: str = "TOXIC",
    media_type: str | None = None,
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
                created_at,
                category,
                media_type
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                chat_id,
                user_id,
                message_id,
                message_text[:MAX_TEXT_LENGTH],
                toxic_score,
                now,
                category,
                media_type,
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
            # Подпись к фото/видео может быть отдельно обработана текстовым
            # обработчиком раньше медиа-воркера. Не считаем одно сообщение
            # вторым нарушением, но обязательно помечаем его как медиа для
            # последующего удаления после подтверждения администратором.
            if media_type in ("photo", "sticker", "video"):
                await db._exec(
                    """
                    UPDATE ai_moderation_violations
                    SET media_type = ?,
                        category = CASE WHEN category = 'TOXIC' THEN ? ELSE category END,
                        message_text = ?,
                        toxic_score = MAX(toxic_score, ?)
                    WHERE chat_id = ? AND message_id = ?
                    """,
                    [media_type, category, message_text[:MAX_TEXT_LENGTH], toxic_score, chat_id, message_id],
                )
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
        # Если карточка существует, НЕ выходим сразу:
        # проверяем, действует ли автоматический мут. Если мут не был
        # применён или уже истёк, пробуем применить его повторно.
        pending_rs = await db._exec(
            """
            SELECT alert_id, auto_muted, muted_until
            FROM ai_moderation_alerts
            WHERE chat_id = ?
              AND user_id = ?
              AND status = 'pending'
            LIMIT 1
            """,
            [chat_id, user_id],
        )

        if getattr(pending_rs, "error", False):
            logger.error(
                "Failed to check pending moderation alert user=%s",
                user_id,
            )
            return

        if pending_rs.rows:
            existing_alert_id, was_auto_muted, old_muted_until = pending_rs.rows[0]
            was_auto_muted = int(was_auto_muted or 0)
            old_muted_until = int(old_muted_until or 0)

            # Не продлеваем мут на каждое сообщение, пока он ещё действует.
            # Но если мут не сработал или срок закончился — пробуем снова.
            mute_is_active = (
                was_auto_muted == 1
                and old_muted_until > now
            )

            if not mute_is_active:
                auto_muted = await _try_mute(
                    bot,
                    chat_id,
                    user_id,
                )

                if auto_muted:
                    muted_until = now + MUTE_SECONDS
                    await db._exec(
                        """
                        UPDATE ai_moderation_alerts
                        SET violation_count = ?,
                            auto_muted = 1,
                            muted_until = ?
                        WHERE alert_id = ?
                        """,
                        [
                            count,
                            muted_until,
                            existing_alert_id,
                        ],
                    )
                    logger.info(
                        "Automatic mute retry succeeded: user=%s alert=%s",
                        user_id,
                        existing_alert_id,
                    )
                    reason = await _get_violation_reason(chat_id, user_id)
                    await _notify_group_action(
                        bot,
                        chat_id=chat_id,
                        user_id=user_id,
                        action="mute",
                        deleted=0,
                        failed=0,
                        reason=reason,
                        include_cleanup=False,
                    )
                else:
                    await db._exec(
                        """
                        UPDATE ai_moderation_alerts
                        SET violation_count = ?,
                            auto_muted = 0,
                            muted_until = NULL
                        WHERE alert_id = ?
                        """,
                        [
                            count,
                            existing_alert_id,
                        ],
                    )
                    logger.error(
                        "Automatic mute retry failed: user=%s alert=%s",
                        user_id,
                        existing_alert_id,
                    )
            else:
                await db._exec(
                    """
                    UPDATE ai_moderation_alerts
                    SET violation_count = ?
                    WHERE alert_id = ?
                    """,
                    [
                        count,
                        existing_alert_id,
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

        if auto_muted:
            reason = await _get_violation_reason(chat_id, user_id)
            await _notify_group_action(
                bot,
                chat_id=chat_id,
                user_id=user_id,
                action="mute",
                deleted=0,
                failed=0,
                reason=reason,
                include_cleanup=False,
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
                message_text=message_text,
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
    context_text: str = "",
) -> None:

    text = (
        message.text
        or message.caption
        or ""
    ).strip()

    if not text:
        return

    # API-контракт сервера остаётся прежним: передаём один параметр text,
    # но внутри него есть короткая история и отдельно отмечено текущее сообщение.
    model_input = context_text or text
    result = await _classify(model_input)

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

    # Защита от повторного обновления одного сообщения.
    key = (message.chat_id, message.message_id)
    if key in _queued_messages:
        return

    # Собираем контекст ДО постановки в очередь: пока медленная модель
    # обрабатывает прошлое сообщение, следующие уже могут прийти в Telegram.
    history = _recent_messages[chat.id]
    current_name = user.full_name or user.username or "Участник"
    previous_lines = [
        f"{entry['name']}: {entry['text'][:70]}"
        for entry in list(history)[-(CONTEXT_MESSAGES - 1):]
    ]
    current_line = f"ТЕКУЩЕЕ СООБЩЕНИЕ ({current_name}): {text[:550]}"
    context_text = (
        "Оценивай текущее сообщение с учётом переписки. "
        "Мат, сленг, пошлые шутки, взаимные подколы и обсуждение 18+ "
        "не являются нарушением сами по себе. Нарушение — направленная "
        "агрессия, унижение, травля, угрозы или эскалация реального конфликта.\n"
        + current_line
        + ("\nПредыдущие сообщения:\n" + "\n".join(previous_lines) if previous_lines else "")
    )[:MAX_TEXT_LENGTH]

    history.append({"name": current_name, "text": text[:250]})

    # Запускаем очередь, если она ещё не запущена.
    if _queue is None or not _workers:
        _start_workers()

    _queued_messages.add(key)

    try:
        _queue.put_nowait(
            (message, context.bot, context_text)
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
# MEDIA MODERATION
# ============================================================

MAX_MEDIA_BYTES = 20 * 1024 * 1024


def _compress_image_bytes(raw: bytes) -> bytes:
    from PIL import Image
    image = Image.open(io.BytesIO(raw))
    image.load()
    image.thumbnail((512, 512), Image.Resampling.LANCZOS)
    if image.mode in ("RGBA", "LA") or (image.mode == "P" and "transparency" in image.info):
        rgba = image.convert("RGBA")
        background = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
        background.alpha_composite(rgba)
        image = background.convert("RGB")
    else:
        image = image.convert("RGB")
    output = io.BytesIO()
    image.save(output, format="JPEG", quality=82, optimize=True)
    return output.getvalue()


def _render_tgs(raw: bytes) -> bytes:
    """Рендерит один средний кадр Telegram-анимации TGS."""
    from rlottie_python import LottieAnimation
    with tempfile.NamedTemporaryFile(suffix=".tgs", delete=False) as tmp:
        tmp.write(raw)
        path = tmp.name
    animation = None
    try:
        animation = LottieAnimation.from_tgs(path)
        total = 1
        for attr in ("lottie_animation_get_totalframe", "get_totalframe", "get_total_frames", "total_frames"):
            value = getattr(animation, attr, None)
            if callable(value):
                total = int(value())
                break
            if value is not None:
                total = int(value)
                break
        if total <= 0:
            inner = getattr(animation, "lottie_animation", None)
            total = int(getattr(inner, "totalFrame", 1) or 1)
        frame = max(0, (total - 1) // 2)
        rendered = animation.render_pillow_frame(frame_num=frame)
        if hasattr(rendered, "save"):
            output = io.BytesIO()
            rendered.save(output, format="PNG")
            return _compress_image_bytes(output.getvalue())
        if isinstance(rendered, (bytes, bytearray)):
            return _compress_image_bytes(bytes(rendered))
        raise RuntimeError("rlottie не вернул изображение кадра")
    finally:
        if animation is not None:
            try:
                animation.lottie_animation_destroy()
            except Exception:
                pass
        try:
            os.unlink(path)
        except OSError:
            pass


def _video_frames(raw: bytes) -> list[tuple[str, bytes]]:
    """Быстро извлекает до трёх равномерно распределённых кадров видео."""
    import imageio_ffmpeg
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    with tempfile.TemporaryDirectory(prefix="modai_video_") as folder:
        source = Path(folder) / "source_video.mp4"
        source.write_bytes(raw)
        probe = subprocess.run(
            [ffmpeg, "-hide_banner", "-i", str(source)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15, check=False,
        )
        details = probe.stderr.decode("utf-8", errors="ignore")
        match = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", details)
        if not match:
            raise RuntimeError("Не удалось определить длительность видео")
        duration = int(match.group(1)) * 3600 + int(match.group(2)) * 60 + float(match.group(3))
        if duration <= 0:
            raise RuntimeError("Видео имеет некорректную длительность")
        fps = 3.0 / max(duration, 0.5)
        pattern = str(Path(folder) / "frame_%02d.jpg")
        subprocess.run(
            [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", str(source),
             "-vf", f"fps={fps:.6f},scale=512:512:force_original_aspect_ratio=decrease",
             "-frames:v", "3", "-q:v", "4", pattern],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=25, check=True,
        )
        frames = []
        for output in sorted(Path(folder).glob("frame_*.jpg")):
            frames.append((output.name, _compress_image_bytes(output.read_bytes())))
        if not frames:
            raise RuntimeError("Не удалось извлечь кадры видео")
        return frames


async def _download_telegram_file(bot, file_id: str) -> bytes:
    telegram_file = await bot.get_file(file_id)
    data = bytes(await telegram_file.download_as_bytearray())
    if not data or len(data) > MAX_MEDIA_BYTES:
        raise RuntimeError("Файл пустой или превышает лимит 20 МБ")
    return data


async def _prepare_media_images(message, bot) -> tuple[list[tuple[str, bytes]], str]:
    if message.photo:
        raw = await _download_telegram_file(bot, message.photo[-1].file_id)
        return [("photo.jpg", await asyncio.to_thread(_compress_image_bytes, raw))], "фотография"

    sticker = message.sticker
    if sticker:
        raw = await _download_telegram_file(bot, sticker.file_id)
        if sticker.is_animated:
            frame = await asyncio.to_thread(_render_tgs, raw)
            return [("animated_sticker.jpg", frame)], "анимированный стикер"
        if sticker.is_video:
            frames = await asyncio.to_thread(_video_frames, raw)
            return frames, "видеостикер (5 кадров)"
        return [("sticker.jpg", await asyncio.to_thread(_compress_image_bytes, raw))], "стикер"

    if message.video:
        raw = await _download_telegram_file(bot, message.video.file_id)
        frames = await asyncio.to_thread(_video_frames, raw)
        return frames, "видео (5 кадров)"

    if message.video_note:
        raw = await _download_telegram_file(bot, message.video_note.file_id)
        frames = await asyncio.to_thread(_video_frames, raw)
        return frames, "видеосообщение (5 кадров)"

    if message.animation:
        raw = await _download_telegram_file(bot, message.animation.file_id)
        frames = await asyncio.to_thread(_video_frames, raw)
        return frames, "анимация (5 кадров)"

    return [], "медиа"


async def _process_media_message(message, bot) -> None:
    if not message.from_user or message.from_user.is_bot:
        return
    images, media_name = await _prepare_media_images(message, bot)
    if not images:
        return
    result = await _classify_images(images)
    if not result:
        return

    items = result.get("results") or result.get("images") or []
    detected_categories: set[str] = set()
    category_scores: dict[str, float] = {"NSFL": 0.0, "NSFW": 0.0}
    for item in items:
        if not isinstance(item, dict):
            continue
        label = str(item.get("label", "")).upper()
        scores = item.get("scores") or {}
        for candidate in ("NSFL", "NSFW"):
            try:
                score = float(scores.get(candidate, scores.get(candidate.lower(), 0)) or 0)
                if score > 1:
                    score /= 100
            except (TypeError, ValueError):
                score = 0.0
            category_scores[candidate] = max(category_scores[candidate], score)
        if label in ("NSFL", "NSFW") or item.get("violation") is True:
            detected_categories.add(label if label in ("NSFL", "NSFW") else "NSFW")

    # Совместимость с сервером, который возвращает общий флаг violation.
    if result.get("violation") and not detected_categories:
        detected_categories.add("NSFW")

    if not detected_categories:
        logger.info(
            "Image moderation safe: user=%s message=%s frames=%s",
            message.from_user.id, message.message_id, len(images),
        )
        return

    # Если модель нашла обе категории, сохраняем более тяжёлую как основную,
    # но reason summary покажет обе категории из таблицы.
    category = "+".join(sorted(detected_categories))
    score = max((category_scores.get(item, 0.0) for item in detected_categories), default=0.0)
    if score <= 0:
        score = 0.99
    media_type = "photo" if message.photo else ("sticker" if message.sticker else "video")
    excerpt = f"{category}: {media_name} — обнаружен небезопасный контент"
    if message.caption:
        excerpt += f" | подпись: {message.caption[:250]}"

    await _save_violation(
        bot=bot,
        chat_id=message.chat_id,
        user_id=message.from_user.id,
        message_id=message.message_id,
        message_text=excerpt,
        toxic_score=score,
        first_name=message.from_user.first_name or "Пользователь",
        username=message.from_user.username,
        category=category,
        media_type=media_type,
    )
    logger.warning(
        "Unsafe media violation: category=%s user=%s message=%s kind=%s",
        category, message.from_user.id, message.message_id, media_name,
    )

async def check_media(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    chat = update.effective_chat
    user = update.effective_user
    if not message or not chat or not user or user.is_bot:
        return
    if chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        return
    if CHAT_ID and chat.id != CHAT_ID:
        return
    if not API_TOKEN:
        return
    if not (message.photo or message.sticker or message.video or message.video_note):
        return
    key = (message.chat_id, message.message_id)
    if key in _queued_media:
        return
    if _media_queue is None or _media_worker is None or _media_worker.done():
        _start_media_worker()
    _queued_media.add(key)
    try:
        _media_queue.put_nowait((message, context.bot))
        logger.debug("Media queued: user=%s message=%s queue=%s", user.id, message.message_id, _media_queue.qsize())
    except asyncio.QueueFull:
        _queued_media.discard(key)
        logger.warning("Media moderation queue full; skipped message=%s", message.message_id)



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
                    "Нарушения за последний час очищены."
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

                deleted, failed = await _delete_confirmed_violations(
                    context.bot,
                    chat_id,
                    user_id,
                )
                reason = await _get_violation_reason(chat_id, user_id)
                await _notify_group_action(
                    context.bot,
                    chat_id=chat_id,
                    user_id=user_id,
                    action="ban",
                    deleted=deleted,
                    failed=failed,
                    reason=reason,
                )

                result_text = (
                    "🔨 Пользователь заблокирован администратором. "
                    f"Удалено медиа-сообщений: {deleted}; ошибок удаления: {failed}."
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

                deleted, failed = await _delete_confirmed_violations(
                    context.bot,
                    chat_id,
                    user_id,
                )
                reason = await _get_violation_reason(chat_id, user_id)
                await _notify_group_action(
                    context.bot,
                    chat_id=chat_id,
                    user_id=user_id,
                    action="mute",
                    deleted=deleted,
                    failed=failed,
                    reason=reason,
                )

                result_text = (
                    "🔇 Мут на 24 часа подтверждён администратором. "
                    f"Удалено медиа-сообщений: {deleted}; ошибок удаления: {failed}."
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
