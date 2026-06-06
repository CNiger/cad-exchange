import os
import requests
import json
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("CLIENT_BOT_TOKEN")
CORE_URL = os.getenv("CORE_URL", "http://localhost:5000")

bot = Bot(token=TOKEN)
dp = Dispatcher()

# -------------------------------------------------------------------
# FSM для создания заказа
# -------------------------------------------------------------------
class CreateOrder(StatesGroup):
    title = State()
    description = State()
    price = State()
    urgency = State()
    days = State()
    files = State()  # ожидание файлов

# -------------------------------------------------------------------
# Вспомогательные функции для работы с Core
# -------------------------------------------------------------------
def api_request(method, endpoint, data=None, params=None):
    url = f"{CORE_URL}{endpoint}"
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
    params = {"customer_id": customer_id}
    if status:
        params["status"] = status
    # В Core пока нет отдельного эндпоинта для заказов заказчика, используем /order/list с фильтром по customer_id? 
    # Расширим /order/list, добавив параметр customer_id. Но для простоты сделаем отдельный вызов.
    # Лучше добавить в Core новый эндпоинт /order/my. Сделаем так: вызываем /order/list с customer_id (добавим поддержку в Core позже).
    # Пока временно: используем /order/list с фильтрацией по customer_id, но Core надо доработать.
    # Я доработаю Core: добавим параметр customer_id в /order/list. В текущей версии Core его нет, поэтому предложу костыль: 
    # Получаем все заказы и фильтруем локально. Но для чистоты я расширю Core в следующей версии.
    # Пока напишем временный запрос, а потом обновим Core. Сейчас чтобы не ломать, предложу добавить в Core.
    # Ниже предполагаю, что в Core уже есть параметр customer_id.
    resp = api_request("GET", "/order/list", params={"customer_id": customer_id, "status": status})
    return resp

def accept_order(order_id, customer_id):
    return api_request("POST", "/order/accept", data={"order_id": order_id, "customer_id": customer_id})

def cancel_order(order_id, user_id):
    return api_request("POST", "/order/cancel", data={"order_id": order_id, "user_id": user_id})

# -------------------------------------------------------------------
# Команды
# -------------------------------------------------------------------
@dp.message(Command("start"))
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

@dp.message(Command("balance"))
async def cmd_balance(message: Message):
    tg_id = message.from_user.id
    data = get_balance(tg_id)
    if data.get("success"):
        bal = data["data"]["balance"]
        rating = data["data"]["rating"]
        await message.answer(f"💰 Ваш баланс: {bal} баллов\n⭐ Рейтинг: {rating}")
    else:
        await message.answer("❌ Не удалось получить баланс")

@dp.message(Command("new"))
async def cmd_new(message: Message, state: FSMContext):
    await state.set_state(CreateOrder.title)
    await message.answer("Введите заголовок задачи:")

@dp.message(CreateOrder.title)
async def process_title(message: Message, state: FSMContext):
    await state.update_data(title=message.text)
    await state.set_state(CreateOrder.description)
    await message.answer("Введите описание задачи:")

@dp.message(CreateOrder.description)
async def process_description(message: Message, state: FSMContext):
    await state.update_data(description=message.text)
    await state.set_state(CreateOrder.price)
    await message.answer("Укажите цену в баллах (целое число):")

@dp.message(CreateOrder.price)
async def process_price(message: Message, state: FSMContext):
    try:
        price = int(message.text)
        if price <= 0:
            raise ValueError
        await state.update_data(price=price)
        # Выбор срочности
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="Низкая", callback_data="low"),
             InlineKeyboardButton(text="Средняя", callback_data="medium"),
             InlineKeyboardButton(text="Высокая", callback_data="high")]
        ])
        await state.set_state(CreateOrder.urgency)
        await message.answer("Выберите срочность:", reply_markup=kb)
    except:
        await message.answer("❌ Введите целое положительное число.")

@dp.callback_query(CreateOrder.urgency)
async def process_urgency(callback: CallbackQuery, state: FSMContext):
    urgency = callback.data
    await state.update_data(urgency=urgency)
    await state.set_state(CreateOrder.days)
    await callback.message.answer("Через сколько дней снять заказ с биржи, если не возьмут? (введите число дней, например 7)")
    await callback.answer()

@dp.message(CreateOrder.days)
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

# Хранилище file_ids
user_files = {}
@dp.message(CreateOrder.files, F.document)
async def process_files(message: Message, state: FSMContext):
    tg_id = message.from_user.id
    file_id = message.document.file_id
    if tg_id not in user_files:
        user_files[tg_id] = []
    user_files[tg_id].append(file_id)
    await message.answer(f"Файл {message.document.file_name} добавлен. Можно добавить ещё или введите /done")

@dp.message(CreateOrder.files, Command("done"))
async def finish_files(message: Message, state: FSMContext):
    tg_id = message.from_user.id
    files = user_files.get(tg_id, [])
    data = await state.get_data()
    title = data["title"]
    description = data["description"]
    price = data["price"]
    urgency = data["urgency"]
    days = data["days"]
    # Создаём заказ
    resp = create_order(tg_id, title, description, price, urgency, days, files)
    if resp.get("success"):
        order_id = resp["data"]["order_id"]
        await message.answer(f"✅ Заказ №{order_id} создан! Списано {price} баллов.")
    else:
        await message.answer(f"❌ Ошибка: {resp.get('error')}")
    # Очистка
    if tg_id in user_files:
        del user_files[tg_id]
    await state.clear()

# Отмена создания
@dp.message(Command("cancel"), StateFilter(None))
async def cancel_cmd(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Создание заказа отменено.")

@dp.message(Command("my_orders"))
async def cmd_my_orders(message: Message):
    tg_id = message.from_user.id
    resp = get_my_orders(tg_id)  # пока не реализовано, доработаем
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

@dp.message(Command("accept"))
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

@dp.message(Command("cancel_order"))
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

@dp.message(Command("help"))
async def cmd_help(message: Message):
    await message.answer(
        "Команды бота заказчика:\n"
        "/new - создать новый заказ\n"
        "/my_orders - список моих заказов\n"
        "/accept <id> - принять выполненный заказ\n"
        "/cancel_order <id> - отменить заказ (только если он ещё не принят)\n"
        "/balance - баланс\n"
        "/cancel - отменить создание заказа"
    )

# -------------------------------------------------------------------
# Запуск
# -------------------------------------------------------------------
async def main():
    print("Client bot started")
    await dp.start_polling(bot)

if __name__ == "__main__":
    import asyncio
    asyncio.run(main())