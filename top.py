"""2.3 Топ чата + зарплата за активность из казны.

Функции, которые регистрирует main.py:
  - track_message       — считает сообщения/дроби зарплаты
  - cmd_top_all         — Топ все
  - cmd_top_day         — Топ день
  - cmd_top_rich        — Топ богачи
  - cmd_my_place        — Мое место
  - flush_job           — периодический flush (interval из main)
  - cleanup_job         — daily cleanup (00:05 UTC)
  - _flush              — вызывается из post_shutdown
  - FLUSH_INTERVAL_SEC  — интервал flush в main.py

Исправления:
  - дробная зарплата копится в pending_salary (в БД), переживает рестарты;
  - _flush не теряет батчи при рестарте (3 ретрая + requeue);
  - flush_job НЕ вызывает дефолт (дефолт — в economy.default_watchdog_job);
  - cleanup_old_days(keep_days=1): не стираем вчерашний messages_day
    до налога в 00:40;
  - catch-up: flush_job выплачивает все накопленные дроби.
"""
import asyncio
import logging
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from html import escape

from telegram import Update
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import ContextTypes

import db
import premium
import accounts

logger = logging.getLogger(__name__)

FLUSH_INTERVAL_SEC = 15
FLUSH_MAX_KEYS = 200

MIN_SALARY_LEN = 2
SALARY_FRAC_PER_MSG = 20

_USER_CACHE: dict[int, tuple[str, str]] = {}
_PREMIUM_CACHE: dict[int, tuple[float, str | None]] = {}
_PREMIUM_CACHE_TTL = 300
_CACHE_MAX_USERS = 10000

# _PENDING — счётчики сообщений для messages/messages_day.
# _PENDING_SALARY — in-memory буфер перед записью в pending_salary.
_PENDING: dict[tuple[int, int, str], int] = defaultdict(int)
_PENDING_SALARY: dict[tuple[int, int, str], int] = defaultdict(int)
_flush_lock = asyncio.Lock()
_salary_locks: dict[tuple[int, int, str], asyncio.Lock] = {}



async def _premium_marks(user_ids: list[int]) -> dict[int, str]:
    """Получает актуальные метки через Premium API.

    Не используем длительный отрицательный кеш: после покупки подписки
    метка должна появиться сразу, а не через несколько минут.
    """
    result: dict[int, str] = {}
    ids = list(dict.fromkeys(int(uid) for uid in user_ids if int(uid) > 0))
    for uid in ids:
        try:
            subscription = await premium.get(uid)
        except Exception:
            logger.exception("Не удалось проверить Premium для пользователя %s", uid)
            continue
        if not subscription or not subscription.get("active"):
            _PREMIUM_CACHE.pop(uid, None)
            continue

        plan = str(subscription.get("plan") or "").lower()
        if plan == "pro":
            result[uid] = "💎"
        elif plan == "base":
            result[uid] = "🤑"
        _PREMIUM_CACHE.pop(uid, None)
    return result


def _display_name(user_id: int, names: dict[int, str] | None = None) -> str:
    cached = _USER_CACHE.get(user_id)
    if cached:
        username, first_name = cached
        return f"@{username}" if username else (first_name or (names or {}).get(user_id, "Пользователь"))
    return (names or {}).get(user_id, "Пользователь")


async def _resolve_display_names(
    context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_ids: list[int],
) -> dict[int, str]:
    """Имена сначала берём из БД, а для старых записей восстанавливаем из Telegram."""
    ids = list(dict.fromkeys(int(uid) for uid in user_ids if int(uid) > 0))
    names = await db.get_user_display_names(ids)
    missing = [uid for uid in ids if not names.get(uid) or names[uid] == "Пользователь"]

    # Для владельцев аккаунтов используем их уникальный логин.
    # В топах хранится owner_tg_id, поэтому ищем аккаунт именно по нему.
    for uid in list(missing):
        try:
            login = await accounts.get_login_for_owner(uid)
            if login:
                names[uid] = login
                missing.remove(uid)
        except Exception as exc:
            logger.debug("Не удалось получить логин аккаунта %s: %s", uid, exc)

    # Для пользователей без аккаунта пробуем получить имя из Telegram.
    for uid in missing:
        try:
            member = await context.bot.get_chat_member(chat_id=chat_id, user_id=uid)
            user = member.user
            username = (user.username or "").strip().lstrip("@")
            first_name = (user.first_name or "").strip()
            display = f"@{username}" if username else (first_name or "Пользователь")
            names[uid] = display
            await db.upsert_user(uid, user.username, user.first_name or "")
            _USER_CACHE[uid] = (user.username or "", user.first_name or "")
        except Exception as exc:
            logger.debug("Не удалось получить имя участника %s из чата %s: %s", uid, chat_id, exc)
    return names


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


async def track_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    user = update.effective_user
    if not msg or not user or user.is_bot:
        return
    chat = update.effective_chat
    if not chat:
        return
    try:
        cur = (user.username or "", user.first_name or "")
        if _USER_CACHE.get(user.id) != cur:
            await db.upsert_user(user.id, user.username, user.first_name)
            _USER_CACHE[user.id] = cur
            if len(_USER_CACHE) > _CACHE_MAX_USERS:
                _USER_CACHE.clear()


        day = _today()
        key = (chat.id, user.id, day)
        _PENDING[key] += 1

        text = (msg.text or "").strip()
        is_cmd = text.startswith("/")
        reply_to_bot = bool(
            msg.reply_to_message
            and msg.reply_to_message.from_user
            and msg.reply_to_message.from_user.id == context.bot.id
        )
        if (
            not is_cmd
            and not reply_to_bot
            and len(text) >= MIN_SALARY_LEN
        ):
            _PENDING_SALARY[key] += SALARY_FRAC_PER_MSG

        if len(_PENDING) + len(_PENDING_SALARY) >= FLUSH_MAX_KEYS:
            await _flush()
    except Exception as e:
        logger.warning("track_message: %s", e)


def _requeue_msg(chat_id: int, user_id: int, day: str, delta: int) -> None:
    if delta > 0:
        _PENDING[(chat_id, user_id, day)] += delta


def _requeue_sal(chat_id: int, user_id: int, day: str, delta: int) -> None:
    if delta > 0:
        _PENDING_SALARY[(chat_id, user_id, day)] += delta


async def _flush() -> None:
    async with _flush_lock:
        if not _PENDING and not _PENDING_SALARY:
            return
        snap_msg = dict(_PENDING)
        snap_sal = dict(_PENDING_SALARY)
        _PENDING.clear()
        _PENDING_SALARY.clear()

    # 1) messages (не критично: при потере — только счётчик топов)
    for (chat_id, user_id, day), delta in snap_msg.items():
        if delta <= 0:
            continue
        key = uuid.uuid4().hex
        ok = False
        for attempt in range(3):
            ok = await db.inc_message_batch_idempotent(
                chat_id, user_id, day, delta, key,
            )
            if ok:
                break
            await asyncio.sleep(0.5 * (attempt + 1))
        if not ok:
            logger.error(
                "flush msg: потерян батч (chat=%s user=%s day=%s delta=%s)",
                chat_id, user_id, day, delta,
            )
            _requeue_msg(chat_id, user_id, day, delta)

    # 2) зарплата: сначала фиксируем дроби в pending_salary,
    #    потом пытаемся выплатить.
    for (chat_id, user_id, day), frac_delta in snap_sal.items():
        if frac_delta <= 0:
            continue
        saved = False
        salary_event_key = uuid.uuid4().hex
        for attempt in range(3):
            saved = await db.add_pending_salary(
                chat_id, user_id, day, frac_delta, event_key=salary_event_key,
            )
            if saved:
                break
            await asyncio.sleep(0.5 * (attempt + 1))
        if not saved:
            logger.error(
                "flush salary: не удалось сохранить pending_salary "
                "(chat=%s user=%s day=%s delta=%s) — возвращаем в память",
                chat_id, user_id, day, frac_delta,
            )
            _requeue_sal(chat_id, user_id, day, frac_delta)
            continue

        await _pay_pending_salary(chat_id, user_id, day)


async def _pay_pending_salary(chat_id: int, user_id: int, day: str) -> None:
    """Пытается выплатить накопленное; блокировка не даёт двум job забрать одну сумму."""
    key = (chat_id, user_id, day)
    lock = _salary_locks.setdefault(key, asyncio.Lock())
    async with lock:
        frac = await db.get_pending_salary(chat_id, user_id, day)
        if frac <= 0:
            return

        taken = await db.take_pending_salary(chat_id, user_id, day, frac)
        if taken <= 0:
            return

        coins = await db.pay_salary_from_treasury(chat_id, user_id, day, taken)
        if coins is None:
            logger.info(
                "salary: не выплачено, возврат в pending_salary (chat=%s user=%s frac=%s)",
                chat_id, user_id, taken,
            )
            if not await db.return_pending_salary(chat_id, user_id, day, taken):
                logger.critical(
                    "salary: не удалось восстановить pending chat=%s user=%s frac=%s",
                    chat_id, user_id, taken,
                )
        else:
            logger.info(
                "salary: выплачено coins=%s (chat=%s user=%s frac_was=%s)",
                coins, chat_id, user_id, taken,
            )


async def flush_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        await _flush()
    except Exception as e:
        logger.warning("flush_job: %s", e)

    # Догоняем выплаты не чаще раза в 5 минут и ограниченной пачкой.
    # Раньше полный DISTINCT-скан и 3-4 SQL-вызова на каждую строку
    # выполнялись каждые 15 секунд и могли перегружать базу.
    try:
        now = datetime.now(timezone.utc).timestamp()
        bot_data = context.application.bot_data
        next_run = float(bot_data.get("top_salary_catchup_at", 0) or 0)
        if now >= next_run:
            bot_data["top_salary_catchup_at"] = now + 300
            rs = await db._exec(
                """SELECT chat_id, user_id, day FROM pending_salary
                   WHERE frac_delta > 0
                   ORDER BY updated_at ASC
                   LIMIT 20"""
            )
            if not getattr(rs, "error", False):
                for chat_id, user_id, day in rs.rows:
                    await _pay_pending_salary(chat_id, user_id, day)
    except Exception as e:
        logger.warning("flush_job catch-up: %s", e)


async def cleanup_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    # keep_days=1: не стираем вчерашний messages_day до налога в 00:40.
    try:
        deleted = await db.cleanup_old_days(keep_days=1)
        logger.info("cleanup_job: удалено строк messages_day: %s", deleted)
    except Exception as e:
        logger.warning("cleanup_job: %s", e)
    try:
        deleted_pending = await db.cleanup_pending_salary(keep_days=2)
        logger.info("cleanup_job: удалено строк pending_salary: %s", deleted_pending)
    except Exception as e:
        logger.warning("cleanup_job pending_salary: %s", e)


# ---------- Топ все / Топ день / Мое место ----------

def _medal(i: int) -> str:
    if i == 1:
        return "🥇"
    if i == 2:
        return "🥈"
    if i == 3:
        return "🥉"
    return f"{i}."


async def _build_top_text(
    context: ContextTypes.DEFAULT_TYPE, chat_id: int, limit: int = 10,
    title: str = "🏆 Топ чата",
) -> str:
    rows = await db.top_messages(chat_id, limit=limit)
    if not rows:
        return "Пока никто не писал — топ пуст."
    names = await _resolve_display_names(context, chat_id, [int(uid) for uid, _ in rows])
    marks = await _premium_marks([int(uid) for uid, _ in rows])
    lines = [f"<b>{escape(title)}</b>"]
    for i, (uid, cnt) in enumerate(rows, start=1):
        uid = int(uid)
        name = _display_name(uid, names)
        mark = f" {marks[uid]}" if uid in marks else ""
        lines.append(f"{_medal(i)} {escape(name)}{mark} — <b>{cnt}</b>")
    return "\n".join(lines)


async def _build_top_day_text(
    context: ContextTypes.DEFAULT_TYPE, chat_id: int, limit: int = 10,
) -> str:
    day = _today()
    rows = await db.top_messages_day(chat_id, day, limit=limit)
    if not rows:
        return "Сегодня ещё никто не писал — топ дня пуст."
    names = await _resolve_display_names(context, chat_id, [int(uid) for uid, _ in rows])
    marks = await _premium_marks([int(uid) for uid, _ in rows])
    lines = [f"<b>🏆 Топ за день ({day})</b>"]
    for i, (uid, cnt) in enumerate(rows, start=1):
        uid = int(uid)
        name = _display_name(uid, names)
        mark = f" {marks[uid]}" if uid in marks else ""
        lines.append(f"{_medal(i)} {escape(name)}{mark} — <b>{cnt}</b>")
    return "\n".join(lines)


async def cmd_top_all(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    msg = update.effective_message
    if not chat or not msg:
        return
    try:
        await _flush()
    except Exception:
        pass
    text = await _build_top_text(context, chat.id, limit=10, title="🏆 Топ за всё время")
    await msg.reply_text(text, parse_mode=ParseMode.HTML)


async def cmd_top_day(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    msg = update.effective_message
    if not chat or not msg:
        return
    try:
        await _flush()
    except Exception:
        pass
    text = await _build_top_day_text(context, chat.id, limit=10)
    await msg.reply_text(text, parse_mode=ParseMode.HTML)


async def cmd_top_rich(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Показывает рейтинг участников чата по кошельку и сберкнижке."""
    msg = update.effective_message
    chat = update.effective_chat
    if not msg or not chat:
        return

    try:
        await _flush()
        rows = await db.top_balances(limit=10, chat_id=chat.id)
    except Exception:
        logger.exception("Не удалось получить топ богачей")
        await msg.reply_text("Не удалось загрузить топ богачей. Попробуйте позже.")
        return

    if not rows:
        await msg.reply_text("💰 Пока нет данных для рейтинга богачей.")
        return

    lines = [
        "<b>💰 Топ богачей</b>",
        "<i>Учитываются монеты в кошельке и сберкнижке.</i>",
    ]
    names = await _resolve_display_names(context, chat.id, [int(user_id) for user_id, _ in rows])
    marks = await _premium_marks([int(user_id) for user_id, _ in rows])
    for i, (user_id, wealth) in enumerate(rows, start=1):
        user_id = int(user_id)
        name = _display_name(user_id, names)
        try:
            amount = int(wealth or 0)
        except (TypeError, ValueError):
            amount = 0
        mark = f" {marks[user_id]}" if user_id in marks else ""
        lines.append(f"{_medal(i)} {escape(name)}{mark} — <b>{amount:,}</b> 🪙")

    await msg.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def cmd_my_place(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    chat = update.effective_chat
    msg = update.effective_message
    if not user or not chat or not msg:
        return
    try:
        await _flush()
    except Exception:
        pass
    res = await db.user_rank(chat.id, user.id)
    if not res:
        await msg.reply_text("Вы ещё не писали в этом чате — места нет.")
        return
    count, rank, total = res
    await msg.reply_text(
        f"📊 Ваше место: <b>{rank}/{total}</b>\n"
        f"Сообщений: <b>{count}</b>",
        parse_mode=ParseMode.HTML,
    )
