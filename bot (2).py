import asyncio
import os
import re

from aiogram import Bot, Dispatcher, Router, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode, ChatMemberStatus
from aiogram.filters import CommandStart, Command, CommandObject, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
import asyncpg

# ─────────────────────────────────────────────
# НАСТРОЙКИ — задаются переменными окружения на Railway
# ─────────────────────────────────────────────

BOT_TOKEN = os.getenv("BOT_TOKEN", "8719901855:AAH_8TyTkO_fD5leaAsADBqZkxV2KTvwykg")

# ID администраторов — только они управляют офферами, делают рассылку, видят статистику.
# Узнать свой ID: напиши @userinfobot
ADMIN_IDS = set(
    int(uid.strip())
    for uid in os.getenv("ADMIN_IDS", "7507890517,8194805214").split(",")
    if uid.strip()
)

DATABASE_URL = os.getenv("DATABASE_URL")

# Канал для обязательной подписки.
# REQUIRED_CHANNEL — то, что понимает Telegram API:
#   публичный канал  -> "@my_channel"
#   приватный канал  -> числовой id "-1001234567890"
# CHANNEL_URL — ссылка для кнопки «Проверить подписку»:
#   публичный  -> "https://t.me/my_channel"
#   приватный  -> пригласительная ссылка "https://t.me/+AbCdEf..."
REQUIRED_CHANNEL = os.getenv("REQUIRED_CHANNEL", "@your_channel")
CHANNEL_URL = os.getenv("CHANNEL_URL", "https://t.me/your_channel")

# Заполнится автоматически при старте (для генерации ссылок ?start=КОД)
BOT_USERNAME = "your_bot"

# Тексты
MSG_NOT_FOUND = "❌ Сбой, такое бывает. Просто перейди по ссылке из Instagram ещё раз."
MSG_SUCCESS_URL = "✅ Вот твой материал:\n\n{url}"
MSG_SUCCESS_FILE = "✅ Вот твой материал 👇"
MSG_NOT_SUBSCRIBED = "❌ Подписки пока не вижу. Подпишись на канал и нажми кнопку ещё раз 🙌"

# ─────────────────────────────────────────────
# БАЗА ДАННЫХ
# ─────────────────────────────────────────────

db_pool: asyncpg.Pool | None = None


async def init_db():
    """Создаёт пул, таблицы users и offers, при первом запуске наполняет офферы."""
    global db_pool
    db_pool = await asyncpg.create_pool(DATABASE_URL)
    async with db_pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id   BIGINT PRIMARY KEY,
                username  TEXT,
                full_name TEXT,
                joined_at TIMESTAMPTZ DEFAULT now()
            );
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS offers (
                code           TEXT PRIMARY KEY,
                image_file_id  TEXT,
                text           TEXT NOT NULL,
                material_type  TEXT NOT NULL,   -- 'url' | 'file'
                material_value TEXT NOT NULL,   -- ссылка ИЛИ file_id документа
                active         BOOLEAN DEFAULT TRUE,
                created_at     TIMESTAMPTZ DEFAULT now()
            );
        """)

        # Первичное наполнение — чтобы старые ссылки ?start=MATERIAL1.. сразу работали.
        # Картинок нет (image_file_id = NULL) — добавишь обложки через /newoffer.
        count = await conn.fetchval("SELECT count(*) FROM offers;")
        if count == 0:
            seed = [
                (
                    "MATERIAL1",
                    None,
                    (
                        "Состоянием легко управлять, если делать это через тело.\n\n"
                        "<b>Забирайте гайд-практикум «Аптечка состояний»</b> для работы с разными "
                        "эмоциями: гнев, злость, страх, тревога, апатия, бессилие, грусть.\n\n"
                        "Внутри простые практики на 5–7 минут, которые быстро приводят в чувства, "
                        "когда эмоции накрывают.\n\n"
                        "<b>Чтобы получить гайд, подпишитесь на мой Телеграм-канал по кнопке ниже.</b>"
                    ),
                    "url",
                    "https://incomparable-custard-4d70d3.netlify.app/",
                ),
                ("MATERIAL2", None, "<b>Материал 2</b>\n\nПодпишись на канал, чтобы забрать.",
                 "url", "https://telegra.ph/Material-2-04-17"),
                ("MATERIAL3", None, "<b>Материал 3</b>\n\nПодпишись на канал, чтобы забрать.",
                 "url", "https://telegra.ph/MATERIAL-3-04-17"),
            ]
            await conn.executemany(
                "INSERT INTO offers (code, image_file_id, text, material_type, material_value) "
                "VALUES ($1, $2, $3, $4, $5);",
                seed,
            )


# --- users ---
async def save_user(user_id: int, username: str | None, full_name: str):
    await db_pool.execute("""
        INSERT INTO users (user_id, username, full_name)
        VALUES ($1, $2, $3)
        ON CONFLICT (user_id) DO UPDATE
            SET username = EXCLUDED.username, full_name = EXCLUDED.full_name;
    """, user_id, username, full_name)


async def get_all_user_ids() -> list[int]:
    rows = await db_pool.fetch("SELECT user_id FROM users;")
    return [r["user_id"] for r in rows]


async def get_user_count() -> int:
    return await db_pool.fetchval("SELECT count(*) FROM users;")


# --- offers ---
async def get_offer(code: str | None):
    if not code:
        return None
    return await db_pool.fetchrow(
        "SELECT * FROM offers WHERE code = $1 AND active = TRUE;", code
    )


async def list_offers():
    return await db_pool.fetch(
        "SELECT code, material_type FROM offers WHERE active = TRUE ORDER BY code;"
    )


async def upsert_offer(code, image, text, mtype, mvalue):
    await db_pool.execute("""
        INSERT INTO offers (code, image_file_id, text, material_type, material_value, active)
        VALUES ($1, $2, $3, $4, $5, TRUE)
        ON CONFLICT (code) DO UPDATE SET
            image_file_id  = EXCLUDED.image_file_id,
            text           = EXCLUDED.text,
            material_type  = EXCLUDED.material_type,
            material_value = EXCLUDED.material_value,
            active         = TRUE;
    """, code, image, text, mtype, mvalue)


async def delete_offer(code: str) -> bool:
    res = await db_pool.execute("DELETE FROM offers WHERE code = $1;", code)
    return res != "DELETE 0"


# ─────────────────────────────────────────────
# ХЕЛПЕРЫ
# ─────────────────────────────────────────────

def offer_keyboard(code: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Проверить подписку ↗", url=CHANNEL_URL)],
        [InlineKeyboardButton(text="Я подписана ✅", callback_data=f"check_sub:{code}")],
    ])


async def is_subscribed(bot: Bot, user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(REQUIRED_CHANNEL, user_id)
    except Exception:
        return False
    if member.status in (
        ChatMemberStatus.CREATOR,
        ChatMemberStatus.ADMINISTRATOR,
        ChatMemberStatus.MEMBER,
    ):
        return True
    if member.status == ChatMemberStatus.RESTRICTED:
        return bool(getattr(member, "is_member", False))
    return False


async def send_offer(bot: Bot, chat_id: int, code: str, offer):
    """Отправляет оффер: картинка (если есть) + текст + кнопки."""
    kb = offer_keyboard(code)
    text = offer["text"]
    image = offer["image_file_id"]

    if image:
        try:
            if len(text) <= 1024:
                await bot.send_photo(chat_id, image, caption=text, reply_markup=kb)
            else:
                await bot.send_photo(chat_id, image)
                await bot.send_message(chat_id, text, reply_markup=kb)
            return
        except Exception:
            pass  # если file_id картинки битый — отправим текстом
    await bot.send_message(chat_id, text, reply_markup=kb)


async def deliver_material(bot: Bot, chat_id: int, offer):
    """Выдаёт материал — ссылку или файл."""
    if offer["material_type"] == "file":
        await bot.send_document(chat_id, offer["material_value"], caption=MSG_SUCCESS_FILE)
    else:
        url = offer["material_value"]
        await bot.send_message(
            chat_id,
            MSG_SUCCESS_URL.format(url=url),
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔗 Открыть материал", url=url)]
            ]),
        )


# ─────────────────────────────────────────────
# FSM: мастер добавления оффера
# ─────────────────────────────────────────────

class NewOffer(StatesGroup):
    code = State()
    image = State()
    text = State()
    material = State()


router = Router()


@router.message(Command("cancel"), StateFilter("*"))
async def cmd_cancel(message: Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS:
        return
    if await state.get_state() is None:
        return
    await state.clear()
    await message.answer("Отменено. /newoffer — начать заново.")


@router.message(Command("newoffer"))
async def cmd_newoffer(message: Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS:
        return
    await state.set_state(NewOffer.code)
    await message.answer(
        "Шаг 1/4. Пришли КОД оффера (латиница / цифры / _ / -), например MATERIAL4.\n"
        "Это то, что идёт в ссылке ?start=КОД.\n\n/cancel — отмена"
    )


@router.message(StateFilter(NewOffer.code))
async def no_code(message: Message, state: FSMContext):
    code = (message.text or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_\-]{1,64}", code):
        await message.answer("Код — только латиница, цифры, _ или -, без пробелов. Пришли ещё раз или /cancel.")
        return
    await state.update_data(code=code)
    note = " (такой код уже есть — он перезапишется)" if await get_offer(code) else ""
    await state.set_state(NewOffer.image)
    await message.answer(
        f"Код: {code}{note}\n\n"
        "Шаг 2/4. Пришли картинку-обложку (фото).\n"
        "Если без картинки — напиши: нет"
    )


@router.message(StateFilter(NewOffer.image))
async def no_image(message: Message, state: FSMContext):
    if message.photo:
        await state.update_data(image=message.photo[-1].file_id)
    elif (message.text or "").strip().lower() in ("нет", "no", "-"):
        await state.update_data(image=None)
    else:
        await message.answer("Пришли фото или напиши «нет».")
        return
    await state.set_state(NewOffer.text)
    await message.answer(
        "Шаг 3/4. Пришли текст оффера.\n"
        "Можешь форматировать прямо в Telegram (жирный, курсив) — сохранится."
    )


@router.message(StateFilter(NewOffer.text))
async def no_text(message: Message, state: FSMContext):
    text = message.html_text if message.text else None
    if not text:
        await message.answer("Нужен текст сообщением. Пришли ещё раз или /cancel.")
        return
    await state.update_data(text=text)
    await state.set_state(NewOffer.material)
    await message.answer(
        "Шаг 4/4. Пришли материал:\n"
        "• ссылку — просто текстом (https://...)\n"
        "• или файл — пришли документ (PDF и т.п.)"
    )


@router.message(StateFilter(NewOffer.material))
async def no_material(message: Message, state: FSMContext):
    if message.document:
        mtype, mvalue = "file", message.document.file_id
    elif message.text and message.text.strip().startswith(("http://", "https://")):
        mtype, mvalue = "url", message.text.strip()
    else:
        await message.answer("Нужна ссылка (https://...) текстом или файл-документ. Пришли ещё раз или /cancel.")
        return

    data = await state.get_data()
    await upsert_offer(data["code"], data.get("image"), data["text"], mtype, mvalue)
    await state.clear()
    await message.answer(
        f"✅ Оффер «{data['code']}» сохранён.\n\n"
        f"Ссылка для запуска:\nhttps://t.me/{BOT_USERNAME}?start={data['code']}\n\n"
        f"Проверить, как выглядит: /testoffer {data['code']}"
    )


# ─────────────────────────────────────────────
# ХЭНДЛЕРЫ ПОЛЬЗОВАТЕЛЯ
# ─────────────────────────────────────────────

@router.message(CommandStart())
async def cmd_start(message: Message, command: CommandObject):
    user = message.from_user
    await save_user(user.id, user.username, user.full_name)

    offer = await get_offer(command.args)
    if not offer:
        await message.answer(MSG_NOT_FOUND)
        return
    await send_offer(message.bot, user.id, command.args, offer)


@router.callback_query(F.data.startswith("check_sub:"))
async def cb_check_sub(callback: CallbackQuery):
    code = callback.data.split(":", 1)[1]
    offer = await get_offer(code)
    if not offer:
        await callback.answer("Что-то пошло не так. Нажми /start ещё раз.", show_alert=True)
        return
    if not await is_subscribed(callback.bot, callback.from_user.id):
        await callback.answer(MSG_NOT_SUBSCRIBED, show_alert=True)
        return
    await callback.answer()
    await deliver_material(callback.bot, callback.from_user.id, offer)


# ─────────────────────────────────────────────
# АДМИН-КОМАНДЫ
# ─────────────────────────────────────────────

@router.message(Command("offers"))
async def cmd_offers(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        return
    rows = await list_offers()
    if not rows:
        await message.answer("Офферов пока нет. Добавить: /newoffer")
        return
    lines = "\n".join(f"• {r['code']} — {r['material_type']}" for r in rows)
    await message.answer(
        f"📦 Офферы ({len(rows)}):\n\n{lines}\n\n"
        "Посмотреть: /testoffer КОД\nУдалить: /deloffer КОД\nДобавить: /newoffer"
    )


@router.message(Command("testoffer"))
async def cmd_testoffer(message: Message, command: CommandObject):
    if message.from_user.id not in ADMIN_IDS:
        return
    code = (command.args or "").strip()
    offer = await get_offer(code)
    if not offer:
        await message.answer("Использование: /testoffer КОД\nСписок: /offers")
        return
    await send_offer(message.bot, message.from_user.id, code, offer)


@router.message(Command("deloffer"))
async def cmd_deloffer(message: Message, command: CommandObject):
    if message.from_user.id not in ADMIN_IDS:
        return
    code = (command.args or "").strip()
    if not code:
        await message.answer("Использование: /deloffer КОД")
        return
    ok = await delete_offer(code)
    await message.answer(f"🗑 Оффер «{code}» удалён." if ok else f"Оффер «{code}» не найден.")


@router.message(Command("stats"))
async def cmd_stats(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        return
    count = await get_user_count()
    await message.answer(f"📊 Всего пользователей: {count}")


@router.message(Command("broadcast"))
async def cmd_broadcast(message: Message):
    """
    /broadcast Текст — разошлёт текст всем
    Ответь на сообщение + /broadcast — перешлёт его всем
    """
    if message.from_user.id not in ADMIN_IDS:
        return

    if message.reply_to_message:
        broadcast_msg = message.reply_to_message
        text = None
    else:
        text = message.text.removeprefix("/broadcast").strip()
        broadcast_msg = None
        if not text:
            await message.answer(
                "Как использовать:\n"
                "• /broadcast Текст — разошлёт текст\n"
                "• Ответь на сообщение + /broadcast — перешлёт его всем"
            )
            return

    user_ids = await get_all_user_ids()
    sent, failed = 0, 0
    status = await message.answer(f"⏳ Рассылка: 0/{len(user_ids)}…")

    for i, uid in enumerate(user_ids, 1):
        try:
            if broadcast_msg:
                await broadcast_msg.copy_to(uid)
            else:
                await message.bot.send_message(uid, text)
            sent += 1
        except Exception:
            failed += 1

        if i % 50 == 0:
            try:
                await status.edit_text(f"⏳ Рассылка: {i}/{len(user_ids)}…")
            except Exception:
                pass

        await asyncio.sleep(0.04)  # ~25 msg/sec — безопасно для лимита Telegram

    await status.edit_text(
        f"✅ Рассылка завершена.\n"
        f"📨 Доставлено: {sent}\n"
        f"❌ Не доставлено: {failed}"
    )


# ─────────────────────────────────────────────
# ЗАПУСК
# ─────────────────────────────────────────────

async def main():
    global BOT_USERNAME
    await init_db()
    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))

    me = await bot.get_me()
    BOT_USERNAME = me.username

    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    print(f"Бот @{BOT_USERNAME} запущен ✅")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
