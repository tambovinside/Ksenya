"""Мут на 10 секунд за точное сообщение «абракадабра».

Работает только с Telegram user_id из message.from_user.
Не использует accounts.py, привязки аккаунтов, подписки или middleware.
"""

import asyncio
import logging

from telegram import ChatPermissions, Update
from telegram.error import TelegramError
from telegram.ext import ContextTypes

logger = logging.getLogger(__name__)

MUTE_SECONDS = 10
REPLY_TEXT = "Ты что, куку? 🥴 Ну вот тебе мут на 10 сек — наколдовала! 🪄"


def is_gibberish(text: str) -> bool:
    """Распознаёт только отдельное слово, без учёта регистра и пробелов по краям."""
    return (text or "").strip().casefold() == "абракадабра"


async def _restore_permissions(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Снимает временный мут через Telegram JobQueue."""
    data = context.job.data
    try:
        await context.bot.restrict_chat_member(
            chat_id=data["chat_id"],
            user_id=data["user_id"],
            permissions=data["permissions"],
        )
        logger.info(
            "Мут снят: chat_id=%s user_id=%s",
            data["chat_id"], data["user_id"],
        )
    except TelegramError:
        logger.exception(
            "Не удалось снять мут: chat_id=%s user_id=%s",
            data["chat_id"], data["user_id"],
        )


async def _restore_after_delay(bot, chat_id: int, user_id: int,
                               permissions: ChatPermissions) -> None:
    """Запасной способ снятия мута, если JobQueue не подключён."""
    await asyncio.sleep(MUTE_SECONDS)
    try:
        await bot.restrict_chat_member(
            chat_id=chat_id,
            user_id=user_id,
            permissions=permissions,
        )
        logger.info("Мут снят: chat_id=%s user_id=%s", chat_id, user_id)
    except TelegramError:
        logger.exception(
            "Не удалось снять мут: chat_id=%s user_id=%s", chat_id, user_id
        )


async def check_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Выдаёт мут именно Telegram-пользователю, отправившему слово."""
    message = update.effective_message
    chat = update.effective_chat

    if message is None or chat is None:
        return
    if chat.type not in ("group", "supergroup"):
        return
    if not is_gibberish(message.text or ""):
        return

    # Никаких accounts.actual_user() и связанных аккаунтов:
    # используем исключительно настоящий Telegram ID автора сообщения.
    telegram_user = message.from_user
    if telegram_user is None or telegram_user.is_bot:
        return

    try:
        # В PTB 20+ у Chat нет .permissions — права берём из get_chat.
        chat_info = await context.bot.get_chat(chat.id)
        restore_permissions = chat_info.permissions
        if restore_permissions is None:
            restore_permissions = ChatPermissions(can_send_messages=True)

        # Запрещаем отправку сообщений только этому Telegram user_id.
        await context.bot.restrict_chat_member(
            chat_id=chat.id,
            user_id=telegram_user.id,
            permissions=ChatPermissions(can_send_messages=False),
        )

        job_queue = context.job_queue
        if job_queue is not None:
            job_queue.run_once(
                _restore_permissions,
                when=MUTE_SECONDS,
                data={
                    "chat_id": chat.id,
                    "user_id": telegram_user.id,
                    "permissions": restore_permissions,
                },
                name=f"abrakadabra:{chat.id}:{telegram_user.id}",
            )
        else:
            asyncio.create_task(
                _restore_after_delay(
                    context.bot, chat.id, telegram_user.id, restore_permissions
                )
            )

        logger.info(
            "Выдан мут на %s сек.: chat_id=%s telegram_user_id=%s",
            MUTE_SECONDS, chat.id, telegram_user.id,
        )
        await message.reply_text(REPLY_TEXT)

    except TelegramError:
        logger.exception(
            "Telegram не смог выдать мут: chat_id=%s telegram_user_id=%s",
            chat.id, telegram_user.id,
        )
        try:
            await message.reply_text(
                "Не получилось выдать мут. Проверь, что бот — администратор "
                "и у него есть право блокировать участников."
            )
        except TelegramError:
            logger.exception("Не удалось отправить сообщение об ошибке мута")
