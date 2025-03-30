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
DATA_DIR = Path("/app/data")
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
LAVA_OFFER_ID = os.getenv("LAVA_OFFER_ID")
CHANNEL_ID = os.getenv("CHANNEL_ID")  # ID закрытого канала
ADMIN_ID = os.getenv("ADMIN_ID")  # ID администратора для уведомлений
DB_PATH = DATA_DIR / "lava_payments.db"

# Инициализация бота
bot = telebot.TeleBot(BOT_TOKEN)

# Функция для создания ссылки на оплату
def create_payment_link(user_id):
    url = "https://gate.lava.top/api/v2/invoice"
    headers = {
        "Content-Type": "application/json",
        "X-Api-Key": LAVA_API_KEY
    }
    payload = {
        "email": f"{user_id}@t.me",
        "offerId": LAVA_OFFER_ID,
        "periodicity": "MONTHLY",
        "currency": "RUB",
        "buyerLanguage": "RU",
        "paymentMethod": "BANK131",
        "clientUtm": {}
    }
    
    try:
        response = requests.post(url, headers=headers, json=payload)
        response.raise_for_status()
        return response.json()
    except Exception as e:
        logger.error(f"Ошибка при создании ссылки на оплату: {str(e)}")
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
        response.raise_for_status()
        
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
        
        return True
    except Exception as e:
        logger.error(f"Ошибка при отмене подписки: {str(e)}")
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
    
    # Уведомляем администратора о попытке оформления подписки
    admin_message = f"<b>Попытка оформления подписки</b>\n\n" \
                   f"<b>Пользователь:</b> {username} (ID: {user_id})"
    notify_admin(admin_message)
    
    # Проверяем, есть ли уже активная подписка
    subscription = check_subscription_status(user_id)
    if subscription["status"] == "active":
        bot.send_message(
            message.chat.id,
            "У вас уже есть активная подписка!"
        )
        
        # Уведомляем администратора
        notify_admin(f"<b>Информация:</b> У пользователя {username} (ID: {user_id}) уже есть активная подписка")
        return
    
    # Создаем ссылку на оплату
    payment_data = create_payment_link(user_id)
    
    if payment_data and "paymentUrl" in payment_data:
        markup = types.InlineKeyboardMarkup()
        payment_button = types.InlineKeyboardButton(
            text="Оплатить подписку", 
            url=payment_data["paymentUrl"]
        )
        markup.add(payment_button)
        
        bot.send_message(
            message.chat.id,
            "Для оформления подписки нажмите на кнопку ниже:",
            reply_markup=markup
        )
        
        logger.info(f"Создана ссылка на оплату для пользователя {username} (ID: {user_id})")
        
        # Уведомляем администратора
        admin_message = f"<b>Создана ссылка на оплату</b>\n\n" \
                       f"<b>Пользователь:</b> {username} (ID: {user_id})\n" \
                       f"<b>ID счета:</b> {payment_data.get('id', 'Н/Д')}"
        notify_admin(admin_message)
    else:
        bot.send_message(
            message.chat.id,
            "Произошла ошибка при создании ссылки на оплату. Пожалуйста, попробуйте позже."
        )
        logger.error(f"Не удалось создать ссылку на оплату для пользователя {username} (ID: {user_id})")
        
        # Уведомляем администратора об ошибке
        notify_admin(f"<b>ОШИБКА:</b> Не удалось создать ссылку на оплату для пользователя {username} (ID: {user_id})")

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
    
    # Отменяем подписку
    if cancel_subscription(user_id, contract_id):
        # Удаляем пользователя из канала
        remove_user_from_channel(user_id)
        
        bot.answer_callback_query(call.id, "Подписка успешно отменена")
        bot.edit_message_text(
            "Ваша подписка успешно отменена.",
            call.message.chat.id,
            call.message.message_id
        )
        
        # Уведомляем администратора об успешной отмене
        notify_admin(f"<b>Подписка успешно отменена</b>\n\n" \
                    f"<b>Пользователь:</b> {username} (ID: {user_id})\n" \
                    f"<b>ID контракта:</b> {contract_id}")
    else:
        bot.answer_callback_query(call.id, "Ошибка при отмене подписки")
        bot.send_message(
            call.message.chat.id,
            "Произошла ошибка при отмене подписки. Пожалуйста, попробуйте позже."
        )
        
        # Уведомляем администратора об ошибке
        notify_admin(f"<b>ОШИБКА:</b> Не удалось отменить подписку для пользователя {username} (ID: {user_id}), контракт {contract_id}")

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
