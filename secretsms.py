"""Секретные сообщения: создание в ЛС, публикация в группе и чтение по доступу."""

import html
import logging
import os
import re
import sqlite3
import time
import uuid

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

logger = logging.getLogger(__name__)

SECRET_TTL = 24 * 60 * 60
SECRET_DB = os.getenv(
    "SECRET_DB_PATH",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "secret_messages.sqlite3"),
)
_SECRET_STAGES = {"recipients", "text", "confirm"}


def _connect():
    conn = sqlite3.connect(SECRET_DB, timeout=15)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS secret_messages ("
        "id TEXT PRIMARY KEY, sender_id INTEGER NOT NULL, "
        "sender_name TEXT NOT NULL, recipients TEXT NOT NULL, "
        "body TEXT NOT NULL, expires_at INTEGER NOT NULL)"
    )
    return conn


def init_db() -> None:
    """Создаёт таблицу при запуске приложения."""
    with _connect():
        pass


def cleanup_expired() -> int:
    """Удаляет секреты, срок действия которых истёк."""
    with _connect() as conn:
        cur = conn.execute(
            "DELETE FROM secret_messages WHERE expires_at <= ?", (int(time.time()),)
        )
        return cur.rowcount


def _parse_usernames(raw: str) -> list[str]:
    """Извлекает ВСЕ Telegram username из строки, в том числе после запятых.

    Поддерживает варианты «@username1, @username2» и «1 @username1, 2 @username2».
    В Telegram username должен содержать от 5 до 32 символов.
    """
    names = re.findall(r"@([A-Za-z0-9_]{5,32})(?![A-Za-z0-9_])", raw or "")
    result: list[str] = []
    for name in names:
        name = name.lower()
        if name not in result:
            result.append(name)
    return result


async def handle_private_update(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    group_chat_id: int,
) -> bool:
    """Обрабатывает сценарий +Секретное в личном чате; True означает, что update обработан."""
    user = update.effective_user
    chat = update.effective_chat
    msg = update.effective_message
    query = update.callback_query

    if not user or not chat or chat.type != "private":
        return False

    if query:
        data = query.data or ""
        if not data.startswith("secret:"):
            return False

        await query.answer()
        action = data.split(":", 1)[1]
        state = context.user_data.get("secret_message")
        if not state or state.get("stage") != "confirm":
            await query.edit_message_text("⌛ Этот шаг уже завершён. Начни заново: +Секретное")
            return True

        if action == "cancel":
            context.user_data.pop("secret_message", None)
            await query.edit_message_text("❌ Создание секретного сообщения отменено.")
            return True

        if action != "confirm":
            return True

        if not group_chat_id:
            await query.edit_message_text("⚠️ Не задан CHAT_ID — не могу отправить сообщение в группу.")
            context.user_data.pop("secret_message", None)
            return True

        secret_id = uuid.uuid4().hex[:16]
        expires_at = int(time.time()) + SECRET_TTL
        recipients = state["recipients"]
        sender_name = user.username or user.full_name or str(user.id)
        with _connect() as conn:
            conn.execute(
                "INSERT INTO secret_messages(id, sender_id, sender_name, recipients, body, expires_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (secret_id, user.id, sender_name, ",".join(recipients), state["body"], expires_at),
            )

        recipient_text = ", ".join("@" + name for name in recipients)
        sender_tag = f"@{html.escape(user.username)}" if user.username else html.escape(user.full_name)
        announcement = (
            f"🔐 <b>{sender_tag}</b> отправил(а) секретное сообщение для "
            f"<b>{html.escape(recipient_text)}</b>.\n\n"
            "Нажмите кнопку ниже, чтобы прочитать его. Сообщение действует 24 часа."
        )
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("🔓 Прочитать", callback_data=f"secretread:{secret_id}")
        ]])
        try:
            await context.bot.send_message(
                chat_id=group_chat_id,
                text=announcement,
                parse_mode=ParseMode.HTML,
                reply_markup=keyboard,
            )
        except Exception:
            logger.exception("Не удалось отправить секретное сообщение в CHAT_ID=%s", group_chat_id)
            with _connect() as conn:
                conn.execute("DELETE FROM secret_messages WHERE id = ?", (secret_id,))
            await query.edit_message_text(
                "⚠️ Не получилось отправить сообщение в группу. Проверь CHAT_ID и права бота."
            )
            context.user_data.pop("secret_message", None)
            return True

        context.user_data.pop("secret_message", None)
        await query.edit_message_text(
            "✅ Секретное сообщение отправлено в группу. Оно будет доступно 24 часа."
        )
        return True

    if not msg or not msg.text:
        return False

    text = msg.text.strip()
    state = context.user_data.get("secret_message")
    if text.lower() == "+секретное":
        context.user_data["secret_message"] = {"stage": "recipients"}
        await msg.reply_text(
            "🔐 Кому отправить секретное сообщение?\n"
            "Введи username через запятую: <code>@user, @user2</code>.\n"
            "У каждого получателя должен быть Telegram username.",
            parse_mode=ParseMode.HTML,
        )
        return True

    if not state or state.get("stage") not in _SECRET_STAGES:
        return False

    if state["stage"] == "recipients":
        recipients = _parse_usernames(text)
        if not recipients:
            await msg.reply_text("Не нашёл username. Введи, например: @user, @user2")
            return True
        if len(recipients) > 20:
            await msg.reply_text("Можно указать не более 20 получателей.")
            return True
        state["recipients"] = recipients
        state["stage"] = "text"
        await msg.reply_text("✍️ Теперь отправь сам текст секретного сообщения.")
        return True

    if state["stage"] == "text":
        if len(text) > 3500:
            await msg.reply_text("Слишком длинный текст. Максимум 3500 символов.")
            return True
        state["body"] = text
        state["stage"] = "confirm"
        recipient_text = ", ".join("@" + name for name in state["recipients"])
        preview = text if len(text) <= 700 else text[:700] + "…"
        await msg.reply_text(
            f"Проверь секретное сообщение.\nПолучатели: {recipient_text}\n\n"
            f"Текст:\n{preview}\n\nОтправить?",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Подтвердить", callback_data="secret:confirm"),
                InlineKeyboardButton("❌ Отказать", callback_data="secret:cancel"),
            ]]),
        )
        return True

    return False


async def read_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Показывает секрет только разрешённому пользователю, нажавшему кнопку."""
    query = update.callback_query
    user = update.effective_user
    if not query or not user:
        return

    secret_id = (query.data or "").partition(":")[2]
    cleanup_expired()
    with _connect() as conn:
        row = conn.execute(
            "SELECT recipients, body, expires_at FROM secret_messages WHERE id = ?",
            (secret_id,),
        ).fetchone()

    if not row or row[2] <= int(time.time()):
        await query.answer("⌛ Срок действия сообщения истёк или оно удалено.", show_alert=True)
        return

    username = (user.username or "").lower()
    allowed = {item for item in row[0].split(",") if item}
    if not username or username not in allowed:
        await query.answer("⛔ Это сообщение предназначено не тебе.", show_alert=True)
        return

    body = row[1]
    if len(body) <= 190:
        await query.answer(body, show_alert=True)
        return

    try:
        await context.bot.send_message(
            chat_id=user.id,
            text="🔐 Секретное сообщение:\n\n" + body,
        )
        await query.answer("🔐 Отправил секретный текст тебе в личные сообщения.", show_alert=True)
    except Exception:
        await query.answer(body[:190] + "… (полный текст не помещается в окно)", show_alert=True)


async def cleanup_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        removed = cleanup_expired()
        if removed:
            logger.info("secret messages cleanup: removed %s expired messages", removed)
    except Exception:
        logger.exception("Ошибка очистки секретных сообщений")
