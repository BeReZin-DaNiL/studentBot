import asyncio
import logging
import json
import os
from datetime import datetime
import re
import time
import sqlite3

from aiogram import Bot, Dispatcher, Router, F, types
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    Message,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    CallbackQuery,
    ReplyKeyboardRemove,
    ReplyKeyboardMarkup,
    KeyboardButton
)
from dotenv import load_dotenv
import gspread
from google.oauth2.service_account import Credentials
from shared import get_all_orders, ADMIN_ID, bot, STATUS_EMOJI_MAP, pluralize_days, get_full_name, get_deadline_keyboard, admin_view_order_handler
from payment import payment_router
from executor_menu import executor_menu_router, is_executor, get_executor_menu_keyboard
from executor_menu import ExecutorStates
from admin_self_take import admin_self_take_router
from admin_self_take import admin_view_order_handler

# --- FastAPI интеграция ---
from fastapi import FastAPI, Request
import uvicorn

app = FastAPI()

@app.get("/")
async def root():
    return {"status": "API is running"}

def init_db():
    try:
        with sqlite3.connect('student.db', timeout=10.0) as conn:
            c = conn.cursor()
            c.execute('''CREATE TABLE IF NOT EXISTS students
                         (user_id INTEGER PRIMARY KEY,
                          first_name TEXT,
                          last_name TEXT,
                          phone_number TEXT,
                          group_name TEXT)''')
            conn.commit()
    except sqlite3.Error as e:
        logging.error(f"Ошибка при инициализации базы данных SQLite: {e}")

# Глобальная карта статусов для консистентности
STATUS_EMOJI_MAP = {
    "Редактируется": "📝",
    "Рассматривается": "🆕",
    "Ожидает подтверждения": "🤔",
    "Ожидает подтверждения от исполнителя": "🙋‍♂️",
    "Ожидает оплаты": "💳",
    "Принята": "✅",
    "В работе": "⏳",
    "Выполнена": "🎉",
    "Отменена": "❌",
}

# Загрузка переменных окружения
load_dotenv()

# Используем токен и ID из .env файла, но если их нет, используем "зашитые"
BOT_TOKEN = os.getenv("BOT_TOKEN", "7763016986:AAFW4Rwh012_bfh8Jt0E_zaq5abvzenr4bE")
# Добавляю EXECUTOR_IDS
EXECUTOR_IDS = [int(x) for x in os.getenv("EXECUTOR_IDS", "123456789").split(",") if x.strip().isdigit()]

ALLOWED_EXTENSIONS = {"pdf", "docx", "png", "jpeg", "jpg"}
MAX_FILE_SIZE = 15 * 1024 * 1024  # 15 MB

# Настройка логирования
logging.basicConfig(level=logging.INFO)

# Инициализация бота и диспетчера
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
router = Router()
dp.include_router(router)
admin_router = Router()
dp.include_router(admin_router)
executor_router = Router()
dp.include_router(executor_router)
dp.include_router(payment_router)
dp.include_router(executor_menu_router)
dp.include_router(admin_self_take_router)

# Google Sheets
GOOGLE_SHEET_ID = "1D15yyPKHyN1Vw8eRnjT79xV28cwL_q5EIZa97tgTF2U"
GOOGLE_SHEET_HEADERS = [
    "Группа", "Университет", "Тип работы", "Методичка", "Задание", "Пример работы", "Дата сдачи", "Комментарий"
]

# --- FSM для админа ---
class AssignExecutor(StatesGroup):
    waiting_for_id = State()

class AdminApproval(StatesGroup):
    waiting_for_new_price = State()

# --- FSM для исполнителя ---
class ExecutorResponse(StatesGroup):
    waiting_for_price = State()
    waiting_for_deadline = State()
    waiting_for_comment = State()
    waiting_for_confirm = State()  # Новый этап

# --- Состояния (FSM) ---
class OrderState(StatesGroup):
    group_name = State()
    university_name = State()
    teacher_name = State()  # Новое состояние
    gradebook = State()     # Новое состояние
    subject = State()       # Новое состояние
    subject_other = State() 
    work_type = State()
    work_type_other = State()
    guidelines_choice = State()
    guidelines_upload = State()
    task_upload = State()
    example_choice = State()
    example_upload = State()
    deadline = State()
    comments = State()
    confirmation = State()

class AdminContact(StatesGroup):
    waiting_for_message = State()

class ClientRevision(StatesGroup):
    waiting_for_revision_comment = State()

class AdminRevision(StatesGroup):
    waiting_for_revision_comment = State()

class AdminBroadcastClients(StatesGroup):
    waiting_for_message = State()
# --- Клавиатуры ---
# --- FSM для настроек исполнителей ---
class AdminSettings(StatesGroup):
    waiting_for_executor_name = State()
    waiting_for_executor_id = State()
    waiting_for_delete_id = State()

    # --- Новый этап FSM для подтверждения ---
class ExecutorResponse(StatesGroup):
    waiting_for_price = State()
    waiting_for_deadline = State()
    waiting_for_comment = State()
    waiting_for_confirm = State()  # Новый этап

EXECUTORS_FILE = "executors.json"


def get_phone_request_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📱 Поделиться номером", request_contact=True)]
        ],
        resize_keyboard=True,
        one_time_keyboard=True
    )
def get_admin_settings_keyboard():
    buttons = [
        [InlineKeyboardButton(text="➕ Добавить исполнителя", callback_data="admin_add_executor")],
        [InlineKeyboardButton(text="➖ Удалить исполнителя", callback_data="admin_delete_executor")],
        [InlineKeyboardButton(text="👥 Показать всех исполнителей", callback_data="admin_show_executors")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="admin_back_to_menu")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_skip_keyboard_admin():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➡️ Пропустить", callback_data="admin_skip_executor_name")]
    ])

def get_executors_list():
    if not os.path.exists(EXECUTORS_FILE):
        return []
    with open(EXECUTORS_FILE, "r", encoding="utf-8") as f:
        try:
            return json.load(f)
        except Exception:
            return []

def save_executors_list(executors):
    with open(EXECUTORS_FILE, "w", encoding="utf-8") as f:
        json.dump(executors, f, ensure_ascii=False, indent=4)

def get_executors_info_keyboard():
    executors = get_executors_list()
    if not executors:
        return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Нет исполнителей", callback_data="none")]])
    buttons = []
    for ex in executors:
        label = f"{ex.get('name') or 'Без ФИО'} | ID: {ex['id']}"
        buttons.append([InlineKeyboardButton(text=label, callback_data="none")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_executors_delete_keyboard():
    executors = get_executors_list()
    if not executors:
        return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Нет исполнителей", callback_data="none")]])
    buttons = []
    for ex in executors:
        label = f"{ex.get('name') or 'Без ФИО'} | ID: {ex['id']}"
        buttons.append([InlineKeyboardButton(text=label, callback_data=f"admin_delete_executor_id_{ex['id']}")])
    buttons.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="admin_settings")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def save_user_phone(user_id, phone_number):
    users_file = "users.json"
    lock_file = users_file + ".lock"
    while os.path.exists(lock_file):
        time.sleep(0.1)
    try:
        open(lock_file, 'w').close()
        users = {}
        if os.path.exists(users_file):
            with open(users_file, "r", encoding="utf-8") as f:
                try:
                    users = json.load(f)
                except json.JSONDecodeError:
                    users = {}
        users[str(user_id)] = {"phone_number": phone_number}
        with open(users_file, "w", encoding="utf-8") as f:
            json.dump(users, f, ensure_ascii=False, indent=4)
    finally:
        if os.path.exists(lock_file):
            os.remove(lock_file)

def get_user_profile(user_id):
    """
    Возвращает профиль пользователя (ФИО, группа, зачетка, университет) из users.json.
    """
    users_file = "users.json"
    if not os.path.exists(users_file):
        return {}
    with open(users_file, "r", encoding="utf-8") as f:
        try:
            users = json.load(f)
        except Exception:
            return {}
    return users.get(str(user_id), {})

def save_user_profile(user_id, profile_data):
    """
    Сохраняет профиль пользователя (ФИО, группа, зачетка, университет) в users.json.
    """
    users_file = "users.json"
    lock_file = users_file + ".lock"
    while os.path.exists(lock_file):
        time.sleep(0.1)
    try:
        open(lock_file, 'w').close()
        users = {}
        if os.path.exists(users_file):
            with open(users_file, "r", encoding="utf-8") as f:
                try:
                    users = json.load(f)
                except json.JSONDecodeError:
                    users = {}
        user_entry = users.get(str(user_id), {})
        user_entry.update(profile_data)
        users[str(user_id)] = user_entry
        with open(users_file, "w", encoding="utf-8") as f:
            json.dump(users, f, ensure_ascii=False, indent=4)
    finally:
        if os.path.exists(lock_file):
            os.remove(lock_file)

def get_executors_assign_keyboard(order_id):
    executors = get_executors_list()
    buttons = []
    if executors:
        for ex in executors:
            label = f"{ex.get('name') or 'Без ФИО'} | ID: {ex['id']}"
            buttons.append([InlineKeyboardButton(text=label, callback_data=f"assign_executor_select_{order_id}_{ex['id']}")])
        buttons.append([InlineKeyboardButton(text="Ввести ID вручную", callback_data=f"assign_executor_manual_{order_id}")])
    buttons.append([InlineKeyboardButton(text="⬅️ Назад", callback_data=f"admin_view_order_{order_id}")])
    return InlineKeyboardMarkup(inline_keyboard=buttons) if buttons else None

@admin_router.message(F.text == "⚙️ Настройки")
async def admin_settings_menu(message: Message, state: FSMContext):
    if message.from_user.id != int(ADMIN_ID): return
    await state.clear()
    await message.answer("⚙️ Настройки исполнителей:", reply_markup=get_admin_settings_keyboard())
    
@admin_router.message(F.text == "📢 Рассылка")
async def admin_broadcast_menu(message: Message, state: FSMContext):
    if message.from_user.id != int(ADMIN_ID): return
    await state.clear()
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👤 Рассылка исполнителям", callback_data="broadcast_executors")],
        [InlineKeyboardButton(text="👨‍💼 Рассылка клиентам", callback_data="broadcast_clients")]
    ])
    await message.answer("📩 Выберите тип рассылки:", reply_markup=keyboard)

@admin_router.callback_query(F.data == "broadcast_executors")
async def broadcast_executors(callback: CallbackQuery, state: FSMContext):
    orders = get_all_orders()
    review_orders = [o for o in orders if o.get('status') == "Рассматривается"]
    if not review_orders:
        await callback.message.edit_text("Нет заявок в статусе 'Рассматривается' для рассылки.")
        return
    keyboard_buttons = []
    for order in review_orders:
        order_id = order['order_id']
        subject = order.get('subject', 'Без темы')
        work_type = order.get('work_type', 'Заявка').replace('work_type_', '')
        button_text = f"Заявка №{order_id} {work_type} | {subject}"
        keyboard_buttons.append([InlineKeyboardButton(text=button_text, callback_data=f"admin_broadcast_select_{order_id}")])
    keyboard = InlineKeyboardMarkup(inline_keyboard=keyboard_buttons)
    await callback.message.edit_text("Выберите заявку для рассылки исполнителям:", reply_markup=keyboard)
    await callback.answer()

@admin_router.callback_query(F.data == "broadcast_clients")
async def broadcast_clients(callback: CallbackQuery, state: FSMContext):
    orders = get_all_orders()
    unique_groups = set(o['group_name'] for o in orders if 'group_name' in o)
    if not unique_groups:
        await callback.message.edit_text("Нет групп для рассылки.")
        return
    keyboard_buttons = [[InlineKeyboardButton(text=group, callback_data=f"broadcast_group_{group}")] for group in unique_groups]
    keyboard = InlineKeyboardMarkup(inline_keyboard=keyboard_buttons)
    await callback.message.edit_text("Выберите группу для рассылки клиентам:", reply_markup=keyboard)
    await callback.answer()

@admin_router.callback_query(F.data.startswith("broadcast_group_"))
async def broadcast_group_selected(callback: CallbackQuery, state: FSMContext):
    group = callback.data.split("_", 2)[-1]
    await state.update_data(selected_group=group)
    await state.set_state(AdminBroadcastClients.waiting_for_message)
    await callback.message.edit_text("💬 Введите сообщение для рассылки клиентам в группе:")
    await callback.answer()

@admin_router.message(AdminBroadcastClients.waiting_for_message)
async def broadcast_message_input(message: Message, state: FSMContext):
    message_text = message.text
    data = await state.get_data()
    group = data.get('selected_group')
    orders = get_all_orders()
    users_to_send = set(o['user_id'] for o in orders if o.get('group_name') == group)
    for user_id in users_to_send:
        try:
            await bot.send_message(user_id, message_text)
        except:
            pass
    await message.answer("✅ Рассылка успешно отправлена.")
    await state.clear()

@admin_router.callback_query(F.data == "admin_settings")
async def admin_settings_menu_cb(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text("⚙️ Настройки исполнителей:", reply_markup=get_admin_settings_keyboard())
    await callback.answer()

@admin_router.callback_query(F.data == "admin_add_executor")
async def admin_add_executor_start(callback: CallbackQuery, state: FSMContext):
    await state.set_state(AdminSettings.waiting_for_executor_name)
    await callback.message.edit_text("✍️ Введите ФИО исполнителя (или пропустите):", reply_markup=get_skip_keyboard_admin())
    await callback.answer()

@admin_router.callback_query(F.data == "admin_skip_executor_name", AdminSettings.waiting_for_executor_name)
async def admin_skip_executor_name(callback: CallbackQuery, state: FSMContext):
    await state.update_data(executor_name="")
    await state.set_state(AdminSettings.waiting_for_executor_id)
    await callback.message.edit_text("🔢 Введите ID исполнителя (обязательно):")
    await callback.answer()

@admin_router.message(AdminSettings.waiting_for_executor_name)
async def admin_executor_name_input(message: Message, state: FSMContext):
    await state.update_data(executor_name=message.text)
    await state.set_state(AdminSettings.waiting_for_executor_id)
    await message.answer("🔢 Введите ID исполнителя (обязательно):")

@admin_router.message(AdminSettings.waiting_for_executor_id)
async def admin_executor_id_input(message: Message, state: FSMContext):
    if not message.text.isdigit():
        await message.answer("ID должен быть числом. Попробуйте еще раз.")
        return
    executor_id = int(message.text)
    data = await state.get_data()
    name = data.get("executor_name", "")
    executors = get_executors_list()
    if any(ex['id'] == executor_id for ex in executors):
        await message.answer("Такой исполнитель уже есть.")
        return
    executors.append({"id": executor_id, "name": name})
    save_executors_list(executors)
    await state.clear()
    await message.answer("✅ Исполнитель добавлен!", reply_markup=get_admin_settings_keyboard())

@admin_router.callback_query(F.data == "admin_delete_executor")
async def admin_delete_executor_start(callback: CallbackQuery, state: FSMContext):
    await state.set_state(AdminSettings.waiting_for_delete_id)
    await callback.message.edit_text("Выберите исполнителя для удаления:", reply_markup=get_executors_delete_keyboard())
    await callback.answer()

@admin_router.callback_query(F.data.startswith("admin_delete_executor_id_"), AdminSettings.waiting_for_delete_id)
async def admin_delete_executor_confirm(callback: CallbackQuery, state: FSMContext):
    executor_id = int(callback.data.split("_")[-1])
    executors = get_executors_list()
    executors = [ex for ex in executors if ex['id'] != executor_id]
    save_executors_list(executors)
    await state.clear()
    await callback.message.edit_text("✅ Исполнитель удален!", reply_markup=get_admin_settings_keyboard())
    await callback.answer()

@admin_router.callback_query(F.data == "admin_show_executors")
async def admin_show_executors(callback: CallbackQuery, state: FSMContext):
    executors = get_executors_list()
    if not executors:
        text = "Нет исполнителей."
    else:
        text = "👥 Текущие исполнители:\n\n" + "\n".join([
            f"{ex.get('name') or 'Без ФИО'} | ID: {ex['id']}" for ex in executors
        ])
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="admin_settings")]
    ])
    await callback.message.edit_text(text, reply_markup=keyboard)
    await callback.answer()
@executor_router.callback_query(F.data == "executor_back_to_price", ExecutorResponse.waiting_for_deadline)
async def executor_back_to_price_handler(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    order_id = data.get('order_id')
    await state.set_state(ExecutorResponse.waiting_for_price)
    await callback.message.edit_text("Отлично! Укажите вашу цену(или введите вручную):", reply_markup=get_price_keyboard(order_id))
    await callback.answer()

@admin_router.callback_query(F.data == "admin_back_to_menu")
async def admin_back_to_menu(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text("Добро пожаловать в панель администратора!", reply_markup=None)
    await bot.send_message(callback.from_user.id, "Главное меню:", reply_markup=get_admin_keyboard())
    await callback.answer()

@router.callback_query(OrderState.work_type, F.data == "back_to_subject")
async def back_to_subject_handler(callback: CallbackQuery, state: FSMContext):
    await state.set_state(OrderState.subject)
    await callback.message.edit_text(
        
    )
    await callback.answer()
def get_admin_keyboard():
    buttons = [
        [KeyboardButton(text="📦 Все заказы")],
        [KeyboardButton(text="⚙️ Настройки")],
        [KeyboardButton(text="📢 Рассылка")],  # Новая кнопка
    ]
    return ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True)

def get_executor_confirm_keyboard(order_id):
    buttons = [
        [
            InlineKeyboardButton(text="✅ Готов взяться", callback_data=f"executor_accept_{order_id}"),
            InlineKeyboardButton(text="❌ Отказаться", callback_data=f"executor_refuse_{order_id}")
        ],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data=f"executor_back_to_materials:{order_id}")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)
def get_executor_final_confirm_keyboard(order_id):
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Отправить", callback_data=f"executor_send_offer:{order_id}"),
            InlineKeyboardButton(text="❌ Отказаться", callback_data=f"executor_refuse_{order_id}")
        ]
    ])
def get_price_keyboard(order_id):
    buttons = [
        [InlineKeyboardButton(text=f"{i} ₽", callback_data=f"price_{i}") for i in range(500, 2501, 500)],
        [InlineKeyboardButton(text=f"{i} ₽", callback_data=f"price_{i}") for i in range(3000, 5001, 1000)],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data=f"executor_back_to_invite:{order_id}")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_deadline_keyboard():
    buttons = [
        [
            InlineKeyboardButton(text="1 день", callback_data="deadline_1 день"),
            InlineKeyboardButton(text="3 дня", callback_data="deadline_3 дня"),
            InlineKeyboardButton(text="До дедлайна", callback_data="deadline_До дедлайна"),
        ],
        [InlineKeyboardButton(text="💬 Ввести свой вариант", callback_data="deadline_manual")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="executor_back_to_price")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_main_reply_keyboard():
    buttons = [
        [KeyboardButton(text="🆕 Новая заявка"), KeyboardButton(text="📂 Мои заявки")],
        [KeyboardButton(text="❓ Помощь"), KeyboardButton(text="👨‍💻 Связаться с администратором")]
    ]
    return ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True)

def get_back_to_main_menu_keyboard():
    buttons = [[InlineKeyboardButton(text="⬅️ Назад", callback_data="back_to_main_menu")]]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_back_keyboard():
    buttons = [[InlineKeyboardButton(text="⬅️ Назад", callback_data="back")]]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_gradebook_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➡️ Пропустить", callback_data="skip_gradebook")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="back")]
    ])

def get_profile_confirm_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✏️ Изменить", callback_data="edit_profile")],
        [InlineKeyboardButton(text="➡️ Далее", callback_data="profile_next")]
    ])
def get_yes_no_keyboard(prefix: str):
    """Возвращает клавиатуру с кнопками 'Да' и 'Нет'."""
    buttons = [
        [
            InlineKeyboardButton(text="✅ Да", callback_data=f"{prefix}_yes"),
            InlineKeyboardButton(text="➡️ Пропустить", callback_data=f"{prefix}_no")
        ],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="back")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)
def get_user_order_keyboard(order_id, status):
    buttons = []
    # Кнопка 'Оплатить' если статус 'Ожидает оплаты'
    if status == "Ожидает оплаты":
        buttons.append([InlineKeyboardButton(text="💳 Оплатить", callback_data=f"pay_{order_id}")])
    # Кнопка 'Отказаться' только если статус не 'Выполнена'
    if status != "Выполнена":
        buttons.append([InlineKeyboardButton(text="❌ Отказаться", callback_data=f"user_cancel_order:{order_id}")])
    # Кнопка 'К списку заявок'
    buttons.append([InlineKeyboardButton(text="⬅️ К списку заявок", callback_data="my_orders_list")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_work_type_keyboard():
    buttons = [
        [InlineKeyboardButton(text="Контрольная", callback_data="work_type_Контрольная")],
        [InlineKeyboardButton(text="Расчётно-графическая", callback_data="work_type_Расчётно-графическая")],
        [InlineKeyboardButton(text="Курсовая", callback_data="work_type_Курсовая")],
        [InlineKeyboardButton(text="Тест", callback_data="work_type_Тест")],
        [InlineKeyboardButton(text="Отчёт", callback_data="work_type_Отчёт")],
        [InlineKeyboardButton(text="Диплом", callback_data="work_type_Диплом")],
        [InlineKeyboardButton(text="Другое (ввести вручную)", callback_data="work_type_other")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="back_to_subject")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_skip_keyboard(prefix: str):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔽 Пропустить", callback_data=f"skip_{prefix}")]
    ])
    
def get_confirmation_keyboard():
    buttons = [
        [InlineKeyboardButton(text="✅ Подтвердить", callback_data="confirm_order")],
        [InlineKeyboardButton(text="❌ Отменить", callback_data="cancel_order")],

        # Кнопка '⬅️ Назад' убрана на этапе подтверждения
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_admin_final_approval_keyboard(order_id, price, executor_id=None, show_materials_button=True):
    approve_cb = f"final_approve_{order_id}_{price}"
    if executor_id is not None:
        approve_cb += f"_{executor_id}"
    buttons = [
        [InlineKeyboardButton(text=f"✅ Утвердить и отправить ({price} ₽)", callback_data=approve_cb)],
        [InlineKeyboardButton(text="✏️ Изменить цену", callback_data=f"final_change_price_{order_id}")],
    ]
    if show_materials_button:
        buttons.append([InlineKeyboardButton(text="📎 Посмотреть материалы заказа", callback_data=f"admin_show_materials:{order_id}")])
    reject_cb = f"final_reject_{order_id}"
    if executor_id is not None:
        reject_cb += f"_{executor_id}"
    buttons.append([InlineKeyboardButton(text="❌ Отклонить предложение", callback_data=reject_cb)])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_client_work_approval_keyboard(order_id):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Принять работу", callback_data=f"client_accept_work:{order_id}")],
        [InlineKeyboardButton(text="✍️ Отправить на доработку", callback_data=f"client_request_revision:{order_id}")]
    ])
def get_skip_comment_keyboard():
    buttons = [
        [InlineKeyboardButton(text="➡️ Пропустить", callback_data="skip_comment")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_admin_order_keyboard(order, show_materials_button=True):
    status = order.get('status')
    executor_is_admin = str(order.get('executor_id')) == str(ADMIN_ID)
    # Для статусов 'В работе' и 'На доработке' всегда показываем 'Посмотреть материалы заказа' и (если исполнитель — админ) 'Сдать работу'
    if status in ["В работе", "На доработке"]:
        buttons = []
        has_files = order.get('guidelines_file') or order.get('task_file') or order.get('task_text') or order.get('example_file')
        
        if executor_is_admin and status == "В работе":
            buttons.append([InlineKeyboardButton(text="✅ Сдать работу", callback_data=f"admin_admin_submit_work_{order['order_id']}")])
        if has_files:
            buttons.append([InlineKeyboardButton(text="📎 Посмотреть материалы заказа", callback_data=f"admin_show_materials:{order['order_id']}")])
        buttons.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="admin_back")])
        return InlineKeyboardMarkup(inline_keyboard=buttons)
    # --- Стандартная логика для остальных случаев ---
    buttons = []
    if 'order_id' not in order:
        buttons.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="admin_back")])
        return InlineKeyboardMarkup(inline_keyboard=buttons)
    if status == "Рассматривается" and not executor_is_admin:
        buttons.append([
            InlineKeyboardButton(text="👤 Выбрать исполнителя", callback_data=f"assign_executor_start_{order['order_id']}")
        ])
        if status in ["Рассматривается", "Ожидает подтверждения"]:
            buttons.append([
                InlineKeyboardButton(text="❇️ Взять заказ", callback_data=f"admin_self_take_{order['order_id']}")
        ])
    if status == "Выполнена":
        buttons.append([InlineKeyboardButton(text="📊 Сохранить в таблицу", callback_data=f"admin_save_to_gsheet:{order['order_id']}")])
    # --- ДОБАВЛЕНО: для статуса 'В работе' показываем кнопку 'Посмотреть материалы', если есть файлы ---
    if status == "В работе":
        has_files = order.get('guidelines_file') or order.get('task_file') or order.get('task_text') or order.get('example_file')
        if has_files:
            buttons.append([InlineKeyboardButton(text="📎 Посмотреть материалы заказа", callback_data=f"admin_show_materials:{order['order_id']}")])
    if status != "Выполнена":
        has_files = order.get('guidelines_file') or order.get('task_file') or order.get('task_text') or order.get('example_file')
        if show_materials_button and has_files and status != "В работе":
            buttons.append([InlineKeyboardButton(text="📎 Посмотреть материалы заказа", callback_data=f"admin_show_materials:{order['order_id']}")])
            buttons.append([InlineKeyboardButton(text="❌ Отказаться от заявки", callback_data=f"admin_delete_order:{order['order_id']}")])
        if not show_materials_button:
            buttons.append([InlineKeyboardButton(text="⬅️ Скрыть материалы", callback_data=f"admin_hide_materials:{order['order_id']}")])
        # Добавляем кнопку "Сдать работу" для статуса "В работе" только если исполнитель — админ
        if status == "В работе" and executor_is_admin:
            buttons.append([InlineKeyboardButton(text="✅ Сдать работу", callback_data=f"admin_admin_submit_work_{order['order_id']}")])
    buttons.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="admin_back")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


# Хендлер для кнопки 'Сдать работу' от админа-исполнителя
@admin_router.callback_query(F.data.startswith("admin_admin_submit_work_"))
async def admin_admin_submit_work_handler(callback: CallbackQuery, state: FSMContext):
    parts = callback.data.split("_")
    order_id = int(parts[4])  # Исправлено: order_id находится в parts[4], а не parts[2]
    executor_id = int(parts[5]) if len(parts) > 5 else None
    await state.update_data(submit_order_id=order_id)
    await state.set_state("admin_waiting_for_work_file")
    await callback.message.edit_text(
        "Пожалуйста, прикрепите файл с выполненной работой (zip, docx, pdf и др.)",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⬅️ Вернуться к заявке", callback_data=f"admin_view_order_{order_id}")]
        ])
    )
    await callback.answer()
def get_user_cancel_confirm_keyboard(order_id):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Да", callback_data=f"user_cancel_confirm:{order_id}"),
         InlineKeyboardButton(text="❌ Нет", callback_data=f"user_cancel_abort:{order_id}")]
    ])

@router.callback_query(F.data.startswith("user_cancel_order:"))
async def user_cancel_order_confirm(callback: CallbackQuery, state: FSMContext):
    order_id = int(callback.data.split(":")[-1])
    await callback.message.edit_text(
        "Вы уверены, что хотите отказаться от этой заявки?",
        reply_markup=get_user_cancel_confirm_keyboard(order_id)
    )
    await callback.answer()

def delete_order_from_gsheet(order_id):
    creds = Credentials.from_service_account_file("google-credentials.json", scopes=["https://www.googleapis.com/auth/spreadsheets"])
    gc = gspread.authorize(creds)
    sh = gc.open_by_key(GOOGLE_SHEET_ID)
    worksheet = sh.sheet1
    # Найти строку с order_id (предположим, что order_id в первом столбце)
    cell = worksheet.find(str(order_id))
    if cell:
        worksheet.delete_rows(cell.row)
@router.callback_query(F.data.startswith("user_cancel_confirm:"))
async def user_cancel_order_yes(callback: CallbackQuery, state: FSMContext):
    order_id = int(callback.data.split(":")[-1])
    user_id = callback.from_user.id
    file_path = "orders.json"
    orders = []
    subject = None
    status = None

    if os.path.exists(file_path) and os.path.getsize(file_path) > 0:
        with open(file_path, "r", encoding="utf-8") as f:
            try:
                orders = json.load(f)
            except json.JSONDecodeError:
                orders = []

    # Найти заявку для subject и status
    for o in orders:
        if str(o.get("order_id")) == str(order_id) and o.get("user_id") == user_id:
            subject = o.get("subject", "Не указан")
            status = o.get("status")
            break

    # Удаляем заявку из orders.json
    new_orders = [o for o in orders if not (str(o.get("order_id")) == str(order_id) and o.get("user_id") == user_id)]
    with open(file_path, "w", encoding="utf-8") as f:
        json.dump(new_orders, f, ensure_ascii=False, indent=4)

    # Если заявка была в статусе "В работе", пробуем удалить из Google Sheets
    if status == "В работе":
        try:
            delete_order_from_gsheet(order_id)
            print("Заявка удалена из гугл таблицы")
        except Exception as e:
            print(f"Ошибка при удалении из Google Sheets: {e}")

    await state.clear()
    await callback.message.edit_text("❌ Заявка отменена и удалена.")
    # Уведомление админу
    if subject is not None:
        await bot.send_message(ADMIN_ID, f"❌ Заказчик отказался от заявки по предмету ({subject})")
    await callback.answer()

@router.callback_query(F.data.startswith("user_cancel_abort:"))
async def user_cancel_order_no(callback: CallbackQuery, state: FSMContext):
    order_id = int(callback.data.split(":")[-1])
    user_id = callback.from_user.id
    orders = get_user_orders(user_id)
    target_order = next((order for order in orders if order['order_id'] == order_id), None)
    if not target_order:
        await callback.message.edit_text("Не удалось найти эту заявку или у вас нет к ней доступа.")
        await callback.answer()
        return
    status = target_order.get('status', 'Не определен')
    status_text = f"{STATUS_EMOJI_MAP.get(status, '📄')} {status}"
    details_text = f"""
<b>Детали заявки №{target_order['order_id']}</b>\n\n<b>Статус:</b> {status_text}\n\n<b>Группа:</b> {target_order.get('group_name', 'Не указано')}\n<b>Университет:</b> {target_order.get('university_name', 'Не указано')}\n<b>Преподаватель:</b> {target_order.get('teacher_name', 'Не указано')}\n<b>Номер зачетки:</b> {target_order.get('gradebook', 'Не указано')}\n<b>Предмет:</b> {target_order.get('subject', 'Не указан')}\n<b>Тип работы:</b> {target_order.get('work_type', 'Не указан')}\n<b>Методичка:</b> {'✅ Да' if target_order.get('has_guidelines') else '❌ Нет'}\n<b>Задание:</b> {'✅ Прикреплено' if target_order.get('task_file') or target_order.get('task_text') else '❌ Нет'}\n<b>Пример работы:</b> {'✅ Да' if target_order.get('has_example') else '❌ Нет'}\n<b>Дата сдачи:</b> {target_order.get('deadline', 'Не указана')}\n<b>Комментарий:</b> {target_order.get('comments', 'Нет')}\n"""
    if status == "На доработке" and target_order.get('revision_comment'):
        details_text += f"\n<b>Доработка:</b> {target_order.get('revision_comment')}"
    keyboard = get_user_order_keyboard(order_id, status)
    await callback.message.edit_text(details_text, parse_mode="HTML", reply_markup=keyboard)
    await callback.answer()
# Хендлер для загрузки файла работы от админа-исполнителя
@admin_router.message(StateFilter("admin_waiting_for_work_file"), F.document)
async def admin_admin_work_file_received(message: Message, state: FSMContext):
    from datetime import datetime
    data = await state.get_data()
    order_id = data.get('submit_order_id')
    file_id = message.document.file_id
    file_name = message.document.file_name
    orders = get_all_orders()
    order = None
    is_admin_executor = False
    for o in orders:
        if o.get('order_id') == order_id:
            if str(o.get('executor_id')) == str(ADMIN_ID):
                is_admin_executor = True
                o['status'] = 'Утверждено администратором'  # <-- исправлено!
                o['submitted_work'] = {'file_id': file_id, 'file_name': file_name}
                o['submitted_at'] = datetime.now().strftime('%d.%m.%Y')
                order = o
            else:
                o['status'] = 'Отправлен на проверку'
                o['submitted_work'] = {'file_id': file_id, 'file_name': file_name}
                o['submitted_at'] = datetime.now().strftime('%d.%m.%Y')
                order = o
            break
    with open('orders.json', 'w', encoding='utf-8') as f:
        json.dump(orders, f, ensure_ascii=False, indent=4)
    subject = order.get('subject', 'Не указан') if order else ''
    work_type = order.get('work_type', 'Не указан').replace('work_type_', '') if order else ''
    submitted_at = order.get('submitted_at', '') if order else ''
    if is_admin_executor:
        customer_id = order.get('user_id')
        if customer_id:
            caption = f"✅ Ваша работа по заказу №{order_id} готова!\nПредмет: {subject}\nТип работы: {work_type}\nДата выполнения: {submitted_at}"
            keyboard = get_client_work_approval_keyboard(order_id)
            await bot.send_document(
                chat_id=customer_id,
                document=file_id,
                caption=caption,
                reply_markup=keyboard
            )
        await message.answer("✅ Файл успешно отправлен, ожидаем подтверждения от заказчика", reply_markup=None)
        # Удаляем возврат к деталям заявки — больше не вызываем admin_view_order_handler
        await state.clear()
        return  # <--- ГАРАНТИРОВАННО ОСТАНАВЛИВАЕМ ФУНКЦИЮ ДЛЯ АДМИНА
    # Только для обычных исполнителей
    admin_text = f"Вы отправили работу по заказу №{order_id} на проверку!\n\nПредмет: {subject}\nТип работы: {work_type}\nДата выполнения: {submitted_at}"
    if order:
        await message.answer(admin_text, reply_markup=get_admin_order_keyboard(order, show_materials_button=True))
    else:
        await message.answer("Ошибка: заказ не найден.")
    await state.clear()


@admin_router.callback_query(F.data == "admin_back")
async def admin_back_handler(callback: CallbackQuery, state: FSMContext):
    await show_admin_orders_list(callback)
    await callback.answer()

# --- Админ-панель ---

# Фильтр, чтобы эти хендлеры работали только для админа
@admin_router.message(Command("admin"))
async def cmd_admin_panel(message: Message, state: FSMContext):
    # Убедимся, что это админ
    if message.from_user.id != int(ADMIN_ID):
        return
    await state.clear()
    await message.answer(
        "Добро пожаловать в панель администратора!",
        reply_markup=get_admin_keyboard()
    )

async def show_admin_orders_list(message_or_callback, state=None):
    """Показывает список всех заказов для админа, используя edit_text для callback и answer для message. Сбрасывает FSM-состояние для предотвращения багов с кнопками."""
    user_id = message_or_callback.from_user.id
    if user_id != int(ADMIN_ID): return

    # Сброс состояния FSM, если передан state
    if state is not None:
        await state.clear()

    orders = get_all_orders()
    if not orders:
        if hasattr(message_or_callback, 'message'):
            await message_or_callback.message.edit_text("Пока нет ни одного заказа.")
        else:
            await message_or_callback.answer("Пока нет ни одного заказа.")
        return

    text = "Все заказы:"
    keyboard_buttons = []
    for order in reversed(orders[-20:]): # Показываем последние 20
        order_id = order['order_id']
        order_status = order.get('status', 'N/A')
        work_type_raw = order.get('work_type', 'Заявка')
        work_type = work_type_raw.replace('work_type_', '')
        subject = order.get('subject', 'Без темы')
        button_text = f"Заказ на тему {subject} ({work_type}) - {order_status}"
        keyboard_buttons.append([InlineKeyboardButton(text=button_text, callback_data=f"admin_view_order_{order_id}")])
    keyboard = InlineKeyboardMarkup(inline_keyboard=keyboard_buttons)
    if hasattr(message_or_callback, 'message'):
        try:
            await message_or_callback.message.edit_text(text, reply_markup=keyboard)
        except Exception:
            await message_or_callback.message.answer(text, reply_markup=keyboard)
    else:
        await message_or_callback.answer(text, reply_markup=keyboard)

@admin_router.message(F.text == "📦 Все заказы")
async def show_all_orders_handler(message_or_callback):
    await show_admin_orders_list(message_or_callback)

@router.callback_query(F.data.startswith("admin_view_order_"))
async def admin_view_order_handler(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != int(ADMIN_ID): return
    order_id = int(callback.data.split("_")[-1])
    orders = get_all_orders()
    target_order = next((order for order in orders if order['order_id'] == order_id), None)
    if not target_order:
        await callback.answer("Заказ не найден.", show_alert=True)
        return
    status = target_order.get('status')
    # --- Новый вид для статуса 'Ожидает подтверждения' с несколькими офферами ---
    if status == 'Ожидает подтверждения' and target_order.get('executor_offers'):
        offers = target_order['executor_offers']
        n = len(offers)
        text = f"На данный момент есть {n} оффер(ов) от исполнителей на заказ.\n\nВыберите оффер для просмотра:" 
        buttons = []
        for offer in offers:
            fio = offer.get('executor_full_name', 'Без ФИО')
            btn_text = f"{fio}"
            buttons.append([InlineKeyboardButton(text=btn_text, callback_data=f"admin_offer_details_{order_id}_{offer.get('executor_id')}")])
        buttons.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="admin_back")])
        keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
        await callback.message.edit_text(text, reply_markup=keyboard)
        await callback.answer()
        return
    # ... остальной код без изменений ...
    elif status == "Рассматривается":
        full_name = get_full_name(target_order)
        header = f"Детали заказа №{order_id} от клиента ({full_name})\n"
        if target_order.get('creation_date'):
            header += f"Дата создания: {target_order.get('creation_date')}\n"
        group = target_order.get("group_name", "Не указана")
        university = target_order.get("university_name", "Не указан")
        teacher = target_order.get("teacher_name", "Не указан")
        gradebook = target_order.get("gradebook", "Не указан")
        subject = target_order.get("subject", "Не указан")
        work_type_key = target_order.get("work_type", "N/A").replace("work_type_", "")
        work_type_str = work_type_key if work_type_key != 'other' else target_order.get('work_type_other_name', 'Другое')
        guidelines = '✅ Да' if target_order.get('has_guidelines') else '❌ Нет'
        task = '✅ Прикреплено' if target_order.get('task_file') or target_order.get('task_text') else '❌ Нет'
        example = '✅ Да' if target_order.get('has_example') else '❌ Нет'
        deadline = target_order.get('deadline', 'Не указана')
        deadline_str = pluralize_days(deadline) if isinstance(deadline, str) and deadline.isdigit() else deadline
        comments = target_order.get('comments', 'Нет')
        details_text = (
            f"{header}\n"
            f"Группа: {group}\n"
            f"ВУЗ: {university}\n"
            f"Преподаватель: {teacher}\n"
            f"Номер зачетки: {gradebook}\n"
            f"Предмет: {subject}\n"
            f"Тип работы: {work_type_str}\n"
            f"Методичка: {guidelines}\n"
            f"Задание: {task}\n"
            f"Пример: {example}\n"
            f"Дедлайн: {deadline_str}\n"
            f"Комментарии: {comments}"
        )
        keyboard = get_admin_order_keyboard(target_order, show_materials_button=True)
        try:
            await callback.message.edit_text(details_text, reply_markup=keyboard)
        except Exception:
            await callback.message.answer(details_text, reply_markup=keyboard)
        await callback.answer()
        return

    elif status == 'Отправлен на проверку':
        submitted_work = target_order.get('submitted_work')
        submitted_at = target_order.get('submitted_at', '—')
        subject = target_order.get('subject', 'Не указан')
        work_type = target_order.get('work_type', 'Не указан').replace('work_type_', '')
        admin_text = f"Исполнитель выполнил заказ по предмету <b>{subject}</b>\nТип работы: <b>{work_type}</b>\nДата выполнения: <b>{submitted_at}</b>"
        admin_keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Утвердить работу", callback_data=f"admin_approve_work_{order_id}")],
            [InlineKeyboardButton(text="🔽 Отправить на доработку", callback_data=f"admin_reject_work_{order_id}")],
            [InlineKeyboardButton(text="📎 Посмотреть материалы заказа", callback_data=f"admin_show_materials:{order_id}")]
        ])
        if submitted_work and submitted_work.get('file_id'):
            await callback.message.delete()
            await bot.send_document(
                callback.from_user.id,
                submitted_work['file_id'],
                caption=None,
                parse_mode=None,
                reply_markup=None
            )
            await bot.send_message(
                callback.from_user.id,
                admin_text,
                parse_mode="HTML",
                reply_markup=admin_keyboard
            )
        else:
            await callback.message.edit_text(admin_text, parse_mode="HTML", reply_markup=admin_keyboard)

    elif status == "В работе":
        full_name = get_full_name(target_order)
        header = f"Детали заказа №{order_id} от клиента ({full_name})\n"
        if target_order.get('creation_date'):
            header += f"Дата создания: {target_order.get('creation_date')}\n"
        group = target_order.get("group_name", "Не указана")
        university = target_order.get("university_name", "Не указан")
        teacher = target_order.get("teacher_name", "Не указан")
        gradebook = target_order.get("gradebook", "Не указан")
        subject = target_order.get("subject", "Не указан")
        work_type_key = target_order.get("work_type", "N/A").replace("work_type_", "")
        work_type_str = work_type_key if work_type_key != 'other' else target_order.get('work_type_other_name', 'Другое')
        guidelines = '✅ Да' if target_order.get('has_guidelines') else '❌ Нет'
        task = '✅ Прикреплено' if target_order.get('task_file') or target_order.get('task_text') else '❌ Нет'
        example = '✅ Да' if target_order.get('has_example') else '❌ Нет'
        deadline = target_order.get('deadline', 'Не указана')
        deadline_str = pluralize_days(deadline) if isinstance(deadline, str) and deadline.isdigit() else deadline
        executor_id = target_order.get('executor_id')
        executor_info = ""
        if executor_id:
            if str(executor_id) == str(ADMIN_ID):
                executor_info = f"Исполнитель: Я"
            else:
                # Здесь можно добавить логику для получения информации о другом исполнителе
                executor_info = f"Исполнитель: {executor_id}"
        details_text = (
            f"{header}\n"
            f"Группа: {group}\n"
            f"ВУЗ: {university}\n"
            f"Преподаватель: {teacher}\n"
            f"Номер зачетки: {gradebook}\n"
            f"Предмет: {subject}\n"
            f"Тип работы: {work_type_str}\n"
            f"Методичка: {guidelines}\n"
            f"Задание: {task}\n"
            f"Пример: {example}\n"
            f"Дедлайн: {deadline_str}\n"
            f"{executor_info}"
        )
        keyboard = get_admin_order_keyboard(target_order, show_materials_button=True)
        try:
            await callback.message.edit_text(details_text, reply_markup=keyboard)
        except Exception:
            await callback.message.answer(details_text, reply_markup=keyboard)
        await callback.answer()
        return
    
    elif status == "Ожидает оплаты":
        full_name = get_full_name(target_order)
        header = f"Детали заказа №{order_id} от клиента ({full_name})\n"
        if target_order.get('creation_date'):
            header += f"Дата создания: {target_order.get('creation_date')}\n"
        group = target_order.get("group_name", "Не указана")
        university = target_order.get("university_name", "Не указан")
        teacher = target_order.get("teacher_name", "Не указан")
        gradebook = target_order.get("gradebook", "Не указан")
        subject = target_order.get("subject", "Не указан")
        work_type_key = target_order.get("work_type", "N/A").replace("work_type_", "")
        work_type_str = work_type_key if work_type_key != 'other' else target_order.get('work_type_other_name', 'Другое')
        guidelines = '✅ Да' if target_order.get('has_guidelines') else '❌ Нет'
        task = '✅ Прикреплено' if target_order.get('task_file') or target_order.get('task_text') else '❌ Нет'
        example = '✅ Да' if target_order.get('has_example') else '❌ Нет'
        deadline = target_order.get('deadline', 'Не указана')
        deadline_str = pluralize_days(deadline) if isinstance(deadline, str) and deadline.isdigit() else deadline
        # --- Исполнитель ---
        executor_id = target_order.get('executor_id')
        executor_offer = target_order.get('executor_offers', {})
        executor_full_name = executor_offer.get('executor_full_name')
        if executor_full_name and executor_offer.get('executor_id'):
            executor_display = f"{executor_full_name} - {executor_offer.get('executor_id')}"
        elif executor_id:
            # Пытаемся найти ФИО по executor_id в executors.json
            from shared import get_executors_list
            executors = get_executors_list()
            found_name = None
            for ex in executors:
                if str(ex.get('id')) == str(executor_id):
                    found_name = ex.get('name') or '—'
                    break
            if found_name:
                executor_display = f"{found_name} - {executor_id}"
            else:
                executor_display = f"ID {executor_id}"
        else:
            executor_display = '—'
        details_text = (
            f"{header}\n"
            f"Группа: {group}\n"
            f"ВУЗ: {university}\n"
            f"Преподаватель: {teacher}\n"
            f"Номер зачетки: {gradebook}\n"
            f"Предмет: {subject}\n"
            f"Тип работы: {work_type_str}\n"
            f"Методичка: {guidelines}\n"
            f"Задание: {task}\n"
            f"Пример: {example}\n"
            f"Дедлайн: {deadline_str}\n"
            f"Исполнитель: {executor_display}\n"
            f"Ожидаем оплату...."
        )
        keyboard = get_admin_order_keyboard(target_order, show_materials_button=True)
        try:
            await callback.message.edit_text(details_text, reply_markup=keyboard)
        except Exception:
            await callback.message.answer(details_text, reply_markup=keyboard)
        await callback.answer()
        return
            
    elif status == "Утверждено администратором":
        full_name = get_full_name(target_order)
        header = f"Детали заказа №{order_id} от клиента ({full_name})\n"
        if target_order.get('creation_date'):
            header += f"Дата создания: {target_order.get('creation_date')}\n"
        
        group = target_order.get("group_name", "Не указана")
        university = target_order.get("university_name", "Не указан")
        teacher = target_order.get("teacher_name", "Не указан")
        gradebook = target_order.get("gradebook", "Не указан")
        subject = target_order.get("subject", "Не указан")
        work_type_key = target_order.get("work_type", "N/A").replace("work_type_", "")
        work_type_str = work_type_key if work_type_key != 'other' else target_order.get('work_type_other_name', 'Другое')
        guidelines = '✅ Да' if target_order.get('has_guidelines') else '❌ Нет'
        task = '✅ Прикреплено' if target_order.get('task_file') or target_order.get('task_text') else '❌ Нет'
        example = '✅ Да' if target_order.get('has_example') else '❌ Нет'
        deadline = target_order.get('deadline', 'Не указана')
     
        deadline_str = pluralize_days(deadline) if isinstance(deadline, str) and deadline.isdigit() else deadline

        details_text = f"""{header}
Группа: {group}
ВУЗ: {university}
Преподаватель: {teacher}
Номер зачетки: {gradebook}
Предмет: {subject}
Тип работы: {work_type_str}
Методичка: {guidelines}
Задание: {task}
Пример: {example}
Дедлайн: {deadline_str}"""

        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⬅️ Вернуться к заявкам", callback_data="admin_back")]
        ])
        
        try:
            await callback.message.edit_text(details_text, reply_markup=keyboard)
        except Exception:
            await callback.message.answer(details_text, reply_markup=keyboard)

    elif status == "Выполнена":
        full_name = get_full_name(target_order)
        header = f"Детали заказа №{order_id} от клиента ({full_name})\n"
        if target_order.get('creation_date'):
            header += f"Дата создания: {target_order.get('creation_date')}\n"
        group = target_order.get("group_name", "Не указана")
        university = target_order.get("university_name", "Не указан")
        teacher = target_order.get("teacher_name", "Не указан")
        gradebook = target_order.get("gradebook", "Не указан")
        subject = target_order.get("subject", "Не указан")
        work_type_key = target_order.get("work_type", "N/A").replace("work_type_", "")
        work_type_str = work_type_key if work_type_key != 'other' else target_order.get('work_type_other_name', 'Другое')
        guidelines = '✅ Да' if target_order.get('has_guidelines') else '❌ Нет'
        task = '✅ Прикреплено' if target_order.get('task_file') or target_order.get('task_text') else '❌ Нет'
        example = '✅ Да' if target_order.get('has_example') else '❌ Нет'
        deadline = target_order.get('deadline', 'Не указана')
        deadline_str = pluralize_days(deadline) if isinstance(deadline, str) and deadline.isdigit() else deadline

        # Получаем заработано (final_price)
        earned = target_order.get('final_price', 0)

        details_text = (
            f"{header}Группа: {group}\nВУЗ: {university}\nПреподаватель: {teacher}\nНомер зачетки: {gradebook}\n"
            f"Предмет: {subject}\nТип работы: {work_type_str}\nМетодичка: {guidelines}\nЗадание: {task}\n"
            f"Пример: {example}\nДедлайн: {deadline_str}\n"
        )
        executor_offer = target_order.get('executor_offers', {})
        if isinstance(executor_offer, list):
            executor_offer = executor_offer[0] if executor_offer else {}
        work_price = executor_offer.get('price', 0)
        admin_price = target_order.get('final_price', 0)
        try:
            work_price = float(work_price)
        except Exception:
            work_price = 0
        try:
            admin_price = float(admin_price)
        except Exception:
            admin_price = 0
        profit = admin_price - work_price
        # Если исполнитель — админ, добавляем пометку
        if str(target_order.get('executor_id')) == str(ADMIN_ID):
            details_text += "\nРабота отправлена клиенту."
        else:
            details_text += (
                f"Цена исполнителя: {work_price} ₽\n"
                f"Моя цена: {admin_price} ₽\n"
                f"Заработано: {profit} ₽"
            )

        # Клавиатура — всегда get_admin_order_keyboard (там уже есть логика для кнопки "Сохранить в таблицу")
        keyboard = get_admin_order_keyboard(target_order, show_materials_button=True)
        try:
            await callback.message.edit_text(details_text, reply_markup=keyboard)
        except Exception:
            await callback.message.answer(details_text, reply_markup=keyboard)
        await callback.answer()
        return

    elif status == "На доработке":
        executor_is_admin = str(target_order.get('executor_id')) == str(ADMIN_ID)
        # --- Формируем details_text как раньше ---
        if executor_is_admin:
            creation_date = target_order.get('creation_date', '—')
            group = target_order.get('group_name', '—')
            university = target_order.get('university_name', '—')
            teacher = target_order.get('teacher_name', '—')
            gradebook = target_order.get('gradebook', '—')
            subject = target_order.get('subject', '—')
            work_type_key = target_order.get('work_type', 'N/A').replace('work_type_', '')
            work_type_str = work_type_key if work_type_key != 'other' else target_order.get('work_type_other_name', 'Другое')
            guidelines = '✅ Да' if target_order.get('has_guidelines') else '❌ Нет'
            task = '✅ Прикреплено' if target_order.get('task_file') or target_order.get('task_text') else '❌ Нет'
            example = '✅ Да' if target_order.get('has_example') else '❌ Нет'
            deadline = target_order.get('deadline', '—')
            deadline_str = pluralize_days(deadline) if isinstance(deadline, str) and deadline.isdigit() else deadline
            revision_comment = target_order.get('revision_comment', '—')
            executor_id = target_order.get('executor_id')
            executor_offer = target_order.get('executor_offers', {})
            executor_full_name = executor_offer.get('executor_full_name')
            if str(executor_id) == str(ADMIN_ID):
                executor_display = 'Я'
            elif executor_full_name:
                executor_display = executor_full_name
            elif executor_id:
                try:
                    from shared import get_executors_list
                    executors = get_executors_list()
                    executor_display = next((ex.get('name') for ex in executors if str(ex.get('id')) == str(executor_id)), f'ID {executor_id}')
                except Exception:
                    executor_display = f'ID {executor_id}'
            else:
                executor_display = 'Не назначен'
            details_text = (
                f"Дата создания: {creation_date}\n\n"
                f"Группа: {group}\n"
                f"ВУЗ: {university}\n"
                f"Преподаватель: {teacher}\n"
                f"Номер зачетки: {gradebook}\n"
                f"Предмет: {subject}\n"
                f"Тип работы: {work_type_str}\n"
                f"Методичка: {guidelines}\n"
                f"Задание: {task}\n"
                f"Пример: {example}\n"
                f"Дедлайн: {deadline_str}\n\n"
                f"Доработка: {revision_comment}\n"
                f"Исполнитель: {executor_display}"
            )
        else:
            # --- Стандартный вид для обычного исполнителя ---
            full_name = get_full_name(target_order)
            header = ""
            if target_order.get('creation_date'):
                header += f"Дата создания: {target_order.get('creation_date')}\n\n"
            group = target_order.get("group_name", "Не указана")
            university = target_order.get("university_name", "Не указан")
            teacher = target_order.get("teacher_name", "Не указан")
            gradebook = target_order.get("gradebook", "Не указан")
            subject = target_order.get("subject", "Не указан")
            work_type_key = target_order.get("work_type", "N/A").replace("work_type_", "")
            work_type_str = work_type_key if work_type_key != 'other' else target_order.get('work_type_other_name', 'Другое')
            guidelines = '✅ Да' if target_order.get('has_guidelines') else '❌ Нет'
            task = '✅ Прикреплено' if target_order.get('task_file') or target_order.get('task_text') else '❌ Нет'
            example = '✅ Да' if target_order.get('has_example') else '❌ Нет'
            deadline = target_order.get('deadline', 'Не указана')
            deadline_str = pluralize_days(deadline) if isinstance(deadline, str) and deadline.isdigit() else deadline
            executor_offer = target_order.get('executor_offers', {})
            executor_full_name = executor_offer.get('executor_full_name', '—')
            revision_comment = target_order.get('revision_comment', '')
            executor_id = target_order.get('executor_id')
            executor_offer = target_order.get('executor_offers', {})
            executor_full_name = executor_offer.get('executor_full_name')
            if str(executor_id) == str(ADMIN_ID):
                executor_display = 'Я'
            elif executor_full_name:
                executor_display = executor_full_name
            elif executor_id:
                try:
                    from shared import get_executors_list
                    executors = get_executors_list()
                    executor_display = next((ex.get('name') for ex in executors if str(ex.get('id')) == str(executor_id)), f'ID {executor_id}')
                except Exception:
                    executor_display = f'ID {executor_id}'
            else:
                executor_display = 'Не назначен'
            details_text = f"""{header}Группа: {group}\nВУЗ: {university}\nПреподаватель: {teacher}\nНомер зачетки: {gradebook}\nПредмет: {subject}\nТип работы: {work_type_str}\nМетодичка: {guidelines}\nЗадание: {task}\nПример: {example}\nДедлайн: {deadline_str}\nИсполнитель: {executor_display} - {executor_id}"""
            if revision_comment:
                details_text += f"\n\nДоработка: {revision_comment}"

        # --- Клавиатура ---
        buttons = []
        has_files = target_order.get('guidelines_file') or target_order.get('task_file') or target_order.get('task_text') or target_order.get('example_file')
        if executor_is_admin:
            buttons.append([InlineKeyboardButton(text="✅ Сдать работу", callback_data=f"admin_admin_submit_work_{order_id}")])
        if has_files:
            buttons.append([InlineKeyboardButton(text="📎 Посмотреть материалы заказа", callback_data=f"admin_show_materials:{order_id}")])
        buttons.append([InlineKeyboardButton(text="⬅️ Вернуться к заявкам", callback_data="admin_back")])
        keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)

        try:
            await callback.message.edit_text(details_text, reply_markup=keyboard)
        except Exception:
            await callback.message.answer(details_text, reply_markup=keyboard)
        await callback.answer()
        return
        # --- Стандартный вид для обычного исполнителя ---
    full_name = get_full_name(target_order)
    header = ""
    if target_order.get('creation_date'):
        header += f"Дата создания: {target_order.get('creation_date')}\n\n"
        group = target_order.get("group_name", "Не указана")
        university = target_order.get("university_name", "Не указан")
        teacher = target_order.get("teacher_name", "Не указан")
        gradebook = target_order.get("gradebook", "Не указан")
        subject = target_order.get("subject", "Не указан")
        work_type_key = target_order.get("work_type", "N/A").replace("work_type_", "")
        work_type_str = work_type_key if work_type_key != 'other' else target_order.get('work_type_other_name', 'Другое')
        guidelines = '✅ Да' if target_order.get('has_guidelines') else '❌ Нет'
        task = '✅ Прикреплено' if target_order.get('task_file') or target_order.get('task_text') else '❌ Нет'
        example = '✅ Да' if target_order.get('has_example') else '❌ Нет'
        deadline = target_order.get('deadline', 'Не указана')
        deadline_str = pluralize_days(deadline) if isinstance(deadline, str) and deadline.isdigit() else deadline
        executor_offer = target_order.get('executor_offers', {})
        executor_full_name = executor_offer.get('executor_full_name', '—')
        revision_comment = target_order.get('revision_comment', '')
        # --- Исполнитель ---
        executor_id = target_order.get('executor_id')
        executor_offer = target_order.get('executor_offers', {})
        executor_full_name = executor_offer.get('executor_full_name')
        if str(executor_id) == str(ADMIN_ID):
            executor_display = 'Я'
        elif executor_full_name:
            executor_display = executor_full_name
        elif executor_id:
            try:
                from shared import get_executors_list
                executors = get_executors_list()
                executor_display = next((ex.get('name') for ex in executors if str(ex.get('id')) == str(executor_id)), f'ID {executor_id}')
            except Exception:
                executor_display = f'ID {executor_id}'
        else:
            executor_display = 'Не назначен'
        details_text = f"""{header}Группа: {group}\nВУЗ: {university}\nПреподаватель: {teacher}\nНомер зачетки: {gradebook}\nПредмет: {subject}\nТип работы: {work_type_str}\nМетодичка: {guidelines}\nЗадание: {task}\nПример: {example}\nДедлайн: {deadline_str}\nИсполнитель: {executor_display} - {executor_id}"""
        if revision_comment:
            details_text += f"\n\nДоработка: {revision_comment}"
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="admin_back")]
        ])
        try:
            await callback.message.edit_text(details_text, reply_markup=keyboard)
        except Exception:
            await callback.message.answer(details_text, reply_markup=keyboard)
        await callback.answer()
        return

    else: # --- Обычное поведение для остальных статусов ---
        summary_text = await build_summary_text(target_order)
        full_name = f"{target_order.get('first_name', '')} {target_order.get('last_name', '')}".strip()
        header = f"\n<b>Детали заказа №{order_id} от клиента ({full_name})</b>\n"
        if target_order.get('creation_date'):
            header += f"<b>Дата создания:</b> {target_order.get('creation_date')}\n"
        details_text = header + "\n" + summary_text
        show_materials_button = bool(
            target_order.get("guidelines_file") or
            target_order.get("task_file") or
            target_order.get("task_text") or
            target_order.get("example_file")
        )
        keyboard = get_admin_order_keyboard(target_order, show_materials_button=True)
        try:
            await callback.message.edit_text(details_text, reply_markup=keyboard, parse_mode="HTML")
        except Exception:
            await callback.message.answer(details_text, reply_markup=keyboard, parse_mode="HTML")

    await callback.answer()


@admin_router.callback_query(F.data.startswith("admin_offer_details_"))
async def admin_offer_details_handler(callback: CallbackQuery, state: FSMContext):
    # Разделяем callback.data: admin_offer_details_{order_id}_{executor_id}
    try:
        parts = callback.data.split("_")
        # ['admin', 'offer', 'details', '{order_id}', '{executor_id}']
        order_id = int(parts[3])
        executor_id = int(parts[4])
    except Exception:
        await callback.answer("Ошибка разбора данных callback.", show_alert=True)
        return

    orders = get_all_orders()
    target_order = next((order for order in orders if order.get('order_id') == order_id), None)
    if not target_order:
        await callback.answer("Заказ не найден.", show_alert=True)
        return

    offers = target_order.get('executor_offers', [])
    offer = next((o for o in offers if o.get('executor_id') == executor_id), None)
    if not offer:
        await callback.answer("Оффер не найден.", show_alert=True)
        return

    fio = offer.get('executor_full_name', 'Без ФИО')
    price = offer.get('price', '—')
    deadline = offer.get('deadline', 'N/A')
    executor_comment = offer.get('executor_comment', 'Нет')
    subject = target_order.get('subject', 'Не указан')
    work_type = target_order.get('work_type', 'N/A').replace('work_type_', '')

    # Корректно определяем срок
    try:
        deadline_str = pluralize_days(deadline) if str(deadline).strip().lower() != 'до дедлайна' else target_order.get('deadline', 'Не указан')
    except Exception:
        deadline_str = deadline

    text = (
        f"✅ Исполнитель {fio} (ID: {executor_id}) готов взяться за заказ по предмету \"{subject}\"\n\n"
        f"<b>Предложенные условия:</b>\n"
        f"💰 <b>Цена:</b> {price} ₽\n"
        f"⏳ <b>Срок:</b> {deadline_str}\n"
        f"💬 <b>Комментарий исполнителя:</b> {executor_comment or 'Нет'}"
    )
    keyboard = get_admin_final_approval_keyboard(order_id, price, executor_id)
    try:
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=keyboard)
    except Exception:
        await callback.message.answer(text, parse_mode="HTML", reply_markup=keyboard)
    await callback.answer()


@admin_router.callback_query(F.data.startswith("assign_executor_start_"))
async def assign_executor_start_handler(callback: CallbackQuery, state: FSMContext):
    order_id = int(callback.data.split("_")[-1])
    await state.update_data(order_id=order_id)
    await callback.message.edit_text("Выберите исполнителя:", reply_markup=get_executors_assign_keyboard(order_id))
    await callback.answer()

async def send_order_to_executor(message_or_callback, order_id: int, executor_id: int):
    """Находит заказ, присваивает исполнителя и отправляет ему уведомление (только через orders.json)."""
    orders = get_all_orders()
    target_order = None
    for order in orders:            
        if order.get("order_id") == order_id:
            target_order = order
            break
    if not target_order:
        text = f"Критическая ошибка: заказ №{order_id} не найден для обновления."
        if hasattr(message_or_callback, 'message'):
            await message_or_callback.message.answer(text)
        else:
            await message_or_callback.answer(text)
        return

    target_order['status'] = "Ожидает подтверждения"
    target_order['executor_id'] = executor_id
    with open("orders.json", "w", encoding="utf-8") as f:
        json.dump(orders, f, ensure_ascii=False, indent=4)

    work_type = target_order.get('work_type', 'N/A').replace('work_type_', '')
    subject = target_order.get('subject', 'Не указан')
    deadline = target_order.get('deadline', 'Не указан')
    executor_caption = (
        f"📬 Вам предложен новый заказ по предмету <b>{subject}</b>\n\n"
        f"📝 <b>Тип работы:</b> {work_type}\n"
        f"🗓 <b>Срок сдачи:</b> {deadline}\n\n"
        "Пожалуйста, ознакомьтесь с материалами заявки и примите решение."
    )
    executor_keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📎 Посмотреть материалы заказа", callback_data=f"executor_show_materials:{order_id}")],
        [InlineKeyboardButton(text="✅ Готов взяться", callback_data=f"executor_accept_{order_id}"),
         InlineKeyboardButton(text="❌ Отказаться", callback_data=f"executor_refuse_{order_id}")],
    ])
    # Получаем ФИО исполнителя
    executor_name = "Без ФИО"
    for ex in get_executors_list():
        if str(ex.get('id')) == str(executor_id):
            executor_name = ex.get('name') or 'Без ФИО'
            break
    try:
        await bot.send_message(executor_id, executor_caption, parse_mode="HTML", reply_markup=executor_keyboard)
        success_text = f"✅ Предложение по заказу: '{work_type}'\nОтправлено исполнителю: {executor_name} с ID {executor_id}."
        if hasattr(message_or_callback, 'message'):
            await message_or_callback.message.answer(success_text)
        else:
            await message_or_callback.answer(success_text)
    except Exception as e:
        error_text = f"⚠️ Не удалось отправить уведомление исполнителю (ID: {executor_id}).\n\n<b>Ошибка:</b> {e}"
        target_order['status'] = "Рассматривается"
        target_order.pop('executor_id', None)
        with open("orders.json", "w", encoding="utf-8") as f:
            json.dump(orders, f, ensure_ascii=False, indent=4)
        if hasattr(message_or_callback, 'message'):
            await message_or_callback.message.answer(error_text, parse_mode="HTML")
        else:
            await message_or_callback.answer(error_text, parse_mode="HTML")

@admin_router.callback_query(F.data.startswith("assign_executor_select_"))
async def assign_executor_select_handler(callback: CallbackQuery, state: FSMContext):
    parts = callback.data.split("_")
    order_id = int(parts[3])
    executor_id = int(parts[4])
    await state.clear()
    await send_order_to_executor(callback, order_id, executor_id)
    try:
        await callback.message.delete()
    except Exception:
        pass

@admin_router.callback_query(F.data == "assign_executor_manual")
async def assign_executor_manual_handler(callback: CallbackQuery, state: FSMContext):
    await state.set_state(AssignExecutor.waiting_for_id)
    await callback.message.edit_text("Введите Telegram ID исполнителя:")
    await callback.answer()

@admin_router.message(AssignExecutor.waiting_for_id)
async def assign_executor_process_id_handler(message: Message, state: FSMContext):
    if message.from_user.id != int(ADMIN_ID): return
    
    if not message.text.isdigit():
        await message.answer("Ошибка: ID должен быть числом. Попробуйте еще раз.")
        return

    executor_id = int(message.text)
    data = await state.get_data()
    order_id = data.get('order_id')
    
    # Находим и обновляем заказ
    orders = get_all_orders()
    target_order = None
    for order in orders:
        if order.get("order_id") == order_id:
            order['status'] = "Ожидает подтверждения"
            order['executor_id'] = executor_id
            target_order = order
            break

    if not target_order:
        await message.answer("Критическая ошибка: заказ не найден для обновления.")
        await state.clear()
        return

    # Сохраняем обновленный список заказов
    with open("orders.json", "w", encoding="utf-8") as f:
        json.dump(orders, f, ensure_ascii=False, indent=4)
    
    # Уведомляем всех
    await message.answer(f"✅ Предложение отправлено исполнителю с ID {executor_id} для заказа №{order_id}.")
    
    # Уведомление для исполнителя
    work_type = target_order.get('work_type', 'N/A').replace('work_type_', '')
    subject = target_order.get('subject', 'Не указан')
    deadline = target_order.get('deadline', 'Не указан')
    executor_caption = (
        f"📬 Вам предложен новый заказ по предмету <b>{subject}</b>\n\n"
        f"📝 <b>Тип работы:</b> {work_type}\n"
        f"🗓 <b>Срок сдачи:</b> {deadline}\n\n"
        "Пожалуйста, ознакомьтесь с материалами заявки и примите решение."
    )
    executor_keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📎 Посмотреть материалы заказа", callback_data=f"executor_show_materials:{order_id}")],
        [InlineKeyboardButton(text="✅ Готов взяться", callback_data=f"executor_accept_{order_id}"),
         InlineKeyboardButton(text="❌ Отказаться", callback_data=f"executor_refuse_{order_id}")]
    ])
    try:
        await bot.send_message(executor_id, executor_caption, parse_mode="HTML", reply_markup=executor_keyboard)
    except Exception as e:
        await message.answer(f"⚠️ Не удалось отправить уведомление исполнителю (ID: {executor_id}). Ошибка: {e}")
        target_order['status'] = "Рассматривается"
        target_order.pop('executor_id', None)
        with open("orders.json", "w", encoding="utf-8") as f:
            json.dump(orders, f, ensure_ascii=False, indent=4)
    await state.clear()

@router.callback_query(F.data.startswith("client_request_revision:"))
async def client_request_revision(callback: CallbackQuery, state: FSMContext):
    order_id = int(callback.data.split(':')[-1])
    orders = get_all_orders()
    target_order = next((o for o in orders if o.get('order_id') == order_id), None)
    if not target_order:
        await callback.answer("Не удалось найти заказ для отправки на доработку.", show_alert=True)
        return
    # Меняем статус на "На доработке"
    target_order['status'] = "На доработке"
    with open("orders.json", "w", encoding="utf-8") as f:
        json.dump(orders, f, ensure_ascii=False, indent=4)
    await state.set_state(ClientRevision.waiting_for_revision_comment)
    await state.update_data(revision_order_id=order_id)
    try:
        await callback.message.edit_text("✍️ Пожалуйста, подробно опишите, какие доработки требуются. Ваше сообщение будет передано исполнителю.")
    except Exception:
        await bot.send_message(callback.from_user.id, "✍️ Пожалуйста, подробно опишите, какие доработки требуются. Ваше сообщение будет передано исполнителю.") 
    await callback.answer()

def update_order_status_in_gsheet(order_id, new_status):
    creds = Credentials.from_service_account_file("google-credentials.json", scopes=["https://www.googleapis.com/auth/spreadsheets"])
    gc = gspread.authorize(creds)
    sh = gc.open_by_key(GOOGLE_SHEET_ID)
    worksheet = sh.sheet1
    cell = worksheet.find(str(order_id))
    if cell:
        # Предположим, что статус во втором столбце (B)
        worksheet.update_cell(cell.row, 14, new_status)

@router.callback_query(F.data.startswith("client_accept_work:"))
async def client_accept_work(callback: CallbackQuery, state: FSMContext):
    order_id = int(callback.data.split(':')[-1])
    orders = get_all_orders()
    target_order = next((o for o in orders if o.get('order_id') == order_id), None)
    if not target_order:
        await callback.answer("Заказ не найден", show_alert=True)
        return

    target_order['status'] = "Выполнена"
    with open("orders.json", "w", encoding="utf-8") as f:
        json.dump(orders, f, ensure_ascii=False, indent=4)

    # Уведомление клиенту
    try:
        await callback.message.edit_text("🎉 Спасибо, что приняли работу! Рады были помочь.")
    except Exception:
        await callback.message.answer("🎉 Спасибо, что приняли работу! Рады были помочь.")

    # Уведомление администратору
    try:
        subject = target_order.get('subject', 'Не указан')
        work_type = target_order.get('work_type', 'Не указан').replace('work_type_', '')
        executor_offer = target_order.get('executor_offers', {})
        if isinstance(executor_offer, list):
            executor_offer = executor_offer[0] if executor_offer else {}
        work_price = 0
        admin_price = 0
        try:
            work_price = float(executor_offer.get('price', 0) or 0)
        except Exception:
            work_price = 0
        try:
            admin_price = float(target_order.get('final_price', 0) or 0)
        except Exception:
            admin_price = 0
        profit = admin_price - work_price
        admin_text = (
            f"✅ Клиент принял работу по заказу!\n"
            f"Предмет: {subject}\n"
            f"Тип работы: {work_type}\n\n"
            f"Заработано: {profit} ₽"
        )
        await bot.send_message(
            ADMIN_ID,
            admin_text
        )
    except Exception:
        pass

    # Уведомление исполнителю (если не админ)
    executor_id = target_order.get('executor_id')
    if executor_id and str(executor_id) != str(ADMIN_ID):
        try:
            subject = target_order.get('subject', 'Не указан')
            work_type = target_order.get('work_type', 'Не указан').replace('work_type_', '')
            executor_offer = target_order.get('executor_offers', {})
            if isinstance(executor_offer, list):
                executor_offer = executor_offer[0] if executor_offer else {}
            work_price = 0
            try:
                work_price = float(executor_offer.get('price', 0) or 0)
            except Exception:
                work_price = 0
            executor_text = (
                f"✅ Клиент принял работу по заказу!\n"
                f"Предмет: {subject}\n"
                f"Тип работы: {work_type}\n\n"
                f"Заработано: {work_price} ₽"
            )
            await bot.send_message(
                executor_id,
                executor_text
            )
        except Exception:
            pass

    await callback.answer()
@router.message(ClientRevision.waiting_for_revision_comment)
async def process_revision_comment(message: Message, state: FSMContext):
    data = await state.get_data()
    order_id = data.get('revision_order_id')
    revision_comment = message.text
    orders = get_all_orders()
    target_order = next((o for o in orders if o.get('order_id') == order_id), None)
    if not target_order:
        await message.answer("Ошибка: заказ не найден.")
        await state.clear()
        return
    # Сохраняем комментарий в заказ
    target_order['revision_comment'] = revision_comment
    target_order['status'] = "На доработке"
    with open("orders.json", "w", encoding="utf-8") as f:
        json.dump(orders, f, ensure_ascii=False, indent=4)
    # --- Формируем красивое уведомление ---
    subject = target_order.get('subject', 'Не указан')
    work_type_raw = target_order.get('work_type', 'Не указан')
    work_type = work_type_raw.replace('work_type_', '') if isinstance(work_type_raw, str) and work_type_raw.startswith('work_type_') else work_type_raw
    from shared import ADMIN_ID
    executor_id = target_order.get('executor_id')
    # --- Сообщение для администратора ---
    if executor_id and str(executor_id) != str(ADMIN_ID):
        admin_text = (
            f"✍️ Клиент отправил комментарий к доработке по заказу №{order_id}:\n"
            f"Предмет: {subject}\n"
            f"Тип работы: {work_type}\n\n"
            f"Сообщение: {revision_comment}\n"
            f"Статус перешел в доработку, исполнителю отправлено сообщение\n\n"
            f"Ожидаем доработку от исполнителя"
        )
        admin_keyboard = None
    else:
        admin_text = (
            f"✍️ Клиент отправил комментарий к доработке по заказу №{order_id}:\n"
            f"Предмет: {subject}\n"
            f"Тип работы: {work_type}\n\n"
            f"Сообщение: {revision_comment}"
        )
        admin_keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📃 Перейти к заказу", callback_data=f"admin_view_order_{order_id}")]
        ])
    # Уведомляем администратора
    try:
        await bot.send_message(ADMIN_ID, admin_text, reply_markup=admin_keyboard)
    except Exception:
        pass
    # --- Сообщение для исполнителя ---
    if executor_id and str(executor_id) != str(ADMIN_ID):
        executor_text = (
            f"✍️ Клиент отправил комментарий к доработке по заказу №{order_id}:\n"
            f"Предмет: {subject}\n"
            f"Тип работы: {work_type}\n\n"
            f"Сообщение: {revision_comment}"
        )
        executor_keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📃 Перейти к заказу", callback_data=f"executor_view_order_{order_id}")]
        ])
        try:
            await bot.send_message(
                executor_id,
                executor_text,
                reply_markup=executor_keyboard
            )
        except Exception:
            pass
    await message.answer("✅ Ваш комментарий отправлен. Ожидайте доработки.")
    await state.clear()

async def send_order_files_to_user(user_id: int, order_data: dict, with_details: bool = True):
    """Отправляет все файлы из заказа указанному пользователю."""
    if with_details:
        details_text = await build_summary_text(order_data)
        await bot.send_message(user_id, "<b>Детали заказа:</b>\n\n" + details_text, parse_mode="HTML")

    async def send_file(file_data, caption):
        if not file_data: return
        if file_data['type'] == 'photo':
            await bot.send_photo(user_id, file_data['id'], caption=caption)
        else:
            await bot.send_document(user_id, file_data['id'], caption=caption)

    await send_file(order_data.get('guidelines_file'), "📄 Методичка")
    
    if order_data.get('task_file'):
        await send_file(order_data.get('task_file'), "📑 Задание")
    elif order_data.get('task_text'):
        await bot.send_message(user_id, f"📑 Текст задания:\n\n{order_data['task_text']}")
    
    await send_file(order_data.get('example_file'), "📄 Пример работы")
# --- Вспомогательная функция для получения полного имени пользователя ---
def get_full_name(user_or_dict):
    if isinstance(user_or_dict, dict):
        first = user_or_dict.get('first_name', '')
        last = user_or_dict.get('last_name', '')
    else:
        first = getattr(user_or_dict, 'first_name', '')
        last = getattr(user_or_dict, 'last_name', '')
    full = f"{first} {last}".strip()
    return full if full else "Без имени"
# --- Логика исполнителя ---
@executor_router.callback_query(F.data.startswith("executor_accept_"))
async def executor_accept_handler(callback: CallbackQuery, state: FSMContext):
    order_id = int(callback.data.split("_")[-1])
    orders = get_all_orders()
    target_order = None
    for o in orders:
        if o.get("order_id") == order_id:
            # Не назначаем executor_id!
            target_order = o
            break
    if not target_order:
        await callback.answer("Это предложение уже неактуально.", show_alert=True)
        return
    await state.set_state(ExecutorResponse.waiting_for_price)
    await state.update_data(order_id=order_id)
    await callback.message.edit_text("Отлично! Укажите вашу цену:", reply_markup=get_price_keyboard(order_id))
    await callback.answer()

@executor_router.callback_query(F.data.startswith("price_"), ExecutorResponse.waiting_for_price)
async def executor_price_handler(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    order_id = data.get('order_id')
    if callback.data == "price_manual":
        await callback.message.edit_text("Пожалуйста, введите цену вручную (только число):", reply_markup=get_price_keyboard(order_id))
        return
    price = callback.data.split("_")[-1]
    await state.update_data(price=price)
    await state.set_state(ExecutorResponse.waiting_for_deadline)
    # Получаем дедлайн от клиента
    from shared import get_all_orders
    orders = get_all_orders()
    order = next((o for o in orders if o.get('order_id') == order_id), None)
    client_deadline = order.get('deadline', 'Не указан') if order else 'Не указан'
    text = f"Цена принята. Теперь укажите срок выполнения: ⏳\nДедлайн: до {client_deadline}"
    await callback.message.edit_text(text, reply_markup=get_deadline_keyboard())
    await callback.answer()

@executor_router.message(ExecutorResponse.waiting_for_price)
async def executor_price_manual_handler(message: Message, state: FSMContext):
    data = await state.get_data()
    order_id = data.get('order_id')
    if not message.text.isdigit():
        await message.answer("Пожалуйста, введите только число.", reply_markup=get_price_keyboard(order_id))
        return
    await state.update_data(price=message.text)
    await state.set_state(ExecutorResponse.waiting_for_deadline)
    await message.answer("Цена принята. Теперь укажите срок выполнения:", reply_markup=get_deadline_keyboard())

@executor_router.callback_query(F.data.startswith("deadline_"), ExecutorResponse.waiting_for_deadline)
async def executor_deadline_handler(callback: CallbackQuery, state: FSMContext):
    if callback.data == "deadline_manual":
        await callback.message.edit_text("Пожалуйста, введите срок выполнения вручную(в днях):")
        return
    deadline = callback.data.split("_", 1)[-1]
    await state.update_data(deadline=deadline)
    await state.set_state(ExecutorResponse.waiting_for_comment)
    await callback.message.edit_text("Добавьте комментарий к заказу (или пропустите этот шаг):", reply_markup=get_executor_comment_keyboard())
    await callback.answer()
def get_executor_comment_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➡️ Пропустить", callback_data="skip_executor_comment")]
    ])

@executor_router.message(ExecutorResponse.waiting_for_deadline)
async def executor_deadline_manual_handler(message: Message, state: FSMContext):
    await state.update_data(deadline=message.text)
    await state.set_state(ExecutorResponse.waiting_for_comment)
    await message.answer("Добавьте комментарий к заказу (или пропустите этот шаг):", reply_markup=get_executor_comment_keyboard())

# --- После ввода комментария показываем карточку подтверждения ---
@executor_router.message(ExecutorResponse.waiting_for_comment)
async def executor_comment_handler(message: Message, state: FSMContext):
    await state.update_data(executor_comment=message.text)
    fsm_data = await state.get_data()
    order_id = fsm_data.get('order_id')
    price = fsm_data.get('price', '—')
    deadline = fsm_data.get('deadline', '—')
    comment = fsm_data.get('executor_comment', '')
    # Если выбран 'До дедлайна', подставляем срок сдачи от клиента
    if str(deadline).strip().lower() == 'до дедлайна':
        from shared import get_all_orders
        orders = get_all_orders()
        order = next((o for o in orders if o.get('order_id') == order_id), None)
        deadline_str = order.get('deadline', 'Не указан') if order else 'Не указан'
    else:
        def _pluralize_days(val):
            try:
                n = int(val)
                if 11 <= n % 100 <= 14:
                    return f"{n} дней"
                elif n % 10 == 1:
                    return f"{n} день"
                elif 2 <= n % 10 <= 4:
                    return f"{n} дня"
                else:
                    return f"{n} дней"
            except Exception:
                return str(val)
        deadline_str = _pluralize_days(deadline)
    text = f"<b>❗️ Проверьте ваши условия:</b>\n\n" \
           f"<b>🏷 Цена:</b> {price} ₽\n\n" \
           f"<b>🗓 Срок:</b> {deadline_str}\n\n" \
           f"<b>💬 Комментарий:</b> {comment or 'Нет'}"
    await state.set_state(ExecutorResponse.waiting_for_confirm)
    await message.answer(text, parse_mode="HTML", reply_markup=get_executor_final_confirm_keyboard(order_id))

@executor_router.callback_query(F.data == "skip_executor_comment", ExecutorResponse.waiting_for_comment)
async def executor_skip_comment_handler(callback: CallbackQuery, state: FSMContext):
    await state.update_data(executor_comment="")
    fsm_data = await state.get_data()
    order_id = fsm_data.get('order_id')
    price = fsm_data.get('price', '—')
    deadline = fsm_data.get('deadline', '—')
    comment = ''
    # Если выбран 'До дедлайна', подставляем срок сдачи от клиента
    if str(deadline).strip().lower() == 'до дедлайна':
       
        orders = get_all_orders()
        order = next((o for o in orders if o.get('order_id') == order_id), None)
        deadline_str = order.get('deadline', 'Не указан') if order else 'Не указан'
    else:
        def _pluralize_days(val):
            try:
                n = int(val)
                if 11 <= n % 100 <= 14:
                    return f"{n} дней"
                elif n % 10 == 1:
                    return f"{n} день"
                elif 2 <= n % 10 <= 4:
                    return f"{n} дня"
                else:
                    return f"{n} дней"
            except Exception:
                return str(val)
        deadline_str = _pluralize_days(deadline)
    text = f"<b>❗️ Проверьте ваши условия:</b>\n\n" \
           f"<b>🏷 Цена:</b> {price} ₽\n\n" \
           f"<b>🗓 Срок:</b> {deadline_str}\n\n" \
           f"<b>💬 Комментарий:</b> Нет"
    await state.set_state(ExecutorResponse.waiting_for_confirm)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=get_executor_final_confirm_keyboard(order_id))
    await callback.answer()

# --- Обработчик кнопки 'Отправить' ---
@executor_router.callback_query(F.data.startswith("executor_send_offer:"), ExecutorResponse.waiting_for_confirm)
async def executor_send_offer_handler(callback: CallbackQuery, state: FSMContext):
    fsm_data = await state.get_data()
    # Назначаем executor_id и executor_offer только здесь
    order_id = fsm_data['order_id']
    price = fsm_data['price']
    deadline = fsm_data['deadline']
    executor_comment = fsm_data.get('executor_comment', '')
    orders = get_all_orders()
    for order in orders:
        if order.get("order_id") == order_id:
            order['status'] = "Ожидает подтверждения"
            # --- Новый блок: добавляем оффер в список ---
            offers = order.get('executor_offers', [])
            # Проверяем, есть ли уже оффер от этого исполнителя
            found = False
            for i, offer in enumerate(offers):
                if offer.get('executor_id') == callback.from_user.id:
                    offers[i] = {
                        'price': price,
                        'deadline': deadline,
                        'executor_id': callback.from_user.id,
                        'executor_username': callback.from_user.username,
                        'executor_full_name': get_full_name(callback.from_user),
                        'executor_comment': executor_comment
                    }
                    found = True
                    break
            if not found:
                offers.append({
                    'price': price,
                    'deadline': deadline,
                    'executor_id': callback.from_user.id,
                    'executor_username': callback.from_user.username,
                    'executor_full_name': get_full_name(callback.from_user),
                    'executor_comment': executor_comment
                })
            order['executor_offers'] = offers
            break
    with open("orders.json", "w", encoding="utf-8") as f:
        json.dump(orders, f, ensure_ascii=False, indent=4)
    await send_offer_to_admin(callback.from_user, fsm_data)
    await callback.message.edit_text("✅ Ваши условия отправлены администратору. Ожидайте подтверждения.")
    await state.clear()
    await callback.answer()



@executor_router.message(ExecutorResponse.waiting_for_comment)
async def executor_comment_handler(message: Message, state: FSMContext):
    await state.update_data(executor_comment=message.text)
    fsm_data = await state.get_data()
    await send_offer_to_admin(message.from_user, fsm_data)
    await message.answer("✅ Ваши условия отправлены администратору. Ожидайте подтверждения.")
    await state.clear()

@executor_router.callback_query(F.data == "skip_executor_comment", ExecutorResponse.waiting_for_comment)
async def executor_skip_comment_handler(callback: CallbackQuery, state: FSMContext):
    await state.update_data(executor_comment="")
    fsm_data = await state.get_data()
    await send_offer_to_admin(callback.from_user, fsm_data)
    await callback.message.edit_text("✅ Ваши условия отправлены администратору. Ожидайте подтверждения.")
    await state.clear()
    await callback.answer()



# --- Новая логика админа для утверждения ---

@admin_router.callback_query(F.data.startswith("final_change_price_"))
async def admin_change_price_start(callback: CallbackQuery, state: FSMContext):
    order_id = int(callback.data.split("_")[-1])
    await state.set_state(AdminApproval.waiting_for_new_price)
    await state.update_data(order_id=order_id, message_id=callback.message.message_id)
    await callback.message.edit_text("Введите новую цену (только число):")
    await callback.answer()

@admin_router.message(AdminApproval.waiting_for_new_price)
async def admin_process_new_price(message: Message, state: FSMContext):
    if not message.text.isdigit():
        await message.answer("Неверный формат. Введите только число.")
        return

    new_admin_price = int(message.text)
    fsm_data = await state.get_data()
    order_id = fsm_data.get('order_id')
    message_id = fsm_data.get('message_id')

    # Обновляем JSON
    orders = get_all_orders()
    executor_full_name = ''
    executor_deadline = ''
    executor_price = None
    for order in orders:
        if order.get("order_id") == order_id:
            # --- Исправление: поддержка executor_offers как списка и dict ---
            offer = order.get('executor_offers')
            if isinstance(offer, list):
                if offer:
                    offer = offer[0]
                else:
                    await message.answer("Нет оффера для изменения цены.")
                    return
            elif not offer:
                offers = order.get('executor_offers', [])
                if offers:
                    offer = offers[0]
                else:
                    await message.answer("Нет оффера для изменения цены.")
                    return
            executor_price = int(offer.get('price', 0))
            offer['admin_price'] = new_admin_price
            executor_full_name = offer.get('executor_full_name', 'Без имени')
            executor_deadline = offer.get('deadline', 'N/A')
            break
    with open("orders.json", "w", encoding="utf-8") as f:
        json.dump(orders, f, ensure_ascii=False, indent=4)
    # Срок: если 'До дедлайна', подставляем срок клиента
    if str(executor_deadline).strip().lower() == 'до дедлайна':
        for order in orders:
            if order.get("order_id") == order_id:
                executor_deadline_str = order.get('deadline', 'Не указан')
                break
        else:
            executor_deadline_str = 'Не указан'
    else:
        executor_deadline_str = pluralize_days(executor_deadline)
    # Итоговая цена
    if new_admin_price == 0:
        total_price = executor_price
    else:
        total_price = new_admin_price
    admin_notification = f"""
✅ Исполнитель {executor_full_name} готов взяться за заказ №{order_id}

<b>Предложенные условия (цена изменена):</b>\n
💰 <b>Цена от исполнителя:</b> {executor_price} ₽\n
💼 <b>Моя цена:</b> {new_admin_price} ₽\n
🧮 <b>Итоговая цена:</b> {total_price} ₽\n
⏳ <b>Срок:</b> до {executor_deadline_str}
"""
    await bot.edit_message_text(
        admin_notification, 
        chat_id=message.chat.id,
        message_id=message_id,
        parse_mode="HTML",
        reply_markup=get_admin_final_approval_keyboard(order_id, total_price, show_materials_button=False)
    )
    await message.delete() # Удаляем сообщение с новой ценой от админа
    await state.clear()


@admin_router.callback_query(F.data.startswith("final_approve_"))
async def admin_final_approve(callback: CallbackQuery, state: FSMContext):
    parts = callback.data.split("_")
    order_id = int(parts[2])
    price = int(parts[3])
    executor_id = int(parts[4]) if len(parts) > 4 else None
    orders = get_all_orders()
    target_order = None
    for order in orders:
        if order.get("order_id") == order_id:
            order['status'] = "Ожидает оплаты"
            order['final_price'] = price
            # --- Новый блок: переносим выбранный оффер в executor_offer, остальные удаляем ---
            if executor_id is not None and order.get('executor_offers'):
                # Найти выбранный оффер
                selected_offer = None
                for offer in order['executor_offers']:
                    if offer.get('executor_id') == executor_id:
                        selected_offer = offer
                        break
                if selected_offer:
                    order['executor_offers'] = selected_offer
                    order['executor_id'] = executor_id
                # Удаляем executor_offers полностью
                if 'executor_offers' in order:
                    del order['executor_offers']
            target_order = order
            break
    if not target_order:
        await callback.answer("Ошибка: заказ не найден", show_alert=True)
        return
    with open("orders.json", "w", encoding="utf-8") as f:
        json.dump(orders, f, ensure_ascii=False, indent=4)
    # Уведомление клиенту
    customer_id = target_order.get('user_id')
    if customer_id:
        offer = target_order.get('executor_offers', {})
        if isinstance(offer, list):
            offer = offer[0] if offer else {}
        deadline = offer.get('deadline') or target_order.get('deadline', '')
        if str(deadline).strip().lower() == 'до дедлайна':
            deadline_str = target_order.get('deadline', 'Не указан')
        else:
            deadline_str = pluralize_days(deadline) if isinstance(deadline, str) and deadline.isdigit() else deadline
        work_type = target_order.get('work_type', 'N/A').replace('work_type_', '')
        subject = target_order.get('subject', 'Не указан')
        customer_text = f"""
✅ Ваша заявка по предмету \"{subject}\"\nТип работы: {work_type}\nДедлайн: до {deadline_str}

<b>Итоговая стоимость:</b> {price} ₽.\n<b>Срок:</b> до {deadline_str}
"""
        payment_button = InlineKeyboardButton(text="💳 Оплатить", callback_data=f"pay_{order_id}")
        keyboard = InlineKeyboardMarkup(inline_keyboard=[[payment_button]])
        try:
            await bot.send_message(customer_id, customer_text, parse_mode="HTML", reply_markup=keyboard)
        except Exception:
            await callback.message.answer(f"⚠️ Не удалось уведомить клиента {customer_id}")
    # Уведомление только выбранному исполнителю
    executor_offer = target_order.get('executor_offers', {})
    if isinstance(executor_offer, list):
        executor_offer = executor_offer[0] if executor_offer else {}
    executor_id = executor_offer.get('executor_id')
    if executor_id:
        try:
            subject = target_order.get('subject', 'Не указан')
            await bot.send_message(executor_id, f'✅ Администратор утвердил ваши условия по заказу.\nПредмет: "{subject}"\nОжидаем оплату от клиента.')
        except Exception:
            await callback.message.answer(f"⚠️ Не удалось уведомить исполнителя {executor_id}")
    await callback.message.edit_text(f"✅ Предложение по заказу №{order_id} на сумму {price} ₽ отправлено клиенту. Ожидаем оплату...")
    await callback.answer()


@admin_router.callback_query(F.data.startswith("final_reject_"))
async def admin_final_reject(callback: CallbackQuery, state: FSMContext):
    # Парсим callback данные: final_reject_{order_id} или final_reject_{order_id}_{executor_id}
    parts = callback.data.split("_")
    if len(parts) >= 3:
        order_id = int(parts[2])  # final_reject_{order_id}
        executor_id_from_callback = int(parts[3]) if len(parts) > 3 else None  # final_reject_{order_id}_{executor_id}
    else:
        await callback.answer("Ошибка: неверный формат callback данных", show_alert=True)
        return
    
    orders = get_all_orders()
    target_order = None
    executor_id = None
    
    # Находим заказ и получаем executor_id, но НЕ изменяем данные
    for order in orders:
        if order.get("order_id") == order_id:
            target_order = order
            # Используем executor_id из callback, если есть, иначе получаем из executor_offers
            if executor_id_from_callback:
                executor_id = executor_id_from_callback
            else:
                executor_offers = order.get('executor_offers')
                if isinstance(executor_offers, dict):
                    executor_id = executor_offers.get('executor_id')
                elif isinstance(executor_offers, list) and executor_offers:
                    executor_id = executor_offers[0].get('executor_id')
            break
    
    if not target_order:
        await callback.answer("Ошибка: заказ не найден", show_alert=True)
        return
    
    # Уведомляем исполнителя (если есть)
    if executor_id:
        try:
            await bot.send_message(executor_id, f"❌ Администратор отклонил ваши условия по заказу №{order_id}.")
        except Exception:
            pass # Не критично
    
    # Отправляем сообщение администратору
    await callback.message.edit_text(f"❌ Вы отклонили предложение исполнителя по заказу №{order_id}. Заказ снова в поиске.")
    
    # ТОЛЬКО ПОСЛЕ отправки сообщения изменяем статус и очищаем данные
    target_order['status'] = "Рассматривается"
    target_order.pop('executor_id', None)
    
    # Обрабатываем executor_offers
    if 'executor_offers' in target_order:
        executor_offers = target_order['executor_offers']
        if executor_id_from_callback and isinstance(executor_offers, list):
            # Удаляем конкретный оффер по executor_id
            target_order['executor_offers'] = [
                offer for offer in executor_offers 
                if offer.get('executor_id') != executor_id_from_callback
            ]
            # Если офферов не осталось, удаляем поле полностью
            if not target_order['executor_offers']:
                target_order.pop('executor_offers', None)
        else:
            # Удаляем все офферы (старое поведение)
            target_order.pop('executor_offers', None)
    
    # Сохраняем изменения
    with open("orders.json", "w", encoding="utf-8") as f:
        json.dump(orders, f, ensure_ascii=False, indent=4)
    
    await callback.answer()

@admin_router.callback_query(F.data.startswith("admin_approve_work_"))
async def admin_approve_work_handler(callback: CallbackQuery, state: FSMContext):
    order_id = int(callback.data.split('_')[-1])
    orders = get_all_orders()
    target_order = next((o for o in orders if o.get('order_id') == order_id), None)

    if not target_order or 'submitted_work' not in target_order:
        await callback.answer("Работа не найдена или была отозвана.", show_alert=True)
        return

    # Меняем статус
    target_order['status'] = "Утверждено администратором"
    with open("orders.json", "w", encoding="utf-8") as f:
        json.dump(orders, f, ensure_ascii=False, indent=4)

    # Отправляем клиенту
    customer_id = target_order.get('user_id')
    submitted_work = target_order.get('submitted_work')
    submitted_at = target_order.get('submitted_at', 'неизвестно')

    if customer_id and submitted_work:
        try:
            subject = target_order.get('subject', 'Не указан')
            work_type = target_order.get('work_type', 'Не указан').replace('work_type_', '')
            caption = f"✅ Ваша работа по заказу готова!\nПредмет: {subject}\nТип работы: {work_type}\nДата выполнения: {submitted_at}"
            keyboard = get_client_work_approval_keyboard(order_id)
            await bot.send_document(
                chat_id=customer_id,
                document=submitted_work['file_id'],
                caption=caption,
                reply_markup=keyboard
            )
            # Удаляем сообщение с файлом работы у админа, если оно есть
            try:
                await callback.message.delete()
            except Exception:
                pass
        except Exception as e:
            await callback.message.edit_text(f"⚠️ Не удалось отправить работу клиенту {customer_id}. Ошибка: {e}")
            return
    else:
        await callback.message.edit_text("Файл работы не найден для отправки клиенту.")
        return

    # Сообщение админу о результате
    try:
        await bot.send_message(callback.from_user.id, "✅ Заказ утвержден! и отправлен заказчику")
    except Exception:
        pass
    await callback.answer()

# --- Обработчик сообщения с комментарием по доработке ---
@admin_router.message(AdminRevision.waiting_for_revision_comment)
async def admin_revision_comment_handler(message: Message, state: FSMContext):
    data = await state.get_data()
    order_id = data.get('order_id')
    revision_comment = message.text
    orders = get_all_orders()
    target_order = next((o for o in orders if o.get('order_id') == order_id), None)
    if not target_order:
        await message.answer("Ошибка: заказ не найден.")
        await state.clear()
        return
    target_order['revision_comment'] = revision_comment
    target_order['status'] = "На доработке"
    with open("orders.json", "w", encoding="utf-8") as f:
        json.dump(orders, f, ensure_ascii=False, indent=4)
    executor_id = target_order.get('executor_id')
    subject = target_order.get('subject', 'Не указан')
    work_type = target_order.get('work_type', 'Не указан').replace('work_type_', '')
    text = (
        f"❗️ Требуется доработка по заказу\n"
        f"Предмет: {subject}\n"
        f"Тип работы: {work_type}\n"
        f"Комментарий по работе: {revision_comment}"
    )
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Перейти к заказу", callback_data=f"executor_view_order_{order_id}")]
    ])
    if executor_id:
        try:
            await bot.send_message(executor_id, text, reply_markup=keyboard)
        except Exception as e:
            await message.answer(f"Не удалось отправить исполнителю: {e}")
    else:
        await message.answer("Ошибка: не найден исполнитель для этого заказа.")
    await message.answer("✅Комментарий отправлен исполнителю\n\n📃Заказ переведён в статус 'На доработке'.")
    await state.clear()
# --- Обработчики команд и главного меню ---

# --- Обработчик кнопки 'Отправить на доработку' ---
@admin_router.callback_query(F.data.startswith("admin_reject_work_"))
async def admin_reject_work_handler(callback: CallbackQuery, state: FSMContext):
    order_id = int(callback.data.split("_")[-1])
    orders = get_all_orders()
    target_order = next((o for o in orders if o.get('order_id') == order_id), None)
    if not target_order:
        await callback.answer("Заказ не найден.", show_alert=True)
        return
    target_order['status'] = "На доработке"
    with open("orders.json", "w", encoding="utf-8") as f:
        json.dump(orders, f, ensure_ascii=False, indent=4)
    await state.set_state(AdminRevision.waiting_for_revision_comment)
    await state.update_data(order_id=order_id)
    await bot.send_message(callback.from_user.id, "✍️ Напишите комментарий по доработке для исполнителя:")
    await callback.answer()



@router.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    from shared import ADMIN_ID
    if message.from_user.id == int(ADMIN_ID):
        await message.answer(
            "Добро пожаловать в меню администратора!",
            reply_markup=get_admin_keyboard()
        )
        return
    # Если исполнитель — показываем его меню
    if is_executor(message.from_user.id):
        await message.answer(
            "👋 Добро пожаловать в меню исполнителя!",
            reply_markup=get_executor_menu_keyboard()
        )
        return
    # Проверяем, есть ли телефон в профиле (или в базе FSM)
    data = await state.get_data()
    phone = data.get("phone_number")
    if not phone:
        await state.set_state("waiting_for_phone")
        await message.answer(
            "🙏 Пожалуйста, поделитесь своим номером телефона для использования бота.",
            reply_markup=get_phone_request_keyboard()
        )
        return
    # Если телефон уже есть — только тогда приветствие и меню
    await message.answer(
        "👋 Здравствуйте! Я бот для приема заявок. Воспользуйтесь кнопками ниже.",
        reply_markup=get_main_reply_keyboard()
    )
@router.message(StateFilter("waiting_for_phone"), F.contact)
async def process_phone_number(message: Message, state: FSMContext):
    phone = message.contact.phone_number
    await state.update_data(phone_number=phone)
    # Сохраняем в users.json
    save_user_phone(message.from_user.id, phone)
    await state.clear()
    await message.answer(
        "🎉 Спасибо! Теперь вы можете оформлять заявки.",
        reply_markup=get_main_reply_keyboard()
    )
@router.message(F.text == "❓ Помощь")
async def txt_help(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(
        'ℹ️ Нажмите кнопку "🆕 Новая заявка" и следуйте инструкциям, чтобы создать новую заявку.'
    )

@router.message(F.text == "👨‍💻 Связаться с администратором")
async def txt_contact_admin(message: Message, state: FSMContext):
    await state.clear()
    await state.set_state(AdminContact.waiting_for_message)
    await message.answer(
        "✍️ Напишите ваше сообщение, и я отправлю его администратору.",
        reply_markup=get_back_to_main_menu_keyboard()
    )

@router.message(AdminContact.waiting_for_message)
async def universal_admin_message_handler(message: Message, state: FSMContext):
    if message.from_user.id == int(ADMIN_ID):
        # Это ответ администратора клиенту или исполнителю
        data = await state.get_data()
        user_id = data.get("reply_user_id")
        reply_msg_id = data.get("reply_msg_id")
        if user_id:
            # Если это исполнитель, отправляем с меню исполнителя
            if is_executor(user_id):
                await bot.send_message(user_id, f"💬 Ответ от администратора:\n\n{message.text}", reply_markup=get_executor_menu_keyboard())
            else:
                await bot.send_message(user_id, f"💬 Ответ от администратора:\n\n{message.text}")
            try:
                await bot.delete_message(ADMIN_ID, reply_msg_id)
            except:
                pass
            await message.answer("Сообщение отправлено и удалено из списка.")
        else:
            await message.answer("Ошибка: не найден пользователь для ответа.")
        await state.clear()
    else:
        # Это пользователь пишет админу
        admin_msg = await bot.send_message(
            ADMIN_ID,
            f"📩 Новое сообщение от пользователя {get_full_name(message.from_user)} (ID: {message.from_user.id}):\n\n"
            f'"{message.text}"',
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [
                    InlineKeyboardButton(text="Ответить", callback_data=f"admin_reply_user:{message.from_user.id}"),
                    InlineKeyboardButton(text="Удалить сообщение", callback_data="admin_delete_user_msg")
                ]
            ])
        )
        await state.clear()
        await state.update_data(
            last_user_msg_text=message.text,
            last_user_id=message.from_user.id
        )
        await message.answer(
            "✅ Ваше сообщение успешно отправлено администратору!",
            reply_markup=get_main_reply_keyboard()
        )

@router.callback_query(F.data == "back_to_main_menu")
async def back_to_main_menu_handler(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text("❌Действие отменено")
    await callback.answer()


# --- Просмотр заявок ---

def get_user_orders(user_id: int) -> list:
    """Читает orders.json и возвращает список заявок для конкретного user_id."""
    file_path = "orders.json"
    if not os.path.exists(file_path) or os.path.getsize(file_path) == 0:
        return []
    with open(file_path, "r", encoding="utf-8") as f:
        try:
            all_orders = json.load(f)
        except json.JSONDecodeError:
            return []
    
    if not isinstance(all_orders, list):
        return []

    user_orders = [order for order in all_orders if isinstance(order, dict) and order.get('user_id') == user_id]
    return user_orders

async def show_my_orders(message_or_callback: types.Message | types.CallbackQuery):
    user_id = message_or_callback.from_user.id
    orders = get_user_orders(user_id)
    # --- ДОБАВЛЕНО: если пользователь — исполнитель, показываем заявки, где он назначен исполнителем ---
    if is_executor(user_id):
        all_orders = get_all_orders()
        executor_orders = [o for o in all_orders if o.get('executor_id') == user_id]
        # Добавляем только те, которых нет в orders (чтобы не дублировать)
        for eo in executor_orders:
            if eo not in orders:
                orders.append(eo)
    draft_orders_exist = any(o.get('status') == "Редактируется" for o in orders)

    if not orders:
        text = "У вас пока нет заявок."
        keyboard = None
    else:
        text = "Вот ваши заявки:"
        if draft_orders_exist:
            text = "У вас есть незавершенная заявка. Выберите ее, чтобы продолжить.\n\n" + text
        keyboard_buttons = []
        # Показываем последние 10 заявок, чтобы не перегружать интерфейс
        for order in reversed(orders[-10:]): 
            order_id = order['order_id']
            order_status = order.get('status', 'N/A')
            emoji = STATUS_EMOJI_MAP.get(order_status, "📄")
            work_type_raw = order.get('work_type', 'Заявка')
            work_type = work_type_raw.replace('work_type_', '')
            button_text = f"{emoji} Заявка  №{order_id} {work_type}  | {order_status}"
            keyboard_buttons.append([InlineKeyboardButton(text=button_text, callback_data=f"view_order_{order_id}")])
        keyboard = InlineKeyboardMarkup(inline_keyboard=keyboard_buttons)

    if isinstance(message_or_callback, types.Message):
        await message_or_callback.answer(text, reply_markup=keyboard)
    else:
        try:
            await message_or_callback.message.edit_text(text, reply_markup=keyboard)
            await message_or_callback.answer()
        except:
            await message_or_callback.message.answer(text, reply_markup=keyboard)
            await message_or_callback.answer()

@router.message(F.text == "📂 Мои заявки")
async def my_orders_handler(message: Message, state: FSMContext):
    await state.clear()
    await show_my_orders(message)

@router.callback_query(F.data == "my_orders_list")
async def back_to_my_orders_list_handler(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await show_my_orders(callback)


@router.callback_query(F.data.startswith("view_order_"))
async def view_order_handler(callback: CallbackQuery, state: FSMContext):
    try:
        order_id = int(callback.data.split("_")[2])
    except (IndexError, ValueError):
        await callback.answer("Ошибка: неверный ID заявки.", show_alert=True)
        return
    user_id = callback.from_user.id
    orders = get_user_orders(user_id)
    target_order = next((order for order in orders if order['order_id'] == order_id), None)
    if not target_order:
        await callback.message.edit_text("Не удалось найти эту заявку или у вас нет к ней доступа.")
        await callback.answer()
        return
    # Если заявка в статусе "Редактируется", возвращаем пользователя к подтверждению
    if target_order.get('status') == "Редактируется":
        await state.set_data(target_order)
        await state.set_state(OrderState.confirmation)
        summary_text = await build_summary_text(target_order)
        await callback.message.edit_text(
            text=summary_text, 
            reply_markup=get_confirmation_keyboard(), 
            parse_mode="HTML"
        )
        await callback.answer()
        return
    # --- Новый блок для "Утверждено администратором" ---
    if target_order.get('status') == "Утверждено администратором":
        submitted_work = target_order.get('submitted_work')
        submitted_at = target_order.get('submitted_at', 'неизвестно')
        if submitted_work:
            subject = target_order.get('subject', 'Не указан')
            work_type = target_order.get('work_type', 'Не указан').replace('work_type_', '')
            caption = f"✅ Ваша работа по заказу №{order_id} готова!\nПредмет: {subject}\nТип работы: {work_type}\nДата выполнения: {submitted_at}"
            keyboard = get_client_work_approval_keyboard(order_id)
            try:
                await callback.message.delete()
            except:
                pass
            await bot.send_document(
                chat_id=callback.from_user.id,
                document=submitted_work['file_id'],
                caption=caption,    
                reply_markup=keyboard
            )
        else:
            await callback.message.edit_text("Ошибка: файл с работой не найден.")
        await callback.answer()
        return
    status = target_order.get('status', 'Не определен')
    status_text = f"{STATUS_EMOJI_MAP.get(status, '📄')} {status}"
    details_text = f"""
<b>Детали заявки №{target_order['order_id']}</b>

<b>Статус:</b> {status_text}

<b>Группа:</b> {target_order.get('group_name', 'Не указано')}
<b>Университет:</b> {target_order.get('university_name', 'Не указано')}
<b>Преподаватель:</b> {target_order.get('teacher_name', 'Не указано')}
<b>Номер зачетки:</b> {target_order.get('gradebook', 'Не указано')}
<b>Предмет:</b> {target_order.get('subject', 'Не указан')}
<b>Тип работы:</b> {target_order.get('work_type', 'Не указан')}
<b>Методичка:</b> {'✅ Да' if target_order.get('has_guidelines') else '❌ Нет'}
<b>Задание:</b> {'✅ Прикреплено' if target_order.get('task_file') or target_order.get('task_text') else '❌ Нет'}
<b>Пример работы:</b> {'✅ Да' if target_order.get('has_example') else '❌ Нет'}
<b>Дата сдачи:</b> {target_order.get('deadline', 'Не указана')}
<b>Комментарий:</b> {target_order.get('comments', 'Нет')}
"""
    # Добавляем блок доработки, если статус 'На доработке'
    if status == "На доработке" and target_order.get('revision_comment'):
        details_text += f"\n<b>Доработка:</b> {target_order.get('revision_comment')}"
    # --- Кнопки ---
    keyboard = get_user_order_keyboard(order_id, status)
    # Если это админ, он исполнитель и статус 'В работе' — добавляем кнопку 'Сдать работ
    if status == "В работе":
        # Если исполнитель — админ, показываем отдельную секцию
        if str(target_order.get('executor_id')) == str(ADMIN_ID):
            full_name = get_full_name(target_order)
            header = f"Детали заказа №{order_id} от клиента ({full_name})\n"
            if target_order.get('creation_date'):
                header += f"Дата создания: {target_order.get('creation_date')}\n"
            group = target_order.get("group_name", "Не указана")
            university = target_order.get("university_name", "Не указан")
            teacher = target_order.get("teacher_name", "Не указан")
            gradebook = target_order.get("gradebook", "Не указан")
            subject = target_order.get("subject", "Не указан")
            work_type_key = target_order.get("work_type", "N/A").replace("work_type_", "")
            work_type_str = work_type_key if work_type_key != 'other' else target_order.get('work_type_other_name', 'Другое')
            guidelines = '✅ Да' if target_order.get('has_guidelines') else '❌ Нет'
            task = '✅ Прикреплено' if target_order.get('task_file') or target_order.get('task_text') else '❌ Нет'
            example = '✅ Да' if target_order.get('has_example') else '❌ Нет'
            deadline = target_order.get('deadline', 'Не указана')
            executor_id = target_order.get('executor_id')
            executor_full_name = 'я'
            details_text = f"{header}\nГруппа: {group}\nВУЗ: {university}\nПреподаватель: {teacher}\nНомер зачетки: {gradebook}\nПредмет: {subject}\nТип работы: {work_type_str}\nМетодичка: {guidelines}\nЗадание: {task}\nПример: {example}\nДедлайн: {deadline}\nИсполнитель: {executor_full_name} - {executor_id}"
            # --- Кнопки ---
            buttons = [[InlineKeyboardButton(text="✅ Сдать работу", callback_data=f"admin_admin_submit_work_{order_id}")]]
            has_files = target_order.get('guidelines_file') or target_order.get('task_file') or target_order.get('task_text') or target_order.get('example_file')
            if has_files:
                buttons.append([InlineKeyboardButton(text="📎 Посмотреть материалы заказа", callback_data=f"admin_show_materials:{order_id}")])
            buttons.append([InlineKeyboardButton(text="⬅️ Вернуться к заявкам", callback_data="admin_back")])
            keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
            try:
                await callback.message.edit_text(details_text, reply_markup=keyboard)
            except Exception:
                await callback.message.answer(details_text, reply_markup=keyboard)
            await callback.answer()
            return
    await callback.message.edit_text(details_text, parse_mode="HTML", reply_markup=keyboard)
    await callback.answer()
# --- Процесс создания нового заказа ---

@router.message(F.text == "🆕 Новая заявка")
async def start_new_order(message: Message, state: FSMContext):
    await state.clear()
    user_id = message.from_user.id
    profile = get_user_profile(user_id)
    required_fields = ["group_name"]
    if all(profile.get(f) for f in required_fields):
        await state.update_data(
            first_name=profile.get("first_name", message.from_user.first_name),
            last_name=profile.get("last_name", message.from_user.last_name or ""),
            phone_number=profile.get("phone_number", ""),
            group_name=profile["group_name"],
            university_name=profile.get("university_name", "")
        )
        text = (
            f"Ваши данные:\n"
            f"ФИО: {profile.get('first_name', message.from_user.first_name)} {profile.get('last_name', message.from_user.last_name or '')}\n"
            f"Группа: {profile['group_name']}\n"
            f"Университет: {profile.get('university_name', 'Не указан')}\n"
            f"Телефон: {profile.get('phone_number', 'Не указан')}\n\n"
            "Вы можете изменить их или продолжить."
        )
        await state.set_state("profile_confirm")
        await message.answer(text, reply_markup=get_profile_confirm_keyboard())
    else:
        await state.set_state(OrderState.group_name)
        await message.answer(
            "📝 Начнем создание заявки. \n\nПожалуйста, укажите название вашей группы.",
            reply_markup=get_skip_keyboard("group_name")
        )

@router.message(OrderState.group_name)
async def process_group_name(message: Message, state: FSMContext):
    await state.update_data(group_name=message.text)
    await state.set_state(OrderState.university_name)
    await message.answer("🏫 Отлично! Теперь введите название вашего университета.", reply_markup=get_back_keyboard())

@router.callback_query(OrderState.group_name, F.data == "skip_group_name")
async def skip_group_name(callback: CallbackQuery, state: FSMContext):
    await state.update_data(group_name="Не указано")
    await state.set_state(OrderState.university_name)
    await callback.message.edit_text("🏫 Отлично! Теперь введите название вашего университета.", reply_markup=get_back_keyboard())
    await callback.answer()

def get_teacher_name_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➡️ Пропустить", callback_data="skip_teacher_name")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="back")]
    ])

@router.message(OrderState.university_name)
async def process_university_name(message: Message, state: FSMContext):
    await state.update_data(university_name=message.text)
    await state.set_state(OrderState.teacher_name)
    await message.answer("👨‍🏫 Введите ФИО преподавателя:", reply_markup=get_teacher_name_keyboard())

@router.message(OrderState.teacher_name)
async def process_teacher_name(message: Message, state: FSMContext):
    await state.update_data(teacher_name=message.text)
    data = await state.get_data()
    user_id = message.from_user.id
    profile = get_user_profile(user_id)
    gradebook = data.get("gradebook") or profile.get("gradebook")
    if gradebook:
        await state.update_data(gradebook=gradebook)
        await state.set_state(OrderState.subject)
        await message.answer("📚 Введите название предмета:", reply_markup=get_back_keyboard())
    else:
        await state.set_state(OrderState.gradebook)
        await message.answer("📒 Введите номер зачетки или вариант (или пропустите):", reply_markup=get_gradebook_keyboard())

@router.callback_query(OrderState.teacher_name, F.data == "skip_teacher_name")
async def skip_teacher_name(callback: CallbackQuery, state: FSMContext):
    await state.update_data(teacher_name="Не указан")
    data = await state.get_data()
    user_id = callback.from_user.id
    profile = get_user_profile(user_id)
    gradebook = data.get("gradebook") or profile.get("gradebook")
    if gradebook:
        await state.update_data(gradebook=gradebook)
        await state.set_state(OrderState.subject)
        await callback.message.edit_text("📚 Введите название предмета:", reply_markup=get_back_keyboard())
    else:
        await state.set_state(OrderState.gradebook)
        await callback.message.edit_text("📒 Введите номер зачетки или вариант (или пропустите):", reply_markup=get_gradebook_keyboard())
    await callback.answer()

@router.message(OrderState.gradebook)
async def process_gradebook(message: Message, state: FSMContext):
    # Больше не проверяем формат, разрешаем любой текст
    await state.update_data(gradebook=message.text.strip())
    await state.set_state(OrderState.subject)
    await message.answer("📚 Напишите название предмета")

@router.callback_query(OrderState.gradebook, F.data == "skip_gradebook")
async def skip_gradebook(callback: CallbackQuery, state: FSMContext):
    await state.update_data(gradebook="Не указано")
    await state.set_state(OrderState.subject)
    await callback.message.edit_text("📚 Напишите название предмета")
    await callback.answer()

@router.message(OrderState.subject)
async def process_subject_input(message: Message, state: FSMContext):
    await state.update_data(subject=message.text)
    await state.set_state(OrderState.work_type)
    await message.answer("📝 Выберите тип работы:", reply_markup=get_work_type_keyboard())

@router.callback_query(OrderState.work_type, F.data.startswith("work_type_"))
async def process_work_type_choice(callback: CallbackQuery, state: FSMContext):
    work_type = callback.data
    
    if work_type == "work_type_other":
        await state.set_state(OrderState.work_type_other)
        await callback.message.edit_text("Пожалуйста, введите тип работы вручную.", reply_markup=get_back_keyboard())
    else:
        await state.update_data(work_type=work_type)
        await state.set_state(OrderState.guidelines_choice)
        await callback.message.edit_text("📄 У вас есть методичка?", reply_markup=get_yes_no_keyboard("guidelines"))
    await callback.answer()

@router.message(OrderState.work_type_other)
async def process_work_type_other(message: Message, state: FSMContext):
    await state.update_data(work_type=message.text)
    await state.set_state(OrderState.guidelines_choice)
    # Просто отправляем новое сообщение вместо редактирования
    await message.answer("📄 У вас есть методичка?", reply_markup=get_yes_no_keyboard("guidelines"))


@router.callback_query(OrderState.guidelines_choice, F.data.startswith("guidelines_"))
async def process_guidelines_choice(callback: CallbackQuery, state: FSMContext):
    choice = callback.data.split("_")[1]
    if choice == "yes":
        await state.update_data(has_guidelines=True)
        await state.set_state(OrderState.guidelines_upload)
        await callback.message.edit_text("Пожалуйста, загрузите файл с методичкой (pdf, docx, png, jpeg).", reply_markup=get_back_keyboard())
    else:
        await state.update_data(has_guidelines=False, guidelines_file=None)
        await state.set_state(OrderState.task_upload)
        await callback.message.edit_text("Понял. Теперь, пожалуйста, загрузите файл с заданием (pdf, docx, png, jpeg) или просто опишите его текстом.", reply_markup=get_back_keyboard())
    await callback.answer()

@router.callback_query(StateFilter("profile_confirm"), F.data == "edit_profile")
async def edit_profile_handler(callback: CallbackQuery, state: FSMContext):
    await state.set_state("edit_full_name")
    await callback.message.edit_text("✏️ Напишите ваше Имя и Фамилию (например: Иван Иванов):")

@router.callback_query(StateFilter("profile_confirm"), F.data == "profile_next")
async def profile_next_handler(callback: CallbackQuery, state: FSMContext):
    await state.set_state(OrderState.teacher_name)
    await callback.message.edit_text("👨‍🏫 Введите ФИО преподавателя:", reply_markup=get_teacher_name_keyboard())

@router.message(StateFilter("edit_full_name"))
async def edit_full_name(message: Message, state: FSMContext):
    # Разделяем на имя и фамилию
    parts = message.text.strip().split(maxsplit=1)
    first_name = parts[0] if len(parts) > 0 else ""
    last_name = parts[1] if len(parts) > 1 else ""
    await state.update_data(first_name=first_name, last_name=last_name)
    await state.set_state("edit_group_name")
    await message.answer("📝 Введите вашу группу:")

@router.message(StateFilter("edit_group_name"))
async def edit_group_name(message: Message, state: FSMContext):
    await state.update_data(group_name=message.text)
    await state.set_state("edit_gradebook")
    await message.answer("📒 Введите номер зачетки (например: 24-15251):")

@router.message(StateFilter("edit_gradebook"))
async def edit_gradebook(message: Message, state: FSMContext):
    await state.update_data(gradebook=message.text)
    await state.set_state("edit_university_name")
    await message.answer("🏫 Введите название вашего университета:")

@router.message(StateFilter("edit_university_name"))
async def edit_university_name(message: Message, state: FSMContext):
    await state.update_data(university_name=message.text)
    data = await state.get_data()
# Получаем номер телефона из FSM или users.json
    phone_number = data.get('phone_number')
    if not phone_number:
        users_file = "users.json"
        if os.path.exists(users_file):
            with open(users_file, "r", encoding="utf-8") as f:
                try:
                    users = json.load(f)
                    phone_number = users.get(str(message.from_user.id), {}).get("phone_number", "")
                except Exception:
                    phone_number = ""
    data['phone_number'] = phone_number
    save_user_profile(
        message.from_user.id,
        {
            "first_name": data.get("first_name"),
            "last_name": data.get("last_name"),
            "phone_number": data.get("phone_number", ""),
            "group_name": data.get("group_name"),
            "university_name": data.get("university_name"),
        }
    )
    text = (
        f"Ваши новые данные:\n"
        f"ФИО: {data.get('first_name')} {data.get('last_name')}\n"
        f"Группа: {data.get('group_name')}\n"
        f"Зачетка: {data.get('gradebook')}\n"
        f"ВУЗ: {data.get('university_name')}\n\n"
        "Продолжить оформление заявки?"
    )
    await state.set_state("profile_confirm")
    await message.answer(text, reply_markup=get_profile_confirm_keyboard())

@router.message(OrderState.guidelines_upload, F.document | F.photo)
async def process_guidelines_upload(message: Message, state: FSMContext):
    # Проверка документа
    if message.document:
        ext = os.path.splitext(message.document.file_name)[-1][1:].lower()
        if ext not in ALLOWED_EXTENSIONS:
            await message.answer("❌ Разрешены только файлы: pdf, docx, png, jpeg, jpg. Попробуйте еще раз.")
            return
        if message.document.file_size > MAX_FILE_SIZE:
            await message.answer("❌ Файл слишком большой. Максимальный размер — 15 МБ.")
            return
        guidelines_file = {'id': message.document.file_id, 'type': 'document'}
    else:
        # Проверка фото
        photo = message.photo[-1]
        if photo.file_size > MAX_FILE_SIZE:
            await message.answer("❌ Фото слишком большое. Максимальный размер — 15 МБ.")
            return
        guidelines_file = {'id': photo.file_id, 'type': 'photo'}
    await state.update_data(guidelines_file=guidelines_file)
    await state.set_state(OrderState.task_upload)
    await message.answer("✅ Методичка принята. Теперь, пожалуйста, загрузите файл с заданием (pdf, docx, png, jpeg) или просто опишите его текстом.", reply_markup=get_back_keyboard())


@router.message(OrderState.task_upload, F.text | F.document | F.photo)
async def process_task_upload(message: Message, state: FSMContext):
    if message.text:
        await state.update_data(task_text=message.text, task_file=None)
    else:
        if message.document:
            ext = os.path.splitext(message.document.file_name)[-1][1:].lower()
            if ext not in ALLOWED_EXTENSIONS:
                await message.answer("❌ Разрешены только файлы: pdf, docx, png, jpeg, jpg. Попробуйте еще раз.")
                return
            if message.document.file_size > MAX_FILE_SIZE:
                await message.answer("❌ Файл слишком большой. Максимальный размер — 15 МБ.")
                return
            task_file = {'id': message.document.file_id, 'type': 'document'}
        else:
            photo = message.photo[-1]
            if photo.file_size > MAX_FILE_SIZE:
                await message.answer("❌ Фото слишком большое. Максимальный размер — 15 МБ.")
                return
            task_file = {'id': photo.file_id, 'type': 'photo'}
        await state.update_data(task_file=task_file, task_text=None)
    await state.set_state(OrderState.example_choice)
    await message.answer("📑 Задание принято. У вас есть пример работы?", reply_markup=get_yes_no_keyboard("example"))


@router.callback_query(OrderState.example_choice, F.data.startswith("example_"))
async def process_example_choice(callback: CallbackQuery, state: FSMContext):
    choice = callback.data.split("_")[-1]
    if choice == "yes":
        await state.update_data(has_example=True)
        await state.set_state(OrderState.example_upload)
        await callback.message.edit_text("Пожалуйста, загрузите файл с примером (pdf, docx, pgn, jpeg).", reply_markup=get_back_keyboard())
    else: 
        await state.update_data(has_example=False, example_file=None)
        await state.set_state(OrderState.deadline)
        await callback.message.edit_text("🗓️ Укажите дату сдачи в формате ДД.ММ.ГГГГ.", reply_markup=get_back_keyboard())
    await callback.answer()


@router.message(OrderState.example_upload, F.document | F.photo)
async def process_example_upload(message: Message, state: FSMContext):
    if message.document:
        ext = os.path.splitext(message.document.file_name)[-1][1:].lower()
        if ext not in ALLOWED_EXTENSIONS:
            await message.answer("❌ Разрешены только файлы: pdf, docx, png, jpeg, jpg. Попробуйте еще раз.")
            return
        if message.document.file_size > MAX_FILE_SIZE:
            await message.answer("❌ Файл слишком большой. Максимальный размер — 15 МБ.")
            return
        example_file = {'id': message.document.file_id, 'type': 'document'}
    else:
        photo = message.photo[-1]
        if photo.file_size > MAX_FILE_SIZE:
            await message.answer("❌ Фото слишком большое. Максимальный размер — 15 МБ.")
            return
        example_file = {'id': photo.file_id, 'type': 'photo'}
    await state.update_data(example_file=example_file)
    await state.set_state(OrderState.deadline)
    await message.answer("🗓️ Пример принят. Укажите дату сдачи в формате ДД.ММ.ГГГГ.", reply_markup=get_back_keyboard())

@router.message(OrderState.deadline)
async def process_deadline(message: Message, state: FSMContext):
    try:
        # Простая проверка формата
        datetime.strptime(message.text, "%d.%m.%Y")
        await state.update_data(deadline=message.text)
        await state.set_state(OrderState.comments)
        await message.answer(
            "💬 Отлично. Теперь введите ваши комментарии по работе (например, по оформлению, преподавателю и т.д.) или нажмите 'Пропустить'", 
            reply_markup=get_skip_comment_keyboard()
        )
    except ValueError:
        await message.answer("❌ Неверный формат даты. Пожалуйста, введите дату в формате ДД.ММ.ГГГГ.", reply_markup=get_back_keyboard())

@router.callback_query(F.data == "skip_comment", OrderState.comments)
async def skip_comment_handler(callback: CallbackQuery, state: FSMContext):
    await state.update_data(comments="Нет")
    data = await state.get_data()
    # Не сохраняем черновик! Просто показываем подтверждение
    summary_text = await build_summary_text(data)
    await state.set_state(OrderState.confirmation)
    await callback.message.edit_text(summary_text, reply_markup=get_confirmation_keyboard(), parse_mode="HTML")
    await callback.answer()

@router.message(OrderState.comments)
async def process_comments(message: Message, state: FSMContext):
    await state.update_data(comments=message.text)
    data = await state.get_data()
    # Не сохраняем черновик! Просто показываем подтверждение
    summary_text = await build_summary_text(data)
    await state.set_state(OrderState.confirmation)
    await message.answer(text=summary_text, reply_markup=get_confirmation_keyboard(), parse_mode="HTML")


async def build_summary_text(data: dict) -> str:
    """Строит текст с итоговой информацией о заявке."""
    group = data.get("group_name", "Не указана")
    university = data.get("university_name", "Не указан")
    teacher = data.get("teacher_name", "Не указан")
    gradebook = data.get("gradebook", "Не указан")
    subject = data.get("subject", "Не указан")
    work_type_key = data.get("work_type", "N/A").replace("work_type_", "")
    work_type_str = work_type_key if work_type_key != 'other' else data.get('work_type_other_name', 'Другое')
    guidelines = '✅ Да' if data.get('has_guidelines') else '❌ Нет'
    task = '✅ Прикреплено' if data.get('task_file') or data.get('task_text') else '❌ Нет'
    example = '✅ Да' if data.get('has_example') else '❌ Нет'
    deadline = data.get('deadline', 'Не указана')
    comments = data.get('comments', 'Нет')
    status = data.get('status', '')
    summary_text = f"""
<b>Группа:</b> {group}
<b>ВУЗ:</b> {university}
<b>Преподаватель:</b> {teacher}
<b>Номер зачетки:</b> {gradebook}
<b>Предмет:</b> {subject}
<b>Тип работы:</b> {work_type_str}
<b>Методичка:</b> {guidelines}
<b>Задание:</b> {task}
<b>Пример:</b> {example}
<b>Дедлайн:</b> {deadline}
"""
    # Добавляем поле "Исполнитель" только для статуса "В работе"
    if status == "В работе":
        executor_id = data.get('executor_id')
        # Сначала пробуем взять first_name и last_name исполнителя
        executor_first_name = data.get('first_name')
        executor_last_name = data.get('last_name')
        executor_name = None
        if executor_first_name or executor_last_name:
            executor_name = f"{executor_first_name or ''} {executor_last_name or ''}".strip()
        elif data.get('executor_full_name'):
            executor_name = data['executor_full_name']
        elif executor_id:
            try:
                executors = get_executors_list()
                for ex in executors:
                    if str(ex.get('id')) == str(executor_id):
                        executor_name = ex.get('name')
                        break
            except Exception:
                pass
        if executor_name:
            summary_text += f"<b>Исполнитель:</b> {executor_name} (ID {executor_id})\n"
        elif executor_id:
            summary_text += f"<b>Исполнитель:</b> ID {executor_id}\n"
        else:
            summary_text += f"<b>Исполнитель:</b> —\n"
    if comments:
        return f"{summary_text}\n<b>Комментарии:</b> {comments}"
    return summary_text

async def build_short_summary_text(data: dict) -> str:
    """Формирует короткий текст-сводку по заявке для админа/исполнителей."""
    work_type = data.get("work_type", "Тип не указан").replace("type_", "").capitalize()
    if work_type == "Other":
        work_type = data.get("work_type_other_name", "Другое")

    subject = data.get("subject", "Не указан")
    deadline = data.get("deadline", "Не указан")
    text = (f"<b>Тип работы:</b> {work_type}\n"
            f"<b>Предмет:</b> {subject}\n"
            f"<b>Срок:</b> до {deadline}")
    return text

# --- Подтверждение и сохранение заказа ---

# --- ДОБАВЛЕНО: Получение максимального order_id из Google Sheets ---
def get_max_order_id_from_gsheet():
    creds = Credentials.from_service_account_file("google-credentials.json", scopes=["https://www.googleapis.com/auth/spreadsheets"])
    gc = gspread.authorize(creds)
    sh = gc.open_by_key(GOOGLE_SHEET_ID)
    worksheet = sh.sheet1
    # Получаем все значения первого столбца (order_id)
    order_ids = worksheet.col_values(1)
    # Пропускаем заголовок, если он есть
    order_ids = [x for x in order_ids if x.strip() and x.strip().lower() != 'номер заказа']
    order_ids = [x for x in order_ids if x.isdigit()]
    if not order_ids:
        return 0
    return max(int(x) for x in order_ids)

async def save_or_update_order(order_data: dict) -> int:
    file_path = "orders.json"
    orders = []
    if os.path.exists(file_path) and os.path.getsize(file_path) > 0:
        with open(file_path, "r", encoding="utf-8") as f:
            try:
                orders = json.load(f)
            except json.JSONDecodeError:
                orders = []
    order_id_to_process = order_data.get("order_id")
    user_id_to_process = order_data.get("user_id")
    status_to_process = order_data.get("status")
    # Если заявка подтверждается ("Рассматривается"), удаляем все черновики с этим order_id и user_id
    if status_to_process == "Рассматривается" and order_id_to_process and user_id_to_process:
        orders = [o for o in orders if not (
            o.get("order_id") == order_id_to_process and o.get("user_id") == user_id_to_process and o.get("status") == "Редактируется"
        )]
    if order_id_to_process: # Обновляем существующую
        found = False
        for i, order in enumerate(orders):
            if order.get("order_id") == order_id_to_process:
                orders[i] = order_data
                found = True
                break
        if not found: # Если по какой-то причине не нашли, добавляем как новую
            max_json_id = orders[-1]['order_id'] if orders else 0
            max_gsheet_id = get_max_order_id_from_gsheet()
            order_id_to_process = max(max_json_id, max_gsheet_id) + 1
            order_data["order_id"] = order_id_to_process
            orders.append(order_data)
    else: # Создаем новую
        max_json_id = orders[-1]['order_id'] if orders else 0
        max_gsheet_id = get_max_order_id_from_gsheet()
        order_id_to_process = max(max_json_id, max_gsheet_id) + 1
        order_data["order_id"] = order_id_to_process
        orders.append(order_data)
    with open(file_path, "w", encoding="utf-8") as f:
        json.dump(orders, f, ensure_ascii=False, indent=4)
    # Save to SQLite if it's a new order or update
    try:
        with sqlite3.connect('student.db', timeout=10.0) as conn:
            c = conn.cursor()
            c.execute('''INSERT OR REPLACE INTO students (user_id, first_name, last_name, phone_number, group_name)
                         VALUES (?, ?, ?, ?, ?)''',
                      (order_data['user_id'], order_data.get('first_name', ''), order_data.get('last_name', ''),
                       order_data.get('phone_number', ''), order_data.get('group_name', '')))
            conn.commit()
    except sqlite3.Error as e:
        logging.error(f"Ошибка при работе с базой данных SQLite: {e}")
        # Продолжаем выполнение, так как основные данные уже сохранены в JSON
    return order_id_to_process

@router.callback_query(OrderState.confirmation, F.data == "confirm_order")
async def process_confirm_order(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    data['status'] = "Рассматривается"
    data['user_id'] = callback.from_user.id
    # Получаем номер телефона из FSM или users.json
    # Получаем номер телефона из FSM или users.json
    phone_number = data.get('phone_number')
    if not phone_number:
        users_file = "users.json"
        if os.path.exists(users_file):
            with open(users_file, "r", encoding="utf-8") as f:
                try:
                    users = json.load(f)
                    phone_number = users.get(str(callback.from_user.id), {}).get("phone_number", "")
                except Exception:
                    phone_number = ""
    data['phone_number'] = phone_number
    save_user_profile(
        callback.from_user.id,
        {
            "first_name": data.get("first_name", callback.from_user.first_name),
            "last_name": data.get("last_name", callback.from_user.last_name or ""),
            "phone_number": data.get("phone_number", ""),
            "group_name": data.get("group_name", ""),
            "university_name": data.get("university_name", ""),
        }
    )
    
    data['username'] = callback.from_user.username or "N/A"
    data['first_name'] = callback.from_user.first_name
    data['last_name'] = callback.from_user.last_name or ""
    data['creation_date'] = datetime.now().strftime("%d.%m.%Y %H:%M")
    order_id = await save_or_update_order(data)
    # Формируем единое сообщение для админа с кнопками
    summary = await build_summary_text(data)
    full_name = f"{data.get('first_name', '')} {data.get('last_name', '')}".strip()
    admin_text = f"🔥 Новая заявка {order_id} от клиента ({full_name})\n\n{summary}"
    admin_keyboard = get_admin_order_keyboard(data, show_materials_button=True)
    await bot.send_message(ADMIN_ID, admin_text, parse_mode="HTML", reply_markup=admin_keyboard)
    # Рассылка исполнителям (оставляем как было)
    if EXECUTOR_IDS:
        short_summary = await build_short_summary_text(data)
        notification_text = f"📢 Появился новый заказ {order_id}\n\n" + short_summary
        for executor_id in EXECUTOR_IDS:
            try:
                await bot.send_message(executor_id, notification_text, parse_mode="HTML")
            except Exception as e:
                print(f"Failed to send notification to executor {executor_id}: {e}")
    await callback.message.edit_text("✅ Ваша заявка успешно отправлена, ожидайте отклика!", reply_markup=None)
    await state.clear()
    await callback.answer()

@router.callback_query(OrderState.confirmation, F.data == "cancel_order")
async def process_cancel_order(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    order_id = data.get("order_id")
    user_id = callback.from_user.id
    file_path = "orders.json"
    orders = []
    if os.path.exists(file_path) and os.path.getsize(file_path) > 0:
        with open(file_path, "r", encoding="utf-8") as f:
            try:
                orders = json.load(f)
            except json.JSONDecodeError:
                orders = []
    # Удаляем заявку пользователя с этим order_id
    new_orders = [o for o in orders if not (str(o.get("order_id")) == str(order_id) and o.get("user_id") == user_id)]
    with open(file_path, "w", encoding="utf-8") as f:
        json.dump(new_orders, f, ensure_ascii=False, indent=4)
    await state.clear()
    await callback.message.edit_text("❌ Заявка отменена и удалена.")
    await callback.answer()

# Дополнительный обработчик для confirm_order без фильтра состояния
@router.callback_query(F.data == "confirm_order")
async def process_confirm_order_fallback(callback: CallbackQuery, state: FSMContext):
    current_state = await state.get_state()
    
    # Если пользователь в правильном состоянии, то основной обработчик должен был сработать
    # Этот обработчик срабатывает только если пользователь не в состоянии OrderState.confirmation
    if current_state != OrderState.confirmation:
        # Проверяем, есть ли данные в FSM
        data = await state.get_data()
        if data and data.get('subject'):  # Если есть данные заявки
            # Устанавливаем правильное состояние и показываем подтверждение
            await state.set_state(OrderState.confirmation)
            summary_text = await build_summary_text(data)
            await callback.message.edit_text(
                text=summary_text, 
                reply_markup=get_confirmation_keyboard(), 
                parse_mode="HTML"
            )
            await callback.answer("Теперь вы можете подтвердить заявку.")
        else:
            # Если нет данных, предлагаем создать новую заявку
            await state.clear()
            await callback.message.edit_text(
                "❌ Данные заявки не найдены. Пожалуйста, создайте новую заявку.",
                reply_markup=None
            )
            await callback.answer()
    else:
        # Если состояние правильное, но основной обработчик не сработал - ошибка
        await callback.answer("Произошла ошибка при обработке. Попробуйте еще раз.", show_alert=True)

# Дополнительный обработчик для cancel_order без фильтра состояния
@router.callback_query(F.data == "cancel_order")
async def process_cancel_order_fallback(callback: CallbackQuery, state: FSMContext):
    current_state = await state.get_state()
    
    if current_state != OrderState.confirmation:
        # Если пользователь не в состоянии подтверждения, просто очищаем состояние
        await state.clear()
        await callback.message.edit_text("❌ Заявка отменена.")
        await callback.answer()
    else:
        # Если состояние правильное, но основной обработчик не сработал - ошибка
        await callback.answer("Произошла ошибка при обработке. Попробуйте еще раз.", show_alert=True)

@router.callback_query(OrderState.confirmation, F.data == "contact_admin_in_order")
async def process_contact_admin_in_order(callback: CallbackQuery, state: FSMContext):
    await state.set_state(AdminContact.waiting_for_message)
    await callback.message.edit_text("✍️ Напишите ваше сообщение, и я отправлю его администратору.")
    await callback.answer()


# --- Обработчик кнопки "Назад" ---
@router.callback_query(F.data == "back", StateFilter(OrderState))
async def process_back_button(callback: CallbackQuery, state: FSMContext):
    current_state_str = await state.get_state()

    async def go_to_group_name(s: FSMContext):
        await s.set_state(OrderState.group_name)
        await callback.message.edit_text("📝 Пожалуйста, укажите название вашей группы.")
    
    async def go_to_university_name(s: FSMContext):
        await s.set_state(OrderState.university_name)
        await callback.message.edit_text("🏫 Введите название вашего университета.", reply_markup=get_back_keyboard())

    async def go_to_work_type(s: FSMContext):
        await s.set_state(OrderState.work_type)
        await callback.message.edit_text("📘 Выберите тип работы:", reply_markup=get_work_type_keyboard())

    async def go_to_guidelines_choice(s: FSMContext):
        await s.set_state(OrderState.guidelines_choice)
        await callback.message.edit_text("📄 У вас есть методичка?", reply_markup=get_yes_no_keyboard("guidelines"))
    
    async def go_to_task_upload(s: FSMContext):
        await s.set_state(OrderState.task_upload)
        await callback.message.edit_text("Понял. Теперь, пожалуйста, загрузите файл с заданием (pdf, docx, png, jpeg) или просто опишите его текстом.", reply_markup=get_back_keyboard())

    async def go_to_example_choice(s: FSMContext):
        await s.set_state(OrderState.example_choice)
        await callback.message.edit_text("📑 Задание принято. У вас есть пример работы?", reply_markup=get_yes_no_keyboard("example"))

    async def go_to_deadline(s: FSMContext):
        await s.set_state(OrderState.deadline)
        await callback.message.edit_text("🗓️ Укажите дату сдачи в формате ДД.ММ.ГГГГ.", reply_markup=get_back_keyboard())

    async def go_to_comments(s: FSMContext):
        await s.set_state(OrderState.comments)
        await callback.message.edit_text("💬 Введите ваши комментарии по работе.", reply_markup=get_back_keyboard())

    back_transitions = {
        OrderState.university_name: go_to_group_name,
        OrderState.work_type: go_to_university_name,
        OrderState.work_type_other: go_to_work_type,
        OrderState.guidelines_choice: go_to_work_type,
        OrderState.guidelines_upload: go_to_guidelines_choice,
        OrderState.task_upload: go_to_guidelines_choice,
        OrderState.example_choice: go_to_task_upload,
        OrderState.example_upload: go_to_example_choice,
        OrderState.deadline: go_to_example_choice,
        OrderState.comments: go_to_deadline,
        OrderState.confirmation: go_to_comments,
    }
    
    if current_state_str in back_transitions:
        await back_transitions[current_state_str](state)
    else: # Если это первый шаг (group_name), то возвращаться некуда
        await state.clear()
        await callback.message.edit_text("❌ Заявка отменена. Вы вернулись в главное меню.")

    await callback.answer()


async def send_offer_to_admin(user, fsm_data):
    order_id = fsm_data['order_id']
    price = fsm_data['price']
    executor_comment = fsm_data.get('executor_comment', '')
    orders = get_all_orders()
    subject = 'Не указан'
    for order in orders:
        if order.get("order_id") == order_id:
            subject = order.get('subject', 'Не указан')
            break
    admin_notification = f"""
    ✅ Исполнитель {get_full_name(user)} (ID: {user.id}) готов взяться за заказ по предмету \"{subject}\"\n<b>Предложенные условия:</b>\n💰 <b>Цена:</b> {price} ₽\n⏳ <b>Срок:</b> {fsm_data['deadline']}\n💬 <b>Комментарий исполнителя:</b> {executor_comment or 'Нет'}
    """
    await bot.send_message(
        ADMIN_ID, 
        admin_notification, 
        parse_mode="HTML",
        reply_markup=get_admin_final_approval_keyboard(order_id, price)
    )


@admin_router.callback_query(F.data.startswith("admin_show_materials:"))
async def admin_show_materials_handler(callback: CallbackQuery, state: FSMContext):
    order_id = callback.data.split(":", 1)[1]
    orders = get_all_orders()
    order = next((o for o in orders if str(o['order_id']) == str(order_id)), None)
    if not order:
        await callback.answer("Заказ не найден.", show_alert=True)
        return
    material_buttons = []
    if order.get('guidelines_file'):
        material_buttons.append([InlineKeyboardButton(text="Методичка", callback_data=f"admin_material_guidelines:{order_id}")])
    if order.get('task_file') or order.get('task_text'):
        material_buttons.append([InlineKeyboardButton(text="Задание", callback_data=f"admin_material_task:{order_id}")])
    if order.get('example_file'):
        material_buttons.append([InlineKeyboardButton(text="Пример работы", callback_data=f"admin_material_example:{order_id}")])
    material_buttons.append([InlineKeyboardButton(text="⬅️ Назад", callback_data=f"admin_view_order_{order_id}")])
    keyboard = InlineKeyboardMarkup(inline_keyboard=material_buttons)
    await callback.message.edit_text("Выберите материал для просмотра:", reply_markup=keyboard)
    await callback.answer()

@admin_router.callback_query(F.data.startswith("admin_hide_materials:"))
async def admin_hide_materials_handler(callback: CallbackQuery, state: FSMContext):
    order_id = callback.data.split(":", 1)[1]
    orders = get_all_orders()
    order = next((o for o in orders if str(o['order_id']) == str(order_id)), None)
    if not order:
        await callback.answer("Заказ не найден.", show_alert=True)
        return
    status = order.get('status')
    # Новый блок: если статус 'Ожидает подтверждения' и есть executor_offer — возвращаем шаблон предложения исполнителя
    if status == 'Ожидает подтверждения' and 'executor_offers' in order:
        offer = order['executor_offers']
        executor_full_name = offer.get('executor_full_name', 'Без имени')
        price = offer.get('price')
        deadline = offer.get('deadline', 'N/A')
        executor_comment = offer.get('executor_comment', 'Нет')
        subject = order.get('subject', 'Не указан')
        if str(deadline).strip().lower() == 'до дедлайна':
            deadline_str = order.get('deadline', 'Не указан')
        else:
    
            deadline_str = pluralize_days(deadline)
        admin_notification = f"""✅ Исполнитель {executor_full_name} готов взяться за заказ по предмету \"{subject}\"\n    \n<b>Предложенные условия:</b>\n💰 <b>Цена:</b> {price} ₽\n⏳ <b>Срок:</b> {deadline_str}\n💬 <b>Комментарий исполнителя:</b> {executor_comment or 'Нет'}"""
        keyboard = get_admin_final_approval_keyboard(int(order_id), price)
        await callback.message.edit_text(admin_notification, parse_mode="HTML", reply_markup=keyboard)
        await callback.answer()
        return
    # Старое поведение для остальных статусов
    details_text = await build_summary_text(order)
    details_text = f"<b>Детали заказа {order_id} от {get_full_name(order)}</b>\n\n" + details_text
    keyboard = get_admin_order_keyboard(order, show_materials_button=True)
    await callback.message.edit_text(details_text, reply_markup=keyboard, parse_mode="HTML")
    await callback.answer()

@admin_router.callback_query(F.data.startswith("admin_material_guidelines:"))
async def admin_material_guidelines_handler(callback: CallbackQuery, state: FSMContext):
    order_id = callback.data.split(":", 1)[1]
    orders = get_all_orders()
    order = next((o for o in orders if str(o['order_id']) == str(order_id)), None)
    if not order or not order.get('guidelines_file'):
        await callback.answer("Методичка не найдена.", show_alert=True)
        return
    file = order['guidelines_file']
    if file['type'] == 'photo':
        await bot.send_photo(callback.from_user.id, file['id'], caption="Методичка")
    else:
        await bot.send_document(callback.from_user.id, file['id'], caption="Методичка")
    await callback.answer()

@admin_router.callback_query(F.data.startswith("admin_material_task:"))
async def admin_material_task_handler(callback: CallbackQuery, state: FSMContext):
    order_id = callback.data.split(":", 1)[1]
    orders = get_all_orders()
    order = next((o for o in orders if str(o['order_id']) == str(order_id)), None)
    if not order:
        await callback.answer("Задание не найдено.", show_alert=True)
        return
    if order.get('task_file'):
        file = order['task_file']
        if file['type'] == 'photo':
            await bot.send_photo(callback.from_user.id, file['id'], caption="Задание")
        else:
            await bot.send_document(callback.from_user.id, file['id'], caption="Задание")
    elif order.get('task_text'):
        await bot.send_message(callback.from_user.id, f"Текст задания:\n\n{order['task_text']}")
    await callback.answer()

@admin_router.callback_query(F.data.startswith("admin_material_example:"))
async def admin_material_example_handler(callback: CallbackQuery, state: FSMContext):
    order_id = callback.data.split(":", 1)[1]
    orders = get_all_orders()
    order = next((o for o in orders if str(o['order_id']) == str(order_id)), None)
    if not order or not order.get('example_file'):
        await callback.answer("Пример работы не найден.", show_alert=True)
        return
    file = order['example_file']
    if file['type'] == 'photo':
        await bot.send_photo(callback.from_user.id, file['id'], caption="Пример работы")
    else:
        await bot.send_document(callback.from_user.id, file['id'], caption="Пример работы")
    await callback.answer()

@admin_router.callback_query(F.data.startswith("admin_delete_order:"))
async def admin_delete_order_handler(callback: CallbackQuery, state: FSMContext):
    order_id = callback.data.split(":", 1)[1]
    orders = get_all_orders()
    new_orders = [o for o in orders if str(o['order_id']) != str(order_id)]
    with open("orders.json", "w", encoding="utf-8") as f:
        json.dump(new_orders, f, ensure_ascii=False, indent=4)
    await callback.message.edit_text(f"❌ Заявка {order_id} удалена.")
    await callback.answer()

@admin_router.callback_query(F.data == "admin_orders_list")
async def admin_back_to_orders_list_handler(callback: CallbackQuery, state: FSMContext):
    await show_admin_orders_list(callback.message)
    await callback.answer()
# Просмотр материалов заказа для Исполнителя
@executor_router.callback_query(F.data.startswith("executor_show_materials:"))
async def executor_show_materials_handler(callback: CallbackQuery, state: FSMContext):
    order_id = callback.data.split(":", 1)[1]
    orders = get_all_orders()
    order = next((o for o in orders if str(o['order_id']) == str(order_id)), None)
    if not order:
        await callback.answer("Заказ не найден.", show_alert=True)
        return
    material_buttons = []
    if order.get('guidelines_file'):
        material_buttons.append([InlineKeyboardButton(text="Методичка", callback_data=f"executor_material_guidelines:{order_id}")])
    if order.get('task_file') or order.get('task_text'):
        material_buttons.append([InlineKeyboardButton(text="Задание", callback_data=f"executor_material_task:{order_id}")])
    if order.get('example_file'):
        material_buttons.append([InlineKeyboardButton(text="Пример работы", callback_data=f"executor_material_example:{order_id}")])
    # Кнопка 'Назад' — разная логика для статуса 'В работе'
    if order.get('status') == 'В работе' or order.get('status') == 'На доработке':
        material_buttons.append([InlineKeyboardButton(text="⬅️ Назад", callback_data=f"executor_view_order_{order_id}")])
    else:
        material_buttons.append([InlineKeyboardButton(text="⬅️ Назад", callback_data=f"executor_hide_materials:{order_id}")])
    keyboard = InlineKeyboardMarkup(inline_keyboard=material_buttons)
    await callback.message.edit_text("Выберите материал для просмотра:", reply_markup=keyboard)
    await callback.answer()

@executor_router.callback_query(F.data.startswith("executor_back_to_invite:"), ExecutorResponse.waiting_for_price)
async def executor_back_to_invite_handler(callback: CallbackQuery, state: FSMContext):
    order_id = int(callback.data.split(":")[-1])
    await state.clear()
    from executor_menu import executor_view_order
    from types import SimpleNamespace
    fake_callback = SimpleNamespace(
        from_user=callback.from_user,
        message=callback.message,
        data=f"executor_view_order_{order_id}"
    )
    await executor_view_order(fake_callback, state)
    await callback.answer()

@executor_router.callback_query(F.data.startswith("executor_hide_materials:"))
async def executor_hide_materials_handler(callback: CallbackQuery, state: FSMContext):
    order_id = callback.data.split(":", 1)[1]
    orders = get_all_orders()
    order = next((o for o in orders if str(o['order_id']) == str(order_id)), None)
    if not order:
        await callback.answer("Заказ не найден.", show_alert=True)
        return

    work_type = order.get('work_type', 'N/A').replace('work_type_', '')
    subject = order.get('subject', 'Не указан')
    deadline = order.get('deadline', 'Не указан')
    executor_caption = (
        f"📬 Вам предложен новый заказ по предмету <b>{subject}</b>\n\n"
        f"📝 <b>Тип работы:</b> {work_type}\n"
        f"🗓 <b>Срок сдачи:</b> {deadline}\n\n"
        "Пожалуйста, ознакомьтесь с материалами заявки и примите решение."
    )
    executor_keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📎 Посмотреть материалы заказа", callback_data=f"executor_show_materials:{order_id}")],
        [
            InlineKeyboardButton(text="✅ Готов взяться", callback_data=f"executor_accept_{order_id}"),
            InlineKeyboardButton(text="❌ Отказаться", callback_data=f"executor_refuse_{order_id}")
        ],
    ])
    await callback.message.edit_text(executor_caption, parse_mode="HTML", reply_markup=executor_keyboard)
    await callback.answer()

@executor_router.callback_query(F.data.startswith("executor_material_guidelines:"))
async def executor_material_guidelines_handler(callback: CallbackQuery, state: FSMContext):
    order_id = callback.data.split(":", 1)[1]
    orders = get_all_orders()
    order = next((o for o in orders if str(o['order_id']) == str(order_id)), None)
    if not order or not order.get('guidelines_file'):
        await callback.answer("Методичка не найдена.", show_alert=True)
        return
    file = order['guidelines_file']
    if file['type'] == 'photo':
        await bot.send_photo(callback.from_user.id, file['id'], caption="Методичка")
    else:
        await bot.send_document(callback.from_user.id, file['id'], caption="Методичка")
    await callback.answer()

@executor_router.callback_query(F.data.startswith("executor_material_task:"))
async def executor_material_task_handler(callback: CallbackQuery, state: FSMContext):
    order_id = callback.data.split(":", 1)[1]
    orders = get_all_orders()
    order = next((o for o in orders if str(o['order_id']) == str(order_id)), None)
    if not order:
        await callback.answer("Задание не найдено.", show_alert=True)
        return
    if order.get('task_file'):
        file = order['task_file']
        if file['type'] == 'photo':
            await bot.send_photo(callback.from_user.id, file['id'], caption="Задание")
        else:
            await bot.send_document(callback.from_user.id, file['id'], caption="Задание")
    elif order.get('task_text'):
        await bot.send_message(callback.from_user.id, f"Текст задания:\n\n{order['task_text']}")
    await callback.answer()

@executor_router.callback_query(F.data.startswith("executor_material_example:"))
async def executor_material_example_handler(callback: CallbackQuery, state: FSMContext):
    order_id = callback.data.split(":", 1)[1]
    orders = get_all_orders()
    order = next((o for o in orders if str(o['order_id']) == str(order_id)), None)
    if not order or not order.get('example_file'):
        await callback.answer("Пример работы не найден.", show_alert=True)
        return
    file = order['example_file']
    if file['type'] == 'photo':
        await bot.send_photo(callback.from_user.id, file['id'], caption="Пример работы")
    else:
        await bot.send_document(callback.from_user.id, file['id'], caption="Пример работы")
    await callback.answer()



# --- Админ отвечает пользователю ---
@admin_router.callback_query(F.data.startswith("admin_reply_user:"))
async def admin_reply_user_handler(callback: CallbackQuery, state: FSMContext):
    user_id = int(callback.data.split(":")[1])
    await state.clear()
    await state.update_data(reply_user_id=user_id, reply_msg_id=callback.message.message_id)
    await state.set_state(AdminContact.waiting_for_message)
    await callback.message.edit_text("✍️ Введите ваш ответ пользователю:")
    await callback.answer()

@admin_router.callback_query(F.data == "admin_delete_user_msg")
async def admin_delete_user_msg_handler(callback: CallbackQuery, state: FSMContext):
    try:
        await bot.delete_message(ADMIN_ID, callback.message.message_id)
    except:
        pass
    await callback.answer("Сообщение удалено.")

@admin_router.callback_query(F.data.startswith("admin_save_to_gsheet:"))
async def admin_save_to_gsheet_handler(callback: CallbackQuery, state: FSMContext):
    order_id = callback.data.split(":", 1)[1]
    orders = get_all_orders()
    order = next((o for o in orders if str(o['order_id']) == str(order_id)), None)
    if not order:
        await callback.answer("Заявка не найдена.", show_alert=True)
        return
    if order.get("status") != "Выполнена":
        await callback.answer("Сохранять в таблицу можно только выполненные заявки!", show_alert=True)
        return
    phone_number = order.get("phone_number", "")
    if not phone_number:
        users_file = "users.json"
        if os.path.exists(users_file):
            with open(users_file, "r", encoding="utf-8") as f:
                try:
                    users = json.load(f)
                    phone_number = users.get(str(order.get("user_id")), {}).get("phone_number", "")
                except Exception:
                    phone_number = ""
    # --- Исправление: поддержка executor_offers как списка и dict ---
    executor_offer = order.get("executor_offers", {})
    if isinstance(executor_offer, list):
        executor_offer = executor_offer[0] if executor_offer else {}
    
    # Получаем deadline - сначала из executor_offer, потом из корневого уровня заявки
    exec_deadline = executor_offer.get("deadline", "")
    if not exec_deadline:
        exec_deadline = order.get("deadline", "")
    
    def pluralize_days(val):
        try:
            n = int(val)
            if 11 <= n % 100 <= 14:
                return f"{n} дней"
            elif n % 10 == 1:
                return f"{n} день"
            elif 2 <= n % 10 <= 4:
                return f"{n} дня"
            else:
                return f"{n} дней"
        except Exception:
            return str(val)
    exec_deadline_str = pluralize_days(exec_deadline)
    executor_price = executor_offer.get("price", 0)
    admin_price = executor_offer.get("admin_price")
    if admin_price is None:
        admin_price = order.get("final_price", 0)
    try:
        executor_price_val = float(executor_price)
    except Exception:
        executor_price_val = 0
    try:
        admin_price_val = float(admin_price)
    except Exception:
        admin_price_val = 0
    profit = admin_price_val - executor_price_val
    row = [
        order.get("order_id", ""),
        f"{order.get('first_name', '')} {order.get('last_name', '')}".strip(),
        phone_number,
        order.get("group_name", ""),
        order.get("gradebook", ""),
        "я" if str(order.get("executor_id")) == str(ADMIN_ID) else executor_offer.get("executor_full_name", ""),
        order.get("subject", ""),
        order.get("creation_date", ""),
        exec_deadline_str,  # Срок выполнения
        "" if order.get("status") != "Выполнена" else order.get("submitted_at", ""),  # Дата сдачи
        executor_price,
        admin_price,
        profit,
        order.get("status", "")
    ]
    try:
        creds = Credentials.from_service_account_file("google-credentials.json", scopes=["https://www.googleapis.com/auth/spreadsheets"])
        gc = gspread.authorize(creds)
        sh = gc.open_by_key(GOOGLE_SHEET_ID)
        worksheet = sh.sheet1
        # --- Новый блок: ищем строку с этим order_id ---
        cell = worksheet.find(str(order.get("order_id", "")))
        if cell:
            worksheet.update(f"A{cell.row}:N{cell.row}", [row])
            await callback.answer("Заявка обновлена в Google таблице!", show_alert=True)
        else:
            worksheet.append_row(row, value_input_option="USER_ENTERED")
            await callback.answer("Заявка добавлена в Google таблицу!", show_alert=True)
    except Exception as e:
        await callback.answer(f"Ошибка при сохранении: {e}", show_alert=True)

@admin_router.callback_query(F.data.startswith("admin_broadcast_select_"))
async def admin_broadcast_select_handler(callback: CallbackQuery, state: FSMContext):
    order_id = int(callback.data.split("_")[-1])
    orders = get_all_orders()
    order = next((o for o in orders if o.get('order_id') == order_id), None)
    if not order:
        await callback.answer("Заявка не найдена.", show_alert=True)
        return
    executors = get_executors_list()
    if not executors:
        await callback.answer("Нет исполнителей для рассылки.", show_alert=True)
        return
    # Формируем оффер для рассылки
    work_type = order.get('work_type', 'N/A').replace('work_type_', '')
    subject = order.get('subject', 'Не указан')
    deadline = order.get('deadline', 'Не указан')
    executor_caption = (
        f"📬 Вам предложен новый заказ по предмету <b>{subject}</b>\n\n"
        f"📝 <b>Тип работы:</b> {work_type}\n"
        f"🗓 <b>Срок сдачи:</b> {deadline}\n\n"
        "Пожалуйста, ознакомьтесь с материалами заявки и примите решение."
    )
    executor_keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📎 Посмотреть материалы заказа", callback_data=f"executor_show_materials:{order_id}")],
        [
            InlineKeyboardButton(text="✅ Готов взяться", callback_data=f"executor_accept_{order_id}"),
            InlineKeyboardButton(text="❌ Отказаться", callback_data=f"executor_refuse_{order_id}")
        ],
    ])
    count = 0
    for ex in executors:
        executor_id = ex.get('id')
        if not executor_id:
            continue
        try:
            await bot.send_message(executor_id, executor_caption, parse_mode="HTML", reply_markup=executor_keyboard)
            count += 1
        except Exception as e:
            print(f"Не удалось отправить рассылку исполнителю {executor_id}: {e}")
    # --- Обновляем статус заявки ---
    order['status'] = "Ожидает подтверждения"
    with open("orders.json", "w", encoding="utf-8") as f:
        json.dump(orders, f, ensure_ascii=False, indent=4)
    await callback.answer(f"Рассылка отправлена {count} исполнителям.", show_alert=True)
    await callback.message.edit_text(f"Рассылка по заявке №{order_id} отправлена {count} исполнителям. Статус заявки обновлён.")

async def main():
    init_db()
    # Запуск aiogram-бота
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())









