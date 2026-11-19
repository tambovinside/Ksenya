"""Резолвинг @username -> user_id через Telethon без файловой сессии.

Сессия читается непосредственно из переменных окружения Render.
Поддерживаемые имена: TG_SESSION, TG_SESSION_STRING, TELETHON_SESSION,
STRING_SESSION, SESSION_STRING. Также поддерживается TG_SESSION_B64,
если в нём находится Base64-кодированная строка Telethon StringSession.

Если сессия не задана/невалидна, бот продолжает работать, а username
разрешается через БД.
"""

import os
import base64
import logging
from typing import Optional

logger = logging.getLogger(__name__)

API_ID = int(os.getenv("API_ID", "0") or "0")
API_HASH = (os.getenv("API_HASH", "") or "").strip()

_client = None
_started = False


def _get_session_string() -> str:
    """Возвращает StringSession из Render Environment Variables.

    Никаких файлов не создаёт и не читает.
    """
    for key in (
        "TG_SESSION",
        "TG_SESSION_STRING",
        "TELETHON_SESSION",
        "STRING_SESSION",
        "SESSION_STRING",
    ):
        value = (os.getenv(key) or "").strip()
        if value:
            logger.info("Telethon: строка сессии получена из переменной %s", key)
            return value

    # Поддержка старого имени переменной: здесь ожидается именно
    # Base64 от ТЕКСТОВОЙ StringSession, а не SQLite-файл .session.
    encoded = (os.getenv("TG_SESSION_B64") or "").strip()
    if encoded:
        try:
            decoded = base64.b64decode(encoded, validate=True).decode("utf-8").strip()
            if decoded:
                logger.info("Telethon: StringSession декодирована из TG_SESSION_B64")
                return decoded
        except Exception as exc:
            raise RuntimeError(
                "TG_SESSION_B64 не содержит Base64-кодированную текстовую StringSession. "
                "Для работы без файла укажи строку Telethon в TG_SESSION."
            ) from exc

    raise RuntimeError(
        "Не найдена строка Telethon-сессии. Добавь её в Render → Environment "
        "под именем TG_SESSION (формат Telethon StringSession)."
    )


def get_client():
    global _client
    if _client is None:
        if not API_ID or not API_HASH:
            raise RuntimeError("API_ID / API_HASH не заданы")

        from telethon import TelegramClient
        from telethon.sessions import StringSession

        session_string = _get_session_string()
        try:
            session = StringSession(session_string)
        except Exception as exc:
            raise RuntimeError(
                "Значение переменной сессии не является корректной Telethon StringSession. "
                "Нужна строка, полученная через StringSession.save()."
            ) from exc

        _client = TelegramClient(session, API_ID, API_HASH)
    return _client


async def start_client() -> None:
    """Подключает Telethon; ошибка не останавливает основной Telegram-бот."""
    global _started
    try:
        client = get_client()
        await client.connect()

        if not await client.is_user_authorized():
            logger.warning(
                "Telethon: сессия не авторизована. Резолв @username будет только через БД."
            )
            await client.disconnect()
            _started = False
            return

        _started = True
        logger.info("Telethon подключён (StringSession из Environment; без файла)")
    except Exception as exc:
        _started = False
        logger.warning(
            "Telethon НЕ подключён (%s). Резолв @username будет только через БД. "
            "Сервис продолжает работать.",
            exc,
        )


async def stop_client() -> None:
    global _client, _started
    if _client is not None:
        try:
            await _client.disconnect()
        except Exception:
            pass
        _client = None
    _started = False


async def resolve_username(username: str) -> Optional[tuple[int, str]]:
    if not _started or _client is None:
        return None

    uname = (username or "").lstrip("@").strip()
    if not uname:
        return None

    try:
        from telethon.errors import FloodWaitError

        entity = await _client.get_entity(uname)
        first_name = getattr(entity, "first_name", None)
        title = getattr(entity, "title", None)
        return entity.id, (first_name or title or uname)
    except FloodWaitError as exc:
        logger.warning("resolve @%s: FloodWait %s сек", uname, exc.seconds)
        return None
    except Exception as exc:
        logger.info("resolve @%s: %s", uname, exc)
        return None
