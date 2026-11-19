"""Ежедневная случайная статья УК РФ с подготовленным объяснением.

Одна статья на аккаунт бота в каждый календарный день по Москве.
Результат хранится в Turso и не сбрасывается при перезапуске приложения.
В пул попадают ТОЛЬКО статьи, для которых вручную подготовлены название,
понятное объяснение и прямая ссылка на полный текст.
"""
import logging
import random
from datetime import datetime, timedelta, timezone

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

import db
import accounts

logger = logging.getLogger(__name__)

# Москва постоянно UTC+3 — смена дня происходит в 00:00 по Москве.
_MSK = timezone(timedelta(hours=3), name="MSK")

# В пул добавляются только статьи с готовым объяснением и проверенной ссылкой.
# Формулировки ниже — краткие бытовые пояснения, а не юридическая консультация.
ARTICLES = {
    "105": {
        "title": "Убийство",
        "meaning": "Умышленное причинение смерти другому человеку.",
        "url": "https://www.consultant.ru/document/cons_doc_LAW_10699/5b3e04338020a09b25fe98ea83bc9362c8bc5a76/",
    },
    "109": {
        "title": "Причинение смерти по неосторожности",
        "meaning": "Когда человек по неосторожности своими действиями или бездействием становится причиной смерти другого человека, не имея умысла его убивать.",
        "url": "https://www.consultant.ru/document/cons_doc_LAW_10699/639472a621d0ccfce338497548ff5d396aa96045/",
    },
    "111": {
        "title": "Умышленное причинение тяжкого вреда здоровью",
        "meaning": "Умышленное причинение вреда здоровью, который опасен для жизни или имеет другие тяжёлые последствия, прямо перечисленные в законе.",
        "url": "https://www.consultant.ru/document/cons_doc_LAW_10699/e7204e825c8e87b5c7be210b06a0cde61cd60a3c/",
    },
    "112": {
        "title": "Умышленное причинение средней тяжести вреда здоровью",
        "meaning": "Умышленное причинение вреда здоровью, который не опасен для жизни и не относится к тяжкому, но вызывает длительное расстройство здоровья или значительную стойкую утрату трудоспособности менее чем на одну треть.",
        "url": "https://www.consultant.ru/document/cons_doc_LAW_10699/de03992e5fb9ce601f30ccc9ac4cea6a01e8fbf4/",
    },
    "115": {
        "title": "Умышленное причинение лёгкого вреда здоровью",
        "meaning": "Умышленное причинение лёгкого вреда здоровью, например вреда, который вызвал кратковременное расстройство здоровья или небольшую стойкую утрату общей трудоспособности.",
        "url": "https://www.consultant.ru/document/cons_doc_LAW_10699/160da87db0e45c893db6d5ca2729ea637bb32001/",
    },
    "116": {
        "title": "Побои",
        "meaning": "Побои или другие насильственные действия, причинившие физическую боль, если в них есть предусмотренные этой статьёй признаки и нет последствий, указанных в статье 115 УК РФ.",
        "url": "https://www.consultant.ru/document/cons_doc_LAW_10699/6dba187564f6295ba160042bed507b2a62939c22/",
    },
    "119": {
        "title": "Угроза убийством или причинением тяжкого вреда здоровью",
        "meaning": "Угроза убить человека или причинить ему тяжкий вред здоровью, если у потерпевшего были реальные основания опасаться исполнения этой угрозы.",
        "url": "https://www.consultant.ru/document/cons_doc_LAW_10699/37fd891311615f23654aa2bd69d386fc6c0c8470/",
    },
    "126": {
        "title": "Похищение человека",
        "meaning": "Незаконный захват человека, его перемещение и последующее удержание против его воли.",
        "url": "https://www.consultant.ru/document/cons_doc_LAW_10699/fbdb5e8eb268dd7d0bd2dc4e63ac9ac2d33c7cde/",
    },
    "128.1": {
        "title": "Клевета",
        "meaning": "Распространение заведомо ложных сведений, которые порочат честь и достоинство другого человека или подрывают его репутацию.",
        "url": "https://www.consultant.ru/document/cons_doc_LAW_10699/8a73d26dba7976d6c43cc94aa1515368fef256f0/",
    },
    "137": {
        "title": "Нарушение неприкосновенности частной жизни",
        "meaning": "Незаконный сбор или распространение сведений о частной жизни человека, составляющих его личную или семейную тайну, без его согласия — при наличии признаков состава преступления.",
        "url": "https://www.consultant.ru/document/cons_doc_LAW_10699/4234a27af714cc608ea71b7bae9400f3613c8f60/",
    },
    "138": {
        "title": "Нарушение тайны переписки и сообщений",
        "meaning": "Незаконное нарушение тайны переписки, телефонных разговоров и других сообщений человека.",
        "url": "https://www.consultant.ru/document/cons_doc_LAW_10699/cad87fb351934e731be49cb6ee1b17de00c25d46/",
    },
    "139": {
        "title": "Нарушение неприкосновенности жилища",
        "meaning": "Незаконное проникновение в жилище против воли проживающего там человека, если нет предусмотренных законом оснований для такого проникновения.",
        "url": "https://www.consultant.ru/document/cons_doc_LAW_10699/50d5e657645fa5ca39c5569d6ed240778c0e40b1/",
    },
    "158": {
        "title": "Кража",
        "meaning": "Тайное хищение чужого имущества.",
        "url": "https://www.consultant.ru/document/cons_doc_LAW_10699/57b5c7b83fcd2cf40cabe2042f2d8f04ed6875ad/",
    },
    "159": {
        "title": "Мошенничество",
        "meaning": "Хищение чужого имущества или получение права на него путём обмана либо злоупотребления доверием.",
        "url": "https://www.consultant.ru/document/cons_doc_LAW_10699/8012ecdf64b7c9cfd62e90d7f55f9b5b7b72b755/",
    },
    "160": {
        "title": "Присвоение или растрата",
        "meaning": "Обращение в свою пользу или незаконное расходование чужого имущества, которое было доверено человеку и находилось у него на законном основании.",
        "url": "https://www.consultant.ru/document/cons_doc_LAW_10699/4641cfe1bdfab945ead3ae228d36c3e8141dd9f1/",
    },
    "161": {
        "title": "Грабёж",
        "meaning": "Открытое хищение чужого имущества: виновный забирает имущество на виду у владельца или других людей, понимая, что его действия заметили.",
        "url": "https://www.consultant.ru/document/cons_doc_LAW_10699/8727611b42df79f2b3ef8d2f3b68fea711ed0c7a/",
    },
    "162": {
        "title": "Разбой",
        "meaning": "Нападение с целью хищения чужого имущества, сопряжённое с опасным для жизни или здоровья насилием либо угрозой такого насилия.",
        "url": "https://www.consultant.ru/document/cons_doc_LAW_10699/0b1063e4835ba9d69ebc8556c87031cd3795405e/",
    },
    "163": {
        "title": "Вымогательство",
        "meaning": "Требование передать имущество, деньги или право на имущество под угрозами, например применить насилие, повредить имущество или распространить порочащие сведения.",
        "url": "https://www.consultant.ru/document/cons_doc_LAW_10699/3cf93ca64f2a009e75430fc6394b66a3642ba176/",
    },
    "166": {
        "title": "Неправомерное завладение автомобилем или иным транспортным средством без цели хищения",
        "meaning": "Неправомерное завладение автомобилем или другим транспортным средством без намерения присвоить его, то есть угон.",
        "url": "https://www.consultant.ru/document/cons_doc_LAW_10699/1917a12954153390d74667e91d0af4f261e560dc/",
    },
    "167": {
        "title": "Умышленные уничтожение или повреждение имущества",
        "meaning": "Умышленное уничтожение или повреждение чужого имущества, если причинён значительный ущерб и имеются остальные признаки, предусмотренные статьёй.",
        "url": "https://www.consultant.ru/document/cons_doc_LAW_10699/d260e55e06d1e6bc720d2e591a8383a43b1a5eed/",
    },
    "171": {
        "title": "Незаконное предпринимательство",
        "meaning": "Осуществление предпринимательской деятельности без обязательной регистрации или лицензии, если при этом есть предусмотренные законом крупный ущерб, доход или иные признаки преступления.",
        "url": "https://www.consultant.ru/document/cons_doc_LAW_10699/cc12ef68af6f5296cb8a9dad10ca87865d02f12f/",
    },
    "207": {
        "title": "Заведомо ложное сообщение об акте терроризма",
        "meaning": "Заведомо ложное сообщение о готовящемся взрыве, поджоге или других действиях, создающих опасность для людей или объектов, перечисленных в статье.",
        "url": "https://www.consultant.ru/document/cons_doc_LAW_10699/2c54dfed2e09c1236bf059a9211bb37b2fcbb8e1/",
    },
    "213": {
        "title": "Хулиганство",
        "meaning": "Грубое нарушение общественного порядка, выражающее явное неуважение к обществу, при наличии предусмотренных статьёй признаков, например применения насилия или угрозы его применения.",
        "url": "https://www.consultant.ru/document/cons_doc_LAW_10699/9d692997b8cddf26782684f489987701b0daacf3/",
    },
}


def _validate_articles() -> None:
    for number, item in ARTICLES.items():
        if not all(item.get(key) for key in ("title", "meaning", "url")):
            raise RuntimeError(f"Для статьи {number} не подготовлены все поля")


_validate_articles()


async def init_db() -> None:
    """Создаёт постоянное хранилище ежедневных статей."""
    rs = await db._exec("""
        CREATE TABLE IF NOT EXISTS daily_criminal_articles (
            account_owner_id INTEGER NOT NULL,
            moscow_day TEXT NOT NULL,
            article_number TEXT NOT NULL,
            created_at TEXT DEFAULT (datetime('now')),
            PRIMARY KEY (account_owner_id, moscow_day)
        )
    """)
    if getattr(rs, "error", False):
        raise RuntimeError("Не удалось создать таблицу daily_criminal_articles")


async def get_daily_article(account_owner_id: int, day: str | None = None) -> str:
    """Возвращает одну подготовленную статью на аккаунт и московский день."""
    day = day or datetime.now(_MSK).date().isoformat()

    existing = await db._exec(
        """SELECT article_number
           FROM daily_criminal_articles
           WHERE account_owner_id = ? AND moscow_day = ?
           LIMIT 1""",
        [int(account_owner_id), day],
    )
    if getattr(existing, "error", False):
        raise RuntimeError("Ошибка чтения ежедневной статьи")

    # Если в базе остался номер из прежнего, более широкого пула,
    # заменяем его — пользователь должен получать только статью с описанием.
    if existing.rows:
        saved_number = str(existing.rows[0][0])
        if saved_number in ARTICLES:
            return saved_number
        replacement = random.choice(list(ARTICLES))
        updated = await db._exec(
            """UPDATE daily_criminal_articles
               SET article_number = ?
               WHERE account_owner_id = ? AND moscow_day = ?""",
            [replacement, int(account_owner_id), day],
        )
        if getattr(updated, "error", False):
            raise RuntimeError("Не удалось обновить ежедневную статью")
        return replacement

    previous = await db._exec(
        """SELECT article_number
           FROM daily_criminal_articles
           WHERE account_owner_id = ?
           ORDER BY moscow_day DESC
           LIMIT 1""",
        [int(account_owner_id)],
    )
    if getattr(previous, "error", False):
        raise RuntimeError("Ошибка чтения предыдущей статьи")

    previous_article = str(previous.rows[0][0]) if previous.rows else None
    candidates = [number for number in ARTICLES if number != previous_article]
    selected = random.choice(candidates or list(ARTICLES))

    inserted = await db._exec(
        """INSERT OR IGNORE INTO daily_criminal_articles
           (account_owner_id, moscow_day, article_number)
           VALUES (?, ?, ?)""",
        [int(account_owner_id), day, selected],
    )
    if getattr(inserted, "error", False):
        raise RuntimeError("Ошибка сохранения ежедневной статьи")

    # При двух одновременных запросах возвращаем запись, которую сохранила БД.
    saved = await db._exec(
        """SELECT article_number
           FROM daily_criminal_articles
           WHERE account_owner_id = ? AND moscow_day = ?
           LIMIT 1""",
        [int(account_owner_id), day],
    )
    if getattr(saved, "error", False) or not saved.rows:
        raise RuntimeError("Не удалось получить сохранённую статью")
    saved_number = str(saved.rows[0][0])
    if saved_number not in ARTICLES:
        raise RuntimeError("В базе сохранён номер статьи без подготовленного объяснения")
    return saved_number


async def cmd_my_article(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработчик фразы «Моя статья» или «статья»."""
    msg = update.effective_message
    user = update.effective_user
    if not msg or not user:
        return

    try:
        # Общая запись для всех Telegram-сессий одного аккаунта бота.
        owner_id = await accounts.data_id_for(int(user.id))
        day = datetime.now(_MSK).date().isoformat()
        article_number = await get_daily_article(owner_id, day)
        article = ARTICLES[article_number]
    except Exception:
        logger.exception(
            "my_article: не удалось выдать статью user_id=%s",
            getattr(user, "id", None),
        )
        await msg.reply_text("⚠️ Не удалось получить статью. Попробуй ещё раз позже.")
        return

    text = (
        f"Твоя статья (УК РФ, статья {article_number})\n\n"
        f"📖 Что это значит:\n\n"
        f"{article['meaning']}\n\n"
        f"🔗 <a href=\"{article['url']}\">Читать статью полностью</a>"
    )
    await msg.reply_text(
        text,
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )
