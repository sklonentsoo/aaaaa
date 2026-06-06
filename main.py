import asyncio
import logging
import os
import random
from datetime import datetime

from aiogram import Bot, Dispatcher, html, F
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, 
    InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton, ChatMemberUpdated
)
from aiogram.client.default import DefaultBotProperties
from pydantic_settings import BaseSettings, SettingsConfigDict
import aiosqlite
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# ==========================================
# КОНФИГУРАЦИЯ И НАСТРОЙКИ (ИЗ .ENV ПАНЕЛИ)
# ==========================================
class Settings(BaseSettings):
    bot_token: str
    owner_id: int  
    global_req_channel_id: int  

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

config = Settings()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_NAME = os.path.join(BASE_DIR, "giveaway_factory.db")
GLOBAL_REQ_LINK = "https://t.me/givedoom"

logging.basicConfig(level=logging.INFO)
bot = Bot(token=config.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher(storage=MemoryStorage())
scheduler = AsyncIOScheduler()

# ==========================================
# СОСТОЯНИЯ ДЛЯ ПОШАГОВОГО ФОРМИРОВАНИЯ (FSM)
# ==========================================
class BotAdminStates(StatesGroup):
    broadcast = State()

class ResourceStates(StatesGroup):
    wait_for_chat = State()

class CreationStates(StatesGroup):
    gv_type = State()
    winners_count = State()
    name = State()
    text = State()
    edit_or_continue = State()
    media = State()
    end_time = State()
    req_refs = State()
    target_chat = State()
    req_chat = State()
    final_screen = State()

# ==========================================
# ИНИЦИАЛИЗАЦИЯ И СТРУКТУРА ТАБЛИЦ БД
# ==========================================
async def init_db():
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                is_admin INTEGER DEFAULT 0
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS resources (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                owner_id INTEGER,
                chat_id INTEGER UNIQUE,
                title TEXT,
                type TEXT
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS giveaways (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                creator_id INTEGER,
                name TEXT,
                text TEXT,
                winners_count INTEGER,
                media_id TEXT,
                media_type TEXT,
                end_time TEXT,
                target_chat_id INTEGER,
                req_chat_id INTEGER,
                req_refs INTEGER DEFAULT 0,
                type TEXT, 
                is_active INTEGER DEFAULT 0 
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
        await db.execute("""
            CREATE TABLE IF NOT EXISTS referrals (
                giveaway_id INTEGER,
                referrer_id INTEGER,
                referral_id INTEGER,
                PRIMARY KEY (giveaway_id, referral_id)
            )
        """)
        await db.execute("INSERT OR IGNORE INTO users (user_id, is_admin) VALUES (?, 1)", (config.owner_id,))
        await db.commit()

# Проверка подписки на твой канал @givedoom
async def check_global_sub(user_id: int) -> bool:
    if user_id == config.owner_id:
        return True
    try:
        member = await bot.get_chat_member(chat_id=config.global_req_channel_id, user_id=user_id)
        return member.status in ["member", "administrator", "creator"]
    except Exception:
        return False

def get_sub_inline_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📢 Подписаться на Doom", url=GLOBAL_REQ_LINK)],
        [InlineKeyboardButton(text="🔄 Проверить подписку", callback_data="check_global_sub")]
    ])

# Главные Reply-кнопки внизу экрана
def main_reply_kb():
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="Розыгрыши"), KeyboardButton(text="Новый розыгрыш")],
        [KeyboardButton(text="Добавить канал( где проводить )"), KeyboardButton(text="Добавить группу ( где проводить )")]
    ], resize_keyboard=True)

# ==========================================
# ОБРАБОТКА СТАРТА И РЕФЕРАЛЬНЫХ ПЕРЕХОДОВ
# ==========================================
@dp.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    user_id = message.from_user.id
    
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (user_id,))
        await db.commit()

    # Считывание реф-ссылки для карточки участия
    args = message.text.split()
    if len(args) > 1 and args[1].startswith("gv_"):
        try:
            parts = args[1].split("_")
            gv_id = int(parts[1])
            ref_id = int(parts[2]) if len(parts) > 2 else None
            await show_contest_card(message, gv_id, ref_id)
            return
        except ValueError: pass

    if not await check_global_sub(user_id):
        await message.answer("⚠️ Для использования бота вы должны быть подписаны на официальный канал Doom!", reply_markup=get_sub_inline_kb())
        return

    await send_welcome_msg(message)

async def send_welcome_msg(message: Message):
    await message.answer(
        "Добро пожаловать в бота для проведения розыгрышей от Doom.\n\n"
        "Бот умеет запускать розыгрыши среди участников одного или нескольких каналов Телеграма и самостоятельно выбирать победителей в назначенное время.\n\n"
        "<b>Команды бота:</b>\n"
        "/create - создать розыгрыш\n"
        "/events - все розыгрыши.",
        reply_markup=main_reply_kb()
    )

@dp.callback_query(F.data == "check_global_sub")
async def callback_check_global_sub(callback: CallbackQuery):
    if await check_global_sub(callback.from_user.id):
        await callback.message.delete()
        await send_welcome_msg(callback.message)
    else:
        await callback.answer("❌ Вы всё еще не подписались на канал Doom!", show_alert=True)

# ==========================================
# ДОБАВЛЕНИЕ КАНАЛОВ И ГРУПП ИЗ МЕНЮ
# ==========================================
@dp.message(F.text.in_(["Добавить канал( где проводить )", "Добавить группу ( где проводить )"]))
async def add_resource_main(message: Message, state: FSMContext):
    if not await check_global_sub(message.from_user.id):
        await message.answer("⚠️ Нет подписки на канал Doom!", reply_markup=get_sub_inline_kb())
        return
    r_type = "channel" if "канал" in message.text else "group"
    await message.answer(f"Чтобы привязать ресурс, добавьте бота в ваш(у) канал/группу <b>как администратора</b>, а затем <b>перешлите любой пост</b> оттуда в этот чат.")
    await state.set_state(ResourceStates.wait_for_chat)
    await state.update_data(res_type=r_type)

@dp.message(ResourceStates.wait_for_chat)
async def process_resource_forward(message: Message, state: FSMContext):
    data = await state.get_data()
    await state.clear()
    target = message.forward_from_chat if message.forward_from_chat else (message.chat if message.chat.id != message.from_user.id else None)
    
    if not target:
        await message.answer("❌ Перешлите реальный пост из канала/группы.")
        return
    try:
        m = await bot.get_chat_member(chat_id=target.id, user_id=bot.id)
        if m.status != "administrator": raise Exception()
    except Exception:
        await message.answer("❌ Бот не админ в этом ресурсе или у него нет к нему доступа.")
        return

    async with aiosqlite.connect(DB_NAME) as db:
        try:
            await db.execute("INSERT INTO resources (owner_id, chat_id, title, type) VALUES (?, ?, ?, ?)", (message.from_user.id, target.id, target.title, data["res_type"]))
            await db.commit()
            await message.answer(f"✅ Ресурс <b>{target.title}</b> успешно привязан!")
        except Exception:
            await message.answer("⚠️ Этот ресурс уже привязан.")

# ==========================================
# ПОШАГОВЫЙ КОНСТРУКТОР РОЗЫГРЫША
# ==========================================
@dp.message(F.text == "Новый розыгрыш")
@dp.message(Command("create"))
async def start_creation_wizard(message: Message, state: FSMContext):
    if not await check_global_sub(message.from_user.id): return
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Обычный (Только подписка)", callback_data="wizard_type_regular")],
        [InlineKeyboardButton(text="Реферальный (Билеты за друзей)", callback_data="wizard_type_tickets")]
    ])
    await message.answer("Шаг 1: Выберите тип розыгрыша:", reply_markup=kb)
    await state.set_state(CreationStates.gv_type)

@dp.callback_query(CreationStates.gv_type)
async def proc_wizard_type(callback: CallbackQuery, state: FSMContext):
    await state.update_data(type=callback.data.split("_")[2])
    await callback.message.answer("Шаг 2: Введите количество победителей (например: 1 или 5):")
    await state.set_state(CreationStates.winners_count)
    await callback.answer()

@dp.message(CreationStates.winners_count)
async def proc_wizard_winners(message: Message, state: FSMContext):
    if not message.text.isdigit() or int(message.text) < 1:
        await message.answer("Введите число!")
        return
    await state.update_data(winners_count=int(message.text))
    await message.answer("Шаг 3: Введите название розыгрыша (чтобы было удобно управлять им в боте):")
    await state.set_state(CreationStates.name)

@dp.message(CreationStates.name)
async def proc_wizard_name(message: Message, state: FSMContext):
    await state.update_data(name=message.text)
    await message.answer("Шаг 4: Введите текст подробного описания розыгрыша:")
    await state.set_state(CreationStates.text)

@dp.message(CreationStates.text)
async def proc_wizard_text(message: Message, state: FSMContext):
    await state.update_data(text=message.text)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✏️ Редактировать текст", callback_data="w_edit")],
        [InlineKeyboardButton(text="➡️ Продолжить", callback_data="w_continue")]
    ])
    await message.answer("Текст получен. Выберите действие:", reply_markup=kb)
    await state.set_state(CreationStates.edit_or_continue)

@dp.callback_query(CreationStates.edit_or_continue)
async def proc_wizard_edit_decision(callback: CallbackQuery, state: FSMContext):
    if callback.data == "w_edit":
        await callback.message.answer("Введите текст описания заново:")
        await state.set_state(CreationStates.text)
    else:
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Пропустить медиа", callback_data="w_skip_media")]])
        await callback.message.answer("Шаг 5: Хотите добавить картинку/gif/видео для розыгрыша? Отправьте файл или нажмите кнопку:", reply_markup=kb)
        await state.set_state(CreationStates.media)
    await callback.answer()

@dp.message(CreationStates.media)
async def proc_wizard_media(message: Message, state: FSMContext):
    if message.photo: await state.update_data(media_id=message.photo[-1].file_id, media_type="photo")
    elif message.video: await state.update_data(media_id=message.video.file_id, media_type="video")
    elif message.animation: await state.update_data(media_id=message.animation.file_id, media_type="animation")
    else:
        await message.answer("Отправьте медиафайл или нажмите кнопку выше!")
        return
    await ask_wizard_time(message, state)

@dp.callback_query(CreationStates.media, F.data == "w_skip_media")
async def proc_wizard_skip_media(callback: CallbackQuery, state: FSMContext):
    await state.update_data(media_id=None, media_type="none")
    await ask_wizard_time(callback.message, state)
    await callback.answer()

async def ask_wizard_time(message: Message, state: FSMContext):
    await message.answer("Шаг 6: Укажите время завершения в формате <code>ГГГГ-ММ-ДД ЧЧ:ММ</code> (или напишите 'нет' для ручного закрытия):")
    await state.set_state(CreationStates.end_time)

@dp.message(CreationStates.end_time)
async def proc_wizard_time_val(message: Message, state: FSMContext):
    t_str = None
    if message.text.lower() != "нет":
        try: datetime.strptime(message.text, "%Y-%m-%d %H:%M"); t_str = message.text
        except ValueError:
            await message.answer("Неверный формат! Используйте пример: 2026-12-31 18:00 или напишите 'нет'")
            return
    await state.update_data(end_time=t_str)
    await message.answer("Шаг 7: Сколько друзей нужно пригласить пользователю, чтобы участвовать в розыгрыше? (Введите число, 0 — если не нужно):")
    await state.set_state(CreationStates.req_refs)

@dp.message(CreationStates.req_refs)
async def proc_wizard_refs(message: Message, state: FSMContext):
    if not message.text.isdigit():
        await message.answer("Введите число!")
        return
    await state.update_data(req_refs=int(message.text))
    await show_target_chat_selection(message, state)

async def show_target_chat_selection(message: Message, state: FSMContext):
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT chat_id, title FROM resources WHERE owner_id = ?", (message.from_user.id,)) as c: rows = await c.fetchall()
    
    buttons = [[InlineKeyboardButton(text=r[1], callback_data=f"wtarget_{r[0]}")] for r in rows]
    buttons.append([InlineKeyboardButton(text="➕ Добавить канал/группу", callback_data="wadd_target")])
    await message.answer("Шаг 8: В каком канале или группе проводить (публиковать) розыгрыш?", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
    await state.set_state(CreationStates.target_chat)

@dp.callback_query(CreationStates.target_chat, F.data == "wadd_target")
async def inline_add_target(callback: CallbackQuery):
    await callback.message.answer("Перешлите пост из нового канала/группы (бот должен быть админом):")
    await callback.answer()

@dp.message(CreationStates.target_chat)
async def inline_target_msg_catch(message: Message, state: FSMContext):
    target = message.forward_from_chat if message.forward_from_chat else None
    if not target:
        await message.answer("Перешлите пост для добавления или выберите канал из кнопок выше.")
        return
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("INSERT OR IGNORE INTO resources (owner_id, chat_id, title, type) VALUES (?, ?, ?, 'channel')", (message.from_user.id, target.id, target.title))
        await db.commit()
    await message.answer(f"✅ Ресурс {target.title} добавлен в систему.")
    await show_target_chat_selection(message, state)

@dp.callback_query(CreationStates.target_chat, F.data.startswith("wtarget_"))
async def proc_wizard_target_chosen(callback: CallbackQuery, state: FSMContext):
    await state.update_data(target_chat_id=int(callback.data.split("_")[1]))
    await show_req_chat_selection(callback.message, callback.from_user.id, state)
    await callback.answer()

async def show_req_chat_selection(message: Message, user_id: int, state: FSMContext):
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT chat_id, title FROM resources WHERE owner_id = ?", (user_id,)) as c: rows = await c.fetchall()
    buttons = [[InlineKeyboardButton(text=r[1], callback_data=f"wreq_{r[0]}")] for r in rows]
    buttons.append([InlineKeyboardButton(text="➕ Добавить канал для подписки", callback_data="wadd_req")])
    await message.answer("Шаг 9: Выберите или добавьте канал, где нужна обязательная подписка для участия:", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
    await state.set_state(CreationStates.req_chat)

@dp.callback_query(CreationStates.req_chat, F.data == "wadd_req")
async def inline_add_req(callback: CallbackQuery):
    await callback.message.answer("Перешлите пост из канала обязательной подписки (бот должен быть админом):")
    await callback.answer()

@dp.message(CreationStates.req_chat)
async def inline_req_msg_catch(message: Message, state: FSMContext):
    target = message.forward_from_chat if message.forward_from_chat else None
    if not target: return
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("INSERT OR IGNORE INTO resources (owner_id, chat_id, title, type) VALUES (?, ?, ?, 'channel')", (message.from_user.id, target.id, target.title))
        await db.commit()
    await show_req_chat_selection(message, message.from_user.id, state)

@dp.callback_query(CreationStates.req_chat, F.data.startswith("wreq_"))
async def proc_wizard_req_chosen(callback: CallbackQuery, state: FSMContext):
    await state.update_data(req_chat_id=int(callback.data.split("_")[1]))
    data = await state.get_data()
    
    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute(
            """INSERT INTO giveaways (creator_id, name, text, winners_count, media_id, media_type, end_time, target_chat_id, req_chat_id, req_refs, type, is_active)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)""",
            (callback.from_user.id, data["name"], data["text"], data["winners_count"], data["media_id"], data["media_type"], data["end_time"], data["target_chat_id"], data["req_chat_id"], data["req_refs"], data["type"])
        )
        gv_id = cursor.lastrowid
        await db.commit()

    await show_final_decision_screen(callback.message, gv_id, state)
    await callback.answer()

async def show_final_decision_screen(message: Message, gv_id: int, state: FSMContext):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🚀 Запустить розыгрыш", callback_data=f"launch_{gv_id}")],
        [InlineKeyboardButton(text="⚙️ Настройки розыгрыша", callback_data=f"options_{gv_id}")]
    ])
    await message.answer("Розыгрыш успешно настроен! Выберите дальнейшее действие:", reply_markup=kb)
    await state.set_state(CreationStates.final_screen)

# ==========================================
# ПУБЛИКАЦИЯ В КАНАЛ И НАСТРОЙКИ (КОНТРОЛЬ)
# ==========================================
@dp.callback_query(F.data.startswith("launch_"))
async def launch_giveaway(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    gv_id = int(callback.data.split("_")[1])
    
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("UPDATE giveaways SET is_active = 1 WHERE id = ?", (gv_id,))
        await db.commit()
        async with db.execute("SELECT name, text, winners_count, media_id, media_type, target_chat_id, end_time FROM giveaways WHERE id = ?", (gv_id,)) as c: gv = await c.fetchone()
        
    bot_info = await bot.get_me()
    post_text = f"🎁 <b>НОВЫЙ РОЗЫГРЫШ: {gv[0]}</b>\n\n{gv[1]}\n\n👥 Количество победителей: {gv[2]}"
    post_kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🎉 Принять участие", url=f"https://t.me/{bot_info.username}?start=gv_{gv_id}")]])
    
    try:
        if gv[4] == "photo": await bot.send_photo(gv[5], gv[3], caption=post_text, reply_markup=post_kb)
        elif gv[4] == "video": await bot.send_video(gv[5], gv[3], caption=post_text, reply_markup=post_kb)
        elif gv[4] == "animation": await bot.send_animation(gv[5], gv[3], caption=post_text, reply_markup=post_kb)
        else: await bot.send_message(gv[5], post_text, reply_markup=post_kb)
        await callback.message.answer("🚀 Розыгрыш запущен и опубликован в целевой канал/группу!")
    except Exception:
        await callback.message.answer("⚠️ Ошибка публикации! Проверьте, дали ли вы боту права админа в канале публикации.")

    if gv[6]:
        scheduler.add_job(finish_giveaway_job, 'date', run_date=datetime.strptime(gv[6], "%Y-%m-%d %H:%M"), args=[gv_id], id=f"job_{gv_id}")
    await callback.answer()

@dp.callback_query(F.data.startswith("options_"))
async def show_gv_options(callback: CallbackQuery):
    gv_id = int(callback.data.split("_")[1])
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 Статистика", callback_data=f"opt_stats_{gv_id}")],
        [InlineKeyboardButton(text="🛑 Закончить досрочно", callback_data=f"opt_stop_{gv_id}")],
        [InlineKeyboardButton(text="🔄 Перевыпустить победителя", callback_data=f"opt_reroll_{gv_id}")],
        [InlineKeyboardButton(text="🗑️ Удалить розыгрыш", callback_data=f"opt_delete_{gv_id}")]
    ])
    await callback.message.edit_text("⚙️ Меню настроек и управления розыгрышем:", reply_markup=kb)
    await callback.answer()

@dp.callback_query(F.data.startswith("opt_"))
async def handle_options(callback: CallbackQuery):
    parts = callback.data.split("_")
    action, gv_id = parts[1], int(parts[2])
    
    if action == "stats":
        async with aiosqlite.connect(DB_NAME) as db:
            async with db.execute("SELECT COUNT(*) FROM participants WHERE giveaway_id = ?", (gv_id,)) as c: count = (await c.fetchone())[0]
        await callback.message.answer(f"📊 Статистика розыгрыша:\nВсего уникальных участников зарегистрировано: {count}")
    elif action == "stop":
        try: scheduler.remove_job(f"job_{gv_id}") 
        except Exception: pass
        await finish_giveaway_job(gv_id)
        await callback.message.answer("🛑 Розыгрыш остановлен досрочно, итоги подведены.")
    elif action == "reroll":
        await finish_giveaway_job(gv_id, is_reroll=True)
        await callback.message.answer("🔄 Победители успешно перевыпущены!")
    elif action == "delete":
        async with aiosqlite.connect(DB_NAME) as db:
            await db.execute("DELETE FROM giveaways WHERE id = ?", (gv_id,))
            await db.commit()
        await callback.message.edit_text("🗑️ Розыгрыш полностью удален из базы.")
    await callback.answer()

# ==========================================
# ПРОСМОТР СПИСКА РОЗЫГРЫШЕЙ ПОЛЬЗОВАТЕЛЯ
# ==========================================
@dp.message(F.text == "Розыгрыши")
@dp.message(Command("events"))
async def list_events(message: Message):
    if not await check_global_sub(message.from_user.id): return
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT id, name, is_active FROM giveaways WHERE creator_id = ?", (message.from_user.id,)) as c: rows = await c.fetchall()
    if not rows:
        await message.answer("У вас пока нет созданных розыгрышей.")
        return
    buttons = [[InlineKeyboardButton(text=f"{'🟢' if r[2]==1 else '🟡'} {r[1]}", callback_data=f"options_{r[0]}")] for r in rows]
    await message.answer("Список ваших розыгрышей (нажмите на нужный для управления):", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))

# ==========================================
# РАБОТА С КАРТОЧКОЙ ДЛЯ ПОДПИСЧИКОВ В КАНАЛАХ
# ==========================================
async def show_contest_card(message: Message, gv_id: int, ref_id: int | None = None):
    user_id = message.from_user.id
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT name, text, req_chat_id, req_refs, is_active FROM giveaways WHERE id = ?", (gv_id,)) as c: gv = await c.fetchone()
    if not gv or gv[4] == 0:
        await message.answer("Розыгрыш завершен или не существует.")
        return
        
    if ref_id and ref_id != user_id:
        async with aiosqlite.connect(DB_NAME) as db:
            try:
                await db.execute("INSERT INTO referrals (giveaway_id, referrer_id, referral_id) VALUES (?, ?, ?)", (gv_id, ref_id, user_id))
                await db.commit()
                await bot.send_message(ref_id, "🔔 Кто-то перешел по вашей ссылке в конкурс!")
            except Exception: pass

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Проверить подписку и Участвовать", callback_data=f"subcheck_{gv_id}")],
        [InlineKeyboardButton(text="🔗 Получить мою реф. ссылку", callback_data=f"reflink_{gv_id}")]
    ])
    await message.answer(f"🎁 <b>Конкурс: {gv[0]}</b>\n\n{gv[1]}\n\nДля участия необходимо подписаться на спонсорский канал и нажать кнопку ниже.", reply_markup=kb)

@dp.callback_query(F.data.startswith("subcheck_"))
async def exec_participant_check(callback: CallbackQuery):
    gv_id = int(callback.data.split("_")[1])
    user_id = callback.from_user.id
    
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT req_chat_id, req_refs FROM giveaways WHERE id = ?", (gv_id,)) as c: gv = await c.fetchone()
        
    try:
        m = await bot.get_chat_member(chat_id=gv[0], user_id=user_id)
        if m.status not in ["member", "administrator", "creator"]: raise Exception()
    except Exception:
        await callback.answer("❌ Вы не подписаны на обязательный канал спонсора!", show_alert=True)
        return

    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT COUNT(*) FROM referrals WHERE giveaway_id = ? AND referrer_id = ?", (gv_id, user_id)) as c: r_count = (await c.fetchone())[0]
        
    if gv[1] > 0 and r_count < gv[1]:
        await callback.answer(f"❌ Не хватает рефералов! У вас {r_count}/{gv[1]}.", show_alert=True)
        return

    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("INSERT OR REPLACE INTO participants (giveaway_id, user_id, tickets) VALUES (?, ?, ?)", (gv_id, user_id, 1 + r_count))
        await db.commit()
    await callback.answer("🎉 Вы успешно зарегистрированы среди участников!", show_alert=True)

@dp.callback_query(F.data.startswith("reflink_"))
async def give_reflink(callback: CallbackQuery):
    gv_id = int(callback.data.split("_")[1])
    bot_info = await bot.get_me()
    await callback.message.answer(f"🔗 Ваша ссылка для приглашения друзей:\n<code>https://t.me/{bot_info.username}?start=gv_{gv_id}_{callback.from_user.id}</code>")
    await callback.answer()

# Автоматический мониторинг отписок пользователей со спонсорских каналов
@dp.chat_member()
async def on_left_event(update: ChatMemberUpdated):
    if update.old_chat_member.status in ["member", "administrator", "creator"] and update.new_chat_member.status in ["left", "kicked"]:
        user_id = update.from_user.id
        async with aiosqlite.connect(DB_NAME) as db:
            async with db.execute("SELECT id, req_refs FROM giveaways WHERE req_chat_id = ? AND is_active = 1", (update.chat.id,)) as c: gvs = await c.fetchall()
            for gv in gvs:
                async with db.execute("SELECT referrer_id FROM referrals WHERE giveaway_id = ? AND referral_id = ?", (gv[0], user_id)) as r_c: row = await r_c.fetchone()
                if row:
                    await db.execute("DELETE FROM referrals WHERE giveaway_id = ? AND referral_id = ?", (gv[0], user_id))
                    async with db.execute("SELECT COUNT(*) FROM referrals WHERE giveaway_id = ? AND referrer_id = ?", (gv[0], row[0])) as check_c: count = (await check_c.fetchone())[0]
                    if gv[1] > 0 and count < gv[1]:
                        await db.execute("DELETE FROM participants WHERE giveaway_id = ? AND user_id = ?", (gv[0], row[0]))
                    else:
                        await db.execute("UPDATE participants SET tickets = ? WHERE giveaway_id = ? AND user_id = ?", (1+count, gv[0], row[0]))
            await db.commit()

# Подведение итогов по шедулеру/вручную
async def finish_giveaway_job(gv_id: int, is_reroll: bool = False):
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT winners_count, target_chat_id, name FROM giveaways WHERE id = ?", (gv_id,)) as c: gv = await c.fetchone()
        async with db.execute("SELECT user_id, tickets FROM participants WHERE giveaway_id = ?", (gv_id,)) as p_c: participants = await p_c.fetchall()
    if not participants: return
    pool = []
    for u_id, t in participants: pool.extend([u_id] * t)
    winners = []
    while len(winners) < min(gv[0], len(set(pool))):
        w = random.choice(pool)
        if w not in winners: winners.append(w)
    mentions = []
    for w in winners:
        try: chat = await bot.get_chat(w); mentions.append(chat.mention_html() if chat.username else f"ID: {w}")
        except Exception: mentions.append(f"Участник {w}")
    try:
        await bot.send_message(gv[1], f"{'🔄 ПЕРЕВЫБОР ПРАВИЛ!' if is_reroll else '🏆 ИТОГИ РОЗЫГРЫША!'} В розыгрыше '{gv[2]}' победили: {', '.join(mentions)} 🎉")
    except Exception: pass
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("UPDATE giveaways SET is_active = 0 WHERE id = ?", (gv_id,))
        await db.commit()

# ==========================================
# ПАНЕЛЬ СОЗДАТЕЛЯ БОТА (ТОЛЬКО ДЛЯ ТЕБЯ)
# ==========================================
@dp.message(Command("admin"))
async def super_admin_panel(message: Message):
    if message.from_user.id != config.owner_id: return
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT COUNT(*) FROM users") as c1: u_count = (await c1.fetchone())[0]
        async with db.execute("SELECT COUNT(DISTINCT chat_id) FROM resources") as c2: r_count = (await c2.fetchone())[0]
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="📢 Сделать рассылку рекламы", callback_data="owner_broadcast")]])
    await message.answer(f"👑 <b>Панель создателя платформы Doom:</b>\n\n📊 Зарегистрировано авторов/юзеров: {u_count}\n🖥 Всего каналов/групп в системе: {r_count}", reply_markup=kb)

@dp.callback_query(F.data == "owner_broadcast")
async def start_owner_broadcast(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != config.owner_id: return
    await callback.message.answer("Введите рекламный текст для рассылки всем пользователям конструктора:")
    await state.set_state(BotAdminStates.broadcast)
    await callback.answer()

@dp.message(BotAdminStates.broadcast)
async def exec_owner_broadcast(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("🚀 Рассылка началась...")
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute("SELECT user_id FROM users") as c: users = await c.fetchall()
    s = 0
    for u in users:
        try: await bot.send_message(u[0], message.text); s += 1; await asyncio.sleep(0.05)
        except Exception: pass
    await message.answer(f"✅ Успешно доставлено: {s}/{len(users)}")

# ==========================================
# ИНИЦИАЛИЗАЦИЯ И ПОЛЛИНГ
# ==========================================
async def main():
    await init_db()
    scheduler.start()
    await dp.start_polling(bot)

if __name__ == "__main__":
    try: asyncio.run(main())
    except KeyboardInterrupt: print("Бот выключен.")