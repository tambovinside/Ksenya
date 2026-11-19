"""Казино с виртуальными ставками и контролируемыми выплатами."""
from __future__ import annotations

import asyncio
import secrets
from collections import defaultdict

import db
from telegram import Update, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import ContextTypes
from ui_design import InlineKeyboardButton

MIN_BET = 5
MAX_BET = 200
BET_OPTIONS = (5, 10, 25, 50, 100, 150, 200)

# Множители означают полную выплату вместе с возвращённой ставкой.
PAYOUTS = {
    "coin": 1.90,
    "roulette": 1.90,
    "highlow": 1.90,
    "dice": 5.40,
    "slots_pair": 1.25,
    "slots_triple": 8.00,
}

_STATS = defaultdict(lambda: {
    "games": 0, "wins": 0, "wagered": 0, "paid": 0, "net": 0,
})
# Callback query ID уникален для нажатия. Кэш не даёт повторно списать ставку
# при повторной доставке одного и того же обновления в рамках процесса.
_PROCESSED_ROUNDS: set[str] = set()
_PROCESSED_ORDER: list[str] = []
_PLAYED_MESSAGES: set[tuple[int, int]] = set()
_ROUND_LOCKS: dict[tuple[int, int], asyncio.Lock] = {}
MAX_PROCESSED_CACHE = 5000


def _cb(user_id: int, *parts: object) -> str:
    """Владелец меню хранится в callback_data, а не в оперативной памяти."""
    return ":".join(("cas", str(user_id), *(str(part) for part in parts)))


def _keyboard(user_id: int):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🪙 Монетка", callback_data=_cb(user_id, "game", "coin")),
         InlineKeyboardButton("🎲 Кубик", callback_data=_cb(user_id, "game", "dice"))],
        [InlineKeyboardButton("🎰 Слоты", callback_data=_cb(user_id, "game", "slots")),
         InlineKeyboardButton("🎡 Рулетка", callback_data=_cb(user_id, "game", "roulette"))],
        [InlineKeyboardButton("🃏 Старшая карта", callback_data=_cb(user_id, "game", "highlow"))],
        [InlineKeyboardButton("📊 Статистика", callback_data=_cb(user_id, "stats")),
         InlineKeyboardButton("📖 Правила", callback_data=_cb(user_id, "help"))],
        [InlineKeyboardButton("← Главное меню", callback_data="home:back")],
    ])


def _back(user_id: int):
    return [InlineKeyboardButton("⬅️ В меню казино", callback_data=_cb(user_id, "back"))]


def _menu_text():
    return (
        "🎰 <b>КАZИНО</b>\n━━━━━━━━━━━━━━━━━━\n"
        "Играй на виртуальные монеты и проверь удачу.\n"
        f"Ставки: <b>{MIN_BET}–{MAX_BET}</b> монет.\n"
        "Выбери игру:"
    )


async def cmd_casino(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user, msg = update.effective_user, update.effective_message
    if not user or not msg:
        return
    if not await db.get_user(user.id):
        await db.upsert_user(user.id, user.username, user.first_name)
    sent = await msg.reply_text(_menu_text(), parse_mode=ParseMode.HTML, reply_markup=_keyboard(user.id))


async def open_casino_from_callback(query, user):
    if not query or not query.message or not user:
        return
    await query.edit_message_text(_menu_text(), parse_mode=ParseMode.HTML, reply_markup=_keyboard(user.id))


async def casino_help_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q, user = update.callback_query, update.effective_user
    if not q or not user or not q.message:
        return
    parts = (q.data or "").split(":")
    # Формат: cas:<user_id>:<action>:... . Проверка не зависит от RAM-кэша
    # и поэтому не ломается после обычных сообщений или перезапуска процесса.
    if len(parts) < 3 or parts[0] != "cas" or not parts[1].isdigit():
        await q.answer("Кнопка устарела. Открой казино заново.", show_alert=True)
        return
    if int(parts[1]) != user.id:
        await q.answer("Это меню казино открыл другой игрок.", show_alert=True)
        return
    data = ["cas", *parts[2:]]
    if data == ["cas", "help"]:
        await _render(
            q,
            "📖 <b>ПРАВИЛА КАЗИНО</b>\n\n"
            f"• Ставка: от <b>{MIN_BET}</b> до <b>{MAX_BET}</b> монет.\n"
            "• Ставка списывается перед раундом.\n"
            "• При проигрыше ставка сгорает.\n"
            "• Выплата при выигрыше включает первоначальную ставку.\n"
            "• Нельзя поставить больше доступного баланса.\n"
            "• Повторное нажатие на уже обработанный раунд не должно повторно списывать ставку.\n\n"
            "<i>Это внутриигровые виртуальные монеты без денежной стоимости.</i>",
            InlineKeyboardMarkup([_back(user.id)]),
        )
        return

    if data == ["cas", "stats"]:
        s = _STATS[user.id]
        total = s["games"]
        pct = 100 * s["wins"] / total if total else 0
        await _render(
            q,
            "📊 <b>СТАТИСТИКА КАЗИНО</b>\n\n"
            f"Раундов: <b>{total}</b>\n"
            f"Побед: <b>{s['wins']}</b>\n"
            f"Ставлено: <b>{s['wagered']}</b> монет\n"
            f"Выплачено: <b>{s['paid']}</b> монет\n"
            f"Итог: <b>{s['net']:+d}</b> монет\n"
            f"Доля побед: <b>{pct:.1f}%</b>",
            InlineKeyboardMarkup([_back(user.id)]),
        )
        return

    if data == ["cas", "back"]:
        await _render(q, _menu_text(), _keyboard(user.id))
        return

    if len(data) == 3 and data[:2] == ["cas", "game"]:
        await _show_bets(q, data[2], user.id)
        return

    if len(data) == 4 and data[:2] == ["cas", "bet"]:
        game, amount_text = data[2], data[3]
        if not amount_text.isdigit():
            await q.answer("Некорректная ставка.", show_alert=True)
            return
        amount = int(amount_text)
        if not MIN_BET <= amount <= MAX_BET:
            await q.answer(f"Ставка должна быть от {MIN_BET} до {MAX_BET}.", show_alert=True)
            return
        await _show_game_choice(q, game, amount, user.id)
        return

    if len(data) >= 4 and data[:2] == ["cas", "play"]:
        await _play(q, user.id, data[2], data[3], data[4:])
        return

    await q.answer("Кнопка устарела. Открой меню заново.", show_alert=True)


async def _show_bets(q, game, user_id):
    if q.message:
        _PLAYED_MESSAGES.discard((q.message.chat.id, q.message.message_id))
    names = {
        "coin": "🪙 МОНЕТКА",
        "dice": "🎲 КУБИК",
        "slots": "🎰 СЛОТЫ",
        "roulette": "🎡 РУЛЕТКА",
        "highlow": "🃏 СТАРШАЯ КАРТА",
    }
    if game not in names:
        await q.answer("Неизвестная игра.", show_alert=True)
        return
    rows = []
    for start in range(0, len(BET_OPTIONS), 3):
        rows.append([
            InlineKeyboardButton(
                f"{amount} 🪙",
                callback_data=_cb(user_id, "bet", game, amount),
            )
            for amount in BET_OPTIONS[start:start + 3]
        ])
    rows.append(_back(user_id))
    await _render(
        q,
        f"{names[game]}\n\nВыбери ставку от <b>{MIN_BET}</b> до <b>{MAX_BET}</b> монет:",
        InlineKeyboardMarkup(rows),
    )


async def _show_game_choice(q, game, amount, user_id):
    prompts = {
        "coin": ("🪙 <b>МОНЕТКА</b>\nУгадай сторону:", [
            InlineKeyboardButton("Орёл", callback_data=_cb(user_id, "play", "coin", amount, "heads")),
            InlineKeyboardButton("Решка", callback_data=_cb(user_id, "play", "coin", amount, "tails")),
        ]),
        "dice": ("🎲 <b>КУБИК</b>\nВыбери число:", [
            InlineKeyboardButton(str(n), callback_data=_cb(user_id, "play", "dice", amount, n))
            for n in range(1, 7)
        ]),
        "roulette": ("🎡 <b>РУЛЕТКА</b>\nУгадай цвет сектора:", [
            InlineKeyboardButton("🔴 Красный", callback_data=_cb(user_id, "play", "roulette", amount, "red")),
            InlineKeyboardButton("⚫ Чёрный", callback_data=_cb(user_id, "play", "roulette", amount, "black")),
        ]),
        "highlow": ("🃏 <b>СТАРШАЯ КАРТА</b>\nУгадай цвет карты:", [
            InlineKeyboardButton("♥ Красная", callback_data=_cb(user_id, "play", "highlow", amount, "red")),
            InlineKeyboardButton("♠ Чёрная", callback_data=_cb(user_id, "play", "highlow", amount, "black")),
        ]),
        "slots": ("🎰 <b>СЛОТЫ</b>\nНажми, чтобы запустить барабаны:", [
            InlineKeyboardButton("Крутить", callback_data=_cb(user_id, "play", "slots", amount)),
        ]),
    }
    if game not in prompts:
        await q.answer("Неизвестная игра.", show_alert=True)
        return
    caption, buttons = prompts[game]
    if buttons and isinstance(buttons[0], list):
        rows = [buttons]
    else:
        rows = [buttons] if game == "slots" else [buttons]
    rows.append([InlineKeyboardButton("↩️ Изменить ставку", callback_data=_cb(user_id, "game", game))])
    rows.append(_back(user_id))
    await _render(q, f"{caption}\nСтавка: <b>{amount}</b> монет", InlineKeyboardMarkup(rows))


def _remember_round(callback_id: str) -> bool:
    if not callback_id or callback_id in _PROCESSED_ROUNDS:
        return False
    _PROCESSED_ROUNDS.add(callback_id)
    _PROCESSED_ORDER.append(callback_id)
    if len(_PROCESSED_ORDER) > MAX_PROCESSED_CACHE:
        old = _PROCESSED_ORDER.pop(0)
        _PROCESSED_ROUNDS.discard(old)
    return True


async def _play(q, user_id, game, amount_text, args):
    if not q.message:
        await q.answer("Не удалось определить раунд.", show_alert=True)
        return
    key = (q.message.chat.id, q.message.message_id)
    lock = _ROUND_LOCKS.setdefault(key, asyncio.Lock())
    async with lock:
        if key in _PLAYED_MESSAGES:
            await q.answer("Ставка в этом раунде уже принята.", show_alert=True)
            return
        await _play_locked(q, user_id, game, amount_text, args, key)


async def _play_locked(q, user_id, game, amount_text, args, message_key):
    if not str(amount_text).isdigit():
        await q.answer("Некорректная ставка.", show_alert=True)
        return
    amount = int(amount_text)
    if not MIN_BET <= amount <= MAX_BET:
        await q.answer("Ставка должна быть от 5 до 200 монет.", show_alert=True)
        return
    if game not in {"coin", "dice", "slots", "roulette", "highlow"}:
        await q.answer("Неизвестная игра.", show_alert=True)
        return

    callback_id = getattr(q, "id", "")
    if not _remember_round(callback_id):
        await q.answer("Этот раунд уже обработан.", show_alert=True)
        return

    # Проверка и списание выполняются атомарным UPDATE в базе.
    if not await db.take_balance_atomic(user_id, amount):
        row = await db.get_user(user_id)
        balance = int((row or {}).get("balance") or 0)
        await q.answer(f"Недостаточно монет. Баланс: {balance}.", show_alert=True)
        return

    _PLAYED_MESSAGES.add(message_key)
    won = False
    gross_payout = 0
    if game == "coin" and args and args[0] in {"heads", "tails"}:
        result = secrets.choice(("heads", "tails"))
        won = args[0] == result
        label = "Выпало: <b>" + ("Орёл" if result == "heads" else "Решка") + "</b>"
        if won:
            gross_payout = int(amount * PAYOUTS["coin"])
    elif game == "dice" and args and args[0].isdigit() and 1 <= int(args[0]) <= 6:
        result = secrets.randbelow(6) + 1
        won = int(args[0]) == result
        label = f"Выпало число: <b>{result}</b>"
        if won:
            gross_payout = int(amount * PAYOUTS["dice"])
    elif game in {"roulette", "highlow"} and args and args[0] in {"red", "black"}:
        result = secrets.choice(("red", "black"))
        won = args[0] == result
        if game == "roulette":
            label = "Выпал сектор: <b>" + ("🔴 красный" if result == "red" else "⚫ чёрный") + "</b>"
        else:
            label = "Карта: <b>" + ("♥ красная" if result == "red" else "♠ чёрная") + "</b>"
        if won:
            gross_payout = int(amount * PAYOUTS[game])
    elif game == "slots" and not args:
        symbols = ["🍒", "🍋", "🍇", "⭐", "💎", "7️⃣"]
        result = [secrets.choice(symbols) for _ in range(3)]
        counts = {symbol: result.count(symbol) for symbol in set(result)}
        if 3 in counts.values():
            won = True
            gross_payout = int(amount * PAYOUTS["slots_triple"])
            combination = "ТРИ ОДИНАКОВЫХ"
        elif 2 in counts.values():
            won = True
            gross_payout = int(amount * PAYOUTS["slots_pair"])
            combination = "ПАРА"
        else:
            combination = "НЕТ СОЧЕТАНИЯ"
        label = "Барабаны: <b>" + "　".join(result) + "</b>\n" + combination
    else:
        # Некорректный callback: возвращаем списанную ставку.
        await db.add_balance(user_id, amount)
        await q.answer("Некорректный ход.", show_alert=True)
        return

    if won and gross_payout > 0:
        if not await db.add_balance(user_id, gross_payout):
            # Если начисление не удалось, отменяем списание, чтобы игрок не потерял ставку.
            await db.add_balance(user_id, amount)
            await q.answer("Не удалось начислить выигрыш. Ставка возвращена.", show_alert=True)
            return

    s = _STATS[user_id]
    s["games"] += 1
    s["wagered"] += amount
    s["paid"] += gross_payout
    s["net"] += gross_payout - amount
    if won:
        s["wins"] += 1

    row = await db.get_user(user_id)
    balance = int((row or {}).get("balance") or 0)
    if won:
        profit = gross_payout - amount
        verdict = f"🎉 <b>Выигрыш!</b>\nВыплата: <b>{gross_payout}</b> монет\nЧистая прибыль: <b>+{profit}</b>"
    else:
        verdict = f"🙂 <b>Не повезло.</b> Ставка <b>{amount}</b> монет проиграна."
    await _render(
        q,
        f"{label}\n\n{verdict}\nБаланс: <b>{balance}</b> монет",
        InlineKeyboardMarkup([
            [InlineKeyboardButton("🔁 Ещё раунд", callback_data=_cb(user_id, "game", game))],
            _back(user_id),
        ]),
    )


async def _render(q, text, keyboard):
    try:
        await q.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=keyboard)
    except TelegramError as exc:
        if "not modified" not in str(exc).lower():
            raise
    await q.answer()
