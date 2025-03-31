import os
import logging
import sqlite3
import requests
import telebot
from telebot import types
import threading
import time
from pathlib import Path

# Настройка логирования
DATA_DIR = Path("/mount/database")
DATA_DIR.mkdir(exist_ok=True)

log_file = DATA_DIR / f"bot_{time.strftime('%Y%m%d')}.log"
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler(log_file),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger("payment_bot")

# Получение настроек из переменных окружения
BOT_TOKEN = os.getenv("BOT_TOKEN")
LAVA_API_KEY = os.getenv("LAVA_API_KEY")
CHANNEL_ID = os.getenv("CHANNEL_ID")
ADMIN_ID = os.getenv("ADMIN_ID")
DB_PATH = DATA_DIR / "lava_payments.db"

# Обновляем словари для переводов
PERIOD_TRANSLATIONS = {
    "MONTHLY": "1 месяц",
    "PERIOD_90_DAYS": "3 месяца",
    "PERIOD_180_DAYS": "6 месяцев",
    "PERIOD_YEAR": "1 год"
}

CURRENCY_TRANSLATIONS = {
    "RUB": "₽",
    "USD": "$",
    "EUR": "€"
}

# Инициализация бота
bot = telebot.TeleBot(BOT_TOKEN)

# Функция для получения списка доступных подписок
def get_available_subscriptions():
    url = "https://gate.lava.top/api/v2/products"
    params = {
        "contentCategories": "PRODUCT",
        "feedVisibility": "ONLY_VISIBLE",
        "showAllSubscriptionPeriods": "true"
    }
    headers = {
        "X-Api-Key": LAVA_API_KEY
    }
    
    try:
        response = requests.get(url, params=params, headers=headers)
        response.raise_for_status()
        data = response.json()
        
        subscriptions = []
        for item in data.get("items", []):
            if item.get("type") == "SUBSCRIPTION":
                for offer in item.get("offers", []):
                    # Группируем цены по периодичности
                    prices_by_period = {}
                    for price in offer["prices"]:
                        if price["periodicity"] not in prices_by_period:
                            prices_by_period[price["periodicity"]] = {}
                        prices_by_period[price["periodicity"]][price["currency"]] = price["amount"]
                    
                    # Преобразуем в список для удобства
                    prices = []
                    for periodicity, currencies in prices_by_period.items():
                        prices.append({
                            "periodicity": periodicity,
                            "currencies": currencies
                        })
                    
                    if prices:
                        subscriptions.append({
                            "offer_id": offer["id"],
                            "name": offer["name"],
                            "description": offer["description"],
                            "prices": prices
                        })
        
        return subscriptions
    except Exception as e:
        logger.error(f"Ошибка при получении списка подписок: {str(e)}")
        return None

# Функция для создания ссылки на оплату
def create_payment_link(user_id, offer_id, periodicity, currency="RUB"):
    url = "https://gate.lava.top/api/v2/invoice"
    headers = {
        "Content-Type": "application/json",
        "X-Api-Key": LAVA_API_KEY
    }
    
    payload = {
        "email": f"{user_id}@t.me",
        "offerId": offer_id,
        "periodicity": periodicity,
        "currency": currency,
        "buyerLanguage": "RU",
        "clientUtm": {}
    }
    
    try:
        response = requests.post(url, headers=headers, json=payload)
        response.raise_for_status()
        return response.json()
    except Exception as e:
        logger.error(f"Ошибка при создании ссылки на оплату: {str(e)}")
        if hasattr(e, 'response') and e.response is not None:
            logger.error(f"Ответ сервера: {e.response.text}")
        return None

# Функция для отмены подписки
def cancel_subscription(user_id, parent_contract_id):
    url = "https://gate.lava.top/api/v1/subscriptions"
    headers = {
        "Content-Type": "application/json",
        "X-Api-Key": LAVA_API_KEY
    }
    payload = {
        "contractId": parent_contract_id,
        "email": f"{user_id}@t.me"
    }
    
    try:
        response = requests.delete(url, headers=headers, json=payload)
        
        # Логируем ответ от LAVA.TOP
        logger.info(f"Ответ от LAVA.TOP при отмене подписки: Статус {response.status_code}, Заголовки: {response.headers}")
        
        # Проверяем код ответа (204 означает успешную отмену)
        if response.status_code == 204:
            # Обновляем статус подписки в БД
            conn = sqlite3.connect(DB_PATH)
            cursor = conn.cursor()
            cursor.execute('''
            UPDATE payments 
            SET status = 'subscription-cancelled' 
            WHERE buyer_email = ? AND (parent_contract_id = ? OR contract_id = ?)
            ''', (f"{user_id}@t.me", parent_contract_id, parent_contract_id))
            conn.commit()
            conn.close()
            
            logger.info(f"Подписка успешно отменена для пользователя {user_id}, контракт {parent_contract_id}")
            return True
        else:
            logger.error(f"Ошибка при отмене подписки: код {response.status_code}, ответ: {response.text}")
            return False
    except Exception as e:
        logger.error(f"Исключение при отмене подписки: {str(e)}")
        return False

# Функция для проверки статуса подписки пользователя
def check_subscription_status(user_id):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    # Ищем активные подписки пользователя
    cursor.execute('''
    SELECT * FROM payments 
    WHERE buyer_email = ? 
    AND (status = 'subscription-active' OR status = 'active')
    ORDER BY timestamp DESC
    LIMIT 1
    ''', (f"{user_id}@t.me",))
    
    active_subscription = cursor.fetchone()
    
    # Ищем последнюю неудачную попытку оплаты
    cursor.execute('''
    SELECT * FROM payments 
    WHERE buyer_email = ? 
    AND (status = 'subscription-failed' OR status = 'failed')
    ORDER BY timestamp DESC
    LIMIT 1
    ''', (f"{user_id}@t.me",))
    
    failed_payment = cursor.fetchone()
    
    conn.close()
    
    if active_subscription:
        return {
            "status": "active",
            "data": active_subscription
        }
    elif failed_payment:
        return {
            "status": "failed",
            "data": failed_payment
        }
    else:
        return {
            "status": "no_subscription"
        }

# Функция для добавления пользователя в закрытый канал
def add_user_to_channel(user_id):
    try:
        # Создаем ссылку-приглашение в канал
        invite_link = bot.create_chat_invite_link(
            chat_id=CHANNEL_ID,
            member_limit=1,  # Ограничение на одного пользователя
            expire_date=int(time.time()) + 86400  # Срок действия 24 часа
        )
        
        # Отправляем пользователю ссылку на канал
        bot.send_message(
            user_id,
            f"Поздравляем! Вы успешно оформили подписку. Вот ваша ссылка для доступа к закрытому каналу: {invite_link.invite_link}",
            disable_web_page_preview=False
        )
        
        logger.info(f"Пользователь {user_id} добавлен в закрытый канал")
        return True
    except Exception as e:
        logger.error(f"Ошибка при добавлении пользователя {user_id} в канал: {str(e)}")
        return False

# Функция для удаления пользователя из закрытого канала
def remove_user_from_channel(user_id):
    try:
        # Пытаемся удалить пользователя из канала
        bot.ban_chat_member(
            chat_id=CHANNEL_ID,
            user_id=user_id
        )
        
        # Сразу разбаниваем, чтобы пользователь мог вернуться при повторной подписке
        bot.unban_chat_member(
            chat_id=CHANNEL_ID,
            user_id=user_id,
            only_if_banned=True
        )
        
        bot.send_message(
            user_id,
            "Ваша подписка отменена. Доступ к закрытому каналу прекращен."
        )
        
        logger.info(f"Пользователь {user_id} удален из закрытого канала")
        return True
    except Exception as e:
        logger.error(f"Ошибка при удалении пользователя {user_id} из канала: {str(e)}")
        return False

# Функция для отправки уведомления администратору
def notify_admin(message):
    if not ADMIN_ID:
        logger.warning("ID администратора не указан. Уведомление не отправлено.")
        return False
    
    try:
        bot.send_message(
            ADMIN_ID,
            message,
            parse_mode="HTML"
        )
        logger.info(f"Уведомление отправлено администратору: {message[:50]}...")
        return True
    except Exception as e:
        logger.error(f"Ошибка при отправке уведомления администратору: {str(e)}")
        return False

# Обновляем функцию check_new_payments для отправки уведомлений администратору
def check_new_payments():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    # Получаем последние записи об успешных платежах, которые еще не обработаны
    cursor.execute('''
    SELECT id, buyer_email, product_title, amount, currency, contract_id, parent_contract_id, event_type, status, timestamp 
    FROM payments 
    WHERE processed = 0
    ''')
    
    new_payments = cursor.fetchall()
    
    for payment in new_payments:
        payment_id, email, product_title, amount, currency, contract_id, parent_contract_id, event_type, status, timestamp = payment
        
        # Извлекаем Telegram ID из email
        user_id = email.split('@')[0]
        
        try:
            # Формируем сообщение для администратора
            admin_message = f"<b>Новая операция по подписке</b>\n\n" \
                           f"<b>Пользователь:</b> {user_id}\n" \
                           f"<b>Продукт:</b> {product_title}\n" \
                           f"<b>Сумма:</b> {amount} {currency}\n" \
                           f"<b>Тип события:</b> {event_type}\n" \
                           f"<b>Статус:</b> {status}\n" \
                           f"<b>Дата:</b> {timestamp}\n" \
                           f"<b>ID контракта:</b> {contract_id}"
            
            # Отправляем уведомление администратору
            notify_admin(admin_message)
            
            # Если платеж успешный, отправляем уведомление пользователю и добавляем в канал
            if status == 'subscription-active' or status == 'active':
                # Отправляем уведомление пользователю
                bot.send_message(
                    user_id,
                    f"Поздравляем! Ваша подписка '{product_title}' успешно оплачена.\n"
                    f"Сумма: {amount} {currency}"
                )
                
                # Добавляем пользователя в закрытый канал
                add_user_to_channel(user_id)
                
                logger.info(f"Отправлено уведомление пользователю {user_id} об успешной оплате")
            elif status == 'subscription-failed' or status == 'failed':
                # Отправляем уведомление о неудачной оплате
                cursor.execute('SELECT error_message FROM payments WHERE id = ?', (payment_id,))
                error_message = cursor.fetchone()[0]
                
                bot.send_message(
                    user_id,
                    f"К сожалению, оплата подписки '{product_title}' не удалась.\n"
                    f"Причина: {error_message}\n\n"
                    f"Вы можете попробовать снова, используя команду /subscribe"
                )
                
                logger.info(f"Отправлено уведомление пользователю {user_id} о неудачной оплате")
            
            # Отмечаем платеж как обработанный
            cursor.execute('UPDATE payments SET processed = 1 WHERE id = ?', (payment_id,))
            conn.commit()
            
        except Exception as e:
            logger.error(f"Ошибка при обработке платежа {payment_id}: {str(e)}")
    
    conn.close()

# Обновляем структуру базы данных для отслеживания обработанных платежей
def update_db_structure():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    # Проверяем, существует ли таблица payments
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='payments'")
    table_exists = cursor.fetchone()
    
    if not table_exists:
        # Таблица не существует, создаем её
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_type TEXT NOT NULL,
            product_id TEXT NOT NULL,
            product_title TEXT NOT NULL,
            buyer_email TEXT NOT NULL,
            contract_id TEXT NOT NULL,
            parent_contract_id TEXT,
            amount REAL NOT NULL,
            currency TEXT NOT NULL,
            timestamp TEXT NOT NULL,
            status TEXT NOT NULL,
            error_message TEXT,
            raw_data TEXT NOT NULL,
            received_at TEXT NOT NULL,
            processed INTEGER DEFAULT 0
        )
        ''')
        conn.commit()
        logger.info("Создана таблица payments в базе данных")
    else:
        # Таблица существует, проверяем наличие колонки processed
        cursor.execute("PRAGMA table_info(payments)")
        columns = [column[1] for column in cursor.fetchall()]
        
        if "processed" not in columns:
            cursor.execute("ALTER TABLE payments ADD COLUMN processed INTEGER DEFAULT 0")
            conn.commit()
            logger.info("Структура базы данных обновлена: добавлена колонка processed")
    
    conn.close()

# Обработчики команд
@bot.message_handler(commands=['start'])
def start_command(message):
    user_id = message.from_user.id
    username = message.from_user.username or f"user_{user_id}"
    
    logger.info(f"Пользователь {username} (ID: {user_id}) запустил бота")
    
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    btn_subscribe = types.KeyboardButton('Оформить подписку')
    btn_status = types.KeyboardButton('Статус подписки')
    markup.add(btn_subscribe, btn_status)
    
    bot.send_message(
        message.chat.id,
        f"Привет, {username}! Я бот для оформления подписки. Выберите действие:",
        reply_markup=markup
    )

@bot.message_handler(commands=['subscribe'])
def subscribe_command(message):
    user_id = message.from_user.id
    username = message.from_user.username or f"user_{user_id}"
    
    logger.info(f"Пользователь {username} (ID: {user_id}) запросил оформление подписки")
    
    # Проверяем, есть ли уже активная подписка
    subscription = check_subscription_status(user_id)
    if subscription["status"] == "active":
        bot.send_message(
            message.chat.id,
            "У вас уже есть активная подписка!"
        )
        return
    
    # Получаем список доступных подписок
    subscriptions = get_available_subscriptions()
    if not subscriptions:
        bot.send_message(
            message.chat.id,
            "Произошла ошибка при получении списка подписок. Пожалуйста, попробуйте позже."
        )
        return
    
    # Для каждой подписки создаем отдельное сообщение с кнопками периодов
    for sub in subscriptions:
        markup = types.InlineKeyboardMarkup(row_width=2)
        
        # Создаем кнопки для каждого периода
        period_buttons = []
        for price in sub["prices"]:
            period_text = PERIOD_TRANSLATIONS.get(price["periodicity"], price["periodicity"])
            # Получаем цену в рублях для отображения в кнопке
            rub_amount = price["currencies"].get("RUB", 0)
            button_text = f"{period_text} - {rub_amount} ₽"
            callback_data = f"pay|{sub['offer_id']}|{price['periodicity']}"
            period_buttons.append(
                types.InlineKeyboardButton(text=button_text, callback_data=callback_data)
            )
        
        markup.add(*period_buttons)
        
        # Отправляем информацию о подписке с кнопками выбора периода
        message_text = f"<b>{sub['name']}</b>\n\n{sub['description']}\n\nВыберите период подписки:"
        bot.send_message(
            message.chat.id,
            message_text,
            reply_markup=markup,
            parse_mode="HTML"
        )

@bot.message_handler(commands=['status'])
def status_command(message):
    user_id = message.from_user.id
    username = message.from_user.username or f"user_{user_id}"
    
    logger.info(f"Пользователь {username} (ID: {user_id}) запросил статус подписки")
    
    subscription = check_subscription_status(user_id)
    
    if subscription["status"] == "active":
        data = subscription["data"]
        
        # Создаем кнопку для отмены подписки
        markup = types.InlineKeyboardMarkup()
        cancel_button = types.InlineKeyboardButton(
            text="Отменить подписку", 
            callback_data=f"cancel_{data[5] or data[6]}"  # contract_id или parent_contract_id
        )
        markup.add(cancel_button)
        
        bot.send_message(
            message.chat.id,
            f"У вас есть активная подписка!\n"
            f"Продукт: {data[3]}\n"
            f"Дата активации: {data[9]}\n"
            f"Сумма: {data[7]} {data[8]}",
            reply_markup=markup
        )
    elif subscription["status"] == "failed":
        data = subscription["data"]
        bot.send_message(
            message.chat.id,
            f"Последняя попытка оплаты не удалась.\n"
            f"Причина: {data[11]}\n"
            f"Дата: {data[9]}\n\n"
            f"Вы можете попробовать оформить подписку снова, используя команду /subscribe"
        )
    else:
        bot.send_message(
            message.chat.id,
            "У вас нет активной подписки. Используйте команду /subscribe для оформления."
        )

@bot.callback_query_handler(func=lambda call: call.data.startswith('cancel_'))
def cancel_subscription_callback(call):
    user_id = call.from_user.id
    username = call.from_user.username or f"user_{user_id}"
    contract_id = call.data.split('_')[1]
    
    logger.info(f"Пользователь {username} (ID: {user_id}) запросил отмену подписки {contract_id}")
    
    # Уведомляем администратора о попытке отмены подписки
    admin_message = f"<b>Попытка отмены подписки</b>\n\n" \
                   f"<b>Пользователь:</b> {username} (ID: {user_id})\n" \
                   f"<b>ID контракта:</b> {contract_id}"
    notify_admin(admin_message)
    
    # Сообщаем пользователю, что запрос обрабатывается
    bot.answer_callback_query(call.id, "Обрабатываем ваш запрос...")
    bot.edit_message_text(
        "Обрабатываем запрос на отмену подписки...",
        call.message.chat.id,
        call.message.message_id
    )
    
    # Отменяем подписку
    if cancel_subscription(user_id, contract_id):
        # Удаляем пользователя из канала только при успешной отмене
        remove_user_from_channel(user_id)
        
        bot.edit_message_text(
            "Ваша подписка успешно отменена. Доступ к закрытому каналу прекращен.",
            call.message.chat.id,
            call.message.message_id
        )
        
        # Уведомляем администратора об успешной отмене
        notify_admin(f"<b>Подписка успешно отменена</b>\n\n" \
                    f"<b>Пользователь:</b> {username} (ID: {user_id})\n" \
                    f"<b>ID контракта:</b> {contract_id}")
    else:
        bot.edit_message_text(
            "Произошла ошибка при отмене подписки. Пожалуйста, попробуйте позже или обратитесь в поддержку.",
            call.message.chat.id,
            call.message.message_id
        )
        
        # Уведомляем администратора об ошибке
        notify_admin(f"<b>ОШИБКА:</b> Не удалось отменить подписку для пользователя {username} (ID: {user_id}), контракт {contract_id}")

@bot.callback_query_handler(func=lambda call: call.data.startswith('pay|'))
def process_payment_callback(call):
    user_id = call.from_user.id
    username = call.from_user.username or f"user_{user_id}"
    
    try:
        # Разбираем данные из callback
        parts = call.data.split('|')
        if len(parts) != 3:
            raise ValueError("Неверный формат данных callback")
        
        _, offer_id, periodicity = parts
        
        # Получаем информацию о подписке для отображения цен
        subscriptions = get_available_subscriptions()
        if not subscriptions:
            raise ValueError("Не удалось получить информацию о подписке")
        
        # Ищем нужную подписку и период
        subscription = next((sub for sub in subscriptions if sub["offer_id"] == offer_id), None)
        if not subscription:
            raise ValueError("Подписка не найдена")
        
        price_info = next((p for p in subscription["prices"] if p["periodicity"] == periodicity), None)
        if not price_info:
            raise ValueError("Информация о ценах не найдена")
        
        # Создаем кнопки выбора валюты
        markup = types.InlineKeyboardMarkup(row_width=1)
        currency_buttons = []
        
        for currency, amount in price_info["currencies"].items():
            currency_symbol = CURRENCY_TRANSLATIONS.get(currency, currency)
            # Убираем информацию о методе оплаты из текста кнопки
            button_text = f"Оплатить {amount} {currency_symbol}"
            callback_data = f"currency|{offer_id}|{periodicity}|{currency}"
            currency_buttons.append(
                types.InlineKeyboardButton(text=button_text, callback_data=callback_data)
            )
        
        # Добавляем каждую кнопку отдельно
        for button in currency_buttons:
            markup.add(button)
        
        # Добавляем кнопку "Назад"
        back_button = types.InlineKeyboardButton(
            text="← Назад к выбору периода",
            callback_data=f"back_to_subscription|{offer_id}"
        )
        markup.add(back_button)
        
        period_text = PERIOD_TRANSLATIONS.get(periodicity, periodicity)
        bot.edit_message_text(
            f"Выберите способ оплаты подписки на {period_text}:",
            call.message.chat.id,
            call.message.message_id,
            reply_markup=markup
        )
        
    except Exception as e:
        logger.error(f"Ошибка при обработке callback выбора периода: {str(e)}")
        bot.answer_callback_query(
            call.id,
            "Произошла ошибка. Пожалуйста, попробуйте позже."
        )

# Добавляем обработчик для кнопок выбора валюты
@bot.callback_query_handler(func=lambda call: call.data.startswith('currency|'))
def process_currency_callback(call):
    user_id = call.from_user.id
    username = call.from_user.username or f"user_{user_id}"
    
    try:
        # Разбираем данные из callback
        parts = call.data.split('|')
        if len(parts) != 4:
            raise ValueError("Неверный формат данных callback")
        
        _, offer_id, periodicity, currency = parts
        
        # Создаем ссылку на оплату
        payment_data = create_payment_link(user_id, offer_id, periodicity, currency)
        
        if payment_data and "paymentUrl" in payment_data:
            markup = types.InlineKeyboardMarkup()
            payment_button = types.InlineKeyboardButton(
                text="Оплатить подписку", 
                url=payment_data["paymentUrl"]
            )
            markup.add(payment_button)
            
            # Добавляем кнопку "Назад к выбору валюты"
            back_button = types.InlineKeyboardButton(
                text="← Назад к выбору валюты",
                callback_data=f"pay|{offer_id}|{periodicity}"
            )
            markup.add(back_button)
            
            period_text = PERIOD_TRANSLATIONS.get(periodicity, periodicity)
            currency_symbol = CURRENCY_TRANSLATIONS.get(currency, currency)
            bot.edit_message_text(
                f"Для оплаты подписки на {period_text} ({currency_symbol}) нажмите на кнопку ниже:",
                call.message.chat.id,
                call.message.message_id,
                reply_markup=markup
            )
            
            logger.info(f"Создана ссылка на оплату для пользователя {username} (ID: {user_id})")
            
            # Уведомляем администратора
            admin_message = f"<b>Создана ссылка на оплату</b>\n\n" \
                          f"<b>Пользователь:</b> {username} (ID: {user_id})\n" \
                          f"<b>Период:</b> {period_text}\n" \
                          f"<b>Валюта:</b> {currency}\n" \
                          f"<b>ID счета:</b> {payment_data.get('id', 'Н/Д')}"
            notify_admin(admin_message)
        else:
            bot.answer_callback_query(
                call.id,
                "Произошла ошибка при создании ссылки на оплату. Пожалуйста, попробуйте позже."
            )
            logger.error(f"Не удалось создать ссылку на оплату для пользователя {username} (ID: {user_id})")
    
    except Exception as e:
        logger.error(f"Ошибка при обработке callback выбора валюты: {str(e)}")
        bot.answer_callback_query(
            call.id,
            "Произошла ошибка. Пожалуйста, попробуйте позже."
        )

# Обновляем обработчик для кнопки "Назад к подписке"
@bot.callback_query_handler(func=lambda call: call.data.startswith('back_to_subscription|'))
def process_back_to_subscription(call):
    try:
        offer_id = call.data.split('|')[1]
        
        # Удаляем текущее сообщение
        bot.delete_message(call.message.chat.id, call.message.message_id)
        
        # Получаем информацию о подписке
        subscriptions = get_available_subscriptions()
        if not subscriptions:
            raise ValueError("Не удалось получить информацию о подписке")
        
        subscription = next((sub for sub in subscriptions if sub["offer_id"] == offer_id), None)
        if not subscription:
            raise ValueError("Подписка не найдена")
        
        # Создаем новое сообщение с кнопками периодов
        markup = types.InlineKeyboardMarkup(row_width=2)
        period_buttons = []
        
        for price in subscription["prices"]:
            period_text = PERIOD_TRANSLATIONS.get(price["periodicity"], price["periodicity"])
            rub_amount = price["currencies"].get("RUB", 0)
            button_text = f"{period_text} - {rub_amount} ₽"
            callback_data = f"pay|{subscription['offer_id']}|{price['periodicity']}"
            period_buttons.append(
                types.InlineKeyboardButton(text=button_text, callback_data=callback_data)
            )
        
        markup.add(*period_buttons)
        
        message_text = f"<b>{subscription['name']}</b>\n\n{subscription['description']}\n\nВыберите период подписки:"
        bot.send_message(
            call.message.chat.id,
            message_text,
            reply_markup=markup,
            parse_mode="HTML"
        )
        
    except Exception as e:
        logger.error(f"Ошибка при возврате к выбору периода: {str(e)}")
        bot.answer_callback_query(
            call.id,
            "Произошла ошибка. Пожалуйста, попробуйте позже."
        )

@bot.message_handler(content_types=['text'])
def text_handler(message):
    if message.text == 'Оформить подписку':
        subscribe_command(message)
    elif message.text == 'Статус подписки':
        status_command(message)
    else:
        bot.send_message(
            message.chat.id,
            "Используйте кнопки или команды /start, /subscribe, /status"
        )

# Функция для периодической проверки новых платежей
def check_payments_periodically():
    while True:
        try:
            # Проверяем существование таблицы перед запросом
            conn = sqlite3.connect(DB_PATH)
            cursor = conn.cursor()
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='payments'")
            table_exists = cursor.fetchone()
            conn.close()
            
            if table_exists:
                check_new_payments()
            else:
                logger.warning("Таблица payments еще не создана. Пропускаем проверку платежей.")
                
        except Exception as e:
            logger.error(f"Ошибка при проверке новых платежей: {str(e)}")
        
        # Проверяем каждые 60 секунд
        time.sleep(60)

# Функция для запуска бота в отдельном потоке
def run_bot():
    try:
        logger.info("Запуск бота...")
        
        # Проверяем наличие токена
        if not BOT_TOKEN:
            logger.error("Не указан токен бота (BOT_TOKEN). Бот не будет запущен.")
            return
            
        if not CHANNEL_ID:
            logger.warning("Не указан ID канала (CHANNEL_ID). Функции работы с каналом будут недоступны.")
        
        # Обновляем структуру БД
        update_db_structure()
        
        # Запускаем периодическую проверку платежей в отдельном потоке
        payment_thread = threading.Thread(target=check_payments_periodically)
        payment_thread.daemon = True
        payment_thread.start()
        
        # Запускаем бота
        bot.polling(none_stop=True, interval=0)
    except Exception as e:
        logger.error(f"Ошибка при запуске бота: {str(e)}", exc_info=True)

# Запуск бота в отдельном потоке
if __name__ == "__main__":
    bot_thread = threading.Thread(target=run_bot)
    bot_thread.daemon = True
    bot_thread.start()
    
    # Держим основной поток активным
    try:
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        logger.info("Бот остановлен")
