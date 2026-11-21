"""
Курсы валют и криптовалют.
Ежедневная публикация в CHAT_ID в 15:30 по Москве с закреплением.
Источники: CoinGecko, CoinPaprika, Банк России и open.er-api.com.
"""

import asyncio
import logging
import os
import xml.etree.ElementTree as ET
from datetime import time as dtime
from zoneinfo import ZoneInfo

import httpx
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

logger = logging.getLogger(__name__)

CHAT_ID = int(os.getenv("CHAT_ID", "0") or 0)
MOSCOW_TZ = ZoneInfo("Europe/Moscow")

# CoinGecko ID: (тикер, английское имя, русское имя, варианты запроса)
COINS = {
    "bitcoin": ("BTC", "Bitcoin", "Биткоин", ["btc", "bitcoin", "биткоин", "биткойн", "биток"]),
    "ethereum": ("ETH", "Ethereum", "Эфириум", ["eth", "ethereum", "эфир", "эфириум", "этериум"]),
    "tether": ("USDT", "Tether", "Тезер", ["usdt", "tether", "тезер", "тетер"]),
    "binancecoin": ("BNB", "BNB", "БНБ", ["bnb", "binance coin", "бинанс коин", "бнб"]),
    "solana": ("SOL", "Solana", "Солана", ["sol", "solana", "солана"]),
    "ripple": ("XRP", "XRP", "Рипл", ["xrp", "ripple", "рипл"]),
    "usd-coin": ("USDC", "USD Coin", "ЮСД Коин", ["usdc", "usd coin", "юсдс", "юсд коин"]),
    "dogecoin": ("DOGE", "Dogecoin", "Догикоин", ["doge", "dogecoin", "догкоин", "догикойн", "дог"]),
    "tron": ("TRX", "TRON", "Трон", ["trx", "tron", "трон"]),
    "cardano": ("ADA", "Cardano", "Кардано", ["ada", "cardano", "кардано"]),
    "bitcoin-cash": ("BCH", "Bitcoin Cash", "Биткоин Кэш", ["bch", "bitcoin cash", "биткоин кэш", "биткэш"]),
    "chainlink": ("LINK", "Chainlink", "Чейнлинк", ["link", "chainlink", "чейнлинк"]),
    "stellar": ("XLM", "Stellar", "Стеллар", ["xlm", "stellar", "стеллар"]),
    "avalanche-2": ("AVAX", "Avalanche", "Аваланч", ["avax", "avalanche", "аваланш", "авах"]),
    "shiba-inu": ("SHIB", "Shiba Inu", "Сиба-ину", ["shib", "shiba", "shiba inu", "шиба", "сиба"]),
    "the-open-network": ("TON", "Toncoin", "Тонкоин", ["ton", "toncoin", "the open network", "тон", "тонкоин"]),
    "polkadot": ("DOT", "Polkadot", "Полкадот", ["dot", "polkadot", "полкадот"]),
    "litecoin": ("LTC", "Litecoin", "Лайткоин", ["ltc", "litecoin", "лайткоин", "лайт"]),
    "uniswap": ("UNI", "Uniswap", "Юнисвап", ["uni", "uniswap", "юнисвап"]),
    "near": ("NEAR", "NEAR Protocol", "НЕАР", ["near", "near protocol", "неар"]),
    "aptos": ("APT", "Aptos", "Аптос", ["apt", "aptos", "аптос"]),
    "sui": ("SUI", "Sui", "Суи", ["sui", "суи"]),
    "internet-computer": ("ICP", "Internet Computer", "Интернет Компьютер", ["icp", "internet computer", "интернет компьютер"]),
    "ethereum-classic": ("ETC", "Ethereum Classic", "Эфириум Классик", ["etc", "ethereum classic", "эфириум классик", "эфир классик"]),
    "monero": ("XMR", "Monero", "Монеро", ["xmr", "monero", "монеро"]),
    "cosmos": ("ATOM", "Cosmos", "Космос", ["atom", "cosmos", "космос", "атом"]),
    "filecoin": ("FIL", "Filecoin", "Файлкоин", ["fil", "filecoin", "файлкоин"]),
    "arbitrum": ("ARB", "Arbitrum", "Арбитрум", ["arb", "arbitrum", "арбитрум"]),
    "optimism": ("OP", "Optimism", "Оптимизм", ["op", "optimism", "оптимизм"]),
    "pepe": ("PEPE", "Pepe", "Пепе", ["pepe", "пепе"]),
    "aave": ("AAVE", "Aave", "Ааве", ["aave", "аве"]),
    "vechain": ("VET", "VeChain", "Вичейн", ["vet", "vechain", "вичейн"]),
    "render-token": ("RENDER", "Render", "Рендер", ["render", "render token", "рендер"]),
    "hedera-hashgraph": ("HBAR", "Hedera", "Хедера", ["hbar", "hedera", "хедера"]),
}

ALIASES = {
    alias.casefold(): coin_id
    for coin_id, (_, _, _, aliases) in COINS.items()
    for alias in aliases
}

COINGECKO_URL = "https://api.coingecko.com/api/v3/simple/price"
CBR_URL = "https://www.cbr.ru/scripts/XML_daily.asp"
COINPAPRIKA_URL = "https://api.coinpaprika.com/v1/tickers"
EXCHANGERATE_URL = "https://open.er-api.com/v6/latest/USD"

# Резервный источник без API-ключа: CoinPaprika.
# Сопоставление идёт по тикеру из нашего списка; среди совпадений
# выбирается актив с наименьшим rank (наиболее крупный/известный).
TICKER_TO_COIN_ID = {values[0].upper(): coin_id for coin_id, values in COINS.items()}


_LAST_GOOD_RATES: tuple[float, dict] | None = None


def _money(value: float) -> str:
    if value >= 1000:
        return f"{value:,.2f}".replace(",", " ").replace(".", ",")
    if value >= 1:
        return f"{value:,.4f}".replace(",", " ").replace(".", ",")
    return f"{value:.8f}".rstrip("0").rstrip(".").replace(".", ",")


async def _request_json(
    client: httpx.AsyncClient,
    url: str,
    *,
    params: dict | None = None,
    attempts: int = 2,
) -> dict | list:
    """Запрос JSON с коротким повтором при временной сетевой ошибке."""
    last_error = None
    for attempt in range(attempts):
        try:
            response = await client.get(url, params=params)
            response.raise_for_status()
            return response.json()
        except (httpx.HTTPError, ValueError) as exc:
            last_error = exc
            if attempt + 1 < attempts:
                await asyncio.sleep(0.6 * (attempt + 1))
    raise RuntimeError(f"Не удалось получить данные от {url}: {last_error}") from last_error


async def _fetch_usd_rub(client: httpx.AsyncClient) -> float | None:
    """Сначала ЦБ РФ, затем бесплатный open.er-api.com без ключа."""
    try:
        response = await client.get(CBR_URL)
        response.raise_for_status()
        root = ET.fromstring(response.content)
        for item in root.findall("Valute"):
            if item.findtext("CharCode") == "USD":
                nominal = float(item.findtext("Nominal", "1").replace(",", "."))
                value = float(item.findtext("Value", "0").replace(",", "."))
                if nominal > 0 and value > 0:
                    return value / nominal
    except Exception as exc:
        logger.warning("ЦБ РФ временно недоступен: %s", exc)

    try:
        payload = await _request_json(client, EXCHANGERATE_URL)
        rate = (payload.get("rates") or {}).get("RUB") if isinstance(payload, dict) else None
        rate = float(rate)
        if rate > 0:
            logger.info("USD/RUB получен через open.er-api.com")
            return rate
    except Exception as exc:
        logger.warning("Резервный источник USD/RUB недоступен: %s", exc)
    return None


async def _fetch_coinpaprika(client: httpx.AsyncClient, usd_rub: float) -> dict:
    """Получает USD-котировки из бесплатного публичного API CoinPaprika."""
    payload = await _request_json(
        client,
        COINPAPRIKA_URL,
        params={"quotes": "USD"},
    )
    if not isinstance(payload, list):
        raise RuntimeError("CoinPaprika вернул неожиданный формат")

    result: dict[str, dict[str, float]] = {}
    # API обычно сортирует активы по rank; сохраняем первое совпадение тикера.
    for item in sorted(
        (x for x in payload if isinstance(x, dict)),
        key=lambda x: int(x.get("rank") or 10**9),
    ):
        ticker = str(item.get("symbol") or "").upper()
        coin_id = TICKER_TO_COIN_ID.get(ticker)
        if not coin_id or coin_id in result:
            continue
        quote = (item.get("quotes") or {}).get("USD") or {}
        try:
            usd_price = float(quote.get("price"))
        except (TypeError, ValueError):
            continue
        if usd_price > 0:
            result[coin_id] = {"usd": usd_price, "rub": usd_price * usd_rub}

    if not result:
        raise RuntimeError("CoinPaprika не вернул подходящих котировок")
    return result


async def _fetch_coingecko(client: httpx.AsyncClient) -> dict:
    payload = await _request_json(
        client,
        COINGECKO_URL,
        params={"ids": ",".join(COINS), "vs_currencies": "rub,usd"},
    )
    if not isinstance(payload, dict) or not payload:
        raise RuntimeError("CoinGecko вернул пустые данные")
    return payload


async def _fetch_rates() -> tuple[float, dict]:
    """CoinGecko -> CoinPaprika; при сбое используем последний успешный кэш."""
    global _LAST_GOOD_RATES

    timeout = httpx.Timeout(12.0, connect=5.0)
    async with httpx.AsyncClient(
        timeout=timeout,
        headers={"User-Agent": "TelegramCryptoRatesBot/2.0"},
        follow_redirects=True,
    ) as client:
        usd_rub = await _fetch_usd_rub(client)

        try:
            crypto_data = await _fetch_coingecko(client)
            logger.info("Курсы криптовалют получены через CoinGecko")
        except Exception as primary_error:
            logger.warning("CoinGecko недоступен: %s; пробую CoinPaprika", primary_error)
            if not usd_rub and _LAST_GOOD_RATES:
                usd_rub = _LAST_GOOD_RATES[0]
            if not usd_rub:
                raise RuntimeError(
                    "Не удалось получить USD/RUB ни от ЦБ, ни от open.er-api.com"
                ) from primary_error
            try:
                crypto_data = await _fetch_coinpaprika(client, float(usd_rub))
                logger.info("Курсы криптовалют получены через CoinPaprika (бесплатный API)")
            except Exception as fallback_error:
                logger.error("CoinPaprika тоже недоступен: %s", fallback_error)
                if _LAST_GOOD_RATES:
                    logger.warning("Оба API недоступны; использую последние успешные курсы")
                    return _LAST_GOOD_RATES
                raise RuntimeError(
                    "CoinGecko и CoinPaprika недоступны, успешного кэша пока нет"
                ) from fallback_error

    if not usd_rub:
        btc = crypto_data.get("bitcoin", {})
        rub, usd = btc.get("rub"), btc.get("usd")
        if rub and usd:
            usd_rub = float(rub) / float(usd)

    if not usd_rub and _LAST_GOOD_RATES:
        usd_rub = _LAST_GOOD_RATES[0]
    if not usd_rub:
        raise RuntimeError("Не удалось определить курс USD/RUB")

    # CoinPaprika отдаёт USD; для него пересчёт в RUB уже сделан выше.
    # CoinGecko обычно отдаёт обе валюты, но заполняем RUB, если её нет.
    for values in crypto_data.values():
        if isinstance(values, dict) and values.get("rub") is None and values.get("usd"):
            values["rub"] = float(values["usd"]) * float(usd_rub)

    result = (float(usd_rub), crypto_data)
    _LAST_GOOD_RATES = result
    return result


def _format_rates_text(usd_rub: float, data: dict) -> str:
    lines = [
        "📊 <b>КУРСЫ ВАЛЮТ И КРИПТОВАЛЮТ</b>",
        f"💵 <b>Доллар США:</b> 1 USD = <b>{_money(usd_rub)} ₽</b>",
        "",
        f"🪙 <b>Криптовалюты к рублю ({len(COINS)} монет)</b>",
    ]
    for coin_id, (ticker, _en_name, ru_name, _aliases) in COINS.items():
        price = data.get(coin_id, {}).get("rub")
        if price is None:
            lines.append(f"• <b>{ticker}</b> ({ru_name}): нет данных")
        else:
            lines.append(f"• <b>{ticker}</b> ({ru_name}): {_money(float(price))} ₽")

    lines.extend([
        "",
        "<i>Источники: CoinGecko / CoinPaprika, Банк России / open.er-api.com</i>",
        "⏰ Обновление ежедневно в 15:30 МСК",
        "Для отдельной монеты напиши: <code>Курс биткоин</code> или <code>Курс ETH</code>",
    ])
    return "\n".join(lines)


async def cmd_rates(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработчик фраз «Курс», «Курс BTC», «Курс биткоин» и т. п."""
    message = update.effective_message
    if not message:
        return

    query = " ".join(context.args or []).strip()
    if not query and message.text:
        parts = message.text.strip().split(maxsplit=1)
        if len(parts) > 1:
            query = parts[1].strip()

    try:
        usd_rub, data = await _fetch_rates()
    except Exception:
        logger.exception("Не удалось получить курсы")
        await message.reply_text("⚠️ Не удалось получить курсы. Попробуй немного позже.")
        return

    if not query:
        await message.reply_text(
            _format_rates_text(usd_rub, data),
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
        return

    key = query.casefold().strip().replace("$", "")
    if key in ("доллар", "доллары", "usd", "доллар сша", "рубль", "доллар в рубли"):
        await message.reply_text(
            f"💵 <b>Курс доллара</b>\n1 USD = <b>{_money(usd_rub)} ₽</b>",
            parse_mode=ParseMode.HTML,
        )
        return

    coin_id = ALIASES.get(key)
    if not coin_id:
        available = ", ".join(item[0] for item in COINS.values())
        await message.reply_text(
            "❓ Не распознал монету.\n"
            f"Доступные тикеры: {available}\n"
            "Пример: <code>Курс BTC</code>, <code>Курс биткоин</code>, <code>Курс эфир</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    ticker, _en_name, ru_name, _aliases = COINS[coin_id]
    price = data.get(coin_id, {}).get("rub")
    if price is None:
        await message.reply_text(f"⚠️ Сейчас нет данных по {ru_name} ({ticker}).")
        return

    await message.reply_text(
        f"🪙 <b>{ru_name} ({ticker})</b>\n"
        f"1 {ticker} = <b>{_money(float(price))} ₽</b>\n"
        f"💵 USD/RUB: {_money(usd_rub)} ₽",
        parse_mode=ParseMode.HTML,
    )


async def daily_rates_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Публикует дневную сводку и закрепляет её в основной группе."""
    if not CHAT_ID:
        logger.warning("Курсы: CHAT_ID не задан, ежедневная публикация пропущена")
        return
    try:
        usd_rub, data = await _fetch_rates()
        text = _format_rates_text(usd_rub, data)
        sent = await context.bot.send_message(
            chat_id=CHAT_ID,
            text=text,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
        try:
            await context.bot.pin_chat_message(
                chat_id=CHAT_ID,
                message_id=sent.message_id,
                disable_notification=True,
            )
        except Exception:
            logger.exception(
                "Курсы опубликованы, но не удалось закрепить сообщение. "
                "Проверь права администратора и право закрепления сообщений."
            )
        logger.info("Ежедневные курсы опубликованы: chat_id=%s", CHAT_ID)
    except Exception:
        logger.exception("Ошибка ежедневной публикации курсов")


def register_jobs(application) -> None:
    """Подключает ежедневную публикацию курсов на 15:30 по московскому времени."""
    if not application.job_queue:
        logger.warning("Курсы: JobQueue недоступен, расписание не создано")
        return
    application.job_queue.run_daily(
        daily_rates_job,
        time=dtime(hour=15, minute=30, tzinfo=MOSCOW_TZ),
        name="daily_crypto_rates",
    )
    logger.info("Ежедневная публикация курсов запланирована на 15:30 МСК")
