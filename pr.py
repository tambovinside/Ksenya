"""Логика PR-бота: проверка подписки, удаление сообщений, приглашение."""

import os
import time
import asyncio
import logging
from html import escape

from telegram import (
    Update,
    ChatMember,
    InlineKeyboardMarkup,
    Message,
)
from telegram.constants import ParseMode, ChatType
from telegram.error import TelegramError
from telegram.ext import ContextTypes, ApplicationHandlerStop

from ui_design import InlineKeyboardButton

import accounts
import db
import abrakadabra

logger = logging.getLogger(__name__)

# Настройки канала из переменных окружения Render.
CANAL_ID = os.getenv("CANAL_ID")
CANAL_URL = os.getenv("CANAL_URL", "https://t.me/")
CANAL_TITLE = os.getenv("CANAL_TITLE", "наш канал")

# Кэш проверки подписки.
_SUBSCRIPTION_CACHE: dict[int, tuple[float, bool]] = {}
_SUBSCRIPTION_LOCKS = tuple(asyncio.Lock() for _ in range(64))

_USER_SYNC_CACHE: dict[int, tuple[float, str, str]] = {}

_SUBSCRIPTION_TTL = 5
_USER_SYNC_TTL = 3600
_CACHE_MAX_USERS = 10000


def _trim_caches(now: float) -> None:
    if len(_SUBSCRIPTION_CACHE) > _CACHE_MAX_USERS:
        for uid, (expires, _) in list(_SUBSCRIPTION_CACHE.items()):
            if expires <= now:
                _SUBSCRIPTION_CACHE.pop(uid, None)

        if len(_SUBSCRIPTION_CACHE) > _CACHE_MAX_USERS:
            _SUBSCRIPTION_CACHE.clear()

    if len(_USER_SYNC_CACHE) > _CACHE_MAX_USERS:
        for uid, (expires, _, _) in list(_USER_SYNC_CACHE.items()):
            if expires <= now:
                _USER_SYNC_CACHE.pop(uid, None)

        if len(_USER_SYNC_CACHE) > _CACHE_MAX_USERS:
            _USER_SYNC_CACHE.clear()


async def _sync_user_if_needed(user) -> None:
    now = time.monotonic()
    current = (user.username or "", user.first_name or "")

    cached = _USER_SYNC_CACHE.get(user.id)

    if cached and cached[0] > now and cached[1:] == current:
        return

    await db.upsert_user(
        user.id,
        user.username,
        user.first_name,
    )

    _USER_SYNC_CACHE[user.id] = (
        now + _USER_SYNC_TTL,
        *current,
    )

    _trim_caches(now)


async def _cached_subscription(bot, user_id: int) -> bool | None:
    now = time.monotonic()

    cached = _SUBSCRIPTION_CACHE.get(user_id)

    if cached and cached[0] > now:
        return cached[1]

    lock = _SUBSCRIPTION_LOCKS[
        hash(user_id) % len(_SUBSCRIPTION_LOCKS)
    ]

    async with lock:
        now = time.monotonic()
        cached = _SUBSCRIPTION_CACHE.get(user_id)

        if cached and cached[0] > now:
            return cached[1]

        result = await is_subscribed(bot, user_id)

        if result is not None:
            # Кэшируем только подтверждённую подписку. Отрицательный результат
            # не кэшируется: после подписки следующий запрос сразу перепроверит её.
            if result:
                _SUBSCRIPTION_CACHE[user_id] = (
                    now + _SUBSCRIPTION_TTL,
                    True,
                )
            else:
                _SUBSCRIPTION_CACHE.pop(user_id, None)
            await db.set_subscription(user_id, result)

        _trim_caches(now)

        return result


_ADMIN_WORDS = {
    "топ", "шип", "сделать", "профиль", "мой", "мое",
    "список", "поймать", "хо",
    "stats", "start", "ping", "check",
    "help", "restart", "казино",
    "ии", "браин", "браини", "мозг", "brainy", "brain",
    "перевод", "казна", "кредит", "монеты",
    "дроч", "аура", "дать", "поцеловать", "обнять",
    "побить", "обоссать", "погладить",
}


def _looks_like_admin_command(text: str) -> bool:
    if not text:
        return False

    stripped = text.lstrip()

    if not stripped:
        return False

    if stripped[0] in "+-–−":
        return True

    first = stripped.split(maxsplit=1)[0].casefold().strip(".,!?:;")

    return first in _ADMIN_WORDS


def _is_reply_to_bot(msg: Message, bot_id: int) -> bool:
    if not msg.reply_to_message or not msg.reply_to_message.from_user:
        return False

    return msg.reply_to_message.from_user.id == bot_id


def _is_normal_user_message(update: Update) -> bool:
    """Разрешаем проверку только обычных сообщений пользователей."""

    msg = update.effective_message
    chat = update.effective_chat
    user = accounts.actual_user(update) or (msg.from_user if msg else None)

    if not msg or not user or not chat:
        return False

    if user.is_bot:
        return False

    if chat.type not in (
        ChatType.GROUP,
        ChatType.SUPERGROUP,
    ):
        return False

    if msg.sender_chat is not None:
        return False

    if msg.new_chat_members or msg.left_chat_member:
        return False

    if not msg.from_user:
        return False

    return True


async def is_subscribed(bot, user_id: int) -> bool | None:
    """Проверяет подписку пользователя."""

    if not CANAL_ID:
        logger.warning(
            "CANAL_ID не задан: проверка подписки отключена"
        )
        return True

    try:
        member = await bot.get_chat_member(
            chat_id=CANAL_ID,
            user_id=user_id,
        )

        if member.status == ChatMember.RESTRICTED:
            return bool(member.is_member)
        return member.status not in (
            ChatMember.LEFT,
            ChatMember.BANNED,
        )

    except TelegramError as e:
        logger.warning(
            "Не удалось проверить подписку user=%s canal=%s: %s. "
            "Статус не изменён; проверьте CANAL_ID и права бота.",
            user_id,
            CANAL_ID,
            e,
        )

        return None


def _subscription_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "📢 Подписаться на канал",
                url=CANAL_URL,
            )
        ]
    ])


def _subscription_text(user) -> str:
    name = escape(user.first_name or "друг")

    if user.username:
        link = (
            f'<a href="https://t.me/{user.username}">'
            f'{name}</a>'
        )
    else:
        link = (
            f'<a href="tg://user?id={user.id}">'
            f'{name}</a>'
        )

    return (
        f"👋 {link}, чтобы писать в чат, сначала подпишитесь на "
        f"<b>{escape(CANAL_TITLE)}</b>.\n\n"
        "После подписки возвращайтесь — и пишите. ✅"
    )


async def check_and_delete(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:

    if not _is_normal_user_message(update):
        return

    msg = update.effective_message
    # Подписка и профиль Telegram — всегда реальный аккаунт отправителя.
    real_user = accounts.actual_user(update) or (msg.from_user if msg else None)
    if not real_user:
        return

    try:
        bot_id = context.bot.id
    except Exception:
        bot_id = 0

    if bot_id and _is_reply_to_bot(msg, bot_id):
        return

    # Для точного слова выполняем проверку подписки и статуса в группе
    # одновременно. Раньше эти Telegram-запросы шли последовательно
    # (а затем ещё отдельно запускался обработчик абракадабры).
    if abrakadabra.is_gibberish(msg.text or ""):
        subscription_task = asyncio.create_task(
            _cached_subscription(context.bot, real_user.id)
        )
        member_task = asyncio.create_task(
            context.bot.get_chat_member(msg.chat_id, real_user.id)
        )
        sync_task = asyncio.create_task(_sync_user_if_needed(real_user))

        results = await asyncio.gather(
            subscription_task, member_task, sync_task,
            return_exceptions=True,
        )
        ok, group_member, sync_result = results

        if isinstance(sync_result, Exception):
            logger.warning("upsert_user для абракадабры: %s", sync_result)

        if isinstance(ok, Exception):
            logger.error(
                "Ошибка проверки подписки для абракадабры user=%s: %s",
                real_user.id, ok,
                exc_info=(type(ok), ok, ok.__traceback__),
            )
            return
        if isinstance(group_member, Exception):
            logger.warning(
                "Не удалось проверить статус участника для абракадабры "
                "user=%s chat=%s: %s",
                real_user.id, msg.chat_id, group_member,
            )
            return

        if ok is None:
            # Не выдаём мут, если подписку не удалось достоверно проверить.
            return

        if not ok:
            try:
                await msg.delete()
            except TelegramError as e:
                logger.info("Не удалось удалить сообщение: %s", e)
            try:
                await context.bot.send_message(
                    chat_id=msg.chat_id,
                    text=_subscription_text(real_user),
                    parse_mode=ParseMode.HTML,
                    reply_markup=_subscription_keyboard(),
                    disable_web_page_preview=True,
                )
            except TelegramError as e:
                logger.warning("Не удалось отправить напоминание: %s", e)
            raise ApplicationHandlerStop

        # Подписка подтверждена, статус группы уже проверен — передаём
        # готовый результат, чтобы не делать второй get_chat_member.
        await abrakadabra.check_message(
            update, context, member=group_member
        )
        return

    try:
        await _sync_user_if_needed(real_user)
    except Exception as e:
        logger.warning("upsert_user: %s", e)

    try:
        ok = await _cached_subscription(
            context.bot,
            real_user.id,
        )
    except Exception:
        logger.exception(
            "Ошибка кэшированной проверки подписки user=%s",
            real_user.id,
        )
        return

    if ok is None:
        return

    if ok:
        return

    try:
        await msg.delete()
    except TelegramError as e:
        logger.info(
            "Не удалось удалить сообщение: %s",
            e,
        )

    try:
        await context.bot.send_message(
            chat_id=msg.chat_id,
            text=_subscription_text(real_user),
            parse_mode=ParseMode.HTML,
            reply_markup=_subscription_keyboard(),
            disable_web_page_preview=True,
        )
    except TelegramError as e:
        logger.warning(
            "Не удалось отправить напоминание: %s",
            e,
        )

    # Не передаём удалённое сообщение следующим обработчикам команд.
    raise ApplicationHandlerStop


async def on_new_member(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:

    msg = update.effective_message

    if not msg or not msg.new_chat_members:
        return

    for member in msg.new_chat_members:

        if member.is_bot:
            continue

        try:
            await _sync_user_if_needed(member)

            ok = await _cached_subscription(
                context.bot,
                member.id,
            )

        except Exception as e:
            logger.warning(
                "Ошибка обработки нового участника: %s",
                e,
            )
            continue

        if ok is None or ok:
            continue

        try:
            await context.bot.send_message(
                chat_id=msg.chat_id,
                text=_subscription_text(member),
                parse_mode=ParseMode.HTML,
                reply_markup=_subscription_keyboard(),
                disable_web_page_preview=True,
            )

        except TelegramError as e:
            logger.warning(
                "Приветствие не отправлено: %s",
                e,
            )


async def recheck_subscription(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:

    user = accounts.actual_user(update) or update.effective_user
    msg = update.effective_message

    if not user or user.is_bot or not msg:
        return

    if not update.effective_chat:
        return

    if update.effective_chat.type not in (
        ChatType.GROUP,
        ChatType.SUPERGROUP,
    ):
        return

    ok = await is_subscribed(
        context.bot,
        user.id,
    )

    if ok is None:
        await msg.reply_text(
            "⚠️ Сейчас не удалось проверить подписку через Telegram. "
            "Попробуйте позже. Администратору нужно проверить, что бот "
            "добавлен в канал и имеет права администратора."
        )
        return

    await db.set_subscription(user.id, ok)

    if ok:
        _SUBSCRIPTION_CACHE[user.id] = (
            time.monotonic() + _SUBSCRIPTION_TTL,
            True,
        )
    else:
        _SUBSCRIPTION_CACHE.pop(user.id, None)

    if ok:
        await msg.reply_text(
            "✅ Подписка активна — можете писать в чат."
        )

    else:
        await msg.reply_text(
            f"❌ Вы пока не подписаны на «{escape(CANAL_TITLE)}».\n"
            "Подпишитесь и возвращайтесь — после этого сообщения "
            "будут проходить.",
            parse_mode=ParseMode.HTML,
            reply_markup=_subscription_keyboard(),
        )
