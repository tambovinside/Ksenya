"""Личный кабинет предложки TAMBOV INSIDE.

Обычные пользователи в личке работают только с предложениями/жалобами.
Групповые функции Superbot этот модуль не затрагивает.
"""
import logging
import os
import secrets
from typing import Optional

from telegram import (
    Update, InlineKeyboardMarkup, ReplyKeyboardMarkup,
    KeyboardButton, ReplyKeyboardRemove,
)
from telegram.ext import ContextTypes, ApplicationHandlerStop

from ui_design import InlineKeyboardButton

import accounts
import db

logger = logging.getLogger(__name__)

MENU_TEXT = (
    "Добрый день! Вы написали в предложку TAMBOV INSIDE.\n\n"
    "Выберите, с чем хотите обратиться:"
)
MENU = ReplyKeyboardMarkup(
    [
        [KeyboardButton("💡 Предложить идею")],
        [KeyboardButton("📰 Предложить пост")],
        [KeyboardButton("⚠️ Жалоба")],
        [KeyboardButton("❓ Почему отклонили пост/идею")],
    ],
    resize_keyboard=True,
    is_persistent=True,
)
PROMPTS = {
    "idea": "💡 Опишите вашу идею как можно подробнее. Мы передадим её на рассмотрение администрации.",
    "post": "📰 Отправьте текст поста. Если к публикации нужны фотографии или видео, укажите это в сообщении.",
    "complaint": "⚠️ Опишите ситуацию и укажите, на кого или на что вы жалуетесь. Обращение увидит администрация.",
}
TYPE_NAMES = {"idea": "ИДЕЯ", "post": "ПОСТ", "complaint": "ЖАЛОБА"}
REASONS = {
    "adult": "Материал содержит контент 18+ или неприемлемые для публикации подробности.",
    "profanity": "В тексте обнаружена ненормативная лексика. Пожалуйста, отправьте исправленный вариант.",
    "spam": "Материал не подходит для публикации: мало смысла, отсутствует полезная информация или это спам.",
}
MAX_TEXT = 3500

TECH_ERROR = "⚠️ Техническая ошибка. Попробуйте ещё раз через минуту."


# ---------------------------------------------------------------------------
# Единая безопасная обёртка над db._exec
# ---------------------------------------------------------------------------

def _db_failed(result) -> bool:
    """Считаем результат неуспешным, если он None или .error/.success говорят о сбое."""
    if result is None:
        return True
    if getattr(result, "error", False):
        return True
    if getattr(result, "success", True) is False:
        return True
    return False


async def _db_exec_safe(*args, **kwargs):
    """Никогда не бросает наружу.

    Возвращает (result, error_marker):
      * result       — результат db._exec, если всё ок;
      * error_marker — строка-причина, если что-то пошло не так.
    Ровно одно из двух будет None.
    """
    try:
        result = await db._exec(*args, **kwargs)
    except Exception:
        logger.exception("helper: db._exec crashed")
        return None, "crash"
    if _db_failed(result):
        logger.error("helper: db._exec returned error result: %r", result)
        return None, "error"
    return result, None


async def init_db() -> None:
    """Создаёт helper_submissions и верифицирует, что таблица реально есть."""
    _, err = await _db_exec_safe("""
        CREATE TABLE IF NOT EXISTS helper_submissions (
            id TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            username TEXT,
            first_name TEXT,
            kind TEXT NOT NULL,
            body TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            rejection_reason TEXT,
            admin_comment TEXT,
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now'))
        )
    """)
    if err:
        raise RuntimeError("Не удалось создать таблицу helper_submissions")

    check, check_err = await _db_exec_safe(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='helper_submissions'"
    )
    if check_err or not getattr(check, "rows", None):
        raise RuntimeError("Таблица helper_submissions отсутствует после CREATE")


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------

async def start_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if not msg:
        return
    context.user_data.pop("helper_state", None)
    try:
        await msg.reply_text(MENU_TEXT, reply_markup=MENU)
    except Exception:
        logger.exception("start_menu: не удалось отправить меню")


# ---------------------------------------------------------------------------
# Отправка обращения
# ---------------------------------------------------------------------------

async def _notify_admin(update: Update, context: ContextTypes.DEFAULT_TYPE,
                        kind: str, body: str, submission_id: str) -> bool:
    user = update.effective_user
    if not user:
        return False
    user_label = f"{user.first_name or 'Пользователь'}"
    if user.username:
        user_label += f" (@{user.username})"
    admin_text = (
        f"📥 НОВОЕ ОБРАЩЕНИЕ · {TYPE_NAMES[kind]}\n"
        f"От: {user_label}\nID: {user.id}\n"
        f"Номер: {submission_id}\n\n"
        f"{body}"
    )
    if kind in ("idea", "post"):
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Принять", callback_data=f"sug:accept:{submission_id}"),
            InlineKeyboardButton("❌ Отказать", callback_data=f"sug:reject:{submission_id}"),
        ]])
    else:
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton("💬 Дать комментарий", callback_data=f"sug:comment:{submission_id}")
        ]])

    admin_chat = int(os.getenv("ADMIN_ID", "0") or 0)
    if not admin_chat:
        logger.error("ADMIN_ID не задан — уведомление не отправлено: %s", submission_id)
        return False

    try:
        await context.bot.send_message(
            chat_id=admin_chat, text=admin_text, reply_markup=keyboard,
        )
        return True
    except Exception:
        logger.exception("Не удалось доставить обращение админу: %s", submission_id)
        return False


async def _create_submission(update: Update, context: ContextTypes.DEFAULT_TYPE,
                             kind: str, body: str) -> None:
    user = update.effective_user
    msg = update.effective_message
    if not user or not msg:
        return
    submission_id = secrets.token_hex(5)

    _, err = await _db_exec_safe(
        """INSERT INTO helper_submissions
           (id, user_id, username, first_name, kind, body)
           VALUES (?, ?, ?, ?, ?, ?)""",
        [submission_id, user.id, user.username or "", user.first_name or "", kind, body],
    )
    if err:
        logger.error(
            "helper: INSERT failed user_id=%s kind=%s (err=%s)",
            user.id, kind, err,
        )
        try:
            await msg.reply_text(
                "Не получилось отправить обращение из-за технической ошибки. "
                "Попробуйте немного позже.",
                reply_markup=MENU,
            )
        except Exception:
            logger.exception("helper: не удалось уведомить пользователя об ошибке INSERT")
        return

    try:
        await msg.reply_text(
            "✅ Спасибо! Ваше обращение отправлено администрации TAMBOV INSIDE.\n"
            "Ожидайте ответа в течение 2 часов.",
            reply_markup=MENU,
        )
    except Exception:
        logger.exception("helper: не удалось подтвердить приём обращения")

    ok = await _notify_admin(update, context, kind, body, submission_id)
    if not ok:
        try:
            await msg.reply_text(
                "⚠️ Обращение сохранено, но уведомить администратора не удалось. "
                "Оно не потерялось — администрация увидит его позже.",
                reply_markup=MENU,
            )
        except Exception:
            logger.exception("helper: не удалось отправить доп. уведомление")


# ---------------------------------------------------------------------------
# Обработка текста пользователя
# ---------------------------------------------------------------------------

async def _handle_user_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    user = update.effective_user
    if not msg or not user:
        return

    try:
        text = (msg.text or "").strip()
        state = context.user_data.get("helper_state")

        if state in ("idea", "post", "complaint"):
            if not text:
                await msg.reply_text(
                    "Пожалуйста, отправьте обращение текстом. "
                    "Если нужно приложить медиа, добавьте ссылку или пояснение."
                )
                return
            if len(text) > MAX_TEXT:
                await msg.reply_text(
                    f"Сообщение слишком длинное. Максимум — {MAX_TEXT} символов. "
                    "Сократите текст и отправьте ещё раз."
                )
                return
            context.user_data.pop("helper_state", None)
            await _create_submission(update, context, state, text)
            return

        if text in ("💡 Предложить идею", "Предложить идею"):
            context.user_data["helper_state"] = "idea"
            await msg.reply_text(PROMPTS["idea"], reply_markup=ReplyKeyboardRemove())
        elif text in ("📰 Предложить пост", "Предложить пост"):
            context.user_data["helper_state"] = "post"
            await msg.reply_text(PROMPTS["post"], reply_markup=ReplyKeyboardRemove())
        elif text in ("⚠️ Жалоба", "Жалоба"):
            context.user_data["helper_state"] = "complaint"
            await msg.reply_text(PROMPTS["complaint"], reply_markup=ReplyKeyboardRemove())
        elif text in ("❓ Почему отклонили пост/идею", "Почему отклонили пост/идею"):
            rs, err = await _db_exec_safe(
                """SELECT rejection_reason FROM helper_submissions
                   WHERE user_id = ? AND status = 'rejected' AND rejection_reason IS NOT NULL
                   ORDER BY created_at DESC LIMIT 1""",
                [user.id],
            )
            if not err and getattr(rs, "rows", None):
                answer = "ℹ️ Причина последнего отказа:\n\n" + str(rs.rows[0][0])
            else:
                answer = (
                    "ℹ️ Материалы могут быть отклонены, если содержат:\n"
                    "• контент 18+ или неприемлемые подробности;\n"
                    "• мат и оскорбления;\n"
                    "• спам, бессодержательный или не относящийся к теме материал.\n\n"
                    "Если вы уже получали отказ, здесь будет указана его конкретная причина."
                )
            await msg.reply_text(answer, reply_markup=MENU)
        elif text in ("/start", "/menu", "Меню"):
            await start_menu(update, context)
        else:
            await msg.reply_text(
                "Выберите действие с помощью кнопок меню ниже.", reply_markup=MENU,
            )
    except Exception:
        logger.exception("_handle_user_message: исключение user_id=%s", user.id)
        try:
            await msg.reply_text(TECH_ERROR, reply_markup=MENU)
        except Exception:
            logger.exception("_handle_user_message: fallback тоже упал")


# ---------------------------------------------------------------------------
# Кнопки админа
# ---------------------------------------------------------------------------

async def admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE,
                         admin_id: int) -> bool:
    query = update.callback_query
    if not query or not query.data or not query.data.startswith("sug:"):
        return False
    if not update.effective_user or update.effective_user.id != admin_id:
        try:
            await query.answer("Эти кнопки доступны только администратору.", show_alert=True)
        except Exception:
            pass
        return True
    try:
        await query.answer()
    except Exception:
        pass

    parts = query.data.split(":")
    if len(parts) != 3:
        return True
    action, submission_id = parts[1], parts[2]

    rs, err = await _db_exec_safe(
        "SELECT user_id, kind, body, status FROM helper_submissions WHERE id = ?",
        [submission_id],
    )
    if err:
        try:
            await query.message.reply_text(TECH_ERROR)
        except Exception:
            pass
        return True

    if not getattr(rs, "rows", None):
        try:
            await query.edit_message_text((query.message.text or "") + "\n\n⚠️ Заявка не найдена.")
        except Exception:
            logger.exception("admin_callback: не удалось показать «заявка не найдена»")
        return True

    user_id, kind, body, status = rs.rows[0]
    if status != "pending":
        try:
            await query.answer("Эта заявка уже обработана.", show_alert=True)
        except Exception:
            pass
        return True

    if action == "accept" and kind in ("idea", "post"):
        _, up_err = await _db_exec_safe(
            "UPDATE helper_submissions SET status='accepted', "
            "updated_at=datetime('now') WHERE id=? AND status='pending'",
            [submission_id],
        )
        if up_err:
            try:
                await query.message.reply_text(TECH_ERROR)
            except Exception:
                pass
            return True
        try:
            await context.bot.send_message(
                chat_id=int(user_id),
                text=f"✅ Ваша заявка ({TYPE_NAMES[kind].lower()}) принята "
                     f"администрацией TAMBOV INSIDE. Спасибо за участие!",
            )
            await query.edit_message_reply_markup(reply_markup=None)
            await query.message.reply_text("Заявка принята. Пользователь получил уведомление.")
        except Exception:
            logger.exception("admin_callback: не удалось уведомить пользователя о принятии")

    elif action == "reject" and kind in ("idea", "post"):
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔞 Контент 18+", callback_data=f"sug:reason_adult:{submission_id}")],
            [InlineKeyboardButton("🤬 Ненормативная лексика", callback_data=f"sug:reason_profanity:{submission_id}")],
            [InlineKeyboardButton("🗑 Спам / бессодержательный материал", callback_data=f"sug:reason_spam:{submission_id}")],
        ])
        try:
            await query.message.reply_text("Выберите причину отказа:", reply_markup=keyboard)
        except Exception:
            logger.exception("admin_callback: не удалось показать причины отказа")

    elif action.startswith("reason_"):
        reason_key = action[len("reason_"):]
        reason = REASONS.get(reason_key)
        if not reason:
            return True
        _, up_err = await _db_exec_safe(
            """UPDATE helper_submissions SET status='rejected', rejection_reason=?,
               updated_at=datetime('now') WHERE id=? AND status='pending'""",
            [reason, submission_id],
        )
        if not up_err:
            try:
                await context.bot.send_message(
                    chat_id=int(user_id),
                    text="❌ К сожалению, администрация отклонила вашу заявку.\n\n"
                         "Причина:\n" + reason,
                )
                await query.edit_message_reply_markup(reply_markup=None)
                await query.message.reply_text("Отказ отправлен пользователю.")
            except Exception:
                logger.exception("admin_callback: не удалось отправить отказ")

    elif action == "comment" and kind == "complaint":
        context.bot_data["helper_admin_comment"] = {
            "submission_id": submission_id, "user_id": int(user_id),
        }
        try:
            await query.message.reply_text(
                "Напишите комментарий одним сообщением. Он будет отправлен автору жалобы."
            )
        except Exception:
            logger.exception("admin_callback: не удалось запросить комментарий")

    else:
        try:
            await query.answer("Действие недоступно для этого обращения.", show_alert=True)
        except Exception:
            pass
    return True


# ---------------------------------------------------------------------------
# Роутер личных обновлений
# ---------------------------------------------------------------------------

async def handle_private_start(update: Update, context: ContextTypes.DEFAULT_TYPE,
                               admin_id: int) -> None:
    user = update.effective_user
    chat = update.effective_chat
    if not user or not chat or chat.type != "private":
        return
    if user.id == admin_id:
        return

    logger.info("Opening suggestion menu for user_id=%s update_id=%s",
                user.id, update.update_id)
    try:
        await start_menu(update, context)
    except Exception:
        logger.exception("Не удалось показать меню предложки user_id=%s", user.id)
        msg = update.effective_message
        if msg:
            try:
                await msg.reply_text(
                    "⚠️ Не удалось открыть меню. Попробуйте ещё раз через минуту."
                )
            except Exception:
                pass
    raise ApplicationHandlerStop


async def handle_private_update(update: Update, context: ContextTypes.DEFAULT_TYPE,
                                admin_id: int) -> None:
    """Роутер группы -2: изолирует личку обычных пользователей от Superbot."""
    chat = update.effective_chat
    if not chat or chat.type != "private":
        return

    user = update.effective_user
    if not user:
        raise ApplicationHandlerStop

    # --- Ветка аккаунта ---
    # Меню +Аккаунт/-Аккаунт, «Сессии», создание/вход и смена данных
    # обрабатываются до предложки. Если это не аккаунтное обновление,
    # возвращается False и дальше работает обычная логика.
    try:
        if await accounts.handle_private_update(update, context):
            raise ApplicationHandlerStop
    except ApplicationHandlerStop:
        raise
    except Exception:
        logger.exception(
            "handle_private_update: accounts router упал user_id=%s", user.id
        )

    # --- Ветка администратора ---
    if user.id == admin_id:
        query = update.callback_query
        if query and query.data and query.data.startswith("sug:"):
            try:
                await admin_callback(update, context, admin_id)
            except Exception:
                logger.exception("handle_private_update: admin_callback упал")
            raise ApplicationHandlerStop

        pending = context.bot_data.get("helper_admin_comment")
        msg = update.effective_message
        if pending and msg and msg.text and not msg.text.startswith("/"):
            context.bot_data.pop("helper_admin_comment", None)
            try:
                await context.bot.send_message(
                    chat_id=pending["user_id"],
                    text="💬 Комментарий администрации по вашей жалобе:\n\n" + msg.text,
                )
                await msg.reply_text("Комментарий отправлен автору жалобы.")
            except Exception:
                logger.exception("handle_private_update: не удалось отправить комментарий админа")
                try:
                    await msg.reply_text(TECH_ERROR)
                except Exception:
                    pass
            raise ApplicationHandlerStop
        return

    # --- Ветка обычного пользователя ---
    msg = update.effective_message
    if msg and msg.text:
        command = msg.text.strip().split(maxsplit=1)[0].split("@", 1)[0].lower()
        if command == "/start":
            try:
                await start_menu(update, context)
            except Exception:
                logger.exception("Не удалось открыть предложку по /start user_id=%s", user.id)
                try:
                    await msg.reply_text(
                        "⚠️ Не удалось открыть меню. Попробуйте ещё раз через минуту."
                    )
                except Exception:
                    pass
        else:
            try:
                await _handle_user_message(update, context)
            except Exception:
                logger.exception(
                    "handle_private_update: _handle_user_message упал user_id=%s",
                    user.id,
                )
                try:
                    await msg.reply_text(TECH_ERROR, reply_markup=MENU)
                except Exception:
                    pass
    elif update.callback_query:
        try:
            await update.callback_query.answer()
        except Exception:
            pass
    elif msg:
        try:
            await msg.reply_text(
                "Чтобы отправить предложение, сначала выберите нужный раздел в меню. "
                "Медиа можно будет приложить к тексту обращения."
            )
            if not context.user_data.get("helper_state"):
                await start_menu(update, context)
        except Exception:
            logger.exception("handle_private_update: не удалось ответить на не-текстовое сообщение")

    raise ApplicationHandlerStop
