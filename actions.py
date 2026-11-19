"""Действия: старые + новые (Засос, Расстрел(админ), По шапке, Выстрел,
Расцеловать, Сделать 18+, развлекательные, Пожать руку).

Все тексты — в TEXTS_OLD и TEXTS_NEW. В коде — нейтральные имена команд.
«Сделать 18+» реализовано нейтрально (плейсхолдер «...»), заполняется в TEXTS_NEW.

Изменения: если цель действия — супруг(а) актора, начисляется XP браку
(через marriage_xp.award_if_spouse).
"""
from __future__ import annotations

import os
import asyncio
import re
import time
import logging
from html import escape
from typing import Optional, Tuple

from telegram import Update
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import ContextTypes

import db
import utils
import marriage_xp

logger = logging.getLogger(__name__)

try:
    ADMIN_ID = int(os.getenv("ADMIN_ID", "0") or "0")
except (TypeError, ValueError):
    logger.warning("Invalid ADMIN_ID; admin-only actions are disabled")
    ADMIN_ID = 0

ACTION_COST = 0
ACTION_COOLDOWN_SEC = 0
_LAST_ACTION: dict[tuple[int, int, str], float] = {}
_ACTION_LOCKS: dict[tuple[int, int, str], asyncio.Lock] = {}
_MAX_ACTION_CACHE = 10_000


TEXTS_OLD: dict[str, str] = {
    "slap": "{actor} отвесил(а) леща {target} 💥",
    "kiss": "{actor} поцеловал(а) {target} 💋",
    "hug": "{actor} обнял(а) {target} 🤗",
    "hit": "{actor} побил(а) {target} 👊",
    "piss": "{actor} обоссал(а) {target} 💦",
    "pat": "{actor} погладил(а) {target} ✋",
}


TEXTS_NEW: dict[str, dict[str, str]] = {
    "kiss_deep": {
        "self": "Себе нельзя 🙂",
        "ok": "{actor} засосал(а) {target} 💋🔥",
    },
    "shoot": {
        "self": "Себе нельзя 🙂",
        "ok": "🔫 {actor} расстрелял(а) {target}",
        "admin_only": "⛔ Только админ может расстреливать.",
    },
    "bonk": {
        "self": "Себе нельзя 🙂",
        "ok": "🔨 {actor} дал(а) по шапке {target}",
    },
    "gunshot": {
        "self": "Себе нельзя 🙂",
        "ok": "💥 {actor} выстрелил(а) в {target}",
    },
    "kiss_all": {
        "self": "Себе нельзя 🙂",
        "ok": "😘 {actor} расцеловал(а) {target}",
    },
    "make18": {
        "self": "Себе нельзя 🙂",
        "ok": "🔞 {actor} сделал(а) интересные дела с {target}",
    },
    "dance": {
        "self": "Сам с собой танцуешь? 🙂",
        "ok": "💃 {actor} станцевал(а) для {target}",
    },
    "hug_all": {
        "self": "Себе нельзя 🙂",
        "ok": "🤗 {actor} обнял(а) всех, включая {target}",
    },
    "pat_all": {
        "self": "Себе нельзя 🙂",
        "ok": "✋ {actor} погладил(а) {target} и всех вокруг",
    },
    "slap_all": {
        "self": "Себе нельзя 🙂",
        "ok": "👋 {actor} раздал(а) лещей всем, включая {target}",
    },
    "poke": {
        "self": "Себе нельзя 🙂",
        "ok": "👉 {actor} тыкнул(а) {target}",
    },
    "tickle": {
        "self": "Себе нельзя 🙂",
        "ok": "🪶 {actor} защекотал(а) {target}",
    },
    "handshake": {
        "self": "Сам с собой жать руку? 🙂",
        "ok": "🤝 {actor} пожал(а) руку {target}",
    },
}


def _fmt(template: str, actor_name: str, target_name: str) -> str:
    return template.format(
        actor=escape(actor_name),
        target=escape(target_name),
    )


async def _resolve_actor_target(
    update: Update, context: ContextTypes.DEFAULT_TYPE, clean_text: str,
) -> Optional[Tuple[int, str, int, str]]:
    """Определяет актёра и цель; ошибки пользователю отправляются только здесь."""
    user = update.effective_user
    msg = update.effective_message
    if not user or not msg:
        return None

    try:
        target = await utils.resolve_target(update, context, clean_text)
    except Exception:
        logger.exception("actions target resolution failed")
        await msg.reply_text("Не удалось определить цель действия. Попробуйте ещё раз.")
        return None

    if not target:
        await msg.reply_text("Укажите @username или сделайте реплай.")
        return None

    try:
        target_id, target_name = target
        target_id = int(target_id)
    except (TypeError, ValueError):
        await msg.reply_text("Не удалось определить пользователя.")
        return None

    if target_id == user.id:
        await msg.reply_text("Нельзя применить это к себе 🙂")
        return None
    if context.bot and target_id == context.bot.id:
        await msg.reply_text("На бота нельзя 🙂")
        return None

    actor_name = user.first_name or user.username or str(user.id)
    return user.id, actor_name, target_id, str(target_name or target_id)


async def _do_old(update: Update, context: ContextTypes.DEFAULT_TYPE,
                  key: str, clean_text: str) -> None:
    msg = update.effective_message
    chat = update.effective_chat
    if not msg or not chat:
        return
    resolved = await _resolve_actor_target(update, context, clean_text)
    if not resolved:
        return
    actor_id, actor_name, target_id, target_name = resolved
    text = _fmt(TEXTS_OLD.get(key, "{actor} → {target}"), actor_name, target_name)
    try:
        await msg.reply_text(text, parse_mode=ParseMode.HTML)
    except TelegramError as e:
        logger.warning("actions old send: %s", e)
        return

    try:
        await marriage_xp.award_if_spouse(chat.id, actor_id, target_id, key)
    except Exception as e:
        logger.warning("actions old marriage_xp: %s", e)


def _clean(text: str, triggers: str) -> str:
    return re.sub(rf"(?i)^(?:{triggers})\s*", "", text or "").strip()


async def cmd_slap(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    t = (update.effective_message.text or "") if update.effective_message else ""
    await _do_old(update, context, "slap", _clean(t, r"дать\s+леща"))


async def cmd_kiss(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    t = (update.effective_message.text or "") if update.effective_message else ""
    await _do_old(update, context, "kiss", _clean(t, r"поцеловать"))


async def cmd_hug(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    t = (update.effective_message.text or "") if update.effective_message else ""
    await _do_old(update, context, "hug", _clean(t, r"обнять"))


async def cmd_hit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    t = (update.effective_message.text or "") if update.effective_message else ""
    await _do_old(update, context, "hit", _clean(t, r"побить"))


async def cmd_piss(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    t = (update.effective_message.text or "") if update.effective_message else ""
    await _do_old(update, context, "piss", _clean(t, r"обоссать"))


async def cmd_pat(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    t = (update.effective_message.text or "") if update.effective_message else ""
    await _do_old(update, context, "pat", _clean(t, r"погладить"))


async def _do_new(update: Update, context: ContextTypes.DEFAULT_TYPE,
                   key: str, clean_text: str, admin_only: bool = False) -> None:
    msg = update.effective_message
    chat = update.effective_chat
    user = update.effective_user
    if not msg or not chat or not user:
        return

    cfg = TEXTS_NEW.get(key)
    if not cfg or not cfg.get("ok"):
        logger.error("Unknown or incomplete action configuration: %s", key)
        await msg.reply_text("Это действие сейчас недоступно.")
        return

    if admin_only and (not ADMIN_ID or user.id != ADMIN_ID):
        await msg.reply_text(cfg.get("admin_only", "⛔ Только админ."))
        return

    resolved = await _resolve_actor_target(update, context, clean_text)
    if not resolved:
        return
    actor_id, actor_name, target_id, target_name = resolved

    lock_key = (actor_id, target_id, key)
    lock = _ACTION_LOCKS.setdefault(lock_key, asyncio.Lock())
    if lock.locked():
        await msg.reply_text("⏳ Это действие уже выполняется. Подожди немного.")
        return

    async with lock:
        now = time.monotonic()
        try:
            cooldown = max(0, int(ACTION_COOLDOWN_SEC))
            cost = max(0, int(ACTION_COST))
        except (TypeError, ValueError, OverflowError):
            logger.exception("Invalid ACTION_COOLDOWN_SEC or ACTION_COST")
            await msg.reply_text("Действие временно недоступно из-за ошибки настроек.")
            return

        last = _LAST_ACTION.get(lock_key, 0.0)
        if cooldown and now - last < cooldown:
            remain = max(1, int(cooldown - (now - last)))
            await msg.reply_text(f"⏳ Подожди {remain} сек.")
            return

        charged = False
        treasury_credited = False
        if cost:
            try:
                charged = bool(await db.take_balance_atomic(actor_id, cost))
            except Exception:
                logger.exception("actions charge failed")
                await msg.reply_text("Не удалось списать монеты. Действие отменено.")
                return
            if not charged:
                await msg.reply_text(
                    f"⛔ Нужно <b>{cost}</b> монет на это действие.",
                    parse_mode=ParseMode.HTML,
                )
                return

        text = _fmt(cfg["ok"], actor_name, target_name)
        try:
            await msg.reply_text(text, parse_mode=ParseMode.HTML)
        except Exception:
            logger.exception("actions new send failed")
            if charged:
                try:
                    if not await db.add_balance(actor_id, cost):
                        logger.critical("Failed to refund action cost to user %s", actor_id)
                except Exception:
                    logger.exception("CRITICAL: user refund failed")
            return

        # Credit the treasury only after Telegram accepted the action message.
        # If this fails, refund the user instead of leaving a paid-but-unaccounted action.
        if charged:
            try:
                treasury_credited = bool(await db.treasury_add(chat.id, cost))
            except Exception:
                logger.exception("actions treasury credit failed")
                treasury_credited = False
            if not treasury_credited:
                try:
                    if not await db.add_balance(actor_id, cost):
                        logger.critical("Failed to refund action cost after treasury error to user %s", actor_id)
                except Exception:
                    logger.exception("CRITICAL: failed to refund action cost after treasury error")

        if cooldown:
            _LAST_ACTION[lock_key] = time.monotonic()

        # Bound in-memory caches for long-running bot processes.
        if len(_LAST_ACTION) > _MAX_ACTION_CACHE:
            newest = sorted(_LAST_ACTION.items(), key=lambda item: item[1], reverse=True)
            _LAST_ACTION.clear()
            _LAST_ACTION.update(newest[:_MAX_ACTION_CACHE])
        if len(_ACTION_LOCKS) > _MAX_ACTION_CACHE:
            for old_key, old_lock in list(_ACTION_LOCKS.items()):
                if old_key != lock_key and not old_lock.locked():
                    _ACTION_LOCKS.pop(old_key, None)
                    if len(_ACTION_LOCKS) <= _MAX_ACTION_CACHE:
                        break

        try:
            await db.inc_action_stat(chat.id, actor_id, key, 1)
        except Exception as e:
            logger.warning("actions stat: %s", e)

        try:
            await marriage_xp.award_if_spouse(chat.id, actor_id, target_id, key)
        except Exception as e:
            logger.warning("actions new marriage_xp: %s", e)


async def cmd_kiss_deep(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    t = (update.effective_message.text or "") if update.effective_message else ""
    await _do_new(update, context, "kiss_deep", _clean(t, r"засос"))


async def cmd_shoot(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    t = (update.effective_message.text or "") if update.effective_message else ""
    await _do_new(update, context, "shoot", _clean(t, r"расстрел"), admin_only=True)


async def cmd_bonk(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    t = (update.effective_message.text or "") if update.effective_message else ""
    await _do_new(update, context, "bonk", _clean(t, r"дать\s+по\s+шапке"))


async def cmd_gunshot(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    t = (update.effective_message.text or "") if update.effective_message else ""
    await _do_new(update, context, "gunshot", _clean(t, r"выстрел"))


async def cmd_kiss_all(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    t = (update.effective_message.text or "") if update.effective_message else ""
    await _do_new(update, context, "kiss_all", _clean(t, r"расцеловать"))


async def cmd_make18(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    t = (update.effective_message.text or "") if update.effective_message else ""
    await _do_new(update, context, "make18", _clean(t, r"сделать\s+18\+?"))


async def cmd_dance(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    t = (update.effective_message.text or "") if update.effective_message else ""
    await _do_new(update, context, "dance", _clean(t, r"станцевать|танец"))


async def cmd_hug_all(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    t = (update.effective_message.text or "") if update.effective_message else ""
    await _do_new(update, context, "hug_all", _clean(t, r"обнять\s+всех"))


async def cmd_pat_all(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    t = (update.effective_message.text or "") if update.effective_message else ""
    await _do_new(update, context, "pat_all", _clean(t, r"погладить\s+всех"))


async def cmd_slap_all(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    t = (update.effective_message.text or "") if update.effective_message else ""
    await _do_new(update, context, "slap_all", _clean(t, r"лещей\s+всем|дать\s+леща\s+всем"))


async def cmd_poke(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    t = (update.effective_message.text or "") if update.effective_message else ""
    await _do_new(update, context, "poke", _clean(t, r"тыкнуть|тык"))


async def cmd_tickle(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    t = (update.effective_message.text or "") if update.effective_message else ""
    await _do_new(update, context, "tickle", _clean(t, r"защекотать|щекотать"))


async def cmd_handshake(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    t = (update.effective_message.text or "") if update.effective_message else ""
    await _do_new(update, context, "handshake", _clean(t, r"пожать\s+руку|рукопожатие"))
