"""
Ksenya AI Security — многоступенчатая AI-модерация Telegram.

Что исправлено в этой версии (по результатам аудита):
- NSFW / NSFL теперь срабатывают мгновенно (как SPAM), а не после 4 нарушений.
- Отдельные семафоры для токсик / спам / image — больше нет полной сериализации.
- Увеличены воркеры (4) и очередь (1000) — сообщения больше не теряются пачками.
- Укорочены таймауты API (25с/20с) — зависший запрос не блокирует всё.
- Устойчивый парсинг ответов классификаторов (score / scores / predictions / label-алиасы).
- Поддержка message.animation (GIF раньше не проверялись).
- Корректный merge категорий для каптион+фото (TOXIC+NSFW больше не теряется).
- Мгновенные действия не дублируются, если сообщение обработано дважды.
- _try_mute больше не может заблокировать воркер на минуту.
- Очередь медиа и текста разделена.
- Уведомление админу при перегрузке очереди (throttled).
- При бане/муте удаляются и текстовые подтверждённые нарушения.
- Счётчик нарушений для токсика больше не включает строки с NSFW/NSFL/SPAM.

Логика наказаний:
- SPAM  -> мут 24ч + удаление сообщения (немедленно).
- NSFL  -> мут 24ч + удаление сообщения (немедленно).
- NSFW  -> мут 24ч + удаление сообщения (немедленно).
- TOXIC -> накопление: >= VIOLATION_THRESHOLD уверенных нарушений за WINDOW_SECONDS.
"""

import asyncio
import html
import io
import logging
import os
import re
import secrets
import subprocess
import tempfile
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
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
SPAM_API_URL = "https://tambovinside.onrender.com/check-spam"
IMAGE_API_URL = "https://tambovinside.onrender.com/check-image"

API_TOKEN = os.getenv("AImoder", "").strip()

ADMIN_ID = int(os.getenv("ADMIN_ID", "0") or 0)
CHAT_ID = int(os.getenv("CHAT_ID", "0") or 0)

ADMIN_MENTION = "@YouWarm"

# Порог для накопления токсичных нарушений (SPAM/NSFL/NSFW — мгновенно).
VIOLATION_THRESHOLD = 3

# RU-Toxic-BERT: срабатываем только на уверенных нарушениях.
MIN_TOXIC_SCORE = 0.85

# FastText: реклама/спам.
MIN_SPAM_SCORE = float(os.getenv("SPAM_THRESHOLD", "0.92"))

# Медиа: уверенность image-классификатора.
MIN_MEDIA_SCORE = float(os.getenv("MEDIA_THRESHOLD", "0.85"))

# Окно накопления для токсика.
WINDOW_SECONDS = 30 * 60
MUTE_SECONDS = 24 * 60 * 60

# HTTP — короче, чтобы не блокировать конвейер.
REQUEST_TIMEOUT_SECONDS = 25
CONNECT_TIMEOUT_SECONDS = 10
SOCK_READ_TIMEOUT_SECONDS = 20

IMAGE_TIMEOUT_TOTAL = 90
IMAGE_TIMEOUT_CONNECT = 30
IMAGE_TIMEOUT_READ = 60

# Конкурентность — отдельные семафоры на каждый эндпоинт.
MAX_PARALLEL_REQUESTS = 4
MAX_TOXIC_PARALLEL = 4
MAX_SPAM_PARALLEL = 4
MAX_IMAGE_PARALLEL = 1

MAX_QUEUE_SIZE = 1000
MAX_MEDIA_QUEUE_SIZE = 60

MAX_TEXT_LENGTH = 1000
CONTEXT_MESSAGES = 6
MAX_MEDIA_BYTES = 20 * 1024 * 1024

IMMEDIATE_CATEGORIES = {"SPAM", "NSFL", "NSFW"}

# ============================================================
# RUNTIME STATE
# ============================================================

_recent_messages: dict[int, deque] = defaultdict(
    lambda: deque(maxlen=CONTEXT_MESSAGES)
)

_queue: asyncio.Queue | None = None
_workers: set[asyncio.Task] = set()

_media_queue: asyncio.Queue | None = None
_media_worker: asyncio.Task | None = None

_session: aiohttp.ClientSession | None = None

_toxic_semaphore = asyncio.Semaphore(MAX_TOXIC_PARALLEL)
_spam_semaphore = asyncio.Semaphore(MAX_SPAM_PARALLEL)
_image_semaphore = asyncio.Semaphore(MAX_IMAGE_PARALLEL)

_user_locks: dict[tuple[int, int], asyncio.Lock] = {}
_queued_messages: set[tuple[int, int]] = set()
_queued_media: set[tuple[int, int]] = set()

_last_overload_notice = 0.0

# ============================================================
# HELPERS
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


def _parse_score(value: Any) -> float:
    try:
        score = float(value)
    except (TypeError, ValueError):
        return 0.0
    if score > 1:
        score /= 100
    return max(0.0, min(score, 1.0))


def _split_categories(category: str) -> set[str]:
    return {p.strip().upper() for p in str(category or "").split("+") if p.strip()}


def _merge_categories(a: str, b: str) -> str:
    parts: list[str] = []
    for chunk in (a or "", b or ""):
        for part in chunk.split("+"):
            part = part.strip().upper()
            if part and part not in parts:
                parts.append(part)
    return "+".join(sorted(parts))


def _is_immediate_category(category: str) -> bool:
    return bool(_split_categories(category) & IMMEDIATE_CATEGORIES)


def _user_lock(chat_id: int, user_id: int) -> asyncio.Lock:
    key = (chat_id, user_id)
    lock = _user_locks.get(key)
    if lock is None:
        if len(_user_locks) > 5000:
            for k, lk in list(_user_locks.items()):
                if not lk.locked():
                    _user_locks.pop(k, None)
        lock = asyncio.Lock()
        _user_locks[key] = lock
    return lock


# ============================================================
# DATABASE
# ============================================================


async def init_db() -> None:
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
        ON ai_moderation_violations(chat_id, user_id, created_at)
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
            muted_until INTEGER,
            category TEXT NOT NULL DEFAULT 'TOXIC',
            message_id INTEGER
        )
        """
    )

    alert_columns_rs = await db._exec("PRAGMA table_info(ai_moderation_alerts)")
    alert_columns = {str(row[1]) for row in (alert_columns_rs.rows or []) if len(row) > 1}
    if "category" not in alert_columns:
        await db._exec_required(
            "ALTER TABLE ai_moderation_alerts ADD COLUMN category TEXT NOT NULL DEFAULT 'TOXIC'"
        )
    if "message_id" not in alert_columns:
        await db._exec_required(
            "ALTER TABLE ai_moderation_alerts ADD COLUMN message_id INTEGER"
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
            "AImoder отсутствует в Render Environment. AI-модерация выключена."
        )
    else:
        logger.info(
            "Ksenya AI Security initialized: chat=%s threshold=%s window=%s "
            "workers=%s toxic=%s spam=%s media=%s",
            CHAT_ID,
            VIOLATION_THRESHOLD,
            WINDOW_SECONDS,
            MAX_PARALLEL_REQUESTS,
            MIN_TOXIC_SCORE,
            MIN_SPAM_SCORE,
            MIN_MEDIA_SCORE,
        )


# ============================================================
# QUEUES / WORKERS
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
                _queued_messages.discard((message.chat_id, message.message_id))
            _queue.task_done()


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


async def _notify_overload_once(bot, reason: str) -> None:
    global _last_overload_notice
    now = time.time()
    if now - _last_overload_notice < 300:
        return
    _last_overload_notice = now
    logger.warning("AI moderation overload: %s", reason)
    if ADMIN_ID:
        try:
            await bot.send_message(
                chat_id=ADMIN_ID,
                text=(
                    "⚠️ AI-модерация перегружена (%s). "
                    "Часть сообщений может быть пропущена."
                ) % reason,
            )
        except TelegramError:
            pass


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
            limit=MAX_TOXIC_PARALLEL + MAX_SPAM_PARALLEL + MAX_IMAGE_PARALLEL + 2,
            ttl_dns_cache=300,
            enable_cleanup_closed=True,
        )
        _session = aiohttp.ClientSession(timeout=timeout, connector=connector)
    return _session


async def close() -> None:
    global _session, _media_worker

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

    if _media_worker is not None:
        _media_worker.cancel()
        await asyncio.gather(_media_worker, return_exceptions=True)
        _media_worker = None

    for task in list(_workers):
        task.cancel()
    if _workers:
        await asyncio.gather(*_workers, return_exceptions=True)
    _workers.clear()

    if _session and not _session.closed:
        await _session.close()
    _session = None
    logger.info("AI moderation stopped")


# ============================================================
# RESPONSE PARSING
# ============================================================


def _extract_toxic_score(result: dict) -> float:
    for key in ("toxic_score", "score", "confidence", "probability", "toxic"):
        if key in result:
            return _parse_score(result[key])
    return 0.0


def _extract_spam_score(result: dict) -> float:
    for key in ("spam_score", "score", "confidence", "probability", "spam"):
        if key in result:
            return _parse_score(result[key])
    return 0.0


_IMAGE_LABEL_ALIASES = {
    "EXPLICIT": "NSFW",
    "ADULT": "NSFW",
    "PORN": "NSFW",
    "PORNOGRAPHY": "NSFW",
    "NUDITY": "NSFW",
    "NUDE": "NSFW",
    "SEXUAL": "NSFW",
    "XXX": "NSFW",
    "GORE": "NSFL",
    "VIOLENCE": "NSFL",
    "SHOCK": "NSFL",
    "DEATH": "NSFL",
    "BLOOD": "NSFL",
    "GORY": "NSFL",
}


def _normalize_media_label(label: str) -> str:
    label = str(label or "").strip().upper()
    return _IMAGE_LABEL_ALIASES.get(label, label)


def _extract_image_items(result: Any) -> list[dict]:
    if isinstance(result, list):
        return [i for i in result if isinstance(i, dict)]
    if not isinstance(result, dict):
        return []
    for key in ("results", "images", "predictions", "data", "items", "detections"):
        val = result.get(key)
        if isinstance(val, list):
            return [i for i in val if isinstance(i, dict)]
    if any(k in result for k in ("label", "labels", "scores", "score")):
        return [result]
    return []


def _extract_item_label(item: dict) -> str:
    for key in (
        "label", "class", "category", "prediction",
        "top_label", "predicted_label", "name",
    ):
        val = item.get(key)
        if isinstance(val, str) and val.strip():
            return _normalize_media_label(val)
    labels = item.get("labels") or item.get("classes") or item.get("categories")
    if isinstance(labels, list) and labels:
        first = labels[0]
        if isinstance(first, str):
            return _normalize_media_label(first)
        if isinstance(first, dict):
            return _normalize_media_label(
                first.get("label") or first.get("name") or ""
            )
    return ""


def _extract_item_scores(item: dict) -> dict[str, float]:
    for key in ("scores", "probabilities", "confidences"):
        val = item.get(key)
        if isinstance(val, dict):
            out: dict[str, float] = {}
            for k, v in val.items():
                if isinstance(v, (int, float)):
                    out[str(k).upper()] = float(v)
            if out:
                return out
    for key in ("score", "confidence", "probability", "prob"):
        val = item.get(key)
        if isinstance(val, (int, float)):
            label = _extract_item_label(item)
            if label:
                return {label: float(val)}
    return {}


# ============================================================
# CLASSIFIER API
# ============================================================


async def _classify(text: str) -> dict[str, Any] | None:
    if not API_TOKEN:
        return None
    session = await _get_session()
    headers = {"X-API-Token": API_TOKEN}
    payload = {"text": text[:MAX_TEXT_LENGTH]}
    async with _toxic_semaphore:
        try:
            async with session.post(API_URL, json=payload, headers=headers) as response:
                if response.status != 200:
                    body = (await response.text())[:300]
                    logger.warning("RU-Toxic-BERT HTTP %s: %s", response.status, body)
                    return None
                result = await response.json(content_type=None)
                if not isinstance(result, dict):
                    logger.warning("RU-Toxic-BERT invalid JSON: %r", result)
                    return None
                return result
        except asyncio.TimeoutError:
            logger.warning("RU-Toxic-BERT request timeout")
        except aiohttp.ClientError:
            logger.exception("RU-Toxic-BERT connection error")
        except ValueError:
            logger.exception("RU-Toxic-BERT JSON parsing error")
        except Exception:
            logger.exception("Unexpected RU-Toxic-BERT error")
    return None


async def _classify_spam(text: str) -> dict[str, Any] | None:
    if not API_TOKEN:
        return None
    session = await _get_session()
    headers = {"X-API-Token": API_TOKEN}
    payload = {"text": text[:MAX_TEXT_LENGTH]}
    async with _spam_semaphore:
        try:
            async with session.post(
                SPAM_API_URL, json=payload, headers=headers
            ) as response:
                if response.status != 200:
                    body = (await response.text())[:300]
                    logger.warning("FastText spam HTTP %s: %s", response.status, body)
                    return None
                result = await response.json(content_type=None)
                if not isinstance(result, dict):
                    logger.warning("FastText spam invalid JSON: %r", result)
                    return None
                return result
        except asyncio.TimeoutError:
            logger.warning("FastText spam request timeout")
        except (aiohttp.ClientError, ValueError):
            logger.exception("FastText spam request failed")
        except Exception:
            logger.exception("Unexpected FastText spam error")
    return None


async def _classify_images(images: list[tuple[str, bytes]]) -> dict[str, Any] | None:
    if not API_TOKEN or not images:
        return None
    session = await _get_session()
    headers = {"X-API-Token": API_TOKEN}
    form = aiohttp.FormData()
    for filename, content in images:
        form.add_field("files", content, filename=filename, content_type="image/jpeg")
    timeout = aiohttp.ClientTimeout(
        total=IMAGE_TIMEOUT_TOTAL,
        connect=IMAGE_TIMEOUT_CONNECT,
        sock_read=IMAGE_TIMEOUT_READ,
    )
    async with _image_semaphore:
        try:
            async with session.post(
                IMAGE_API_URL, data=form, headers=headers, timeout=timeout
            ) as response:
                if response.status != 200:
                    body = (await response.text())[:300]
                    logger.warning(
                        "Image classifier HTTP %s: %s", response.status, body
                    )
                    return None
                result = await response.json(content_type=None)
                return result if isinstance(result, (dict, list)) else None
        except asyncio.TimeoutError:
            logger.warning("Image classifier request timeout")
        except (aiohttp.ClientError, ValueError):
            logger.exception("Image classifier request failed")
        except Exception:
            logger.exception("Unexpected image classifier error")
    return None


# ============================================================
# MUTE
# ============================================================


async def _try_mute(bot, chat_id: int, user_id: int) -> bool:
    until = datetime.now(timezone.utc) + timedelta(seconds=MUTE_SECONDS)
    for attempt in range(1, 3):
        try:
            result = await bot.restrict_chat_member(
                chat_id=chat_id,
                user_id=user_id,
                permissions=_permissions_muted(),
                until_date=until,
            )
            if result is False:
                logger.error(
                    "Telegram returned false for mute chat=%s user=%s",
                    chat_id, user_id,
                )
                return False
            logger.info(
                "Mute applied chat=%s user=%s duration=%ss attempt=%s",
                chat_id, user_id, MUTE_SECONDS, attempt,
            )
            return True
        except TelegramError as error:
            name = type(error).__name__
            retry_after = getattr(error, "retry_after", None)
            temporary = (
                name in ("TimedOut", "NetworkError", "RetryAfter", "TelegramRetryAfter")
                or retry_after is not None
            )
            logger.error(
                "Mute failed chat=%s user=%s attempt=%s/2 type=%s err=%s",
                chat_id, user_id, attempt, name, error,
            )
            if not temporary or attempt >= 2:
                return False
            delay = float(retry_after) if retry_after is not None else 1.0
            await asyncio.sleep(max(0.5, min(delay, 3.0)))
        except Exception:
            logger.exception(
                "Unexpected mute error chat=%s user=%s", chat_id, user_id
            )
            return False
    return False


# ============================================================
# CLEANUP / NOTICES
# ============================================================


async def _delete_confirmed_violations(
    bot, chat_id: int, user_id: int, include_text: bool = True
) -> tuple[int, int]:
    cutoff = int(time.time()) - WINDOW_SECONDS
    if include_text:
        query = (
            "SELECT message_id FROM ai_moderation_violations "
            "WHERE chat_id=? AND user_id=? AND created_at>=? "
            "ORDER BY created_at DESC"
        )
    else:
        query = (
            "SELECT message_id FROM ai_moderation_violations "
            "WHERE chat_id=? AND user_id=? AND created_at>=? "
            "AND media_type IN ('photo','sticker','video') "
            "ORDER BY created_at DESC"
        )
    rs = await db._exec(query, [chat_id, user_id, cutoff])
    if getattr(rs, "error", False):
        logger.error("Could not load violations for deletion user=%s", user_id)
        return 0, 0

    message_ids = list(
        dict.fromkeys(
            int(row[0]) for row in (rs.rows or []) if row and row[0] is not None
        )
    )
    deleted = 0
    failed = 0
    for message_id in message_ids:
        try:
            await bot.delete_message(chat_id=chat_id, message_id=message_id)
            deleted += 1
        except TelegramError:
            failed += 1
            logger.warning(
                "Could not delete violation chat=%s message=%s",
                chat_id, message_id, exc_info=True,
            )
    logger.info(
        "Violation cleanup user=%s deleted=%s failed=%s", user_id, deleted, failed
    )
    return deleted, failed


async def _get_violation_reason(chat_id: int, user_id: int) -> str:
    rs = await db._exec(
        "SELECT DISTINCT category FROM ai_moderation_violations "
        "WHERE chat_id=? AND user_id=? AND created_at>=?",
        [chat_id, user_id, int(time.time()) - WINDOW_SECONDS],
    )
    categories: set[str] = set()
    for row in (rs.rows or []):
        if row and row[0]:
            categories |= _split_categories(str(row[0]))
    labels: list[str] = []
    if "NSFL" in categories:
        labels.append("NSFL — шокирующий контент и жестокость")
    if "NSFW" in categories:
        labels.append("NSFW — сексуальный контент или нагота")
    if "TOXIC" in categories:
        labels.append("оскорбления или травля")
    if "SPAM" in categories:
        labels.append("реклама или спам")
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
    user_link = f'<a href="tg://user?id={user_id}">Участник</a>'
    if action == "ban":
        heading = f"🚫 {user_link} заблокирован. Причина: {html.escape(reason)}."
    else:
        heading = f"🔇 {user_link} получил мут на 24 часа. Причина: {html.escape(reason)}."
    cleanup = ""
    if include_cleanup:
        if deleted and not failed:
            cleanup = f"\nУдалено сообщений с нарушениями: {deleted}."
        elif deleted and failed:
            cleanup = (
                f"\nУдалено: {deleted}; не удалось удалить: {failed}."
            )
        elif failed:
            cleanup = (
                "\nНе удалось удалить отмеченные сообщения. "
                "Проверьте права бота."
            )
    try:
        await bot.send_message(
            chat_id=chat_id,
            text=f"{heading}{cleanup}",
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
    except TelegramError:
        logger.exception("Could not send group action notice chat=%s", chat_id)


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
    category: str = "TOXIC",
) -> None:
    if not ADMIN_ID:
        logger.warning("ADMIN_ID не задан: уведомление не отправлено")
        return

    shown_name = f"@{username}" if username else (first_name or "Пользователь")
    user_link = (
        f'<a href="tg://user?id={user_id}">{html.escape(shown_name)}</a>'
    )
    excerpt = html.escape((message_text or "")[:700]) or "(пустое сообщение)"
    status = (
        "Мут на 24 часа применён."
        if auto_muted
        else "Автоматический мут не применился — проверьте права бота."
    )

    parts = _split_categories(category)
    if "SPAM" in parts:
        details = (
            "Категория: <b>Реклама / спам</b>\n"
            f"Оценка FastText: <b>{toxic_score * 100:.1f}%</b>\n"
        )
    elif parts & {"NSFL", "NSFW"}:
        details = (
            f"Категория: <b>{html.escape(category)}</b>\n"
            f"Оценка image-классификатора: <b>{toxic_score * 100:.1f}%</b>\n"
        )
    else:
        details = (
            f"Нарушений за окно: <b>{count}/{VIOLATION_THRESHOLD}</b>\n"
            f"Категория: <b>{html.escape(category)}</b>\n"
            f"Оценка RU-Toxic-BERT: <b>{toxic_score * 100:.1f}%</b>\n"
        )

    text = (
        "<b>Проверка модерации</b>\n"
        f"Участник: {user_link}\n"
        f"{details}"
        f"{html.escape(status)}\n\n"
        f"<b>Последнее сообщение:</b>\n<blockquote>{excerpt}</blockquote>"
    )
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🚫 Забанить", callback_data=f"modai:ban:{alert_id}")],
        [InlineKeyboardButton(
            "🔇 Оставить мут на 24 часа", callback_data=f"modai:mute:{alert_id}"
        )],
        [InlineKeyboardButton(
            "❌ Это ошибка", callback_data=f"modai:wrong:{alert_id}"
        )],
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
            "Не удалось отправить уведомление админу=%s", ADMIN_ID
        )


# ============================================================
# ACTIONS
# ============================================================


async def _handle_immediate_action(
    *,
    bot,
    chat_id: int,
    user_id: int,
    message_id: int,
    message_text: str,
    toxic_score: float,
    first_name: str,
    username: str | None,
    category: str,
    media_type: str | None,
) -> None:
    now = int(time.time())

    pending_rs = await db._exec(
        "SELECT alert_id, category, auto_muted, muted_until "
        "FROM ai_moderation_alerts "
        "WHERE chat_id=? AND user_id=? AND status='pending' LIMIT 1",
        [chat_id, user_id],
    )

    alert_id: str | None = None
    if pending_rs.rows:
        old_id, old_cat, old_auto_muted, old_until = pending_rs.rows[0]
        old_parts = _split_categories(str(old_cat or ""))
        new_parts = _split_categories(category)
        if old_parts & new_parts & IMMEDIATE_CATEGORIES:
            alert_id = str(old_id)
            await db._exec(
                "UPDATE ai_moderation_alerts SET category=?, message_id=? "
                "WHERE alert_id=?",
                [category, message_id, alert_id],
            )
        else:
            await db._exec(
                "UPDATE ai_moderation_alerts SET status='superseded' "
                "WHERE alert_id=?",
                [str(old_id)],
            )

    if not alert_id:
        alert_id = secrets.token_hex(6)
        created = await db._exec(
            """
            INSERT INTO ai_moderation_alerts
            (alert_id, chat_id, user_id, violation_count, status, auto_muted,
             created_at, muted_until, category, message_id)
            VALUES (?, ?, ?, 1, 'pending', 0, ?, NULL, ?, ?)
            """,
            [alert_id, chat_id, user_id, now, category, message_id],
        )
        if getattr(created, "error", False):
            logger.error("Failed to create alert user=%s", user_id)
            return

    auto_muted = await _try_mute(bot, chat_id, user_id)
    muted_until = now + MUTE_SECONDS if auto_muted else None

    await db._exec(
        "UPDATE ai_moderation_alerts SET auto_muted=?, muted_until=? "
        "WHERE alert_id=?",
        [1 if auto_muted else 0, muted_until, alert_id],
    )

    deleted = False
    try:
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
        deleted = True
    except TelegramError:
        logger.exception(
            "Could not delete immediate violation chat=%s message=%s",
            chat_id, message_id,
        )

    reason = await _get_violation_reason(chat_id, user_id)
    user_link = f'<a href="tg://user?id={user_id}">Участник</a>'
    parts = _split_categories(category)
    if "SPAM" in parts:
        if auto_muted:
            text = f"🔇 {user_link} получил мут на 24 часа за рекламу или спам."
        else:
            text = (
                f"⚠️ У {user_link} обнаружена реклама или спам, "
                "но автоматический мут не применился. Администратор уведомлён."
            )
    else:
        if auto_muted:
            text = (
                f"🔇 {user_link} получил мут на 24 часа. "
                f"Причина: {html.escape(reason)}."
            )
        else:
            text = (
                f"⚠️ У {user_link} обнаружено нарушение "
                f"({html.escape(reason)}), но автоматический мут не применился. "
                "Администратор уведомлён."
            )
    if not deleted:
        text += (
            "\nНе удалось удалить нарушающее сообщение — проверьте права бота."
        )
    try:
        await bot.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
    except TelegramError:
        logger.exception("Could not send group notice chat=%s", chat_id)

    await _notify_admin(
        bot,
        alert_id=alert_id,
        chat_id=chat_id,
        user_id=user_id,
        first_name=first_name,
        username=username,
        count=1,
        auto_muted=auto_muted,
        toxic_score=toxic_score,
        message_text=message_text,
        category=category,
    )
    logger.warning(
        "IMMEDIATE ACTION: user=%s category=%s muted=%s deleted=%s score=%.2f%%",
        user_id, category, auto_muted, deleted, toxic_score * 100,
    )


async def _handle_threshold_action(
    *,
    bot,
    chat_id: int,
    user_id: int,
    message_id: int,
    message_text: str,
    toxic_score: float,
    first_name: str,
    username: str | None,
    category: str,
    count: int,
) -> None:
    now = int(time.time())

    pending_rs = await db._exec(
        "SELECT alert_id, auto_muted, muted_until, category "
        "FROM ai_moderation_alerts "
        "WHERE chat_id=? AND user_id=? AND status='pending' LIMIT 1",
        [chat_id, user_id],
    )

    if pending_rs.rows:
        alert_id, was_auto_muted, old_until, old_cat = pending_rs.rows[0]
        alert_id = str(alert_id)
        was_auto_muted = int(was_auto_muted or 0)
        old_until = int(old_until or 0)
        mute_active = was_auto_muted == 1 and old_until > now

        if mute_active:
            # Пользователь уже наказан — просто обновляем счётчик.
            await db._exec(
                "UPDATE ai_moderation_alerts SET violation_count=?, "
                "message_id=? WHERE alert_id=?",
                [count, message_id, alert_id],
            )
            return

        # Пытаемся наказать снова (мьют был, но не применился / истёк).
        auto_muted = await _try_mute(bot, chat_id, user_id)
        if auto_muted:
            new_until = now + MUTE_SECONDS
            await db._exec(
                "UPDATE ai_moderation_alerts SET violation_count=?, "
                "auto_muted=1, muted_until=?, category=?, message_id=? "
                "WHERE alert_id=?",
                [count, new_until, category, message_id, alert_id],
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
                "UPDATE ai_moderation_alerts SET violation_count=?, "
                "auto_muted=0, muted_until=NULL, category=?, message_id=? "
                "WHERE alert_id=?",
                [count, category, message_id, alert_id],
            )
        return

    alert_id = secrets.token_hex(6)
    created = await db._exec(
        """
        INSERT INTO ai_moderation_alerts
        (alert_id, chat_id, user_id, violation_count, status, auto_muted,
         created_at, muted_until, category, message_id)
        VALUES (?, ?, ?, ?, 'pending', 0, ?, NULL, ?, ?)
        """,
        [alert_id, chat_id, user_id, count, now, category, message_id],
    )
    if getattr(created, "error", False):
        logger.error("Failed to create threshold alert user=%s", user_id)
        return

    auto_muted = await _try_mute(bot, chat_id, user_id)
    muted_until = now + MUTE_SECONDS if auto_muted else None

    await db._exec(
        "UPDATE ai_moderation_alerts SET auto_muted=?, muted_until=? "
        "WHERE alert_id=?",
        [1 if auto_muted else 0, muted_until, alert_id],
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
        category=category,
    )
    logger.warning(
        "THRESHOLD ACTION: user=%s count=%s muted=%s score=%.2f%%",
        user_id, count, auto_muted, toxic_score * 100,
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
            (chat_id, user_id, message_id, message_text, toxic_score,
             created_at, category, media_type)
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
            logger.error("Failed to save violation user=%s", user_id)
            return

        is_new = int(getattr(inserted, "rows_affected", 0) or 0) > 0

        if not is_new:
            # Строка уже есть — мерджим категорию и обновляем медиа-метку.
            existing = await db._exec(
                "SELECT category, media_type FROM ai_moderation_violations "
                "WHERE chat_id=? AND message_id=?",
                [chat_id, message_id],
            )
            if not existing.rows:
                return
            old_cat = str(existing.rows[0][0] or "")
            old_media = existing.rows[0][1]
            merged_cat = _merge_categories(old_cat, category)
            merged_media = media_type or old_media
            await db._exec(
                "UPDATE ai_moderation_violations "
                "SET category=?, media_type=?, message_text=?, "
                "toxic_score=MAX(toxic_score, ?) "
                "WHERE chat_id=? AND message_id=?",
                [
                    merged_cat,
                    merged_media,
                    message_text[:MAX_TEXT_LENGTH],
                    toxic_score,
                    chat_id,
                    message_id,
                ],
            )

            old_parts = _split_categories(old_cat)
            new_parts = _split_categories(merged_cat)
            newly_immediate = (new_parts - old_parts) & IMMEDIATE_CATEGORIES

            if newly_immediate:
                await _handle_immediate_action(
                    bot=bot,
                    chat_id=chat_id,
                    user_id=user_id,
                    message_id=message_id,
                    message_text=message_text,
                    toxic_score=toxic_score,
                    first_name=first_name,
                    username=username,
                    category=merged_cat,
                    media_type=merged_media,
                )
            else:
                logger.debug(
                    "Merged category %s -> %s, no new immediate trigger",
                    old_cat, merged_cat,
                )
            return

        effective_category = category

        # Мгновенные категории.
        if _is_immediate_category(effective_category):
            await _handle_immediate_action(
                bot=bot,
                chat_id=chat_id,
                user_id=user_id,
                message_id=message_id,
                message_text=message_text,
                toxic_score=toxic_score,
                first_name=first_name,
                username=username,
                category=effective_category,
                media_type=media_type,
            )
            return

        # Токсичные — накопление.
        await db._exec(
            "DELETE FROM ai_moderation_violations WHERE created_at < ?", [cutoff]
        )

        count_rs = await db._exec(
            """
            SELECT COUNT(*) FROM ai_moderation_violations
            WHERE chat_id=? AND user_id=? AND created_at>=?
              AND category NOT LIKE '%SPAM%'
              AND category NOT LIKE '%NSFL%'
              AND category NOT LIKE '%NSFW%'
            """,
            [chat_id, user_id, cutoff],
        )
        if getattr(count_rs, "error", False) or not count_rs.rows:
            return
        count = int(count_rs.rows[0][0] or 0)

        logger.info(
            "Toxic counter %s/%s user=%s score=%.2f%%",
            count, VIOLATION_THRESHOLD, user_id, toxic_score * 100,
        )

        if count < VIOLATION_THRESHOLD:
            return

        await _handle_threshold_action(
            bot=bot,
            chat_id=chat_id,
            user_id=user_id,
            message_id=message_id,
            message_text=message_text,
            toxic_score=toxic_score,
            first_name=first_name,
            username=username,
            category=effective_category,
            count=count,
        )


# ============================================================
# PROCESS TEXT
# ============================================================


async def _process_message(message, bot, context_text: str = "") -> None:
    text = (message.text or message.caption or "").strip()
    if not text or not message.from_user:
        return

    model_input = context_text or text

    toxic_result, spam_result = await asyncio.gather(
        _classify(model_input),
        _classify_spam(text),
        return_exceptions=True,
    )

    categories: list[str] = []
    scores: list[float] = []

    if isinstance(toxic_result, dict):
        try:
            classification = int(toxic_result.get("class", 0))
            toxic_score = _extract_toxic_score(toxic_result)
            if toxic_score >= MIN_TOXIC_SCORE:
                categories.append("TOXIC")
                scores.append(toxic_score)
            logger.info(
                "RU-Toxic-BERT: user=%s message=%s class=%s toxic=%.2f%% "
                "threshold=%.2f%% latency=%s",
                message.from_user.id, message.message_id, classification,
                toxic_score * 100, MIN_TOXIC_SCORE * 100,
                toxic_result.get("latency_ms"),
            )
        except (TypeError, ValueError):
            logger.warning("Invalid RU-Toxic-BERT result: %r", toxic_result)
    elif isinstance(toxic_result, BaseException):
        logger.error("Toxicity task failed: %s", toxic_result)

    if isinstance(spam_result, dict):
        spam_score = _extract_spam_score(spam_result)
        is_spam = spam_score >= MIN_SPAM_SCORE
        logger.info(
            "FastText spam: user=%s message=%s spam=%s score=%.2f%% "
            "threshold=%.2f%% latency=%s",
            message.from_user.id, message.message_id, is_spam,
            spam_score * 100, MIN_SPAM_SCORE * 100,
            spam_result.get("latency_ms"),
        )
        if is_spam:
            categories.append("SPAM")
            scores.append(spam_score)
    elif isinstance(spam_result, BaseException):
        logger.error("Spam task failed: %s", spam_result)

    if not categories:
        return

    await _save_violation(
        bot=bot,
        chat_id=message.chat_id,
        user_id=message.from_user.id,
        message_id=message.message_id,
        message_text=text,
        toxic_score=max(scores, default=0.0),
        first_name=message.from_user.first_name or "Пользователь",
        username=message.from_user.username,
        category="+".join(sorted(set(categories))),
    )


# ============================================================
# MEDIA HELPERS
# ============================================================


def _compress_image_bytes(raw: bytes) -> bytes:
    from PIL import Image
    image = Image.open(io.BytesIO(raw))
    image.load()
    image.thumbnail((512, 512), Image.Resampling.LANCZOS)
    if image.mode in ("RGBA", "LA") or (
        image.mode == "P" and "transparency" in image.info
    ):
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
    from rlottie_python import LottieAnimation
    with tempfile.NamedTemporaryFile(suffix=".tgs", delete=False) as tmp:
        tmp.write(raw)
        path = tmp.name
    animation = None
    try:
        animation = LottieAnimation.from_tgs(path)
        total = 1
        for attr in (
            "lottie_animation_get_totalframe",
            "get_totalframe",
            "get_total_frames",
            "total_frames",
        ):
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
    import imageio_ffmpeg
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    with tempfile.TemporaryDirectory(prefix="modai_video_") as folder:
        source = Path(folder) / "source_video.mp4"
        source.write_bytes(raw)
        probe = subprocess.run(
            [ffmpeg, "-hide_banner", "-i", str(source)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=15, check=False,
        )
        details = probe.stderr.decode("utf-8", errors="ignore")
        match = re.search(
            r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", details
        )
        if not match:
            raise RuntimeError("Не удалось определить длительность видео")
        duration = (
            int(match.group(1)) * 3600
            + int(match.group(2)) * 60
            + float(match.group(3))
        )
        if duration <= 0:
            raise RuntimeError("Видео имеет некорректную длительность")
        fps = 3.0 / max(duration, 0.5)
        pattern = str(Path(folder) / "frame_%02d.jpg")
        subprocess.run(
            [
                ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                "-i", str(source),
                "-vf",
                f"fps={fps:.6f},"
                "scale=512:512:force_original_aspect_ratio=decrease",
                "-frames:v", "3", "-q:v", "4", pattern,
            ],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=25, check=True,
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
        return [
            ("photo.jpg", await asyncio.to_thread(_compress_image_bytes, raw))
        ], "фотография"

    sticker = message.sticker
    if sticker:
        raw = await _download_telegram_file(bot, sticker.file_id)
        if sticker.is_animated:
            frame = await asyncio.to_thread(_render_tgs, raw)
            return [("animated_sticker.jpg", frame)], "анимированный стикер"
        if sticker.is_video:
            frames = await asyncio.to_thread(_video_frames, raw)
            return frames, "видеостикер"
        return [
            ("sticker.jpg", await asyncio.to_thread(_compress_image_bytes, raw))
        ], "стикер"

    if message.video:
        raw = await _download_telegram_file(bot, message.video.file_id)
        frames = await asyncio.to_thread(_video_frames, raw)
        return frames, "видео"

    if message.video_note:
        raw = await _download_telegram_file(bot, message.video_note.file_id)
        frames = await asyncio.to_thread(_video_frames, raw)
        return frames, "видеосообщение"

    if message.animation:
        raw = await _download_telegram_file(bot, message.animation.file_id)
        frames = await asyncio.to_thread(_video_frames, raw)
        return frames, "анимация"

    return [], "медиа"


async def _process_media_message(message, bot) -> None:
    if not message.from_user or message.from_user.is_bot:
        return
    try:
        images, media_name = await _prepare_media_images(message, bot)
    except Exception:
        logger.exception(
            "Failed to prepare media for moderation message=%s",
            message.message_id,
        )
        return
    if not images:
        return

    result = await _classify_images(images)
    if not result:
        return

    items = _extract_image_items(result)
    detected: set[str] = set()
    category_scores: dict[str, float] = {"NSFL": 0.0, "NSFW": 0.0}

    for item in items:
        label = _extract_item_label(item)
        scores = _extract_item_scores(item)
        for candidate in ("NSFL", "NSFW"):
            raw = scores.get(candidate, scores.get(candidate.lower(), 0))
            try:
                score = float(raw or 0)
            except (TypeError, ValueError):
                score = 0.0
            if score > 1:
                score /= 100
            category_scores[candidate] = max(category_scores[candidate], score)
        if label in ("NSFL", "NSFW"):
            if category_scores[label] >= MIN_MEDIA_SCORE:
                detected.add(label)

    if not detected:
        logger.info(
            "Image moderation safe: user=%s message=%s frames=%s",
            message.from_user.id, message.message_id, len(images),
        )
        return

    category = "+".join(sorted(detected))
    score = max(
        (category_scores.get(item, 0.0) for item in detected), default=0.0
    ) or MIN_MEDIA_SCORE
    media_type = (
        "photo" if message.photo
        else ("sticker" if message.sticker else "video")
    )
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
        "Unsafe media: category=%s user=%s message=%s kind=%s",
        category, message.from_user.id, message.message_id, media_name,
    )


# ============================================================
# HANDLERS
# ============================================================


async def check_message(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    message = update.effective_message
    chat = update.effective_chat
    user = update.effective_user
    if not message or not chat or not user:
        return
    if user.is_bot:
        return
    if chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        return
    if CHAT_ID and chat.id != CHAT_ID:
        return

    text = (message.text or message.caption or "").strip()
    if not text:
        return
    if not API_TOKEN:
        return

    key = (message.chat_id, message.message_id)
    if key in _queued_messages:
        return

    history = _recent_messages[chat.id]
    current_name = user.full_name or user.username or "Участник"
    previous_lines = [
        f"{entry['name']}: {entry['text'][:70]}"
        for entry in list(history)[-(CONTEXT_MESSAGES - 1):]
    ]
    current_line = f"ТЕКУЩЕЕ СООБЩЕНИЕ ({current_name}): {text[:550]}"
    context_text = (
        "Ты — модератор подросткового чата. Твоя задача — отличать реальную "
        "травлю и агрессию от обычного общения. "
        "НЕ считай нарушением: мат, сленг, пошлые шутки, обсуждение 18+, "
        "взаимные подколы, дружеские оскорбления, капс, эмоциональные "
        "восклицания. "
        "Нарушение — это НАПРАВЛЕННАЯ агрессия: унижение конкретного "
        "человека, травля, угрозы, пожелания смерти/болезней, "
        "систематические оскорбления одного участника, эскалация реального "
        "конфликта. "
        "Одиночное токсичное сообщение без ответа/эскалации — НЕ нарушение. "
        "Если сомневаешься — считай, что нарушения нет.\n"
        + current_line
        + (
            "\nПредыдущие сообщения:\n" + "\n".join(previous_lines)
            if previous_lines else ""
        )
    )[:MAX_TEXT_LENGTH]

    history.append({"name": current_name, "text": text[:250]})

    if _queue is None or not _workers:
        _start_workers()

    _queued_messages.add(key)
    try:
        _queue.put_nowait((message, context.bot, context_text))
        logger.debug(
            "Message queued: user=%s message=%s queue=%s",
            user.id, message.message_id, _queue.qsize(),
        )
    except asyncio.QueueFull:
        _queued_messages.discard(key)
        logger.warning(
            "AI queue full, message skipped: user=%s message=%s",
            user.id, message.message_id,
        )
        await _notify_overload_once(context.bot, "text queue full")


async def check_media(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
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
    if not (
        message.photo
        or message.sticker
        or message.video
        or message.video_note
        or message.animation
    ):
        return

    key = (message.chat_id, message.message_id)
    if key in _queued_media:
        return

    if (
        _media_queue is None
        or _media_worker is None
        or _media_worker.done()
    ):
        _start_media_worker()

    _queued_media.add(key)
    try:
        _media_queue.put_nowait((message, context.bot))
        logger.debug(
            "Media queued: user=%s message=%s queue=%s",
            user.id, message.message_id, _media_queue.qsize(),
        )
    except asyncio.QueueFull:
        _queued_media.discard(key)
        logger.warning(
            "Media queue full, skipped message=%s", message.message_id
        )
        await _notify_overload_once(context.bot, "media queue full")


# ============================================================
# ADMIN CALLBACKS
# ============================================================


async def moderation_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.callback_query
    if not query or not query.data:
        return
    if not ADMIN_ID or query.from_user.id != ADMIN_ID:
        await query.answer(
            "Эта кнопка доступна только администратору.", show_alert=True
        )
        return

    try:
        _, action, alert_id = query.data.split(":", 2)
    except ValueError:
        await query.answer("Некорректная кнопка.", show_alert=True)
        return

    if action not in ("wrong", "ban", "mute"):
        await query.answer("Неизвестное действие.", show_alert=True)
        return

    rs = await db._exec(
        "SELECT chat_id, user_id, status, auto_muted, category, message_id "
        "FROM ai_moderation_alerts WHERE alert_id=? LIMIT 1",
        [alert_id],
    )
    if getattr(rs, "error", False) or not rs.rows:
        await query.answer("Карточка не найдена.", show_alert=True)
        return

    chat_id, user_id, status, auto_muted, alert_category, alert_message_id = rs.rows[0]
    chat_id = int(chat_id)
    user_id = int(user_id)

    if status != "pending":
        await query.answer("Это предупреждение уже обработано.", show_alert=True)
        return

    lock = _user_lock(chat_id, user_id)
    async with lock:
        current = await db._exec(
            "SELECT status, auto_muted, category, message_id "
            "FROM ai_moderation_alerts WHERE alert_id=? LIMIT 1",
            [alert_id],
        )
        if not current.rows or current.rows[0][0] != "pending":
            await query.answer("Карточка уже обработана.", show_alert=True)
            return

        auto_muted = int(current.rows[0][1] or 0)
        alert_category = str(current.rows[0][2] or "TOXIC").upper()
        alert_message_id = current.rows[0][3]
        alert_parts = _split_categories(alert_category)

        try:
            if action == "wrong":
                if "SPAM" in alert_parts and alert_message_id is not None:
                    await db._exec(
                        "DELETE FROM ai_moderation_violations "
                        "WHERE chat_id=? AND message_id=?",
                        [chat_id, int(alert_message_id)],
                    )
                    remaining = await db._exec(
                        "SELECT COUNT(*) FROM ai_moderation_violations "
                        "WHERE chat_id=? AND user_id=? AND created_at>=? "
                        "AND category NOT LIKE '%SPAM%' "
                        "AND category NOT LIKE '%NSFL%' "
                        "AND category NOT LIKE '%NSFW%'",
                        [chat_id, user_id, int(time.time()) - WINDOW_SECONDS],
                    )
                    if auto_muted and (
                        not remaining.rows
                        or int(remaining.rows[0][0] or 0) < VIOLATION_THRESHOLD
                    ):
                        await context.bot.restrict_chat_member(
                            chat_id=chat_id,
                            user_id=user_id,
                            permissions=_permissions_unmuted(),
                        )
                else:
                    if auto_muted:
                        await context.bot.restrict_chat_member(
                            chat_id=chat_id,
                            user_id=user_id,
                            permissions=_permissions_unmuted(),
                        )
                    await db._exec(
                        "DELETE FROM ai_moderation_violations "
                        "WHERE chat_id=? AND user_id=? AND created_at>=?",
                        [chat_id, user_id, int(time.time()) - WINDOW_SECONDS],
                    )

                await db._exec(
                    "UPDATE ai_moderation_alerts SET status='false_positive' "
                    "WHERE alert_id=?",
                    [alert_id],
                )
                result_text = (
                    "❌ Администратор отменил срабатывание. "
                    + (
                        "Удалено только это спам-срабатывание."
                        if "SPAM" in alert_parts
                        else "Нарушения за последнее окно очищены."
                    )
                )

            elif action == "ban":
                await context.bot.ban_chat_member(
                    chat_id=chat_id, user_id=user_id
                )
                await db._exec(
                    "UPDATE ai_moderation_alerts SET status='banned' "
                    "WHERE alert_id=?",
                    [alert_id],
                )
                deleted, failed = await _delete_confirmed_violations(
                    context.bot, chat_id, user_id, include_text=True
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
                    f"Удалено: {deleted}; ошибок удаления: {failed}."
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
                    "UPDATE ai_moderation_alerts SET status='muted', "
                    "auto_muted=1, muted_until=? WHERE alert_id=?",
                    [int(time.time()) + MUTE_SECONDS, alert_id],
                )
                deleted, failed = await _delete_confirmed_violations(
                    context.bot, chat_id, user_id, include_text=True
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
                    f"Удалено: {deleted}; ошибок удаления: {failed}."
                )

            await query.answer("Решение сохранено.")

            if query.message:
                old_text = (
                    query.message.text_html or query.message.text or ""
                )
                await query.edit_message_text(
                    f"{old_text}\n\n<b>{html.escape(result_text)}</b>",
                    parse_mode=ParseMode.HTML,
                    reply_markup=None,
                    disable_web_page_preview=True,
                )

        except TelegramError:
            logger.exception("Moderation action failed: %s", action)
            await query.answer(
                "Telegram не выполнил действие. Проверь права бота.",
                show_alert=True,
            )
