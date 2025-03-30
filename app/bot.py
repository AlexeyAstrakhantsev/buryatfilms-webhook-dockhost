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
    else:
        bot.send_message(
            message.chat.id,
            "Произошла ошибка при создании ссылки на оплату. Пожалуйста, попробуйте позже."
        )
        logger.error(f"Не удалось создать ссылку на оплату для пользователя {username} (ID: {user_id})")

@bot.message_handler(commands=['status'])
def status_command(message):
    user_id = message.from_user.id
    username = message.from_user.username or f"user_{user_id}"
    
    logger.info(f"Пользователь {username} (ID: {user_id}) запросил статус подписки")
    
    subscription = check_subscription_status(user_id)
    
    if subscription["status"] == "active":
        data = subscription["data"]
        bot.send_message(
            message.chat.id,
            f"У вас есть активная подписка!\n"
            f"Продукт: {data[3]}\n"
            f"Дата активации: {data[9]}\n"
            f"Сумма: {data[7]} {data[8]}"
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

# Функция для запуска бота в отдельном потоке
def run_bot():
    logger.info("Запуск бота...")
    bot.polling(none_stop=True, interval=0)

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
