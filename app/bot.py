import os
import logging
import sqlite3
import requests
import telebot
from telebot import types
import threading
import time
from pathlib import Path
from datetime import datetime, timedelta, timezone
import json

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
logging.getLogger("payment_bot").setLevel(logging.DEBUG)
# Получение настроек из переменных окружения
BOT_TOKEN = os.getenv("BOT_TOKEN")
LAVA_API_KEY = os.getenv("LAVA_API_KEY")
CHANNEL_ID = os.getenv("CHANNEL_ID")
ADMIN_ID = os.getenv("ADMIN_ID")
DB_PATH = DATA_DIR / "lava_payments.db"

# Список привилегированных пользователей (всегда имеют доступ к каналу)
PRIVILEGED_USERS = os.getenv("PRIVILEGED_USERS", "").split(",")  # ID через запятую в переменной окружения
if ADMIN_ID:  # Автоматически добавляем админа в список привилегированных пользователей
    PRIVILEGED_USERS.append(ADMIN_ID)

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

# Добавляем константы для настройки уведомлений
GRACE_PERIOD_DAYS = 3  # Дней отсрочки после окончания подписки
NOTIFY_BEFORE_DAYS = [7, 3, 1]  # За сколько дней уведомлять об окончании подписки

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
            member_limit=1,
            expire_date=int(time.time()) + 86400
        )
        
        # Получаем информацию о последнем платеже
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute('''
        SELECT id, timestamp, raw_data
        FROM payments 
        WHERE buyer_email = ? 
        AND (status = 'subscription-active' OR status = 'active')
        ORDER BY timestamp DESC
        LIMIT 1
        ''', (f"{user_id}@t.me",))
        
        payment = cursor.fetchone()
        if payment:
            payment_id, timestamp, raw_data = payment
            
            # Определяем дату окончания подписки
            try:
                raw_data = json.loads(raw_data)
                periodicity = raw_data.get('periodicity', 'MONTHLY')
                days = {
                    "MONTHLY": 30,
                    "PERIOD_90_DAYS": 90,
                    "PERIOD_180_DAYS": 180,
                    "PERIOD_YEAR": 365
                }.get(periodicity, 30)
                
                end_date = (datetime.fromisoformat(timestamp.replace('Z', '+00:00')) + 
                           timedelta(days=days)).isoformat()
            except:
                end_date = None
            
            # Добавляем или обновляем запись в channel_members
            cursor.execute('''
            INSERT OR REPLACE INTO channel_members 
            (user_id, status, subscription_end_date, last_payment_id)
            VALUES (?, 'active', ?, ?)
            ''', (user_id, end_date, payment_id))
            
            conn.commit()
        
        conn.close()
        
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

# Обновляем функцию remove_user_from_channel
def remove_user_from_channel(user_id):
    try:
        logger.debug(f"Попытка удаления пользователя {user_id} из канала {CHANNEL_ID}")
        
        # Проверяем права бота в канале
        bot_member = bot.get_chat_member(CHANNEL_ID, bot.get_me().id)
        logger.debug(f"Права бота в канале: {bot_member.status}")
        if bot_member.status != 'administrator':
            logger.error(f"Бот не является администратором канала {CHANNEL_ID}")
            return False
        
        # Проверяем текущий статус пользователя
        current_status = bot.get_chat_member(CHANNEL_ID, user_id)
        logger.debug(f"Текущий статус пользователя {user_id} в канале: {current_status.status}")
        
        # Пытаемся удалить пользователя
        result = bot.ban_chat_member(CHANNEL_ID, user_id)
        logger.debug(f"Результат удаления пользователя: {result}")
        
        # Сразу разбаниваем, чтобы пользователь мог вернуться после оплаты
        bot.unban_chat_member(CHANNEL_ID, user_id)
        logger.debug(f"Пользователь разбанен для возможности повторного входа")
        
        return result
    except Exception as e:
        logger.error(f"Ошибка при удалении пользователя {user_id} из канала: {str(e)}", exc_info=True)
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

# Обновляем функцию update_db_structure
def update_db_structure():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    # Создаем таблицу для платежей, если её нет
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
    
    # Создаем таблицу для участников канала
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS channel_members (
        user_id INTEGER PRIMARY KEY,
        added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        status TEXT DEFAULT 'active',
        subscription_end_date TIMESTAMP,
        last_payment_id INTEGER,
        FOREIGN KEY (last_payment_id) REFERENCES payments(id)
    )
    ''')
    
    conn.commit()
    conn.close()

# Обработчики команд должны быть перед обработчиком текстовых сообщений

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

# Добавляем функцию для расчета оставшихся дней подписки
def calculate_days_left(timestamp, periodicity):
    # Преобразуем строку в datetime
    start_date = datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
    
    # Определяем длительность периода в днях
    period_days = {
        "MONTHLY": 30,
        "PERIOD_90_DAYS": 90,
        "PERIOD_180_DAYS": 180,
        "PERIOD_YEAR": 365
    }
    
    days = period_days.get(periodicity, 30)  # По умолчанию 30 дней
    end_date = start_date + timedelta(days=days)
    
    # Вычисляем оставшееся время
    days_left = (end_date - datetime.now(end_date.tzinfo)).days
    
    return max(0, days_left)  # Возвращаем 0, если подписка уже закончилась

# Обновляем функцию проверки подписок
def check_subscription_expiration():
    try:
        logger.debug("Начало проверки сроков подписок")
        
        # Получаем всех активных пользователей канала
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        
        # Получаем пользователей с активным статусом
        cursor.execute('''
        SELECT 
            cm.user_id,
            cm.subscription_end_date,
            p.status,
            p.event_type
        FROM channel_members cm
        LEFT JOIN payments p ON p.id = cm.last_payment_id
        WHERE cm.status = 'active'
        ''')
        
        active_members = cursor.fetchall()
        logger.debug(f"Найдено {len(active_members)} активных пользователей")
        
        current_time = datetime.now(timezone.utc)
        
        for member in active_members:
            user_id = member[0]
            end_date = datetime.fromisoformat(member[1].replace('Z', '+00:00'))
            payment_status = member[2]
            event_type = member[3]
            
            # Вычисляем оставшиеся дни
            days_left = (end_date - current_time).days
            
            try:
                # Проверяем, является ли пользователь участником канала
                chat_member = bot.get_chat_member(CHANNEL_ID, user_id)
                if chat_member.status not in ['left', 'kicked']:
                    
                    # Если подписка истекла и закончился льготный период
                    if days_left < -GRACE_PERIOD_DAYS:
                        logger.info(
                            f"Удаление пользователя {user_id} из канала: "
                            f"подписка истекла {member[1]}, "
                            f"прошло дней после окончания: {-days_left}"
                        )
                        
                        # Удаляем пользователя из канала
                        result = remove_user_from_channel(user_id)
                        
                        if result:
                            # Обновляем статус в БД
                            cursor.execute('''
                            UPDATE channel_members 
                            SET status = 'removed' 
                            WHERE user_id = ?
                            ''', (user_id,))
                            
                            # Уведомляем пользователя
                            bot.send_message(
                                user_id,
                                "❌ Ваша подписка истекла, и льготный период подошел к концу.\n"
                                "Доступ к каналу прекращен.\n"
                                "Для возобновления доступа используйте команду /subscribe"
                            )
                            
                            # Уведомляем администратора
                            notify_admin(
                                f"<b>Пользователь удален из канала</b>\n\n"
                                f"<b>ID пользователя:</b> {user_id}\n"
                                f"<b>Причина:</b> Истекла подписка и льготный период\n"
                                f"<b>Дата окончания:</b> {member[1]}"
                            )
                    
                    # Если подписка истекла, но еще действует льготный период
                    elif days_left < 0:
                        days_grace_left = GRACE_PERIOD_DAYS + days_left
                        bot.send_message(
                            user_id,
                            f"⚠️ Ваша подписка истекла!\n\n"
                            f"У вас есть еще {days_grace_left} дней льготного периода.\n"
                            f"После этого доступ к каналу будет прекращен.\n\n"
                            f"Для продления подписки используйте команду /subscribe"
                        )
                    
                    # Уведомления о скором окончании подписки
                    elif days_left in NOTIFY_BEFORE_DAYS:
                        bot.send_message(
                            user_id,
                            f"ℹ️ Ваша подписка закончится через {days_left} дней.\n"
                            f"Не забудьте продлить её, чтобы сохранить доступ к каналу.\n\n"
                            f"Для продления используйте команду /subscribe"
                        )
            
            except Exception as e:
                logger.error(f"Ошибка при проверке пользователя {user_id}: {str(e)}", exc_info=True)
                continue
        
        conn.commit()
        conn.close()
        logger.info("Проверка участников канала завершена")
            
    except Exception as e:
        logger.error(f"Ошибка при проверке сроков подписок: {str(e)}", exc_info=True)

# Обновляем функцию status_command для красивого вывода даты
@bot.message_handler(commands=['status'])
def status_command(message):
    user_id = message.from_user.id
    
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        
        # Получаем информацию о последнем платеже и статусе подписки
        cursor.execute('''
        WITH LastPayments AS (
            SELECT 
                buyer_email,
                status,
                timestamp,
                raw_data,
                ROW_NUMBER() OVER (PARTITION BY buyer_email ORDER BY timestamp DESC) as rn
            FROM payments
            WHERE buyer_email = ?
        )
        SELECT status, timestamp, raw_data
        FROM LastPayments
        WHERE rn = 1
        ''', (f"{user_id}@t.me",))
        
        payment_info = cursor.fetchone()
        
        # Создаем клавиатуру
        markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
        markup.add(types.KeyboardButton('Оформить подписку'))
        
        if payment_info:
            status, timestamp, raw_data = payment_info
            
            # Преобразуем timestamp в читаемый формат
            activation_date = datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
            formatted_activation = activation_date.strftime("%d.%m.%Y %H:%M")
            
            # Получаем дату окончания подписки
            try:
                raw_data = json.loads(raw_data)
                periodicity = raw_data.get('periodicity', 'MONTHLY')
                days = {
                    "MONTHLY": 30,
                    "PERIOD_90_DAYS": 90,
                    "PERIOD_180_DAYS": 180,
                    "PERIOD_YEAR": 365
                }.get(periodicity, 30)
                
                end_date = activation_date + timedelta(days=days)
                formatted_end_date = end_date.strftime("%d.%m.%Y %H:%M")
                days_left = (end_date - datetime.now(end_date.tzinfo)).days
                
                subscription_type = PERIOD_TRANSLATIONS.get(periodicity, periodicity)
                
                if status in ['subscription-active', 'active']:
                    message_text = (
                        f"✅ <b>Ваша подписка активна</b>\n\n"
                        f"📅 Дата активации: {formatted_activation}\n"
                        f"⏳ Дата окончания: {formatted_end_date}\n"
                        f"📊 Осталось дней: {max(0, days_left)}\n"
                        f"📦 Тип подписки: {subscription_type}"
                    )
                    # Добавляем кнопку отмены подписки для активных подписок
                    markup.add(types.KeyboardButton('Отменить подписку'))
                else:
                    message_text = (
                        f"❌ <b>Ваша подписка неактивна</b>\n\n"
                        f"📅 Последний платеж: {formatted_activation}\n"
                        f"ℹ️ Статус: {status}\n\n"
                        f"Для оформления новой подписки используйте команду /subscribe"
                    )
            except Exception as e:
                logger.error(f"Ошибка при обработке данных подписки: {str(e)}")
                message_text = "❌ Ошибка при получении информации о подписке"
        else:
            message_text = (
                "❌ <b>У вас нет активной подписки</b>\n\n"
                "Для оформления подписки используйте команду /subscribe"
            )
        
        bot.reply_to(message, message_text, parse_mode="HTML", reply_markup=markup)
        
    except Exception as e:
        logger.error(f"Ошибка при проверке статуса подписки: {str(e)}")
        bot.reply_to(message, "❌ Произошла ошибка при проверке статуса подписки")
    finally:
        conn.close()

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

@bot.message_handler(commands=['test_payment'])
def test_payment_command(message):
    # Проверяем, что команду отправил администратор
    if str(message.from_user.id) != ADMIN_ID:
        bot.reply_to(message, "Эта команда доступна только администратору")
        return
    
    # Проверяем, есть ли ID пользователя в сообщении
    args = message.text.split()
    if len(args) > 1:
        try:
            user_id = int(args[1])
        except ValueError:
            bot.reply_to(message, "Неверный формат ID пользователя. Используйте числовой ID.")
            return
    else:
        user_id = message.from_user.id
    
    # Создаем тестовый платеж
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    # Текущее время в формате ISO
    current_time = datetime.utcnow().isoformat() + 'Z'
    
    # Тестовые данные платежа
    test_payment = {
        'event_type': 'payment.success',
        'product_id': 'test_product',
        'product_title': 'Тестовая подписка',
        'buyer_email': f"{user_id}@t.me",
        'contract_id': f"test_{int(time.time())}",
        'parent_contract_id': None,
        'amount': 100,
        'currency': 'RUB',
        'timestamp': current_time,
        'status': 'active',
        'error_message': None,
        'raw_data': json.dumps({
            'periodicity': 'MONTHLY',
            'test_payment': True
        })
    }
    
    try:
        # Добавляем тестовый платеж в БД
        cursor.execute('''
        INSERT INTO payments (
            event_type, product_id, product_title, buyer_email,
            contract_id, parent_contract_id, amount, currency,
            timestamp, status, error_message, raw_data, received_at, processed
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
        ''', (
            test_payment['event_type'],
            test_payment['product_id'],
            test_payment['product_title'],
            test_payment['buyer_email'],
            test_payment['contract_id'],
            test_payment['parent_contract_id'],
            test_payment['amount'],
            test_payment['currency'],
            test_payment['timestamp'],
            test_payment['status'],
            test_payment['error_message'],
            test_payment['raw_data'],
            current_time
        ))
        
        conn.commit()
        conn.close()
        
        bot.reply_to(message, 
            f"Тестовый платеж создан успешно для пользователя {user_id}!\n"
            "Бот должен обработать его в течение минуты.\n"
            "Используйте /status для проверки статуса подписки."
        )
        
        logger.info(f"Создан тестовый платеж для пользователя {user_id}")
        
    except Exception as e:
        conn.rollback()
        conn.close()
        bot.reply_to(message, f"Ошибка при создании тестового платежа: {str(e)}")
        logger.error(f"Ошибка при создании тестового платежа: {str(e)}")

# Добавляем команду для эмуляции неуспешного платежа
@bot.message_handler(commands=['test_failed_payment'])
def test_failed_payment_command(message):
    # Проверяем, что команду отправил администратор
    if str(message.from_user.id) != ADMIN_ID:
        bot.reply_to(message, "Эта команда доступна только администратору")
        return
    
    # Проверяем, есть ли ID пользователя в сообщении
    args = message.text.split()
    if len(args) > 1:
        try:
            user_id = int(args[1])
        except ValueError:
            bot.reply_to(message, "Неверный формат ID пользователя. Используйте числовой ID.")
            return
    else:
        user_id = message.from_user.id
    
    # Создаем тестовый неуспешный платеж
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    current_time = datetime.utcnow().isoformat() + 'Z'
    
    test_payment = {
        'event_type': 'payment.failed',
        'product_id': 'test_product',
        'product_title': 'Тестовая подписка',
        'buyer_email': f"{user_id}@t.me",
        'contract_id': f"test_failed_{int(time.time())}",
        'parent_contract_id': None,
        'amount': 100,
        'currency': 'RUB',
        'timestamp': current_time,
        'status': 'failed',
        'error_message': 'Тестовая ошибка оплаты',
        'raw_data': json.dumps({
            'periodicity': 'MONTHLY',
            'test_payment': True
        })
    }
    
    try:
        cursor.execute('''
        INSERT INTO payments (
            event_type, product_id, product_title, buyer_email,
            contract_id, parent_contract_id, amount, currency,
            timestamp, status, error_message, raw_data, received_at, processed
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
        ''', (
            test_payment['event_type'],
            test_payment['product_id'],
            test_payment['product_title'],
            test_payment['buyer_email'],
            test_payment['contract_id'],
            test_payment['parent_contract_id'],
            test_payment['amount'],
            test_payment['currency'],
            test_payment['timestamp'],
            test_payment['status'],
            test_payment['error_message'],
            test_payment['raw_data'],
            current_time
        ))
        
        conn.commit()
        conn.close()
        
        bot.reply_to(message, 
            f"Тестовый неуспешный платеж создан для пользователя {user_id}!\n"
            "Бот должен обработать его в течение минуты.\n"
            "Используйте /status для проверки статуса."
        )
        
        logger.info(f"Создан тестовый неуспешный платеж для пользователя {user_id}")
        
    except Exception as e:
        conn.rollback()
        conn.close()
        bot.reply_to(message, f"Ошибка при создании тестового платежа: {str(e)}")
        logger.error(f"Ошибка при создании тестового платежа: {str(e)}")

# Добавляем команду для тестирования истечения подписки
@bot.message_handler(commands=['test_expire'])
def test_expire_command(message):
    # Проверяем, что команду отправил администратор
    if str(message.from_user.id) != ADMIN_ID:
        bot.reply_to(message, "Эта команда доступна только администратору")
        return
    
    # Проверяем, есть ли ID пользователя в сообщении
    args = message.text.split()
    if len(args) > 1:
        try:
            user_id = int(args[1])
        except ValueError:
            bot.reply_to(message, "Неверный формат ID пользователя. Используйте числовой ID.")
            return
    else:
        user_id = message.from_user.id
    
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        
        # Создаем тестовый платеж с истекшей датой
        current_time = (datetime.utcnow() - timedelta(days=31)).isoformat() + 'Z'
        
        test_payment = {
            'event_type': 'payment.success',
            'product_id': 'test_product',
            'product_title': 'Тестовая подписка',
            'buyer_email': f"{user_id}@t.me",
            'contract_id': f"test_expired_{int(time.time())}",
            'parent_contract_id': None,
            'amount': 100,
            'currency': 'RUB',
            'timestamp': current_time,
            'status': 'active',
            'error_message': None,
            'raw_data': json.dumps({
                'periodicity': 'MONTHLY',
                'test_payment': True
            })
        }
        
        # Добавляем тестовый платеж в БД
        cursor.execute('''
        INSERT INTO payments (
            event_type, product_id, product_title, buyer_email,
            contract_id, parent_contract_id, amount, currency,
            timestamp, status, error_message, raw_data, received_at, processed
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
        ''', (
            test_payment['event_type'],
            test_payment['product_id'],
            test_payment['product_title'],
            test_payment['buyer_email'],
            test_payment['contract_id'],
            test_payment['parent_contract_id'],
            test_payment['amount'],
            test_payment['currency'],
            test_payment['timestamp'],
            test_payment['status'],
            test_payment['error_message'],
            test_payment['raw_data'],
            current_time
        ))
        
        # Обновляем или добавляем запись в channel_members
        cursor.execute('''
        INSERT OR REPLACE INTO channel_members 
        (user_id, status, subscription_end_date, last_payment_id)
        VALUES (?, 'active', ?, last_insert_rowid())
        ''', (user_id, (datetime.fromisoformat(current_time.replace('Z', '+00:00')) + timedelta(days=30)).isoformat()))
        
        conn.commit()
        conn.close()
        
        bot.reply_to(message, 
            f"Создана тестовая истекшая подписка для пользователя {user_id}!\n"
            "Бот должен удалить пользователя при следующей проверке подписок (в течение 15 минут).\n"
            "Используйте /status для проверки статуса подписки."
        )
        
        logger.info(f"Создана тестовая истекшая подписка для пользователя {user_id}")
        
    except Exception as e:
        if conn:
            conn.rollback()
            conn.close()
        bot.reply_to(message, f"Ошибка при создании тестовой истекшей подписки: {str(e)}")
        logger.error(f"Ошибка при создании тестовой истекшей подписки: {str(e)}")

# Обработчик текстовых сообщений должен быть последним
@bot.message_handler(content_types=['text'])
def text_handler(message):
    if message.text == 'Оформить подписку':
        subscribe_command(message)
    elif message.text == 'Статус подписки':
        status_command(message)
    elif message.text == 'Отменить подписку':
        cancel_subscription(message)
    else:
        # Проверяем, является ли сообщение командой
        if message.text.startswith('/'):
            bot.reply_to(message, "Неизвестная команда. Доступные команды: /start, /subscribe, /status")
        else:
            bot.reply_to(message, "Используйте кнопки или команды /start, /subscribe, /status")

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

# Обновляем функцию проверки подписок
def check_subscriptions_periodically():
    while True:
        try:
            # Проверяем существование таблицы перед запросом
            conn = sqlite3.connect(DB_PATH)
            cursor = conn.cursor()
            cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='payments'")
            table_exists = cursor.fetchone()
            conn.close()
            
            if table_exists:
                check_subscription_expiration()
                logger.info("Выполнена проверка активных подписок")
            else:
                logger.warning("Таблица payments еще не создана. Пропускаем проверку подписок.")
                
        except Exception as e:
            logger.error(f"Ошибка при периодической проверке подписок: {str(e)}")
        
        # Проверяем каждые 15 минут
        time.sleep(900)

# Обновляем функцию run_bot для запуска периодической проверки подписок
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
        
        # Запускаем периодическую проверку подписок в отдельном потоке
        subscription_thread = threading.Thread(target=check_subscriptions_periodically)
        subscription_thread.daemon = True
        subscription_thread.start()
        
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
