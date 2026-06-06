import os
import requests
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("EXECUTOR_BOT_TOKEN")
CORE_URL = os.getenv("CORE_URL", "http://localhost:5000")

bot = Bot(token=TOKEN)
dp = Dispatcher()

# -------------------------------------------------------------------
# Вспомогательные функции
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
        return {"success": False, "error": str(e)}

def register_user(tg_id, username):
    return api_request("GET", "/user/get_or_create", params={"telegram_id": tg_id, "username": username})

def get_balance(tg_id):
    return api_request("GET", "/user/balance", params={"telegram_id": tg_id})

def get_orders(filters):
    # filters: status, urgency, price_min, price_max, limit, offset
    return api_request("GET", "/order/list", params=filters)

def take_order(order_id, executor_id):
    return api_request("POST", "/order/take", data={"order_id": order_id, "executor_id": executor_id})

def submit_order(order_id, executor_id, result_files):
    return api_request("POST", "/order/submit", data={"order_id": order_id, "executor_id": executor_id, "result_files": result_files})

def get_order_details(order_id):
    return api_request("GET", "/order/get", params={"id": order_id})

# -------------------------------------------------------------------
# Состояния для отправки результата
# -------------------------------------------------------------------
class SubmitOrder(StatesGroup):
    waiting_for_files = State()

user_temp = {}  # временное хранилище order_id для submit

@dp.message(Command("start"))
async def cmd_start(message: Message):
    tg_id = message.from_user.id
    username = message.from_user.username or ""
    register_user(tg_id, username)
    await message.answer(
        "👷 Добро пожаловать в биржу CAD (исполнитель)!\n"
        "Команды:\n"
        "/feed - показать открытые заказы (с фильтрами)\n"
        "/take <id> - взять заказ\n"
        "/my - мои текущие заказы\n"
        "/submit <id> - сдать выполненный заказ (после команды приложите файлы)\n"
        "/balance - баланс\n"
        "/help - помощь"
    )

@dp.message(Command("balance"))
async def cmd_balance(message: Message):
    data = get_balance(message.from_user.id)
    if data.get("success"):
        bal = data["data"]["balance"]
        rating = data["data"]["rating"]
        await message.answer(f"💰 Ваш баланс: {bal} баллов\n⭐ Рейтинг: {rating}")
    else:
        await message.answer("❌ Ошибка получения баланса")

@dp.message(Command("feed"))
async def cmd_feed(message: Message):
    # Простой вариант: показываем первые 10 открытых заказов с кнопкой "Взять"
    resp = get_orders({"status": "open", "limit": 10})
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

@dp.callback_query(lambda c: c.data.startswith("take_"))
async def callback_take(callback: CallbackQuery):
    order_id = int(callback.data.split("_")[1])
    executor_id = callback.from_user.id
    resp = take_order(order_id, executor_id)
    if resp.get("success"):
        await callback.message.answer(f"✅ Вы взяли заказ #{order_id} в работу.")
        # Обновим сообщение, убрав кнопку
        await callback.message.edit_reply_markup(reply_markup=None)
    else:
        await callback.message.answer(f"❌ Не удалось взять: {resp.get('error')}")
    await callback.answer()

@dp.message(Command("take"))
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
    resp = take_order(order_id, message.from_user.id)
    if resp.get("success"):
        await message.answer(f"✅ Вы взяли заказ #{order_id}.")
    else:
        await message.answer(f"❌ Ошибка: {resp.get('error')}")

@dp.message(Command("my"))
async def cmd_my_orders(message: Message):
    # Получаем заказы, где исполнитель = текущий пользователь
    # Сделаем запрос с фильтром executor_id (добавим в Core)
    # Пока временно: получим все заказы и отфильтруем, но для MVP можно расширить Core.
    # Для чистоты предложу добавить в Core параметр executor_id в /order/list.
    # Напишем с предположением, что такой параметр есть.
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

@dp.message(Command("submit"))
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
    # Проверим, что заказ belongs to исполнителю и статус in_progress
    order = get_order_details(order_id)
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

@dp.message(SubmitOrder.waiting_for_files, F.document)
async def submit_files(message: Message, state: FSMContext):
    tg_id = message.from_user.id
    if tg_id not in user_temp:
        await message.answer("Начните с /submit")
        return
    # Сохраняем file_id временно в памяти (можно в словарь с list)
    if 'result_files' not in user_temp:
        user_temp.setdefault('result_files', {})
    if tg_id not in user_temp['result_files']:
        user_temp['result_files'][tg_id] = []
    user_temp['result_files'][tg_id].append(message.document.file_id)
    await message.answer(f"Файл {message.document.file_name} добавлен. Ещё или /done_files")

@dp.message(SubmitOrder.waiting_for_files, Command("done_files"))
async def finish_submit(message: Message, state: FSMContext):
    tg_id = message.from_user.id
    order_id = user_temp.get(tg_id)
    files = user_temp.get('result_files', {}).get(tg_id, [])
    if not order_id:
        await message.answer("Ошибка: не найден заказ. Повторите /submit")
        return
    resp = submit_order(order_id, tg_id, files)
    if resp.get("success"):
        await message.answer(f"✅ Решение по заказу #{order_id} отправлено заказчику.")
    else:
        await message.answer(f"❌ Ошибка: {resp.get('error')}")
    # Очистка
    if tg_id in user_temp:
        del user_temp[tg_id]
    if tg_id in user_temp.get('result_files', {}):
        del user_temp['result_files'][tg_id]
    await state.clear()

@dp.message(Command("help"))
async def cmd_help(message: Message):
    await message.answer(
        "Команды исполнителя:\n"
        "/feed - список открытых заказов\n"
        "/take <id> - взять заказ\n"
        "/my - мои текущие заказы\n"
        "/submit <id> - сдать решение (после команды присылайте файлы и /done_files)\n"
        "/balance - баланс\n"
        "/cancel - отменить текущую операцию"
    )

@dp.message(Command("cancel"))
async def cmd_cancel_state(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Отменено.")

async def main():
    print("Executor bot started")
    await dp.start_polling(bot)

if __name__ == "__main__":
    import asyncio
    asyncio.run(main())