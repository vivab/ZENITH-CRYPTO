import asyncio
import logging
import os
import re
import time
from collections import OrderedDict
from dataclasses import dataclass

import aiohttp
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.types import LinkPreviewOptions, Message

# =========================
#         НАСТРОЙКИ
# =========================

BOT_TOKEN = os.getenv("BOT_TOKEN", "ВСТАВЬ_СЮДА_ТОКЕН_ОТ_BOTFATHER")

ORDER_TTL_SECONDS = 30 * 60        # ордер актуален 30 минут
CLEANUP_INTERVAL_SECONDS = 60      # как часто чистим протухшие ордера
MAX_CHECK_SNAPSHOTS_PER_CHAT = 50  # сколько последних /check помним (для /take)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("order_bot")

# Ключевые слова, по которым определяем тип ордера.
BUY_KEYWORDS = ["хавну", "возьму", "нужны", "нужен", "нужна", "дайте", "продайте", "куплю"]
SELL_KEYWORDS = ["продам", "дам", "отдам"]

BUY_EMOJI_FALLBACK = "🟩"
SELL_EMOJI_FALLBACK = "🟥"

# Кастомные премиум-эмодзи
BUY_CUSTOM_EMOJI_ID = "5296596700704548349"
SELL_CUSTOM_EMOJI_ID = "5294049355601292129"

GREEN_T_EMOJI_ID = "5406841020769936275"
CLOCK_EMOJI_ID = "5197402116016072864"
YELLOW_CIRCLE_EMOJI_ID = "5461117441612462242"
GLASSES_CIRCLE_EMOJI_ID = "5368562433981947135"

# Доллар и рубль теперь используют один и тот же кастомный эмодзи
DOLLAR_EMOJI_ID = "5296597065776769217"
RUBLE_EMOJI_ID = "5296597065776769217"


def custom_emoji_html(emoji_id: str, fallback: str) -> str:
    return f'<tg-emoji emoji-id="{emoji_id}">{fallback}</tg-emoji>'


BUY_EMOJI = custom_emoji_html(BUY_CUSTOM_EMOJI_ID, BUY_EMOJI_FALLBACK)
SELL_EMOJI = custom_emoji_html(SELL_CUSTOM_EMOJI_ID, SELL_EMOJI_FALLBACK)

GREEN_T = custom_emoji_html(GREEN_T_EMOJI_ID, "🟢")
CLOCK = custom_emoji_html(CLOCK_EMOJI_ID, "⌚")
YELLOW_CIRCLE = custom_emoji_html(YELLOW_CIRCLE_EMOJI_ID, "🟡")
GLASSES_CIRCLE = custom_emoji_html(GLASSES_CIRCLE_EMOJI_ID, "😎")
# Фолбэк должен быть настоящим эмодзи (не "$" / "₽"), иначе Telegram
# вернёт ошибку ENTITY_TEXT_INVALID
DOLLAR = custom_emoji_html(DOLLAR_EMOJI_ID, "💲")
RUBLE = custom_emoji_html(RUBLE_EMOJI_ID, "💰")

# =========================
#      КУРСЫ ВАЛЮТ (/course)
# =========================

COURSE_ASSETS = [
    ("tether", "$"),
    ("the-open-network", "TON (Gram)"),
]

COURSE_CACHE_TTL_SECONDS = 60
COINGECKO_URL = "https://api.coingecko.com/api/v3/simple/price"

http_session: aiohttp.ClientSession | None = None
_course_cache: dict = {"data": None, "fetched_at": 0.0}

HOURLY_COURSE_INTERVAL_SECONDS = 60 * 60
COMMANDS_INTERVAL_SECONDS = 12 * 60 * 60  # каждые 12 часов
known_chats: set[int] = set()

NO_PREVIEW = LinkPreviewOptions(is_disabled=True)


def format_rub(value: float) -> str:
    if value >= 1000:
        return f"{value:,.0f}".replace(",", " ")
    if value >= 1:
        return f"{value:,.2f}".replace(",", " ")
    return f"{value:.4f}"


async def fetch_rates() -> dict | None:
    now = time.time()
    if _course_cache["data"] is not None and now - _course_cache["fetched_at"] < COURSE_CACHE_TTL_SECONDS:
        return _course_cache["data"]

    ids = ",".join(asset_id for asset_id, _ in COURSE_ASSETS)
    params = {"ids": ids, "vs_currencies": "rub"}

    try:
        async with http_session.get(
            COINGECKO_URL, params=params, timeout=aiohttp.ClientTimeout(total=10)
        ) as resp:
            if resp.status != 200:
                logger.warning(f"CoinGecko вернул статус {resp.status}")
                return None
            data = await resp.json()
    except Exception as e:
        logger.warning(f"Не удалось получить курс: {e}")
        return None

    _course_cache["data"] = data
    _course_cache["fetched_at"] = now
    return data


# =========================
#        ХРАНИЛИЩЕ
# =========================

@dataclass
class Order:
    order_id: int
    user_id: int
    display_name: str
    username: str | None
    raw_text: str
    order_type: str  # "buy" или "sell"
    created_at: float
    expire_at: float


orders: dict[int, dict[int, Order]] = {}
order_counters: dict[int, int] = {}
user_current_order: dict[int, dict[int, int]] = {}
check_snapshots: dict[int, "OrderedDict[int, list[int]]"] = {}


# =========================
#        РЕГУЛЯРКИ
# =========================

SET_ORDER_RE = re.compile(r'^/set(@\w+)?\s+(.+)$', re.IGNORECASE | re.DOTALL)
REMOVE_ORDER_RE = re.compile(r'^/-set(@\w+)?\s*$', re.IGNORECASE)
UP_RE = re.compile(r'^\.\s*up\s*$', re.IGNORECASE)
CHECK_RE = re.compile(r'^/check(@\w+)?\s*$', re.IGNORECASE)
TAKE_RE = re.compile(r'^/take(@\w+)?\s+(\d+)\s*$', re.IGNORECASE)
COURSE_RE = re.compile(r'^/course(@\w+)?\s*$', re.IGNORECASE)
COMMANDS_RE = re.compile(r'^/commands(@\w+)?\s*$', re.IGNORECASE)


def detect_order_type(text: str) -> str | None:
    candidates: list[tuple[int, str]] = []

    for kw in BUY_KEYWORDS:
        m = re.search(r'\b' + re.escape(kw) + r'\w*', text, re.IGNORECASE)
        if m:
            candidates.append((m.start(), "buy"))

    for kw in SELL_KEYWORDS:
        m = re.search(r'\b' + re.escape(kw) + r'\w*', text, re.IGNORECASE)
        if m:
            candidates.append((m.start(), "sell"))

    if not candidates:
        return None

    candidates.sort(key=lambda item: item[0])
    return candidates[0][1]


def escape_html(text: str) -> str:
    return text.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')


def mention_html(user_id: int, display_name: str) -> str:
    return f'<a href="tg://user?id={user_id}">{escape_html(display_name)}</a>'


def user_label(user_id: int, username: str | None, display_name: str) -> str:
    if username:
        safe_username = escape_html(username)
        return f'<a href="https://t.me/{safe_username}">@{safe_username}</a>'
    return escape_html(display_name)


def build_commands_message() -> str:
    """Красивое сообщение с инструкциями (как на скриншоте)."""
    return (
        f"{GREEN_T} Привет это криптик! Ваш личный помощник поиска ордеров только в этом чате {GREEN_T}\n\n"
        f"<b>Выставить ордер:</b>\n"
        f"<code>/set Куплю $ на 10000₽</code>\n"
        f"или\n"
        f"<code>/set Продам $ на 10000₽</code>\n\n"
        f"{CLOCK} Ордер действует 30 минут.\n\n"
        f"Продлить: <code>.up</code>\n"
        f"Удалить: <code>/-set</code>\n"
        f"Посмотреть ордера: <code>/check</code>\n\n"
        f"{YELLOW_CIRCLE} Нашли подходящий ордер? Ответьте на сообщение /check командой:\n"
        f"<code>/take 1</code> — где 1 это номер ордера.\n\n"
        f"🔄 Курс: <code>/course</code>\n\n"
        f"{GLASSES_CIRCLE} После того как нашли контрагента, проводите сделку через проверенного гаранта чата.\n\n"
        f"{RUBLE} Удачных сделок {DOLLAR}"
    )


dp = Dispatcher()


@dp.message.outer_middleware()
async def track_known_chats(handler, event: Message, data):
    if event.chat.type in ("group", "supergroup"):
        known_chats.add(event.chat.id)
    return await handler(event, data)


# =========================
#         ХЕНДЛЕРЫ
# =========================

@dp.message(Command("start", "help"))
async def cmd_start(message: Message):
    await message.answer(
        build_commands_message(),
        link_preview_options=NO_PREVIEW
    )


@dp.message(F.text.regexp(COMMANDS_RE.pattern, flags=re.IGNORECASE))
async def cmd_commands(message: Message):
    await message.answer(
        build_commands_message(),
        link_preview_options=NO_PREVIEW
    )


@dp.message(F.text.regexp(SET_ORDER_RE.pattern, flags=re.IGNORECASE | re.DOTALL))
async def set_order(message: Message):
    match = SET_ORDER_RE.match(message.text.strip())
    if not match:
        return

    raw_text = match.group(2).strip()
    order_type = detect_order_type(raw_text)

    if order_type is None:
        await message.reply(
            "Не понял, покупка это или продажа 🤔\n"
            "Добавь в текст одно из слов:\n"
            f"Покупка: {', '.join(BUY_KEYWORDS)}\n"
            f"Продажа: {', '.join(SELL_KEYWORDS)}",
            link_preview_options=NO_PREVIEW
        )
        return

    chat_id = message.chat.id
    user = message.from_user
    now = time.time()

    existing_id = user_current_order.get(chat_id, {}).get(user.id)
    if existing_id is not None:
        orders.get(chat_id, {}).pop(existing_id, None)

    order_id = order_counters.get(chat_id, 1)
    order_counters[chat_id] = order_id + 1

    orders.setdefault(chat_id, {})[order_id] = Order(
        order_id=order_id,
        user_id=user.id,
        display_name=user.full_name,
        username=user.username,
        raw_text=raw_text,
        order_type=order_type,
        created_at=now,
        expire_at=now + ORDER_TTL_SECONDS,
    )
    user_current_order.setdefault(chat_id, {})[user.id] = order_id

    emoji = BUY_EMOJI if order_type == "buy" else SELL_EMOJI
    label = "Покупка" if order_type == "buy" else "Продажа"
    await message.reply(
        f"Ордер добавлен ✅ {emoji} {label}\nАктуален 30 минут. Продлить: <code>.up</code>",
        link_preview_options=NO_PREVIEW
    )


@dp.message(F.text.regexp(REMOVE_ORDER_RE.pattern, flags=re.IGNORECASE))
async def remove_order(message: Message):
    chat_id = message.chat.id
    user_id = message.from_user.id

    order_id = user_current_order.get(chat_id, {}).pop(user_id, None)
    if order_id is not None:
        orders.get(chat_id, {}).pop(order_id, None)
        await message.reply("Ордер удалён ❌", link_preview_options=NO_PREVIEW)
    else:
        await message.reply("У тебя нет активного ордера.", link_preview_options=NO_PREVIEW)


@dp.message(F.text.regexp(UP_RE.pattern, flags=re.IGNORECASE))
async def extend_order(message: Message):
    chat_id = message.chat.id
    user_id = message.from_user.id

    order_id = user_current_order.get(chat_id, {}).get(user_id)
    order = orders.get(chat_id, {}).get(order_id) if order_id is not None else None

    if order is None or order.expire_at <= time.time():
        await message.reply("У тебя нет активного ордера для продления.", link_preview_options=NO_PREVIEW)
        return

    order.expire_at = time.time() + ORDER_TTL_SECONDS
    await message.reply("Ордер продлён ✅ ещё на 30 минут.", link_preview_options=NO_PREVIEW)


@dp.message(F.text.regexp(CHECK_RE.pattern, flags=re.IGNORECASE))
async def check_orders(message: Message):
    chat_id = message.chat.id
    now = time.time()

    chat_orders = orders.get(chat_id, {})
    active = [o for o in chat_orders.values() if o.expire_at > now]

    if not active:
        await message.reply("Активных ордеров нет.", link_preview_options=NO_PREVIEW)
        return

    active.sort(key=lambda o: o.created_at)

    lines = [
        "<b>Актуальные ордера тут!</b>",
        f"{BUY_EMOJI} - Покупка",
        f"{SELL_EMOJI} - Продажа",
        "",
    ]

    snapshot_ids: list[int] = []
    for i, order in enumerate(active, start=1):
        emoji = BUY_EMOJI if order.order_type == "buy" else SELL_EMOJI
        label = user_label(order.user_id, order.username, order.display_name)
        if i > 1:
            lines.append("")
        lines.append(f"{i}. {emoji} {label} - {escape_html(order.raw_text)}")
        snapshot_ids.append(order.order_id)

    sent = await message.answer(
        "\n".join(lines),
        link_preview_options=NO_PREVIEW
    )

    chat_snapshots = check_snapshots.setdefault(chat_id, OrderedDict())
    chat_snapshots[sent.message_id] = snapshot_ids
    if len(chat_snapshots) > MAX_CHECK_SNAPSHOTS_PER_CHAT:
        chat_snapshots.popitem(last=False)


@dp.message(F.text.regexp(TAKE_RE.pattern, flags=re.IGNORECASE))
async def take_order(message: Message):
    match = TAKE_RE.match(message.text.strip())
    if not match:
        return

    if message.reply_to_message is None:
        await message.reply(
            "Команду /take нужно отправлять в ответ на сообщение со списком ордеров (/check).",
            link_preview_options=NO_PREVIEW
        )
        return

    chat_id = message.chat.id
    reply_to_id = message.reply_to_message.message_id
    snapshot = check_snapshots.get(chat_id, {}).get(reply_to_id)

    if snapshot is None:
        await message.reply(
            "Это не то сообщение со списком ордеров, или список устарел. "
            "Сделай новый /check и отвечай /take уже на него.",
            link_preview_options=NO_PREVIEW
        )
        return

    number = int(match.group(2))
    if number < 1 or number > len(snapshot):
        await message.reply(f"Нет ордера под номером {number} в этом списке.", link_preview_options=NO_PREVIEW)
        return

    order_id = snapshot[number - 1]
    order = orders.get(chat_id, {}).get(order_id)

    if order is None or order.expire_at <= time.time():
        await message.reply("Этот ордер уже неактуален (взят или истёк).", link_preview_options=NO_PREVIEW)
        return

    taker = message.from_user
    if taker.id == order.user_id:
        await message.reply("Нельзя взять свой же ордер 🙂", link_preview_options=NO_PREVIEW)
        return

    orders.get(chat_id, {}).pop(order_id, None)
    if user_current_order.get(chat_id, {}).get(order.user_id) == order_id:
        del user_current_order[chat_id][order.user_id]

    creator_mention = user_label(order.user_id, order.username, order.display_name)
    taker_mention = user_label(taker.id, taker.username, taker.full_name)

    await message.reply(
        f"{creator_mention} ваш ордер взял {taker_mention}\n"
        "Внимание, проводите сделку через проверенных гарантов чата!\n"
        "Будьте аккуратнее и удачи вам!",
        link_preview_options=NO_PREVIEW
    )


def build_course_lines(data: dict) -> list[str] | None:
    lines = ["<b>Актуальный курс:</b>"]
    for asset_id, label in COURSE_ASSETS:
        entry = data.get(asset_id)
        if not entry or "rub" not in entry:
            continue
        lines.append(f"1 {label} = {format_rub(entry['rub'])}₽")

    if len(lines) == 1:
        return None

    return lines


@dp.message(F.text.regexp(COURSE_RE.pattern, flags=re.IGNORECASE))
async def show_course(message: Message):
    data = await fetch_rates()
    if not data:
        await message.reply("Не получилось получить курс, попробуй чуть позже.", link_preview_options=NO_PREVIEW)
        return

    lines = build_course_lines(data)
    if lines is None:
        await message.reply("Не получилось получить курс, попробуй чуть позже.", link_preview_options=NO_PREVIEW)
        return

    await message.reply("\n".join(lines), link_preview_options=NO_PREVIEW)


# =========================
#      ФОНОВАЯ ОЧИСТКА
# =========================

async def cleanup_task():
    while True:
        await asyncio.sleep(CLEANUP_INTERVAL_SECONDS)
        now = time.time()

        for chat_id, chat_orders in list(orders.items()):
            for order_id, order in list(chat_orders.items()):
                if order.expire_at <= now:
                    del chat_orders[order_id]
                    current = user_current_order.get(chat_id, {})
                    if current.get(order.user_id) == order_id:
                        del current[order.user_id]
                    logger.info(f"Ордер {order_id} в чате {chat_id} истёк")
            if not chat_orders:
                del orders[chat_id]


async def hourly_course_task(bot: Bot):
    while True:
        await asyncio.sleep(HOURLY_COURSE_INTERVAL_SECONDS)

        data = await fetch_rates()
        lines = build_course_lines(data) if data else None
        if lines is None:
            logger.warning("Почасовой курс: не удалось получить данные, пропускаю рассылку")
            continue

        text = "\n".join(lines)
        for chat_id in list(known_chats):
            try:
                await bot.send_message(chat_id, text, link_preview_options=NO_PREVIEW)
            except Exception as e:
                logger.warning(f"Не удалось отправить курс в чат {chat_id}: {e}")


async def commands_broadcast_task(bot: Bot):
    """Каждые 12 часов отправляет инструкцию во все известные чаты."""
    while True:
        await asyncio.sleep(COMMANDS_INTERVAL_SECONDS)

        text = build_commands_message()
        for chat_id in list(known_chats):
            try:
                await bot.send_message(chat_id, text, link_preview_options=NO_PREVIEW)
            except Exception as e:
                logger.warning(f"Не удалось отправить /commands в чат {chat_id}: {e}")


# =========================
#           MAIN
# =========================

async def main():
    global http_session

    if BOT_TOKEN == "ВСТАВЬ_СЮДА_ТОКЕН_ОТ_BOTFATHER":
        raise RuntimeError(
            "Укажи токен бота: через переменную окружения BOT_TOKEN "
            "или прямо в коде вместо заглушки."
        )

    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    http_session = aiohttp.ClientSession()
    asyncio.create_task(cleanup_task())
    asyncio.create_task(hourly_course_task(bot))
    asyncio.create_task(commands_broadcast_task(bot))

    logger.info("Бот запущен, слушаю сообщения...")
    try:
        await dp.start_polling(bot)
    finally:
        await http_session.close()


if __name__ == "__main__":
    asyncio.run(main())
