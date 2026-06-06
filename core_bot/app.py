import os
import sqlite3
import json
from datetime import datetime, timedelta
from flask import Flask, request, jsonify
from apscheduler.schedulers.background import BackgroundScheduler
import requests
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)

# Конфигурация токенов (из переменных окружения)
CORE_BOT_TOKEN = os.getenv("CORE_BOT_TOKEN")          # для себя (не используется в отправке)
CLIENT_BOT_TOKEN = os.getenv("CLIENT_BOT_TOKEN")      # бот для заказчиков
EXECUTOR_BOT_TOKEN = os.getenv("EXECUTOR_BOT_TOKEN")  # бот для исполнителей

DB_PATH = "cad_exchange.db"

# -------------------------------------------------------------------
# 1. Инициализация базы данных
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
            files TEXT,                 -- JSON-массив file_id
            price INTEGER NOT NULL,
            urgency TEXT CHECK(urgency IN ('low','medium','high')) DEFAULT 'medium',
            days_to_live INTEGER NOT NULL,
            status TEXT CHECK(status IN ('open','in_progress','completed','closed','expired','cancelled')) DEFAULT 'open',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            expires_at TIMESTAMP,
            taken_at TIMESTAMP,
            completed_at TIMESTAMP,
            result_files TEXT,          -- JSON-массив file_id результата
            FOREIGN KEY (customer_id) REFERENCES users(telegram_id),
            FOREIGN KEY (executor_id) REFERENCES users(telegram_id)
        )
    ''')
    # Индексы для скорости
    c.execute('CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status)')
    c.execute('CREATE INDEX IF NOT EXISTS idx_orders_expires ON orders(expires_at)')
    conn.commit()
    conn.close()

init_db()

# -------------------------------------------------------------------
# 2. Вспомогательные функции для работы с БД
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
# 3. Отправка уведомлений через соответствующего бота
# -------------------------------------------------------------------
def send_notification(telegram_id, bot_type, text):
    """
    bot_type: 'client' или 'executor'
    """
    if bot_type == 'client':
        token = CLIENT_BOT_TOKEN
    else:
        token = EXECUTOR_BOT_TOKEN
    if not token or token == "заглушка":
        print(f"Уведомление для {telegram_id} (тип {bot_type}): {text}")
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        requests.post(url, json={"chat_id": telegram_id, "text": text}, timeout=5)
    except Exception as e:
        print(f"Ошибка отправки уведомления: {e}")

# -------------------------------------------------------------------
# 4. Планировщик: автоматическое снятие просроченных заказов
# -------------------------------------------------------------------
def expire_orders():
    conn = get_db_connection()
    now = datetime.utcnow().isoformat()
    # Находим просроченные открытые заказы
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
# 5. Эндпоинты API
# -------------------------------------------------------------------

# 5.1. Получить или создать пользователя
@app.route("/user/get_or_create", methods=["GET"])
def api_user_get_or_create():
    telegram_id = request.args.get("telegram_id", type=int)
    username = request.args.get("username", "")
    if not telegram_id:
        return jsonify({"success": False, "error": "telegram_id required"}), 400
    user = get_user(telegram_id, username)
    return jsonify({"success": True, "data": user})

# 5.2. Баланс пользователя
@app.route("/user/balance", methods=["GET"])
def api_user_balance():
    telegram_id = request.args.get("telegram_id", type=int)
    if not telegram_id:
        return jsonify({"success": False, "error": "telegram_id required"}), 400
    user = get_user(telegram_id)
    if not user:
        return jsonify({"success": False, "error": "User not found"}), 404
    return jsonify({"success": True, "data": {"balance": user["balance"], "rating": user["rating"]}})

# 5.3. Создание заказа
@app.route("/order/create", methods=["POST"])
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
    # Проверка баланса заказчика
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
    # Списание баллов
    update_balance(customer_id, -price)
    return jsonify({"success": True, "data": {"order_id": order_id}})

# 5.4. Список заказов (с фильтрами и пагинацией)
@app.route("/order/list", methods=["GET"])
def api_order_list():
    # Параметры фильтрации
    status = request.args.get("status", "open")
    urgency = request.args.get("urgency")
    price_min = request.args.get("price_min", type=int)
    price_max = request.args.get("price_max", type=int)
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
    query += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
    params.extend([limit, offset])

    conn = get_db_connection()
    rows = conn.execute(query, params).fetchall()
    orders = [dict(row) for row in rows]
    conn.close()
    return jsonify({"success": True, "data": orders})

# 5.5. Детали одного заказа
@app.route("/order/get", methods=["GET"])
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

# 5.6. Взятие заказа
@app.route("/order/take", methods=["POST"])
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
    # Проверяем, не истёк ли срок
    if datetime.utcnow().isoformat() > order["expires_at"]:
        conn.execute("UPDATE orders SET status='expired' WHERE id=?", (order_id,))
        conn.commit()
        conn.close()
        return jsonify({"success": False, "error": "Order expired"}), 400
    # Берём заказ
    now = datetime.utcnow().isoformat()
    conn.execute(
        "UPDATE orders SET executor_id=?, status='in_progress', taken_at=? WHERE id=?",
        (executor_id, now, order_id)
    )
    conn.commit()
    conn.close()
    # Уведомление заказчику
    send_notification(order["customer_id"], "client",
                      f"🔧 Исполнитель @id{executor_id} взял ваш заказ №{order_id} в работу.")
    return jsonify({"success": True, "data": {"status": "in_progress"}})

# 5.7. Сдача выполненного заказа
@app.route("/order/submit", methods=["POST"])
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

# 5.8. Приёмка работы (начисление баллов)
@app.route("/order/accept", methods=["POST"])
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
    # Начисляем баллы исполнителю
    executor_id = order["executor_id"]
    reward = order["price"]
    update_balance(executor_id, reward)
    # Меняем статус
    conn.execute("UPDATE orders SET status='closed' WHERE id=?", (order_id,))
    conn.commit()
    conn.close()
    # Уведомления
    send_notification(executor_id, "executor",
                      f"🎉 Заказчик принял вашу работу по заказу №{order_id}. Вам начислено {reward} баллов.")
    send_notification(customer_id, "client",
                      f"✅ Вы приняли работу по заказу №{order_id}. Спасибо за использование биржи!")
    return jsonify({"success": True, "data": {"status": "closed"}})

# 5.9. Отмена заказа (заказчиком или системой)
@app.route("/order/cancel", methods=["POST"])
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
    # Отменить может только заказчик
    if order["customer_id"] != user_id:
        conn.close()
        return jsonify({"success": False, "error": "Only customer can cancel"}), 403
    if order["status"] not in ("open", "in_progress"):
        conn.close()
        return jsonify({"success": False, "error": "Cannot cancel order in current status"}), 400
    old_status = order["status"]
    # Возвращаем баллы заказчику, если заказ ещё открыт
    if old_status == "open":
        update_balance(user_id, order["price"])
    # Если заказ был взят, уведомляем исполнителя
    if old_status == "in_progress" and order["executor_id"]:
        send_notification(order["executor_id"], "executor",
                          f"⚠️ Заказчик отменил заказ №{order_id}, который вы выполняли.")
    conn.execute("UPDATE orders SET status='cancelled' WHERE id=?", (order_id,))
    conn.commit()
    conn.close()
    send_notification(user_id, "client", f"❌ Вы отменили заказ №{order_id}.")
    return jsonify({"success": True, "data": {"status": "cancelled"}})

# -------------------------------------------------------------------
# 6. Запуск
# -------------------------------------------------------------------
if __name__ == "__main__":
    print("=== ЗАРЕГИСТРИРОВАННЫЕ МАРШРУТЫ ===")
    for rule in app.url_map.iter_rules():
        print(f"{rule.endpoint}: {', '.join(rule.methods)} {rule}")
    print("==================================")
    app.run(host="0.0.0.0", port=5000, debug=False)
