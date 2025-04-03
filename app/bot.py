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
SUPPORT_USERNAME = os.getenv("SUPPORT_USERNAME", "support")  # Имя пользователя техподдержки в Telegram
CHANNEL_LINK = os.getenv("CHANNEL_LINK", "")  # Постоянная ссылка на канал

# В начале файла, где определяются другие константы
default_message = """Добро пожаловать в канал с бурятскими мультфильмами и сериалами.
"""

# Получаем сообщение из переменной окружения или используем значение по умолчанию
MAIN_MESSAGE = os.getenv("MAIN_MESSAGE", default_message).replace('\\n', '\n')

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
        logger.info(f"Создание ссылки на оплату для пользователя {user_id}")
        logger.debug(f"URL запроса: {url}")
        logger.debug(f"Заголовки: {headers}")
        logger.debug(f"Тело запроса: {payload}")
        
        response = requests.post(url, headers=headers, json=payload)
        logger.debug(f"Код ответа: {response.status_code}")
        logger.debug(f"Тело ответа: {response.text}")
        
        response.raise_for_status()
        response_data = response.json()
        
        logger.info(f"Успешно получен ответ от API: {response_data}")
        return response_data
        
    except requests.exceptions.RequestException as e:
        logger.error(f"Ошибка при отправке запроса: {str(e)}")
        if hasattr(e, 'response') and e.response is not None:
            logger.error(f"Код ответа: {e.response.status_code}")
            logger.error(f"Тело ответа: {e.response.text}")
        return None
    except Exception as e:
        logger.error(f"Неожиданная ошибка при создании ссылки: {str(e)}", exc_info=True)
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
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        
        # Сначала проверяем статус в channel_members
        cursor.execute('''
        SELECT status, subscription_end_date, last_payment_id
        FROM channel_members
        WHERE user_id = ? AND status = 'active'
        ''', (user_id,))
        
        member = cursor.fetchone()
        
        if member:
            status, end_date, last_payment_id = member
            
            # Проверяем, не истекла ли подписка
            if end_date and datetime.fromisoformat(end_date) > datetime.now(timezone.utc):
                # Получаем дополнительную информацию о платеже
                cursor.execute('''
                SELECT status, timestamp, event_type
                FROM payments
                WHERE id = ?
                ''', (last_payment_id,))
                payment = cursor.fetchone()
                
                return {
                    "status": "active",
                    "end_date": end_date,
                    "data": payment
                }
        
        # Если нет активной записи в channel_members, проверяем последний платеж
        cursor.execute('''
        SELECT p.status, p.timestamp, p.event_type, cm.subscription_end_date
        FROM payments p
        LEFT JOIN channel_members cm ON cm.last_payment_id = p.id
        WHERE p.buyer_email = ?
        AND p.event_type IN ('payment.success', 'subscription.recurring.payment.success')
        ORDER BY p.timestamp DESC
        LIMIT 1
        ''', (f"{user_id}@t.me",))
        
        payment = cursor.fetchone()
        conn.close()
        
        if payment:
            status, timestamp, event_type, end_date = payment
            
            # Проверяем, что подписка активна и не истекла
            is_active = (
                status in ['subscription-active', 'active'] and
                (end_date is None or datetime.fromisoformat(end_date) > datetime.now(timezone.utc))
            )
            
            return {
                "status": "active" if is_active else "inactive",
                "end_date": end_date,
                "data": payment
            }
        
        return {"status": "no_subscription"}
        
    except Exception as e:
        logger.error(f"Ошибка при проверке статуса подписки: {str(e)}")
        return {"status": "error", "error": str(e)}

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
            
            # Добавляем или обновляем запись в channel_members с текущей датой
            current_time = datetime.now(timezone.utc).isoformat()
            cursor.execute('''
            INSERT OR REPLACE INTO channel_members 
            (user_id, status, joined_at, subscription_end_date, last_payment_id)
            VALUES (?, 'active', ?, ?, ?)
            ''', (user_id, current_time, end_date, payment_id))
            
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
    try:
        conn = sqlite3.connect(DB_PATH, timeout=20)  # Увеличиваем timeout
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
                    
                    # Отправляем сообщение с кнопкой для входа в канал
                    channel_markup = types.InlineKeyboardMarkup(row_width=1)
                    channel_button = types.InlineKeyboardButton('📺 Войти в канал', url=CHANNEL_LINK)
                    channel_markup.add(channel_button)
                    
                    bot.send_message(
                        user_id,
                        "Твой доступ к каналу:",
                        reply_markup=channel_markup
                    )
                    
                    # Показываем основное меню
                    markup = types.InlineKeyboardMarkup(row_width=1)
                    btn_status = types.InlineKeyboardButton('ℹ️ Статус подписки', callback_data='show_status')
                    btn_channel = types.InlineKeyboardButton('📺 Перейти в канал', url=CHANNEL_LINK)
                    btn_support = types.InlineKeyboardButton('📞 Поддержка', url=f"https://t.me/{SUPPORT_USERNAME}")
                    markup.add(btn_status, btn_channel, btn_support)
                    
                    bot.send_message(
                        user_id,
                        MAIN_MESSAGE,
                        reply_markup=markup,
                        parse_mode="HTML"
                    )
                    
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
                    
                    # Показываем основное меню
                    markup = types.InlineKeyboardMarkup(row_width=1)
                    btn_subscribe = types.InlineKeyboardButton('💳 Оформить подписку', callback_data='show_subscribe')
                    btn_status = types.InlineKeyboardButton('ℹ️ Статус подписки', callback_data='show_status')
                    btn_support = types.InlineKeyboardButton('📞 Поддержка', url=f"https://t.me/{SUPPORT_USERNAME}")
                    markup.add(btn_subscribe, btn_status, btn_support)
                    
                    bot.send_message(
                        user_id,
                        MAIN_MESSAGE,
                        reply_markup=markup,
                        parse_mode="HTML"
                    )
                    
                    logger.info(f"Отправлено уведомление пользователю {user_id} о неудачной оплате")
                
                # Отмечаем платеж как обработанный
                cursor.execute('UPDATE payments SET processed = 1 WHERE id = ?', (payment_id,))
                conn.commit()
                
            except sqlite3.OperationalError as e:
                if "database is locked" in str(e):
                    logger.warning(f"База данных заблокирована, пропускаем обработку платежа {payment_id}")
                    continue
                raise
            except Exception as e:
                logger.error(f"Ошибка при обработке платежа {payment_id}: {str(e)}")
                
    finally:
        conn.close()

# Обработчики команд должны быть перед обработчиком текстовых сообщений

def show_subscription_menu(message):
    """
    Показывает меню выбора периода подписки
    """
    # Получаем список доступных подписок
    subscriptions = get_available_subscriptions()
    if not subscriptions:
        markup = types.InlineKeyboardMarkup(row_width=1)
        btn_menu = types.InlineKeyboardButton('🔙 Главное меню', callback_data='show_menu')
        markup.add(btn_menu)
        
        try:
            bot.edit_message_text(
                "Произошла ошибка при получении списка подписок. Пожалуйста, попробуйте позже.",
                chat_id=message.chat.id,
                message_id=message.message_id,
                reply_markup=markup
            )
        except Exception as e:
            bot.send_message(
                message.chat.id,
                "Произошла ошибка при получении списка подписок. Пожалуйста, попробуйте позже.",
                reply_markup=markup
            )
        return
    
    # Для каждой подписки создаем отдельное сообщение с кнопками периодов
    for sub in subscriptions:
        markup = types.InlineKeyboardMarkup(row_width=1)
        
        # Создаем кнопки для каждого периода
        for price in sub["prices"]:
            period_text = PERIOD_TRANSLATIONS.get(price["periodicity"], price["periodicity"])
            rub_amount = price["currencies"].get("RUB", 0)
            button_text = f"{period_text} - {rub_amount} ₽"
            
            # Сокращаем periodicity для callback_data
            short_period = {
                "MONTHLY": "1m",
                "PERIOD_90_DAYS": "3m",
                "PERIOD_180_DAYS": "6m",
                "PERIOD_YEAR": "1y"
            }.get(price["periodicity"], price["periodicity"])
            
            callback_data = f"p|{sub['offer_id']}|{short_period}"
            markup.add(types.InlineKeyboardButton(text=button_text, callback_data=callback_data))
        
        # Добавляем кнопку возврата в меню
        markup.add(types.InlineKeyboardButton('🔙 Главное меню', callback_data='show_menu'))
        
        message_text = f"<b>{sub['name']}</b>\n\n{sub['description']}\n\nВыберите период подписки:"
        
        try:
            bot.edit_message_text(
                message_text,
                chat_id=message.chat.id,
                message_id=message.message_id,
                reply_markup=markup,
                parse_mode="HTML"
            )
        except Exception as e:
            bot.send_message(
                message.chat.id,
                message_text,
                reply_markup=markup,
                parse_mode="HTML"
            )


@bot.message_handler(commands=['subscribe'])
def subscribe_command(message):
    # Правильно получаем ID пользователя
    user_id = message.from_user.id
    username = message.from_user.username or f"user_{user_id}"
    
    logger.info(f"Пользователь {username} (ID: {user_id}) запросил оформление подписки")
   
    # Проверяем, есть ли уже активная подписка
    subscription = check_subscription_status(user_id)
    if subscription["status"] == "active":
        markup = types.InlineKeyboardMarkup(row_width=1)
        btn_status = types.InlineKeyboardButton('ℹ️ Проверить статус', callback_data='show_status')
        btn_menu = types.InlineKeyboardButton('🔙 Главное меню', callback_data='show_menu')
        markup.add(btn_status)
        markup.add(btn_menu)
        
        try:
            bot.edit_message_text(
                "У вас уже есть активная подписка!",
                chat_id=message.chat.id,
                message_id=message.message_id,
                reply_markup=markup
            )
        except Exception as e:
            bot.send_message(
                message.chat.id,
                "У вас уже есть активная подписка!",
                reply_markup=markup
            )
        return
    
    show_subscription_menu(message)

@bot.message_handler(commands=['start'])
def start_command(message):
    user_id = message.from_user.id
    username = message.from_user.username or f"user_{user_id}"
    
    logger.info(f"Пользователь {username} (ID: {user_id}) запустил бота")
    
    # Создаем inline-клавиатуру
    markup = types.InlineKeyboardMarkup(row_width=1)
    
    # Основные кнопки
    btn_subscribe = types.InlineKeyboardButton('💳 Оформить подписку', callback_data='show_subscribe')
    btn_status = types.InlineKeyboardButton('ℹ️ Статус подписки', callback_data='show_status')
    btn_support = types.InlineKeyboardButton('📞 Поддержка', url=f"https://t.me/{SUPPORT_USERNAME}")
    
    # Добавляем кнопки в клавиатуру
    markup.add(btn_subscribe)
    markup.add(btn_status)
    markup.add(btn_support)
    
    bot.send_message(
        message.chat.id,
        MAIN_MESSAGE,
        reply_markup=markup,
        parse_mode="HTML"
    )

# Обработчик для кнопки "Статус подписки"
@bot.callback_query_handler(func=lambda call: call.data == 'show_status')
def show_status_callback(call):
    try:
        user_id = call.from_user.id
        subscription = check_subscription_status(user_id)
        
        markup = types.InlineKeyboardMarkup(row_width=1)
        
        if subscription["status"] == "active":
            # Получаем дату окончания подписки
            end_date = subscription.get("end_date")
            end_date_str = datetime.fromisoformat(end_date).strftime("%d.%m.%Y") if end_date else "не указана"
            
            message_text = (
                "✅ У вас активная подписка!\n\n"
                f"Дата окончания: {end_date_str}\n\n"
                "Используйте кнопки ниже для управления подпиской:"
            )
            
            # Кнопки для активной подписки
            btn_channel = types.InlineKeyboardButton('📺 Перейти в канал', url=CHANNEL_LINK)
            btn_support = types.InlineKeyboardButton('📞 Поддержка', url=f"https://t.me/{SUPPORT_USERNAME}")
            btn_menu = types.InlineKeyboardButton('🔙 Главное меню', callback_data='show_menu')
            markup.add(btn_channel, btn_support, btn_menu)
            
        else:
            message_text = (
                "❌ У вас нет активной подписки.\n\n"
                "Оформите подписку, чтобы получить доступ к закрытому каналу!"
            )
            
            # Кнопки для неактивной подписки
            btn_subscribe = types.InlineKeyboardButton('💳 Оформить подписку', callback_data='show_subscribe')
            btn_support = types.InlineKeyboardButton('📞 Поддержка', url=f"https://t.me/{SUPPORT_USERNAME}")
            btn_menu = types.InlineKeyboardButton('🔙 Главное меню', callback_data='show_menu')
            markup.add(btn_subscribe, btn_support, btn_menu)
        
        try:
            bot.edit_message_text(
                message_text,
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                reply_markup=markup
            )
        except Exception as e:
            bot.send_message(
                call.message.chat.id,
                message_text,
                reply_markup=markup
            )
            
    except Exception as e:
        logger.error(f"Ошибка при проверке статуса подписки: {str(e)}")
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton('🔙 Главное меню', callback_data='show_menu'))
        bot.send_message(
            call.message.chat.id,
            "❌ Произошла ошибка при проверке статуса подписки. Попробуйте позже.",
            reply_markup=markup
        )

# Обработчик для inline-кнопок основного меню
@bot.callback_query_handler(func=lambda call: call.data in ['show_subscribe', 'show_status', 'show_support', 'show_menu'])
def process_main_menu(call):
    try:
        if call.data == 'show_subscribe':
            subscribe_command(call.message)
        elif call.data == 'show_status':
            show_status_callback(call)
        elif call.data == 'show_support':
            if SUPPORT_USERNAME:
                bot.answer_callback_query(
                    call.id,
                    "Перенаправляем в чат поддержки...",
                    show_alert=False
                )
            else:
                bot.answer_callback_query(
                    call.id,
                    "❌ Извините, служба поддержки временно недоступна",
                    show_alert=True
                )
        elif call.data == 'show_menu':
            show_main_menu(call.message)
        
    except Exception as e:
        logger.error(f"Ошибка при обработке кнопки меню: {str(e)}")
        bot.answer_callback_query(call.id, "Произошла ошибка. Попробуйте позже.")

# Функция для показа главного меню
def show_main_menu(message):
    markup = types.InlineKeyboardMarkup(row_width=1)
    
    # Проверяем статус подписки для определения доступных кнопок
    subscription = check_subscription_status(message.chat.id)
    
    if subscription["status"] == "active":
        # Кнопки для активной подписки
        btn_status = types.InlineKeyboardButton('ℹ️ Статус подписки', callback_data='show_status')
        btn_channel = types.InlineKeyboardButton('📺 Перейти в канал', url=CHANNEL_LINK)
        btn_support = types.InlineKeyboardButton('📞 Поддержка', url=f"https://t.me/{SUPPORT_USERNAME}")
        markup.add(btn_status)
        markup.add(btn_channel)
        markup.add(btn_support)
    else:
        # Кнопки для неактивной подписки
        btn_subscribe = types.InlineKeyboardButton('💳 Оформить подписку', callback_data='show_subscribe')
        btn_status = types.InlineKeyboardButton('ℹ️ Статус подписки', callback_data='show_status')
        btn_support = types.InlineKeyboardButton('📞 Поддержка', url=f"https://t.me/{SUPPORT_USERNAME}")
        markup.add(btn_subscribe)
        markup.add(btn_status)
        markup.add(btn_support)
    
    try:
        bot.edit_message_text(
            MAIN_MESSAGE,
            chat_id=message.chat.id,
            message_id=message.message_id,
            reply_markup=markup,
            parse_mode="HTML"
        )
    except Exception as e:
        # Если не удалось отредактировать, отправляем новое сообщение
        bot.send_message(
            message.chat.id,
            MAIN_MESSAGE,
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

# Обновляем функцию status_command
@bot.message_handler(commands=['status'])
def status_command(message):
    try:
        user_id = message.from_user.id
        subscription = check_subscription_status(user_id)
        
        markup = types.InlineKeyboardMarkup(row_width=1)
        
        if subscription["status"] == "active":
            # Получаем дату окончания подписки
            end_date = subscription.get("end_date")
            end_date_str = datetime.fromisoformat(end_date).strftime("%d.%m.%Y") if end_date else "не указана"
            
            message_text = (
                "✅ У вас активная подписка!\n\n"
                f"Дата окончания: {end_date_str}\n\n"
                "Используйте кнопки ниже для управления подпиской:"
            )
            
            # Кнопки для активной подписки
            btn_channel = types.InlineKeyboardButton('📺 Перейти в канал', url=CHANNEL_LINK)
            btn_support = types.InlineKeyboardButton('📞 Поддержка', url=f"https://t.me/{SUPPORT_USERNAME}")
            btn_menu = types.InlineKeyboardButton('🔙 Главное меню', callback_data='show_menu')
            markup.add(btn_channel, btn_support, btn_menu)
            
        else:
            message_text = (
                "❌ У вас нет активной подписки.\n\n"
                "Оформите подписку, чтобы получить доступ к закрытому каналу!"
            )
            
            # Кнопки для неактивной подписки
            btn_subscribe = types.InlineKeyboardButton('💳 Оформить подписку', callback_data='show_subscribe')
            btn_support = types.InlineKeyboardButton('📞 Поддержка', url=f"https://t.me/{SUPPORT_USERNAME}")
            btn_menu = types.InlineKeyboardButton('🔙 Главное меню', callback_data='show_menu')
            markup.add(btn_subscribe, btn_support, btn_menu)
        
        try:
            bot.edit_message_text(
                message_text,
                chat_id=message.chat.id,
                message_id=message.message_id,
                reply_markup=markup
            )
        except Exception as e:
            bot.send_message(
                message.chat.id,
                message_text,
                reply_markup=markup
            )
            
    except Exception as e:
        logger.error(f"Ошибка при проверке статуса подписки: {str(e)}")
        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton('🔙 Главное меню', callback_data='show_menu'))
        bot.send_message(
            message.chat.id,
            "❌ Произошла ошибка при проверке статуса подписки. Попробуйте позже.",
            reply_markup=markup
        )

# Добавляем обработчик callback-запросов для кнопки отмены подписки
@bot.callback_query_handler(func=lambda call: call.data.startswith('cancel_'))
def cancel_subscription_callback(call):
    try:
        contract_id = call.data.split('_')[1]
        user_id = call.from_user.id
        
        # Отменяем подписку
        if cancel_subscription(user_id, contract_id):
            # Обновляем сообщение после отмены
            bot.edit_message_text(
                "✅ Ваша подписка успешно отменена.\n"
                "Для оформления новой подписки используйте команду /subscribe",
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                reply_markup=None
            )
            # Отправляем новую клавиатуру
            markup = types.ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
            markup.add(types.KeyboardButton('Оформить подписку'))
            markup.add(types.KeyboardButton('Статус подписки'))
            markup.add(types.KeyboardButton('Поддержка'))
            bot.send_message(
                call.message.chat.id,
                "Выберите действие:",
                reply_markup=markup
            )
        else:
            bot.answer_callback_query(
                call.id,
                "❌ Произошла ошибка при отмене подписки. Попробуйте позже или обратитесь в поддержку."
            )
    except Exception as e:
        logger.error(f"Ошибка при обработке отмены подписки: {str(e)}")
        bot.answer_callback_query(
            call.id,
            "❌ Произошла ошибка при отмене подписки"
        )

@bot.callback_query_handler(func=lambda call: call.data.startswith('p|'))
def process_payment_callback(call):
    try:
        # Получаем ID пользователя из callback
        user_id = call.from_user.id
        
        # Разбираем данные из callback
        parts = call.data.split('|')
        if len(parts) != 3:
            raise ValueError("Неверный формат данных callback")
        
        _, offer_id, short_period = parts
        
        # Преобразуем короткий период обратно в полный
        period_map = {
            "1m": "MONTHLY",
            "3m": "PERIOD_90_DAYS",
            "6m": "PERIOD_180_DAYS",
            "1y": "PERIOD_YEAR"
        }
        periodicity = period_map.get(short_period, short_period)
        
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
        
        # Добавляем кнопки для каждой доступной валюты
        for currency, amount in price_info["currencies"].items():
            currency_symbol = CURRENCY_TRANSLATIONS.get(currency, currency)
            button_text = f"Оплатить {amount} {currency_symbol}"
            callback_data = f"currency|{offer_id}|{periodicity}|{currency}"
            markup.add(types.InlineKeyboardButton(text=button_text, callback_data=callback_data))
        
        # Добавляем кнопку "Назад"
        markup.add(types.InlineKeyboardButton('← Назад к выбору периода', callback_data='show_subscribe'))
        
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

@bot.callback_query_handler(func=lambda call: call.data.startswith('currency|'))
def process_currency_callback(call):
    try:
        # Получаем ID пользователя из callback
        user_id = call.from_user.id
        logger.info(f"Обработка выбора валюты для пользователя {user_id}")
        
        # Разбираем данные из callback
        parts = call.data.split('|')
        if len(parts) != 4:
            raise ValueError("Неверный формат данных callback")
        
        _, offer_id, periodicity, currency = parts
        logger.info(f"Параметры платежа: offer_id={offer_id}, periodicity={periodicity}, currency={currency}")
        
        # Создаем ссылку на оплату
        payment_data = create_payment_link(user_id, offer_id, periodicity, currency)
        logger.info(f"Получены данные для оплаты: {payment_data}")
        
        if not payment_data:
            raise ValueError("Не удалось создать ссылку на оплату")
        
        # Получаем ссылку из ответа
        payment_url = payment_data.get('paymentUrl')
        if not payment_url:
            raise ValueError("В ответе отсутствует ссылка на оплату")
        
        logger.info(f"Создана ссылка на оплату: {payment_url}")
        
        # Создаем клавиатуру с кнопками
        markup = types.InlineKeyboardMarkup(row_width=1)
        pay_button = types.InlineKeyboardButton('💳 Перейти к оплате', url=payment_url)
        back_button = types.InlineKeyboardButton('← Назад к выбору периода', callback_data='show_subscribe')
        markup.add(pay_button)
        markup.add(back_button)
        
        # Отправляем сообщение с кнопкой оплаты
        bot.edit_message_text(
            "Для оплаты подписки нажмите на кнопку ниже:",
            call.message.chat.id,
            call.message.message_id,
            reply_markup=markup
        )
        
        # Отмечаем callback как обработанный
        bot.answer_callback_query(call.id)
        
        # Логируем успешное создание ссылки
        logger.info(f"Успешно создана ссылка на оплату для пользователя {user_id}")
        
    except Exception as e:
        logger.error(f"Ошибка при создании ссылки на оплату: {str(e)}", exc_info=True)
        bot.answer_callback_query(
            call.id,
            "Произошла ошибка при создании ссылки на оплату. Попробуйте позже."
        )

@bot.callback_query_handler(func=lambda call: call.data == 'show_detailed_stats')
def show_detailed_stats(call):
    if str(call.from_user.id) != ADMIN_ID:
        bot.answer_callback_query(call.id, "Эта функция доступна только администратору")
        return
    
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        
        # Получаем активных пользователей с деталями
        cursor.execute('''
        WITH LastPayments AS (
            SELECT 
                buyer_email,
                status,
                timestamp,
                event_type,
                ROW_NUMBER() OVER (PARTITION BY buyer_email ORDER BY timestamp DESC) as rn
            FROM payments
            WHERE event_type IN ('payment.success', 'subscription.recurring.payment.success')
        )
        SELECT 
            REPLACE(buyer_email, '@t.me', '') as user_id,
            status,
            timestamp,
            event_type
        FROM LastPayments
        WHERE rn = 1
        ORDER BY timestamp DESC
        LIMIT 50
        ''')
        
        active_users = cursor.fetchall()
        
        # Формируем подробный отчет
        detailed_stats = "📋 <b>Подробная статистика по пользователям</b>\n\n"
        
        for user in active_users:
            user_id = user[0]
            status = user[1]
            timestamp = datetime.fromisoformat(user[2].replace('Z', '+00:00')).strftime("%d.%m.%Y %H:%M")
            event_type = "🔄 Продление" if 'recurring' in user[3] else "💳 Первая оплата"
            
            status_emoji = "✅" if status in ['subscription-active', 'active'] else "❌"
            
            detailed_stats += (
                f"{status_emoji} <a href='tg://user?id={user_id}'>Пользователь {user_id}</a>\n"
                f"Статус: {status}\n"
                f"Последнее событие: {event_type}\n"
                f"Дата: {timestamp}\n"
                "➖➖➖➖➖➖➖➖➖➖\n"
            )
        
        # Разбиваем на части, если сообщение слишком длинное
        if len(detailed_stats) > 4096:
            for x in range(0, len(detailed_stats), 4096):
                part = detailed_stats[x:x+4096]
                bot.send_message(
                    call.message.chat.id,
                    part,
                    parse_mode="HTML",
                    disable_web_page_preview=True
                )
        else:
            bot.send_message(
                call.message.chat.id,
                detailed_stats,
                parse_mode="HTML",
                disable_web_page_preview=True
            )
        
        bot.answer_callback_query(call.id)
        
    except Exception as e:
        logger.error(f"Ошибка при получении подробной статистики: {str(e)}")
        bot.answer_callback_query(call.id, "❌ Произошла ошибка при получении статистики")
    finally:
        conn.close()

@bot.message_handler(commands=['stat'])
def stat_command(message):
    # Проверяем, что команду отправил администратор
    if str(message.from_user.id) != ADMIN_ID:
        bot.reply_to(message, "Эта команда доступна только администратору")
        return
    
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        
        # Получаем общую статистику
        cursor.execute('''
        SELECT 
            COUNT(DISTINCT buyer_email) as total_users,
            COUNT(DISTINCT CASE WHEN event_type = 'payment.success' THEN buyer_email END) as unique_paid,
            COUNT(DISTINCT CASE WHEN event_type = 'subscription.recurring.payment.success' THEN buyer_email END) as unique_renewed,
            COUNT(CASE WHEN event_type = 'payment.success' THEN 1 END) as total_payments,
            COUNT(CASE WHEN event_type = 'subscription.recurring.payment.success' THEN 1 END) as total_renewals,
            COUNT(CASE WHEN event_type = 'payment.failed' THEN 1 END) as failed_payments,
            COUNT(CASE WHEN event_type = 'subscription.recurring.payment.failed' THEN 1 END) as failed_renewals
        FROM payments
        ''')
        
        stats = cursor.fetchone()
        
        # Получаем количество активных подписок
        cursor.execute('''
        SELECT COUNT(*) 
        FROM channel_members 
        WHERE status = 'active'
        ''')
        active_subs = cursor.fetchone()[0]
        
        # Формируем краткую статистику
        summary = (
            "📊 <b>Статистика подписок</b>\n\n"
            f"👥 Всего пользователей: {stats[0]}\n"
            f"✅ Активных подписок: {active_subs}\n"
            f"💳 Уникальных оплат: {stats[1]}\n"
            f"🔄 Уникальных продлений: {stats[2]}\n"
            f"📈 Всего успешных оплат: {stats[3]}\n"
            f"📊 Всего успешных продлений: {stats[4]}\n"
            f"❌ Неудачных оплат: {stats[5]}\n"
            f"⚠️ Неудачных продлений: {stats[6]}\n\n"
            "Для подробной информации нажмите кнопку ниже:"
        )
        
        # Создаем клавиатуру
        markup = types.InlineKeyboardMarkup(row_width=1)
        btn_details = types.InlineKeyboardButton('📋 Подробная статистика', callback_data='show_detailed_stats')
        markup.add(btn_details)
        
        bot.reply_to(message, summary, parse_mode="HTML", reply_markup=markup)
        
    except Exception as e:
        logger.error(f"Ошибка при получении статистики: {str(e)}")
        bot.reply_to(message, "❌ Произошла ошибка при получении статистики")
    finally:
        conn.close()

# Обработчик текстовых сообщений должен быть последним
@bot.message_handler(content_types=['text'])
def text_handler(message):
    if message.text == 'Оформить подписку':
        subscribe_command(message)
    elif message.text == 'Статус подписки':
        status_command(message)
    elif message.text == 'Поддержка':
        if SUPPORT_USERNAME:
            bot.send_message(
                message.chat.id,
                f"📞 Напишите нам: @{SUPPORT_USERNAME}",
                disable_web_page_preview=True
            )
        else:
            bot.reply_to(message, "❌ Извините, служба поддержки временно недоступна")
    elif message.text == 'Перейти в канал':
        if CHANNEL_LINK:
            bot.reply_to(
                message,
                f"🔗 Ссылка для входа в канал:\n{CHANNEL_LINK}",
                disable_web_page_preview=True
            )
        else:
            bot.reply_to(message, "❌ Ссылка на канал не настроена")
    else:
        # Проверяем, является ли сообщение командой
        if message.text.startswith('/'):
            available_commands = [
                "/start - начать работу с ботом",
                "/subscribe - оформить подписку",
                "/status - проверить статус подписки"
            ]
            
            # Добавляем админские команды, если сообщение от админа
            if str(message.from_user.id) == ADMIN_ID:
                available_commands.extend([
                    "/stat - статистика подписок",
                    "/test - тестовый платеж",
                    "/test_fail - тестовый неуспешный платеж",
                    "/test_expire - тестовая истекшая подписка"
                ])
            
            bot.reply_to(
                message, 
                "Неизвестная команда.\nДоступные команды:\n" + "\n".join(available_commands)
            )
        else:
            bot.reply_to(
                message, 
                "Используйте кнопки меню или команды:\n" + "\n".join([
                    "/start - начать работу с ботом",
                    "/subscribe - оформить подписку",
                    "/status - проверить статус подписки"
                ])
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

