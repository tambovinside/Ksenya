"""Экономика: кредит, монеты, перевод, казна, налог, зарплата, дефолт, топ.

FIX #3: убран SECOND_COIN_FEE; эмитенты управляются через +Эмитент /
-Эмитент / Эмитенты (обработчики в coins.py, регистрируются в main.py).
"""
import os
import re
import logging
from datetime import time as dtime, datetime, timezone, timedelta
from html import escape

from telegram import Update
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import ContextTypes

import db
import premium
import utils

logger = logging.getLogger(__name__)

def _env_int(name: str, default: int = 0) -> int:
    try:
        return int(os.getenv(name, str(default)) or str(default))
    except (TypeError, ValueError, OverflowError):
        logger.warning("Некорректная переменная окружения %s; используется %s", name, default)
        return default


ADMIN_ID = _env_int("ADMIN_ID", 0)

CREDIT_DAILY_LIMIT = 1000
CREDIT_PERCENT_PER_DAY = 1
CREDIT_AUTO_COLLECT_PCT = 25

TRANSFER_FEE_PCT = 3
TRANSFER_MIN = 5
TRANSFER_FEE_MIN = 1

TAX_PCT = 3
TAX_MIN_BALANCE = 50
TAX_ACTIVE_DAYS = 1
TAX_REPORT_CHAT_ID = _env_int("CHAT_ID", 0)

DEFAULT_COOLDOWN_HOURS = 6
DEFAULT_PCT = 40

SAVINGS_INTEREST_PCT = 0.2  # в день.
SAVINGS_INTEREST_BPS = 20  # 20 basis points = 0.2% в день

FORCE_DEFAULT_CMD = "Defloult220099"

TZ = timezone.utc

_default_in_progress: set[int] = set()
_DEFAULT_COLLECTIONS: dict[int, list[tuple[int, int, int, int]]] = {}


async def _treasury_checked(chat_id: int) -> int | None:
    """Отличает пустую казну от ошибки чтения БД (treasury_get возвращает 0 в обоих случаях)."""
    try:
        rs = await db._exec("SELECT balance FROM treasury WHERE chat_id = ?", [chat_id])
    except Exception:
        logger.exception("Не удалось прочитать казну chat=%s", chat_id)
        return None
    if getattr(rs, "error", False):
        logger.error("Ошибка SQL при чтении казны chat=%s", chat_id)
        return None
    if not rs.rows:
        return 0
    try:
        return max(0, int(rs.rows[0][0] or 0))
    except (TypeError, ValueError, IndexError):
        logger.exception("Некорректный баланс казны chat=%s", chat_id)
        return None


def _today_utc() -> str:
    return datetime.now(TZ).strftime("%Y-%m-%d")


def _num_after(text: str, keyword: str) -> int | None:
    m = re.search(rf"(?i){re.escape(keyword)}\s+(\d+)", text)
    return int(m.group(1)) if m else None


async def cmd_credit_take(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    chat = update.effective_chat
    msg = update.effective_message
    if not user or not chat or not msg:
        return

    amount = _num_after(msg.text or "", "+Кредит")
    if amount is None:
        await msg.reply_text(
            "Формат: <code>+Кредит 500</code> (от 1 до 1000).",
            parse_mode=ParseMode.HTML,
        )
        return
    if amount < 1 or amount > CREDIT_DAILY_LIMIT:
        await msg.reply_text(f"⛔ Сумма кредита: от 1 до {CREDIT_DAILY_LIMIT}.")
        return

    row = await db.get_user(user.id)
    if not row:
        await db.upsert_user(user.id, user.username, user.first_name)
        row = await db.get_user(user.id)
    if not row:
        await msg.reply_text("⚠️ Не удалось получить профиль.")
        return

    balance = row.get("balance") or 0
    credit = row.get("credit") or 0
    taken_day = row.get("credit_taken_day")

    if balance < 0:
        await msg.reply_text(
            "⛔ У вас отрицательный баланс — новый кредит взять нельзя.",
            parse_mode=ParseMode.HTML,
        )
        return
    if credit > 0:
        await msg.reply_text(
            f"⛔ У вас уже есть долг <b>{credit}</b>. Сначала погасите его.",
            parse_mode=ParseMode.HTML,
        )
        return
    if taken_day == _today_utc():
        await msg.reply_text("⛔ Вы уже брали кредит сегодня.")
        return

    ok = await db.take_credit_from_treasury(chat.id, user.id, amount, _today_utc())
    if not ok:
        treasury = await db.treasury_get(chat.id)
        await msg.reply_text(
            f"🏦 Не удалось выдать кредит.\n"
            f"В казне: <b>{treasury}</b>, нужно: <b>{amount}</b>.\n"
            f"<i>Возможно, кредиты временно недоступны.</i>",
            parse_mode=ParseMode.HTML,
        )
        return

    new_row = await db.get_user(user.id)
    new_credit = (new_row or {}).get("credit") or amount
    new_treasury = await db.treasury_get(chat.id)
    await msg.reply_text(
        f"💳 Взят кредит из казны: <b>{amount}</b>\n"
        f"Долг: <b>{new_credit}</b>\n"
        f"Казна: <b>{new_treasury}</b>\n"
        f"Ставка: <b>{CREDIT_PERCENT_PER_DAY}%</b> в день.\n"
        f"Автопогашение: до <b>{CREDIT_AUTO_COLLECT_PCT}%</b> баланса раз в сутки.",
        parse_mode=ParseMode.HTML,
    )
    await _check_default_after(context, chat.id)


async def cmd_credit_repay(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    chat = update.effective_chat
    msg = update.effective_message
    if not user or not chat or not msg:
        return

    amount = _num_after(msg.text or "", "-Кредит")
    if amount is None:
        m = re.search(r"(?i)Кредит\s+(\d+)", msg.text or "")
        amount = int(m.group(1)) if m else None
    if amount is None:
        await msg.reply_text("Формат: <code>-Кредит 50</code>.", parse_mode=ParseMode.HTML)
        return
    if amount <= 0:
        await msg.reply_text("⛔ Сумма должна быть положительной.")
        return

    row = await db.get_user(user.id)
    if not row:
        await msg.reply_text("⚠️ Профиль не найден.")
        return
    balance = row.get("balance") or 0
    credit = row.get("credit") or 0

    if credit <= 0:
        await msg.reply_text("✅ У вас нет долга.")
        return
    if balance <= 0:
        await msg.reply_text(
            f"⛔ Недостаточно монет. Баланс: <b>{balance}</b>.",
            parse_mode=ParseMode.HTML,
        )
        return

    amount = min(amount, credit, balance)
    if amount <= 0:
        await msg.reply_text("⛔ Нечего погашать.")
        return

    ok = await db.repay_credit_to_treasury(chat.id, user.id, amount)
    if not ok:
        await msg.reply_text("⚠️ Не удалось погасить. Попробуйте ещё раз.")
        return

    new_row = await db.get_user(user.id)
    new_credit = (new_row or {}).get("credit") or 0
    new_balance = (new_row or {}).get("balance") or 0
    new_treasury = await db.treasury_get(chat.id)
    await msg.reply_text(
        f"✅ Погашено <b>{amount}</b> в казну.\n"
        f"Остаток долга: <b>{new_credit}</b>.\n"
        f"Баланс: <b>{new_balance}</b>.\n"
        f"Казна: <b>{new_treasury}</b>.",
        parse_mode=ParseMode.HTML,
    )


async def cmd_balance(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Показывает обычный баланс пользователя и баланс сберкнижки."""
    user = update.effective_user
    msg = update.effective_message
    if not user or not msg:
        return

    row = await db.get_user(user.id)
    if not row:
        await db.upsert_user(user.id, user.username, user.first_name)
        row = await db.get_user(user.id)

    if not row:
        await msg.reply_text("⚠️ Не удалось получить баланс.")
        return

    balance = int(row.get("balance") or 0)
    savings = await db.savings_get(user.id)
    savings_balance = int(savings.get("balance") or 0)
    total = balance + savings_balance

    await msg.reply_text(
        "💰 Баланс пользователя\n"
        f"Кошелёк: {balance:,} монет\n"
        f"📘 Сберкнижка: {savings_balance:,} монет\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"💎 Всего: {total:,} монет",
    )


async def cmd_savings_book(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    msg = update.effective_message
    if not user or not msg:
        return
    row = await db.get_user(user.id)
    if not row:
        await db.upsert_user(user.id, user.username, user.first_name)
        row = await db.get_user(user.id)
    if not row:
        await msg.reply_text("⚠️ Не удалось открыть сберкнижку.")
        return
    savings = await db.savings_get(user.id)
    frac = savings["interest_pending_hundredths"]
    interest = frac / 100
    await msg.reply_text(
        "📘 <b>Государственная сберкнижка</b>\n"
        f"Накоплено: <b>{savings['balance']}</b> монет\n"
        f"Ожидает выплаты процентов: <b>{interest:.2f}</b> монет\n"
        f"Ставка: <b>{SAVINGS_INTEREST_PCT:.1f}% в день</b>\n"
        "Проценты выплачиваются из казны; при нехватке средств остаются в ожидании.",
        parse_mode=ParseMode.HTML,
    )


async def cmd_savings_deposit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user, msg = update.effective_user, update.effective_message
    if not user or not msg:
        return
    amount = _num_after(msg.text or "", "+Сберкнижка")
    if amount is None or amount <= 0 or amount > 1_000_000_000:
        await msg.reply_text("Формат: <code>+Сберкнижка 500</code> (до 1 000 000 000)", parse_mode=ParseMode.HTML)
        return
    if not await db.savings_deposit(user.id, amount):
        row = await db.get_user(user.id)
        balance = int((row or {}).get("balance") or 0)
        await msg.reply_text(f"⛔ Не удалось положить деньги. Обычный баланс: {balance}.")
        return
    savings = await db.savings_get(user.id)
    row = await db.get_user(user.id)
    await msg.reply_text(
        f"📘 В сберкнижку положено <b>{amount}</b> монет.\n"
        f"На сберкнижке: <b>{savings['balance']}</b>\n"
        f"Обычный баланс: <b>{int((row or {}).get('balance') or 0)}</b>",
        parse_mode=ParseMode.HTML,
    )


async def cmd_savings_withdraw(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user, msg = update.effective_user, update.effective_message
    if not user or not msg:
        return
    amount = _num_after(msg.text or "", "-Сберкнижка")
    if amount is None or amount <= 0 or amount > 1_000_000_000:
        await msg.reply_text("Формат: <code>-Сберкнижка 200</code> (до 1 000 000 000)", parse_mode=ParseMode.HTML)
        return
    if not await db.savings_withdraw(user.id, amount):
        savings = await db.savings_get(user.id)
        await msg.reply_text(f"⛔ Недостаточно средств. На сберкнижке: {savings['balance']}.")
        return
    savings = await db.savings_get(user.id)
    row = await db.get_user(user.id)
    await msg.reply_text(
        f"💵 Со сберкнижки снято <b>{amount}</b> монет.\n"
        f"Остаток: <b>{savings['balance']}</b>\n"
        f"Обычный баланс: <b>{int((row or {}).get('balance') or 0)}</b>",
        parse_mode=ParseMode.HTML,
    )


async def cmd_admin_coins(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    msg = update.effective_message
    if not user or not msg:
        return
    if not ADMIN_ID or user.id != ADMIN_ID:
        await msg.reply_text("⛔ Только админ.")
        return

    text = msg.text or ""
    m = re.search(r"(?i)\+Монеты\s+(\d+)", text)
    if not m:
        await msg.reply_text(
            "Формат: <code>+Монеты 100 @user</code> или реплай.",
            parse_mode=ParseMode.HTML,
        )
        return
    amount = int(m.group(1))
    if amount <= 0 or amount > 1_000_000:
        await msg.reply_text("⛔ Сумма должна быть от 1 до 1 000 000.")
        return

    clean = re.sub(r"(?i)\+Монеты\s+\d+", "", text)
    target = await utils.resolve_target(update, context, clean)
    if not target:
        await msg.reply_text("Укажите @username или сделайте реплай.")
        return
    uid, name = target

    ok = await db.add_balance(uid, amount)
    if not ok:
        await msg.reply_text("⚠️ Не удалось изменить баланс.")
        return

    new_row = await db.get_user(uid)
    new_balance = (new_row or {}).get("balance") or 0
    sign = "+"
    await msg.reply_text(
        f"💰 {escape(str(name))}: <b>{sign}{amount}</b>\n"
        f"Баланс: <b>{new_balance}</b>",
        parse_mode=ParseMode.HTML,
    )


async def cmd_admin_coins_minus(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    chat = update.effective_chat
    msg = update.effective_message
    if not user or not chat or not msg:
        return
    if not ADMIN_ID or user.id != ADMIN_ID:
        await msg.reply_text("⛔ Только админ.")
        return

    text = msg.text or ""
    m = re.search(r"(?i)[-–−]Монеты\s+(\d+)", text)
    if not m:
        await msg.reply_text(
            "Формат: <code>-Монеты 100 @user</code> или реплай.",
            parse_mode=ParseMode.HTML,
        )
        return
    amount = int(m.group(1))
    if amount <= 0:
        await msg.reply_text("⛔ Сумма должна быть положительной.")
        return

    clean = re.sub(r"(?i)[-–−]Монеты\s+\d+", "", text)
    target = await utils.resolve_target(update, context, clean)
    if not target:
        await msg.reply_text("Укажите @username или сделайте реплай.")
        return
    uid, name = target

    taken = await db.take_balance_any(uid, amount)
    if taken == 0:
        await msg.reply_text(
            f"⚠️ У {escape(str(name))} нечего списывать.",
            parse_mode=ParseMode.HTML,
        )
        return

    try:
        treasury_ok = await db.treasury_add(chat.id, taken)
    except Exception:
        logger.exception("cmd_admin_coins_minus treasury credit failed")
        treasury_ok = False
    if not treasury_ok:
        try:
            restored = await db.add_balance(uid, taken)
        except Exception:
            logger.exception("cmd_admin_coins_minus compensation failed")
            restored = False
        if not restored:
            logger.critical("Не удалось вернуть списанные монеты user=%s amount=%s", uid, taken)
            await msg.reply_text("⚠️ Ошибка казны и возврата. Нужна ручная сверка администратором.")
        else:
            await msg.reply_text("⚠️ Казна недоступна. Списание отменено, монеты возвращены.")
        return

    new_row = await db.get_user(uid)
    new_balance = (new_row or {}).get("balance") or 0
    note = ""
    if taken < amount:
        note = f"\n<i>Списано меньше: было только {taken}.</i>"
    await msg.reply_text(
        f"💸 {escape(str(name))}: <b>-{taken}</b>\n"
        f"Баланс: <b>{new_balance}</b>{note}",
        parse_mode=ParseMode.HTML,
    )


async def cmd_transfer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    chat = update.effective_chat
    msg = update.effective_message
    if not user or not chat or not msg:
        return

    text = msg.text or ""
    m = re.search(r"(?i)^Перевод\s+(\d+)", text)
    if not m:
        await msg.reply_text(
            "Формат: <code>Перевод 100 @user</code> или реплай.\n"
            f"Комиссия: {TRANSFER_FEE_PCT}% (мин. {TRANSFER_FEE_MIN}), "
            f"минимум перевода — {TRANSFER_MIN}.",
            parse_mode=ParseMode.HTML,
        )
        return
    amount = int(m.group(1))
    if amount < TRANSFER_MIN:
        await msg.reply_text(f"⛔ Минимум перевода — {TRANSFER_MIN} монет.")
        return
    if amount > 1_000_000_000:
        await msg.reply_text("⛔ Максимум одного перевода — 1 000 000 000 монет.")
        return

    clean = re.sub(r"(?i)^Перевод\s+\d+", "", text)
    target = await utils.resolve_target(update, context, clean)
    if not target:
        await msg.reply_text("Укажите @username или сделайте реплай.")
        return
    uid, name = target
    if uid == user.id:
        await msg.reply_text("Себе переводить нельзя 🙂")
        return

    row = await db.get_user(uid)
    if not row:
        await db.upsert_user(uid, None, name)

    fee = max(TRANSFER_FEE_MIN, int(amount * TRANSFER_FEE_PCT / 100))
    if fee >= amount:
        await msg.reply_text("⛔ Сумма слишком мала для комиссии.")
        return

    ok = await db.transfer_atomic(user.id, uid, amount, fee)
    if not ok:
        me_row = await db.get_user(user.id)
        bal = (me_row or {}).get("balance") or 0
        await msg.reply_text(
            f"⛔ Недостаточно монет. Баланс: <b>{bal}</b>, нужно: <b>{amount}</b>.",
            parse_mode=ParseMode.HTML,
        )
        return

    try:
        fee_added = await db.treasury_add(chat.id, fee)
    except Exception:
        logger.exception("cmd_transfer treasury fee credit failed")
        fee_added = False
    if not fee_added:
        try:
            fee_refunded = await db.add_balance(user.id, fee)
        except Exception:
            logger.exception("cmd_transfer fee refund failed")
            fee_refunded = False
        if not fee_refunded:
            logger.critical("Перевод прошёл, но комиссия %s не зачислена и не возвращена; from=%s to=%s", fee, user.id, uid)
            await msg.reply_text("⚠️ Перевод выполнен, но возникла ошибка комиссии. Обратись к администратору для сверки.")
            return

    me_row = await db.get_user(user.id)
    new_bal = (me_row or {}).get("balance") or 0
    net = amount - fee
    await msg.reply_text(
        f"💸 <b>Перевод</b>\n"
        f"Кому: {escape(str(name))}\n"
        f"Сумма: <b>{amount}</b> · комиссия <b>{fee}</b>\n"
        f"Получатель получил: <b>{net}</b>\n"
        f"Ваш баланс: <b>{new_bal}</b>",
        parse_mode=ParseMode.HTML,
    )


async def cmd_treasury(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    chat = update.effective_chat
    msg = update.effective_message
    if not user or not chat or not msg:
        return
    if not ADMIN_ID or user.id != ADMIN_ID:
        await msg.reply_text("⛔ Только админ.")
        return
    bal = await _treasury_checked(chat.id)
    if bal is None:
        await msg.reply_text("⚠️ Не удалось прочитать казну.")
        return
    await msg.reply_text(
        f"🏦 <b>Казна чата</b>\n"
        f"Накоплено: <b>{bal}</b> монет\n"
        f"<i>chat_id: {chat.id}</i>",
        parse_mode=ParseMode.HTML,
    )


async def cmd_treasury_add(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    chat = update.effective_chat
    msg = update.effective_message
    if not user or not chat or not msg:
        return
    if not ADMIN_ID or user.id != ADMIN_ID:
        await msg.reply_text("⛔ Только админ.")
        return
    m = re.search(r"(?i)\+ВКассу\s+(\d+)", msg.text or "")
    if not m:
        await msg.reply_text("Формат: <code>+ВКассу 500</code>", parse_mode=ParseMode.HTML)
        return
    amount = int(m.group(1))
    if amount <= 0 or amount > 10_000_000:
        await msg.reply_text("⛔ Сумма должна быть от 1 до 10 000 000.")
        return
    try:
        added = await db.treasury_add(chat.id, amount)
    except Exception:
        logger.exception("cmd_treasury_add failed")
        added = False
    if not added:
        await msg.reply_text("⚠️ Не удалось пополнить казну.")
        return
    new_treasury = await db.treasury_get(chat.id)
    await msg.reply_text(
        f"🏦 В казну добавлено: <b>{amount}</b>\nКазна: <b>{new_treasury}</b>",
        parse_mode=ParseMode.HTML,
    )


async def cmd_treasury_trim(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    chat = update.effective_chat
    msg = update.effective_message
    if not user or not chat or not msg:
        return
    if not ADMIN_ID or user.id != ADMIN_ID:
        await msg.reply_text("⛔ Только админ.")
        return
    m = re.search(r"(?i)ОбрезатьКасну\s+(\d+)", msg.text or "")
    if not m:
        await msg.reply_text(
            "Формат: <code>ОбрезатьКасну 15000</code>",
            parse_mode=ParseMode.HTML,
        )
        return
    keep = int(m.group(1))
    if keep < 0:
        await msg.reply_text("⛔ Не может быть отрицательным.")
        return
    burned = await db.treasury_trim(chat.id, keep)
    new_bal = await db.treasury_get(chat.id)
    await msg.reply_text(
        f"🔥 Сожжено: <b>{burned}</b>\nОсталось: <b>{new_bal}</b>",
        parse_mode=ParseMode.HTML,
    )


async def cmd_burn_treasury(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    chat = update.effective_chat
    msg = update.effective_message
    if not user or not chat or not msg:
        return
    if not ADMIN_ID or user.id != ADMIN_ID:
        await msg.reply_text("⛔ Только админ.")
        return
    m = re.search(r"(?i)[-–−]Утил\s+(\d+)\s+казна", msg.text or "")
    if not m:
        await msg.reply_text(
            "Формат: <code>-Утил 100 казна</code>",
            parse_mode=ParseMode.HTML,
        )
        return
    amount = int(m.group(1))
    burned = await db.burn_treasury(chat.id, amount)
    if burned == 0:
        await msg.reply_text("⚠️ В казне нечего сжигать.")
        return
    new_bal = await db.treasury_get(chat.id)
    await msg.reply_text(
        f"🔥 Казна: сожжено <b>{burned}</b>\nОсталось: <b>{new_bal}</b>",
        parse_mode=ParseMode.HTML,
    )
    await _check_default_after(context, chat.id)


async def cmd_burn_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    msg = update.effective_message
    if not user or not msg:
        return
    if not ADMIN_ID or user.id != ADMIN_ID:
        await msg.reply_text("⛔ Только админ.")
        return
    text = msg.text or ""
    m = re.search(r"(?i)[-–−]Утил\s+(\d+)", text)
    if not m:
        await msg.reply_text(
            "Формат: <code>-Утил 100 @user</code>",
            parse_mode=ParseMode.HTML,
        )
        return
    amount = int(m.group(1))
    if amount <= 0:
        await msg.reply_text("⛔ Сумма должна быть положительной.")
        return
    clean = re.sub(r"(?i)[-–−]Утил\s+\d+", "", text).strip()
    target = await utils.resolve_target(update, context, clean)
    if not target:
        await msg.reply_text("Укажите @username или сделайте реплай.")
        return
    uid, name = target
    burned = await db.burn_balance(uid, amount)
    if burned == 0:
        await msg.reply_text(
            f"⚠️ У {escape(str(name))} нечего сжигать.",
            parse_mode=ParseMode.HTML,
        )
        return
    new_row = await db.get_user(uid)
    new_balance = (new_row or {}).get("balance") or 0
    await msg.reply_text(
        f"🔥 {escape(str(name))}: <b>-{burned}</b>\n"
        f"Баланс: <b>{new_balance}</b>",
        parse_mode=ParseMode.HTML,
    )


async def cmd_mint_treasury(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    chat = update.effective_chat
    msg = update.effective_message
    if not user or not chat or not msg:
        return
    if not ADMIN_ID or user.id != ADMIN_ID:
        await msg.reply_text("⛔ Только админ.")
        return
    m = re.search(r"(?i)\+Утил\s+(\d+)\s+казна", msg.text or "")
    if not m:
        await msg.reply_text("Формат: <code>+Утил 100 казна</code>", parse_mode=ParseMode.HTML)
        return
    amount = int(m.group(1))
    if amount <= 0 or amount > 10_000_000:
        await msg.reply_text("⛔ Сумма должна быть от 1 до 10 000 000.")
        return
    try:
        added = await db.treasury_add(chat.id, amount)
    except Exception:
        logger.exception("cmd_mint_treasury failed")
        added = False
    if not added:
        await msg.reply_text("⚠️ Не удалось пополнить казну.")
        return
    new_bal = await db.treasury_get(chat.id)
    await msg.reply_text(
        f"✨ Казна: +{amount}\nКазна: <b>{new_bal}</b>",
        parse_mode=ParseMode.HTML,
    )


async def cmd_mint_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    msg = update.effective_message
    if not user or not msg:
        return
    if not ADMIN_ID or user.id != ADMIN_ID:
        await msg.reply_text("⛔ Только админ.")
        return
    text = msg.text or ""
    m = re.search(r"(?i)\+Утил\s+(\d+)", text)
    if not m:
        await msg.reply_text("Формат: <code>+Утил 100 @user</code>", parse_mode=ParseMode.HTML)
        return
    amount = int(m.group(1))
    if amount <= 0 or amount > 1_000_000:
        await msg.reply_text("⛔ Сумма должна быть от 1 до 1 000 000.")
        return
    clean = re.sub(r"(?i)\+Утил\s+\d+", "", text)
    target = await utils.resolve_target(update, context, clean)
    if not target:
        await msg.reply_text("Укажите @username или сделайте реплай.")
        return
    uid, name = target
    ok = await db.add_balance(uid, amount)
    if not ok:
        await msg.reply_text("⚠️ Не удалось начислить.")
        return
    new_row = await db.get_user(uid)
    new_balance = (new_row or {}).get("balance") or 0
    await msg.reply_text(
        f"✨ {escape(str(name))}: <b>+{amount}</b>\n"
        f"Баланс: <b>{new_balance}</b>",
        parse_mode=ParseMode.HTML,
    )


async def cmd_salary(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    msg = update.effective_message
    if not user or not msg:
        return
    day = _today_utc()
    paid_coins = int(await db.get_salary_today(user.id, day) or 0)
    frac = int(await db.get_salary_frac(user.id, day) or 0)
    # pending_salary содержит заработанные, но ещё не выплаченные из казны дроби.
    pending = 0
    if TAX_REPORT_CHAT_ID:
        pending = int(await db.get_pending_salary(TAX_REPORT_CHAT_ID, user.id, day) or 0)
    earned_hundredths = paid_coins * 100 + frac + pending
    earned = f"{earned_hundredths // 100}.{earned_hundredths % 100:02d}"
    pending_text = f"\nОжидает выплаты из казны: <b>{pending // 100}.{pending % 100:02d}</b> монет" if pending else ""
    await msg.reply_text(
        f"💼 <b>Зарплата за сегодня</b>\n"
        f"Всего заработано: <b>{earned}</b> монет{pending_text}\n\n"
        f"<i>1 сообщение ≥ 2 символов = 0.20 монеты. "
        f"Дроби копятся; целые монеты выплачиваются из казны.</i>",
        parse_mode=ParseMode.HTML,
    )


async def cmd_top_rich(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Совместимый обработчик топа богачей: только 10 участников чата."""
    chat = update.effective_chat
    msg = update.effective_message
    if not chat or not msg:
        return
    try:
        rows = await db.top_balances(limit=10, chat_id=chat.id)
        names = await db.get_user_display_names([int(uid) for uid, _ in rows])
    except Exception:
        logger.exception("Не удалось получить топ богачей")
        await msg.reply_text("Не удалось загрузить топ богачей. Попробуйте позже.")
        return
    if not rows:
        await msg.reply_text("Топ богачей пуст.")
        return
    lines = ["💰 <b>Топ богачей</b>"]
    for i, (uid, wealth) in enumerate(rows, start=1):
        name = names.get(int(uid), "Пользователь")
        medal = "🥇" if i == 1 else "🥈" if i == 2 else "🥉" if i == 3 else f"{i}."
        lines.append(f"{medal} {escape(name)} — <b>{int(wealth or 0):,}</b> 🪙")
    await msg.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def _collect_balances(chat_id: int, pct: int) -> tuple[int, int]:
    """Конфискует долю обычного баланса, сберкнижки и целых начисленных процентов."""
    pct = max(0, min(100, int(pct)))
    if pct <= 0:
        return 0, 0
    rs = await db._exec(
        """SELECT user_id, COALESCE(balance, 0), COALESCE(savings_balance, 0),
                  COALESCE(savings_interest_frac, 0), COALESCE(savings_interest_remainder, 0)
           FROM users
           WHERE COALESCE(balance, 0) > 0 OR COALESCE(savings_balance, 0) > 0
              OR COALESCE(savings_interest_frac, 0) >= 100"""
    )
    if getattr(rs, "error", False):
        logger.error("_collect_balances: SELECT failed")
        return 0, 0
    total = 0
    users_count = 0
    collected_rows: list[tuple[int, int, int, int]] = []
    for uid, balance, savings, interest_frac, interest_remainder in rs.rows:
        try:
            if await premium.has_protection(int(uid)):
                continue
        except Exception:
            # Сбой проверки защиты не должен прерывать дефолт после уже
            # обработанных пользователей: такого пользователя безопаснее пропустить.
            logger.exception("_collect_balances: protection check failed user=%s", uid)
            continue
        balance = int(balance or 0)
        savings = int(savings or 0)
        interest_frac = int(interest_frac or 0)
        interest_remainder = int(interest_remainder or 0)
        pending_whole = interest_frac // 100
        take_balance = balance if pct == 100 else balance * pct // 100
        take_savings = savings if pct == 100 else savings * pct // 100
        take_interest = pending_whole if pct == 100 else pending_whole * pct // 100
        if take_balance + take_savings + take_interest <= 0:
            continue
        new_interest_frac = interest_frac - take_interest * 100
        # Дробный остаток процента меньше одной монеты не конфискуется.
        new_remainder = interest_remainder
        upd = await db._exec(
            """UPDATE users
               SET balance = balance - ?,
                   savings_balance = savings_balance - ?,
                   savings_interest_frac = ?,
                   savings_interest_remainder = ?
               WHERE user_id = ? AND COALESCE(balance, 0) = ?
                 AND COALESCE(savings_balance, 0) = ?
                 AND COALESCE(savings_interest_frac, 0) = ?
                 AND COALESCE(savings_interest_remainder, 0) = ?""",
            [take_balance, take_savings, new_interest_frac, new_remainder,
             uid, balance, savings, interest_frac, interest_remainder],
        )
        if getattr(upd, "error", False) or (getattr(upd, "rows_affected", 0) or 0) <= 0:
            continue
        row_total = take_balance + take_savings + take_interest
        total += row_total
        users_count += 1
        collected_rows.append((int(uid), take_balance, take_savings, take_interest * 100))
        # Храним журнал сразу: при исключении в следующей итерации его можно откатить.
        _DEFAULT_COLLECTIONS[chat_id] = list(collected_rows)
    _DEFAULT_COLLECTIONS[chat_id] = collected_rows
    logger.info("_collect_balances: confiscated=%s users=%s pct=%s", total, users_count, pct)
    return total, users_count


async def _restore_default_collection(chat_id: int) -> bool:
    """Возвращает активы, если перевод собранной суммы в казну не состоялся."""
    rows = _DEFAULT_COLLECTIONS.pop(chat_id, [])
    success = True
    for uid, wallet, savings, interest_hundredths in rows:
        try:
            rs = await db._exec(
                """UPDATE users
                   SET balance = COALESCE(balance, 0) + ?,
                       savings_balance = COALESCE(savings_balance, 0) + ?,
                       savings_interest_frac = COALESCE(savings_interest_frac, 0) + ?
                   WHERE user_id = ?""",
                [wallet, savings, interest_hundredths, uid],
            )
            if getattr(rs, "error", False) or (getattr(rs, "rows_affected", 0) or 0) <= 0:
                success = False
                logger.critical("default compensation failed user=%s", uid)
        except Exception:
            success = False
            logger.exception("default compensation exception user=%s", uid)
    return success


async def _run_default(context, chat_id: int, force: bool = False) -> bool:
    if chat_id in _default_in_progress:
        logger.info("default: уже выполняется для chat=%s, skip", chat_id)
        return False
    _default_in_progress.add(chat_id)
    try:
        logger.info("default: start chat=%s force=%s", chat_id, force)

        if not force:
            last = await db.get_last_default(chat_id)
            if last:
                try:
                    last_dt = datetime.strptime(last, "%Y-%m-%d %H:%M:%S").replace(tzinfo=TZ)
                    if (datetime.now(TZ) - last_dt) < timedelta(hours=DEFAULT_COOLDOWN_HOURS):
                        logger.info("default: cooldown, skip")
                        return False
                except Exception as e:
                    logger.warning("default: parse last: %s", e)

        pct = 100 if force else DEFAULT_PCT
        try:
            total, users_count = await _collect_balances(chat_id, pct)
        except Exception:
            logger.exception("default: collection failed; attempting compensation")
            restored = await _restore_default_collection(chat_id)
            if not restored:
                logger.critical("default: collection rollback incomplete chat=%s", chat_id)
            return False
        if total <= 0:
            logger.info("default: собирать нечего")
            return False

        try:
            treasury_ok = await db.treasury_add(chat_id, total)
        except Exception:
            logger.exception("default: treasury credit failed")
            treasury_ok = False
        if not treasury_ok:
            restored = await _restore_default_collection(chat_id)
            if not restored:
                logger.critical("default: не все активы удалось вернуть chat=%s", chat_id)
            return False
        try:
            recorded = await db._exec(
                """INSERT INTO treasury_meta (chat_id, last_default_at)
                   VALUES (?, ?)
                   ON CONFLICT(chat_id) DO UPDATE SET
                     last_default_at = excluded.last_default_at""",
                [chat_id, datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S")],
            )
            record_ok = not getattr(recorded, "error", False) and (getattr(recorded, "rows_affected", 0) or 0) > 0
        except Exception:
            logger.exception("default: не удалось записать cooldown")
            record_ok = False
        if not record_ok:
            try:
                treasury_rollback = await db.treasury_withdraw(chat_id, total)
            except Exception:
                logger.exception("default: treasury rollback after metadata failure")
                treasury_rollback = False
            restored = await _restore_default_collection(chat_id)
            if not treasury_rollback or not restored:
                logger.critical("default: metadata failure rollback incomplete chat=%s total=%s", chat_id, total)
            return False
        _DEFAULT_COLLECTIONS.pop(chat_id, None)

        if force:
            text = (
                "⚠️ <b>ВНИМАНИЕ — ДЕФОЛТ</b> ⚠️\n\n"
                "🏦 <b>ЭКСТРЕМАЛЬНЫЙ ДЕФОЛТ</b>\n"
                "Правительство объявило дефолт.\n"
                "Конфискованы доступные положительные средства: кошелёк, сберкнижка и целые ожидающие проценты.\n"
                "Отрицательные балансы и долги по кредитам не обнуляются.\n"
                f"Затронуто юзеров: <b>{users_count}</b>\n"
                f"Собрано в казну: <b>{total}</b> монет."
            )
        else:
            text = (
                "⚠️ <b>ВНИМАНИЕ — ДЕФОЛТ</b> ⚠️\n\n"
                "🏦 <b>ДЕФОЛТ</b>\n"
                "Казна пуста. Объявлен дефолт.\n"
                f"Списано <b>{DEFAULT_PCT}%</b> с балансов "
                f"<b>{users_count}</b> юзеров.\n"
                f"Собрано в казну: <b>{total}</b> монет."
            )

        try:
            await context.bot.send_message(
                chat_id=chat_id, text=text, parse_mode=ParseMode.HTML,
            )
        except TelegramError as e:
            logger.warning("default send: %s", e)

        return True
    finally:
        _default_in_progress.discard(chat_id)


async def _check_default_after(context, chat_id: int) -> None:
    bal = await _treasury_checked(chat_id)
    if bal is None or bal > 0:
        return
    await _run_default(context, chat_id, force=False)


async def default_watchdog_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = _env_int("CHAT_ID", 0)
    if not chat_id:
        return
    bal = await _treasury_checked(chat_id)
    if bal is None:
        return
    if bal <= 0:
        await _run_default(context, chat_id, force=False)


async def cmd_force_default(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    chat = update.effective_chat
    msg = update.effective_message
    if not user or not chat or not msg:
        return

    if not ADMIN_ID or user.id != ADMIN_ID:
        await msg.reply_text("⛔ Только админ.")
        return

    try:
        await msg.delete()
    except TelegramError as e:
        logger.info("cmd_force_default: не удалось удалить команду: %s", e)

    ok = await _run_default(context, chat.id, force=True)
    if not ok:
        try:
            await context.bot.send_message(
                chat_id=chat.id,
                text="⚠️ Экстремальный дефолт не удался: собирать нечего "
                     "(у юзеров нулевые балансы) или ошибка БД.",
                parse_mode=ParseMode.HTML,
            )
        except TelegramError:
            pass


async def cmd_all_money(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    chat = update.effective_chat
    msg = update.effective_message
    if not user or not chat or not msg:
        return

    if not ADMIN_ID or user.id != ADMIN_ID:
        await msg.reply_text("⛔ Только админ.")
        return

    text = msg.text or ""
    m = re.search(r"(?i)^/?ALLMONEY\s+(\d+)\s*$", text.strip())
    if not m:
        await msg.reply_text(
            "Формат: <code>/ALLMONEY 100</code>",
            parse_mode=ParseMode.HTML,
        )
        return
    amount = int(m.group(1))
    if amount <= 0:
        await msg.reply_text("⛔ Сумма должна быть положительной.")
        return
    if amount > 1_000_000:
        await msg.reply_text("⛔ Слишком большая сумма (макс. 1 000 000).")
        return

    try:
        await msg.delete()
    except TelegramError as e:
        logger.info("cmd_all_money: не удалось удалить команду: %s", e)

    try:
        rs = await db._exec("SELECT COUNT(*) FROM users")
        total_users = (rs.rows[0][0] if rs.rows else 0) or 0
    except Exception as e:
        logger.warning("cmd_all_money count: %s", e)
        total_users = 0

    if total_users == 0:
        await context.bot.send_message(
            chat_id=chat.id,
            text="⚠️ В БД нет ни одного юзера — раздавать некому.",
            parse_mode=ParseMode.HTML,
        )
        return

    total_minted = amount * total_users
    if total_minted > 9_000_000_000_000_000:
        await context.bot.send_message(chat_id=chat.id, text="⚠️ Общая сумма раздачи слишком велика.")
        return
    treasury_before = await _treasury_checked(chat.id)
    if treasury_before is None:
        await context.bot.send_message(chat_id=chat.id, text="⚠️ Не удалось проверить казну. Раздача отменена.")
        return
    if treasury_before < total_minted:
        await context.bot.send_message(
            chat_id=chat.id,
            text=(
                f"⚠️ В казне недостаточно монет.\n"
                f"Нужно: <b>{total_minted}</b>, в казне: <b>{treasury_before}</b>.\n"
                f"<i>Пополни казну через +ВКассу или +Утил казна.</i>"
            ),
            parse_mode=ParseMode.HTML,
        )
        return

    ok = await db.treasury_withdraw(chat.id, total_minted)
    if not ok:
        await context.bot.send_message(
            chat_id=chat.id,
            text="⚠️ Не удалось списать из казны. Ошибка БД.",
            parse_mode=ParseMode.HTML,
        )
        return

    try:
        rs2 = await db._exec(
            "UPDATE users SET balance = COALESCE(balance, 10) + ?",
            [amount],
        )
    except Exception as e:
        logger.exception("cmd_all_money update: %s", e)
        await db.treasury_add(chat.id, total_minted)
        await context.bot.send_message(
            chat_id=chat.id,
            text="⚠️ Не удалось начислить монеты. Ошибка БД. Деньги возвращены в казну.",
            parse_mode=ParseMode.HTML,
        )
        return

    if getattr(rs2, "error", False) or (getattr(rs2, "rows_affected", 0) or 0) != total_users:
        # UPDATE является одним SQL-оператором; при несовпадении количества
        # строк компенсируем начисление и возвращаем резерв казны.
        try:
            undo = await db._exec(
                "UPDATE users SET balance = COALESCE(balance, 10) - ?",
                [amount],
            )
            if getattr(undo, "error", False):
                logger.critical("ALLMONEY: не удалось отменить частичное начисление")
        except Exception:
            logger.exception("ALLMONEY: ошибка отката начисления")
        await db.treasury_add(chat.id, total_minted)
        await context.bot.send_message(
            chat_id=chat.id,
            text="⚠️ Не удалось начислить монеты. Ошибка БД. Деньги возвращены в казну.",
            parse_mode=ParseMode.HTML,
        )
        return

    treasury_after = await db.treasury_get(chat.id)

    try:
        await context.bot.send_message(
            chat_id=chat.id,
            text=(
                "⚠️ <b>ВНИМАНИЕ — РАЗДАЧА</b> ⚠️\n\n"
                "💰 <b>ВСЕМ НАЧИСЛЕНО</b>\n"
                f"По <b>{amount}</b> монет каждому юзеру в БД.\n"
                f"Затронуто: <b>{total_users}</b> юзеров.\n"
                f"Списано из казны: <b>{total_minted}</b> монет.\n"
                f"Казна: <b>{treasury_after}</b>"
            ),
            parse_mode=ParseMode.HTML,
        )
    except TelegramError as e:
        logger.warning("cmd_all_money send: %s", e)


async def _claim_tax_day(chat_id: int, day: str) -> bool:
    """Не допускает повторного сбора налога в один день при дубле job/рестарте."""
    try:
        create = await db._exec(
            """CREATE TABLE IF NOT EXISTS economy_job_runs (
                   chat_id INTEGER NOT NULL,
                   job_name TEXT NOT NULL,
                   run_day TEXT NOT NULL,
                   PRIMARY KEY(chat_id, job_name, run_day)
               )"""
        )
        if getattr(create, "error", False):
            return False
        claim = await db._exec(
            "INSERT OR IGNORE INTO economy_job_runs(chat_id, job_name, run_day) VALUES (?, 'tax', ?)",
            [chat_id, day],
        )
        return not getattr(claim, "error", False) and (getattr(claim, "rows_affected", 0) or 0) > 0
    except Exception:
        logger.exception("tax_job: не удалось зарезервировать дневной сбор")
        return False


async def _release_tax_day(chat_id: int, day: str) -> None:
    try:
        await db._exec(
            "DELETE FROM economy_job_runs WHERE chat_id = ? AND job_name = 'tax' AND run_day = ?",
            [chat_id, day],
        )
    except Exception:
        logger.exception("tax_job: не удалось снять резерв дневного сбора")


async def tax_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Ежедневно собирает налог с активных пользователей указанного чата."""
    chat_id = TAX_REPORT_CHAT_ID
    if not chat_id:
        logger.error("tax_job: CHAT_ID не задан — налог не собран")
        return
    day = _today_utc()
    if not await _claim_tax_day(chat_id, day):
        logger.info("tax_job: налог за %s уже собирается/собран либо БД недоступна", day)
        return

    # Сбор ведётся с CAS по каждому балансу; если казна не приняла налог,
    # списания компенсируются обратно.
    collected: list[tuple[int, int]] = []
    total = 0
    try:
        rs = await db._exec(
            """SELECT user_id, balance FROM users
               WHERE balance > ? AND EXISTS (
                   SELECT 1 FROM messages_day m
                   WHERE m.user_id = users.user_id AND m.chat_id = ?
                     AND m.day >= date('now', ?)
               )""",
            [TAX_MIN_BALANCE, chat_id, f"-{int(TAX_ACTIVE_DAYS)} days"],
        )
        if getattr(rs, "error", False):
            raise RuntimeError("ошибка чтения активных балансов")
        for uid, raw_balance in rs.rows:
            balance = int(raw_balance or 0)
            if balance <= TAX_MIN_BALANCE:
                continue
            tax = balance * TAX_PCT // 100
            if tax <= 0:
                continue
            upd = await db._exec(
                """UPDATE users SET balance = balance - ?
                   WHERE user_id = ? AND balance = ? AND balance > ?
                     AND EXISTS (
                       SELECT 1 FROM messages_day m
                       WHERE m.user_id = users.user_id AND m.chat_id = ?
                         AND m.day >= date('now', ?)
                     )""",
                [tax, uid, balance, TAX_MIN_BALANCE, chat_id,
                 f"-{int(TAX_ACTIVE_DAYS)} days"],
            )
            if getattr(upd, "error", False):
                logger.warning("tax_job: не удалось удержать налог user=%s", uid)
                continue
            if (getattr(upd, "rows_affected", 0) or 0) > 0:
                collected.append((int(uid), tax))
                total += tax
    except Exception:
        logger.exception("tax_job: ошибка сбора налога")
        await _release_tax_day(chat_id, day)
        return

    if total > 0:
        try:
            treasury_ok = await db.treasury_add(chat_id, total)
        except Exception:
            logger.exception("tax_job: ошибка зачисления налога")
            treasury_ok = False
        if not treasury_ok:
            rollback_ok = True
            for uid, tax in collected:
                try:
                    restored = await db.add_balance(uid, tax)
                except Exception:
                    logger.exception("tax_job: возврат налога user=%s failed", uid)
                    restored = False
                rollback_ok = bool(restored) and rollback_ok
            if not rollback_ok:
                logger.critical("tax_job: не все списания удалось вернуть; chat=%s total=%s", chat_id, total)
            await _release_tax_day(chat_id, day)
            return

    try:
        new_treasury = await db.treasury_get(chat_id)
    except Exception as e:
        logger.exception("tax_job: не удалось получить баланс казны: %s", e)
        new_treasury = 0

    if total <= 0:
        text = (
            f"🏦 <b>Налог дня</b>\n"
            f"Сегодня платить некому: у активных баланс ≤ {TAX_MIN_BALANCE}.\n"
            f"Казна: <b>{new_treasury}</b>"
        )
    else:
        text = (
            f"🏦 <b>Налог дня</b>\n"
            f"Ставка: <b>{TAX_PCT}%</b> с баланса > {TAX_MIN_BALANCE}\n"
            f"Платят только активные (писали за {TAX_ACTIVE_DAYS} дн.)\n"
            f"Собрано в казну: <b>{total}</b> монет\n"
            f"Казна: <b>{new_treasury}</b>\n\n"
            f"<i>Неактивные не платят. Вернулся — баланс на месте.</i>"
        )
    try:
        await context.bot.send_message(
            chat_id=chat_id, text=text, parse_mode=ParseMode.HTML,
        )
    except TelegramError as e:
        logger.warning("tax_job send: %s", e)


async def credit_auto_collect_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Ежедневно удерживает часть положительного баланса в счёт кредита."""
    try:
        affected = await db.auto_collect_credit_payments(CREDIT_AUTO_COLLECT_PCT, _today_utc())
        if affected:
            logger.info("credit_auto_collect_job: автоматически погашено кредитов у %s пользователей", affected)
    except Exception as e:
        logger.exception("credit_auto_collect_job: %s", e)


async def daily_interest_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = _env_int("CHAT_ID", 0)
    if not chat_id:
        return
    try:
        affected = await db.apply_credit_interest_to_treasury(
            chat_id, CREDIT_PERCENT_PER_DAY, _today_utc(),
        )
        logger.info("daily_interest_job: затронуто %s, казна пополнена", affected)
    except Exception as e:
        logger.warning("daily_interest_job: %s", e)


async def savings_interest_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Ежедневные проценты по сберкнижкам; источник выплат — казна."""
    chat_id = _env_int("CHAT_ID", 0)
    if not chat_id:
        logger.error("savings_interest_job: CHAT_ID не задан")
        return
    try:
        paid, users_paid = await db.savings_accrue_and_pay(
            chat_id, _today_utc(), SAVINGS_INTEREST_BPS,
        )
        if paid:
            logger.info("savings_interest_job: выплачено %s монет %s пользователям", paid, users_paid)
    except Exception:
        logger.exception("savings_interest_job: ошибка начисления процентов")


def register_jobs(app) -> None:
    if not app.job_queue:
        logger.warning("economy: job_queue нет, джобы не будут начисляться")
        return
    app.job_queue.run_daily(
        savings_interest_job,
        time=dtime(hour=0, minute=5, tzinfo=TZ),
    )
    app.job_queue.run_daily(
        daily_interest_job,
        time=dtime(hour=0, minute=10, tzinfo=TZ),
    )
    app.job_queue.run_daily(
        credit_auto_collect_job,
        time=dtime(hour=0, minute=25, tzinfo=TZ),
    )
    app.job_queue.run_daily(
        tax_job,
        time=dtime(hour=0, minute=40, tzinfo=TZ),
    )
    app.job_queue.run_repeating(
        default_watchdog_job, interval=3600, first=60,
    )
    logger.info(
        "economy: сберкнижки (00:05), проценты по кредитам (00:10), автопогашение кредитов (00:25), налог (00:40), "
        "watchdog (раз в час), cooldown дефолта %sч, "
        "органический pct=%s%%, искусственный pct=100%% запланированы",
        DEFAULT_COOLDOWN_HOURS, DEFAULT_PCT,
    )
