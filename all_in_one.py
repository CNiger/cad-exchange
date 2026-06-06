import os
import sqlite3
import json
import asyncio
import threading
from datetime import datetime, timedelta
from flask import Flask, request, jsonify
from apscheduler.schedulers.background import BackgroundScheduler
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
import requests
from dotenv import load_dotenv

load_dotenv()

# -------------------------------------------------------------------
# Конфиги
# -------------------------------------------------------------------
CLIENT_TOKEN = os.getenv("CLIENT_BOT_TOKEN")
EXECUTOR_TOKEN = os.getenv("EXECUTOR_BOT_TOKEN")
CORE_BOT_TOKEN = os.getenv("CORE_BOT_TOKEN")
DB_PATH = "cad_exchange.db"

# -------------------------------------------------------------------
# 1. Flask приложение (Core API)
# -------------------------------------------------------------------
flask_app = Flask(__name__)

# -------------------------------------------------------------------
# 1.1 Инициализация базы данных
# -------------------------------------------------------------------
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    # Таблица пользователей
    c.execute('''
        CREATE TABLE IF NOT EXISTS users (
            telegram_id INTEGER PRIMARY KEY,
            username TEXT,
            balance INTEGER DEFAULT 20,
            rating REAL DEFAULT 0.0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    # Таблица заказов
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

# -------------------------------------------------------------------
# 1.3 Отправка уведомлений через соответствующего бота
# -------------------------------------------------------------------
def send_notification(telegram_id, bot_type, text):
    if bot_type == 'client':
        token = CLIENT_TOKEN
    else:
        token = EXECUTOR_TOKEN
    if not token or token == "заглушка":
        print(f"Уведомление для {telegram_id} (тип {bot_type}): {text}")
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        requests.post(url, json={"chat_id": telegram_id, "text": text}, timeout=5)
    except Exception as e:
        print(f"Ошибка отправки уведомления: {e}")

# -------------------------------------------------------------------
# 1.4 Планировщик: автоматическое снятие просроченных заказов
# -------------------------------------------------------------------
def expire_orders():
    conn = get_db_connection()
    now = datetime.utcnow().isoformat()
    expired = conn.execute(
        "SELECT id, customer_id FROM orders WHERE status='open' AND expires_at < ?",
        (now,)
    ).fetchall()
    for order in expired:
        conn.execute("UPDATE orders SET status='expired' WHERE id=?", (order["id"],))
        send_notification(order["customer_id"], "client",
                          f"⏰ Ваш заказ №{order['id']} снят с биржи, так как никто не взял его в срок.")
    conn.commit()
    conn.close()

scheduler = BackgroundScheduler()
scheduler.add_job(func=expire_orders, trigger="interval", hours=1)
scheduler.start()

# -------------------------------------------------------------------
# 1.5 Эндпоинты API
# -------------------------------------------------------------------
@flask_app.route("/user/get_or_create", methods=["GET"])
def api_user_get_or_create():
    telegram_id = request.args.get("telegram_id", type=int)
    username = request.args.get("username", "")
    if not telegram_id:
        return jsonify({"success": False, "error": "telegram_id required"}), 400
    user = get_user(telegram_id, username)
    return jsonify({"success": True, "data": user})

@flask_app.route("/user/balance", methods=["GET"])
def api_user_balance():
    telegram_id = request.args.get("telegram_id", type=int)
    if not telegram_id:
        return jsonify({"success": False, "error": "telegram_id required"}), 400
    user = get_user(telegram_id)
    if not user:
        return jsonify({"success": False, "error": "User not found"}), 404
    return jsonify({"success": True, "data": {"balance": user["balance"], "rating": user["rating"]}})

@flask_app.route("/order/create", methods=["POST"])
def api_order_create():
    data = request.json
    required = ["customer_id", "title", "price", "days_to_live"]
    for field in required:
        if field not in data:
            return jsonify({"success": False, "error": f"Missing {field}"}), 400
    customer_id = data["customer_id"]
    title = data["title"]
    description = data.get("description", "")
    files = json.dumps(data.get("files", []))
    price = int(data["price"])
    urgency = data.get("urgency", "medium")
    days_to_live = int(data["days_to_live"])
    
    user = get_user(customer_id)
    if not user:
        return jsonify({"success": False, "error": "Customer not found"}), 404
    if user["balance"] < price:
        return jsonify({"success": False, "error": "Insufficient balance"}), 400
    
    expires_at = (datetime.utcnow() + timedelta(days=days_to_live)).isoformat()
    conn = get_db_connection()
    cursor = conn.execute(
        '''INSERT INTO orders 
           (customer_id, title, description, files, price, urgency, days_to_live, expires_at, status)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'open')''',
        (customer_id, title, description, files, price, urgency, days_to_live, expires_at)
    )
    order_id = cursor.lastrowid
    conn.commit()
    conn.close()
    update_balance(customer_id, -price)
    return jsonify({"success": True, "data": {"order_id": order_id}})

@flask_app.route("/order/list", methods=["GET"])
def api_order_list():
    status = request.args.get("status", "open")
    urgency = request.args.get("urgency")
    price_min = request.args.get("price_min", type=int)
    price_max = request.args.get("price_max", type=int)
    customer_id = request.args.get("customer_id", type=int)
    executor_id = request.args.get("executor_id", type=int)
    limit = request.args.get("limit", 20, type=int)
    offset = request.args.get("offset", 0, type=int)

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
    return jsonify({"success": True, "data": orders})

@flask_app.route("/order/get", methods=["GET"])
def api_order_get():
    order_id = request.args.get("id", type=int)
    if not order_id:
        return jsonify({"success": False, "error": "id required"}), 400
    conn = get_db_connection()
    order = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
    conn.close()
    if not order:
        return jsonify({"success": False, "error": "Order not found"}), 404
    return jsonify({"success": True, "data": dict(order)})

@flask_app.route("/order/take", methods=["POST"])
def api_order_take():
    data = request.json
    order_id = data.get("order_id")
    executor_id = data.get("executor_id")
    if not order_id or not executor_id:
        return jsonify({"success": False, "error": "order_id and executor_id required"}), 400
    
    conn = get_db_connection()
    order = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
    if not order:
        conn.close()
        return jsonify({"success": False, "error": "Order not found"}), 404
    if order["status"] != "open":
        conn.close()
        return jsonify({"success": False, "error": "Order is not open"}), 400
    if order["customer_id"] == executor_id:
        conn.close()
        return jsonify({"success": False, "error": "Cannot take your own order"}), 400
    if datetime.utcnow().isoformat() > order["expires_at"]:
        conn.execute("UPDATE orders SET status='expired' WHERE id=?", (order_id,))
        conn.commit()
        conn.close()
        return jsonify({"success": False, "error": "Order expired"}), 400
    
    now = datetime.utcnow().isoformat()
    conn.execute(
        "UPDATE orders SET executor_id=?, status='in_progress', taken_at=? WHERE id=?",
        (executor_id, now, order_id)
    )
    conn.commit()
    conn.close()
    send_notification(order["customer_id"], "client",
                      f"🔧 Исполнитель @id{executor_id} взял ваш заказ №{order_id} в работу.")
    return jsonify({"success": True, "data": {"status": "in_progress"}})

@flask_app.route("/order/submit", methods=["POST"])
def api_order_submit():
    data = request.json
    order_id = data.get("order_id")
    executor_id = data.get("executor_id")
    result_files = json.dumps(data.get("result_files", []))
    if not order_id or not executor_id:
        return jsonify({"success": False, "error": "order_id and executor_id required"}), 400
    
    conn = get_db_connection()
    order = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
    if not order:
        conn.close()
        return jsonify({"success": False, "error": "Order not found"}), 404
    if order["status"] != "in_progress":
        conn.close()
        return jsonify({"success": False, "error": "Order is not in progress"}), 400
    if order["executor_id"] != executor_id:
        conn.close()
        return jsonify({"success": False, "error": "You are not the executor"}), 403
    
    now = datetime.utcnow().isoformat()
    conn.execute(
        "UPDATE orders SET status='completed', completed_at=?, result_files=? WHERE id=?",
        (now, result_files, order_id)
    )
    conn.commit()
    conn.close()
    send_notification(order["customer_id"], "client",
                      f"✅ Исполнитель сдал работу по заказу №{order_id}. Для подтверждения используйте /accept {order_id}")
    return jsonify({"success": True, "data": {"status": "completed"}})

@flask_app.route("/order/accept", methods=["POST"])
def api_order_accept():
    data = request.json
    order_id = data.get("order_id")
    customer_id = data.get("customer_id")
    if not order_id or not customer_id:
        return jsonify({"success": False, "error": "order_id and customer_id required"}), 400
    
    conn = get_db_connection()
    order = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
    if not order:
        conn.close()
        return jsonify({"success": False, "error": "Order not found"}), 404
    if order["status"] != "completed":
        conn.close()
        return jsonify({"success": False, "error": "Order is not completed"}), 400
    if order["customer_id"] != customer_id:
        conn.close()
        return jsonify({"success": False, "error": "You are not the customer"}), 403
    
    executor_id = order["executor_id"]
    reward = order["price"]
    update_balance(executor_id, reward)
    conn.execute("UPDATE orders SET status='closed' WHERE id=?", (order_id,))
    conn.commit()
    conn.close()
    
    send_notification(executor_id, "executor",
                      f"🎉 Заказчик принял вашу работу по заказу №{order_id}. Вам начислено {reward} баллов.")
    send_notification(customer_id, "client",
                      f"✅ Вы приняли работу по заказу №{order_id}. Спасибо за использование биржи!")
    return jsonify({"success": True, "data": {"status": "closed"}})

@flask_app.route("/order/cancel", methods=["POST"])
def api_order_cancel():
    data = request.json
    order_id = data.get("order_id")
    user_id = data.get("user_id")
    if not order_id or not user_id:
        return jsonify({"success": False, "error": "order_id and user_id required"}), 400
    
    conn = get_db_connection()
    order = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
    if not order:
        conn.close()
        return jsonify({"success": False, "error": "Order not found"}), 404
    if order["customer_id"] != user_id:
        conn.close()
        return jsonify({"success": False, "error": "Only customer can cancel"}), 403
    if order["status"] not in ("open", "in_progress"):
        conn.close()
        return jsonify({"success": False, "error": "Cannot cancel order in current status"}), 400
    
    old_status = order["status"]
    if old_status == "open":
        update_balance(user_id, order["price"])
    if old_status == "in_progress" and order["executor_id"]:
        send_notification(order["executor_id"], "executor",
                          f"⚠️ Заказчик отменил заказ №{order_id}, который вы выполняли.")
    
    conn.execute("UPDATE orders SET status='cancelled' WHERE id=?", (order_id,))
    conn.commit()
    conn.close()
    send_notification(user_id, "client", f"❌ Вы отменили заказ №{order_id}.")
    return jsonify({"success": True, "data": {"status": "cancelled"}})

# -------------------------------------------------------------------
# 2. Client Bot
# -------------------------------------------------------------------
client_bot = Bot(token=CLIENT_TOKEN)
dp_client = Dispatcher()

class CreateOrder(StatesGroup):
    title = State()
    description = State()
    price = State()
    urgency = State()
    days = State()
    files = State()

def api_request(method, endpoint, data=None, params=None):
    url = f"http://localhost:{os.getenv('PORT', 8080)}{endpoint}"
    try:
        if method == "GET":
            resp = requests.get(url, params=params, timeout=10)
        else:
            resp = requests.post(url, json=data, timeout=10)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        print(f"API error: {e}")
        return {"success": False, "error": str(e)}

def register_user(tg_id, username):
    return api_request("GET", "/user/get_or_create", params={"telegram_id": tg_id, "username": username})

def get_balance(tg_id):
    return api_request("GET", "/user/balance", params={"telegram_id": tg_id})

def create_order(customer_id, title, description, price, urgency, days_to_live, files):
    data = {
        "customer_id": customer_id,
        "title": title,
        "description": description,
        "price": price,
        "urgency": urgency,
        "days_to_live": days_to_live,
        "files": files
    }
    return api_request("POST", "/order/create", data=data)

def get_my_orders(customer_id, status=None):
    return api_request("GET", "/order/list", params={"customer_id": customer_id, "status": status})

def accept_order(order_id, customer_id):
    return api_request("POST", "/order/accept", data={"order_id": order_id, "customer_id": customer_id})

def cancel_order(order_id, user_id):
    return api_request("POST", "/order/cancel", data={"order_id": order_id, "user_id": user_id})

@dp_client.message(Command("start"))
async def cmd_start(message: Message):
    tg_id = message.from_user.id
    username = message.from_user.username or ""
    register_user(tg_id, username)
    balance_data = get_balance(tg_id)
    balance = balance_data.get("data", {}).get("balance", 0) if balance_data.get("success") else 0
    await message.answer(
        f"🏗️ Добро пожаловать в биржу CAD (клиентский бот)!\n"
        f"Ваш баланс: {balance} баллов\n\n"
        "Команды:\n"
        "/new - создать заказ\n"
        "/my_orders - мои заказы\n"
        "/balance - проверить баланс\n"
        "/help - помощь"
    )

@dp_client.message(Command("balance"))
async def cmd_balance(message: Message):
    tg_id = message.from_user.id
    data = get_balance(tg_id)
    if data.get("success"):
        bal = data["data"]["balance"]
        rating = data["data"]["rating"]
        await message.answer(f"💰 Ваш баланс: {bal} баллов\n⭐ Рейтинг: {rating}")
    else:
        await message.answer("❌ Не удалось получить баланс")

@dp_client.message(Command("new"))
async def cmd_new(message: Message, state: FSMContext):
    await state.set_state(CreateOrder.title)
    await message.answer("Введите заголовок задачи:")

@dp_client.message(CreateOrder.title)
async def process_title(message: Message, state: FSMContext):
    await state.update_data(title=message.text)
    await state.set_state(CreateOrder.description)
    await message.answer("Введите описание задачи:")

@dp_client.message(CreateOrder.description)
async def process_description(message: Message, state: FSMContext):
    await state.update_data(description=message.text)
    await state.set_state(CreateOrder.price)
    await message.answer("Укажите цену в баллах (целое число):")

@dp_client.message(CreateOrder.price)
async def process_price(message: Message, state: FSMContext):
    try:
        price = int(message.text)
        if price <= 0:
            raise ValueError
        await state.update_data(price=price)
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Низкая", callback_data="low"),
             InlineKeyboardButton(text="Средняя", callback_data="medium"),
             InlineKeyboardButton(text="Высокая", callback_data="high")]
        ])
        await state.set_state(CreateOrder.urgency)
        await message.answer("Выберите срочность:", reply_markup=kb)
    except:
        await message.answer("❌ Введите целое положительное число.")

@dp_client.callback_query(CreateOrder.urgency)
async def process_urgency(callback: CallbackQuery, state: FSMContext):
    urgency = callback.data
    await state.update_data(urgency=urgency)
    await state.set_state(CreateOrder.days)
    await callback.message.answer("Через сколько дней снять заказ с биржи, если не возьмут? (введите число дней, например 7)")
    await callback.answer()

@dp_client.message(CreateOrder.days)
async def process_days(message: Message, state: FSMContext):
    try:
        days = int(message.text)
        if days < 1:
            raise ValueError
        await state.update_data(days=days)
        await state.set_state(CreateOrder.files)
        await message.answer("Приложите файлы к заданию (можно несколько, после отправки нажмите /done). Для завершения загрузки введите /done")
    except:
        await message.answer("❌ Введите целое число дней (минимум 1)")

user_files = {}
@dp_client.message(CreateOrder.files, F.document)
async def process_files(message: Message, state: FSMContext):
    tg_id = message.from_user.id
    file_id = message.document.file_id
    if tg_id not in user_files:
        user_files[tg_id] = []
    user_files[tg_id].append(file_id)
    await message.answer(f"Файл {message.document.file_name} добавлен. Можно добавить ещё или введите /done")

@dp_client.message(CreateOrder.files, Command("done"))
async def finish_files(message: Message, state: FSMContext):
    tg_id = message.from_user.id
    files = user_files.get(tg_id, [])
    data = await state.get_data()
    resp = create_order(tg_id, data["title"], data["description"], data["price"], data["urgency"], data["days"], files)
    if resp.get("success"):
        order_id = resp["data"]["order_id"]
        await message.answer(f"✅ Заказ №{order_id} создан! Списано {data['price']} баллов.")
    else:
        await message.answer(f"❌ Ошибка: {resp.get('error')}")
    if tg_id in user_files:
        del user_files[tg_id]
    await state.clear()

@dp_client.message(Command("cancel"), StateFilter(None))
async def cancel_cmd(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Создание заказа отменено.")

@dp_client.message(Command("my_orders"))
async def cmd_my_orders(message: Message):
    tg_id = message.from_user.id
    resp = get_my_orders(tg_id)
    if not resp.get("success"):
        await message.answer("❌ Не удалось получить заказы")
        return
    orders = resp.get("data", [])
    if not orders:
        await message.answer("У вас пока нет заказов.")
        return
    text = "📋 Ваши заказы:\n"
    for o in orders:
        text += f"#{o['id']} | {o['title']} | {o['status']} | {o['price']} баллов\n"
    await message.answer(text)

@dp_client.message(Command("accept"))
async def cmd_accept(message: Message):
    args = message.text.split()
    if len(args) != 2:
        await message.answer("Использование: /accept <ID заказа>")
        return
    try:
        order_id = int(args[1])
    except:
        await message.answer("ID должно быть числом")
        return
    tg_id = message.from_user.id
    resp = accept_order(order_id, tg_id)
    if resp.get("success"):
        await message.answer(f"✅ Заказ #{order_id} принят, баллы начислены исполнителю.")
    else:
        await message.answer(f"❌ Ошибка: {resp.get('error')}")

@dp_client.message(Command("cancel_order"))
async def cmd_cancel_order(message: Message):
    args = message.text.split()
    if len(args) != 2:
        await message.answer("Использование: /cancel_order <ID заказа>")
        return
    try:
        order_id = int(args[1])
    except:
        await message.answer("ID должно быть числом")
        return
    tg_id = message.from_user.id
    resp = cancel_order(order_id, tg_id)
    if resp.get("success"):
        await message.answer(f"❌ Заказ #{order_id} отменён.")
    else:
        await message.answer(f"❌ Ошибка: {resp.get('error')}")

@dp_client.message(Command("help"))
async def cmd_help(message: Message):
    await message.answer(
        "Команды бота заказчика:\n"
        "/new - создать новый заказ\n"
        "/my_orders - список моих заказов\n"
        "/accept <id> - принять выполненный заказ\n"
        "/cancel_order <id> - отменить заказ\n"
        "/balance - баланс\n"
        "/cancel - отменить создание заказа"
    )

# -------------------------------------------------------------------
# 3. Executor Bot
# -------------------------------------------------------------------
executor_bot = Bot(token=EXECUTOR_TOKEN)
dp_executor = Dispatcher()

class SubmitOrder(StatesGroup):
    waiting_for_files = State()

user_temp = {}

@dp_executor.message(Command("start"))
async def cmd_start_executor(message: Message):
    tg_id = message.from_user.id
    username = message.from_user.username or ""
    register_user(tg_id, username)
    await message.answer(
        "👷 Добро пожаловать в биржу CAD (исполнитель)!\n"
        "Команды:\n"
        "/feed - показать открытые заказы (с фильтрами)\n"
        "/take <id> - взять заказ\n"
        "/my - мои текущие заказы\n"
        "/submit <id> - сдать выполненный заказ\n"
        "/balance - баланс\n"
        "/help - помощь"
    )

@dp_executor.message(Command("balance"))
async def cmd_balance_executor(message: Message):
    data = get_balance(message.from_user.id)
    if data.get("success"):
        bal = data["data"]["balance"]
        rating = data["data"]["rating"]
        await message.answer(f"💰 Ваш баланс: {bal} баллов\n⭐ Рейтинг: {rating}")
    else:
        await message.answer("❌ Ошибка получения баланса")

@dp_executor.message(Command("feed"))
async def cmd_feed(message: Message):
    resp = api_request("GET", "/order/list", params={"status": "open", "limit": 10})
    if not resp.get("success"):
        await message.answer("❌ Ошибка получения списка")
        return
    orders = resp.get("data", [])
    if not orders:
        await message.answer("Нет открытых заказов.")
        return
    for o in orders:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Взять", callback_data=f"take_{o['id']}")]
        ])
        text = f"🔹 #{o['id']} | {o['title']}\n💰 {o['price']} баллов\n🔥 Срочность: {o['urgency']}\n📅 Снятие: {o['expires_at'][:10]}\n{o['description'][:100]}"
        await message.answer(text, reply_markup=kb)

@dp_executor.callback_query(lambda c: c.data.startswith("take_"))
async def callback_take(callback: CallbackQuery):
    order_id = int(callback.data.split("_")[1])
    executor_id = callback.from_user.id
    resp = api_request("POST", "/order/take", data={"order_id": order_id, "executor_id": executor_id})
    if resp.get("success"):
        await callback.message.answer(f"✅ Вы взяли заказ #{order_id} в работу.")
        await callback.message.edit_reply_markup(reply_markup=None)
    else:
        await callback.message.answer(f"❌ Не удалось взять: {resp.get('error')}")
    await callback.answer()

@dp_executor.message(Command("take"))
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
    resp = api_request("POST", "/order/take", data={"order_id": order_id, "executor_id": message.from_user.id})
    if resp.get("success"):
        await message.answer(f"✅ Вы взяли заказ #{order_id}.")
    else:
        await message.answer(f"❌ Ошибка: {resp.get('error')}")

@dp_executor.message(Command("my"))
async def cmd_my_orders_executor(message: Message):
    tg_id = message.from_user.id
    resp = api_request("GET", "/order/list", params={"executor_id": tg_id})
    if not resp.get("success"):
        await message.answer("Ошибка")
        return
    orders = resp.get("data", [])
    if not orders:
        await message.answer("У вас нет взятых заказов.")
        return
    text = "📌 Ваши заказы:\n"
    for o in orders:
        text += f"#{o['id']} | {o['title']} | {o['status']}\n"
    await message.answer(text)

@dp_executor.message(Command("submit"))
async def cmd_submit(message: Message, state: FSMContext):
    args = message.text.split()
    if len(args) != 2:
        await message.answer("Использование: /submit <id заказа>")
        return
    try:
        order_id = int(args[1])
    except:
        await message.answer("ID должно быть числом")
        return
    
    order = api_request("GET", "/order/get", params={"id": order_id})
    if not order.get("success"):
        await message.answer("❌ Заказ не найден")
        return
    order_data = order["data"]
    if order_data["executor_id"] != message.from_user.id:
        await message.answer("❌ Вы не исполнитель этого заказа")
        return
    if order_data["status"] != "in_progress":
        await message.answer("❌ Заказ не в статусе 'в работе'")
        return
    
    user_temp[message.from_user.id] = order_id
    await state.set_state(SubmitOrder.waiting_for_files)
    await message.answer("Пришлите файлы с результатом работы (можно несколько). Когда закончите, введите /done_files")

@dp_executor.message(SubmitOrder.waiting_for_files, F.document)
async def submit_files(message: Message, state: FSMContext):
    tg_id = message.from_user.id
    if tg_id not in user_temp:
        await message.answer("Начните с /submit")
        return
    if 'result_files' not in user_temp:
        user_temp['result_files'] = {}
    if tg_id not in user_temp['result_files']:
        user_temp['result_files'][tg_id] = []
    user_temp['result_files'][tg_id].append(message.document.file_id)
    await message.answer(f"Файл {message.document.file_name} добавлен. Ещё или /done_files")

@dp_executor.message(SubmitOrder.waiting_for_files, Command("done_files"))
async def finish_submit(message: Message, state: FSMContext):
    tg_id = message.from_user.id
    order_id = user_temp.get(tg_id)
    files = user_temp.get('result_files', {}).get(tg_id, [])
    if not order_id:
        await message.answer("Ошибка: не найден заказ. Повторите /submit")
        return
    
    resp = api_request("POST", "/order/submit", data={"order_id": order_id, "executor_id": tg_id, "result_files": files})
    if resp.get("success"):
        await message.answer(f"✅ Решение по заказу #{order_id} отправлено заказчику.")
    else:
        await message.answer(f"❌ Ошибка: {resp.get('error')}")
    
    if tg_id in user_temp:
        del user_temp[tg_id]
    if tg_id in user_temp.get('result_files', {}):
        del user_temp['result_files'][tg_id]
    await state.clear()

@dp_executor.message(Command("help"))
async def cmd_help_executor(message: Message):
    await message.answer(
        "Команды исполнителя:\n"
        "/feed - список открытых заказов\n"
        "/take <id> - взять заказ\n"
        "/my - мои текущие заказы\n"
        "/submit <id> - сдать решение\n"
        "/balance - баланс\n"
        "/cancel - отменить текущую операцию"
    )

@dp_executor.message(Command("cancel"))
async def cmd_cancel_state(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Отменено.")

# -------------------------------------------------------------------
# 4. Запуск всех компонентов в одном процессе
# -------------------------------------------------------------------
def run_flask():
    port = int(os.getenv("PORT", 8080))
    flask_app.run(host="0.0.0.0", port=port)

async def run_bots():
    await asyncio.gather(
        dp_client.start_polling(client_bot),
        dp_executor.start_polling(executor_bot)
    )

if __name__ == "__main__":
    threading.Thread(target=run_flask, daemon=True).start()
    asyncio.run(run_bots())
