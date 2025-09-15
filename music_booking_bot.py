import logging
import sqlite3
from datetime import datetime, timedelta
from typing import Dict, Optional

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ConversationHandler,
    MessageHandler,
    filters,
    ContextTypes,
)
import os
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN"))
DB_PATH = "booking.db"
TIME_SLOT_DURATION = 30  # минут
WORK_START_HOUR = 10
WORK_END_HOUR = 20
PAYMENT_TIMEOUT_MINUTES = 15  # через сколько минут отменить бронь, если не оплачено

# --- Состояния для ConversationHandler ---
(
    SELECT_SPECIALIZATION,
    SELECT_DIRECTION,
    SELECT_INSTRUMENT,
    SELECT_DATE,
    SELECT_TIME,
    CONFIRM_BOOKING,
    PAYMENT_WAIT,
    ADMIN_SELECT_SPEC,
    ADMIN_SELECT_DIR,
    ADMIN_SELECT_INSTRUMENT,
    ADMIN_SELECT_DATE,
    ADMIN_SELECT_TIME,
    WAIT_PRICE_INPUT,
) = range(13)

# --- Логирование ---
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- База данных ---
import os
DB_PATH = os.path.join(os.path.dirname(__file__), "booking.db")

def init_db():
    print(f"📁 Используется база данных по пути: {os.path.abspath(DB_PATH)}")  # 👈 ВЫВОД ПУТИ!
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    c.execute('''
        CREATE TABLE IF NOT EXISTS bookings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            specialization TEXT,
            direction TEXT,
            instrument TEXT,
            date TEXT,
            time_slot TEXT,
            status TEXT DEFAULT 'pending_payment',
            payment_id TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            paid_at DATETIME,
            price REAL NOT NULL DEFAULT 800.0
        )
    ''')

    c.execute('''
        CREATE TABLE IF NOT EXISTS prices (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            specialization TEXT UNIQUE,
            direction TEXT UNIQUE,
            price REAL NOT NULL DEFAULT 800.0
        )
    ''')

    c.execute('''
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            language_code TEXT,
            joined_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    default_prices = [
        ('solo', 'percussion', 800.0),
        ('solo', 'strings', 800.0),
        ('solo', 'brass', 800.0),
        ('solo', 'piano', 800.0),
        ('solo', 'vocal', 800.0),
        ('solo', 'mix', 800.0),

        ('duet', 'percussion', 1200.0),
        ('duet', 'strings', 1200.0),
        ('duet', 'brass', 1200.0),
        ('duet', 'piano', 1200.0),
        ('duet', 'vocal', 1200.0),
        ('duet', 'mix', 1200.0),

        ('ensemble', 'percussion', 1500.0),
        ('ensemble', 'strings', 1500.0),
        ('ensemble', 'brass', 1500.0),
        ('ensemble', 'piano', 1500.0),
        ('ensemble', 'vocal', 1500.0),
        ('ensemble', 'mix', 1500.0),
    ]

    for spec, dir, price in default_prices:
        c.execute('''
            INSERT OR IGNORE INTO prices (specialization, direction, price)
            VALUES (?, ?, ?)
        ''', (spec, dir, price))

    conn.commit()
    conn.close()
    print("✅ База данных инициализирована.")


# --- Получить цену по специализации и направлению ---
def get_price(spec: str, dir: str) -> float:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute('SELECT price FROM prices WHERE specialization = ? AND direction = ?', (spec, dir))
    row = c.fetchone()
    conn.close()
    return row['price'] if row else 800.0


# --- Проверка доступности слота ---
def is_slot_available(date_str: str, time_slot: str) -> bool:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''
        SELECT COUNT(*) FROM bookings 
        WHERE date = ? AND time_slot = ? AND status IN ('confirmed', 'pending_payment')
    ''', (date_str, time_slot))
    count = c.fetchone()[0]
    conn.close()
    return count == 0


# --- Получить все доступные слоты на дату ---
def get_available_slots(date_str: str) -> list:
    slots = []
    for hour in range(WORK_START_HOUR, WORK_END_HOUR):
        for minute in [0, 30]:
            slot_time = f"{hour:02d}:{minute:02d}"
            if is_slot_available(date_str, slot_time):
                slots.append(slot_time)
    return slots


# --- Сохранить бронь ---
def save_booking(user_id: int, spec: str, dir: str, inst: str, date: str, time_slot: str, status='pending_payment'):
    price = get_price(spec, dir)
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''
        INSERT INTO bookings (user_id, specialization, direction, instrument, date, time_slot, status, price)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    ''', (user_id, spec, dir, inst, date, time_slot, status, price))
    booking_id = c.lastrowid
    conn.commit()
    conn.close()
    return booking_id


# --- Обновить статус брони ---
def update_booking_status(booking_id: int, status: str, payment_id: str = None):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    if status == "confirmed":
        c.execute('''
            UPDATE bookings SET status = ?, paid_at = CURRENT_TIMESTAMP WHERE id = ?
        ''', (status, booking_id))
    else:
        c.execute('''
            UPDATE bookings SET status = ? WHERE id = ?
        ''', (status, booking_id))
    conn.commit()
    conn.close()


# --- Получить бронь по ID ---
def get_booking_by_id(booking_id: int) -> Optional[dict]:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute('SELECT * FROM bookings WHERE id = ?', (booking_id,))
    row = c.fetchone()
    conn.close()
    return dict(row) if row else None


# --- Удалить просроченные брони ---
def cleanup_expired_bookings():
    timeout = datetime.now() - timedelta(minutes=PAYMENT_TIMEOUT_MINUTES)
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''
        UPDATE bookings SET status = 'expired' 
        WHERE status = 'pending_payment' AND created_at < ?
    ''', (timeout.strftime('%Y-%m-%d %H:%M:%S'),))
    conn.commit()
    conn.close()
    logger.info("Просроченные брони очищены.")


# --- Отправить напоминание за 1 час ---
async def send_reminder(context: ContextTypes.DEFAULT_TYPE):
    job = context.job
    booking_id = job.data['booking_id']
    booking = get_booking_by_id(booking_id)
    if not booking or booking['status'] != 'confirmed':
        return

    user_id = booking['user_id']
    date = booking['date']
    time_slot = booking['time_slot']
    direction = booking['direction']
    instrument = booking.get('instrument') or ''

    text = f"🔔 Напоминание!\n\nВы записаны на занятие:\n📅 {date}\n⏰ {time_slot}\n🎯 {direction}"
    if instrument:
        text += f" ({instrument})"
    text += "\n\n📍 Адрес: ул. Музыкальная, д. 5, каб. 203\n📞 Контакт: +7 (XXX) XXX-XX-XX\n\nПриходите за 10 минут!"

    try:
        await context.bot.send_message(chat_id=user_id, text=text)
        logger.info(f"Напоминание отправлено пользователю {user_id} о брони #{booking_id}")
    except Exception as e:
        logger.error(f"Не удалось отправить напоминание: {e}")


# --- Команда /start ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_id = user.id
    username = user.username or ""
    first_name = user.first_name or ""
    language_code = user.language_code or ""

    # Сохраняем пользователя в базу
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''
        INSERT OR IGNORE INTO users (user_id, username, first_name, language_code)
        VALUES (?, ?, ?, ?)
    ''', (user_id, username, first_name, language_code))
    conn.commit()
    conn.close()

    keyboard = [[InlineKeyboardButton("🎹 Выбрать специализацию", callback_data='select_spec')]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(
        "Привет! 👋\nДобро пожаловать в студию музыкального образования!\n\n"
        "Здесь ты можешь забронировать место на занятие по любому инструменту — соло, дуэт или ансамбль.\n\n"
        "Выбери направление, чтобы начать:",
        reply_markup=reply_markup
    )


# --- Обработчик выбора специализации ---
async def select_specialization(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    print(f"🔥 [DEBUG] select_specialization вызван")
    await query.answer()

    keyboard = [
        [InlineKeyboardButton("🎼 Соло", callback_data='spec_solo')],
        [InlineKeyboardButton("💞 Дуэт", callback_data='spec_duet')],
        [InlineKeyboardButton("🎻 Ансамбль (3+)", callback_data='spec_ensemble')]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await query.edit_message_text(
        "Выберите тип занятия:",
        reply_markup=reply_markup
    )
    return SELECT_SPECIALIZATION


# --- Обработчик выбора направления ---
async def select_direction(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    print(f"🔥 [DEBUG] select_direction вызван. data='{query.data}'")
    await query.answer()

    spec = query.data.split('_')[1]  # spec_solo → 'solo'
    context.user_data['specialization'] = spec

    keyboard = [
        [InlineKeyboardButton("🥁 Ударные", callback_data='dir_percussion')],
        [InlineKeyboardButton("🎻 Струнные", callback_data='dir_strings')],
        [InlineKeyboardButton("🎷 Духовые", callback_data='dir_brass')],
        [InlineKeyboardButton("🎹 Фортепиано", callback_data='dir_piano')],
        [InlineKeyboardButton("🎤 Вокал", callback_data='dir_vocal')],
        [InlineKeyboardButton("🎶 Микс", callback_data='dir_mix')]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await query.edit_message_text(
        "Выберите направление:",
        reply_markup=reply_markup
    )
    return SELECT_DIRECTION


# --- Обработчик выбора инструмента (если ударные) ---
async def select_instrument(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    print(f"🔥 [DEBUG] select_instrument вызван. data='{query.data}'")
    await query.answer()

    direction = query.data.split('_')[1]  # dir_percussion → 'percussion'
    context.user_data['direction'] = direction

    if direction == 'percussion':
        keyboard = [
            [InlineKeyboardButton("🥁 Барабаны", callback_data='inst_drums')],
            [InlineKeyboardButton("🥁 Перкуссия", callback_data='inst_percc')],
            [InlineKeyboardButton("🥁 Тимпаны", callback_data='inst_timpani')],
            [InlineKeyboardButton("🥁 Электронные ударные", callback_data='inst_electronic')],
            [InlineKeyboardButton("🥁 Все вышеперечисленное", callback_data='inst_all')],
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(
            "Выберите конкретный инструмент:",
            reply_markup=reply_markup
        )
        return SELECT_INSTRUMENT
    else:
        context.user_data['instrument'] = None
        await select_date(update, context)
        return SELECT_DATE


# --- Обработчик выбора инструмента (после выбора) ---
async def handle_instrument_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    print(f"🔥 [DEBUG] handle_instrument_choice вызван. data='{query.data}'")
    await query.answer()

    instrument = query.data.split('_')[1]
    context.user_data['instrument'] = instrument
    await select_date(update, context)
    return SELECT_DATE


# --- Календарь (выбор даты) ---
async def select_date(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    print(f"🔥 [DEBUG] select_date вызван")
    await query.answer()

    today = datetime.today()
    dates = []
    for i in range(14):
        d = today + timedelta(days=i)
        date_str = d.strftime('%Y-%m-%d')
        dates.append((d.strftime('%d.%m'), date_str))

    keyboard = []
    row = []
    for label, date_str in dates:
        available_slots = get_available_slots(date_str)
        if available_slots:
            row.append(InlineKeyboardButton(label, callback_data=f'date_{date_str}'))
        else:
            row.append(InlineKeyboardButton(f"{label} 🚫", callback_data='ignore'))

        if len(row) == 3:
            keyboard.append(row)
            row = []

    if row:
        keyboard.append(row)
    keyboard.append([InlineKeyboardButton("← Назад", callback_data='back_to_dir')])

    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.edit_message_text(
        "Выберите дату занятия:",
        reply_markup=reply_markup
    )
    return SELECT_DATE


# --- Обработка выбора даты ---
async def handle_date_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    print(f"🔥 [DEBUG] handle_date_choice вызван. data='{query.data}'")
    await query.answer()

    if query.data == 'back_to_dir':
        await select_direction(update, context)
        return SELECT_DIRECTION

    if query.data.startswith('date_'):
        date_str = query.data.split('_')[1]
        context.user_data['selected_date'] = date_str

        slots = get_available_slots(date_str)
        if not slots:
            await query.edit_message_text("На эту дату нет свободных слотов. Попробуйте другую.")
            return SELECT_DATE

        keyboard = []
        for slot in slots:
            keyboard.append([InlineKeyboardButton(slot, callback_data=f'time_{slot}')])

        keyboard.append([InlineKeyboardButton("← Назад к датам", callback_data='back_to_dates')])
        reply_markup = InlineKeyboardMarkup(keyboard)

        await query.edit_message_text(
            f"Выбрана дата: {date_str}\n\nВыберите время:",
            reply_markup=reply_markup
        )
        return SELECT_TIME


# --- Обработка выбора времени ---
async def handle_time_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    print(f"🔥 [DEBUG] handle_time_choice вызван. data='{query.data}'")
    await query.answer()

    if query.data == 'back_to_dates':
        await select_date(update, context)
        return SELECT_DATE

    if query.data.startswith('time_'):
        time_slot = query.data.split('_')[1]
        context.user_data['selected_time'] = time_slot

        spec = context.user_data['specialization']
        dir = context.user_data['direction']
        inst = context.user_data.get('instrument') or ''
        date = context.user_data['selected_date']

        booking_id = save_booking(
            user_id=query.from_user.id,
            spec=spec,
            dir=dir,
            inst=inst,
            date=date,
            time_slot=time_slot
        )
        context.user_data['booking_id'] = booking_id

        booking = get_booking_by_id(booking_id)
        price = booking['price']

        text = (
            f"Вы выбрали:\n"
            f"📅 Дата: {date}\n"
            f"⏰ Время: {time_slot}\n"
            f"🎯 Специализация: {'Соло' if spec=='solo' else 'Дуэт' if spec=='duet' else 'Ансамбль'} | {dir}"
        )

        if inst:
            text += f"\n🎸 Инструмент: {inst}"

        text += f"\n\n💰 Стоимость: {price} ₽\n\n"
        text += f"[Оплатить {price}₽](https://example.com/pay?booking={booking_id})\n\n"
        text += "⚠️ Внимание: слот будет зарезервирован на 15 минут. Если оплата не пройдёт — место освободится."

        keyboard = [
            [InlineKeyboardButton("✅ Я оплатил", callback_data='confirm_payment')],
            [InlineKeyboardButton("❌ Отменить", callback_data='cancel_booking')]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await query.edit_message_text(
            text,
            reply_markup=reply_markup,
            parse_mode='Markdown'
        )

        return CONFIRM_BOOKING


# --- Подтверждение оплаты ---
async def confirm_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    print(f"🔥 [DEBUG] confirm_payment вызван")
    await query.answer()

    booking_id = context.user_data.get('booking_id')
    if not booking_id:
        await query.edit_message_text("Ошибка: бронь не найдена.")
        return

    update_booking_status(booking_id, "confirmed")
    booking = get_booking_by_id(booking_id)

    booking_datetime = datetime.strptime(f"{booking['date']} {booking['time_slot']}", "%Y-%m-%d %H:%M")
    reminder_time = booking_datetime - timedelta(hours=1)
    now = datetime.now()
    delay = (reminder_time - now).total_seconds()

    if delay > 0:
        context.job_queue.run_once(send_reminder, when=delay, data={'booking_id': booking_id})

    await query.edit_message_text(
        f"✅ ЗАБРОНИРОВАНО!\n\n"
        f"Вы успешно записаны на занятие:\n\n"
        f"📅 {booking['date']}\n"
        f"⏰ {booking['time_slot']}\n"
        f"🎯 {booking['direction']}"
        f"{f' ({booking["instrument"]})' if booking['instrument'] else ''}\n\n"
        f"📍 Адрес: ул. Музыкальная, д. 5, каб. 203\n"
        f"📞 Контакт: +7 (XXX) XXX-XX-XX\n\n"
        f"Приходите за 10 минут до начала!\n\n"
        f"Спасибо, что выбираете нас ❤️"
    )


# --- Отмена брони ---
async def cancel_booking(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    print(f"🔥 [DEBUG] cancel_booking вызван")
    await query.answer()

    booking_id = context.user_data.get('booking_id')
    if booking_id:
        update_booking_status(booking_id, "cancelled")
        context.user_data.clear()

    keyboard = [[InlineKeyboardButton("🎹 Выбрать специализацию", callback_data='select_spec')]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await query.edit_message_text(
        "❌ Бронь отменена. Слот освобождён.\n\n"
        "Хочешь забронировать другое время? Выбери специализацию ниже:",
        reply_markup=reply_markup
    )
    return SELECT_SPECIALIZATION


# --- Команда /mybookings ---
async def my_bookings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute('''
        SELECT date, time_slot, direction, instrument, status 
        FROM bookings 
        WHERE user_id = ? AND status IN ('confirmed', 'pending_payment') 
        ORDER BY date, time_slot
    ''', (user_id,))
    rows = c.fetchall()
    conn.close()

    if not rows:
        await update.message.reply_text("У вас нет активных броней.")
        return

    text = "📋 Ваши брони:\n\n"
    for row in rows:
        status_emoji = "✅" if row['status'] == 'confirmed' else "⏳"
        inst_text = f" ({row['instrument']})" if row['instrument'] else ""
        text += f"{status_emoji} {row['date']} {row['time_slot']} — {row['direction']}{inst_text}\n"

    await update.message.reply_text(text)


# --- Команда /admin ---
async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id != ADMIN_ID:
        await update.message.reply_text(
            "❌ Доступ запрещён. Это приватная панель для администратора."
        )
        return  # 👈 НИЧЕГО НЕ ВЫВОДИМ — ПОЛЬЗОВАТЕЛЬ НЕ ВИДИТ МЕНЮ!

    # 👇 ТОЛЬКО ДЛЯ АДМИНА — ПОКАЗЫВАЕМ МЕНЮ
    keyboard = [
        [InlineKeyboardButton("📊 Просмотр всех броней", callback_data='admin_view_bookings')],
        [InlineKeyboardButton("➕ Забронировать без оплаты", callback_data='admin_create_booking')],
        [InlineKeyboardButton("💰 Изменить цену", callback_data='admin_change_price')],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(
        "🔐 Админ-панель:\n\nВыберите действие:",
        reply_markup=reply_markup
    )


# --- Админ: просмотр всех броней ---
async def admin_view_bookings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    print(f"🔥 [DEBUG] admin_view_bookings вызван")
    await query.answer()

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute('''
        SELECT 
            b.id, 
            u.username, 
            b.specialization, 
            b.direction, 
            b.instrument, 
            b.date, 
            b.time_slot, 
            b.status
        FROM bookings b
        LEFT JOIN (SELECT DISTINCT user_id, username FROM users) u ON b.user_id = u.user_id
        ORDER BY b.date DESC, b.time_slot DESC
    ''')
    rows = c.fetchall()
    conn.close()

    if not rows:
        await query.edit_message_text("📭 Нет броней.")
        return

    text = "📋 Все брони:\n\n"
    for row in rows:
        username = row['username'] if row['username'] is not None else f"ID:{row['user_id']}"
        status_emoji = "✅" if row['status'] == 'confirmed' else "⏳" if row['status'] == 'pending_payment' else "❌"
        instrument = row['instrument'] if 'instrument' in row and row['instrument'] is not None else ""
        inst_text = f" ({instrument})" if instrument else ""

        text += f"{status_emoji} {row['date']} {row['time_slot']} — {row['specialization']} | {row['direction']}{inst_text}\n"
        text += f"   👤 {username}\n"

    keyboard = [[InlineKeyboardButton("← Назад в админку", callback_data='admin_back')]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await query.edit_message_text(text, reply_markup=reply_markup)


# --- Админ: начать создание брони ---
async def admin_start_booking(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    print(f"🔥 [DEBUG] admin_start_booking вызван. data='{query.data}'")
    await query.answer()

    keyboard = [
        [InlineKeyboardButton("🎼 Соло", callback_data='admin_spec_solo')],
        [InlineKeyboardButton("💞 Дуэт", callback_data='admin_spec_duet')],
        [InlineKeyboardButton("🎻 Ансамбль (3+)", callback_data='admin_spec_ensemble')]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await query.edit_message_text(
        "🔹 Выберите тип занятия:",
        reply_markup=reply_markup
    )
    return ADMIN_SELECT_SPEC


# --- Админ: выбор направления ---
async def admin_select_direction(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    print(f"🔥 [DEBUG] admin_select_direction вызван. data='{query.data}'")
    await query.answer()

    spec = query.data.split('_')[2]  # admin_spec_solo → 'solo'
    context.user_data['admin_spec'] = spec

    keyboard = [
        [InlineKeyboardButton("🥁 Ударные", callback_data='admin_dir_percussion')],
        [InlineKeyboardButton("🎻 Струнные", callback_data='admin_dir_strings')],
        [InlineKeyboardButton("🎷 Духовые", callback_data='admin_dir_brass')],
        [InlineKeyboardButton("🎹 Фортепиано", callback_data='admin_dir_piano')],
        [InlineKeyboardButton("🎤 Вокал", callback_data='admin_dir_vocal')],
        [InlineKeyboardButton("🎶 Микс", callback_data='admin_dir_mix')]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await query.edit_message_text(
        "🔹 Выберите направление:",
        reply_markup=reply_markup
    )
    return ADMIN_SELECT_DIR


# --- Админ: выбор инструмента (если ударные) ---
async def admin_select_instrument(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    print(f"🔥 [DEBUG] admin_select_instrument вызван. data='{query.data}'")
    await query.answer()

    direction = query.data.split('_')[2]  # admin_dir_percussion → 'percussion'
    context.user_data['admin_dir'] = direction

    if direction == 'percussion':
        keyboard = [
            [InlineKeyboardButton("🥁 Барабаны", callback_data='admin_inst_drums')],
            [InlineKeyboardButton("🥁 Перкуссия", callback_data='admin_inst_percc')],
            [InlineKeyboardButton("🥁 Тимпаны", callback_data='admin_inst_timpani')],
            [InlineKeyboardButton("🥁 Электронные ударные", callback_data='admin_inst_electronic')],
            [InlineKeyboardButton("🥁 Все вышеперечисленное", callback_data='admin_inst_all')],
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.edit_message_text(
            "🔹 Выберите инструмент:",
            reply_markup=reply_markup
        )
        return ADMIN_SELECT_INSTRUMENT
    else:
        context.user_data['admin_inst'] = None
        await admin_select_date(update, context)
        return ADMIN_SELECT_DATE


# --- Админ: обработка выбора инструмента ---
async def admin_handle_instrument_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    print(f"🔥 [DEBUG] admin_handle_instrument_choice вызван. data='{query.data}'")
    await query.answer()

    instrument = query.data.split('_')[2]
    context.user_data['admin_inst'] = instrument
    await admin_select_date(update, context)
    return ADMIN_SELECT_DATE


# --- Админ: выбор даты ---
async def admin_select_date(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    print(f"🔥 [DEBUG] admin_select_date вызван")
    await query.answer()

    today = datetime.today()
    dates = []
    for i in range(14):
        d = today + timedelta(days=i)
        date_str = d.strftime('%Y-%m-%d')
        dates.append((d.strftime('%d.%m'), date_str))

    keyboard = []
    row = []
    for label, date_str in dates:
        row.append(InlineKeyboardButton(label, callback_data=f'admin_date_{date_str}'))
        if len(row) == 3:
            keyboard.append(row)
            row = []

    if row:
        keyboard.append(row)
    keyboard.append([InlineKeyboardButton("← Назад", callback_data='admin_back_to_spec')])

    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.edit_message_text(
        "🔹 Выберите дату:",
        reply_markup=reply_markup
    )
    return ADMIN_SELECT_DATE


# --- Админ: выбор времени ---
async def admin_handle_date_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    print(f"🔥 [DEBUG] admin_handle_date_choice вызван. data='{query.data}'")
    await query.answer()

    if query.data == 'admin_back_to_spec':
        await admin_start_booking(update, context)
        return ADMIN_SELECT_SPEC

    if query.data.startswith('admin_date_'):
        date_str = query.data.split('_')[2]
        context.user_data['admin_date'] = date_str

        slots = get_available_slots(date_str)
        if not slots:
            await query.edit_message_text("На эту дату нет свободных слотов.")
            return ADMIN_SELECT_DATE

        keyboard = []
        for slot in slots:
            keyboard.append([InlineKeyboardButton(slot, callback_data=f'admin_time_{slot}')])

        keyboard.append([InlineKeyboardButton("← Назад к датам", callback_data='admin_back_to_date')])
        reply_markup = InlineKeyboardMarkup(keyboard)

        await query.edit_message_text(
            f"🔹 Выбрана дата: {date_str}\n\nВыберите время:",
            reply_markup=reply_markup
        )
        return ADMIN_SELECT_TIME


# --- Админ: подтверждение брони без оплаты ---
async def admin_handle_time_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    print(f"🔥 [DEBUG] admin_handle_time_choice вызван. data='{query.data}'")
    await query.answer()

    if query.data == 'admin_back_to_date':
        await admin_select_date(update, context)
        return ADMIN_SELECT_DATE

    if query.data.startswith('admin_time_'):
        time_slot = query.data.split('_')[2]
        spec = context.user_data['admin_spec']
        dir = context.user_data['admin_dir']
        inst = context.user_data.get('admin_inst') or ''
        date = context.user_data['admin_date']

        booking_id = save_booking(
            user_id=ADMIN_ID,
            spec=spec,
            dir=dir,
            inst=inst,
            date=date,
            time_slot=time_slot,
            status='confirmed'
        )
        price = get_price(spec, dir)

        text = (
            f"✅ АДМИН БРОНИРОВАЛ БЕЗ ОПЛАТЫ!\n\n"
            f"📅 {date}\n"
            f"⏰ {time_slot}\n"
            f"🎯 {spec} | {dir}"
            f"{f' ({inst})' if inst else ''}\n"
            f"💰 Цена: {price} ₽\n"
            f"👤 Забронировал: Админ"
        )

        keyboard = [[InlineKeyboardButton("← Назад в админку", callback_data='admin_back')]]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await query.edit_message_text(text, reply_markup=reply_markup)
        return ConversationHandler.END


# --- Админ: выбрать цену для пары (спец + направление) ---
async def admin_change_price_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    print(f"🔥 [DEBUG] admin_change_price_menu вызван")
    await query.answer()

    keyboard = [
        [InlineKeyboardButton("🎼 Соло — Ударные", callback_data='admin_price_solo_percussion')],
        [InlineKeyboardButton("🎼 Соло — Струнные", callback_data='admin_price_solo_strings')],
        [InlineKeyboardButton("🎼 Соло — Фортепиано", callback_data='admin_price_solo_piano')],
        [InlineKeyboardButton("🎼 Соло — Вокал", callback_data='admin_price_solo_vocal')],
        [InlineKeyboardButton("🎼 Соло — Микс", callback_data='admin_price_solo_mix')],

        [InlineKeyboardButton("💞 Дуэт — Ударные", callback_data='admin_price_duet_percussion')],
        [InlineKeyboardButton("💞 Дуэт — Струнные", callback_data='admin_price_duet_strings')],
        [InlineKeyboardButton("💞 Дуэт — Фортепиано", callback_data='admin_price_duet_piano')],
        [InlineKeyboardButton("💞 Дуэт — Вокал", callback_data='admin_price_duet_vocal')],
        [InlineKeyboardButton("💞 Дуэт — Микс", callback_data='admin_price_duet_mix')],

        [InlineKeyboardButton("🎻 Ансамбль — Ударные", callback_data='admin_price_ensemble_percussion')],
        [InlineKeyboardButton("🎻 Ансамбль — Струнные", callback_data='admin_price_ensemble_strings')],
        [InlineKeyboardButton("🎻 Ансамбль — Фортепиано", callback_data='admin_price_ensemble_piano')],
        [InlineKeyboardButton("🎻 Ансамбль — Вокал", callback_data='admin_price_ensemble_vocal')],
        [InlineKeyboardButton("🎻 Ансамбль — Микс", callback_data='admin_price_ensemble_mix')],
    ]
    keyboard.append([InlineKeyboardButton("← Назад", callback_data='admin_back')])
    reply_markup = InlineKeyboardMarkup(keyboard)

    await query.edit_message_text(
        "💰 Выберите комбинацию для изменения цены:",
        reply_markup=reply_markup
    )


# --- Админ: назад в меню ---
async def admin_back(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    print(f"🔥 [DEBUG] admin_back вызван")
    await query.answer()

    keyboard = [
        [InlineKeyboardButton("📊 Просмотр всех броней", callback_data='admin_view_bookings')],
        [InlineKeyboardButton("➕ Забронировать без оплаты", callback_data='admin_create_booking')],
        [InlineKeyboardButton("💰 Изменить цену", callback_data='admin_change_price')],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await query.edit_message_text(
        "🔐 Админ-панель:\n\nВыберите действие:",
        reply_markup=reply_markup
    )


# --- Админ: ввести новую цену ---
async def admin_set_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    print(f"🔥 [DEBUG] admin_set_price вызван. data='{query.data}'")
    await query.answer()

    data = query.data
    spec, dir = data.replace('admin_price_', '').split('_')
    context.user_data['price_spec'] = spec
    context.user_data['price_dir'] = dir

    current_price = get_price(spec, dir)
    await query.edit_message_text(
        f"Текущая цена: {current_price} ₽\n\n"
        f"Введите новую цену (число, например: 900):\n\n"
        f"💡 Пример: 1200"
    )
    return WAIT_PRICE_INPUT


# --- Обработка ввода цены ---
async def handle_price_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    print(f"🔥 [DEBUG] handle_price_input вызван. Текст: '{update.message.text}'")
    try:
        new_price = float(update.message.text.strip())
        if new_price < 0:
            raise ValueError
    except:
        await update.message.reply_text("❌ Неверный формат. Введите число (например: 900)")
        return WAIT_PRICE_INPUT

    spec = context.user_data['price_spec']
    dir = context.user_data['price_dir']

    print(f"🔧 [DEBUG] Обновляем цену: spec='{spec}', dir='{dir}', price={new_price}")

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''
        INSERT OR REPLACE INTO prices (specialization, direction, price)
        VALUES (?, ?, ?)
    ''', (spec, dir, new_price))
    conn.commit()

    # Проверка
    c.execute('SELECT price FROM prices WHERE specialization = ? AND direction = ?', (spec, dir))
    row = c.fetchone()
    actual_price = row[0]
    print(f"✅ [DEBUG] Актуальная цена после обновления: {actual_price}")
    conn.close()

    await update.message.reply_text(
        f"✅ Цена успешно изменена!\n\n"
        f"{spec} | {dir}: {actual_price} ₽"
    )

    # Вернём в админку
    keyboard = [
        [InlineKeyboardButton("📊 Просмотр всех броней", callback_data='admin_view_bookings')],
        [InlineKeyboardButton("➕ Забронировать без оплаты", callback_data='admin_create_booking')],
        [InlineKeyboardButton("💰 Изменить цену", callback_data='admin_change_price')],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(
        "🔐 Админ-панель:\n\nВыберите действие:",
        reply_markup=reply_markup
    )

    return ConversationHandler.END


# --- Обработка ошибок ---
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error(f"Update {update} caused error: {context.error}")


# --- Главная функция ---
def main():
    init_db()
    app = Application.builder().token(BOT_TOKEN).build()

    conv_handler = ConversationHandler(
        entry_points=[CallbackQueryHandler(select_specialization, pattern='^select_spec$')],
        states={
            SELECT_SPECIALIZATION: [
                CallbackQueryHandler(select_direction, pattern=r'^spec_'),
                CallbackQueryHandler(select_specialization, pattern='^select_spec$'),
            ],
            SELECT_DIRECTION: [
                CallbackQueryHandler(select_instrument, pattern=r'^dir_'),
            ],
            SELECT_INSTRUMENT: [
                CallbackQueryHandler(handle_instrument_choice, pattern=r'^inst_'),
            ],
            SELECT_DATE: [
                CallbackQueryHandler(handle_date_choice, pattern=r'^date_'),
                CallbackQueryHandler(select_date, pattern=r'^back_to_dir$'),
            ],
            SELECT_TIME: [
                CallbackQueryHandler(handle_time_choice, pattern=r'^time_'),
                CallbackQueryHandler(select_date, pattern=r'^back_to_dates$'),
            ],
            CONFIRM_BOOKING: [
                CallbackQueryHandler(confirm_payment, pattern='^confirm_payment$'),
                CallbackQueryHandler(cancel_booking, pattern='^cancel_booking$'),
            ],

            ADMIN_SELECT_SPEC: [
                CallbackQueryHandler(admin_select_direction, pattern=r'^admin_spec_'),
            ],
            ADMIN_SELECT_DIR: [
                CallbackQueryHandler(admin_select_instrument, pattern=r'^admin_dir_'),
            ],
            ADMIN_SELECT_INSTRUMENT: [
                CallbackQueryHandler(admin_handle_instrument_choice, pattern=r'^admin_inst_'),
            ],
            ADMIN_SELECT_DATE: [
                CallbackQueryHandler(admin_handle_date_choice, pattern=r'^admin_date_'),
                CallbackQueryHandler(admin_select_date, pattern=r'^admin_back_to_spec$'),
            ],
            ADMIN_SELECT_TIME: [
                CallbackQueryHandler(admin_handle_time_choice, pattern=r'^admin_time_'),
                CallbackQueryHandler(admin_select_date, pattern=r'^admin_back_to_date$'),
            ],
            WAIT_PRICE_INPUT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_price_input),
            ],
        },
        fallbacks=[
            CommandHandler('start', start),
            CommandHandler('admin', admin_panel),
            CallbackQueryHandler(admin_set_price, pattern=r'^admin_price_'),
        ],
        per_message=False,
    )

    # Регистрация обработчиков
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("mybookings", my_bookings))
    app.add_handler(conv_handler)
    app.add_handler(CommandHandler("admin", admin_panel))

    # Админские обработчики
    app.add_handler(CallbackQueryHandler(admin_view_bookings, pattern='^admin_view_bookings$'))
    app.add_handler(CallbackQueryHandler(admin_start_booking, pattern='^admin_create_booking$'))
    app.add_handler(CallbackQueryHandler(admin_change_price_menu, pattern='^admin_change_price$'))
    app.add_handler(CallbackQueryHandler(admin_back, pattern='^admin_back$'))

    # Обработчик ошибок
    app.add_error_handler(error_handler)

    # Запуск фоновой задачи по очистке просроченных броней каждые 5 минут
    app.job_queue.run_repeating(cleanup_expired_bookings, interval=300, first=10)

    logger.info("Бот запущен...")
    app.run_polling()


if __name__ == '__main__':
    main()
