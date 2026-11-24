import asyncio
import logging
import os
import sqlite3
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.exceptions import TelegramAPIError
from aiohttp import web

# --- НАСТРОЙКИ ---
BOT_TOKEN = os.getenv("BOT_TOKEN")
TEACHER_SECRET_CODE = os.getenv("TEACHER_SECRET_CODE")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN environment variable is required")
if not TEACHER_SECRET_CODE:
    raise RuntimeError("TEACHER_SECRET_CODE environment variable is required")

logging.basicConfig(level=logging.INFO)
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# --- БАЗА ДАННЫХ ---
DB_PATH = os.getenv(
    "DB_PATH",
    os.path.join(os.getenv("RAILWAY_VOLUME_MOUNT_PATH", "."), "leader_edu_groups.db"),
)
DB_DIR = os.path.dirname(DB_PATH)
if DB_DIR:
    os.makedirs(DB_DIR, exist_ok=True)

conn = sqlite3.connect(DB_PATH, check_same_thread=False)
cursor = conn.cursor()

cursor.execute("""
CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,
    full_name TEXT,
    role TEXT,
    group_name TEXT,
    coins INTEGER DEFAULT 0,
    teacher_id INTEGER
)
""")

cursor.execute("""
CREATE TABLE IF NOT EXISTS shop (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT,
    price INTEGER
)
""")
cursor.execute("""
CREATE TABLE IF NOT EXISTS exit_requests (
    student_id INTEGER PRIMARY KEY,
    teacher_id INTEGER NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)
""")
conn.commit()

cursor.execute("SELECT COUNT(*) FROM shop")
if cursor.fetchone()[0] == 0:
    cursor.executemany("INSERT INTO shop (title, price) VALUES (?, ?)", [
        ("🍭 Сладкий приз", 30),
        ("🎟 +5 баллов к экзамену", 100),
        ("👕 Фирменная футболка LEADER", 500)
    ])
    conn.commit()

# --- СОСТОЯНИЯ (FSM) ---
class RegState(StatesGroup):
    waiting_for_teacher_code = State()
    waiting_for_teacher_name = State()
    waiting_for_teacher_group = State()
    waiting_for_student_name = State()
    waiting_for_teacher_choice = State()

class GiveCoinsState(StatesGroup):
    waiting_for_student_id = State()
    waiting_for_amount = State()

# --- КЛАВИАТУРЫ ---
def get_rank(coins: int) -> str:
    if coins < 50: return "🥉 Новичок"
    elif coins < 150: return "🥈 Исследователь"
    elif coins < 350: return "🥇 Мастер"
    elif coins < 700: return "💎 Эксперт"
    return "👑 Легенда LEADER"

def student_kb():
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="👤 Мой профиль"), KeyboardButton(text="🏆 Школьный ТОП")],
        [KeyboardButton(text="🎁 Магазин наград"), KeyboardButton(text="🎓 Курс завершён / запросить выход")]
    ], resize_keyboard=True)

def teacher_kb():
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text="📋 Моя группа и ученики"), KeyboardButton(text="➕ Начислить монеты")],
        [KeyboardButton(text="🏆 Школьный ТОП")]
    ], resize_keyboard=True)

# --- СТАРТ И РЕГИСТРАЦИЯ ---
@dp.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext):
    cursor.execute("SELECT role, group_name FROM users WHERE user_id = ?", (message.from_user.id,))
    user = cursor.fetchone()
    
    if user:
        role, group = user
        kb = teacher_kb() if role == "teacher" else student_kb()
        await message.answer(f"С возвращением в **LEADER EDUCATION**!\nВаша группа/роль: *{group}*", reply_markup=kb, parse_mode="Markdown")
        return

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎓 Я Ученик", callback_data="role_student")],
        [InlineKeyboardButton(text="👨‍🏫 Я Учитель", callback_data="role_teacher")]
    ])
    await message.answer(
        "Добро пожаловать в систему **LEADER EDUCATION**! 🚀\nВыберите, кто вы:",
        reply_markup=kb, parse_mode="Markdown"
    )

@dp.callback_query(F.data.startswith("role_"))
async def process_role(callback: types.CallbackQuery, state: FSMContext):
    role = callback.data.split("_")[1]
    if role == "teacher":
        await callback.message.answer("Введите секретный код для регистрации преподавателя:")
        await state.set_state(RegState.waiting_for_teacher_code)
    else:
        cursor.execute("SELECT user_id, full_name, group_name FROM users WHERE role = 'teacher'")
        teachers = cursor.fetchall()
        
        if not teachers:
            await callback.message.answer("❌ В боте еще не зарегистрирован ни один учитель! Попросите вашего преподавателя сначала зайти в бот.")
            await callback.answer()
            return
            
        buttons = []
        for t_id, t_name, t_group in teachers:
            buttons.append([InlineKeyboardButton(text=f"👨‍🏫 {t_name} ({t_group})", callback_data=f"select_teacher_{t_id}")])
            
        kb = InlineKeyboardMarkup(inline_keyboard=buttons)
        await callback.message.answer("Выберите своего преподавателя из списка:", reply_markup=kb)
        await state.set_state(RegState.waiting_for_teacher_choice)
    await callback.answer()

@dp.message(RegState.waiting_for_teacher_code)
async def check_teacher_code(message: types.Message, state: FSMContext):
    if message.text.strip() == TEACHER_SECRET_CODE:
        await message.answer("Код принят! ✅ Введите ваше **Имя и Фамилию**:", parse_mode="Markdown")
        await state.set_state(RegState.waiting_for_teacher_name)
    else:
        await message.answer("❌ Неверный код! Попробуйте еще раз или введите /start.")

@dp.message(RegState.waiting_for_teacher_name)
async def get_teacher_name(message: types.Message, state: FSMContext):
    await state.update_data(teacher_name=message.text.strip())
    await message.answer("Введите название вашей группы (например: *Frontend-1* или *Python Pro*):", parse_mode="Markdown")
    await state.set_state(RegState.waiting_for_teacher_group)

@dp.message(RegState.waiting_for_teacher_group)
async def save_teacher(message: types.Message, state: FSMContext):
    data = await state.get_data()
    name = data["teacher_name"]
    group_name = message.text.strip()
    
    cursor.execute("INSERT INTO users (user_id, full_name, role, group_name, coins, teacher_id) VALUES (?, ?, 'teacher', ?, 0, NULL)",
                   (message.from_user.id, name, group_name))
    conn.commit()
    await state.clear()
    
    await message.answer(f"✅ Регистрация успешна!\nПреподаватель: **{name}**\nГруппа: **{group_name}**", reply_markup=teacher_kb(), parse_mode="Markdown")

@dp.callback_query(F.data.startswith("select_teacher_"))
async def select_teacher(callback: types.CallbackQuery, state: FSMContext):
    teacher_id = int(callback.data.split("_")[2])
    
    cursor.execute("SELECT group_name FROM users WHERE user_id = ?", (teacher_id,))
    t_group = cursor.fetchone()[0]
    
    await state.update_data(teacher_id=teacher_id, group_name=t_group)
    await callback.message.answer("Отлично! Теперь введите ваши **Имя и Фамилию** (как в журнале):", parse_mode="Markdown")
    await state.set_state(RegState.waiting_for_student_name)
    await callback.answer()

@dp.message(RegState.waiting_for_student_name)
async def save_student(message: types.Message, state: FSMContext):
    data = await state.get_data()
    full_name = message.text.strip()
    teacher_id = data["teacher_id"]
    group_name = data["group_name"]
    
    cursor.execute("INSERT INTO users (user_id, full_name, role, group_name, coins, teacher_id) VALUES (?, ?, 'student', ?, 0, ?)",
                   (message.from_user.id, full_name, group_name, teacher_id))
    conn.commit()
    await state.clear()
    
    await message.answer(f"🎉 Регистрация завершена, **{full_name}**!\nВы добавлены в группу: **{group_name}**", reply_markup=student_kb(), parse_mode="Markdown")

# --- ФУНКЦИОНАЛ УЧЕНИКА ---
@dp.message(F.text == "👤 Мой профиль")
async def my_profile(message: types.Message):
    cursor.execute("SELECT full_name, group_name, coins, teacher_id FROM users WHERE user_id = ? AND role = 'student'", (message.from_user.id,))
    user = cursor.fetchone()
    if not user: return
    
    name, group, coins, t_id = user
    rank = get_rank(coins)
    
    cursor.execute("SELECT full_name FROM users WHERE user_id = ?", (t_id,))
    t_name = cursor.fetchone()[0]
    
    cursor.execute("SELECT COUNT(*) FROM users WHERE role = 'student' AND coins > ?", (coins,))
    place = cursor.fetchone()[0] + 1
    
    text = (
        f"🎓 **Ваш профиль (LEADER EDUCATION)**\n\n"
        f"👤 Имя: **{name}**\n"
        f"📚 Группа: **{group}**\n"
        f"👨‍🏫 Преподаватель: **{t_name}**\n"
        f"💰 Баланс: **{coins} монет**\n"
        f"🎖 Ранг: **{rank}**\n"
        f"📊 Место в общем рейтинге: **#{place}**"
    )
    await message.answer(text, parse_mode="Markdown")

@dp.message(F.text == "🏆 Школьный ТОП")
async def leaderboard(message: types.Message):
    cursor.execute("SELECT full_name, group_name, coins FROM users WHERE role = 'student' ORDER BY coins DESC LIMIT 10")
    top_users = cursor.fetchall()
    
    if not top_users:
        await message.answer("Рейтинг пока пуст.")
        return

    text = "🏆 **ТОП-10 Учеников LEADER EDUCATION**\n\n"
    medals = ["🥇", "🥈", "🥉"]
    
    for idx, (name, group, coins) in enumerate(top_users, start=1):
        prefix = medals[idx - 1] if idx <= 3 else f"**{idx}.**"
        text += f"{prefix} {name} ({group}) — **{coins}** 💰\n"
        
    await message.answer(text, parse_mode="Markdown")

@dp.message(F.text == "🎁 Магазин наград")
async def shop_menu(message: types.Message):
    cursor.execute("SELECT id, title, price FROM shop")
    items = cursor.fetchall()
    buttons = [[InlineKeyboardButton(text=f"{title} — {price} 💰", callback_data=f"buy_{item_id}")] for item_id, title, price in items]
    kb = InlineKeyboardMarkup(inline_keyboard=buttons)
    await message.answer("🎁 **Магазин наград**\nВыберите приз для обмена монет:", reply_markup=kb, parse_mode="Markdown")

@dp.callback_query(F.data.startswith("buy_"))
async def buy_item(callback: types.CallbackQuery):
    item_id = int(callback.data.split("_")[1])
    user_id = callback.from_user.id
    
    cursor.execute("SELECT full_name, coins, teacher_id FROM users WHERE user_id = ?", (user_id,))
    user = cursor.fetchone()
    cursor.execute("SELECT title, price FROM shop WHERE id = ?", (item_id,))
    item = cursor.fetchone()
    
    if not user or not item: return
    name, coins, t_id = user
    title, price = item
    
    if coins < price:
        await callback.answer(f"❌ Недостаточно монет! Нужно еще {price - coins} 💰", show_alert=True)
        return
        
    cursor.execute("UPDATE users SET coins = coins - ? WHERE user_id = ?", (price, user_id))
    conn.commit()
    
    await callback.message.answer(f"✅ Вы приобрели: **{title}** за {price} монет!\nПокажите это сообщение вашему преподавателю.", parse_mode="Markdown")
    
    try:
        await bot.send_message(t_id, f"🛍 **Покупка в вашей группе!**\nУченик: **{name}**\nКупил: **{title}** ({price} монет)", parse_mode="Markdown")
    except Exception:
        pass
    await callback.answer()

# --- ЗАПРОС УЧЕНИКА НА ВЫХОД ---
@dp.message(F.text == "🎓 Курс завершён / запросить выход")
async def request_exit(message: types.Message):
    cursor.execute("SELECT full_name, group_name, teacher_id FROM users WHERE user_id = ? AND role = 'student'", (message.from_user.id,))
    user = cursor.fetchone()
    if not user:
        return
    
    name, group, t_id = user
    cursor.execute("SELECT 1 FROM exit_requests WHERE student_id = ?", (message.from_user.id,))
    if cursor.fetchone():
        await message.answer("⏳ Запрос уже отправлен. Учитель скоро примет решение — пока вы остаётесь в группе.")
        return

    cursor.execute(
        "INSERT INTO exit_requests (student_id, teacher_id) VALUES (?, ?)",
        (message.from_user.id, t_id)
    )
    conn.commit()
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🎓 Выпустить и удалить аккаунт", callback_data=f"approve_exit_{message.from_user.id}"),
            InlineKeyboardButton(text="↩️ Оставить в группе", callback_data=f"deny_exit_{message.from_user.id}")
        ]
    ])
    
    try:
        await bot.send_message(
            t_id,
            f"🎉 **Финишная прямая! Запрос на выпуск**\n\n"
            f"Ученик **{name}** из группы *{group}* сообщил(а), что завершил(а) курс.\n"
            "Решите, удалить ли аккаунт ученика или оставить его в группе.",
            reply_markup=kb,
            parse_mode="Markdown"
        )
        await message.answer("🚀 Поздравляем с завершением курса! Запрос отправлен учителю. До его решения ваш аккаунт останется активным.")
    except TelegramAPIError:
        cursor.execute(
            "DELETE FROM exit_requests WHERE student_id = ? AND teacher_id = ?",
            (message.from_user.id, t_id)
        )
        conn.commit()
        logging.exception("Could not send course completion request for student %s", message.from_user.id)
        await message.answer("❌ Не удалось отправить запрос учителю. Попробуйте позже.")

# --- РЕШЕНИЕ УЧИТЕЛЯ ПО ВЫХОДУ ---
@dp.callback_query(F.data.startswith("approve_exit_"))
async def approve_exit(callback: types.CallbackQuery):
    student_id = int(callback.data.split("_")[2])
    
    cursor.execute(
        "SELECT u.full_name FROM users u "
        "JOIN exit_requests r ON r.student_id = u.user_id "
        "WHERE u.user_id = ? AND u.teacher_id = ? AND u.role = 'student' AND r.teacher_id = ?",
        (student_id, callback.from_user.id, callback.from_user.id)
    )
    student = cursor.fetchone()
    if not student:
        await callback.answer("Запрос уже обработан или он адресован другому учителю.", show_alert=True)
        return
    student_name = student[0]
    
    cursor.execute("DELETE FROM exit_requests WHERE student_id = ?", (student_id,))
    cursor.execute("DELETE FROM users WHERE user_id = ?", (student_id,))
    conn.commit()
    
    await callback.message.edit_text(f"✅ Вы подтвердили выпуск ученика **{student_name}**. Его аккаунт удалён из системы.", parse_mode="Markdown")
    
    try:
        await bot.send_message(
            student_id,
            "🎉 **Поздравляем!** Преподаватель подтвердил завершение вашего курса в **LEADER EDUCATION**. Удачи в дальнейших начинаниях! 🚀",
            parse_mode="Markdown"
        )
    except TelegramAPIError:
        logging.exception("Could not notify student %s about account removal", student_id)
    await callback.answer()

@dp.callback_query(F.data.startswith("deny_exit_"))
async def deny_exit(callback: types.CallbackQuery):
    student_id = int(callback.data.split("_")[2])
    
    cursor.execute(
        "SELECT u.full_name FROM users u "
        "JOIN exit_requests r ON r.student_id = u.user_id "
        "WHERE u.user_id = ? AND u.teacher_id = ? AND u.role = 'student' AND r.teacher_id = ?",
        (student_id, callback.from_user.id, callback.from_user.id)
    )
    student = cursor.fetchone()
    if not student:
        await callback.answer("Запрос уже обработан или он адресован другому учителю.", show_alert=True)
        return

    cursor.execute("DELETE FROM exit_requests WHERE student_id = ?", (student_id,))
    conn.commit()
    await callback.message.edit_text(
        f"↩️ Вы решили оставить ученика **{student[0]}** в группе. Аккаунт продолжает работать.",
        parse_mode="Markdown"
    )
    
    try:
        await bot.send_message(
            student_id,
            "📚 Учитель решил оставить вас в группе. Ваш аккаунт и прогресс сохранены — продолжайте обучение!",
            parse_mode="Markdown"
        )
    except TelegramAPIError:
        logging.exception("Could not notify student %s about the exit request decision", student_id)
    await callback.answer()

# --- ФУНКЦИОНАЛ УЧИТЕЛЯ ---
@dp.message(F.text == "📋 Моя группа и ученики")
async def list_my_students(message: types.Message):
    cursor.execute("SELECT user_id, full_name, coins FROM users WHERE teacher_id = ? AND role = 'student' ORDER BY full_name", (message.from_user.id,))
    students = cursor.fetchall()
    
    cursor.execute("SELECT group_name FROM users WHERE user_id = ?", (message.from_user.id,))
    g_info = cursor.fetchone()
    group_name = g_info[0] if g_info else "Ваша группа"
    
    if not students:
        await message.answer(f"📚 Группа: **{group_name}**\n\nВ вашей группе пока нет зарегистрированных учеников.")
        return
        
    text = f"📚 Группа: **{group_name}**\n📋 **Список ваших учеников:**\n\n"
    for uid, name, coins in students:
        text += f"• {name} (`{uid}`) — **{coins}** 💰\n"
    text += "\n💡 *Скопируйте ID ученика для начисления монет.*"
    await message.answer(text, parse_mode="Markdown")

@dp.message(F.text == "➕ Начислить монеты")
async def start_give_coins(message: types.Message, state: FSMContext):
    cursor.execute("SELECT role FROM users WHERE user_id = ?", (message.from_user.id,))
    user = cursor.fetchone()
    if not user or user[0] != "teacher": return
        
    await message.answer("Введите **ID ученика** (из вашего списка группы):", parse_mode="Markdown")
    await state.set_state(GiveCoinsState.waiting_for_student_id)

@dp.message(GiveCoinsState.waiting_for_student_id)
async def get_student_id(message: types.Message, state: FSMContext):
    if not message.text.isdigit():
        await message.answer("❌ ID должен состоять только из цифр:")
        return
        
    student_id = int(message.text)
    cursor.execute("SELECT full_name FROM users WHERE user_id = ? AND teacher_id = ? AND role = 'student'", (student_id, message.from_user.id))
    student = cursor.fetchone()
    
    if not student:
        await message.answer("❌ Ученик с таким ID не найден в вашей группе. Проверьте список:")
        return
        
    await state.update_data(student_id=student_id, student_name=student[0])
    await message.answer(f"Ученик: **{student[0]}**.\nСколько монет начислить? (например: `15`)", parse_mode="Markdown")
    await state.set_state(GiveCoinsState.waiting_for_amount)

@dp.message(GiveCoinsState.waiting_for_amount)
async def process_give_coins(message: types.Message, state: FSMContext):
    if not message.text.isdigit():
        await message.answer("❌ Введите число:")
        return
        
    amount = int(message.text)
    data = await state.get_data()
    student_id = data["student_id"]
    student_name = data["student_name"]
    
    cursor.execute("UPDATE users SET coins = coins + ? WHERE user_id = ?", (amount, student_id))
    conn.commit()
    await state.clear()
    
    await message.answer(f"✅ Успешно! Начислено **+{amount} монет** ученику **{student_name}**.", reply_markup=teacher_kb(), parse_mode="Markdown")
    
    try:
        await bot.send_message(student_id, f"🎉 **Вам начислены монеты!**\nПреподаватель оценил вашу работу и дал **+{amount} 💰**!", parse_mode="Markdown")
    except Exception:
        pass

# --- ЗАПУСК БОТА ---
async def health(request: web.Request) -> web.Response:
    return web.json_response({"status": "ok"})


async def main():
    app = web.Application()
    app.router.add_get("/", health)
    app.router.add_get("/health", health)

    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("PORT", "8080"))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logging.info("Health server listening on port %s", port)

    polling_task = asyncio.create_task(dp.start_polling(bot))
    try:
        await polling_task
    finally:
        await runner.cleanup()

if __name__ == "__main__":
    asyncio.run(main())