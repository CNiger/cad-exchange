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
from aiogram.dispatcher.filters import Command, Text
from aiogram.contrib.fsm_storage.memory import MemoryStorage
from aiogram.dispatcher import FSMContext
from aiogram.dispatcher.filters.state import State, StatesGroup
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
import requests
import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

# ---------- Настройки ----------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
load_dotenv()

CLIENT_TOKEN = os.getenv("CLIENT_BOT_TOKEN")
EXECUTOR_TOKEN = os.getenv("EXECUTOR_BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL not set")

# ---------- PostgreSQL ----------
def get_db_connection():
    return psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)

def init_db():
    conn = get_db_connection()
    with conn:
        with conn.cursor() as c:
            # Таблица пользователей (добавлены поля для рейтинга)
            c.execute('''
                CREATE TABLE IF NOT EXISTS users (
                    telegram_id BIGINT PRIMARY KEY,
                    username TEXT,
                    balance INTEGER DEFAULT 20,
                    real_balance DECIMAL(10,2) DEFAULT 0.00,
                    rating REAL DEFAULT 0.0,
                    completed_orders INTEGER DEFAULT 0,
                    cancelled_orders INTEGER DEFAULT 0,
                    expired_orders INTEGER DEFAULT 0,
                    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            # Таблица заказов (добавлен critical)
            c.execute('''
                CREATE TABLE IF NOT EXISTS orders (
                    id SERIAL PRIMARY KEY,
                    customer_id BIGINT NOT NULL,
                    executor_id BIGINT DEFAULT NULL,
                    title TEXT NOT NULL,
                    description TEXT,
                    files TEXT,
                    price INTEGER NOT NULL,
                    urgency TEXT CHECK(urgency IN ('low','medium','high','critical')) DEFAULT 'medium',
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
            # Таблица оценок (для рейтинга)
            c.execute('''
                CREATE TABLE IF NOT EXISTS reviews (
                    id SERIAL PRIMARY KEY,
                    order_id INTEGER NOT NULL REFERENCES orders(id),
                    executor_id BIGINT NOT NULL REFERENCES users(telegram_id),
                    customer_id BIGINT NOT NULL REFERENCES users(telegram_id),
                    score INTEGER CHECK (score >= 1 AND score <= 5),
                    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                )
            ''')
            c.execute('CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status)')
            c.execute('CREATE INDEX IF NOT EXISTS idx_orders_expires ON orders(expires_at)')
            c.execute('CREATE INDEX IF NOT EXISTS idx_orders_urgency ON orders(urgency)')
    conn.close()
    logger.info("Database initialized (critical + reviews)")

init_db()

# ---------- Работа с пользователями ----------
def get_user(telegram_id, username=None):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT * FROM users WHERE telegram_id = %s", (telegram_id,))
    user = cur.fetchone()
    if not user:
        if username is None:
            username = f"user_{telegram_id}"
        cur.execute(
            "INSERT INTO users (telegram_id, username) VALUES (%s, %s)",
            (telegram_id, username)
        )
        conn.commit()
        cur.execute("SELECT * FROM users WHERE telegram_id = %s", (telegram_id,))
        user = cur.fetchone()
    cur.close()
    conn.close()
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

def update_user_stats(telegram_id, field, delta=1):
    """Обновляет счётчики completed_orders, cancelled_orders, expired_orders"""
    conn = get_db_connection()
    with conn:
        with conn.cursor() as c:
            c.execute(
                f"UPDATE users SET {field} = {field} + %s WHERE telegram_id = %s",
                (delta, telegram_id)
            )
    conn.close()

# ---------- Уведомления ----------
def send_notification(telegram_id, bot_type, text):
    token = CLIENT_TOKEN if bot_type == 'client' else EXECUTOR_TOKEN
    if not token:
        logger.info(f"Уведомление для {telegram_id}: {text}")
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": telegram_id, "text": text},
            timeout=5
        )
    except Exception as e:
        logger.error(f"Ошибка отправки: {e}")

# ---------- Планировщик (каждые 5 минут) ----------
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
                # Обновляем статистику заказчика
                update_user_stats(order["customer_id"], "expired_orders")
                send_notification(order["customer_id"], "client", f"⏰ Заказ №{order['id']} снят")
    conn.close()

scheduler = BackgroundScheduler()
scheduler.add_job(func=expire_orders, trigger="interval", minutes=5)
scheduler.start()

# ---------- Бизнес-логика ----------
def create_order_logic(customer_id, title, description, price, urgency, hours_to_live, files):
    user = get_user(customer_id)
    if not user:
        return {"success": False, "error": "Пользователь не найден"}
    if user["balance"] < price:
        return {"success": False, "error": "Недостаточно баллов"}

    # Critical: принудительно 30 минут
    if urgency == "critical":
        hours_to_live = 0.5

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
                update_user_stats(order["customer_id"], "expired_orders")
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
            update_user_stats(executor_id, "completed_orders")
            c.execute("UPDATE orders SET status='closed' WHERE id = %s", (order_id,))
    conn.close()
    send_notification(executor_id, "executor", f"🎉 Заказчик принял работу. Вам начислено {reward} баллов")
    send_notification(customer_id, "client", f"✅ Вы приняли работу по заказу №{order_id}")
    # Запрос оценки
    ask_for_review(customer_id, executor_id, order_id)
    return {"success": True}

def ask_for_review(customer_id, executor_id, order_id):
    """Отправляет запрос на оценку исполнителя"""
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⭐ 1", callback_data=f"rate_{order_id}_1"),
         InlineKeyboardButton(text="⭐ 2", callback_data=f"rate_{order_id}_2"),
         InlineKeyboardButton(text="⭐ 3", callback_data=f"rate_{order_id}_3"),
         InlineKeyboardButton(text="⭐ 4", callback_data=f"rate_{order_id}_4"),
         InlineKeyboardButton(text="⭐ 5", callback_data=f"rate_{order_id}_5")]
    ])
    send_notification(
        customer_id,
        "client",
        f"⭐ Оцените исполнителя за заказ №{order_id}:",
        reply_markup=kb
    )

def rate_executor_logic(order_id, customer_id, score):
    """Сохраняет оценку в таблицу reviews и обновляет рейтинг исполнителя"""
    conn = get_db_connection()
    with conn:
        with conn.cursor() as c:
            # Проверяем, что заказ существует и принадлежит заказчику
            c.execute("SELECT executor_id FROM orders WHERE id = %s AND customer_id = %s", (order_id, customer_id))
            order = c.fetchone()
            if not order:
                conn.close()
                return {"success": False, "error": "Заказ не найден"}
            executor_id = order["executor_id"]
            # Сохраняем оценку
            c.execute(
                "INSERT INTO reviews (order_id, executor_id, customer_id, score) VALUES (%s, %s, %s, %s)",
                (order_id, executor_id, customer_id, score)
            )
            # Пересчитываем средний рейтинг исполнителя
            c.execute(
                "SELECT AVG(score) as avg_rating FROM reviews WHERE executor_id = %s",
                (executor_id,)
            )
            avg = c.fetchone()["avg_rating"]
            c.execute(
                "UPDATE users SET rating = %s WHERE telegram_id = %s",
                (avg if avg else 0, executor_id)
            )
    conn.close()
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
                update_user_stats(order["executor_id"], "cancelled_orders")
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

# ---------- Flask ----------
flask_app = Flask(__name__)

@flask_app.route('/')
@flask_app.route('/health')
def health():
    return jsonify({"status": "ok", "database": "postgresql"})

# ---------- Client Bot ----------
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
    user = get_user(message.from_user.id, message.from_user.username)
    await message.answer(
        f"🏗️ Добро пожаловать!\nВаш баланс: {user['balance']} баллов\n\n"
        "/new - создать заказ\n/balance - баланс\n/profile - мой профиль\n/help - помощь"
    )

@dp_client.message_handler(Command("balance"))
async def cmd_balance(message: Message):
    user = get_user(message.from_user.id)
    await message.answer(f"💰 Баланс: {user['balance']} баллов\n⭐ Рейтинг: {user['rating']}")

@dp_client.message_handler(Command("profile"))
async def cmd_profile(message: Message):
    user = get_user(message.from_user.id)
    await message.answer(
        f"👤 Ваш профиль:\n"
        f"⭐ Рейтинг: {user['rating']}\n"
        f"✅ Завершено: {user['completed_orders']}\n"
        f"❌ Отменено: {user['cancelled_orders']}\n"
        f"⏰ Просрочено: {user['expired_orders']}\n"
        f"💰 Баланс: {user['balance']} баллов"
    )

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
            [InlineKeyboardButton(text="🔴 Сверхсрочная (30 мин)", callback_data="critical")],
            [InlineKeyboardButton(text="🟥 Высокая (1 час)", callback_data="high"),
             InlineKeyboardButton(text="🟨 Средняя (24 часа)", callback_data="medium")],
            [InlineKeyboardButton(text="🟩 Низкая (сколько укажу)", callback_data="low")]
        ])
        await CreateOrder.next()
        await message.answer("Выберите срочность:", reply_markup=kb)
    except:
        await message.answer("❌ Введите положительное число")

@dp_client.callback_query_handler(lambda c: c.data in ['critical', 'high', 'medium', 'low'], state=CreateOrder.urgency)
async def process_urgency(callback: CallbackQuery, state: FSMContext):
    urgency = callback.data
    async with state.proxy() as data:
        data['urgency'] = urgency
    messages = {
        "critical": "🔴 Сверхсрочный заказ! Будет снят через 30 минут.",
        "high": "🟥 Высокая — снятие через 1 час",
        "medium": "🟨 Средняя — снятие через 24 часа",
        "low": "🟩 Низкая — через указанное количество часов"
    }
    await callback.message.answer(messages[urgency])
    await CreateOrder.next()
    if urgency == "critical":
        # Для critical пропускаем ввод часов
        async with state.proxy() as data:
            data['hours'] = 0.5
        await CreateOrder.next()
        await callback.message.answer("📎 Присылайте файлы. Когда закончите, введите /done")
    else:
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
        data.get('hours', 0.5),
        data.get('files', [])
    )
    if result.get("success"):
        await message.answer(f"✅ Заказ №{result['order_id']} создан! Списано {data['price']} баллов.")
    else:
        await message.answer(f"❌ Ошибка: {result.get('error')}")
    await state.finish()

@dp_client.callback_query_handler(lambda c: c.data.startswith("rate_"))
async def rate_callback(callback: CallbackQuery):
    parts = callback.data.split("_")
    order_id = int(parts[1])
    score = int(parts[2])
    result = rate_executor_logic(order_id, callback.from_user.id, score)
    if result.get("success"):
        await callback.message.answer(f"✅ Спасибо за оценку! ⭐ {score}")
    else:
        await callback.message.answer(f"❌ Ошибка: {result.get('error')}")
    await callback.answer()

# ---------- Executor Bot ----------
executor_bot = Bot(token=EXECUTOR_TOKEN)
dp_executor = Dispatcher(executor_bot, storage=MemoryStorage())

class SubmitOrder(StatesGroup):
    waiting_for_files = State()

user_temp = {}
user_filters = {}

def get_filter_keyboard(current_filters=None):
    """Генерирует клавиатуру фильтров"""
    if current_filters is None:
        current_filters = {}
    urgency = current_filters.get("urgency", "all")
    price = current_filters.get("price", "all")
    
    kb = InlineKeyboardMarkup(row_width=3)
    # Срочность
    kb.add(
        InlineKeyboardButton("🔴 Critical" + (" ✅" if urgency == "critical" else ""), callback_data="filter_urgency_critical"),
        InlineKeyboardButton("🟥 High" + (" ✅" if urgency == "high" else ""), callback_data="filter_urgency_high"),
        InlineKeyboardButton("🟨 Medium" + (" ✅" if urgency == "medium" else ""), callback_data="filter_urgency_medium"),
        InlineKeyboardButton("🟩 Low" + (" ✅" if urgency == "low" else ""), callback_data="filter_urgency_low"),
        InlineKeyboardButton("📋 Все" + (" ✅" if urgency == "all" else ""), callback_data="filter_urgency_all")
    )
    # Цена
    kb.add(
        InlineKeyboardButton("💰 До 10" + (" ✅" if price == "0-10" else ""), callback_data="filter_price_0-10"),
        InlineKeyboardButton("💰 10-50" + (" ✅" if price == "10-50" else ""), callback_data="filter_price_10-50"),
        InlineKeyboardButton("💰 50-100" + (" ✅" if price == "50-100" else ""), callback_data="filter_price_50-100"),
        InlineKeyboardButton("💰 100+" + (" ✅" if price == "100+" else ""), callback_data="filter_price_100+"),
        InlineKeyboardButton("💰 Любая" + (" ✅" if price == "all" else ""), callback_data="filter_price_all")
    )
    # Действия
    kb.add(
        InlineKeyboardButton("🔄 Сбросить фильтры", callback_data="filter_reset"),
        InlineKeyboardButton("📋 Обновить ленту", callback_data="filter_refresh")
    )
    return kb

@dp_executor.message_handler(Command("start"))
async def cmd_start_executor(message: Message):
    get_user(message.from_user.id, message.from_user.username)
    await message.answer(
        "👷 Биржа CAD (исполнитель)!\n"
        "Используйте кнопки для управления лентой.\n\n"
        "/feed - показать заказы\n"
        "/filters - настроить фильтры\n"
        "/take <id> - взять заказ\n"
        "/my - мои заказы\n"
        "/submit <id> - сдать\n"
        "/profile - мой профиль\n"
        "/balance - баланс"
    )

@dp_executor.message_handler(Command("balance"))
async def cmd_balance_executor(message: Message):
    user = get_user(message.from_user.id)
    await message.answer(f"💰 Баланс: {user['balance']} баллов\n⭐ Рейтинг: {user['rating']}")

@dp_executor.message_handler(Command("profile"))
async def cmd_profile_executor(message: Message):
    user = get_user(message.from_user.id)
    await message.answer(
        f"👤 Ваш профиль исполнителя:\n"
        f"⭐ Рейтинг: {user['rating']}\n"
        f"✅ Завершено: {user['completed_orders']}\n"
        f"❌ Отменено: {user['cancelled_orders']}\n"
        f"⏰ Просрочено: {user['expired_orders']}\n"
        f"💰 Баланс: {user['balance']} баллов"
    )

@dp_executor.message_handler(Command("filters"))
async def cmd_filters(message: Message):
    tg_id = message.from_user.id
    current = user_filters.get(tg_id, {})
    kb = get_filter_keyboard(current)
    await message.answer("🔍 Выберите фильтры:", reply_markup=kb)

@dp_executor.callback_query_handler(lambda c: c.data.startswith("filter_"))
async def filter_callback(callback: CallbackQuery):
    tg_id = callback.from_user.id
    action = callback.data.replace("filter_", "")
    
    if action == "reset":
        user_filters[tg_id] = {}
        await callback.message.answer("🔄 Фильтры сброшены.")
        await callback.answer()
        return
    
    if action == "refresh":
        await callback.message.answer("🔄 Лента обновлена.")
        await callback.answer()
        # Показываем ленту с текущими фильтрами
        await show_feed(callback.message, tg_id)
        return
    
    # Парсим фильтры
    parts = action.split("_")
    if len(parts) != 2:
        await callback.answer()
        return
    
    filter_type, value = parts[0], parts[1]
    if tg_id not in user_filters:
        user_filters[tg_id] = {}
    
    if filter_type == "urgency":
        user_filters[tg_id]["urgency"] = value if value != "all" else None
    elif filter_type == "price":
        user_filters[tg_id]["price"] = value if value != "all" else None
    
    # Обновляем клавиатуру
    current = user_filters[tg_id]
    kb = get_filter_keyboard(current)
    await callback.message.edit_text("🔍 Выберите фильтры:", reply_markup=kb)
    await callback.answer()

async def show_feed(message, tg_id, offset=0):
    """Показывает ленту с учётом фильтров и пагинации"""
    filters = user_filters.get(tg_id, {})
    
    # Преобразуем фильтры в параметры для БД
    db_filters = {"status": "open", "limit": 5, "offset": offset}
    
    # Срочность
    if filters.get("urgency"):
        db_filters["urgency"] = filters["urgency"]
    
    # Цена
    price_filter = filters.get("price")
    if price_filter:
        if price_filter == "0-10":
            db_filters["price_min"] = 0
            db_filters["price_max"] = 10
        elif price_filter == "10-50":
            db_filters["price_min"] = 10
            db_filters["price_max"] = 50
        elif price_filter == "50-100":
            db_filters["price_min"] = 50
            db_filters["price_max"] = 100
        elif price_filter == "100+":
            db_filters["price_min"] = 100
    
    result = get_orders_logic(db_filters)
    if not result.get("success"):
        await message.answer("❌ Ошибка загрузки заказов")
        return
    
    orders = result.get("data", [])
    if not orders:
        await message.answer("📭 Нет заказов, соответствующих фильтрам.")
        return
    
    for o in orders:
        expires_dt = datetime.fromisoformat(o['expires_at'])
        expires_str = expires_dt.strftime("%d.%m.%Y %H:%M")
        
        # Иконки срочности
        urgency_icons = {
            "critical": "🔴",
            "high": "🟥",
            "medium": "🟨",
            "low": "🟩"
        }
        icon = urgency_icons.get(o['urgency'], "🟩")
        
        has_files = o.get('files') and o['files'] != '[]'
        text = (
            f"{icon} #{o['id']} | {o['title']}{' 📎' if has_files else ''}\n"
            f"💰 {o['price']} баллов\n"
            f"⏳ Снятие: {expires_str}\n"
            f"📝 {o['description'][:80]}"
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Взять", callback_data=f"take_{o['id']}")]
        ])
        await message.answer(text, reply_markup=kb)
        if has_files:
            try:
                for file_id in json.loads(o['files']):
                    await message.answer_document(file_id)
            except:
                pass
    
    # Пагинация
    nav_kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton("⬅️ Назад", callback_data=f"page_{offset-5}"),
            InlineKeyboardButton(f"📄 {offset//5 + 1}", callback_data="page_info"),
            InlineKeyboardButton("Вперед ➡️", callback_data=f"page_{offset+5}")
        ],
        [InlineKeyboardButton("🔍 Фильтры", callback_data="show_filters")]
    ])
    await message.answer("📋 Навигация:", reply_markup=nav_kb)

@dp_executor.callback_query_handler(lambda c: c.data.startswith("page_"))
async def page_callback(callback: CallbackQuery):
    offset = int(callback.data.split("_")[1])
    if offset < 0:
        offset = 0
    await show_feed(callback.message, callback.from_user.id, offset)
    await callback.answer()

@dp_executor.callback_query_handler(lambda c: c.data == "show_filters")
async def show_filters_callback(callback: CallbackQuery):
    tg_id = callback.from_user.id
    current = user_filters.get(tg_id, {})
    kb = get_filter_keyboard(current)
    await callback.message.answer("🔍 Выберите фильтры:", reply_markup=kb)
    await callback.answer()

@dp_executor.message_handler(Command("feed"))
async def cmd_feed(message: Message):
    await show_feed(message, message.from_user.id, 0)

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
    status_emoji = {"in_progress": "🔄", "completed": "⏳", "closed": "✅", "cancelled": "❌", "expired": "⏰"}
    for o in orders:
        text += f"{status_emoji.get(o['status'], '❓')} #{o['id']} | {o['title']} | {o['status']}\n"
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
    if not orders_result.get("success") or not orders_result.get("data"):
        await message.answer("❌ Заказ не найден")
        return
    order = orders_result.get("data")[0]
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
    user_temp.setdefault('result_files', {})
    user_temp['result_files'].setdefault(tg_id, []).append(message.document.file_id)
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
    user_temp.pop(tg_id, None)
    user_temp.get('result_files', {}).pop(tg_id, None)
    await state.finish()

@dp_executor.message_handler(Command("help"))
async def cmd_help_executor(message: Message):
    await message.answer(
        "👷 Команды исполнителя:\n"
        "/feed - лента заказов\n"
        "/filters - настройка фильтров\n"
        "/take <id> - взять заказ\n"
        "/my - мои заказы\n"
        "/submit <id> - сдать решение\n"
        "/profile - мой профиль\n"
        "/balance - баланс\n"
        "/cancel - отменить текущую операцию"
    )

@dp_executor.message_handler(Command("cancel"), state='*')
async def cmd_cancel_state(message: Message, state: FSMContext):
    await state.finish()
    await message.answer("Отменено.")

# ---------- Запуск ----------
def run_flask():
    flask_app.run(host="0.0.0.0", port=int(os.getenv("PORT", 10000)), use_reloader=False)

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
    logger.info("🚀 Запуск CAD Exchange (critical + filters + rating)")
    threading.Thread(target=run_flask, daemon=True).start()
    try:
        asyncio.run(run_bots_async())
    except KeyboardInterrupt:
        logger.info("Остановка...")
    except Exception as e:
        logger.error(f"Критическая ошибка: {e}")
        sys.exit(1)
