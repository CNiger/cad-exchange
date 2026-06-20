import os
import json
import sys
import asyncio
import threading
import logging
from datetime import datetime, timedelta
from flask import Flask, request, jsonify
from apscheduler.schedulers.background import BackgroundScheduler
from aiogram import Bot, Dispatcher, types
from aiogram.dispatcher.filters import Command
from aiogram.contrib.fsm_storage.memory import MemoryStorage
from aiogram.dispatcher import FSMContext
from aiogram.dispatcher.filters.state import State, StatesGroup
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
import requests
import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

# Настройка логирования
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

load_dotenv()

# -------------------------------------------------------------------
# Конфиги
# -------------------------------------------------------------------
CLIENT_TOKEN = os.getenv("CLIENT_BOT_TOKEN")
EXECUTOR_TOKEN = os.getenv("EXECUTOR_BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL environment variable not set")

# -------------------------------------------------------------------
# 1. Подключение к PostgreSQL
# -------------------------------------------------------------------
def get_db_connection():
    conn = psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)
    return conn

def init_db():
    conn = get_db_connection()
    with conn:
        with conn.cursor() as c:
            c.execute('''
                CREATE TABLE IF NOT EXISTS users (
                    telegram_id BIGINT PRIMARY KEY,
                    username TEXT,
                    balance INTEGER DEFAULT 20,
                    real_balance DECIMAL(10,2) DEFAULT 0.00,
                    rating REAL DEFAULT 0.0,
                    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            c.execute('''
                CREATE TABLE IF NOT EXISTS orders (
                    id SERIAL PRIMARY KEY,
                    customer_id BIGINT NOT NULL,
                    executor_id BIGINT DEFAULT NULL,
                    title TEXT NOT NULL,
                    description TEXT,
                    files TEXT,
                    price INTEGER NOT NULL,
                    urgency TEXT CHECK(urgency IN ('low','medium','high')) DEFAULT 'medium',
                    hours_to_live INTEGER NOT NULL,
                    status TEXT CHECK(status IN ('open','in_progress','completed','closed','expired','cancelled')) DEFAULT 'open',
                    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                    expires_at TIMESTAMP WITH TIME ZONE,
                    taken_at TIMESTAMP WITH TIME ZONE,
                    completed_at TIMESTAMP WITH TIME ZONE,
                    result_files TEXT,
                    real_price DECIMAL(10,2) DEFAULT NULL,
                    payment_method TEXT,
                    payment_id TEXT,
                    payment_status TEXT DEFAULT 'pending',
                    FOREIGN KEY (customer_id) REFERENCES users(telegram_id),
                    FOREIGN KEY (executor_id) REFERENCES users(telegram_id)
                )
            ''')
            c.execute('CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status)')
            c.execute('CREATE INDEX IF NOT EXISTS idx_orders_expires ON orders(expires_at)')
    conn.close()
    logger.info("Database initialized")

init_db()

# -------------------------------------------------------------------
# 2. Функции работы с БД
# -------------------------------------------------------------------
def get_user(telegram_id, username=None):
    conn = get_db_connection()
    with conn:
        with conn.cursor() as c:
            c.execute("SELECT * FROM users WHERE telegram_id = %s", (telegram_id,))
            user = c.fetchone()
            if not user and username is not None:
                c.execute(
                    "INSERT INTO users (telegram_id, username) VALUES (%s, %s)",
                    (telegram_id, username)
                )
                conn.commit()
                c.execute("SELECT * FROM users WHERE telegram_id = %s", (telegram_id,))
                user = c.fetchone()
    conn.close()
    # Преобразуем RealDictRow в обычный dict, если это не None
    return dict(user) if user else None

def update_balance(telegram_id, delta):
    conn = get_db_connection()
    with conn:
        with conn.cursor() as c:
            c.execute(
                "UPDATE users SET balance = balance + %s WHERE telegram_id = %s",
                (delta, telegram_id)
            )
    conn.close()

# -------------------------------------------------------------------
# 3. Отправка уведомлений
# -------------------------------------------------------------------
def send_notification(telegram_id, bot_type, text):
    token = CLIENT_TOKEN if bot_type == 'client' else EXECUTOR_TOKEN
    if not token:
        logger.info(f"Уведомление для {telegram_id}: {text}")
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        requests.post(url, json={"chat_id": telegram_id, "text": text}, timeout=5)
    except Exception as e:
        logger.error(f"Ошибка отправки: {e}")

# -------------------------------------------------------------------
# 4. Планировщик
# -------------------------------------------------------------------
def expire_orders():
    conn = get_db_connection()
    with conn:
        with conn.cursor() as c:
            now = datetime.utcnow().isoformat()
            c.execute(
                "SELECT id, customer_id FROM orders WHERE status='open' AND expires_at < %s",
                (now,)
            )
            expired = c.fetchall()
            for order in expired:
                c.execute("UPDATE orders SET status='expired' WHERE id = %s", (order["id"],))
                send_notification(
                    order["customer_id"],
                    "client",
                    f"⏰ Заказ №{order['id']} снят"
                )
    conn.close()

scheduler = BackgroundScheduler()
scheduler.add_job(func=expire_orders, trigger="interval", hours=1)
scheduler.start()

# -------------------------------------------------------------------
# 5. Ядро – бизнес-логика
# -------------------------------------------------------------------
def create_order_logic(customer_id, title, description, price, urgency, hours_to_live, files):
    user = get_user(customer_id)
    if not user:
        return {"success": False, "error": "Пользователь не найден"}
    if user["balance"] < price:
        return {"success": False, "error": "Недостаточно баллов"}

    # Принудительные лимиты для срочности
    if urgency == "high":
        hours_to_live = 1
    elif urgency == "medium":
        hours_to_live = 24
    # для low оставляем как есть

    expires_at = (datetime.utcnow() + timedelta(hours=hours_to_live)).isoformat()
    
    conn = get_db_connection()
    with conn:
        with conn.cursor() as c:
            c.execute(
                '''INSERT INTO orders 
                   (customer_id, title, description, files, price, urgency, hours_to_live, expires_at, status)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'open')
                   RETURNING id''',
                (customer_id, title, description, json.dumps(files), price, urgency, hours_to_live, expires_at)
            )
            order_id = c.fetchone()["id"]
    conn.close()
    update_balance(customer_id, -price)
    return {"success": True, "order_id": order_id}

def take_order_logic(order_id, executor_id):
    conn = get_db_connection()
    with conn:
        with conn.cursor() as c:
            c.execute("SELECT * FROM orders WHERE id = %s", (order_id,))
            order = c.fetchone()
            if not order:
                return {"success": False, "error": "Заказ не найден"}
            if order["status"] != "open":
                return {"success": False, "error": "Заказ уже не открыт"}
            if order["customer_id"] == executor_id:
                return {"success": False, "error": "Нельзя взять свой заказ"}
            if datetime.utcnow().isoformat() > order["expires_at"]:
                c.execute("UPDATE orders SET status='expired' WHERE id = %s", (order_id,))
                return {"success": False, "error": "Срок заказа истёк"}
            now = datetime.utcnow().isoformat()
            c.execute(
                "UPDATE orders SET executor_id=%s, status='in_progress', taken_at=%s WHERE id=%s",
                (executor_id, now, order_id)
            )
    conn.close()
    send_notification(order["customer_id"], "client", f"🔧 Исполнитель взял заказ №{order_id}")
    return {"success": True}

def submit_order_logic(order_id, executor_id, result_files):
    conn = get_db_connection()
    with conn:
        with conn.cursor() as c:
            c.execute("SELECT * FROM orders WHERE id = %s", (order_id,))
            order = c.fetchone()
            if not order:
                return {"success": False, "error": "Заказ не найден"}
            if order["status"] != "in_progress":
                return {"success": False, "error": "Заказ не в работе"}
            if order["executor_id"] != executor_id:
                return {"success": False, "error": "Вы не исполнитель"}
            now = datetime.utcnow().isoformat()
            c.execute(
                "UPDATE orders SET status='completed', completed_at=%s, result_files=%s WHERE id=%s",
                (now, json.dumps(result_files), order_id)
            )
    conn.close()
    send_notification(order["customer_id"], "client", f"✅ Исполнитель сдал заказ №{order_id}. Примите /accept {order_id}")
    return {"success": True}

def accept_order_logic(order_id, customer_id):
    conn = get_db_connection()
    with conn:
        with conn.cursor() as c:
            c.execute("SELECT * FROM orders WHERE id = %s", (order_id,))
            order = c.fetchone()
            if not order:
                return {"success": False, "error": "Заказ не найден"}
            if order["status"] != "completed":
                return {"success": False, "error": "Заказ не сдан"}
            if order["customer_id"] != customer_id:
                return {"success": False, "error": "Вы не заказчик"}
            executor_id = order["executor_id"]
            reward = order["price"]
            update_balance(executor_id, reward)
            c.execute("UPDATE orders SET status='closed' WHERE id = %s", (order_id,))
    conn.close()
    send_notification(executor_id, "executor", f"🎉 Заказчик принял работу. Вам начислено {reward} баллов")
    send_notification(customer_id, "client", f"✅ Вы приняли работу по заказу №{order_id}")
    return {"success": True}

def cancel_order_logic(order_id, user_id):
    conn = get_db_connection()
    with conn:
        with conn.cursor() as c:
            c.execute("SELECT * FROM orders WHERE id = %s", (order_id,))
            order = c.fetchone()
            if not order:
                return {"success": False, "error": "Заказ не найден"}
            if order["customer_id"] != user_id:
                return {"success": False, "error": "Только заказчик может отменить"}
            if order["status"] not in ("open", "in_progress"):
                return {"success": False, "error": "Невозможно отменить"}
            if order["status"] == "open":
                update_balance(user_id, order["price"])
            if order["status"] == "in_progress" and order["executor_id"]:
                send_notification(order["executor_id"], "executor", f"⚠️ Заказчик отменил заказ №{order_id}")
            c.execute("UPDATE orders SET status='cancelled' WHERE id = %s", (order_id,))
    conn.close()
    send_notification(user_id, "client", f"❌ Вы отменили заказ №{order_id}")
    return {"success": True}

def get_orders_logic(filters):
    status = filters.get("status", "open")
    urgency = filters.get("urgency")
    price_min = filters.get("price_min")
    price_max = filters.get("price_max")
    customer_id = filters.get("customer_id")
    executor_id = filters.get("executor_id")
    limit = filters.get("limit", 20)
    offset = filters.get("offset", 0)

    query = "SELECT * FROM orders WHERE 1=1"
    params = []
    if status:
        query += " AND status = %s"
        params.append(status)
    if urgency:
        query += " AND urgency = %s"
        params.append(urgency)
    if price_min is not None:
        query += " AND price >= %s"
        params.append(price_min)
    if price_max is not None:
        query += " AND price <= %s"
        params.append(price_max)
    if customer_id is not None:
        query += " AND customer_id = %s"
        params.append(customer_id)
    if executor_id is not None:
        query += " AND executor_id = %s"
        params.append(executor_id)
    query += " ORDER BY created_at DESC LIMIT %s OFFSET %s"
    params.extend([limit, offset])

    conn = get_db_connection()
    with conn:
        with conn.cursor() as c:
            c.execute(query, params)
            orders = c.fetchall()
    conn.close()
    return {"success": True, "data": [dict(row) for row in orders]}

# -------------------------------------------------------------------
# 6. Flask (только healthcheck)
# -------------------------------------------------------------------
flask_app = Flask(__name__)

@flask_app.route('/')
@flask_app.route('/health')
def health():
    return jsonify({"status": "ok", "database": "postgresql"})

# -------------------------------------------------------------------
# 7. Client Bot
# -------------------------------------------------------------------
storage = MemoryStorage()
client_bot = Bot(token=CLIENT_TOKEN)
dp_client = Dispatcher(client_bot, storage=storage)

class CreateOrder(StatesGroup):
    title = State()
    description = State()
    price = State()
    urgency = State()
    hours = State()
    files = State()

@dp_client.message_handler(Command("start"))
async def cmd_start(message: Message):
    tg_id = message.from_user.id
    username = message.from_user.username or ""
    user = get_user(tg_id, username)
    await message.answer(
        f"🏗️ Добро пожаловать в биржу CAD (клиент)!\nВаш баланс: {user['balance']} баллов\n\n"
        "/new - создать заказ\n/my_orders - мои заказы\n/balance - баланс\n/help - помощь"
    )

@dp_client.message_handler(Command("balance"))
async def cmd_balance(message: Message):
    user = get_user(message.from_user.id)
    await message.answer(f"💰 Баланс: {user['balance']} баллов\n⭐ Рейтинг: {user['rating']}")

@dp_client.message_handler(Command("new"))
async def cmd_new(message: Message):
    await CreateOrder.title.set()
    await message.answer("Введите заголовок задачи:")

@dp_client.message_handler(state=CreateOrder.title)
async def process_title(message: Message, state: FSMContext):
    async with state.proxy() as data:
        data['title'] = message.text
        data['files'] = []
    await CreateOrder.next()
    await message.answer("Введите описание:")

@dp_client.message_handler(state=CreateOrder.description)
async def process_description(message: Message, state: FSMContext):
    async with state.proxy() as data:
        data['description'] = message.text
    await CreateOrder.next()
    await message.answer("Укажите цену в баллах:")

@dp_client.message_handler(state=CreateOrder.price)
async def process_price(message: Message, state: FSMContext):
    try:
        price = int(message.text)
        if price <= 0:
            raise ValueError
        async with state.proxy() as data:
            data['price'] = price
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🟥 Высокая (1 час)", callback_data="high"),
             InlineKeyboardButton(text="🟨 Средняя (24 часа)", callback_data="medium"),
             InlineKeyboardButton(text="🟩 Низкая (сколько укажу)", callback_data="low")]
        ])
        await CreateOrder.next()
        await message.answer("Выберите срочность:", reply_markup=kb)
    except:
        await message.answer("❌ Введите положительное число")

@dp_client.callback_query_handler(lambda c: c.data in ['low', 'medium', 'high'], state=CreateOrder.urgency)
async def process_urgency(callback: CallbackQuery, state: FSMContext):
    urgency = callback.data
    async with state.proxy() as data:
        data['urgency'] = urgency
    
    messages = {
        "high": "🟥 Высокая — заказ будет снят через 1 час",
        "medium": "🟨 Средняя — заказ будет снят через 24 часа",
        "low": "🟩 Низкая — заказ будет снят через указанное вами количество часов"
    }
    await callback.message.answer(messages[urgency])
    await CreateOrder.next()
    await callback.message.answer("⏰ Через сколько часов снять заказ? (введите число)")
    await callback.answer()

@dp_client.message_handler(state=CreateOrder.hours)
async def process_hours(message: Message, state: FSMContext):
    try:
        hours = int(message.text)
        if hours < 1:
            raise ValueError
        async with state.proxy() as data:
            data['hours'] = hours
        await CreateOrder.next()
        await message.answer("📎 Присылайте файлы. Когда закончите, введите /done")
    except:
        await message.answer("❌ Введите число больше 0")

@dp_client.message_handler(content_types=['document'], state=CreateOrder.files)
async def process_files(message: Message, state: FSMContext):
    async with state.proxy() as data:
        data['files'].append(message.document.file_id)
    await message.answer(f"✅ Файл {message.document.file_name} добавлен. Ещё или /done")

@dp_client.message_handler(Command("done"), state=CreateOrder.files)
async def finish_files(message: Message, state: FSMContext):
    data = await state.get_data()
    result = create_order_logic(
        message.from_user.id,
        data['title'],
        data['description'],
        data['price'],
        data['urgency'],
        data['hours'],
        data.get('files', [])
    )
    if result.get("success"):
        await message.answer(f"✅ Заказ №{result['order_id']} создан! Списано {data['price']} баллов.")
    else:
        await message.answer(f"❌ Ошибка: {result.get('error')}")
    await state.finish()

@dp_client.message_handler(Command("cancel"), state='*')
async def cancel_cmd(message: Message, state: FSMContext):
    await state.finish()
    await message.answer("Создание заказа отменено.")

@dp_client.message_handler(Command("my_orders"))
async def cmd_my_orders(message: Message):
    result = get_orders_logic({"customer_id": message.from_user.id})
    if not result.get("success"):
        await message.answer("❌ Ошибка")
        return
    orders = result.get("data", [])
    if not orders:
        await message.answer("У вас нет заказов.")
        return
    text = "📋 Ваши заказы:\n"
    for o in orders:
        text += f"#{o['id']} | {o['title']} | {o['status']} | {o['price']} баллов\n"
    await message.answer(text)

@dp_client.message_handler(Command("accept"))
async def cmd_accept(message: Message):
    args = message.text.split()
    if len(args) != 2:
        await message.answer("Использование: /accept <ID>")
        return
    try:
        order_id = int(args[1])
    except:
        await message.answer("ID должно быть числом")
        return
    result = accept_order_logic(order_id, message.from_user.id)
    if result.get("success"):
        await message.answer(f"✅ Заказ #{order_id} принят")
    else:
        await message.answer(f"❌ {result.get('error')}")

@dp_client.message_handler(Command("cancel_order"))
async def cmd_cancel_order(message: Message):
    args = message.text.split()
    if len(args) != 2:
        await message.answer("Использование: /cancel_order <ID>")
        return
    try:
        order_id = int(args[1])
    except:
        await message.answer("ID должно быть числом")
        return
    result = cancel_order_logic(order_id, message.from_user.id)
    if result.get("success"):
        await message.answer(f"❌ Заказ #{order_id} отменён")
    else:
        await message.answer(f"❌ {result.get('error')}")

@dp_client.message_handler(Command("help"))
async def cmd_help(message: Message):
    await message.answer(
        "Команды заказчика:\n"
        "/new - создать заказ\n"
        "/my_orders - мои заказы\n"
        "/accept <id> - принять работу\n"
        "/cancel_order <id> - отменить заказ\n"
        "/balance - баланс\n"
        "/cancel - отменить создание заказа"
    )

# -------------------------------------------------------------------
# 8. Executor Bot (ПОЛНАЯ ВЕРСИЯ С ФАЙЛАМИ И ФИЛЬТРАМИ)
# -------------------------------------------------------------------
executor_bot = Bot(token=EXECUTOR_TOKEN)
dp_executor = Dispatcher(executor_bot, storage=MemoryStorage())

class SubmitOrder(StatesGroup):
    waiting_for_files = State()

user_temp = {}

@dp_executor.message_handler(Command("start"))
async def cmd_start_executor(message: Message):
    tg_id = message.from_user.id
    username = message.from_user.username or ""
    get_user(tg_id, username)
    await message.answer(
        "👷 Биржа CAD (исполнитель)!\n"
        "/feed - все заказы\n"
        "/feed high - только срочные (1 час)\n"
        "/feed medium - средние (24 часа)\n"
        "/feed low - низкие\n"
        "/take <id> - взять\n"
        "/my - мои заказы\n"
        "/submit <id> - сдать\n"
        "/balance - баланс"
    )

@dp_executor.message_handler(Command("balance"))
async def cmd_balance_executor(message: Message):
    user = get_user(message.from_user.id)
    await message.answer(f"💰 Баланс: {user['balance']} баллов")

@dp_executor.message_handler(Command("feed"))
async def cmd_feed(message: Message):
    args = message.text.split()
    filters = {"status": "open", "limit": 20}
    
    if len(args) > 1:
        if args[1] in ['high', 'medium', 'low']:
            filters["urgency"] = args[1]
    
    result = get_orders_logic(filters)
    if not result.get("success"):
        await message.answer("❌ Ошибка")
        return
    
    orders = result.get("data", [])
    if not orders:
        await message.answer("Нет открытых заказов.")
        return
    
    for o in orders:
        # Формируем время снятия
        expires_dt = datetime.fromisoformat(o['expires_at'])
        expires_str = expires_dt.strftime("%d.%m.%Y %H:%M")
        
        # Иконка срочности
        urgency_icons = {"high": "🟥", "medium": "🟨", "low": "🟩"}
        icon = urgency_icons.get(o['urgency'], "🟩")
        
        # Проверяем наличие файлов
        has_files = o.get('files') and o['files'] != '[]'
        files_indicator = " 📎" if has_files else ""
        
        text = (
            f"{icon} #{o['id']} | {o['title']}{files_indicator}\n"
            f"💰 {o['price']} баллов\n"
            f"⏳ Снятие: {expires_str}\n"
            f"📝 {o['description'][:80]}"
        )
        
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Взять", callback_data=f"take_{o['id']}")]
        ])
        
        # Если есть файлы — отправляем их прямо в ленте
        if has_files:
            await message.answer(text, reply_markup=kb)
            try:
                files = json.loads(o['files'])
                for file_id in files:
                    await message.answer_document(file_id)
            except:
                pass
        else:
            await message.answer(text, reply_markup=kb)

@dp_executor.callback_query_handler(lambda c: c.data.startswith("take_"))
async def callback_take(callback: CallbackQuery):
    order_id = int(callback.data.split("_")[1])
    result = take_order_logic(order_id, callback.from_user.id)
    if result.get("success"):
        await callback.message.answer(f"✅ Вы взяли заказ #{order_id}")
        await callback.message.edit_reply_markup(reply_markup=None)
    else:
        await callback.message.answer(f"❌ {result.get('error')}")
    await callback.answer()

@dp_executor.message_handler(Command("take"))
async def cmd_take(message: Message):
    args = message.text.split()
    if len(args) != 2:
        await message.answer("Использование: /take <id>")
        return
    try:
        order_id = int(args[1])
    except:
        await message.answer("ID должно быть числом")
        return
    result = take_order_logic(order_id, message.from_user.id)
    if result.get("success"):
        await message.answer(f"✅ Вы взяли заказ #{order_id}")
    else:
        await message.answer(f"❌ {result.get('error')}")

@dp_executor.message_handler(Command("my"))
async def cmd_my_orders_executor(message: Message):
    result = get_orders_logic({"executor_id": message.from_user.id})
    orders = result.get("data", [])
    if not orders:
        await message.answer("У вас нет взятых заказов.")
        return
    text = "📌 Ваши заказы:\n"
    for o in orders:
        status_emoji = {
            "in_progress": "🔄",
            "completed": "⏳",
            "closed": "✅",
            "cancelled": "❌"
        }.get(o['status'], "❓")
        text += f"{status_emoji} #{o['id']} | {o['title']} | {o['status']}\n"
    await message.answer(text)

@dp_executor.message_handler(Command("submit"))
async def cmd_submit(message: Message):
    args = message.text.split()
    if len(args) != 2:
        await message.answer("Использование: /submit <id>")
        return
    try:
        order_id = int(args[1])
    except:
        await message.answer("ID должно быть числом")
        return
    
    orders_result = get_orders_logic({"id": order_id})
    if not orders_result.get("success"):
        await message.answer("❌ Заказ не найден")
        return
    orders = orders_result.get("data", [])
    if not orders:
        await message.answer("❌ Заказ не найден")
        return
    order = orders[0]
    
    if order.get("executor_id") != message.from_user.id:
        await message.answer("❌ Вы не исполнитель")
        return
    if order.get("status") != "in_progress":
        await message.answer("❌ Заказ не в работе")
        return
    
    user_temp[message.from_user.id] = order_id
    await SubmitOrder.waiting_for_files.set()
    await message.answer("Пришлите файлы, затем /done_files")

@dp_executor.message_handler(content_types=['document'], state=SubmitOrder.waiting_for_files)
async def submit_files(message: Message, state: FSMContext):
    tg_id = message.from_user.id
    if 'result_files' not in user_temp:
        user_temp['result_files'] = {}
    if tg_id not in user_temp['result_files']:
        user_temp['result_files'][tg_id] = []
    user_temp['result_files'][tg_id].append(message.document.file_id)
    await message.answer(f"Файл добавлен. Ещё или /done_files")

@dp_executor.message_handler(Command("done_files"), state=SubmitOrder.waiting_for_files)
async def finish_submit(message: Message, state: FSMContext):
    tg_id = message.from_user.id
    order_id = user_temp.get(tg_id)
    files = user_temp.get('result_files', {}).get(tg_id, [])
    if not order_id:
        await message.answer("Ошибка. Повторите /submit")
        return
    result = submit_order_logic(order_id, tg_id, files)
    if result.get("success"):
        await message.answer(f"✅ Решение по заказу #{order_id} отправлено")
    else:
        await message.answer(f"❌ {result.get('error')}")
    del user_temp[tg_id]
    if tg_id in user_temp.get('result_files', {}):
        del user_temp['result_files'][tg_id]
    await state.finish()

@dp_executor.message_handler(Command("help"))
async def cmd_help_executor(message: Message):
    await message.answer(
        "Команды исполнителя:\n"
        "/feed - все открытые заказы\n"
        "/feed high - только срочные (1 час)\n"
        "/feed medium - средние (24 часа)\n"
        "/feed low - низкие\n"
        "/take <id> - взять заказ\n"
        "/my - мои заказы\n"
        "/submit <id> - сдать решение\n"
        "/balance - баланс\n"
        "/cancel - отменить текущую операцию"
    )

@dp_executor.message_handler(Command("cancel"), state='*')
async def cmd_cancel_state(message: Message, state: FSMContext):
    await state.finish()
    await message.answer("Отменено.")

# -------------------------------------------------------------------
# 9. Запуск
# -------------------------------------------------------------------
def run_flask():
    port = int(os.getenv("PORT", 10000))
    flask_app.run(host="0.0.0.0", port=port, use_reloader=False)

async def run_bots_async():
    logger.info("Запуск ботов...")
    await dp_client.bot.delete_webhook()
    await dp_executor.bot.delete_webhook()
    await dp_client.skip_updates()
    await dp_executor.skip_updates()
    await asyncio.gather(
        dp_client.start_polling(),
        dp_executor.start_polling()
    )

if __name__ == "__main__":
    logger.info("Запуск CAD Exchange платформы (PostgreSQL)")
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    logger.info(f"Flask запущен на порту {os.getenv('PORT', 10000)}")
    try:
        asyncio.run(run_bots_async())
    except KeyboardInterrupt:
        logger.info("Остановка...")
    except Exception as e:
        logger.error(f"Критическая ошибка: {e}")
        sys.exit(1)
