"""Подсчёт слов участников с пакетной записью в Turso."""
from __future__ import annotations

import asyncio
import logging
import re
from collections import Counter, defaultdict
from html import escape

import db

logger = logging.getLogger(__name__)

_WORD_RE = re.compile(r"[A-Za-zА-Яа-яЁё]{2,}", re.UNICODE)
_STOP_WORDS = set("""
а и 
""".split())

# Буферы в памяти: на сообщение нет ни одного обращения к базе данных.
_TOTALS: Counter[tuple[int, int]] = Counter()
_FREQUENCIES: Counter[tuple[int, int, str]] = Counter()
_LOCK = asyncio.Lock()
_MAX_BUFFER_ITEMS = 5000


async def init_db() -> None:
    await db._exec_required("""
        CREATE TABLE IF NOT EXISTS word_totals (
            chat_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            total_words INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (chat_id, user_id)
        )
    """)
    await db._exec_required("""
        CREATE TABLE IF NOT EXISTS word_frequencies (
            chat_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            word TEXT NOT NULL,
            count INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (chat_id, user_id, word)
        )
    """)
    await db._exec_required(
        "CREATE INDEX IF NOT EXISTS idx_word_totals_chat "
        "ON word_totals(chat_id, total_words DESC)"
    )
    await db._exec_required(
        "CREATE INDEX IF NOT EXISTS idx_word_frequency_top "
        "ON word_frequencies(chat_id, user_id, count DESC)"
    )
    logger.info("Word statistics tables are ready")


def _extract_words(text: str) -> list[str]:
    return [w.casefold().replace("ё", "е") for w in _WORD_RE.findall(text or "")]


async def track_message(update, context) -> None:
    """Накапливает статистику группового текстового сообщения без SQL-запроса."""
    message = update.effective_message
    user = update.effective_user
    chat = update.effective_chat
    if not message or not user or user.is_bot or not chat:
        return
    if chat.type not in ("group", "supergroup"):
        return

    text = (message.text or "").strip()
    if not text:
        return

    if " ".join(text.casefold().split()) == "слова все":
        await show_all_words(update, context)
        return
    if text.startswith("/"):
        return

    words = _extract_words(text)
    if not words:
        return

    key = (int(chat.id), int(user.id))
    _TOTALS[key] += len(words)
    freq = Counter(w for w in words if w not in _STOP_WORDS)
    for word, amount in freq.items():
        _FREQUENCIES[(key[0], key[1], word)] += amount

    # Защита от неограниченного роста RAM при очень активном чате.
    if len(_FREQUENCIES) >= _MAX_BUFFER_ITEMS:
        await flush()


async def _upsert_many(table: str, columns: tuple[str, ...], rows: list[tuple],
                       update_expr: str) -> int:
    """Возвращает число строк из начала списка, успешно записанных в БД."""
    if not rows:
        return 0
    width = len(columns)
    # Не превышаем лимит параметров SQLite/libSQL.
    completed = 0
    for offset in range(0, len(rows), 100):
        chunk = rows[offset:offset + 100]
        values_sql = ",".join(["(" + ",".join(["?"] * width) + ")"] * len(chunk))
        params = [value for row in chunk for value in row]
        sql = (
            f"INSERT INTO {table} ({','.join(columns)}) VALUES {values_sql} "
            f"ON CONFLICT({','.join(columns[:len(columns)-1] if table == 'word_totals' else columns[:3])}) "
            f"DO UPDATE SET {update_expr}"
        )
        # Для word_totals PK — первые 2 столбца; для word_frequencies — первые 3.
        result = await db._exec(sql, params)
        if getattr(result, "error", False):
            return completed
        completed += len(chunk)
    return completed


async def flush() -> None:
    """Сбрасывает накопленные счётчики двумя пакетными сериями SQL-запросов."""
    async with _LOCK:
        if not _TOTALS and not _FREQUENCIES:
            return
        totals = list(_TOTALS.items())
        frequencies = list(_FREQUENCIES.items())
        _TOTALS.clear()
        _FREQUENCIES.clear()

    total_rows = [(chat_id, user_id, count) for (chat_id, user_id), count in totals]
    freq_rows = [(chat_id, user_id, word, count)
                 for (chat_id, user_id, word), count in frequencies]

    totals_done = await _upsert_many(
        "word_totals", ("chat_id", "user_id", "total_words"), total_rows,
        "total_words = word_totals.total_words + excluded.total_words",
    )
    frequencies_done = 0
    if totals_done == len(total_rows):
        frequencies_done = await _upsert_many(
            "word_frequencies", ("chat_id", "user_id", "word", "count"), freq_rows,
            "count = word_frequencies.count + excluded.count",
        )

    if totals_done < len(total_rows) or frequencies_done < len(freq_rows):
        logger.error("Word statistics flush incomplete; restoring unwritten counters")
        async with _LOCK:
            for (key, count) in totals[totals_done:]:
                _TOTALS[key] += count
            for (key, count) in frequencies[frequencies_done:]:
                _FREQUENCIES[key] += count


async def flush_job(context) -> None:
    await flush()


async def get_user_stats(chat_id: int, user_id: int) -> dict:
    rs = await db._exec(
        """SELECT t.total_words,
                  (SELECT f.word FROM word_frequencies f
                   WHERE f.chat_id=t.chat_id AND f.user_id=t.user_id
                   ORDER BY f.count DESC, f.word ASC LIMIT 1) AS frequent_word
           FROM word_totals t WHERE t.chat_id=? AND t.user_id=? LIMIT 1""",
        [int(chat_id), int(user_id)],
    )
    persisted_total = 0
    frequent_word = None
    if not getattr(rs, "error", False) and rs.rows:
        persisted_total = int(rs.rows[0][0] or 0)
        frequent_word = rs.rows[0][1]
    pending_total = _TOTALS.get((int(chat_id), int(user_id)), 0)
    pending_words = {word: count for (cid, uid, word), count in _FREQUENCIES.items()
                     if cid == int(chat_id) and uid == int(user_id)}
    if pending_words:
        pending_top = max(pending_words.items(), key=lambda item: (item[1], item[0]))[0]
        if frequent_word is None:
            frequent_word = pending_top
    return {"total_words": persisted_total + pending_total,
            "frequent_word": frequent_word}


async def get_all_stats(chat_id: int) -> list[dict]:
    rs = await db._exec(
        """SELECT t.user_id, t.total_words,
                  (SELECT f.word FROM word_frequencies f
                   WHERE f.chat_id=t.chat_id AND f.user_id=t.user_id
                   ORDER BY f.count DESC, f.word ASC LIMIT 1) AS frequent_word,
                  COALESCE(u.first_name, ''), COALESCE(u.username, '')
           FROM word_totals t
           LEFT JOIN users u ON u.user_id=t.user_id
           WHERE t.chat_id=?
           ORDER BY t.total_words DESC, t.user_id ASC""",
        [int(chat_id)],
    )
    if getattr(rs, "error", False):
        logger.error("get_all_stats: database query failed")
        return []
    return [{"user_id": int(row[0]), "total_words": int(row[1] or 0),
             "frequent_word": row[2], "first_name": row[3], "username": row[4]}
            for row in rs.rows]


async def show_all_words(update, context) -> None:
    message = update.effective_message
    chat = update.effective_chat
    if not message or not chat:
        return
    # Показываем и ещё не сброшенные в Turso данные текущего процесса.
    await flush()
    rows = await get_all_stats(chat.id)
    if not rows:
        await message.reply_text("📝 Пока статистики слов нет. Она начнёт собираться с новых сообщений.")
        return

    lines = ["📝 <b>СТАТИСТИКА СЛОВ УЧАСТНИКОВ</b>", "━━━━━━━━━━━━━━━"]
    for index, row in enumerate(rows, 1):
        name = row["first_name"] or row["username"] or f"ID {row['user_id']}"
        if row["username"]:
            name = f"@{row['username']}"
        frequent = row["frequent_word"] or "—"
        lines.append(
            f"{index}. <b>{escape(str(name))}</b> — "
            f"<b>{row['total_words']:,}</b> слов · частое: <code>{escape(str(frequent))}</code>"
        )

    # Telegram ограничивает длину одного сообщения.
    chunk = ""
    for line in lines:
        candidate = f"{chunk}\n{line}" if chunk else line
        if len(candidate) > 3800 and chunk:
            await message.reply_text(chunk, parse_mode="HTML")
            chunk = line
        else:
            chunk = candidate
    if chunk:
        await message.reply_text(chunk, parse_mode="HTML")
