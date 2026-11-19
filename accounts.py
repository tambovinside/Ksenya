"""Система аккаунтов TAMBOV INSIDE.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import re
import secrets
import time
from contextvars import ContextVar
from dataclasses import dataclass
from html import escape
from typing import Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update, User
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import ContextTypes

import db

logger = logging.getLogger(__name__)

ADMIN_ID = int(os.getenv("ADMIN_ID", "0") or "0")

# ---------------------------------------------------------------------------
# Валидация
# ---------------------------------------------------------------------------

LOGIN_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{2,19}$")
PASSWORD_RE = re.compile(r"^(?=.*[A-Za-z])[A-Za-z0-9&?@!#$_]{8,20}$")
CODE_RE = re.compile(r"^\d{5,10}$")

LOGIN_RULES = (
    "Логин: 3–20 символов, только латиница, цифры и <code>_</code>. "
    "Первый символ — буква. Пример: <code>russkiu</code>."
)
PASSWORD_RULES = (
    "Пароль: 8–20 символов. Разрешены латиница, цифры и символы "
    "<code>&amp; ? @ ! # $ _</code>. Пароль должен содержать хотя бы одну букву."
)
CODE_RULES = (
    "Проверочный код: от 5 до 10 цифр. Пример: <code>3228156</code>."
)

_PBKDF2_ITERATIONS = 120_000
_MAX_LOGIN_FAILS = 10
_LOGIN_FAIL_WINDOW = 900  # 15 минут

# ---------------------------------------------------------------------------
# Кэш и ContextVar
# ---------------------------------------------------------------------------

_POS_TTL = 300
_NEG_TTL = 900

_CACHE: dict[int, tuple[float, Optional["Identity"]]] = {}

_ACTUAL_USER: ContextVar[Optional[User]] = ContextVar(
    "accounts_actual_user", default=None
)


@dataclass(frozen=True)
class Identity:
    tg_id: int
    account_id: int
    login: str
    owner_id: int
    is_primary: bool
    tg_username: str
    tg_first_name: str
    owner_username: str
    owner_first_name: str


def actual_user(update: Optional[Update] = None) -> Optional[User]:
    """Реальный Telegram-пользователь (до подмены effective_user)."""
    user = _ACTUAL_USER.get()
    if user is not None:
        return user
    return update.effective_user if update is not None else None


def _now() -> float:
    return time.monotonic()


def _cache_get(tg_id: int) -> tuple[bool, Optional[Identity]]:
    item = _CACHE.get(int(tg_id))
    if not item:
        return False, None
    expires, value = item
    if expires <= _now():
        _CACHE.pop(int(tg_id), None)
        return False, None
    return True, value


def _cache_put(tg_id: int, value: Optional[Identity]) -> None:
    ttl = _POS_TTL if value is not None else _NEG_TTL
    _CACHE[int(tg_id)] = (_now() + ttl, value)


def invalidate(tg_id: Optional[int] = None) -> None:
    if tg_id is None:
        _CACHE.clear()
    else:
        _CACHE.pop(int(tg_id), None)


# ---------------------------------------------------------------------------
# Хеширование
# ---------------------------------------------------------------------------


def _new_salt() -> str:
    return secrets.token_hex(16)


def _hash_secret(secret: str, salt_hex: str) -> str:
    return hashlib.pbkdf2_hmac(
        "sha256",
        secret.encode("utf-8"),
        bytes.fromhex(salt_hex),
        _PBKDF2_ITERATIONS,
    ).hex()


def _verify_secret(secret: str, salt_hex: str, expected_hex: str) -> bool:
    try:
        return hmac.compare_digest(
            _hash_secret(secret, salt_hex),
            str(expected_hex or ""),
        )
    except Exception:
        return False


# ---------------------------------------------------------------------------
# SQL / инициализация
# ---------------------------------------------------------------------------


async def init_db() -> None:
    statements = [
        """
        CREATE TABLE IF NOT EXISTS accounts (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            owner_tg_id    INTEGER NOT NULL UNIQUE,
            login          TEXT NOT NULL,
            login_lc       TEXT NOT NULL UNIQUE,
            password_hash  TEXT NOT NULL,
            password_salt  TEXT NOT NULL,
            code_hash      TEXT NOT NULL,
            code_salt      TEXT NOT NULL,
            created_at     TEXT DEFAULT (datetime('now')),
            updated_at     TEXT DEFAULT (datetime('now')),
            synced_at      TEXT
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS account_sessions (
            tg_id       INTEGER PRIMARY KEY,
            account_id  INTEGER NOT NULL,
            username    TEXT,
            first_name  TEXT,
            is_primary  INTEGER NOT NULL DEFAULT 0,
            created_at  TEXT DEFAULT (datetime('now')),
            last_seen   TEXT
        )
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_account_sessions_account
        ON account_sessions(account_id)
        """,
    ]

    for sql in statements:
        rs = await db._exec(sql)
        if getattr(rs, "error", False):
            raise RuntimeError("Не удалось создать таблицы системы аккаунтов")

    logger.info("Система аккаунтов: таблицы готовы")


async def _fetch_identity(tg_id: int) -> Optional[Identity]:
    rs = await db._exec(
        """SELECT s.account_id, s.is_primary, COALESCE(s.username, ''),
                  COALESCE(s.first_name, ''), a.owner_tg_id, a.login,
                  COALESCE(u.username, ''), COALESCE(u.first_name, '')
           FROM account_sessions s
           JOIN accounts a ON a.id = s.account_id
           LEFT JOIN users u ON u.user_id = a.owner_tg_id
           WHERE s.tg_id = ?
           LIMIT 1""",
        [int(tg_id)],
    )
    if getattr(rs, "error", False):
        return None
    if not rs.rows:
        return None
    row = rs.rows[0]
    return Identity(
        tg_id=int(tg_id),
        account_id=int(row[0]),
        is_primary=bool(row[1]),
        tg_username=str(row[2] or ""),
        tg_first_name=str(row[3] or ""),
        owner_id=int(row[4]),
        login=str(row[5] or ""),
        owner_username=str(row[6] or ""),
        owner_first_name=str(row[7] or ""),
    )


async def get_identity(tg_id: int) -> Optional[Identity]:
    hit, value = _cache_get(int(tg_id))
    if hit:
        return value

    value = await _fetch_identity(int(tg_id))
    if value is not None:
        _cache_put(int(tg_id), value)
        return value

    # Не кэшируем ошибку БД: _fetch_identity вернул None и при ошибке тоже.
    # Для отрицательного результата делаем отдельный быстрый запрос-проверку.
    rs = await db._exec(
        "SELECT 1 FROM account_sessions WHERE tg_id = ? LIMIT 1",
        [int(tg_id)],
    )
    if getattr(rs, "error", False):
        return None
    if not rs.rows:
        _cache_put(int(tg_id), None)
    return None


async def data_id_for(tg_id: int) -> int:
    """ID, под которым хранятся данные пользователя."""
    ident = await get_identity(int(tg_id))
    return ident.owner_id if ident else int(tg_id)


async def synthetic_user(user: User) -> User:
    """Возвращает пользователя с ID владельца аккаунта (для платежей и т.п.)."""
    if user is None:
        return user
    ident = await get_identity(int(user.id))
    if ident is None or ident.owner_id == int(user.id):
        return user
    try:
        return User(
            id=ident.owner_id,
            is_bot=False,
            first_name=ident.owner_first_name or user.first_name or "",
            last_name=user.last_name,
            username=ident.owner_username or user.username,
        )
    except Exception:
        return user


async def get_account_by_owner(owner_tg_id: int) -> Optional[dict]:
    rs = await db._exec(
        """SELECT id, owner_tg_id, login, created_at, updated_at, synced_at
           FROM accounts WHERE owner_tg_id = ? LIMIT 1""",
        [int(owner_tg_id)],
    )
    if getattr(rs, "error", False) or not rs.rows:
        return None
    row = rs.rows[0]
    return {
        "id": int(row[0]),
        "owner_tg_id": int(row[1]),
        "login": str(row[2] or ""),
        "created_at": row[3],
        "updated_at": row[4],
        "synced_at": row[5],
    }


async def set_registration_date(owner_tg_id: int, date_iso: str) -> bool:
    """Меняет дату создания аккаунта; для старых профилей без аккаунта — дату users."""
    owner_tg_id = int(owner_tg_id)
    rs = await db._exec(
        "UPDATE accounts SET created_at = ?, updated_at = datetime('now') WHERE owner_tg_id = ?",
        [f"{date_iso} 00:00:00", owner_tg_id],
    )
    if getattr(rs, "error", False):
        return False
    if (getattr(rs, "rows_affected", 0) or 0) > 0:
        return True

    rs = await db._exec(
        "UPDATE users SET created_at = ? WHERE user_id = ?",
        [f"{date_iso} 00:00:00", owner_tg_id],
    )
    return not getattr(rs, "error", False) and (getattr(rs, "rows_affected", 0) or 0) > 0


async def get_registration_date(owner_tg_id: int) -> Optional[str]:
    """Возвращает дату регистрации аккаунта, а для старых профилей — users.created_at."""
    rs = await db._exec(
        "SELECT created_at FROM accounts WHERE owner_tg_id = ? LIMIT 1",
        [int(owner_tg_id)],
    )
    if getattr(rs, "error", False):
        return None
    if rs.rows:
        return str(rs.rows[0][0] or "") or None

    rs = await db._exec(
        "SELECT created_at FROM users WHERE user_id = ? LIMIT 1",
        [int(owner_tg_id)],
    )
    if getattr(rs, "error", False) or not rs.rows:
        return None
    return str(rs.rows[0][0] or "") or None



async def get_login_for_owner(owner_tg_id: int) -> Optional[str]:
    acc = await get_account_by_owner(owner_tg_id)
    return acc["login"] if acc else None


async def _fetch_account_by_login(login: str) -> Optional[dict]:
    rs = await db._exec(
        """SELECT id, owner_tg_id, login, login_lc, password_hash,
                  password_salt, code_hash, code_salt
           FROM accounts WHERE login_lc = ? LIMIT 1""",
        [str(login or "").strip().lower()],
    )
    if getattr(rs, "error", False) or not rs.rows:
        return None
    row = rs.rows[0]
    return {
        "id": int(row[0]),
        "owner_tg_id": int(row[1]),
        "login": str(row[2] or ""),
        "login_lc": str(row[3] or ""),
        "password_hash": str(row[4] or ""),
        "password_salt": str(row[5] or ""),
        "code_hash": str(row[6] or ""),
        "code_salt": str(row[7] or ""),
    }


async def _fetch_account_by_id(account_id: int) -> Optional[dict]:
    rs = await db._exec(
        """SELECT id, owner_tg_id, login, login_lc, password_hash,
                  password_salt, code_hash, code_salt
           FROM accounts WHERE id = ? LIMIT 1""",
        [int(account_id)],
    )
    if getattr(rs, "error", False) or not rs.rows:
        return None
    row = rs.rows[0]
    return {
        "id": int(row[0]),
        "owner_tg_id": int(row[1]),
        "login": str(row[2] or ""),
        "login_lc": str(row[3] or ""),
        "password_hash": str(row[4] or ""),
        "password_salt": str(row[5] or ""),
        "code_hash": str(row[6] or ""),
        "code_salt": str(row[7] or ""),
    }


async def _session_for_tg(tg_id: int) -> Optional[int]:
    rs = await db._exec(
        "SELECT account_id FROM account_sessions WHERE tg_id = ? LIMIT 1",
        [int(tg_id)],
    )
    if getattr(rs, "error", False) or not rs.rows:
        return None
    return int(rs.rows[0][0])


async def _ensure_user_row(owner_id: int, username: str, first_name: str) -> None:
    try:
        row = await db.get_user(int(owner_id))
        if row is None:
            await db.upsert_user(int(owner_id), username or None, first_name or "")
    except Exception:
        logger.exception("accounts: не удалось создать users-строку owner=%s", owner_id)


# ---------------------------------------------------------------------------
# Регистрация / вход / изменения
# ---------------------------------------------------------------------------


async def create_account(
    tg_id: int,
    login: str,
    password: str,
    code: str,
    username: str,
    first_name: str,
) -> tuple[bool, str]:
    tg_id = int(tg_id)
    login = login.strip()
    login_lc = login.lower()

    # Один Telegram = один аккаунт.
    owner_rs = await db._exec(
        "SELECT id FROM accounts WHERE owner_tg_id = ? LIMIT 1", [tg_id]
    )
    if getattr(owner_rs, "error", False):
        return False, "⚠️ Ошибка базы данных. Попробуйте позже."
    if owner_rs.rows:
        return False, (
            "У этого Telegram уже есть аккаунт. Войдите в него или удалите "
            "через <b>-Аккаунт</b>."
        )

    session_rs = await db._exec(
        "SELECT account_id FROM account_sessions WHERE tg_id = ? LIMIT 1", [tg_id]
    )
    if getattr(session_rs, "error", False):
        return False, "⚠️ Ошибка базы данных. Попробуйте позже."
    if session_rs.rows:
        return False, (
            "Этот Telegram уже привязан к аккаунту. Сначала выйдите из него "
            "в меню <b>-Аккаунт</b>."
        )

    taken = await db._exec(
        "SELECT id FROM accounts WHERE login_lc = ? LIMIT 1", [login_lc]
    )
    if getattr(taken, "error", False):
        return False, "⚠️ Ошибка базы данных. Попробуйте позже."
    if taken.rows:
        return False, "❌ Этот логин уже занят. Придумайте другой."

    password_salt = _new_salt()
    code_salt = _new_salt()

    ins = await db._exec(
        """INSERT INTO accounts
               (owner_tg_id, login, login_lc, password_hash, password_salt,
                code_hash, code_salt, synced_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'))""",
        [
            tg_id,
            login,
            login_lc,
            _hash_secret(password, password_salt),
            password_salt,
            _hash_secret(code, code_salt),
            code_salt,
        ],
    )
    if getattr(ins, "error", False):
        return False, "⚠️ Не удалось сохранить аккаунт. Попробуйте позже."

    acc_rs = await db._exec(
        "SELECT id FROM accounts WHERE login_lc = ? LIMIT 1", [login_lc]
    )
    if getattr(acc_rs, "error", False) or not acc_rs.rows:
        return False, "⚠️ Не удалось сохранить аккаунт. Попробуйте позже."
    account_id = int(acc_rs.rows[0][0])

    sess = await db._exec(
        """INSERT INTO account_sessions
               (tg_id, account_id, username, first_name, is_primary, last_seen)
           VALUES (?, ?, ?, ?, 1, datetime('now'))""",
        [tg_id, account_id, username or "", first_name or ""],
    )
    if getattr(sess, "error", False):
        # Откатываем «осиротевший» аккаунт.
        await db._exec("DELETE FROM accounts WHERE id = ?", [account_id])
        return False, "⚠️ Не удалось привязать Telegram к аккаунту. Попробуйте позже."

    await _ensure_user_row(tg_id, username, first_name)
    invalidate(tg_id)
    logger.info("accounts: создан аккаунт login=%s owner=%s", login, tg_id)
    return True, (
        "✅ <b>Аккаунт создан!</b>\n\n"
        f"🔑 Логин: <code>{escape(login)}</code>\n"
        "🔢 Проверочный код сохранён.\n\n"
        "Все данные, которые были привязаны к вашему Telegram ID, теперь "
        "привязаны к аккаунту. Повторная синхронизация не выполняется."
    )


async def _bind_session(
    tg_id: int,
    account_id: int,
    is_primary: bool,
    username: str,
    first_name: str,
) -> tuple[bool, str]:
    tg_id = int(tg_id)
    existing = await _session_for_tg(tg_id)
    if existing is not None:
        if int(existing) == int(account_id):
            invalidate(tg_id)
            return True, "already"
        return False, (
            "Этот Telegram уже привязан к другому аккаунту. Сначала выйдите "
            "из него через <b>-Аккаунт</b>."
        )

    rs = await db._exec(
        """INSERT INTO account_sessions
               (tg_id, account_id, username, first_name, is_primary, last_seen)
           VALUES (?, ?, ?, ?, ?, datetime('now'))""",
        [tg_id, int(account_id), username or "", first_name or "", 1 if is_primary else 0],
    )
    if getattr(rs, "error", False):
        return False, "⚠️ Не удалось создать сессию. Попробуйте позже."

    invalidate(tg_id)
    return True, "ok"


async def login_account(
    tg_id: int,
    login: str,
    password: str,
    code: str,
    username: str,
    first_name: str,
) -> tuple[bool, str, str]:
    """Вход. Возвращает (успех, сообщение, стадия ошибки)."""
    acc = await _fetch_account_by_login(login)
    if acc is None:
        return False, "❌ Неверный логин или пароль.", "credentials"
    if not _verify_secret(password, acc["password_salt"], acc["password_hash"]):
        return False, "❌ Неверный логин или пароль.", "credentials"
    if not _verify_secret(code, acc["code_salt"], acc["code_hash"]):
        return False, "❌ Неверный проверочный код.", "code"

    ok, msg = await _bind_session(
        tg_id,
        acc["id"],
        int(acc["owner_tg_id"]) == int(tg_id),
        username,
        first_name,
    )
    if not ok:
        return False, msg, "session"
    if msg == "already":
        return True, (
            "ℹ️ Вы уже вошли в этот аккаунт.\n\n"
            f"🔑 Логин: <code>{escape(acc['login'])}</code>"
        ), ""

    await _ensure_user_row(int(acc["owner_tg_id"]), username, first_name)
    logger.info(
        "accounts: вход login=%s tg=%s owner=%s",
        acc["login"], tg_id, acc["owner_tg_id"],
    )
    return True, (
        "✅ <b>Вход выполнен!</b>\n\n"
        f"🔑 Логин: <code>{escape(acc['login'])}</code>\n"
        "Теперь в группе вы пишете от имени аккаунта. "
        "Синхронизация при входе не выполняется."
    ), ""


async def verify_account_password(account_id: int, password: str) -> bool:
    acc = await _fetch_account_by_id(account_id)
    if acc is None:
        return False
    return _verify_secret(password, acc["password_salt"], acc["password_hash"])


async def verify_account_code(account_id: int, code: str) -> bool:
    acc = await _fetch_account_by_id(account_id)
    if acc is None:
        return False
    return _verify_secret(code, acc["code_salt"], acc["code_hash"])


async def change_login(account_id: int, new_login: str) -> tuple[bool, str]:
    new_login = new_login.strip()
    taken = await db._exec(
        "SELECT id FROM accounts WHERE login_lc = ? AND id != ? LIMIT 1",
        [new_login.lower(), int(account_id)],
    )
    if getattr(taken, "error", False):
        return False, "⚠️ Ошибка базы данных. Попробуйте позже."
    if taken.rows:
        return False, "❌ Этот логин уже занят."

    rs = await db._exec(
        """UPDATE accounts
           SET login = ?, login_lc = ?, updated_at = datetime('now')
           WHERE id = ?""",
        [new_login, new_login.lower(), int(account_id)],
    )
    if getattr(rs, "error", False):
        return False, "⚠️ Не удалось изменить логин."
    invalidate()
    return True, f"✅ Логин изменён на <code>{escape(new_login)}</code>."


async def change_password(account_id: int, new_password: str) -> bool:
    salt = _new_salt()
    rs = await db._exec(
        """UPDATE accounts
           SET password_hash = ?, password_salt = ?, updated_at = datetime('now')
           WHERE id = ?""",
        [_hash_secret(new_password, salt), salt, int(account_id)],
    )
    invalidate()
    return not getattr(rs, "error", False)


async def change_code(account_id: int, new_code: str) -> bool:
    salt = _new_salt()
    rs = await db._exec(
        """UPDATE accounts
           SET code_hash = ?, code_salt = ?, updated_at = datetime('now')
           WHERE id = ?""",
        [_hash_secret(new_code, salt), salt, int(account_id)],
    )
    invalidate()
    return not getattr(rs, "error", False)


async def logout_session(tg_id: int) -> bool:
    rs = await db._exec(
        "DELETE FROM account_sessions WHERE tg_id = ?", [int(tg_id)]
    )
    invalidate(int(tg_id))
    return not getattr(rs, "error", False)


async def delete_account(account_id: int) -> bool:
    """Удаляет аккаунт, все его сессии и данные владельца."""
    acc = await _fetch_account_by_id(account_id)
    if acc is None:
        return False

    sess_rs = await db._exec(
        "SELECT tg_id FROM account_sessions WHERE account_id = ?", [int(account_id)]
    )
    session_ids = [int(r[0]) for r in (sess_rs.rows or [])]

    owner_id = int(acc["owner_tg_id"])

    await db._exec(
        "DELETE FROM account_sessions WHERE account_id = ?", [int(account_id)]
    )
    rs = await db._exec(
        "DELETE FROM accounts WHERE id = ?", [int(account_id)]
    )
    if getattr(rs, "error", False):
        return False

    await _wipe_user_data(owner_id)

    invalidate()
    for tg_id in session_ids:
        invalidate(tg_id)
    invalidate(owner_id)
    logger.info("accounts: удалён аккаунт login=%s owner=%s", acc["login"], owner_id)
    return True


async def _wipe_user_data(user_id: int) -> None:
    """Best-effort удаление данных владельца аккаунта.

    Ошибки отдельных таблиц не мешают удалению аккаунта: главное — закрыть
    доступ. Но всё, что удалось удалить, действительно пропадает.
    """
    statements: list[tuple[str, list]] = [
        ("DELETE FROM user_cosmetics WHERE user_id = ?", [user_id]),
        ("DELETE FROM coin_holders WHERE user_id = ?", [user_id]),
        ("DELETE FROM coin_market_lots WHERE seller_id = ?", [user_id]),
        ("DELETE FROM coin_income_log WHERE user_id = ?", [user_id]),
        ("DELETE FROM coin_purchases WHERE buyer_id = ?", [user_id]),
        ("DELETE FROM premium_subscriptions WHERE user_id = ?", [user_id]),
        ("DELETE FROM shop_purchases WHERE user_id = ?", [user_id]),
        ("DELETE FROM reputation_votes WHERE voter_id = ? OR target_id = ?",
         [user_id, user_id]),
        ("DELETE FROM reputation_cooldowns WHERE user_id = ?", [user_id]),
        ("DELETE FROM aura WHERE user_id = ?", [user_id]),
        ("DELETE FROM awards WHERE user_id = ?", [user_id]),
        ("DELETE FROM action_stats WHERE user_id = ?", [user_id]),
        ("DELETE FROM pending_salary WHERE user_id = ?", [user_id]),
        ("DELETE FROM messages WHERE user_id = ?", [user_id]),
        ("DELETE FROM messages_day WHERE user_id = ?", [user_id]),
        ("DELETE FROM subscriptions WHERE user_id = ?", [user_id]),
        ("DELETE FROM marriage_divorce_resets WHERE user_id = ?", [user_id]),
        ("UPDATE marriages SET active = 0 WHERE user1_id = ? OR user2_id = ?",
         [user_id, user_id]),
        ("DELETE FROM users WHERE user_id = ?", [user_id]),
    ]

    for sql, params in statements:
        try:
            rs = await db._exec(sql, params)
            if getattr(rs, "error", False):
                logger.warning("accounts wipe: ошибка SQL: %.80s", sql)
        except Exception:
            logger.exception("accounts wipe: исключение: %.80s", sql)


# ---------------------------------------------------------------------------
# Список сессий
# ---------------------------------------------------------------------------


async def list_sessions(account_id: int) -> list[dict]:
    rs = await db._exec(
        """SELECT tg_id, username, first_name, is_primary, created_at, last_seen
           FROM account_sessions
           WHERE account_id = ?
           ORDER BY is_primary DESC, created_at ASC""",
        [int(account_id)],
    )
    if getattr(rs, "error", False):
        return []
    keys = ["tg_id", "username", "first_name", "is_primary", "created_at", "last_seen"]
    result = []
    for row in rs.rows:
        item = dict(zip(keys, row))
        item["tg_id"] = int(item["tg_id"])
        item["is_primary"] = bool(item["is_primary"])
        result.append(item)
    return result


async def _session_info(tg_id: int) -> Optional[dict]:
    rs = await db._exec(
        """SELECT tg_id, account_id, username, first_name, is_primary,
                  created_at, last_seen
           FROM account_sessions WHERE tg_id = ? LIMIT 1""",
        [int(tg_id)],
    )
    if getattr(rs, "error", False) or not rs.rows:
        return None
    row = rs.rows[0]
    keys = ["tg_id", "account_id", "username", "first_name",
            "is_primary", "created_at", "last_seen"]
    item = dict(zip(keys, row))
    item["tg_id"] = int(item["tg_id"])
    item["account_id"] = int(item["account_id"])
    item["is_primary"] = bool(item["is_primary"])
    return item


# ---------------------------------------------------------------------------
# Анти-брутфорс
# ---------------------------------------------------------------------------

_FAILS: dict[int, list[float]] = {}


def _rate_limited(tg_id: int) -> bool:
    now = time.time()
    fails = [t for t in _FAILS.get(int(tg_id), []) if now - t < _LOGIN_FAIL_WINDOW]
    _FAILS[int(tg_id)] = fails
    return len(fails) >= _MAX_LOGIN_FAILS


def _add_fail(tg_id: int) -> None:
    now = time.time()
    fails = [t for t in _FAILS.get(int(tg_id), []) if now - t < _LOGIN_FAIL_WINDOW]
    fails.append(now)
    _FAILS[int(tg_id)] = fails


def _reset_fails(tg_id: int) -> None:
    _FAILS.pop(int(tg_id), None)


# ---------------------------------------------------------------------------
# Клавиатуры и отображение
# ---------------------------------------------------------------------------


def _btn(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text, callback_data=data)


def _user_label(username: str, first_name: str, tg_id: int) -> str:
    if username:
        return f"@{escape(username)}"
    if first_name:
        return escape(first_name)
    return f"ID {tg_id}"


def _user_label_plain(username: str, first_name: str, tg_id: int) -> str:
    """Подпись для кнопки: Telegram не разбирает HTML в тексте кнопок."""
    if username:
        return f"@{username}"
    if first_name:
        return first_name
    return f"ID {tg_id}"


def _menu_keyboard(ident: Optional[Identity], delete_only: bool = False) -> InlineKeyboardMarkup:
    if delete_only:
        if ident:
            return InlineKeyboardMarkup([
                [_btn("🚪 Выйти из аккаунта", "acc:logout")],
                [_btn("🗑 Удалить аккаунт", "acc:delete")],
                [_btn("← Назад", "acc:menu")],
            ])
        return InlineKeyboardMarkup([
            [_btn("🔑 Войти", "acc:login")],
            [_btn("← Назад", "acc:menu")],
        ])

    if ident:
        return InlineKeyboardMarkup([
            [_btn("🗂 Сессии", "acc:sessions")],
            [_btn("✏️ Сменить логин", "acc:chg:login"),
             _btn("🔒 Сменить пароль", "acc:chg:pass")],
            [_btn("🔢 Сменить код", "acc:chg:code")],
            [_btn("🚪 Выйти", "acc:logout"),
             _btn("🗑 Удалить аккаунт", "acc:delete")],
        ])

    return InlineKeyboardMarkup([
        [_btn("➕ Создать аккаунт", "acc:create"),
         _btn("🔑 Войти", "acc:login")],
    ])


async def _menu_text(tg_id: int, ident: Optional[Identity]) -> str:
    head = "🔐 <b>АККАУНТ</b>\n━━━━━━━━━━━━━━━━━━\n"
    if ident:
        primary = "⭐ Основная сессия" if ident.is_primary else "📎 Дополнительная сессия"
        return (
            head
            + f"👤 Логин: <code>{escape(ident.login)}</code>\n"
            + f"📱 Telegram: {_user_label(ident.tg_username, ident.tg_first_name, tg_id)} "
            + f"(<code>{tg_id}</code>)\n"
            + f"{primary}\n"
            + "━━━━━━━━━━━━━━━━━━\n"
            + "Выберите действие:"
        )

    owner = await get_account_by_owner(tg_id)
    if owner:
        return (
            head
            + "У этого Telegram уже есть аккаунт, но вы не вошли в него.\n\n"
            + f"🔑 Логин аккаунта: <code>{escape(owner['login'])}</code>\n\n"
            + "Нажмите «Войти» и введите пароль с проверочным кодом."
        )
    return (
        head
        + "Вы не вошли в аккаунт.\n\n"
        + "➕ Создайте новый аккаунт — все ваши текущие данные "
        + "автоматически станут данными аккаунта.\n"
        + "🔑 Или войдите в существующий аккаунт с другого Telegram."
    )


async def _show_menu(update: Update, context: ContextTypes.DEFAULT_TYPE,
                     delete_only: bool = False) -> None:
    user = update.effective_user
    if not user:
        return
    ident = await get_identity(user.id)
    text = await _menu_text(user.id, ident)
    if delete_only and ident:
        text += (
            "\n\n━━━━━━━━━━━━━━━━━━\n"
            "🚪 <b>Выйти</b> — просто отвязать этот Telegram, данные сохранятся.\n"
            "🗑 <b>Удалить аккаунт</b> — удалить аккаунт и все данные навсегда."
        )
    await _respond(update, text, _menu_keyboard(ident, delete_only=delete_only))


async def _respond(update: Update, text: str, keyboard=None) -> None:
    query = update.callback_query
    if query is not None:
        try:
            await query.edit_message_text(
                text, parse_mode=ParseMode.HTML, reply_markup=keyboard
            )
            return
        except TelegramError:
            pass
        if query.message:
            try:
                await query.message.reply_text(
                    text, parse_mode=ParseMode.HTML, reply_markup=keyboard
                )
                return
            except TelegramError:
                logger.exception("accounts: не удалось отправить ответ")
        return

    msg = update.effective_message
    if msg:
        try:
            await msg.reply_text(
                text, parse_mode=ParseMode.HTML, reply_markup=keyboard
            )
        except TelegramError:
            logger.exception("accounts: не удалось отправить сообщение")


# ---------------------------------------------------------------------------
# Машина состояний
# ---------------------------------------------------------------------------


def _set_state(context: ContextTypes.DEFAULT_TYPE, **values) -> None:
    context.user_data["acc_state"] = values


def _get_state(context: ContextTypes.DEFAULT_TYPE) -> Optional[dict]:
    return context.user_data.get("acc_state")


def _clear_state(context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.pop("acc_state", None)
    context.user_data.pop("acc_tmp_password", None)


async def _ask(update: Update, context: ContextTypes.DEFAULT_TYPE,
               flow: str, step: str, text: str, **extra) -> None:
    context.user_data["acc_state"] = {"flow": flow, "step": step, **extra}
    await _respond(update, text, InlineKeyboardMarkup([[_btn("✖ Отмена", "acc:menu")]]))


async def _process_state(update: Update, context: ContextTypes.DEFAULT_TYPE,
                         state: dict, text: str) -> None:
    user = update.effective_user
    msg = update.effective_message
    if not user or not msg:
        return
    tg_id = int(user.id)
    flow = state.get("flow")
    step = state.get("step")
    text = (text or "").strip()

    # --- регистрация ------------------------------------------------------
    if flow == "reg":
        if step == "login":
            if not LOGIN_RE.match(text):
                await _respond(
                    update,
                    "❌ Некорректный логин.\n\n" + LOGIN_RULES
                    + "\n\nПопробуйте ещё раз:",
                    InlineKeyboardMarkup([[_btn("✖ Отмена", "acc:menu")]]),
                )
                return
            taken = await db._exec(
                "SELECT 1 FROM accounts WHERE login_lc = ? LIMIT 1", [text.lower()]
            )
            if getattr(taken, "error", False):
                await _respond(update, "⚠️ Ошибка базы. Попробуйте позже.")
                return
            if taken.rows:
                await _respond(
                    update,
                    f"❌ Логин <code>{escape(text)}</code> уже занят. "
                    "Придумайте другой:",
                    InlineKeyboardMarkup([[_btn("✖ Отмена", "acc:menu")]]),
                )
                return
            await _ask(
                update, context, "reg", "password",
                f"✅ Логин <code>{escape(text)}</code> свободен.\n\n"
                "Придумайте пароль.\n\n" + PASSWORD_RULES,
                login=text,
            )
            return

        if step == "password":
            if not PASSWORD_RE.match(text):
                await _respond(
                    update,
                    "❌ Пароль не подходит.\n\n" + PASSWORD_RULES
                    + "\n\nПридумайте пароль ещё раз:",
                    InlineKeyboardMarkup([[_btn("✖ Отмена", "acc:menu")]]),
                )
                return
            # Пароль не храним в состоянии — только после ввода кода.
            context.user_data["acc_tmp_password"] = text
            await _ask(
                update, context, "reg", "code",
                "Придумайте проверочный код.\n\n" + CODE_RULES
                + "\n\n<i>Он понадобится для входа в аккаунт и удаления.</i>",
                login=state.get("login"),
            )
            return

        if step == "code":
            if not CODE_RE.match(text):
                await _respond(
                    update,
                    "❌ Код должен состоять только из цифр (5–10).\n\n"
                    + CODE_RULES + "\n\nВведите код ещё раз:",
                    InlineKeyboardMarkup([[_btn("✖ Отмена", "acc:menu")]]),
                )
                return
            password = context.user_data.pop("acc_tmp_password", "")
            login = state.get("login") or ""
            if not password or not login:
                _clear_state(context)
                await _respond(update, "⚠️ Сессия создания сброшена. Начните заново.")
                return
            ok, result = await create_account(
                tg_id, login, password, text,
                user.username or "", user.first_name or "",
            )
            _clear_state(context)
            if not ok:
                await _respond(update, result)
                return
            await _respond(
                update, result,
                _menu_keyboard(await get_identity(tg_id)),
            )
            return

    # --- вход -------------------------------------------------------------
    if flow == "login":
        if step == "login":
            if not text:
                await _respond(update, "Введите логин:")
                return
            await _ask(
                update, context, "login", "password",
                f"🔑 Логин: <code>{escape(text)}</code>\n\nВведите пароль:",
                login=text,
            )
            return

        if step == "password":
            if _rate_limited(tg_id):
                _clear_state(context)
                await _respond(
                    update,
                    "⛔ Слишком много неудачных попыток. Попробуйте через 15 минут.",
                )
                return
            acc = await _fetch_account_by_login(state.get("login") or "")
            if acc is None or not _verify_secret(
                text, acc["password_salt"], acc["password_hash"]
            ):
                _add_fail(tg_id)
                _clear_state(context)
                await _respond(
                    update,
                    "❌ Неверный логин или пароль.\n\n"
                    "Нажмите «Войти» и попробуйте снова.",
                    InlineKeyboardMarkup([[_btn("🔑 Войти", "acc:login")]]),
                )
                return
            await _ask(
                update, context, "login", "code",
                "Введите проверочный код:",
                login=state.get("login"), account_id=acc["id"],
            )
            return

        if step == "code":
            if _rate_limited(tg_id):
                _clear_state(context)
                await _respond(
                    update,
                    "⛔ Слишком много неудачных попыток. Попробуйте через 15 минут.",
                )
                return
            account_id = int(state.get("account_id") or 0)
            if not await verify_account_code(account_id, text):
                _add_fail(tg_id)
                _clear_state(context)
                await _respond(
                    update,
                    "❌ Неверный проверочный код.\n\n"
                    "Нажмите «Войти» и попробуйте снова.",
                    InlineKeyboardMarkup([[_btn("🔑 Войти", "acc:login")]]),
                )
                return
            acc = await _fetch_account_by_id(account_id)
            _clear_state(context)
            if acc is None:
                await _respond(update, "⚠️ Аккаунт не найден.")
                return
            ok, result = await _bind_session(
                tg_id, account_id,
                int(acc["owner_tg_id"]) == tg_id,
                user.username or "", user.first_name or "",
            )
            if not ok:
                await _respond(update, result)
                return
            _reset_fails(tg_id)
            await _ensure_user_row(
                int(acc["owner_tg_id"]), user.username or "", user.first_name or ""
            )
            logger.info(
                "accounts: вход login=%s tg=%s owner=%s",
                acc["login"], tg_id, acc["owner_tg_id"],
            )
            text_done = (
                "✅ <b>Вход выполнен!</b>\n\n"
                f"🔑 Логин: <code>{escape(acc['login'])}</code>\n"
                "Теперь в группе вы пишете от имени аккаунта. "
                "Синхронизация при входе не выполняется."
            )
            await _respond(update, text_done, _menu_keyboard(await get_identity(tg_id)))
            return

    ident = await get_identity(tg_id)
    if ident is None:
        _clear_state(context)
        await _respond(update, "❌ Вы не вошли в аккаунт.")
        return

    # --- смена логина -----------------------------------------------------
    if flow == "chg_login":
        if step == "password":
            if not await verify_account_password(ident.account_id, text):
                _clear_state(context)
                await _respond(update, "❌ Неверный пароль.", _menu_keyboard(ident))
                return
            await _ask(
                update, context, "chg_login", "new_login",
                "Введите новый логин.\n\n" + LOGIN_RULES,
                account_id=ident.account_id,
            )
            return
        if step == "new_login":
            if not LOGIN_RE.match(text):
                await _respond(
                    update, "❌ Некорректный логин.\n\n" + LOGIN_RULES,
                    InlineKeyboardMarkup([[_btn("✖ Отмена", "acc:menu")]]),
                )
                return
            ok, result = await change_login(ident.account_id, text)
            _clear_state(context)
            await _respond(update, result, _menu_keyboard(await get_identity(tg_id)))
            return

    # --- смена пароля -----------------------------------------------------
    if flow == "chg_pass":
        if step == "password":
            if not await verify_account_password(ident.account_id, text):
                _clear_state(context)
                await _respond(update, "❌ Неверный пароль.", _menu_keyboard(ident))
                return
            await _ask(
                update, context, "chg_pass", "new_pass",
                "Придумайте новый пароль.\n\n" + PASSWORD_RULES,
                account_id=ident.account_id,
            )
            return
        if step == "new_pass":
            if not PASSWORD_RE.match(text):
                await _respond(
                    update, "❌ Пароль не подходит.\n\n" + PASSWORD_RULES,
                    InlineKeyboardMarkup([[_btn("✖ Отмена", "acc:menu")]]),
                )
                return
            ok = await change_password(ident.account_id, text)
            _clear_state(context)
            if ok:
                await _respond(
                    update, "✅ Пароль изменён.",
                    _menu_keyboard(await get_identity(tg_id)),
                )
            else:
                await _respond(update, "⚠️ Не удалось изменить пароль.")
            return

    # --- смена кода -------------------------------------------------------
    if flow == "chg_code":
        if step == "password":
            if not await verify_account_password(ident.account_id, text):
                _clear_state(context)
                await _respond(update, "❌ Неверный пароль.", _menu_keyboard(ident))
                return
            await _ask(
                update, context, "chg_code", "new_code",
                "Придумайте новый проверочный код.\n\n" + CODE_RULES,
                account_id=ident.account_id,
            )
            return
        if step == "new_code":
            if not CODE_RE.match(text):
                await _respond(
                    update, "❌ Код должен состоять только из цифр (5–10).\n\n"
                    + CODE_RULES,
                    InlineKeyboardMarkup([[_btn("✖ Отмена", "acc:menu")]]),
                )
                return
            ok = await change_code(ident.account_id, text)
            _clear_state(context)
            if ok:
                await _respond(
                    update, "✅ Проверочный код изменён.",
                    _menu_keyboard(await get_identity(tg_id)),
                )
            else:
                await _respond(update, "⚠️ Не удалось изменить код.")
            return

    # --- удаление ---------------------------------------------------------
    if flow == "delete":
        if step == "password":
            if not await verify_account_password(ident.account_id, text):
                _clear_state(context)
                await _respond(update, "❌ Неверный пароль.", _menu_keyboard(ident))
                return
            _set_state(
                context, flow="delete", step="confirm",
                account_id=ident.account_id,
            )
            await _respond(
                update,
                "⚠️ <b>Вы уверены?</b>\n\n"
                "Аккаунт и все связанные с ним данные будут удалены "
                "<b>безвозвратно</b>: баланс, токены, брак, аура, награды, "
                "покупки и сессии.\n\n"
                "Это действие нельзя отменить.",
                InlineKeyboardMarkup([
                    [_btn("🗑 Да, удалить", "acc:del_confirm")],
                    [_btn("← Отмена", "acc:menu")],
                ]),
            )
            return
        if step == "code":
            if not await verify_account_code(ident.account_id, text):
                await _respond(
                    update,
                    "❌ Неверный проверочный код. Аккаунт не удалён.\n\n"
                    "Попробуйте ещё раз или нажмите «Отмена».",
                    InlineKeyboardMarkup([[_btn("✖ Отмена", "acc:menu")]]),
                )
                return
            ok = await delete_account(ident.account_id)
            _clear_state(context)
            if ok:
                logger.info("accounts: аккаунт удалён пользователем tg=%s", tg_id)
                await _respond(
                    update,
                    "🗑 <b>Аккаунт удалён.</b>\n\n"
                    "Все данные аккаунта утеряны. Вы можете создать новый аккаунт.",
                    _menu_keyboard(None),
                )
            else:
                await _respond(update, "⚠️ Не удалось удалить аккаунт. Попробуйте позже.")
            return

    _clear_state(context)
    await _show_menu(update, context)


# ---------------------------------------------------------------------------
# Callback-кнопки
# ---------------------------------------------------------------------------


async def _render_sessions(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user:
        return
    ident = await get_identity(user.id)
    if ident is None:
        await _respond(
            update, "❌ Вы не вошли в аккаунт.",
            InlineKeyboardMarkup([[_btn("🔑 Войти", "acc:login")]]),
        )
        return

    sessions = await list_sessions(ident.account_id)
    if not sessions:
        await _respond(
            update, "Сессий нет.",
            InlineKeyboardMarkup([[_btn("← Назад", "acc:menu")]]),
        )
        return

    # Текущее устройство — первым, затем основная сессия, затем остальные.
    sessions.sort(
        key=lambda s: (
            0 if s["tg_id"] == user.id else 1,
            0 if s["is_primary"] else 1,
        )
    )

    rows = []
    for s in sessions:
        sid = s["tg_id"]
        if sid == user.id:
            if s["username"]:
                label = f"Я (@{s['username']})"
            elif s["first_name"]:
                label = f"Я ({s['first_name']})"
            else:
                label = f"Я (ID {sid})"
        else:
            label = _user_label_plain(s["username"], s["first_name"], sid)
        rows.append([_btn(label, f"acc:sess:{sid}")])
    rows.append([_btn("← Назад", "acc:menu")])

    await _respond(
        update,
        "🗂 <b>Зарегистрированные устройства</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"Аккаунт: <code>{escape(ident.login)}</code>\n\n"
        "Нажмите на устройство, чтобы посмотреть данные или удалить сессию.",
        InlineKeyboardMarkup(rows),
    )


async def _render_session(update: Update, context: ContextTypes.DEFAULT_TYPE,
                          sid: int) -> None:
    user = update.effective_user
    if not user:
        return
    ident = await get_identity(user.id)
    if ident is None:
        await _respond(update, "❌ Вы не вошли в аккаунт.")
        return
    info = await _session_info(sid)
    if info is None or int(info["account_id"]) != ident.account_id:
        await _respond(
            update, "Сессия не найдена.",
            InlineKeyboardMarkup([[_btn("← Назад", "acc:sessions")]]),
        )
        return

    label = _user_label(info["username"], info["first_name"], sid)
    kind = "⭐ Основная" if info["is_primary"] else "📎 Дополнительная"
    if sid == user.id:
        kind += " · это вы"
    await _respond(
        update,
        "📱 <b>Сессия</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"👤 {label}\n"
        f"🆔 <code>{sid}</code>\n"
        f"Тип: {kind}\n"
        f"Добавлена: <code>{escape(str(info['created_at'] or '—'))}</code>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "Удаление сессии отвязывает этот Telegram от аккаунта. "
        "В группе он больше не будет зарегистрирован под аккаунтом.",
        InlineKeyboardMarkup([
            [_btn("🗑 Удалить сессию", f"acc:sessdel:{sid}")],
            [_btn("← Назад", "acc:sessions")],
        ]),
    )


async def _handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE,
                           data: str) -> None:
    user = update.effective_user
    if not user:
        return
    tg_id = int(user.id)
    parts = data.split(":")
    action = parts[1] if len(parts) > 1 else "menu"

    if action == "menu":
        _clear_state(context)
        await _show_menu(update, context)
        return

    if action == "menu_delete":
        _clear_state(context)
        await _show_menu(update, context, delete_only=True)
        return

    if action == "create":
        _clear_state(context)
        ident = await get_identity(tg_id)
        if ident:
            await _respond(
                update,
                "У этого Telegram уже есть аккаунт. Один Telegram — один аккаунт.\n"
                "Чтобы создать новый, сначала удалите текущий.",
                _menu_keyboard(ident),
            )
            return
        await _ask(
            update, context, "reg", "login",
            "➕ <b>Создание аккаунта</b>\n\nПридумайте логин.\n\n" + LOGIN_RULES,
        )
        return

    if action == "login":
        _clear_state(context)
        ident = await get_identity(tg_id)
        if ident:
            await _respond(
                update,
                f"Вы уже вошли в аккаунт <code>{escape(ident.login)}</code>.",
                _menu_keyboard(ident),
            )
            return
        await _ask(
            update, context, "login", "login",
            "🔑 <b>Вход в аккаунт</b>\n\nВведите логин:",
        )
        return

    if action == "sessions":
        await _render_sessions(update, context)
        return

    if action == "sess" and len(parts) >= 3:
        try:
            sid = int(parts[2])
        except ValueError:
            await _render_sessions(update, context)
            return
        await _render_session(update, context, sid)
        return

    if action == "sessdel" and len(parts) >= 3:
        try:
            sid = int(parts[2])
        except ValueError:
            await _render_sessions(update, context)
            return
        ident = await get_identity(tg_id)
        if ident is None:
            await _respond(update, "❌ Вы не вошли в аккаунт.")
            return
        info = await _session_info(sid)
        if info is None or int(info["account_id"]) != ident.account_id:
            await _respond(update, "Сессия не найдена.")
            return
        ok = await logout_session(sid)
        if not ok:
            await _respond(update, "⚠️ Не удалось удалить сессию.")
            return
        # Инвалидируем кэш для всех сессий аккаунта.
        invalidate()
        if sid == tg_id:
            await _respond(
                update,
                "🚪 Сессия удалена. Вы вышли из аккаунта.\n"
                "Данные аккаунта сохранены — можно войти снова.",
                _menu_keyboard(None),
            )
        else:
            await _respond(
                update,
                f"🗑 Сессия <code>{sid}</code> удалена. "
                "Этот Telegram больше не привязан к аккаунту.",
                _menu_keyboard(await get_identity(tg_id)),
            )
        return

    if action == "logout":
        ident = await get_identity(tg_id)
        if ident is None:
            await _respond(
                update, "Вы не вошли в аккаунт.",
                InlineKeyboardMarkup([[_btn("🔑 Войти", "acc:login")]]),
            )
            return
        ok = await logout_session(tg_id)
        if ok:
            _clear_state(context)
            await _respond(
                update,
                "🚪 Вы вышли из аккаунта.\n\n"
                "Данные аккаунта сохранены. Войти снова: <b>+Аккаунт</b> → «Войти».",
                _menu_keyboard(None),
            )
        else:
            await _respond(update, "⚠️ Не удалось выйти. Попробуйте позже.")
        return

    if action == "chg" and len(parts) >= 3:
        kind = parts[2]
        ident = await get_identity(tg_id)
        if ident is None:
            await _respond(update, "❌ Вы не вошли в аккаунт.")
            return
        if kind == "login":
            await _ask(
                update, context, "chg_login", "password",
                "✏️ <b>Смена логина</b>\n\nВведите текущий пароль:",
                account_id=ident.account_id,
            )
        elif kind == "pass":
            await _ask(
                update, context, "chg_pass", "password",
                "🔒 <b>Смена пароля</b>\n\nВведите текущий пароль:",
                account_id=ident.account_id,
            )
        elif kind == "code":
            await _ask(
                update, context, "chg_code", "password",
                "🔢 <b>Смена проверочного кода</b>\n\nВведите текущий пароль:",
                account_id=ident.account_id,
            )
        else:
            await _show_menu(update, context)
        return

    if action == "delete":
        ident = await get_identity(tg_id)
        if ident is None:
            await _respond(
                update, "❌ Вы не вошли в аккаунт.",
                InlineKeyboardMarkup([[_btn("🔑 Войти", "acc:login")]]),
            )
            return
        await _ask(
            update, context, "delete", "password",
            "🗑 <b>Удаление аккаунта</b>\n\n"
            "Введите пароль от аккаунта.\n\n"
            "<i>После пароля нужно будет подтвердить удаление и ввести "
            "проверочный код.</i>",
            account_id=ident.account_id,
        )
        return

    if action == "del_confirm":
        state = _get_state(context)
        ident = await get_identity(tg_id)
        if ident is None:
            await _respond(update, "❌ Вы не вошли в аккаунт.")
            return
        if not state or state.get("flow") != "delete":
            await _respond(update, "Подтверждение устарело. Начните заново.", _menu_keyboard(ident))
            return
        _set_state(
            context, flow="delete", step="code",
            account_id=ident.account_id,
        )
        await _respond(
            update,
            "Введите проверочный код для окончательного удаления:",
            InlineKeyboardMarkup([[_btn("✖ Отмена", "acc:menu")]]),
        )
        return

    if action == "del_cancel":
        _clear_state(context)
        await _show_menu(update, context, delete_only=True)
        return

    await _show_menu(update, context)


async def account_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    query = update.callback_query
    if not query or not (query.data or "").startswith("acc:"):
        return False

    chat = update.effective_chat
    if not chat or chat.type != "private":
        try:
            await query.answer(
                "Откройте бота в личных сообщениях, чтобы управлять аккаунтом.",
                show_alert=True,
            )
        except TelegramError:
            pass
        return True

    try:
        await query.answer()
    except TelegramError:
        pass

    try:
        await _handle_callback(update, context, query.data or "acc:menu")
    except Exception:
        logger.exception("accounts: ошибка callback %r", query.data)
        try:
            if query.message:
                await query.message.reply_text(
                    "⚠️ Техническая ошибка. Попробуйте ещё раз через минуту."
                )
        except TelegramError:
            pass
    return True


# ---------------------------------------------------------------------------
# Команды в группе
# ---------------------------------------------------------------------------


async def _send_group_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE,
                             delete_only: bool) -> None:
    msg = update.effective_message
    if not msg:
        return

    payload = "account_delete" if delete_only else "account"
    username = getattr(context.bot, "username", None)
    if not username:
        try:
            me = await context.bot.get_me()
            username = me.username
        except TelegramError:
            username = None

    if delete_only:
        text = (
            "🔐 <b>АККАУНТ</b>\n\n"
            "Выход и удаление аккаунта доступны в личных сообщениях бота.\n"
            "Нажмите кнопку ниже — бот откроет меню "
            "«Выйти / Удалить аккаунт»."
        )
        button_text = "➡️ Перейти в бота"
    else:
        text = (
            "🔐 <b>АККАУНТ</b>\n\n"
            "Управление аккаунтом доступно в личных сообщениях бота.\n"
            "Нажмите кнопку ниже — бот откроет меню "
            "«Создать аккаунт / Войти»."
        )
        button_text = "➡️ Перейти в бота"

    keyboard = None
    if username:
        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton(
                button_text,
                url=f"https://t.me/{username}?start={payload}",
            )
        ]])
    else:
        text += "\n\n⚠️ Не удалось определить имя бота. Откройте бота в ЛС."

    try:
        await msg.reply_text(
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard,
            disable_web_page_preview=True,
        )
    except TelegramError:
        logger.exception("accounts: не удалось отправить групповой prompt")


async def cmd_account_plus(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _send_group_prompt(update, context, delete_only=False)


async def cmd_account_minus(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _send_group_prompt(update, context, delete_only=True)


# ---------------------------------------------------------------------------
# Роутер личных сообщений
# ---------------------------------------------------------------------------

_ACCOUNT_WORDS = {
    "аккаунт": "menu",
    "+аккаунт": "menu",
    "account": "menu",
    "сессии": "sessions",
    "сессия": "sessions",
    "sessions": "sessions",
    "создать аккаунт": "create",
    "войти": "login",
    "вход": "login",
    "выйти": "logout",
    "выход": "logout",
    "-аккаунт": "menu_delete",
    "удалить аккаунт": "delete",
    "смена пароля": "chg_pass",
    "сменить пароль": "chg_pass",
    "смена кода": "chg_code",
    "сменить код": "chg_code",
    "смена логина": "chg_login",
    "сменить логин": "chg_login",
    "зарегистрированные устройства": "sessions",
}


async def handle_private_update(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Обрабатывает только «аккаунтные» обновления в ЛС.

    Возвращает True, если обновление полностью обработано и его нельзя
    передавать дальше (в меню предложки).
    """
    chat = update.effective_chat
    if not chat or chat.type != "private":
        return False

    if update.callback_query:
        if (update.callback_query.data or "").startswith("acc:"):
            await account_callback(update, context)
            return True
        return False

    msg = update.effective_message
    user = update.effective_user
    if not msg or not user:
        return False

    state = _get_state(context)
    text = (msg.text or "").strip()

    # Пока идёт диалог — забираем любой текст.
    if state and text:
        await _process_state(update, context, state, text)
        return True

    if not text:
        return False

    # /start account | /start account_delete
    first = text.split(maxsplit=1)[0].split("@", 1)[0].lower()
    if first == "/start":
        payload = ""
        if " " in text:
            payload = text.split(maxsplit=1)[1].strip().lower()
        if payload in ("account", "account_delete"):
            context.user_data.pop("helper_state", None)
            _clear_state(context)
            if payload == "account_delete":
                await _show_menu(update, context, delete_only=True)
            else:
                await _show_menu(update, context)
            return True
        return False

    # Пока идёт диалог предложки, не перехватываем произвольный текст.
    if context.user_data.get("helper_state") and first not in ("/account", "/аккаунт"):
        return False

    word = text.lower().strip()
    action = _ACCOUNT_WORDS.get(word)
    if action is None:
        # Команда /account — тоже меню.
        if first in ("/account", "/аккаунт"):
            action = "menu"
        else:
            return False

    context.user_data.pop("helper_state", None)

    if action == "menu":
        _clear_state(context)
        await _show_menu(update, context)
    elif action == "menu_delete":
        _clear_state(context)
        await _show_menu(update, context, delete_only=True)
    elif action == "sessions":
        await _render_sessions(update, context)
    elif action == "create":
        await _handle_callback(update, context, "acc:create")
    elif action == "login":
        await _handle_callback(update, context, "acc:login")
    elif action == "logout":
        await _handle_callback(update, context, "acc:logout")
    elif action == "delete":
        await _handle_callback(update, context, "acc:delete")
    elif action == "chg_login":
        await _handle_callback(update, context, "acc:chg:login")
    elif action == "chg_pass":
        await _handle_callback(update, context, "acc:chg:pass")
    elif action == "chg_code":
        await _handle_callback(update, context, "acc:chg:code")
    else:
        await _show_menu(update, context)
    return True


# ---------------------------------------------------------------------------
# Подмена identity в группах
# ---------------------------------------------------------------------------

_TOUCHED: dict[int, float] = {}
_TOUCH_INTERVAL = 3600


async def _touch_session(ident: Identity, user: User) -> None:
    now = _now()
    last = _TOUCHED.get(ident.tg_id, 0.0)
    if now - last < _TOUCH_INTERVAL:
        return
    _TOUCHED[ident.tg_id] = now
    username = user.username or ""
    first_name = user.first_name or ""
    if username != ident.tg_username or first_name != ident.tg_first_name:
        try:
            await db._exec(
                """UPDATE account_sessions
                   SET username = ?, first_name = ?, last_seen = datetime('now')
                   WHERE tg_id = ?""",
                [username, first_name, int(ident.tg_id)],
            )
            invalidate(ident.tg_id)
        except Exception:
            logger.exception("accounts: не удалось обновить данные сессии")


async def identity_middleware(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Подменяет effective_user на владельца аккаунта в групповых чатах.

    В личных сообщениях подмены нет: там пользователь управляет аккаунтом
    от имени своего реального Telegram-аккаунта.
    """
    actual = update.effective_user
    _ACTUAL_USER.set(actual)
    if not actual or actual.is_bot:
        return

    chat = update.effective_chat
    if not chat or chat.type == "private":
        return

    # Главного админа не подменяем: у него должны работать админ-команды.
    if ADMIN_ID and actual.id == ADMIN_ID:
        return

    try:
        ident = await get_identity(actual.id)
    except Exception:
        logger.exception("accounts: ошибка получения identity tg=%s", actual.id)
        return
    if ident is None:
        return

    try:
        await _touch_session(ident, actual)
    except Exception:
        logger.exception("accounts: touch session failed")

    if ident.owner_id == actual.id:
        return

    synthetic = User(
        id=ident.owner_id,
        is_bot=False,
        first_name=ident.owner_first_name or actual.first_name or "",
        last_name=actual.last_name,
        username=ident.owner_username or actual.username,
    )
    try:
        # Update — слот-объект; object.__setattr__ обходит «заморозку» PTB.
        object.__setattr__(update, "_effective_user", synthetic)
        logger.debug(
            "accounts: подмена identity tg=%s -> owner=%s (login=%s)",
            actual.id, ident.owner_id, ident.login,
        )
    except Exception:
        logger.exception(
            "accounts: не удалось подменить effective_user tg=%s", actual.id
        )
