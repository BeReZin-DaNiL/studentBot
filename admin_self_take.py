from aiogram import Router, F
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.fsm.context import FSMContext
from shared import ADMIN_ID, get_price_keyboard, get_deadline_keyboard, admin_view_order_handler, get_admin_deadline_keyboard, get_admin_comment_skip_keyboard, pluralize_days, get_all_orders, bot
import json
from datetime import datetime, timedelta

admin_self_take_router = Router()

# FSM для админа, когда он сам берет заказ
class AdminSelfTake(StatesGroup):
    waiting_for_price = State()
    waiting_for_deadline = State()
    waiting_for_comment = State()
    waiting_for_confirm = State()

# --- Первый хендлер: старт процесса ---
@admin_self_take_router.callback_query(F.data.startswith("admin_self_take_"))
async def admin_self_take_start(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != int(ADMIN_ID):
        return
    order_id = int(callback.data.split("_")[-1])
    await state.update_data(order_id=order_id)
    await state.set_state(AdminSelfTake.waiting_for_price)
    await callback.message.edit_text("💰 Выберите или введите цену для клиента, или напишите вручную(только число):", reply_markup=get_price_keyboard(order_id=order_id, for_admin=True))
    await callback.answer()

# --- Остальные заглушки ---
@admin_self_take_router.callback_query(F.data.startswith("price_"), AdminSelfTake.waiting_for_price)
async def admin_self_take_price_choice(callback: CallbackQuery, state: FSMContext):
    price = callback.data.split("_")[-1]
    await state.update_data(price=price)
    await state.set_state(AdminSelfTake.waiting_for_deadline)
    await callback.message.edit_text("⏳ Выберите или введите срок выполнения:", reply_markup=get_admin_deadline_keyboard())
    await callback.answer()

@admin_self_take_router.callback_query(F.data == "price_manual", AdminSelfTake.waiting_for_price)
async def admin_self_take_price_manual(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text("Пожалуйста, введите цену вручную (только число):")
    await callback.answer()

@admin_self_take_router.message(AdminSelfTake.waiting_for_price)
async def admin_self_take_price_manual_input(message: Message, state: FSMContext):
    if not message.text.isdigit():
        await message.answer("Пожалуйста, введите только число.")
        return
    await state.update_data(price=message.text)
    await state.set_state(AdminSelfTake.waiting_for_deadline)
    await message.answer("⏳ Выберите или введите срок выполнения(только число):", reply_markup=get_admin_deadline_keyboard())

@admin_self_take_router.callback_query(F.data.startswith("admin_deadline_"), AdminSelfTake.waiting_for_deadline)
async def admin_self_take_deadline_choice(callback: CallbackQuery, state: FSMContext):
    deadline = callback.data.split("_", 2)[-1]
    await state.update_data(deadline=deadline)
    await state.set_state(AdminSelfTake.waiting_for_comment)
    await callback.message.edit_text("💬 Добавьте комментарий к заказу (или пропустите этот шаг):", reply_markup=get_admin_comment_skip_keyboard())
    await callback.answer()



@admin_self_take_router.message(AdminSelfTake.waiting_for_deadline)
async def admin_self_take_deadline_manual_input(message: Message, state: FSMContext):
    await state.update_data(deadline=message.text)
    await state.set_state(AdminSelfTake.waiting_for_comment)
    await message.answer("💬 Добавьте комментарий к заказу (или пропустите этот шаг):", reply_markup=get_admin_comment_skip_keyboard())

@admin_self_take_router.message(AdminSelfTake.waiting_for_comment)
async def admin_self_take_comment_input(message: Message, state: FSMContext):
    await state.update_data(comment=message.text)
    data = await state.get_data()
    order_id = data.get('order_id')
    # --- Получаем subject и work_type из state или из orders.json ---
    subject = data.get('subject')
    work_type_raw = data.get('work_type')
    if not subject or not work_type_raw:
        try:
            with open('orders.json', 'r', encoding='utf-8') as f:
                orders = json.load(f)
            order = next((o for o in orders if o.get('order_id') == order_id), None)
            if order:
                if not subject:
                    subject = order.get('subject', '—')
                if not work_type_raw:
                    work_type_raw = order.get('work_type', '—')
        except Exception:
            subject = subject or '—'
            work_type_raw = work_type_raw or '—'
    work_type = work_type_raw.replace('work_type_', '') if isinstance(work_type_raw, str) and work_type_raw.startswith('work_type_') else work_type_raw
    if work_type == 'other':
        work_type = data.get('work_type_other_name', 'Другое')
    price = data.get('price', '—')
    deadline = data.get('deadline', '—')
    deadline_str = pluralize_days(deadline) if str(deadline).isdigit() else str(deadline)
    comment = data.get('comment') or 'нету'
    text = (
        "<b>Проверьте введнные данные</b>\n\n"
        f"📚 <b>Предмет:</b> {subject}\n\n"
        f"📝 <b>Тип работы:</b> {work_type}\n\n"
        f"💰 <b>Сумма заказа:</b> {price} ₽\n\n"
        f"⏳ <b>Срок выполнения:</b> {deadline_str}\n\n"
        f"💬 <b>Комментарий:</b> {comment}"
    )
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💳 Отправить на оплату", callback_data="admin_self_send_to_pay")]
    ])
    await state.set_state(AdminSelfTake.waiting_for_confirm)
    await message.answer(text, reply_markup=keyboard, parse_mode="HTML")

@admin_self_take_router.callback_query(F.data == "admin_skip_comment", AdminSelfTake.waiting_for_comment)
async def admin_self_take_skip_comment(callback: CallbackQuery, state: FSMContext):
    await state.update_data(comment="нету")
    data = await state.get_data()
    order_id = data.get('order_id')
    subject = data.get('subject')
    work_type_raw = data.get('work_type')
    if not subject or not work_type_raw:
        try:
            with open('orders.json', 'r', encoding='utf-8') as f:
                orders = json.load(f)
            order = next((o for o in orders if o.get('order_id') == order_id), None)
            if order:
                if not subject:
                    subject = order.get('subject', '—')
                if not work_type_raw:
                    work_type_raw = order.get('work_type', '—')
        except Exception:
            subject = subject or '—'
            work_type_raw = work_type_raw or '—'
    work_type = work_type_raw.replace('work_type_', '') if isinstance(work_type_raw, str) and work_type_raw.startswith('work_type_') else work_type_raw
    if work_type == 'other':
        work_type = data.get('work_type_other_name', 'Другое')
    price = data.get('price', '—')
    deadline = data.get('deadline', '—')
    deadline_str = pluralize_days(deadline) if str(deadline).isdigit() else str(deadline)
    comment = data.get('comment') or 'нету'
    text = (
        "<b>Проверьте введенные данные</b>\n\n"
        f"📚 <b>Предмет:</b> {subject}\n"
        f"📝 <b>Тип работы:</b> {work_type}\n"
        f"💰 <b>Сумма заказа:</b> {price} ₽\n"
        f"⏳ <b>Срок выполнения:</b> {deadline_str}\n\n"
        f"💬 <b>Комментарий:</b> {comment}"
    )
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💳 Отправить на оплату", callback_data="admin_self_send_to_pay")]
    ])
    await state.set_state(AdminSelfTake.waiting_for_confirm)
    await callback.message.edit_text(text, reply_markup=keyboard, parse_mode="HTML")
    await callback.answer()

@admin_self_take_router.callback_query(F.data == "admin_self_send_to_pay", AdminSelfTake.waiting_for_confirm)
async def admin_self_send_to_pay_handler(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    order_id = data.get('order_id')
    price = data.get('price')
    deadline = data.get('deadline')
    comment = data.get('comment', '')
    # Обновляем заказ: назначаем админа исполнителем, статус 'Ожидает оплаты', финальная цена и дедлайн
    orders = get_all_orders()
    order = next((o for o in orders if o.get('order_id') == order_id), None)
    if not order:
        await callback.message.edit_text("Ошибка: заказ не найден.")
        await state.clear()
        await callback.answer()
        return
    order['executor_id'] = int(ADMIN_ID)
    order['status'] = 'Ожидает оплаты'
    order['final_price'] = price
    order['deadline'] = deadline  # Срок выполнения в днях/текстом

    # --- Расчет и сохранение даты сдачи ---
    due_date = order.get('deadline_date') # Исходный дедлайн от клиента
    if str(deadline).isdigit():
        try:
            # Если срок выполнения - число, считаем от сегодня
            due_date = (datetime.now() + timedelta(days=int(deadline))).strftime('%d.%m.%Y')
        except ValueError:
            pass # Оставляем исходный due_date если deadline не число
    elif deadline == "До дедлайна" and due_date:
        pass # Используем исходный дедлайн от клиента
    else: # Если срок - строка (напр. "1 день"), пытаемся распарсить
        try:
            days = int(deadline.split()[0])
            due_date = (datetime.now() + timedelta(days=days)).strftime('%d.%m.%Y')
        except (ValueError, IndexError):
            pass # Оставляем исходный

    order['due_date'] = due_date # Сохраняем дату сдачи
    order['executor_full_name'] = "Администратор"
    order['admin_self_comment'] = comment
    with open('orders.json', 'w', encoding='utf-8') as f:
        json.dump(orders, f, ensure_ascii=False, indent=4)
    # Сообщение клиенту
    customer_id = order.get('user_id')
    subject = order.get('subject', 'Не указан')
    work_type = order.get('work_type', 'Не указан').replace('work_type_', '')
    deadline_str = pluralize_days(deadline) if str(deadline).isdigit() else str(deadline)
    text = (
        f"🙋‍♂️ Исполнитель найден!\n\n"
        f"📚 Предмет: <b>{subject}</b>\n"
        f"📝 Тип работы: <b>{work_type}</b>\n"
        f"💰 Сумма заказа: <b>{price} ₽</b>\n"
        f"⏳ Срок выполнения: <b>{deadline_str}</b>\n\n"
        f"Пожалуйста, оплатите заказ для начала работы."
    )
    # Кнопка 'Я оплатил' (callback_data=pay_{order_id})
    pay_keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💳 Оплатить", callback_data=f"pay_{order_id}")],
        [InlineKeyboardButton(text="❌ Отказаться", callback_data=f"payment_cancel:{order_id}")]
    ])
    if customer_id:
        await bot.send_message(customer_id, text, parse_mode="HTML", reply_markup=pay_keyboard)
    await callback.message.edit_text("✅ Заказ отправлен клиенту на оплату!", reply_markup=None)
    await state.clear()
    await callback.answer()

@admin_self_take_router.callback_query(F.data.startswith("admin_self_back_to_order_"), AdminSelfTake.waiting_for_price)
async def admin_self_back_to_order_handler(callback: CallbackQuery, state: FSMContext):
    order_id = int(callback.data.split("_")[-1])
    await state.clear()
    await admin_view_order_handler(callback, state)
    await callback.answer()

@admin_self_take_router.callback_query(F.data == "admin_self_back_to_price", AdminSelfTake.waiting_for_deadline)
async def admin_self_back_to_price_handler(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    order_id = data.get('order_id')
    await state.set_state(AdminSelfTake.waiting_for_price)
    await callback.message.edit_text("💰 Выберите или введите цену для клиента, или напишите вручную(только число):", reply_markup=get_price_keyboard(order_id=order_id, for_admin=True))
    await callback.answer()

@admin_self_take_router.callback_query(F.data.startswith("admin_self_view_revision_"))
async def admin_self_view_revision_handler(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != int(ADMIN_ID):
        return
    order_id = int(callback.data.split("_")[-1])
    orders = get_all_orders()
    order = next((o for o in orders if o.get('order_id') == order_id), None)
    if not order:
        await callback.answer("Заказ не найден.", show_alert=True)
        return
    # Формируем подробный текст по шаблону пользователя
    creation_date = order.get('creation_date', '—')
    group = order.get('group_name', '—')
    university = order.get('university_name', '—')
    teacher = order.get('teacher_name', '—')
    gradebook = order.get('gradebook', '—')
    subject = order.get('subject', '—')
    work_type_key = order.get('work_type', 'N/A').replace('work_type_', '')
    work_type_str = work_type_key if work_type_key != 'other' else order.get('work_type_other_name', 'Другое')
    guidelines = '✅ Да' if order.get('has_guidelines') else '❌ Нет'
    task = '✅ Прикреплено' if order.get('task_file') or order.get('task_text') else '❌ Нет'
    example = '✅ Да' if order.get('has_example') else '❌ Нет'
    deadline = order.get('deadline', '—')
    revision_comment = order.get('revision_comment', '—')
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