"""
Курсы валют и криптовалют.
Ежедневная публикация в CHAT_ID в 15:30 по Москве с закреплением.
Источники: CoinGecko, CoinCap и Банк России.
"""

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
COINCAP_URL = "https://api.coincap.io/v2/assets"

# CoinGecko ID -> CoinCap asset ID (для резервного источника).
COINCAP_IDS = {
    "bitcoin": "bitcoin",
    "ethereum": "ethereum",
    "tether": "tether",
    "binancecoin": "binance-coin",
    "solana": "solana",
    "ripple": "xrp",
    "usd-coin": "usd-coin",
    "dogecoin": "dogecoin",
    "tron": "tron",
    "cardano": "cardano",
    "bitcoin-cash": "bitcoin-cash",
    "chainlink": "chainlink",
    "stellar": "stellar",
    "avalanche-2": "avalanche",
    "shiba-inu": "shiba-inu",
    "the-open-network": "toncoin",
    "polkadot": "polkadot",
    "litecoin": "litecoin",
    "uniswap": "uniswap",
    "near": "near-protocol",
    "aptos": "aptos",
    "sui": "sui",
    "internet-computer": "internet-computer",
    "ethereum-classic": "ethereum-classic",
    "monero": "monero",
    "cosmos": "cosmos",
    "filecoin": "filecoin",
    "arbitrum": "arbitrum",
    "optimism": "optimism",
    "pepe": "pepe",
    "aave": "aave",
    "vechain": "vechain",
    "render-token": "render-token",
    "hedera-hashgraph": "hedera",
}

_LAST_GOOD_RATES: tuple[float, dict] | None = None


def _money(value: float) -> str:
    if value >= 1000:
        return f"{value:,.2f}".replace(",", " ").replace(".", ",")
    if value >= 1:
        return f"{value:,.4f}".replace(",", " ").replace(".", ",")
    return f"{value:.8f}".rstrip("0").rstrip(".").replace(".", ",")


async def _fetch_usd_rub(client: httpx.AsyncClient) -> float | None:
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
    except Exception:
        logger.warning("Не удалось получить курс USD/RUB от ЦБ РФ", exc_info=True)
    return None


async def _fetch_coincap(client: httpx.AsyncClient, usd_rub: float) -> dict:
    ids = ",".join(COINCAP_IDS.values())
    response = await client.get(COINCAP_URL, params={"ids": ids})
    response.raise_for_status()
    payload = response.json()
    items = payload.get("data", []) if isinstance(payload, dict) else []
    reverse_ids = {asset_id: coin_id for coin_id, asset_id in COINCAP_IDS.items()}
    result: dict[str, dict[str, float]] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        coin_id = reverse_ids.get(str(item.get("id", "")))
        try:
            usd_price = float(item.get("priceUsd"))
        except (TypeError, ValueError):
            continue
        if coin_id and usd_price > 0:
            result[coin_id] = {
                "usd": usd_price,
                "rub": usd_price * usd_rub,
            }
    if not result:
        raise RuntimeError("CoinCap вернул пустой список котировок")
    return result


async def _fetch_rates() -> tuple[float, dict]:
    global _LAST_GOOD_RATES

    async with httpx.AsyncClient(
        timeout=httpx.Timeout(20.0, connect=8.0),
        headers={"User-Agent": "TelegramCryptoRatesBot/1.2"},
    ) as client:
        # ЦБ запрашиваем отдельно: его курс нужен и для резервного CoinCap.
        usd_rub = await _fetch_usd_rub(client)

        try:
            crypto_response = await client.get(
                COINGECKO_URL,
                params={"ids": ",".join(COINS), "vs_currencies": "rub,usd"},
            )
            crypto_response.raise_for_status()
            crypto_data = crypto_response.json()
            if not isinstance(crypto_data, dict) or not crypto_data:
                raise RuntimeError("CoinGecko вернул пустые данные")
            logger.info("Курсы криптовалют получены через CoinGecko")
        except Exception as primary_error:
            logger.warning(
                "CoinGecko недоступен (%s); переключаюсь на CoinCap",
                primary_error,
            )
            if not usd_rub:
                # Если ЦБ временно недоступен, используем сохранённый курс USD/RUB.
                if _LAST_GOOD_RATES:
                    usd_rub = _LAST_GOOD_RATES[0]
                else:
                    raise RuntimeError(
                        "CoinGecko недоступен, а курс USD/RUB от ЦБ не получен"
                    ) from primary_error
            try:
                crypto_data = await _fetch_coincap(client, float(usd_rub))
                logger.info("Курсы криптовалют получены через резервный CoinCap")
            except Exception as fallback_error:
                logger.error("CoinCap тоже недоступен: %s", fallback_error)
                if _LAST_GOOD_RATES:
                    logger.warning("Использую последние успешно полученные курсы")
                    return _LAST_GOOD_RATES
                raise RuntimeError(
                    "CoinGecko и CoinCap недоступны, кэш ещё пуст"
                ) from fallback_error

    if not usd_rub:
        btc = crypto_data.get("bitcoin", {})
        rub, usd = btc.get("rub"), btc.get("usd")
        if rub and usd:
            usd_rub = float(rub) / float(usd)

    if not usd_rub:
        if _LAST_GOOD_RATES:
            usd_rub = _LAST_GOOD_RATES[0]
        else:
            raise RuntimeError("Не удалось получить курс доллара к рублю")

    # CoinCap отдаёт цены в USD — пересчитываем их в RUB по курсу ЦБ.
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
        "<i>Источники: CoinGecko / CoinCap и Банк России</i>",
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
