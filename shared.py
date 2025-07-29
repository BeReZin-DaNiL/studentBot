import os
import json
from aiogram import Bot
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from aiogram.fsm.context import FSMContext

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
    "Отправлен на проверку": "📬",
    "Утверждено администратором": "✅",
    "На доработке": "✍️",
}

ADMIN_ID = os.getenv("ADMIN_ID", "842270366")
BOT_TOKEN = os.getenv("BOT_TOKEN", "7763016986:AAFW4Rwh012_bfh8Jt0E_zaq5abvzenr4bE")
bot = Bot(token=BOT_TOKEN)

def get_all_orders() -> list:
    file_path = "orders.json"
    if not os.path.exists(file_path) or os.path.getsize(file_path) == 0:
        return []
    with open(file_path, "r", encoding="utf-8") as f:
        try:
            all_orders = json.load(f)
        except json.JSONDecodeError:
            return []
    return all_orders if isinstance(all_orders, list) else []

def get_full_name(user_or_dict):
    if isinstance(user_or_dict, dict):
        first = user_or_dict.get('first_name', '')
        last = user_or_dict.get('last_name', '')
    else:
        first = getattr(user_or_dict, 'first_name', '')
        last = getattr(user_or_dict, 'last_name', '')
    full = f"{first} {last}".strip()
    return full if full else "Без имени"

def pluralize_days(n):
    try:
        n = int(n)
    except (ValueError, TypeError):
        return str(n)
    if 11 <= n % 100 <= 14:
        return f"{n} дней"
    elif n % 10 == 1:
        return f"{n} день"
    elif 2 <= n % 10 <= 4:
        return f"{n} дня"
    else:
        return f"{n} дней"

def get_price_keyboard(order_id, for_admin=False):
    buttons = [
        [InlineKeyboardButton(text=f"{i} ₽", callback_data=f"price_{i}") for i in range(500, 2501, 500)],
        [InlineKeyboardButton(text=f"{i} ₽", callback_data=f"price_{i}") for i in range(3000, 5001, 1000)],
    ]
    if for_admin:
        back_btn = InlineKeyboardButton(text="⬅️ Назад", callback_data=f"admin_self_back_to_order_{order_id}")
    else:
        back_btn = InlineKeyboardButton(text="⬅️ Назад", callback_data=f"executor_back_to_invite:{order_id}")
    buttons.append([back_btn])
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

def get_admin_deadline_keyboard():
    buttons = [
        [
            InlineKeyboardButton(text="1 день", callback_data="admin_deadline_1 день"),
            InlineKeyboardButton(text="3 дня", callback_data="admin_deadline_3 дня"),
            InlineKeyboardButton(text="До дедлайна", callback_data="admin_deadline_До дедлайна"),
        ],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="admin_self_back_to_price")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def     get_admin_order_keyboard(order, show_materials_button=True):
    status = order.get('status')
    executor_is_admin = str(order.get('executor_id')) == str(ADMIN_ID)

    # Если статус 'В работе' и исполнитель — админ, показываем кнопки 'Сдать работу' и 'Посмотреть материалы'
    if status == "В работе" and executor_is_admin:
        buttons = [
            [InlineKeyboardButton(text="✅ Сдать работу", callback_data=f"admin_admin_submit_work_{order['order_id']}")],
            [InlineKeyboardButton(text="📎 Посмотреть материалы заказа", callback_data=f"admin_show_materials:{order['order_id']}")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="admin_back")]
        ]
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
    if status != "Выполнена":
        buttons.append([InlineKeyboardButton(text="❌ Отказаться от заявки", callback_data=f"admin_delete_order:{order['order_id']}")])
        has_files = order.get('guidelines_file') or order.get('task_file') or order.get('task_text') or order.get('example_file')
        if show_materials_button and has_files:
            buttons.append([InlineKeyboardButton(text="📎 Посмотреть материалы заказа", callback_data=f"admin_show_materials:{order['order_id']}")])
        if not show_materials_button:
            buttons.append([InlineKeyboardButton(text="⬅️ Скрыть материалы", callback_data=f"admin_hide_materials:{order['order_id']}")])
        # Добавляем кнопку "Сдать работу" для статуса "В работе"
        if status == "В работе":
            buttons.append([InlineKeyboardButton(text="✅ Сдать работу", callback_data=f"admin_admin_submit_work_{order['order_id']}")])
    buttons.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="admin_back")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_admin_comment_skip_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➡️ Пропустить", callback_data="admin_skip_comment")]
    ])

async def admin_view_order_handler(callback: CallbackQuery, state: FSMContext):
    from shared import get_all_orders, ADMIN_ID, pluralize_days, get_full_name, get_admin_order_keyboard
    if callback.from_user.id != int(ADMIN_ID): return
    order_id = int(callback.data.split("_")[-1])
    orders = get_all_orders()
    target_order = next((order for order in orders if order['order_id'] == order_id), None)
    if not target_order:
        await callback.answer("Заказ не найден.", show_alert=True)
        return
 
    status = target_order.get('status')
    
    if status == 'Ожидает подтверждения' and 'executor_offer' in target_order:
        offer = target_order['executor_offer']
        executor_full_name = offer.get('executor_full_name', 'Без имени')
        price = offer.get('price')
        deadline = offer.get('deadline', 'N/A')
        executor_comment = offer.get('executor_comment', 'Нет')
        subject = target_order.get('subject', 'Не указан')
        deadline_str = pluralize_days(deadline)

        admin_notification = f"""✅ Исполнитель {executor_full_name} готов взяться за заказ по предмету \"{subject}\"\n\n<b>Предложенные условия:</b>\n💰 <b>Цена:</b> {price} ₽\n⏳ <b>Срок:</b> {deadline_str}\n💬 <b>Комментарий исполнителя:</b> {executor_comment or 'Нет'}"""

        keyboard = get_admin_order_keyboard(target_order, show_materials_button=True)
        try:
            await callback.message.edit_text(admin_notification, parse_mode="HTML", reply_markup=keyboard)
        except Exception:
            await callback.message.answer(admin_notification, parse_mode="HTML", reply_markup=keyboard)

    else:
        # Особый вывод для статуса 'На доработке' и если исполнитель — админ
        executor_is_admin = str(target_order.get('executor_id')) == str(ADMIN_ID)
        if status == 'На доработке' and executor_is_admin:
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
            revision_comment = target_order.get('revision_comment', '—')
            admin_name = 'Администратор'
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
                f"Дедлайн: {deadline}\n\n"
                f"Доработка: {revision_comment}\n"
                f"Исполнитель: {admin_name}"
            )
            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="✅ Сдать работу", callback_data=f"admin_admin_submit_work_{order_id}")],
                [InlineKeyboardButton(text="⬅️ Вернуться к заявкам", callback_data="admin_orders_list")]
            ])
            try:
                await callback.message.edit_text(details_text, reply_markup=keyboard)
            except Exception:
                await callback.message.answer(details_text, reply_markup=keyboard)
            await callback.answer()
            return
        # Показываем подробные детали заказа
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
        details_text = f"""<b>{header}</b>\n\nГруппа: {group}\nВУЗ: {university}\nПреподаватель: {teacher}\nНомер зачетки: {gradebook}\nПредмет: {subject}\nТип работы: {work_type_str}\nМетодичка: {guidelines}\nЗадание: {task}\nПример: {example}\nДедлайн: {deadline}"""
        keyboard = get_admin_order_keyboard(target_order, show_materials_button=True)
        try:
            await callback.message.edit_text(details_text, reply_markup=keyboard, parse_mode="HTML")
        except Exception:
            await callback.message.answer(details_text, reply_markup=keyboard, parse_mode="HTML")
    await callback.answer()

def get_executors_list():
    EXECUTORS_FILE = "executors.json"
    if not os.path.exists(EXECUTORS_FILE):
        return []
    with open(EXECUTORS_FILE, "r", encoding="utf-8") as f:
        try:
            return json.load(f)
        except Exception:
            return [] 
        

async def save_order_to_gsheets(order):
    """
    Сохраняет заявку в Google Sheets.
    """
    try:
        from google.oauth2.service_account import Credentials
        import gspread
        GOOGLE_SHEET_ID = "1D15yyPKHyN1Vw8eRnjT79xV28cwL_q5EIZa97tgTF2U"
        users_file = "users.json"
        phone_number = order.get("phone_number", "")
        if not phone_number and os.path.exists(users_file):
            with open(users_file, "r", encoding="utf-8") as f:
                try:
                    users = json.load(f)
                    phone_number = users.get(str(order.get("user_id")), {}).get("phone_number", "")
                except Exception:
                    phone_number = ""

         # --- Новый блок: срок выполнения в днях ---
        exec_deadline = ""
        executor_name = ""
        executor_price = ""

        if str(order.get('executor_id')) == str(ADMIN_ID):
            exec_deadline = order.get('deadline', '') # Срок выполнения (напр. "5 дней")
            due_date = order.get('due_date', '') # Дата сдачи (напр. "29.07.2025")
            executor_name = "Администратор"
            executor_price = order.get('final_price', '')
        elif 'executor_offers' in order and order.get('executor_id'):
            executor_id = str(order.get('executor_id'))
            selected_offer = next((o for o in order['executor_offers'] if str(o.get('executor_id')) == executor_id), None)
            if selected_offer:
                exec_deadline = selected_offer.get("deadline", "")
                due_date = selected_offer.get('due_date', order.get('deadline_date')) # Используем due_date из оффера или исходный
                executor_name = selected_offer.get("executor_full_name", "")
                executor_price = selected_offer.get("price", "")

        # Форматируем срок выполнения для вывода
        exec_deadline_str = pluralize_days(exec_deadline) if str(exec_deadline).isdigit() else exec_deadline

        profit = float(order.get("final_price", 0) or 0) - float(executor_price or 0)

        row = [
            order.get("order_id", ""),
            f"{order.get('first_name', '')} {order.get('last_name', '')}".strip(),
            phone_number,
            order.get("group_name", ""),
            order.get("gradebook", ""),
            executor_name,
            order.get("subject", ""),
            order.get("creation_date", ""),
            exec_deadline_str,  # Срок выполнения (форматированный)
            due_date,  # Дата сдачи
            executor_price,
            order.get("final_price", ""),
            str(profit),
            order.get("status", "")
        ]
        creds = Credentials.from_service_account_file("google-credentials.json", scopes=["https://www.googleapis.com/auth/spreadsheets"])
        gc = gspread.authorize(creds)
        sh = gc.open_by_key(GOOGLE_SHEET_ID)
        worksheet = sh.sheet1
        worksheet.append_row(row, value_input_option="USER_ENTERED")
    except Exception as e:
        print(f"Ошибка при сохранении в Google Sheets: {e}")