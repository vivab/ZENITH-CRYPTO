import asyncio
import logging
import os
import re
import time
from dataclasses import dataclass

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.types import Message

# =========================
#         НАСТРОЙКИ
# =========================

BOT_TOKEN = os.getenv("BOT_TOKEN", "ВСТАВЬ_СЮДА_ТОКЕН_ОТ_BOTFATHER")

BALANCE_TTL_SECONDS = 60 * 60      # через сколько сгорает баланс (1 час)
CLEANUP_INTERVAL_SECONDS = 60      # как часто чистим протухшие балансы
EXCLUDE_ORDER_AUTHOR = True        # не тегать автора ордера, даже если его диапазон подходит

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("balance_bot")

# =========================
#        ХРАНИЛИЩЕ
# =========================


@dataclass
class UserBalance:
    user_id: int
    display_name: str
    username: str | None
    min_amount: float
    max_amount: float
    expire_at: float
    rate_min: float | None = None
    rate_max: float | None = None
    note: str | None = None


# chat_id -> {user_id: UserBalance}
balances: dict[int, dict[int, UserBalance]] = {}

# =========================
#        РЕГУЛЯРКИ
# =========================

# +баланс 500-1000  /  +баланс 500,5-1000
SET_BALANCE_RE = re.compile(
    r'^\+\s*баланс\s+(\d+(?:[.,]\d+)?)\s*-\s*(\d+(?:[.,]\d+)?)\s*$',
    re.IGNORECASE,
)

# -баланс
REMOVE_BALANCE_RE = re.compile(r'^-\s*баланс\s*$', re.IGNORECASE)

# +курс 75-85
SET_RATE_RE = re.compile(
    r'^\+\s*курс\s+(\d+(?:[.,]\d+)?)\s*-\s*(\d+(?:[.,]\d+)?)\s*$',
    re.IGNORECASE,
)

# -курс
REMOVE_RATE_RE = re.compile(r'^-\s*курс\s*$', re.IGNORECASE)

# .Обновить
REFRESH_RE = re.compile(r'^\.\s*обновить\s*$', re.IGNORECASE)

# +Примечание Сбп Куар Грев (1-5 слов)
SET_NOTE_RE = re.compile(r'^\+\s*примечание\s+(.+)$', re.IGNORECASE)

# -Примечание
REMOVE_NOTE_RE = re.compile(r'^-\s*примечание\s*$', re.IGNORECASE)

MAX_NOTE_WORDS = 5

# 500/75, 500-75, 738.5/73, 7300/73,5, 500/75 сбп, 500-75 Куар и т.д.
# Разделитель может быть "/" или "-", после числа может идти любой текст (сбп, грев, карта...)
ORDER_RE = re.compile(r'(\d+(?:[.,]\d+)?)\s*[/\-]\s*(\d+(?:[.,]\d+)?)')


def parse_number(raw: str) -> float:
    return float(raw.replace(',', '.'))


def mention_html(user_id: int, display_name: str) -> str:
    safe_name = (
        display_name.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
    )
    return f'<a href="tg://user?id={user_id}">{safe_name}</a>'


dp = Dispatcher()

# =========================
#         ХЕНДЛЕРЫ
# =========================


@dp.message(Command("start", "help"))
async def cmd_start(message: Message):
    await message.answer(
        "Привет! Я слежу за ордерами в чате.\n\n"
        "➕ <code>+баланс 500-1000</code> — задать диапазон суммы. "
        "Как только придёт подходящий ордер (например 600/79) — я тебя тегну.\n"
        "➖ <code>-баланс</code> — удалить диапазон суммы.\n\n"
        "➕ <code>+курс 75-85</code> — задать диапазон курса (ордер должен попадать и в сумму, и в курс).\n"
        "➖ <code>-курс</code> — удалить диапазон курса.\n\n"
        "➕ <code>+примечание Сбп Куар Грев</code> — до 5 слов пометки (необязательно).\n"
        "➖ <code>-примечание</code> — удалить примечание.\n\n"
        "🔄 <code>.Обновить</code> — сбросить таймер автоудаления ещё на 1 час.\n\n"
        "📋 <code>/баланс</code> — список всех активных балансов в чате.\n\n"
        "Баланс (и всё, что к нему привязано) автоматически удаляется через 1 час "
        "после установки, если не обновить его через <code>.Обновить</code>."
    )


@dp.message(F.text.regexp(SET_BALANCE_RE.pattern, flags=re.IGNORECASE))
async def set_balance(message: Message):
    match = SET_BALANCE_RE.match(message.text.strip())
    if not match:
        return

    low_raw, high_raw = match.groups()
    low, high = parse_number(low_raw), parse_number(high_raw)
    if low > high:
        low, high = high, low

    chat_id = message.chat.id
    user = message.from_user

    balances.setdefault(chat_id, {})[user.id] = UserBalance(
        user_id=user.id,
        display_name=user.full_name,
        username=user.username,
        min_amount=low,
        max_amount=high,
        expire_at=time.time() + BALANCE_TTL_SECONDS,
    )

    await message.reply(
        f"Баланс установлен ✅ ({low:g}-{high:g})\nАвтоудаление через 1 час."
    )


@dp.message(F.text.regexp(REMOVE_BALANCE_RE.pattern, flags=re.IGNORECASE))
async def remove_balance(message: Message):
    chat_id = message.chat.id
    user_id = message.from_user.id
    chat_balances = balances.get(chat_id, {})

    if chat_balances.pop(user_id, None) is not None:
        await message.reply("Баланс удалён ❌")
    else:
        await message.reply("У тебя не было установленного баланса.")


def get_active_balance(chat_id: int, user_id: int) -> UserBalance | None:
    chat_balances = balances.get(chat_id, {})
    bal = chat_balances.get(user_id)
    if bal is None or bal.expire_at <= time.time():
        return None
    return bal


NO_BALANCE_HINT = "Сначала задай диапазон суммы: <code>+баланс 500-1000</code>"


@dp.message(F.text.regexp(SET_RATE_RE.pattern, flags=re.IGNORECASE))
async def set_rate(message: Message):
    match = SET_RATE_RE.match(message.text.strip())
    if not match:
        return

    chat_id = message.chat.id
    user_id = message.from_user.id
    bal = get_active_balance(chat_id, user_id)
    if bal is None:
        await message.reply(NO_BALANCE_HINT)
        return

    low_raw, high_raw = match.groups()
    low, high = parse_number(low_raw), parse_number(high_raw)
    if low > high:
        low, high = high, low

    bal.rate_min, bal.rate_max = low, high
    await message.reply(f"Курс установлен ✅ ({low:g}-{high:g})")


@dp.message(F.text.regexp(REMOVE_RATE_RE.pattern, flags=re.IGNORECASE))
async def remove_rate(message: Message):
    chat_id = message.chat.id
    user_id = message.from_user.id
    bal = get_active_balance(chat_id, user_id)

    if bal is None or bal.rate_min is None:
        await message.reply("У тебя не было установленного курса.")
        return

    bal.rate_min, bal.rate_max = None, None
    await message.reply("Курс удалён ❌")


@dp.message(F.text.regexp(SET_NOTE_RE.pattern, flags=re.IGNORECASE))
async def set_note(message: Message):
    match = SET_NOTE_RE.match(message.text.strip())
    if not match:
        return

    chat_id = message.chat.id
    user_id = message.from_user.id
    bal = get_active_balance(chat_id, user_id)
    if bal is None:
        await message.reply(NO_BALANCE_HINT)
        return

    raw_note = match.group(1).strip()
    words = raw_note.split()
    if len(words) > MAX_NOTE_WORDS:
        await message.reply(f"Примечание слишком длинное, максимум {MAX_NOTE_WORDS} слов.")
        return

    note = ", ".join(words)
    bal.note = note
    await message.reply(f"Примечание установлено ✅ ({note})")


@dp.message(F.text.regexp(REMOVE_NOTE_RE.pattern, flags=re.IGNORECASE))
async def remove_note(message: Message):
    chat_id = message.chat.id
    user_id = message.from_user.id
    bal = get_active_balance(chat_id, user_id)

    if bal is None or bal.note is None:
        await message.reply("У тебя не было установленного примечания.")
        return

    bal.note = None
    await message.reply("Примечание удалено ❌")


@dp.message(F.text.regexp(REFRESH_RE.pattern, flags=re.IGNORECASE))
async def refresh_balance(message: Message):
    chat_id = message.chat.id
    user_id = message.from_user.id
    bal = get_active_balance(chat_id, user_id)

    if bal is None:
        await message.reply(NO_BALANCE_HINT)
        return

    bal.expire_at = time.time() + BALANCE_TTL_SECONDS
    await message.reply("Таймер обновлён ✅ Ещё 1 час.")


@dp.message(F.text.regexp(r'^/баланс(@\w+)?\s*$', flags=re.IGNORECASE))
async def list_balances(message: Message):
    chat_id = message.chat.id
    chat_balances = balances.get(chat_id, {})
    now = time.time()

    active = {uid: b for uid, b in chat_balances.items() if b.expire_at > now}

    if not active:
        await message.reply("Активных балансов нет.")
        return

    lines = ["<b>Актуальные балансы:</b>"]
    for uid, b in sorted(active.items(), key=lambda item: item[1].min_amount):
        label = f"@{b.username}" if b.username else b.display_name

        parts = [f"{b.min_amount:g}-{b.max_amount:g}"]
        if b.rate_min is not None:
            parts.append(f"курс {b.rate_min:g}-{b.rate_max:g}")
        if b.note:
            parts.append(b.note)

        lines.append(f"{mention_html(uid, label)} {' | '.join(parts)}")

    await message.reply("\n".join(lines))


@dp.message(F.text.regexp(ORDER_RE.pattern))
async def handle_order(message: Message):
    text = message.text.strip()

    # игнорируем сами команды бота — некоторые из них (например "+курс 75-85")
    # тоже подходят под шаблон "число-число" и иначе будут приняты за ордер
    if (
        SET_BALANCE_RE.match(text)
        or REMOVE_BALANCE_RE.match(text)
        or SET_RATE_RE.match(text)
        or REMOVE_RATE_RE.match(text)
        or REFRESH_RE.match(text)
        or SET_NOTE_RE.match(text)
        or REMOVE_NOTE_RE.match(text)
    ):
        return

    chat_id = message.chat.id
    chat_balances = balances.get(chat_id)
    if not chat_balances:
        return

    now = time.time()
    matches = ORDER_RE.findall(text)
    if not matches:
        return

    author_id = message.from_user.id if message.from_user else None
    matched_users: dict[int, UserBalance] = {}

    for amount_raw, rate_raw in matches:
        amount = parse_number(amount_raw)
        rate = parse_number(rate_raw)
        for uid, bal in list(chat_balances.items()):
            if bal.expire_at <= now:
                continue
            if EXCLUDE_ORDER_AUTHOR and uid == author_id:
                continue
            if not (bal.min_amount <= amount <= bal.max_amount):
                continue
            # если пользователь задал курс — ордер должен попадать и в этот диапазон тоже
            if bal.rate_min is not None and not (bal.rate_min <= rate <= bal.rate_max):
                continue
            matched_users[uid] = bal

    if not matched_users:
        return

    mentions = " ".join(
        mention_html(uid, bal.display_name) for uid, bal in matched_users.items()
    )
    await message.reply(mentions)


# =========================
#      ФОНОВАЯ ОЧИСТКА
# =========================


async def cleanup_task():
    while True:
        await asyncio.sleep(CLEANUP_INTERVAL_SECONDS)
        now = time.time()
        for chat_id, chat_balances in list(balances.items()):
            for uid, bal in list(chat_balances.items()):
                if bal.expire_at <= now:
                    del chat_balances[uid]
                    logger.info(f"Баланс пользователя {uid} в чате {chat_id} истёк")
            if not chat_balances:
                del balances[chat_id]


# =========================
#           MAIN
# =========================


async def main():
    if BOT_TOKEN == "ВСТАВЬ_СЮДА_ТОКЕН_ОТ_BOTFATHER":
        raise RuntimeError(
            "Укажи токен бота: через переменную окружения BOT_TOKEN "
            "или прямо в коде вместо заглушки."
        )

    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    asyncio.create_task(cleanup_task())

    logger.info("Бот запущен, слушаю сообщения...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
