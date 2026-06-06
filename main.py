import asyncio
import logging
from datetime import datetime
import random
import os

from aiogram import Bot, Dispatcher, html, F
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, 
    InlineKeyboardButton, ChatMemberUpdated
)
from pydantic_settings import BaseSettings, SettingsConfigDict
import aiosqlite
from apscheduler.schedulers.asyncio import AsyncioScheduler

# ==========================================
# ВАЛИДАЦИЯ И ЗАГРУЗКА ПЕРЕМЕННЫХ ОКРУЖЕНИЯ
# ==========================================
class Settings(BaseSettings):
    bot_token: str
    channel_id: int
    channel_link: str
    admin_ids_str: str

    @property
    def admin_ids(self) -> list[int]:
        return [int(x.strip()) for x in self.admin_ids_str.split(",") if x.strip().isdigit()]

    # Пытаемся читать из .env файла, если он есть, иначе берем из системы (хостинга)
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

config = Settings()

# Путь к БД делаем абсолютным, чтобы на хостингах она не терялась при смене директорий
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_NAME = os.path.join(BASE_DIR, "giveaway_bot.db")

logging.basicConfig(level=logging.INFO)
bot = Bot(token=config.bot_token, parse_mode=ParseMode.HTML)
dp = Dispatcher(storage=MemoryStorage())
scheduler = AsyncioScheduler()

# ==========================================
# СОСТОЯНИЯ FSM (МАШИНА СОСТОЯНИЙ)
# ==========================================
class AdminStates(StatesGroup):
    broadcast_text = State()
    gv_text = State()
    gv_type = State()
    gv_refs_count = State()
    gv_time = State()

# ==========================================
# ИНИЦИАЛИЗАЦИЯ БАЗЫ ДАННЫХ
# ==========================================
async def init_db():
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                referrer_id INTEGER,
                is_admin INTEGER DEFAULT 0
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS giveaways (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                text TEXT,
                type TEXT,
                required_refs INTEGER DEFAULT 0,
                end_time TEXT,
                is_active INTEGER DEFAULT 1
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS referrals (
                referrer_id INTEGER,
                referral_id INTEGER,
                giveaway_id INTEGER,
                PRIMARY KEY (referral_id, giveaway_id)
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS participants (
                giveaway_id INTEGER,
                user_id INTEGER,
                tickets INTEGER DEFAULT 1,
                PRIMARY KEY (giveaway_id, user_id)
            )
        """)
        for admin_id in config.admin_ids:
            await db.execute(
                "INSERT OR IGNORE INTO users (user_id, is_admin) VALUES (?, 1)", 
                (admin_id,)
            )
        await db.commit()

# ==========================================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ==========================================
async def check_subscription(user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(chat_id=config.channel_id, user_id=user_id)
        return member.status in ["member", "administrator", "creator"]
    except Exception:
        return False

async def get_active_giveaway():
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute(
            "SELECT id, text, type, required_refs, end_time FROM giveaways WHERE is_active = 1 LIMIT 1"
        ) as cursor:
            row = await cursor.fetchone()
            if row:
                return {"id": row[0], "text": row[1], "type": row[2], "required_refs": row[3], "end_time": row[4]}
    return None

async def get_user_refs(user_id: int, giveaway_id: int) -> int:
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute(
            "SELECT COUNT(*) FROM referrals WHERE referrer_id = ? AND giveaway_id = ?", 
            (user_id, giveaway_id)
        ) as cursor:
            res = await cursor.fetchone()
            return res[0] if res else 0

# ==========================================
# ИНЛАЙН-КЛАВИАТУРЫ
# ==========================================
def user_menu_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎉 Участвовать", callback_data="gv_join")],
        [InlineKeyboardButton(text="🔗 Моя ссылка", callback_data="gv_link"),
         InlineKeyboardButton(text="📊 Статистика", callback_data="gv_stats")]
    ])

def admin_menu_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Создать конкурс", callback_data="adm_create")],
        [InlineKeyboardButton(text="📢 Рассылка", callback_data="adm_broadcast")],
        [InlineKeyboardButton(text="🛑 Остановить текущий розыгрыш", callback_data="adm_stop_gv")]
    ])

# ==========================================
# ХЭНДЛЕРЫ ПОЛЬЗОВАТЕЛЕЙ
# ==========================================
@dp.message(CommandStart())
async def cmd_start(message: Message):
    user_id = message.from_user.id
    args = message.text.split()
    
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT user_id FROM users WHERE user_id = ?", (user_id,)) as cursor:
            is_new = await cursor.fetchone() is None

        if is_new:
            referrer_id = None
            if len(args) > 1 and args[1].startswith("ref_"):
                try:
                    possible_ref = int(args[1].split("_")[1])
                    if possible_ref != user_id:
                        referrer_id = possible_ref
                except ValueError:
                    pass
            
            await db.execute("INSERT INTO users (user_id, referrer_id) VALUES (?, ?)", (user_id, referrer_id))
            await db.commit()

            if referrer_id:
                gv = await get_active_giveaway()
                if gv and await check_subscription(user_id):
                    try:
                        await db.execute(
                            "INSERT OR IGNORE INTO referrals (referrer_id, referral_id, giveaway_id) VALUES (?, ?, ?)",
                            (referrer_id, user_id, gv["id"])
                        )
                        await db.commit()
                        await bot.send_message(
                            referrer_id, 
                            f"🎉 Новый реферал! Пользователь {html.bold(message.from_user.full_name)} зашёл по вашей ссылке."
                        )
                    except Exception:
                        pass

    gv = await get_active_giveaway()
    if not gv:
        await message.answer("Привет! Активных конкурсов прямо сейчас нет, но следи за обновлениями!")
        return

    await message.answer(
        f"👋 Привет! Рады видеть тебя.\n\nТекущий конкурс:\n{gv['text']}\n\nНажми кнопку ниже, чтобы проверить условия и зарегистрироваться!",
        reply_markup=user_menu_kb()
    )

@dp.callback_query(F.data == "gv_link")
async def process_gv_link(callback: CallbackQuery):
    gv = await get_active_giveaway()
    if not gv:
        await callback.answer("Конкурс не найден или завершен.", show_alert=True)
        return
        
    bot_info = await bot.get_me()
    ref_link = f"https://t.me/{bot_info.username}?start=ref_{callback.from_user.id}"
    
    await callback.message.answer(
        f"🔗 Ваша реферальная ссылка для конкурса:\n<code>{ref_link}</code>\n\n"
        f"Отправляйте друзьям. Чтобы вам засчитался балл, они должны запустить бота и подписаться на канал!"
    )
    await callback.answer()

@dp.callback_query(F.data == "gv_stats")
async def process_gv_stats(callback: CallbackQuery):
    gv = await get_active_giveaway()
    if not gv:
        await callback.answer("Активных конкурсов нет.", show_alert=True)
        return
        
    user_id = callback.from_user.id
    refs = await get_user_refs(user_id, gv["id"])
    
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute(
            "SELECT tickets FROM participants WHERE giveaway_id = ? AND user_id = ?", 
            (gv["id"], user_id)
        ) as cursor:
            row = await cursor.fetchone()
            is_part = row is not None
            tickets = row[0] if is_part else 0

    mode_text = ""
    if gv["type"] == "regular":
        mode_text = "Режим: Обычный (нужна только подписка)"
    elif gv["type"] == "ref_fixed":
        mode_text = f"Режим: Реферальный порог (нужно пригласить {gv['required_refs']} друзей)"
    elif gv["type"] == "ref_tickets":
        mode_text = "Режим: Реферальные билеты (больше друзей = больше шансов)"

    status_text = f"✅ Участвуете ({tickets} билетов)" if is_part else "❌ Не участвуете"

    await callback.message.answer(
        f"📊 Твоя статистика в текущем конкурсе:\n\n"
        f"ℹ️ {mode_text}\n"
        f"👥 Приглашено друзей: {html.bold(str(refs))}\n"
        f"🏆 Статус: {html.bold(status_text)}"
    )
    await callback.answer()

@dp.callback_query(F.data == "gv_join")
async def process_gv_join(callback: CallbackQuery):
    gv = await get_active_giveaway()
    if not gv:
        await callback.answer("Конкурс уже завершен.", show_alert=True)
        return

    user_id = callback.from_user.id

    if not await check_subscription(user_id):
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Подписаться на канал", url=config.channel_link)],
            [InlineKeyboardButton(text="🔄 Проверить подписку", callback_data="gv_join")]
        ])
        await callback.message.answer("⚠️ Вы не подписаны на канал! Подпишитесь и нажмите кнопку заново.", reply_markup=kb)
        await callback.answer()
        return

    refs = await get_user_refs(user_id, gv["id"])
    tickets = 1

    if gv["type"] == "ref_fixed":
        if refs < gv["required_refs"]:
            await callback.message.answer(
                f"❌ Недостаточно рефералов. У вас {refs}/{gv['required_refs']}.\n"
                f"Воспользуйтесь кнопкой '🔗 Моя ссылка' чтобы пригласить друзей."
            )
            await callback.answer()
            return
    elif gv["type"] == "ref_tickets":
        tickets = 1 + refs

    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            "INSERT OR REPLACE INTO participants (giveaway_id, user_id, tickets) VALUES (?, ?, ?)",
            (gv["id"], user_id, tickets)
        )
        await db.commit()

    await callback.message.answer("🎉 Отлично! Вы успешно зарегистрированы среди участников конкурса!")
    await callback.answer()

# ==========================================
# МОНИТОРИНГ ОТПИСОК
# ==========================================
@dp.chat_member()
async def on_chat_member_update(update: ChatMemberUpdated):
    if update.chat.id != config.channel_id:
        return

    if update.old_chat_member.status in ["member", "administrator", "creator"] and \
       update.new_chat_member.status in ["left", "kicked"]:
        
        user_id = update.from_user.id
        gv = await get_active_giveaway()
        if not gv:
            return

        async with aiosqlite.connect(DB_NAME) as db:
            async with db.execute(
                "SELECT referrer_id FROM referrals WHERE referral_id = ? AND giveaway_id = ?",
                (user_id, gv["id"])
            ) as cursor:
                row = await cursor.fetchone()
            
            if row:
                referrer_id = row[0]
                await db.execute(
                    "DELETE FROM referrals WHERE referral_id = ? AND giveaway_id = ?",
                    (user_id, gv["id"])
                )
                
                if gv["type"] == "ref_tickets":
                    await db.execute(
                        "UPDATE participants SET tickets = MAX(1, tickets - 1) WHERE user_id = ? AND giveaway_id = ?",
                        (referrer_id, gv["id"])
                    )
                elif gv["type"] == "ref_fixed":
                    new_refs = await get_user_refs(referrer_id, gv["id"])
                    if new_refs < gv["required_refs"]:
                        await db.execute(
                            "DELETE FROM participants WHERE user_id = ? AND giveaway_id = ?",
                            (referrer_id, gv["id"])
                        )
                        try:
                            await bot.send_message(
                                referrer_id, 
                                "⚠️ Внимание! Ваш реферал отписался от канала. Вам больше не хватает рефералов, вы выбыли из списка участников!"
                            )
                        except Exception: pass
                
                await db.commit()
                try:
                    if gv["type"] != "ref_fixed":
                        await bot.send_message(
                            referrer_id, 
                            "⚠️ Твой реферал отписался от канала. Баланс рефералов/билетов уменьшен."
                        )
                except Exception: pass

# ==========================================
# ХЭНДЛЕРЫ АДМИНИСТРАТОРА
# ==========================================
@dp.message(Command("admin"))
async def cmd_admin(message: Message):
    if message.from_user.id not in config.admin_ids:
        return
    await message.answer("⚙️ Добро пожаловать в Админ-Панель бота:", reply_markup=admin_menu_kb())

@dp.callback_query(F.data == "adm_broadcast")
async def adm_broadcast_start(callback: CallbackQuery, state: FSMContext):
    await callback.message.answer("Введите текст сообщения для рассылки всем пользователям бота:")
    await state.set_state(AdminStates.broadcast_text)
    await callback.answer()

@dp.message(AdminStates.broadcast_text)
async def adm_broadcast_exec(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("🚀 Рассылка запущена...")
    
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT user_id FROM users") as cursor:
            users = await cursor.fetchall()
    
    success = 0
    for user in users:
        try:
            await bot.send_message(user[0], message.text)
            success += 1
            await asyncio.sleep(0.05)
        except Exception: pass
            
    await message.answer(f"✅ Рассылка завершена. Успешно отправлено: {success}/{len(users)}")

@dp.callback_query(F.data == "adm_create")
async def adm_gv_start(callback: CallbackQuery, state: FSMContext):
    if await get_active_giveaway():
        await callback.message.answer("⚠️ Уже есть запущенный активный конкурс! Сначала завершите его.")
        await callback.answer()
        return

    await callback.message.answer("Шаг 1: Введите текст конкурса (Призы, описание):")
    await state.set_state(AdminStates.gv_text)
    await callback.answer()

@dp.message(AdminStates.gv_text)
async def adm_gv_type(message: Message, state: FSMContext):
    await state.update_data(text=message.text)
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Обычный (Только подписка)", callback_data="type_regular")],
        [InlineKeyboardButton(text="Реф: Фиксированный порог", callback_data="type_ref_fixed")],
        [InlineKeyboardButton(text="Реф: Билеты (Шансы)", callback_data="type_ref_tickets")]
    ])
    await message.answer("Шаг 2: Выберите режим проведения конкурса:", reply_markup=kb)
    await state.set_state(AdminStates.gv_type)

@dp.callback_query(AdminStates.gv_type, F.data.startswith("type_"))
async def adm_gv_type_chosen(callback: CallbackQuery, state: FSMContext):
    gv_type = callback.data.split("_")[1] if "fixed" not in callback.data and "tickets" not in callback.data else callback.data.replace("type_", "")
    await state.update_data(type=gv_type)
    
    if gv_type == "ref_fixed":
        await callback.message.answer("Шаг 2.5: Сколько рефералов должен пригласить юзер для участия? (Введите число):")
        await state.set_state(AdminStates.gv_refs_count)
    else:
        await state.update_data(required_refs=0)
        await callback.message.answer("Шаг 3: Напишите время завершения в формате ГГГГ-ММ-ДД ЧЧ:ММ (или 'нет' для ручного закрытия):")
        await state.set_state(AdminStates.gv_time)
    await callback.answer()

@dp.message(AdminStates.gv_refs_count)
async def adm_gv_refs_handler(message: Message, state: FSMContext):
    if not message.text.isdigit():
        await message.answer("Пожалуйста, введите корректное число!")
        return
    await state.update_data(required_refs=int(message.text))
    await message.answer("Шаг 3: Напишите время завершения в формате ГГГГ-ММ-ДД ЧЧ:ММ (или 'нет' для ручного закрытия):")
    await state.set_state(AdminStates.gv_time)

@dp.message(AdminStates.gv_time)
async def adm_gv_final(message: Message, state: FSMContext):
    data = await state.get_data()
    await state.clear()
    
    end_time_str = None
    if message.text.lower() != "нет":
        try:
            datetime.strptime(message.text, "%Y-%m-%d %H:%M")
            end_time_str = message.text
        except ValueError:
            await message.answer("❌ Неверный формат даты! Конкурс создан в режиме ручного закрытия.")
    
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            "INSERT INTO giveaways (text, type, required_refs, end_time, is_active) VALUES (?, ?, ?, ?, 1)",
            (data["text"], data["type"], data["required_refs"], end_time_str)
        )
        await db.commit()
    
    if end_time_str:
        scheduler.add_job(
            finish_giveaway_job, 
            'date', 
            run_date=datetime.strptime(end_time_str, "%Y-%m-%d %H:%M"),
            id="active_gv_job"
        )

    await message.answer("🎉 Конкурс успешно запущен!")

# ==========================================
# ПОДВЕДЕНИЕ ИТОГОВ
# ==========================================
async def execute_draw(gv_id: int):
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT user_id, tickets FROM participants WHERE giveaway_id = ?", (gv_id,)) as cursor:
            rows = await cursor.fetchall()
            
        if not rows:
            return None
            
        pool = []
        for user_id, tickets in rows:
            pool.extend([user_id] * tickets)
            
        winner_id = random.choice(pool)
        await db.execute("UPDATE giveaways SET is_active = 0 WHERE id = ?", (gv_id,))
        await db.commit()
        return winner_id

async def finish_giveaway_job():
    gv = await get_active_giveaway()
    if gv:
        winner_id = await execute_draw(gv["id"])
        if winner_id:
            try:
                chat = await bot.get_chat(winner_id)
                winner_text = chat.mention_html() if chat.username else f"ID: {winner_id}"
            except Exception:
                winner_text = f"Участник с ID {winner_id}"
                
            await bot.send_message(
                config.channel_id, 
                f"🏆 <b>Итоги конкурса подошли к концу!</b>\n\nПобедитель определен: {winner_text} 🎉\nПоздравляем!"
            )
            try:
                await bot.send_message(winner_id, "🎉 Поздравляем! Ты выиграл в конкурсе! Свяжись с администрацией.")
            except Exception: pass
        else:
            await bot.send_message(config.channel_id, "⚠️ Конкурс завершен, но участников оказалось 0.")

@dp.callback_query(F.data == "adm_stop_gv")
async def adm_stop_gv_handler(callback: CallbackQuery):
    if callback.from_user.id not in config.admin_ids: return
    
    gv = await get_active_giveaway()
    if not gv:
        await callback.message.answer("Нет активных конкурсов для остановки.")
        await callback.answer()
        return
        
    try:
        scheduler.remove_job("active_gv_job")
    except Exception: pass
    
    await finish_giveaway_job()
    await callback.message.answer("🛑 Конкурс завершен, победитель выбран!")
    await callback.answer()

# ==========================================
# ЗАПУСК БОТА
# ==========================================
async def main():
    await init_db()
    scheduler.start()
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Бот выключен.")