from aiogram import Router, F, types
from aiogram.types import Message, KeyboardButton, ReplyKeyboardMarkup, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.filters import StateFilter
import json
import os
from shared import ADMIN_ID, bot, get_full_name
from datetime import datetime

EXECUTORS_FILE = "executors.json"
ORDERS_FILE = "orders.json"

executor_menu_router = Router()

class ExecutorStates(StatesGroup):
    waiting_for_admin_message = State()
    waiting_for_work_file = State()
    waiting_for_work_submit = State()

class ExecutorCancelOrder(StatesGroup):
    waiting_for_confirm = State()
    waiting_for_reason = State()
    waiting_for_custom_reason = State()
    waiting_for_comment = State()

class ExecutorContactClient(StatesGroup):
    waiting_for_message = State()

EXECUTOR_CANCEL_REASONS = [
    "Не успею до дедлайна",
    "Передумал",
    "Сложная тема",
    "Другое (ввести вручную)"
]

# Список статусов, которые видит исполнитель
EXECUTOR_VISIBLE_STATUSES = [
    'Ожидает подтверждения',
    'В работе',
    'Выполнена',
    'Отправлен на проверку',
    'На доработке',
    'Утверждено администратором',
    'Ожидает оплаты'
]

def is_executor(user_id: int) -> bool:
    if not os.path.exists(EXECUTORS_FILE):
        return False
    with open(EXECUTORS_FILE, "r", encoding="utf-8") as f:
        try:
            executors = json.load(f)
        except Exception:
            return False
    return any(str(ex.get("id")) == str(user_id) for ex in executors)

def get_executor_menu_keyboard():
    buttons = [
        [KeyboardButton(text="📂 Мои заказы")],
        [KeyboardButton(text="👨‍💻 Связаться с администратором")]
    ]
    return ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True)

def get_executor_cancel_confirm_keyboard(order_id):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Да", callback_data=f"executor_cancel_confirm:{order_id}")],
        [InlineKeyboardButton(text="Нет", callback_data=f"executor_cancel_abort:{order_id}")]
    ])

def get_executor_cancel_reason_keyboard(order_id):
    buttons = [
        [InlineKeyboardButton(text=reason, callback_data=f"executor_cancel_reason:{order_id}:{i}")]
        for i, reason in enumerate(EXECUTOR_CANCEL_REASONS)
    ]
    buttons.append([InlineKeyboardButton(text="⬅️ Назад", callback_data=f"executor_view_order_{order_id}")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_executor_cancel_comment_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➡️ Пропустить", callback_data="executor_skip_cancel_comment")]
    ])

def get_executor_orders(user_id: int) -> list:
    if not os.path.exists(ORDERS_FILE):
        return []
    with open(ORDERS_FILE, "r", encoding="utf-8") as f:
        try:
            all_orders = json.load(f)
        except Exception:
            return []
    result = []
    for o in all_orders:
        status = o.get("status")
        # Обычные статусы — только если исполнитель назначен
        if str(o.get("executor_id")) == str(user_id) and status in EXECUTOR_VISIBLE_STATUSES:
            result.append(o)
        # Для рассылки: если статус 'Ожидает подтверждения' и оффер был отправлен всем
        elif status == "Ожидает подтверждения":
            from shared import get_executors_list
            all_execs = get_executors_list()
            all_exec_ids = [str(ex.get("id")) for ex in all_execs]
            if str(user_id) in all_exec_ids:
                result.append(o)
    return result

@executor_menu_router.message(F.text == "/start")
async def executor_start(message: Message, state: FSMContext):
    if is_executor(message.from_user.id):
        await state.clear()
        await message.answer(
            "👋 Добро пожаловать в меню исполнителя!",
            reply_markup=get_executor_menu_keyboard()
        )

@executor_menu_router.message(F.text == "📂 Мои заказы")
@executor_menu_router.callback_query(F.data == "executor_back_to_orders")
async def executor_my_orders(message_or_callback, state: FSMContext):
    user_id = message_or_callback.from_user.id
    orders = get_executor_orders(user_id)
    if not orders:
        text = "❗️ У вас пока нет назначенных заказов."
        keyboard = None
    else:
        text = "Ваши заявки:"
        keyboard_buttons = []
        for order in reversed(orders[-10:]):
            order_id = order.get('order_id')
            status = order.get('status', 'N/A')
            subject = order.get('subject', 'Не указан')
            work_type = order.get('work_type', 'Заявка').replace('work_type_', '')
            button_text = f"Заказ на тему: {work_type} | {status}"
            keyboard_buttons.append([InlineKeyboardButton(text=button_text, callback_data=f"executor_view_order_{order_id}")])
        keyboard = InlineKeyboardMarkup(inline_keyboard=keyboard_buttons)
    if isinstance(message_or_callback, Message):
        await message_or_callback.answer(text, reply_markup=keyboard)
    else:
        await message_or_callback.message.edit_text(text, reply_markup=keyboard)
        if hasattr(message_or_callback, "answer"):
            await message_or_callback.answer()

@executor_menu_router.callback_query(F.data.startswith("executor_view_order_"))
async def executor_view_order(callback: CallbackQuery, state: FSMContext):
    order_id = int(callback.data.split("_")[-1])
    orders = get_executor_orders(callback.from_user.id)
    order = next((o for o in orders if o.get('order_id') == order_id), None)
    if not order:
        if hasattr(callback, "answer"):
            await callback.answer("Заявка не найдена.", show_alert=True)
        return
    from shared import STATUS_EMOJI_MAP
    status = order.get('status', 'N/A')
    emoji = STATUS_EMOJI_MAP.get(status, '📄')
    work_type = order.get('work_type', 'Не указан').replace('work_type_', '')
    subject = order.get('subject', 'Не указан')
    deadline = order.get('deadline', 'Не указан')
    comment = order.get('comments', 'Нет')

    # Новый блок для статусов 'Ожидает подтверждения' и 'Ожидает подтверждения от исполнителя'
    if status in ["Ожидает подтверждения", "Ожидает подтверждения от исполнителя"]:
        text = (
            f"📬 Вам предложен новый заказ по предмету {subject}\n\n"
            f"📝 Тип работы: {work_type}\n"
            f"🗓 Срок сдачи: {deadline}\n\n"
            f"Пожалуйста, ознакомьтесь с материалами заявки и примите решение."
        )
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📎 Посмотреть материалы", callback_data=f"executor_show_materials:{order_id}")],
            [InlineKeyboardButton(text="✅ Готов взяться", callback_data=f"executor_accept_{order_id}"),
             InlineKeyboardButton(text="❌ Отказаться", callback_data=f"executor_refuse_work_{order_id}")],
            [InlineKeyboardButton(text="⬅️ Вернуться к заказам", callback_data="executor_back_to_orders")]
        ])
        if hasattr(callback.message, "edit_text"):
            await callback.message.edit_text(text, reply_markup=keyboard)
        else:
            await callback.message.answer(text, reply_markup=keyboard)
        if hasattr(callback, "answer"):
            await callback.answer()
        return

    # Новый блок для статуса 'Ожидает оплаты'
    if status == "Ожидает оплаты":
        text = (
            f"⏳ Заказ по предмету {subject} ожидает оплаты клиентом.\n\n"
            f"📝 Тип работы: {work_type}\n"
            f"🗓 Срок сдачи: {deadline}\n\n"
            f"Ожидайте подтверждения оплаты."
        )
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⬅️ Вернуться к заявкам", callback_data="executor_back_to_orders")]
        ])
        if hasattr(callback.message, "edit_text"):
            await callback.message.edit_text(text, reply_markup=keyboard)
        else:
            await callback.message.answer(text, reply_markup=keyboard)
        if hasattr(callback, "answer"):
            await callback.answer()
        return

    # Новый блок для статуса 'Утверждено администратором'
    if status == "Утверждено администратором":
        group = order.get("group_name", "Не указана")
        university = order.get("university_name", "Не указан")
        teacher = order.get("teacher_name", "Не указан")
        gradebook = order.get("gradebook", "Не указан")
        work_type_key = order.get("work_type", "N/A").replace("work_type_", "")
        work_type_str = work_type_key if work_type_key != 'other' else order.get('work_type_other_name', 'Другое')
        guidelines = '✅ Да' if order.get('has_guidelines') else '❌ Нет'
        task = '✅ Прикреплено' if order.get('task_file') or order.get('task_text') else '❌ Нет'
        example = '✅ Да' if order.get('has_example') else '❌ Нет'
        deadline = order.get('deadline', 'Не указана')
        details_text = f"""
<b>Детали заказа №{order_id}</b>\n
<b>Статус:</b> {emoji} {status}
<b>Предмет:</b> {subject}
<b>Тип работы:</b> {work_type_str}
<b>Методичка:</b> {guidelines}
<b>Задание:</b> {task}
<b>Пример:</b> {example}
<b>Дедлайн:</b> {deadline}\n
<b>Комментарий:</b> {comment}
            """
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⬅️ Вернуться к заказам", callback_data="executor_back_to_orders")]
        ])
        if hasattr(callback.message, "edit_text"):
            await callback.message.edit_text(details_text, parse_mode="HTML", reply_markup=keyboard)
        else:
            await callback.message.answer(details_text, parse_mode="HTML", reply_markup=keyboard)
        if hasattr(callback, "answer"):
            await callback.answer()
        return

    # Новый блок для статуса 'Выполнена'
    if status == "Выполнена":
        subject = order.get('subject', '—')
        work_type = order.get('work_type', '—').replace('work_type_', '')
        executor_offers = order.get('executor_offers', [])
        
        # Найти предложение текущего исполнителя
        work_price = 0
        if executor_offers:
            for offer in executor_offers:
                if offer.get('executor_id') == callback.from_user.id:
                    work_price = offer.get('price', 0)
                    break
            # Если не найдено предложение текущего исполнителя, взять первое
            if work_price == 0 and executor_offers:
                work_price = executor_offers[0].get('price', 0)
        
        details_text = (
            f"🎉 Клиент принял вашу работу по заказу\n"
            f"Предмет: {subject}\n"
            f"Тип работы: {work_type}\n"
            f"Заработал: {work_price} ₽"
        )
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⬅️ Вернуться к заказам", callback_data="executor_back_to_orders")]
        ])
        if hasattr(callback.message, "edit_text"):
            await callback.message.edit_text(details_text, reply_markup=keyboard)
        else:
            await callback.message.answer(details_text, reply_markup=keyboard)
        if hasattr(callback, "answer"):
            await callback.answer()
        return

    text = f"{emoji} Детали заявки №{order_id}\n\n" \
           f"Статус: {status}\n" \
           f"Предмет: {subject}\n" \
           f"Тип работы: {work_type}\n" \
           f"Дедлайн: {deadline}\n" \
           f"Комментарий: {comment}"
    
    keyboard_buttons = []

    if status == "Отправлен на проверку":
        submitted_at = order.get('submitted_at', '—')
        text += f"\nОтправлено: {submitted_at}"
    elif status == "На доработке":
        revision_comment = order.get('revision_comment', 'Нет')
        text += f"\n\n❗️Комментарий клиента к доработке:\n{revision_comment}"

    if status in ["В работе", "На доработке"]:
        keyboard_buttons.append([InlineKeyboardButton(text="✅ Сдать работу", callback_data=f"executor_submit_work_{order_id}")])

    if status in ["В работе", "Ожидает подтверждения", "На доработке"]:
        keyboard_buttons.append([InlineKeyboardButton(text="📎 Посмотреть материалы", callback_data=f"executor_show_materials:{order_id}")])
        keyboard_buttons.append([InlineKeyboardButton(text="❌ Отказаться", callback_data=f"executor_refuse_work_{order_id}")])
    
    keyboard_buttons.append([InlineKeyboardButton(text="⬅️ Вернуться к заказам", callback_data="executor_back_to_orders")])
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=keyboard_buttons)

    if hasattr(callback.message, "edit_text"):
        await callback.message.edit_text(text, reply_markup=keyboard)
    else:
        await callback.message.answer(text, reply_markup=keyboard)
    if hasattr(callback, "answer"):
        await callback.answer()

@executor_menu_router.message(F.text == "👨‍💻 Связаться с администратором")
async def executor_contact_admin(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("✍️ Напишите ваше сообщение, и я отправлю его администратору.")
    await state.set_state(ExecutorStates.waiting_for_admin_message)

@executor_menu_router.message(StateFilter(ExecutorStates.waiting_for_admin_message))
async def executor_send_admin_message(message: Message, state: FSMContext):
    await bot.send_message(
        ADMIN_ID,
        f"📩 Сообщение от исполнителя {get_full_name(message.from_user)} (ID: {message.from_user.id}):\n\n{message.text}"
    )
    await message.answer("✅ Ваше сообщение отправлено администратору.", reply_markup=get_executor_menu_keyboard())
    await state.clear()

@executor_menu_router.callback_query(F.data.startswith("executor_submit_work_"))
async def executor_submit_work_start(callback: CallbackQuery, state: FSMContext):
    order_id = int(callback.data.split("_")[-1])
    await state.set_state(ExecutorStates.waiting_for_work_file)
    await state.update_data(submit_order_id=order_id)
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ Вернуться к заказу", callback_data=f"executor_view_order_{order_id}")],
    ])
    if hasattr(callback.message, "edit_text"):
        await callback.message.edit_text(
            "Пожалуйста, прикрепите файл с выполненной работой (zip, docx, pdf и др.)",
            reply_markup=keyboard
        )
    else:
        await callback.message.answer(
            "Пожалуйста, прикрепите файл с выполненной работой (zip, docx, pdf и др.)",
            reply_markup=keyboard
        )
    if hasattr(callback, "answer"):
        await callback.answer()

@executor_menu_router.message(ExecutorStates.waiting_for_work_file, F.document)
async def executor_work_file_received(message: Message, state: FSMContext):
    data = await state.get_data()
    order_id = data.get('submit_order_id')
    await state.update_data(work_file_id=message.document.file_id, work_file_name=message.document.file_name)
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📤 Отправить на проверку", callback_data=f"executor_send_work_{order_id}")],
        [InlineKeyboardButton(text="❌ Отменить", callback_data=f"executor_cancel_submit_{order_id}")],
    ])
    if hasattr(message, "answer"):
        await message.answer("Файл успешно прикреплен!", reply_markup=keyboard)

@executor_menu_router.callback_query(F.data.startswith("executor_send_work_"), ExecutorStates.waiting_for_work_file)
async def executor_send_work(callback: CallbackQuery, state: FSMContext):
    from shared import get_all_orders, ADMIN_ID, bot
    import json
    data = await state.get_data()
    order_id = data.get('submit_order_id')
    file_id = data.get('work_file_id')
    file_name = data.get('work_file_name')
    orders = get_all_orders()
    for order in orders:
        if order.get('order_id') == order_id:
            order['status'] = 'Отправлен на проверку'
            order['submitted_work'] = {'file_id': file_id, 'file_name': file_name}
            order['submitted_at'] = datetime.now().strftime('%d.%m.%Y')
            break
    with open('orders.json', 'w', encoding='utf-8') as f:
        json.dump(orders, f, ensure_ascii=False, indent=4)
    order = next((o for o in orders if o.get('order_id') == order_id), None)
    subject = order.get('subject', 'Не указан')
    work_type = order.get('work_type', 'Не указан').replace('work_type_', '')
    submitted_at = order.get('submitted_at', '')
    admin_text = f"Исполнитель выполнил заказ по предмету <b>{subject}</b>\nТип работы: <b>{work_type}</b>\nДата выполнения: <b>{submitted_at}</b>"
    admin_keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Утвердить заказ", callback_data=f"admin_approve_work_{order_id}"),
         InlineKeyboardButton(text="🔽 Отправить на доработку", callback_data=f"admin_reject_work_{order_id}")],
    ])
    await bot.send_document(ADMIN_ID, file_id, caption=admin_text, parse_mode="HTML", reply_markup=admin_keyboard)
    await bot.send_message(callback.from_user.id, "💼 Работа отправлена на проверку администратору!\n⏳Ожидайте ответа.")
    await state.clear()
    if hasattr(callback, "answer"):
        await callback.answer()

@executor_menu_router.callback_query(F.data.startswith("executor_cancel_submit_"), ExecutorStates.waiting_for_work_file)
async def executor_cancel_submit(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    if hasattr(callback.message, "edit_text"):
        await callback.message.edit_text("❌ Отправка работы отменена.")
    else:
        await callback.message.answer("❌ Отправка работы отменена.")
    if hasattr(callback, "answer"):
        await callback.answer()



@executor_menu_router.callback_query(F.data.startswith("executor_refuse_work_") | F.data.startswith("executor_refuse_"))
async def executor_refuse_start(callback: CallbackQuery, state: FSMContext):
    from shared import get_all_orders
    
    order_id_str = callback.data.split('_')[-1]
    if not order_id_str.isdigit():
        if hasattr(callback, "answer"):
            await callback.answer("Ошибка: неверный ID заказа.", show_alert=True)
        return
    order_id = int(order_id_str)

    all_orders = get_all_orders()
    order = next((o for o in all_orders if o.get('order_id') == order_id and o.get('executor_id') == callback.from_user.id), None)

    if not order:
        if hasattr(callback, "answer"):
            await callback.answer("Заказ не найден или уже не актуален.", show_alert=True)
        return

    if order.get("status") == "В работе":
        await state.set_state(ExecutorCancelOrder.waiting_for_confirm)
        await state.update_data(cancel_order_id=order_id)
        if hasattr(callback.message, "edit_text"):
            await callback.message.edit_text(
                "❗️ Вы уверены, что хотите отказаться от этого заказа?",
                reply_markup=get_executor_cancel_confirm_keyboard(order_id)
            )
        else:
            await callback.message.answer(
                "❗️ Вы уверены, что хотите отказаться от этого заказа?",
                reply_markup=get_executor_cancel_confirm_keyboard(order_id)
            )
    else:
        order['status'] = "Рассматривается"
        order.pop('executor_id', None)
        order.pop('executor_offers', None)
        
        with open("orders.json", "w", encoding="utf-8") as f:
            json.dump(all_orders, f, ensure_ascii=False, indent=4)
        
        subject = order.get('subject', 'Не указан')
        await bot.send_message(
            ADMIN_ID,
            f"❌ Исполнитель {get_full_name(callback.from_user)} (ID: {callback.from_user.id}) отказался от заказа по предмету \"{subject}\"",
            parse_mode="HTML"
        )
        if hasattr(callback.message, "edit_text"):
            await callback.message.edit_text(f"❗️ Вы отказались от заказа по предмету: {subject} 📄")
        else:
            await callback.message.answer(f"❗️ Вы отказались от заказа по предмету: {subject} 📄")
    if hasattr(callback, "answer"):
        await callback.answer()

@executor_menu_router.callback_query(F.data.startswith("executor_cancel_confirm:"), ExecutorCancelOrder.waiting_for_confirm)
async def executor_cancel_confirm(callback: CallbackQuery, state: FSMContext):
    order_id = int(callback.data.split(":")[1])
    await state.set_state(ExecutorCancelOrder.waiting_for_reason)
    if hasattr(callback.message, "edit_text"):
        await callback.message.edit_text(
            "💬 Пожалуйста, выберите причину отказа:",
            reply_markup=get_executor_cancel_reason_keyboard(order_id)
        )
    else:
        await callback.message.answer(
            "💬 Пожалуйста, выберите причину отказа:",
            reply_markup=get_executor_cancel_reason_keyboard(order_id)
        )
    if hasattr(callback, "answer"):
        await callback.answer()

@executor_menu_router.callback_query(F.data.startswith("executor_cancel_abort:"), ExecutorCancelOrder.waiting_for_confirm)
async def executor_cancel_abort(callback: CallbackQuery, state: FSMContext):
    order_id = int(callback.data.split(":")[1])
    await state.clear()
    callback.data = f"executor_view_order_{order_id}"
    await executor_view_order(callback, state)

@executor_menu_router.callback_query(F.data.startswith("executor_cancel_reason:"), ExecutorCancelOrder.waiting_for_reason)
async def executor_cancel_reason(callback: CallbackQuery, state: FSMContext):
    _, order_id_str, idx_str = callback.data.split(":")
    order_id = int(order_id_str)
    idx = int(idx_str)
    reason = EXECUTOR_CANCEL_REASONS[idx]

    await state.update_data(cancellation_reason=reason)

    if reason.startswith("Другое"):
        await state.set_state(ExecutorCancelOrder.waiting_for_custom_reason)
        if hasattr(callback.message, "edit_text"):
            await callback.message.edit_text("✍️ Пожалуйста, введите причину отказа:")
        else:
            await callback.message.answer("✍️ Пожалуйста, введите причину отказа:")
    else:
        await state.set_state(ExecutorCancelOrder.waiting_for_comment)
        if hasattr(callback.message, "edit_text"):
            await callback.message.edit_text(
                "💬 Добавьте комментарий к отказу (или пропустите):",
                reply_markup=get_executor_cancel_comment_keyboard()
            )
        else:
            await callback.message.answer(
                "💬 Добавьте комментарий к отказу (или пропустите):",
                reply_markup=get_executor_cancel_comment_keyboard()
            )
    if hasattr(callback, "answer"):
        await callback.answer()

@executor_menu_router.message(ExecutorCancelOrder.waiting_for_custom_reason)
async def executor_cancel_custom_reason(message: Message, state: FSMContext):
    await state.update_data(cancellation_reason=message.text)
    await state.set_state(ExecutorCancelOrder.waiting_for_comment)
    if hasattr(message, "answer"):
        await message.answer(
            "💬 Добавьте комментарий к отказу (или пропустите):",
            reply_markup=get_executor_cancel_comment_keyboard()
        )
    else:
        await message.answer(
            "💬 Добавьте комментарий к отказу (или пропустите):",
            reply_markup=get_executor_cancel_comment_keyboard()
        )

@executor_menu_router.message(ExecutorCancelOrder.waiting_for_comment)
async def executor_cancel_comment_input(message: Message, state: FSMContext):
    data = await state.get_data()
    order_id = data.get("cancel_order_id")
    reason = data.get("cancellation_reason")
    comment = message.text
    await finish_executor_cancel_order(message, state, order_id, reason, comment)

@executor_menu_router.callback_query(F.data == "executor_skip_cancel_comment", ExecutorCancelOrder.waiting_for_comment)
async def executor_cancel_skip_comment(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    order_id = data.get("cancel_order_id")
    reason = data.get("cancellation_reason")
    await finish_executor_cancel_order(callback, state, order_id, reason, "")
    if hasattr(callback, "answer"):
        await callback.answer()


async def finish_executor_cancel_order(message_or_callback, state, order_id, reason, comment):
    from shared import get_all_orders, get_full_name
    all_orders = get_all_orders()
    target_order = None
    for order in all_orders:
        if order.get('order_id') == order_id:
            order['status'] = "Рассматривается"
            order.pop('executor_id', None)
            order.pop('executor_offers', None)
            target_order = order
            break
            
    if not target_order:
        if isinstance(message_or_callback, Message):
            await message_or_callback.answer("Не удалось обработать отказ, заказ не найден.")
        else:
            await message_or_callback.message.edit_text("Не удалось обработать отказ, заказ не найден.")
        await state.clear()
        return

    with open("orders.json", "w", encoding="utf-8") as f:
        json.dump(all_orders, f, ensure_ascii=False, indent=4)
    
    await state.clear()
    
    if isinstance(message_or_callback, Message):
        await message_or_callback.answer("Вы отказались от заказа. Администратор уведомлен.")
    else:
        await message_or_callback.message.edit_text("Вы отказались от заказа. Администратор уведомлен.")

    admin_text = (f"❌ Исполнитель {get_full_name(message_or_callback.from_user)} (ID: {message_or_callback.from_user.id}) "
                  f"отказался от заказа №{order_id}, который был в работе.\n\n"
                  f"<b>Причина:</b> {reason}\n"
                  f"<b>Комментарий:</b> {comment or 'Нет'}")
    await bot.send_message(ADMIN_ID, admin_text, parse_mode="HTML")


@executor_menu_router.callback_query(F.data.startswith("executor_contact_client:"))
async def executor_contact_client_handler(callback: CallbackQuery, state: FSMContext):
    order_id = int(callback.data.split(":")[-1])
    await state.update_data(order_id=order_id)
    await state.set_state(ExecutorContactClient.waiting_for_message)
    await callback.message.answer("Напишите ваше сообщение для клиента. Оно будет переслано ему, а также администратору.")
    await callback.answer()

@executor_menu_router.message(ExecutorContactClient.waiting_for_message)
async def executor_send_message_to_client(message: Message, state: FSMContext):
    data = await state.get_data()
    order_id = data.get("order_id")
    executor_id = message.from_user.id

    with open(ORDERS_FILE, "r", encoding="utf-8") as f:
        orders = json.load(f)
    
    order = next((o for o in orders if o.get('order_id') == order_id), None)

    if not order:
        await message.answer("❗️ Заказ не найден.")
        await state.clear()
        return

    client_id = order.get("user_id")
    executor_name = get_full_name(message.from_user)

    # Сообщение для клиента
    client_message = (
        f"Сообщение от исполнителя по заказу №{order_id}:\n\n"
        f"{message.text}"
    )
    reply_keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Ответить", callback_data=f"client_reply_to_executor:{order_id}")]
    ])

    try:
        await bot.send_message(client_id, client_message, reply_markup=reply_keyboard)
        # Копия администратору
        admin_message = (
            f"Исполнитель {executor_name} (ID: {executor_id}) отправил сообщение клиенту по заказу №{order_id}:\n\n"
            f"{message.text}"
        )
        await bot.send_message(ADMIN_ID, admin_message)

        await message.answer("✅ Ваше сообщение отправлено клиенту и администратору.")
    except Exception as e:
        await message.answer(f"❗️ Не удалось отправить сообщение. Ошибка: {e}")
    
    await state.clear()
    
@executor_menu_router.callback_query(F.data.startswith("executor_show_materials:"))
async def executor_show_materials_handler(callback: CallbackQuery, state: FSMContext):
    order_id = callback.data.split(":", 1)[1]
    from shared import get_all_orders
    orders = get_all_orders()
    order = next((o for o in orders if str(o['order_id']) == str(order_id)), None)
    if not order:
        if hasattr(callback, "answer"):
            await callback.answer("Заказ не найден.", show_alert=True)
        return
    material_buttons = []
    if order.get('guidelines_file'):
        material_buttons.append([InlineKeyboardButton(text="Методичка", callback_data=f"executor_material_guidelines:{order_id}")])
    if order.get('task_file') or order.get('task_text'):
        material_buttons.append([InlineKeyboardButton(text="Задание", callback_data=f"executor_material_task:{order_id}")])
    if order.get('example_file'):
        material_buttons.append([InlineKeyboardButton(text="Пример работы", callback_data=f"executor_material_example:{order_id}")])
    material_buttons.append([InlineKeyboardButton(text="⬅️ Назад", callback_data=f"executor_view_order_{order_id}")])
    keyboard = InlineKeyboardMarkup(inline_keyboard=material_buttons)
    if hasattr(callback.message, "edit_text"):
        await callback.message.edit_text("Выберите материал для просмотра:", reply_markup=keyboard)
    else:
        await callback.message.answer("Выберите материал для просмотра:", reply_markup=keyboard)
    if hasattr(callback, "answer"):
        await callback.answer()