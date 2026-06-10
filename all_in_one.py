import os
import sqlite3
import json
import sys
import asyncio
import threading
import logging
from datetime import datetime, timedelta
from flask import Flask, request, jsonify
from apscheduler.schedulers.background import BackgroundScheduler
from aiogram import Bot, Dispatcher, types
from aiogram.dispatcher.filters import Command, StateFilter
from aiogram.contrib.fsm_storage.memory import MemoryStorage
from aiogram.dispatcher import FSMContext
from aiogram.dispatcher.filters.state import State, StatesGroup
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
import requests
from dotenv import load_dotenv

# Настройка логирования
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Для совместимости с Windows
if sys.version_info[0] == 3 and sys.version_info[1] >= 8 and sys.platform.startswith('win'):
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

load_dotenv()

# -------------------------------------------------------------------
# Конфиги
# -------------------------------------------------------------------
CLIENT_TOKEN = os.getenv("CLIENT_BOT_TOKEN")
EXECUTOR_TOKEN = os.getenv("EXECUTOR_BOT_TOKEN")
DB_PATH = "cad_exchange.db"

# -------------------------------------------------------------------
# 1. Flask приложение (только для healthcheck)
# -------------------------------------------------------------------
flask_app = Flask(__name__)

@flask_app.route('/')
def home():
    return "CAD Exchange API is running"

@flask_app.route('/health')
def health():
    return jsonify({"status": "ok", "bots_running": True})

# -------------------------------------------------------------------
# 1.1 Инициализация базы данных
# -------------------------------------------------------------------
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''
        CREATE TABLE IF NOT EXISTS users (
            telegram_id INTEGER PRIMARY KEY,
            username TEXT,
            balance INTEGER DEFAULT 20,
            rating REAL DEFAULT 0.0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    c.execute('''
        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            customer_id INTEGER NOT NULL,
            executor_id INTEGER DEFAULT NULL,
            title TEXT NOT NULL,
            description TEXT,
            files TEXT,
            price INTEGER NOT NULL,
            urgency TEXT CHECK(urgency IN ('low','medium','high')) DEFAULT 'medium',
            days_to_live INTEGER NOT NULL,
            status TEXT CHECK(status IN ('open','in_progress','completed','closed','expired','cancelled')) DEFAULT 'open',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            expires_at TIMESTAMP,
            taken_at TIMESTAMP,
            completed_at TIMESTAMP,
            result_files TEXT,
            FOREIGN KEY (customer_id) REFERENCES users(telegram_id),
            FOREIGN KEY (executor_id) REFERENCES users(telegram_id)
        )
    ''')
    c.execute('CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_orders_expires ON orders(expires_at)')
    conn.commit()
    conn.close()

init_db()

# -------------------------------------------------------------------
# 1.2 Вспомогательные функции для работы с БД
# -------------------------------------------------------------------
def get_db_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def get_user(telegram_id, username=None):
    conn = get_db_connection()
    user = conn.execute("SELECT * FROM users WHERE telegram_id = ?", (telegram_id,)).fetchone()
    if not user and username is not None:
        conn.execute("INSERT INTO users (telegram_id, username) VALUES (?, ?)", (telegram_id, username))
        conn.commit()
        user = conn.execute("SELECT * FROM users WHERE telegram_id = ?", (telegram_id,)).fetchone()
    conn.close()
    return dict(user) if user else None

def update_balance(telegram_id, delta):
    conn = get_db_connection()
    conn.execute("UPDATE users SET balance = balance + ? WHERE telegram_id = ?", (delta, telegram_id))
    conn.commit()
    conn.close()

def send_notification(telegram_id, bot_type, text):
    token = CLIENT_TOKEN if bot_type == 'client' else EXECUTOR_TOKEN
    if not token:
        print(f"Уведомление для {telegram_id}: {text}")
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        requests.post(url, json={"chat_id": telegram_id, "text": text}, timeout=5)
    except Exception as e:
        print(f"Ошибка отправки: {e}")

def expire_orders():
    conn = get_db_connection()
    now = datetime.utcnow().isoformat()
    expired = conn.execute(
        "SELECT id, customer_id FROM orders WHERE status='open' AND expires_at < ?",
        (now,)
    ).fetchall()
    for order in expired:
        conn.execute("UPDATE orders SET status='expired' WHERE id=?", (order["id"],))
        send_notification(order["customer_id"], "client", f"⏰ Заказ №{order['id']} снят")
    conn.commit()
    conn.close()

scheduler = BackgroundScheduler()
scheduler.add_job(func=expire_orders, trigger="interval", hours=1)
scheduler.start()

# -------------------------------------------------------------------
# 1.3 Логические функции ядра (без HTTP)
# -------------------------------------------------------------------
def create_order_logic(customer_id, title, description, price, urgency, days_to_live, files):
    user = get_user(customer_id)
    if not user:
        return {"success": False, "error": "Пользователь не найден"}
    if user["balance"] < price:
        return {"success": False, "error": "Недостаточно баллов"}
    
    expires_at = (datetime.utcnow() + timedelta(days=days_to_live)).isoformat()
    conn = get_db_connection()
    cursor = conn.execute(
        '''INSERT INTO orders 
           (customer_id, title, description, files, price, urgency, days_to_live, expires_at, status)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'open')''',
        (customer_id, title, description, json.dumps(files), price, urgency, days_to_live, expires_at)
    )
    order_id = cursor.lastrowid
    conn.commit()
    conn.close()
    update_balance(customer_id, -price)
    return {"success": True, "order_id": order_id}

def take_order_logic(order_id, executor_id):
    conn = get_db_connection()
    order = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
    if not order:
        conn.close()
        return {"success": False, "error": "Заказ не найден"}
    if order["status"] != "open":
        conn.close()
        return {"success": False, "error": "Заказ уже не открыт"}
    if order["customer_id"] == executor_id:
        conn.close()
        return {"success": False, "error": "Нельзя взять свой заказ"}
    if datetime.utcnow().isoformat() > order["expires_at"]:
        conn.execute("UPDATE orders SET status='expired' WHERE id=?", (order_id,))
        conn.commit()
        conn.close()
        return {"success": False, "error": "Срок заказа истёк"}
    
    now = datetime.utcnow().isoformat()
    conn.execute(
        "UPDATE orders SET executor_id=?, status='in_progress', taken_at=? WHERE id=?",
        (executor_id, now, order_id)
    )
    conn.commit()
    conn.close()
    send_notification(order["customer_id"], "client", f"🔧 Исполнитель взял заказ №{order_id}")
    return {"success": True}

def submit_order_logic(order_id, executor_id, result_files):
    conn = get_db_connection()
    order = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
    if not order:
        conn.close()
        return {"success": False, "error": "Заказ не найден"}
    if order["status"] != "in_progress":
        conn.close()
        return {"success": False, "error": "Заказ не в работе"}
    if order["executor_id"] != executor_id:
        conn.close()
        return {"success": False, "error": "Вы не исполнитель"}
    
    now = datetime.utcnow().isoformat()
    conn.execute(
        "UPDATE orders SET status='completed', completed_at=?, result_files=? WHERE id=?",
        (now, json.dumps(result_files), order_id)
    )
    conn.commit()
    conn.close()
    send_notification(order["customer_id"], "client", f"✅ Исполнитель сдал заказ №{order_id}. Примите /accept {order_id}")
    return {"success": True}

def accept_order_logic(order_id, customer_id):
    conn = get_db_connection()
    order = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
    if not order:
        conn.close()
        return {"success": False, "error": "Заказ не найден"}
    if order["status"] != "completed":
        conn.close()
        return {"success": False, "error": "Заказ не сдан"}
    if order["customer_id"] != customer_id:
        conn.close()
        return {"success": False, "error": "Вы не заказчик"}
    
    executor_id = order["executor_id"]
    reward = order["price"]
    update_balance(executor_id, reward)
    conn.execute("UPDATE orders SET status='closed' WHERE id=?", (order_id,))
    conn.commit()
    conn.close()
    send_notification(executor_id, "executor", f"🎉 Заказчик принял работу. Вам начислено {reward} баллов")
    send_notification(customer_id, "client", f"✅ Вы приняли работу по заказу №{order_id}")
    return {"success": True}

def cancel_order_logic(order_id, user_id):
    conn = get_db_connection()
    order = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
    if not order:
        conn.close()
        return {"success": False, "error": "Заказ не найден"}
    if order["customer_id"] != user_id:
        conn.close()
        return {"success": False, "error": "Только заказчик может отменить"}
    if order["status"] not in ("open", "in_progress"):
        conn.close()
        return {"success": False, "error": "Невозможно отменить"}
    
    if order["status"] == "open":
        update_balance(user_id, order["price"])
    if order["status"] == "in_progress" and order["executor_id"]:
        send_notification(order["executor_id"], "executor", f"⚠️ Заказчик отменил заказ №{order_id}")
    conn.execute("UPDATE orders SET status='cancelled' WHERE id=?", (order_id,))
    conn.commit()
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
        query += " AND status = ?"
        params.append(status)
    if urgency:
        query += " AND urgency = ?"
        params.append(urgency)
    if price_min is not None:
        query += " AND price >= ?"
        params.append(price_min)
    if price_max is not None:
        query += " AND price <= ?"
        params.append(price_max)
    if customer_id is not None:
        query += " AND customer_id = ?"
        params.append(customer_id)
    if executor_id is not None:
        query += " AND executor_id = ?"
        params.append(executor_id)
    query += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
    params.extend([limit, offset])

    conn = get_db_connection()
    rows = conn.execute(query, params).fetchall()
    orders = [dict(row) for row in rows]
    conn.close()
    return {"success": True, "data": orders}

# -------------------------------------------------------------------
# 2. Client Bot
# -------------------------------------------------------------------
storage = MemoryStorage()
client_bot = Bot(token=CLIENT_TOKEN)
dp_client = Dispatcher(client_bot, storage=storage)

class CreateOrder(StatesGroup):
    title = State()
    description = State()
    price = State()
    urgency = State()
    days = State()
    files = State()

user_files = {}

@dp_client.message_handler(Command("start"))
async def cmd_start(message: Message):
    tg_id = message.from_user.id
    username = message.from_user.username or ""
    get_user(tg_id, username)
    balance_data = get_user(tg_id)["balance"]
    await message.answer(
        f"🏗️ Добро пожаловать в биржу CAD (клиент)!\nВаш баланс: {balance_data} баллов\n\n"
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
            [InlineKeyboardButton(text="Низкая", callback_data="low"),
             InlineKeyboardButton(text="Средняя", callback_data="medium"),
             InlineKeyboardButton(text="Высокая", callback_data="high")]
        ])
        await CreateOrder.next()
        await message.answer("Выберите срочность:", reply_markup=kb)
    except:
        await message.answer("❌ Введите положительное число")

@dp_client.callback_query_handler(lambda c: c.data in ['low', 'medium', 'high'], state=CreateOrder.urgency)
async def process_urgency(callback: CallbackQuery, state: FSMContext):
    async with state.proxy() as data:
        data['urgency'] = callback.data
    await CreateOrder.next()
    await callback.message.answer("Через сколько дней снять заказ? (число дней)")
    await callback.answer()

@dp_client.message_handler(state=CreateOrder.days)
async def process_days(message: Message, state: FSMContext):
    try:
        days = int(message.text)
        if days < 1:
            raise ValueError
        async with state.proxy() as data:
            data['days'] = days
        await CreateOrder.next()
        await message.answer("Приложите файлы. После всех файлов введите /done")
    except:
        await message.answer("❌ Введите число больше 0")

@dp_client.message_handler(content_types=['document'], state=CreateOrder.files)
async def process_files(message: Message, state: FSMContext):
    tg_id = message.from_user.id
    if tg_id not in user_files:
        user_files[tg_id] = []
    user_files[tg_id].append(message.document.file_id)
    await message.answer(f"Файл добавлен. Ещё или /done")

@dp_client.message_handler(Command("done"), state=CreateOrder.files)
async def finish_files(message: Message, state: FSMContext):
    tg_id = message.from_user.id
    files = user_files.get(tg_id, [])
    data = await state.get_data()
    result = create_order_logic(
        tg_id, data['title'], data['description'], data['price'],
        data['urgency'], data['days'], files
    )
    if result.get("success"):
        await message.answer(f"✅ Заказ №{result['order_id']} создан! Списано {data['price']} баллов.")
    else:
        await message.answer(f"❌ Ошибка: {result.get('error')}")
    if tg_id in user_files:
        del user_files[tg_id]
    await state.finish()

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

# -------------------------------------------------------------------
# 3. Executor Bot
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
        "/feed - заказы\n/take <id> - взять\n/my - мои заказы\n/submit <id> - сдать\n/balance - баланс"
    )

@dp_executor.message_handler(Command("balance"))
async def cmd_balance_executor(message: Message):
    user = get_user(message.from_user.id)
    await message.answer(f"💰 Баланс: {user['balance']} баллов")

@dp_executor.message_handler(Command("feed"))
async def cmd_feed(message: Message):
    result = get_orders_logic({"status": "open", "limit": 10})
    if not result.get("success"):
        await message.answer("❌ Ошибка")
        return
    orders = result.get("data", [])
    if not orders:
        await message.answer("Нет открытых заказов.")
        return
    for o in orders:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Взять", callback_data=f"take_{o['id']}")]
        ])
        text = f"🔹 #{o['id']} | {o['title']}\n💰 {o['price']} баллов\n🔥 {o['urgency']}\n📅 до {o['expires_at'][:10]}"
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
        text += f"#{o['id']} | {o['title']} | {o['status']}\n"
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
    
    # Проверяем, что заказ принадлежит исполнителю и в работе
    orders_result = get_orders_logic({"id": order_id})
    if not orders_result.get("success"):
        await message.answer("❌ Заказ не найден")
        return
    order = orders_result["data"][0]
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

# -------------------------------------------------------------------
# 4. Запуск всех компонентов
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
    logger.info("Запуск CAD Exchange платформы")
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    logger.info(f"Flask запущен на порту {os.getenv('PORT', 10000)}")
    try:
        asyncio.run(run_bots_async())
    except KeyboardInterrupt:
        logger.info("Остановка...")
    except Exception as e:
        logger.error(f"Ошибка: {e}")
        sys.exit(1)
