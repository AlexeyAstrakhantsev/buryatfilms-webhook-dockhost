import os
import logging
import sqlite3
import requests
import telebot
from telebot import types, apihelper
import threading
import time
from pathlib import Path
from datetime import datetime, timedelta, timezone
import json

# Настройка логирования
DATA_DIR = Path("/mount/database")
DATA_DIR.mkdir(exist_ok=True)

log_file = DATA_DIR / f"log_{time.strftime('%Y%m%d')}.log"
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
USERNAME = os.getenv("WEBHOOK_USERNAME", "admin")
PASSWORD = os.getenv("WEBHOOK_PASSWORD", "password")

# Цены из переменных окружения
PRICE_MONTHLY = float(os.getenv("PRICE_MONTHLY", "500"))
PRICE_3_MONTHS = float(os.getenv("PRICE_3_MONTHS", "1200"))
PRICE_6_MONTHS = float(os.getenv("PRICE_6_MONTHS", "2000"))
PRICE_YEARLY = float(os.getenv("PRICE_YEARLY", "3850"))

# Словарь соответствия цен и периодичности
PRICE_PERIODICITY = {
    PRICE_MONTHLY: "MONTHLY",
    PRICE_3_MONTHS: "PERIOD_90_DAYS", 
    PRICE_6_MONTHS: "PERIOD_180_DAYS",
    PRICE_YEARLY: "PERIOD_YEAR"
}

# Словарь дней для каждого периода
PERIOD_DAYS = {
    "MONTHLY": 30,
    "PERIOD_90_DAYS": 90,
    "PERIOD_180_DAYS": 180,
    "PERIOD_YEAR": 365
}

# Функция для определения периодичности по стоимости

def get_periodicity_by_amount(amount: float) -> str:
    """
    Определяет периодичность подписки по стоимости
    """
    # Ищем точное совпадение цены
    if amount in PRICE_PERIODICITY:
        return PRICE_PERIODICITY[amount]
    # Если точного совпадения нет, ищем ближайшую цену
    closest_price = min(PRICE_PERIODICITY.keys(), key=lambda x: abs(x - amount))
    price_difference = abs(closest_price - amount)
    if price_difference <= closest_price * 0.1:
        logger.info(f"Цена {amount} близка к {closest_price}, используем периодичность {PRICE_PERIODICITY[closest_price]}")
        return PRICE_PERIODICITY[closest_price]
    logger.warning(f"Не удалось определить периодичность для цены {amount}, используем MONTHLY")
    return "MONTHLY"

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
def cancel_subscription(user_id, contract_id):
    try:
        url = "https://gate.lava.top/api/v1/subscriptions"
        headers = {
            "Content-Type": "application/json",
            "X-Api-Key": LAVA_API_KEY
        }
        
        # Добавляем параметры в URL для DELETE запроса
        params = {
            "contractId": contract_id,
            "email": f"{user_id}@t.me"
        }
        
        logger.info(f"Отправка запроса на отмену подписки:")
        logger.info(f"URL: {url}")
        logger.info(f"Headers: {headers}")
        logger.info(f"Params: {params}")
        
        response = requests.delete(url, headers=headers, params=params)
        
        logger.info(f"Ответ от LAVA.TOP:")
        logger.info(f"Статус: {response.status_code}")
        logger.info(f"Тело ответа: {response.text}")
        logger.info(f"Заголовки: {response.headers}")
        
        # Проверяем оба кода успешного ответа: 200 и 204
        if response.status_code in [200, 204]:
            logger.info(f"Подписка успешно отменена для пользователя {user_id}")
            
            # Обновляем статус в БД, но сохраняем дату окончания
            conn = sqlite3.connect(DB_PATH)
            cursor = conn.cursor()
            cursor.execute('''
            UPDATE channel_members 
            SET status = 'cancelled' 
            WHERE user_id = ? AND status = 'active'
            ''', (user_id,))
            conn.commit()
            conn.close()
            
            return True
        else:
            logger.error(f"Ошибка при отмене подписки: код {response.status_code}, ответ: {response.text}")
            return False
            
    except Exception as e:
        logger.error(f"Ошибка при отмене подписки: {str(e)}")
        return False

# Функция для проверки статуса подписки пользователя
def check_subscription_status(user_id):
    conn = None  # Инициализируем conn вне try-блока
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        
        # Сначала проверяем статус в channel_members
        cursor.execute('''
        SELECT cm.status, cm.subscription_end_date, cm.last_payment_id,
               p.contract_id, p.parent_contract_id
        FROM channel_members cm
        LEFT JOIN payments p ON p.id = cm.last_payment_id
        WHERE cm.user_id = ? AND (cm.status = 'active' OR cm.status = 'cancelled')
        ''', (user_id,))
        
        member = cursor.fetchone()
        
        if member:
            status, end_date_str, last_payment_id, contract_id, parent_contract_id = member
            
            # Проверяем, не истекла ли подписка, обрабатывая возможные ошибки даты
            if end_date_str:
                try:
                    end_date = datetime.fromisoformat(end_date_str.replace('Z', '+00:00'))
                    if end_date > datetime.now(timezone.utc):
                        return {
                            "status": status,  # Возвращаем фактический статус (active или cancelled)
                            "end_date": end_date_str,
                            "contract_id": parent_contract_id or contract_id
                        }
                except ValueError as ve:
                    logger.error(f"Ошибка формата даты в check_subscription_status (member): {end_date_str} - {ve}")
                except Exception as e:
                    logger.error(f"Неожиданная ошибка при парсинге даты в check_subscription_status (member): {end_date_str} - {e}")
        
        # Если нет активной записи в channel_members или она истекла, проверяем последний платеж
        cursor.execute('''
        SELECT p.status, p.timestamp, p.event_type, cm.subscription_end_date,
               p.contract_id, p.parent_contract_id, p.amount
        FROM payments p
        LEFT JOIN channel_members cm ON cm.last_payment_id = p.id
        WHERE p.buyer_email = ?
        AND p.event_type IN ('payment.success', 'subscription.recurring.payment.success')
        ORDER BY p.timestamp DESC
        LIMIT 1
        ''', (f"{user_id}@t.me",))
        
        payment = cursor.fetchone()
        
        if payment:
            status, timestamp_str, event_type, end_date_str_from_payment, contract_id, parent_contract_id, amount = payment
            
            # Если end_date_str_from_payment пуст, рассчитываем его на основе amount
            if not end_date_str_from_payment and timestamp_str and amount is not None:
                try:
                    periodicity = get_periodicity_by_amount(amount)
                    days = PERIOD_DAYS.get(periodicity, 30)
                    start_date = datetime.fromisoformat(timestamp_str.replace('Z', '+00:00'))
                    end_date_calculated = (start_date + timedelta(days=days)).isoformat()
                    end_date_str_from_payment = end_date_calculated
                    logger.info(f"Рассчитана дата окончания подписки для {user_id} по последнему платежу: {end_date_calculated}")
                except Exception as e:
                    logger.error(f"Ошибка при расчете даты окончания по amount для {user_id}: {e}")
                    end_date_str_from_payment = None # Очищаем, чтобы не использовать некорректную дату

            is_active = False
            if end_date_str_from_payment:
                try:
                    end_date = datetime.fromisoformat(end_date_str_from_payment.replace('Z', '+00:00'))
                    if end_date > datetime.now(timezone.utc):
                        is_active = True
                except ValueError as ve:
                    logger.error(f"Ошибка формата даты в check_subscription_status (payment): {end_date_str_from_payment} - {ve}")
                except Exception as e:
                    logger.error(f"Неожиданная ошибка при парсинге даты в check_subscription_status (payment): {end_date_str_from_payment} - {e}")
            
            return {
                "status": "active" if is_active else "inactive",
                "end_date": end_date_str_from_payment,
                "contract_id": parent_contract_id or contract_id
            }
        
        return {"status": "no_subscription"}
        
    except sqlite3.Error as sqle:
        logger.error(f"Ошибка SQLite при проверке статуса подписки для {user_id}: {sqle}")
        return {"status": "error", "error": f"Ошибка базы данных: {sqle}"}
    except Exception as e:
        logger.error(f"Неожиданная ошибка при проверке статуса подписки для {user_id}: {str(e)}", exc_info=True)
        return {"status": "error", "error": f"Неизвестная ошибка: {e}"}
    finally:
        if conn:
            conn.close()

# Функция для добавления пользователя в закрытый канал
def add_user_to_channel(user_id):
    try:
        # Создаем ссылку-приглашение в канал
        invite_link = bot.create_chat_invite_link(
            chat_id=CHANNEL_ID,
            member_limit=1,
            expire_date=int(time.time()) + 2592000
        )
        # Получаем информацию о последнем платеже
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute('''
        SELECT id, timestamp, raw_data, amount
        FROM payments 
        WHERE buyer_email = ? 
        AND (status = 'subscription-active' OR status = 'active')
        ORDER BY timestamp DESC
        LIMIT 1
        ''', (f"{user_id}@t.me",))
        payment = cursor.fetchone()
        if payment:
            payment_id, timestamp, raw_data, amount = payment
            # Определяем периодичность по стоимости
            periodicity = get_periodicity_by_amount(amount)
            days = PERIOD_DAYS.get(periodicity, 30)
            try:
                end_date = (datetime.fromisoformat(timestamp.replace('Z', '+00:00')) + 
                           timedelta(days=days)).isoformat()
                logger.info(f"Пользователь {user_id}: стоимость {amount}, периодичность {periodicity}, дней {days}, окончание {end_date}")
            except Exception as e:
                logger.error(f"Ошибка при расчете даты окончания подписки: {str(e)}")
                end_date = (datetime.fromisoformat(timestamp.replace('Z', '+00:00')) + timedelta(days=30)).isoformat()
            # Добавляем или обновляем запись в channel_members с текущей датой
            current_time = datetime.now(timezone.utc).isoformat()
            cursor.execute('''
            INSERT OR REPLACE INTO channel_members 
            (user_id, status, joined_at, subscription_end_date, last_payment_id)
            VALUES (?, 'active', ?, ?, ?)
            ''', (user_id, current_time, end_date, payment_id))
            conn.commit()
        conn.close()
        # Отправляем сообщение с кнопкой для входа в канал
        channel_markup = types.InlineKeyboardMarkup(row_width=1)
        channel_button = types.InlineKeyboardButton('📺 Войти в канал', url=invite_link.invite_link)
        channel_markup.add(channel_button)
        bot.send_message(
            user_id,
            f"Поздравляем! Вы успешно оформили подписку. Вот ваша ссылка для доступа к закрытому каналу: {invite_link.invite_link}",
            reply_markup=channel_markup
        )        
        # Отправляем пользователю ссылку на канал
        welcome_message = bot.send_message(
            user_id,
            f"⠀⠀⠀⠀⠀Меню подписчика⠀⠀⠀⠀⠀",
            disable_web_page_preview=False
        )
        # Показываем главное меню
        show_main_menu(welcome_message)
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


# Функция для показа меню выбора периода подписки
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

# Функция для показа главного меню
def show_main_menu(message):
    markup = types.InlineKeyboardMarkup(row_width=1)
    
    # Проверяем статус подписки для определения доступных кнопок
    subscription = check_subscription_status(message.chat.id)
    
    # Общие кнопки
    btn_about = types.InlineKeyboardButton('🔍 Подробнее о канале', callback_data='show_about')
    
    # Создаем кнопку поддержки только если SUPPORT_USERNAME задан
    btn_support = None
    if SUPPORT_USERNAME:
        btn_support = types.InlineKeyboardButton('📞 Поддержка', url=f"https://t.me/{SUPPORT_USERNAME}")

    if subscription["status"] in ["active", "cancelled"]:
        # Кнопки для активной или отмененной подписки
        btn_status = types.InlineKeyboardButton('ℹ️ Статус подписки', callback_data='show_status')
        markup.add(btn_status)
        
        # Создаем кнопку перехода в канал только если CHANNEL_LINK задан
        if CHANNEL_LINK:
            btn_channel = types.InlineKeyboardButton('📺 Перейти в канал', url=CHANNEL_LINK)
            markup.add(btn_channel)
            
        markup.add(btn_about)
        if btn_support: # Добавляем кнопку поддержки, если она была создана
            markup.add(btn_support)
        
    else:
        # Кнопки для неактивной подписки
        btn_subscribe = types.InlineKeyboardButton('💳 Оформить подписку', callback_data='show_subscribe')
        btn_status = types.InlineKeyboardButton('ℹ️ Статус подписки', callback_data='show_status')
        markup.add(btn_subscribe)
        markup.add(btn_status)
        markup.add(btn_about)
        if btn_support: # Добавляем кнопку поддержки, если она была создана
            markup.add(btn_support)
        
    # Отправляем меню отдельным сообщением
    try:
        bot.send_message( # Используем send_message для надежности
            message.chat.id,
            "⠀⠀⠀⠀⠀Меню подписчика⠀⠀⠀⠀⠀",
            reply_markup=markup,
            parse_mode="HTML"
        )
    except Exception as e:
        logger.error(f"Ошибка при отправке главного меню пользователю {message.chat.id}: {str(e)}")
# Обработчик для кнопки отмены подписки
@bot.callback_query_handler(func=lambda call: call.data.startswith('cancel_'))
def cancel_subscription_callback(call):
    try:
        user_id = call.from_user.id
        subscription = check_subscription_status(user_id)

        # Обрабатываем ошибку от check_subscription_status явно
        if subscription.get("status") == "error":
            bot.answer_callback_query(
                call.id,
                f"❌ Произошла ошибка при проверке статуса подписки: {subscription.get('error', 'Неизвестная ошибка')}. Попробуйте позже."
            )
            bot.send_message(
                call.message.chat.id,
                f"❌ Произошла ошибка при проверке статуса подписки: {subscription.get('error', 'Неизвестная ошибка')}. "
                f"Пожалуйста, попробуйте позже или обратитесь в поддержку."
            )
            logger.error(f"Ошибка check_subscription_status при обработке отмены для user {user_id}: {subscription.get('error')}")
            return

        # Получаем contract_id из callback_data
        try:
            contract_id = call.data.split('_')[1]
        except IndexError:
            logger.error(f"Некорректный формат callback_data для отмены подписки: {call.data} для user {user_id}")
            bot.answer_callback_query(
                call.id,
                "❌ Некорректные данные для отмены подписки. Пожалуйста, обратитесь в поддержку."
            )
            bot.send_message(
                call.message.chat.id,
                "❌ Произошла ошибка при отмене подписки. Не удалось распознать данные. Пожалуйста, обратитесь в поддержку."
            )
            return

        # Если это первый шаг (запрос подтверждения)
        if not call.data.endswith('_confirmed'):
            end_date = subscription.get("end_date")
            end_date_str = "не определена"
            if end_date and isinstance(end_date, str):
                try:
                    end_date_str = datetime.fromisoformat(end_date.replace('Z', '+00:00')).strftime("%d.%m.%Y")
                except ValueError:
                    logger.error(f"Некорректный формат даты: {end_date}")
                    end_date_str = "не определена"
            
            # Проверяем наличие contract_id перед формированием кнопки подтверждения
            if not subscription.get("contract_id"):
                logger.error(f"Не найден contract_id для отмены подписки пользователя {user_id} при запросе подтверждения.")
                bot.answer_callback_query(
                    call.id,
                    "❌ Не удалось найти данные для отмены подписки. Пожалуйста, обратитесь в поддержку."
                )
                bot.send_message(
                    call.message.chat.id,
                    "❌ Не удалось найти данные для отмены подписки. "
                    "Пожалуйста, попробуйте позже или обратитесь в поддержку."
                )
                return

            markup = types.InlineKeyboardMarkup(row_width=1)
            btn_confirm = types.InlineKeyboardButton('✅ Да, отписаться', 
                                                   callback_data=f"cancel_{contract_id}_confirmed")
            btn_back = types.InlineKeyboardButton('🔙 Нет, вернуться', 
                                                callback_data='show_status')
            markup.add(btn_confirm, btn_back)
            
            try:
                # Удаляем предыдущее сообщение с меню
                bot.delete_message(call.message.chat.id, call.message.message_id)
            except apihelper.ApiTelegramException as e:
                logger.warning(f"Не удалось удалить сообщение {call.message.message_id} в чате {call.message.chat.id}: {e}")
            
            # Отправляем запрос подтверждения
            bot.send_message(
                call.message.chat.id,
                f"⚠️ Вы уверены, что хотите отписаться?\n\n"
                f"При отписке доступ к каналу останется до {end_date_str}.\n"
                f"Автопродление будет отключено.",
                reply_markup=markup
            )
        
        # Если это подтверждение отмены
        else:
            # Дополнительная проверка contract_id перед вызовом cancel_subscription
            if not contract_id: # contract_id уже извлечен выше, но стоит убедиться, что он не пуст
                 logger.error(f"Пустой contract_id при подтвержденной отмене для user {user_id}.")
                 bot.answer_callback_query(
                     call.id,
                     "❌ Не удалось отменить подписку: отсутствуют данные контракта."
                 )
                 bot.send_message(
                     call.message.chat.id,
                     "❌ Произошла ошибка при отмене подписки: отсутствуют данные контракта. Пожалуйста, обратитесь в поддержку."
                 )
                 return

            if cancel_subscription(user_id, contract_id):
                if subscription.get("end_date"):
                    end_date_str = datetime.fromisoformat(subscription["end_date"].replace('Z', '+00:00')).strftime("%d.%m.%Y")
                else:
                    end_date_str = "не определена"
                    logger.warning(f"end_date не найдена в subscription после успешной отмены для user {user_id}")
                
                try:
                    # Удаляем предыдущее сообщение с меню
                    bot.delete_message(call.message.chat.id, call.message.message_id)
                except apihelper.ApiTelegramException as e:
                    logger.warning(f"Не удалось удалить сообщение {call.message.message_id} в чате {call.message.chat.id} после отмены: {e}")
                
                # Отправляем сообщение об успешной отмене
                bot.send_message(
                    call.message.chat.id,
                    f"✅ Автопродление подписки отключено.\n\n"
                    f"Доступ к каналу сохранится до {end_date_str}.\n"
                    f"После этой даты вы сможете оформить новую подписку. Мы всегда рады видеть Вас снова!"
                )
                
                # Показываем главное меню
                show_main_menu(call.message)
                
                # Уведомляем админа
                notify_admin(
                    f"🔔 <b>Отмена подписки</b>\n\n"
                    f"Пользователь: {user_id}\n"
                    f"Доступ активен до: {end_date_str}"
                )
            else:
                bot.answer_callback_query(
                    call.id,
                    "❌ Произошла ошибка при отмене подписки. Попробуйте позже или обратитесь в поддержку."
                )
    except Exception as e:
        logger.error(f"Неожиданная ошибка при обработке отмены подписки для пользователя {user_id}: {str(e)}", exc_info=True)
        bot.answer_callback_query(
            call.id,
            "❌ Произошла ошибка при отмене подписки"
        )


# Обработчик для кнопки "Подробнее о канале"
@bot.callback_query_handler(func=lambda call: call.data == 'show_about')
def show_about_callback(call):
    try:
        # Удаляем предыдущее сообщение с меню
        bot.delete_message(call.message.chat.id, call.message.message_id)
    except apihelper.ApiTelegramException as e:
        logger.warning(f"Не удалось удалить сообщение {call.message.message_id} в чате {call.message.chat.id}: {e}")
    
    about_text = """В ЗАКРЫТОМ КАНАЛЕ:

✅ Хиты на бурятском — «Шрек», «Кунг-фу Панда» и другие любимые мультфильмы. Мы постоянно пополняем коллекцию.

✅ Вы — наш генеральный партнёр. Ваша подписка помогает создавать новые мультфильмы и фильмы на бурятском языке.

Вместе мы создадим индустрию бурятского кино.
Сделаем родной язык — модным, сильным и вечным."""

    # Отправляем информацию о канале
    bot.send_message(
        call.message.chat.id,
        about_text
    )
    
    # Проверяем статус подписки
    subscription = check_subscription_status(call.from_user.id)
    
    # Показываем меню
    markup = types.InlineKeyboardMarkup(row_width=1)
    
    # Если нет активной подписки, добавляем кнопку подписки
    if subscription["status"] != "active":
        btn_subscribe = types.InlineKeyboardButton('💳 Оформить подписку', callback_data='show_subscribe')
        markup.add(btn_subscribe)
        
    btn_back = types.InlineKeyboardButton('🔙 Главное меню', callback_data='show_menu')
    markup.add(btn_back)
    
    # Отправляем меню отдельным сообщением
    bot.send_message(
        call.message.chat.id,
        "⠀⠀⠀⠀⠀Меню подписчика⠀⠀⠀⠀⠀",
        reply_markup=markup
    )

# Обработчик для кнопки "Статус подписки"
@bot.callback_query_handler(func=lambda call: call.data == 'show_status')
def show_status_callback(call):
    try:
        try:
            # Удаляем предыдущее сообщение с меню
            bot.delete_message(call.message.chat.id, call.message.message_id)
        except apihelper.ApiTelegramException as e:
            logger.warning(f"Не удалось удалить сообщение {call.message.message_id} в чате {call.message.chat.id}: {e}")
        
        user_id = call.from_user.id
        subscription = check_subscription_status(user_id)
        
        if subscription["status"] == "error":
            bot.send_message(
                call.message.chat.id,
                f"❌ Произошла ошибка при проверке статуса подписки: {subscription['error']}.\n"
                f"Пожалуйста, попробуйте позже или обратитесь в поддержку."
            )
            markup = types.InlineKeyboardMarkup()
            markup.add(types.InlineKeyboardButton('🔙 Главное меню', callback_data='show_menu'))
            bot.send_message(
                call.message.chat.id,
                "⠀⠀⠀⠀⠀Меню подписчика⠀⠀⠀⠀⠀",
                reply_markup=markup
            )
            return

        if subscription["status"] in ["active", "cancelled"]:
            # Получаем дату окончания подписки
            end_date = subscription.get("end_date")
            end_date_str = datetime.fromisoformat(end_date.replace('Z', '+00:00')).strftime("%d.%m.%Y") if end_date else "не указана"
            
            # Формируем сообщение в зависимости от статуса
            if subscription["status"] == "active":
                status_text = "✅ У вас активная подписка!"
            else:  # cancelled
                status_text = "ℹ️ Автопродление подписки отключено. "
            
            # Отправляем информацию о подписке
            bot.send_message(
                call.message.chat.id,
                f"{status_text}\n\n"
                f"Доступ к каналу действует до: {end_date_str}"
            )
            
            # Показываем меню с кнопками управления
            markup = types.InlineKeyboardMarkup(row_width=1)
            btn_channel = types.InlineKeyboardButton('📺 Перейти в канал', url=CHANNEL_LINK)
            markup.add(btn_channel)
            
            # Показываем кнопку отмены подписки только если статус active
            if subscription["status"] == "active":
                btn_cancel = types.InlineKeyboardButton('❌ Отключить автопродление', 
                                                  callback_data=f"cancel_{subscription['contract_id']}")
                markup.add(btn_cancel)
            
            btn_support = types.InlineKeyboardButton('📞 Поддержка', url=f"https://t.me/{SUPPORT_USERNAME}")
            btn_menu = types.InlineKeyboardButton('🔙 Главное меню', callback_data='show_menu')
            markup.add(btn_support, btn_menu)
            
        else:
            # Отправляем информацию об отсутствии подписки
            bot.send_message(
                call.message.chat.id,
                "❌ У вас нет активной подписки.\n\n"
                "Оформите подписку, чтобы получить доступ к закрытому каналу!"
            )
            
            # Кнопки для неактивной подписки
            markup = types.InlineKeyboardMarkup(row_width=1)
            btn_subscribe = types.InlineKeyboardButton('💳 Оформить подписку', callback_data='show_subscribe')
            btn_support = types.InlineKeyboardButton('📞 Поддержка', url=f"https://t.me/{SUPPORT_USERNAME}")
            btn_menu = types.InlineKeyboardButton('🔙 Главное меню', callback_data='show_menu')
            markup.add(btn_subscribe, btn_support, btn_menu)
        
        # Отправляем меню отдельным сообщением
        bot.send_message(
            call.message.chat.id,
        "⠀⠀⠀⠀⠀Меню подписчика⠀⠀⠀⠀⠀",
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

# Обработчик для выбора периода оплаты
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
            # Сокращаем offer_id до первых 20 символов, этого должно быть достаточно для уникальности
            short_offer_id = offer_id[:20]
            callback_data = f"c|{short_offer_id}|{short_period}|{currency}"
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

# Добавляем новую функцию для сокращения ссылки
def shorten_payment_url(payment_url: str) -> str:
    try:
        # URL вашего webhook сервиса
        webhook_url = "http://localhost:8000"  # Измените на актуальный URL
        
        # Данные для авторизации
        auth = (USERNAME, PASSWORD)  # Используйте те же креды, что и в main.py
        
        # Отправляем запрос на сокращение
        response = requests.post(
            f"{webhook_url}/shorten",
            json={"original_url": payment_url},
            auth=auth
        )
        
        if response.status_code == 200:
            short_code = response.json()["short_code"]
            return f"https://buryat-films.ru/payment/{short_code}"
        else:
            logger.error(f"Ошибка при сокращении ссылки: {response.text}")
            return payment_url
            
    except Exception as e:
        logger.error(f"Ошибка при сокращении ссылки: {str(e)}")

# Модифицируем функцию process_currency_callback
@bot.callback_query_handler(func=lambda call: call.data.startswith('c|'))
def process_currency_callback(call):
    try:
        # Получаем ID пользователя из callback
        user_id = call.from_user.id
        logger.info(f"Обработка выбора валюты для пользователя {user_id}")
        
        # Разбираем данные из callback
        parts = call.data.split('|')
        if len(parts) != 4:
            raise ValueError("Неверный формат данных callback")
        
        _, short_offer_id, short_period, currency = parts
        
        # Получаем полный offer_id
        subscriptions = get_available_subscriptions()
        full_offer_id = next((sub["offer_id"] for sub in subscriptions 
                        if sub["offer_id"].startswith(short_offer_id)), None)
        
        if not full_offer_id:
            raise ValueError("Подписка не найдена")
            
        # Преобразуем короткий период обратно в полный
        period_map = {
            "1m": "MONTHLY",
            "3m": "PERIOD_90_DAYS",
            "6m": "PERIOD_180_DAYS",
            "1y": "PERIOD_YEAR"
        }
        periodicity = period_map.get(short_period)
        
        # Создаем ссылку на оплату с полным offer_id
        payment_data = create_payment_link(user_id, full_offer_id, periodicity, currency)
        logger.info(f"Получены данные для оплаты: {payment_data}")
        
        if not payment_data:
            raise ValueError("Не удалось создать ссылку на оплату")
        
        # Получаем ссылку из ответа
        payment_url = payment_data.get('paymentUrl')
        if not payment_url:
            raise ValueError("В ответе отсутствует ссылка на оплату")
        
        # Сокращаем ссылку
        short_payment_url = shorten_payment_url(payment_url)
        logger.info(f"Создана короткая ссылка на оплату: {short_payment_url}")
        
        # Создаем клавиатуру с кнопками
        markup = types.InlineKeyboardMarkup(row_width=1)
        pay_button = types.InlineKeyboardButton('💳 Перейти к оплате', url=short_payment_url)
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

# Функция для проверки сроков подписок
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
                        #bot.send_message(
                        #    user_id,
                        #    f"⚠️ Ваша подписка истекла!\n\n"
                        #    f"У вас есть еще {days_grace_left} дней льготного периода.\n"
                        #    f"После этого доступ к каналу будет прекращен.\n\n"
                        #    f"Для продления подписки используйте команду /subscribe"
                        #)
                    
                    # Уведомления о скором окончании подписки
                    #elif days_left in NOTIFY_BEFORE_DAYS:
                        #bot.send_message(
                        #    user_id,
                        #    f"ℹ️ Ваша подписка закончится через {days_left} дней.\n"
                        #    f"Не забудьте продлить её, чтобы сохранить доступ к каналу.\n\n"
                        #    f"Для продления используйте команду /subscribe"
                        #)
            
            except Exception as e:
                logger.error(f"Ошибка при проверке пользователя {user_id}: {str(e)}", exc_info=True)
                continue
        
        conn.commit()
        conn.close()
        logger.info("Проверка участников канала завершена")
            
    except Exception as e:
        logger.error(f"Ошибка при проверке сроков подписок: {str(e)}", exc_info=True)

# Обработчик для команды /status
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
# Добавляем новый обработчик для команды рассылки
@bot.message_handler(commands=['broadcast'])
def broadcast_command(message):
    try:
        user_id = str(message.from_user.id)
        
        # Проверяем, является ли пользователь администратором
        if user_id != str(ADMIN_ID):
            bot.reply_to(message, "❌ У вас нет прав для использования этой команды.")
            return
        
        # Проверяем наличие текста для рассылки
        command_parts = message.text.split(maxsplit=1)
        if len(command_parts) < 2:
            bot.reply_to(
                message,
                "ℹ️ Использование команды:\n"
                "/broadcast <текст сообщения>\n\n"
                "Поддерживается HTML-разметка."
            )
            return
        
        broadcast_text = command_parts[1]
        
        # Получаем список всех пользователей из базы данных
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        
        # Получаем уникальных пользователей из таблицы channel_members
        cursor.execute('SELECT DISTINCT user_id FROM channel_members')
        users = cursor.fetchall()
        
        # Добавляем пользователей из таблицы payments, которых нет в channel_members
        cursor.execute('''
        SELECT DISTINCT SUBSTR(buyer_email, 1, INSTR(buyer_email, '@') - 1) 
        FROM payments 
        WHERE buyer_email NOT IN (
            SELECT user_id || '@t.me' 
            FROM channel_members
        )
        ''')
        additional_users = cursor.fetchall()
        
        conn.close()
        
        # Объединяем списки пользователей
        all_users = list(set([user[0] for user in users + additional_users]))
        
        # Отправляем статус о начале рассылки
        status_message = bot.reply_to(
            message,
            f"📤 Начинаю рассылку...\n"
            f"Всего получателей: {len(all_users)}"
        )
        
        # Счетчики для статистики
        successful = 0
        failed = 0
        
        # Выполняем рассылку
        for user_id in all_users:
            try:
                bot.send_message(
                    user_id,
                    broadcast_text,
                    parse_mode="HTML",
                    disable_web_page_preview=True
                )
                successful += 1
                
                # Обновляем статус каждые 10 отправленных сообщений
                if (successful + failed) % 10 == 0:
                    bot.edit_message_text(
                        f"📤 Отправка сообщений...\n"
                        f"Успешно: {successful}\n"
                        f"Ошибок: {failed}\n"
                        f"Всего: {len(all_users)}",
                        chat_id=status_message.chat.id,
                        message_id=status_message.message_id
                    )
                
                # Задержка между отправками, чтобы избежать ограничений Telegram
                time.sleep(0.1)
                
            except Exception as e:
                logger.error(f"Ошибка при отправке сообщения пользователю {user_id}: {str(e)}")
                failed += 1
        
        # Отправляем итоговый отчет
        bot.edit_message_text(
            f"✅ Рассылка завершена\n\n"
            f"📊 Статистика:\n"
            f"Успешно доставлено: {successful}\n"
            f"Ошибок доставки: {failed}\n"
            f"Всего получателей: {len(all_users)}",
            chat_id=status_message.chat.id,
            message_id=status_message.message_id
        )
        
    except Exception as e:
        logger.error(f"Ошибка при выполнении рассылки: {str(e)}")
        bot.reply_to(message, "❌ Произошла ошибка при выполнении рассылки.")


# Обработчик для команды /subscribe
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

# Обработчик для команды /start
@bot.message_handler(commands=['start'])
def start_command(message):
    user_id = message.from_user.id
    username = message.from_user.username or f"user_{user_id}"
    
    logger.info(f"Пользователь {username} (ID: {user_id}) запустил бота")
    
    # Отправляем приветственное сообщение
    bot.send_message(
        message.chat.id,
        MAIN_MESSAGE,
        parse_mode="HTML"
    )
    
    # Затем показываем меню
    show_main_menu(message)

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
                # check_new_payments()
                pass
            
            else:
                logger.warning("Таблица payments еще не создана. Пропускаем проверку платежей.")
                
        except Exception as e:
            logger.error(f"Ошибка при проверке новых платежей: {str(e)}")
        
        # Проверяем каждые 60 секунд
        time.sleep(20)

# Функция проверки подписок
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
        time.sleep(3600)

# Функция для запуска бота
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
        
        # Запускаем бота с увеличенными таймаутами
        # interval=3: увеличиваем интервал между запросами
        # timeout=30: увеличиваем таймаут соединения
        bot.polling(none_stop=True, interval=3, timeout=30)
    except requests.exceptions.ReadTimeout as e:
        logger.warning(f"Таймаут при обращении к Telegram API: {str(e)}. Перезапуск бота...")
        time.sleep(5)  # Ожидаем 5 секунд перед повторным запуском
        run_bot()  # Рекурсивно запускаем бота снова
    except Exception as e:
        logger.error(f"Ошибка при запуске бота: {str(e)}", exc_info=True)
        time.sleep(10)  # Ожидаем 10 секунд перед повторным запуском
        run_bot()  # Рекурсивно запускаем бота снова

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

