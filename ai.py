
"""ИИ-модуль: запросы к Pollinations + контекст диалога."""
import os
import asyncio
import time
import logging
from typing import Optional
from urllib.parse import urlparse
from contextlib import asynccontextmanager

import aiohttp
import db
from cachetools import TTLCache

logger = logging.getLogger(__name__)

AI_TOKEN = os.getenv("AI_TOKEN", "").strip()
BASE_URL = os.getenv("BASE_URL", "").strip().rstrip("/")

MODEL = "llama"

INSUFFICIENT_TOKENS_PREFIX = "🧠 Недостаточно токенов:"
MAX_PROMPT_CHARS = 16000
MAX_OUTPUT_TOKENS = 4096

COOLDOWN_SEC = 5.0
MAX_HISTORY = 10

_last_request = TTLCache(maxsize=10000, ttl=COOLDOWN_SEC * 2)
_cooldown_lock = asyncio.Lock()

_history = TTLCache(maxsize=5000, ttl=3600)

# Не допускаем одновременную обработку запросов одного пользователя.
_user_locks: dict[int, asyncio.Lock] = {}
_user_lock_users: dict[int, int] = {}
_user_locks_guard = asyncio.Lock()


@asynccontextmanager
async def _user_lock(user_id: int):
    # Refcount prevents a lock from being removed between lookup and acquire.
    async with _user_locks_guard:
        lock = _user_locks.setdefault(user_id, asyncio.Lock())
        _user_lock_users[user_id] = _user_lock_users.get(user_id, 0) + 1
    try:
        async with lock:
            yield
    finally:
        async with _user_locks_guard:
            remaining = _user_lock_users.get(user_id, 1) - 1
            if remaining <= 0:
                _user_lock_users.pop(user_id, None)
                if _user_locks.get(user_id) is lock and not lock.locked():
                    _user_locks.pop(user_id, None)
            else:
                _user_lock_users[user_id] = remaining


_session: Optional[aiohttp.ClientSession] = None
_session_lock = asyncio.Lock()


async def _get_session() -> aiohttp.ClientSession:
    global _session

    async with _session_lock:
        if _session is None or _session.closed:
            _session = aiohttp.ClientSession(
                connector=aiohttp.TCPConnector(
                    limit=100,
                    limit_per_host=20,
                    ttl_dns_cache=300,
                ),
                timeout=aiohttp.ClientTimeout(
                    total=120,
                    connect=15,
                    sock_read=90,
                ),
            )
        return _session


async def close_session() -> None:
    global _session

    async with _session_lock:
        if _session and not _session.closed:
            await _session.close()
        _session = None


def _api_url() -> str:
    if not BASE_URL:
        logger.error("ai: BASE_URL не задан")
        return ""

    parsed = urlparse(BASE_URL)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        logger.error("ai: неправильный BASE_URL")
        return ""

    if BASE_URL.endswith("/v1/chat/completions"):
        return BASE_URL

    if BASE_URL.endswith("/v1"):
        return f"{BASE_URL}/chat/completions"

    return f"{BASE_URL}/v1/chat/completions"


def reset_history(user_id: int) -> None:
    _history.pop(user_id, None)


def _get_history(user_id: int) -> list:
    try:
        return _history[user_id]
    except KeyError:
        _history[user_id] = []
        return _history[user_id]


def _append(user_id: int, role: str, content: str) -> None:
    history = _get_history(user_id)

    history.append({
        "role": role,
        "content": content,
    })

    while len(history) > MAX_HISTORY:
        history.pop(0)


def count_words(text: str) -> int:
    return len((text or "").split())


async def ask_ai(
    prompt: str,
    user_id: Optional[int] = None,
) -> Optional[str]:

    if not AI_TOKEN:
        logger.error("ai: AI_TOKEN не задан")
        return None

    url = _api_url()

    if not url:
        return None

    prompt = (prompt or "").strip()

    if not prompt:
        return None

    if len(prompt) > MAX_PROMPT_CHARS:
        prompt = prompt[:MAX_PROMPT_CHARS]

    if user_id is not None:
        user_id = int(user_id)
        async with _user_lock(user_id):
            return await _ask_ai_locked(prompt, user_id, url)

    return await _ask_ai_locked(prompt, None, url)


async def _ask_ai_locked(
    prompt: str,
    user_id: Optional[int],
    url: str,
) -> Optional[str]:
    token_cost = max(1, count_words(prompt))
    input_charged = False
    output_charged = 0
    success = False

    try:
        if user_id is not None:
            input_charged = bool(await db.consume_ai_tokens(user_id, token_cost))
            if not input_charged:
                balance = await db.get_ai_tokens(user_id)
                return (
                    f"{INSUFFICIENT_TOKENS_PREFIX} нужно {token_cost}, "
                    f"у тебя {balance}.\n"
                    "Купи токены в «Магазине» → «Токены ИИ»."
                )

        messages = [{
            "role": "system",
            "content": (
                "Ты умный ассистент женского пола по имени Ксения. "
                "Пиши пошленько как сучка. Не нужен пафос. "
                "Будь на лайтах. Пиши максимально коротко и не сухо!"
            ),
        }]
        if user_id is not None:
            messages.extend(dict(message) for message in _get_history(user_id))
        messages.append({"role": "user", "content": prompt})

        payload = {"model": MODEL, "messages": messages, "max_tokens": MAX_OUTPUT_TOKENS}
        headers = {"Content-Type": "application/json", "Authorization": f"Bearer {AI_TOKEN}"}
        session = await _get_session()
        async with session.post(url, json=payload, headers=headers) as resp:
            if resp.status in (401, 402):
                logger.error("ai: HTTP %s — проверьте настройки API/баланс провайдера", resp.status)
                return None
            if resp.status == 429:
                logger.warning("ai: 429 Rate Limit: %s", resp.headers.get("Retry-After", "?"))
                return None
            if resp.status != 200:
                body = await resp.text()
                logger.warning("ai: HTTP %s — %s", resp.status, body[:500])
                return None
            data = await resp.json(content_type=None)

        if not isinstance(data, dict):
            logger.warning("ai: API вернул JSON неожиданного типа")
            return None
        choices = data.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            logger.warning("ai: пустой или некорректный ответ API")
            return None
        choice = choices[0]
        if choice.get("finish_reason") == "length":
            logger.warning("ai: достигнут лимит ответа")
        message = choice.get("message")
        if not isinstance(message, dict) or not isinstance(message.get("content"), str):
            logger.warning("ai: отсутствует корректный message.content")
            return None
        content = message["content"].strip()
        if not content:
            return None

        output_cost = count_words(content)
        if user_id is not None and output_cost:
            if not await db.consume_ai_tokens(user_id, output_cost):
                try:
                    refunded = await db.refund_ai_tokens(user_id, token_cost)
                    if refunded is False:
                        logger.error("ai: не удалось вернуть оплату запроса user=%s", user_id)
                    else:
                        input_charged = False
                except Exception:
                    logger.exception("ai: ошибка возврата оплаты запроса user=%s", user_id)
                balance = await db.get_ai_tokens(user_id)
                total_needed = token_cost + output_cost
                return (
                    f"{INSUFFICIENT_TOKENS_PREFIX} нужно {total_needed} "
                    f"(запрос: {token_cost}, ответ: {output_cost}), "
                    f"у тебя {balance}.\n"
                    "Купи токены в «Магазине» → «Токены ИИ»."
                )
            output_charged = output_cost

        if user_id is not None:
            _append(user_id, "user", prompt)
            _append(user_id, "assistant", content)
        success = True
        return content
    except asyncio.CancelledError:
        raise
    except asyncio.TimeoutError:
        logger.warning("ai: таймаут запроса")
        return None
    except aiohttp.ClientError as exc:
        logger.warning("ai: ошибка сети: %s", exc)
        return None
    except Exception as exc:
        logger.exception("ai: неожиданная ошибка: %s", exc)
        return None
    finally:
        if user_id is not None and not success:
            refund_amount = (token_cost if input_charged else 0) + output_charged
            if refund_amount:
                try:
                    refunded = await db.refund_ai_tokens(user_id, refund_amount)
                    if refunded is False:
                        logger.error("ai: не удалось вернуть %s токенов user=%s", refund_amount, user_id)
                except Exception:
                    logger.exception("ai: ошибка возврата %s токенов user=%s", refund_amount, user_id)


async def try_acquire(user_id: int) -> float:
    try:
        user_id = int(user_id)
    except (TypeError, ValueError):
        raise ValueError("user_id должен быть целым числом")

    async with _cooldown_lock:
        now = time.monotonic()
        last = _last_request.get(user_id)

        if last is not None:
            elapsed = now - last

            if elapsed < COOLDOWN_SEC:
                return COOLDOWN_SEC - elapsed

        _last_request[user_id] = now

        return 0.0
