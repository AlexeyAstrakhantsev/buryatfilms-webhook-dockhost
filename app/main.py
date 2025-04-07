import os
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, Any, Union

from fastapi import FastAPI, Depends, HTTPException, Request, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials
import secrets
import sqlite3
import json
from pydantic import BaseModel
from fastapi.responses import RedirectResponse
import hashlib
import base64
import time
import requests

# Настройка логирования
DATA_DIR = Path("/mount/database")
DATA_DIR.mkdir(exist_ok=True)

log_file = DATA_DIR / f"lava_webhook_{datetime.now().strftime('%Y%m%d')}.log"
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler(log_file),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger("lava_webhook")

# Инициализация FastAPI
app = FastAPI(title="Lava.top Webhook Service")
security = HTTPBasic()

# Получение настроек из переменных окружения
USERNAME = os.getenv("WEBHOOK_USERNAME", "admin")
PASSWORD = os.getenv("WEBHOOK_PASSWORD", "password")
DB_PATH = DATA_DIR / "lava_payments.db"

# Модели данных
class Product(BaseModel):
    id: str
    title: str

class Buyer(BaseModel):
    email: str

class WebhookPayload(BaseModel):
    eventType: str
    product: Product
    buyer: Buyer
    contractId: str
    parentContractId: Optional[str] = None
    amount: Optional[float] = None
    currency: Optional[str] = None
    timestamp: Optional[str] = None
    status: Optional[str] = None
    errorMessage: Optional[str] = ""
    cancelledAt: Optional[str] = None
    willExpireAt: Optional[str] = None

# Добавляем новую модель для запроса сокращения ссылки
class ShortenLinkRequest(BaseModel):
    original_url: str

# Инициализация базы данных
def init_db():
    """Инициализация базы данных при запуске"""
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        
        # Создаем таблицу payments
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
        
        # Создаем таблицу channel_members
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS channel_members (
            user_id TEXT PRIMARY KEY,
            status TEXT NOT NULL,
            joined_at TEXT NOT NULL,
            expires_at TEXT,
            subscription_end_date TEXT,
            last_payment_id INTEGER,
            FOREIGN KEY (last_payment_id) REFERENCES payments(id)
        )
        ''')
        
        # Создаем таблицу для сокращенных ссылок
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS shortened_links (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            short_code TEXT UNIQUE NOT NULL,
            original_url TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        ''')
        
        conn.commit()
        logger.info("База данных успешно инициализирована")
        
    except Exception as e:
        logger.error(f"Ошибка при инициализации БД: {str(e)}")
    finally:
        conn.close()

# Проверка авторизации
def verify_credentials(credentials: HTTPBasicCredentials = Depends(security)):
    correct_username = secrets.compare_digest(credentials.username, USERNAME)
    correct_password = secrets.compare_digest(credentials.password, PASSWORD)
    
    if not (correct_username and correct_password):
        logger.warning(f"Неудачная попытка авторизации: {credentials.username}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Неверные учетные данные",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username

# Сохранение данных в БД
def save_to_db(payload: WebhookPayload, raw_data: str):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    if payload.eventType == "subscription.cancelled":
        # Для события отмены подписки
        cursor.execute('''
        INSERT INTO payments (
            event_type, product_id, product_title, buyer_email, contract_id, 
            parent_contract_id, timestamp, status, raw_data, received_at,
            amount, currency
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            payload.eventType,
            payload.product.id,
            payload.product.title,
            payload.buyer.email,
            payload.contractId,
            payload.parentContractId,
            payload.cancelledAt,
            'cancelled',
            raw_data,
            datetime.now().isoformat(),
            0,  # amount для отмены не важен
            'RUB'  # валюта для отмены не важна
        ))
        
        # Обновляем статус в channel_members
        cursor.execute('''
        UPDATE channel_members 
        SET status = 'cancelled',
            subscription_end_date = ?
        WHERE user_id = ? AND status = 'active'
        ''', (
            payload.willExpireAt,
            payload.buyer.email.split('@')[0]
        ))
    else:
        # Для остальных событий оставляем старую логику
        cursor.execute('''
        INSERT INTO payments (
            event_type, product_id, product_title, buyer_email, contract_id, 
            parent_contract_id, amount, currency, timestamp, status, 
            error_message, raw_data, received_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            payload.eventType,
            payload.product.id,
            payload.product.title,
            payload.buyer.email,
            payload.contractId,
            payload.parentContractId,
            payload.amount,
            payload.currency,
            payload.timestamp,
            payload.status,
            payload.errorMessage,
            raw_data,
            datetime.now().isoformat()
        ))
    
    conn.commit()
    conn.close()
    logger.info(f"Данные сохранены в БД: {payload.eventType}, contractId: {payload.contractId}")

# Функция для генерации короткого кода
def generate_short_code(url: str) -> str:
    # Создаем хеш из URL и текущего времени
    hash_input = f"{url}{time.time()}"
    hash_object = hashlib.sha256(hash_input.encode())
    # Берем первые 8 символов base64-encoded хеша
    short_code = base64.urlsafe_b64encode(hash_object.digest())[:8].decode()
    return short_code

# В main.py добавим функцию для прямой отправки уведомлений в бот
def notify_bot(user_id: str, message: str, markup=None):
    try:
        from bot import bot  # Импортируем экземпляр бота
        
        if markup:
            bot.send_message(user_id, message, reply_markup=markup)
        else:
            bot.send_message(user_id, message)
            
        return True
    except Exception as e:
        logger.error(f"Ошибка при отправке уведомления в бот: {str(e)}")
        return False

# Маршруты
@app.on_event("startup")
async def startup_event():
    init_db()
    logger.info("Сервер запущен")

@app.get("/")
async def root(_: str = Depends(verify_credentials)):
    return {"status": "ok", "message": "Lava.top webhook service is running"}

@app.post("/lava/payment")
async def lava_webhook(request: Request, username: str = Depends(verify_credentials)):
    try:
        # Получаем тело запроса
        body = await request.body()
        raw_data = body.decode("utf-8")
        
        # Логируем входящие данные
        logger.info(f"Получены данные от lava.top: {raw_data}")
        
        # Парсим JSON
        payload = WebhookPayload.parse_raw(raw_data)
        
        # Сохраняем в БД
        save_to_db(payload, raw_data)
        
        # Получаем user_id из email
        user_id = payload.buyer.email.split('@')[0]
        
        # Импортируем функции из bot.py
        from bot import add_user_to_channel, notify_admin, bot
        
        # Обрабатываем успешный платеж
        if payload.eventType == "payment.success":
            # Отправляем уведомление пользователю
            bot.send_message(
                user_id,
                f"✅ Поздравляем! Ваша подписка '{payload.product.title}' успешно оплачена.\n"
                f"Сумма: {payload.amount} {payload.currency}"
            )
            
            # Добавляем пользователя в канал
            if add_user_to_channel(user_id):
                logger.info(f"Пользователь {user_id} успешно добавлен в канал")
                
                # Уведомляем администратора
                notify_admin(
                    f"🎉 <b>Новая подписка</b>\n\n"
                    f"<b>Пользователь:</b> {user_id}\n"
                    f"<b>Подписка:</b> {payload.product.title}\n"
                    f"<b>Сумма:</b> {payload.amount} {payload.currency}"
                )
            else:
                logger.error(f"Не удалось добавить пользователя {user_id} в канал")
                
        # Обрабатываем неудачный платеж
        elif payload.eventType == "payment.failed":
            bot.send_message(
                user_id,
                f"❌ К сожалению, оплата подписки '{payload.product.title}' не удалась.\n"
                f"Причина: {payload.errorMessage}\n\n"
                f"Вы можете попробовать снова, используя команду /subscribe"
            )
            
            # Показываем основное меню
            from bot import types, SUPPORT_USERNAME, show_main_menu
            
            # Сначала создаем сообщение, чтобы затем на него повесить меню
            menu_message = bot.send_message(
                user_id,
                "⠀⠀⠀⠀⠀Выберите пункт меню⠀⠀⠀⠀⠀"
            )
            
            # Показываем главное меню пользователю после неудачной оплаты
            show_main_menu(menu_message)
            
            # Уведомляем администратора о неудачном платеже
            notify_admin(
                f"❌ <b>Неудачный платеж</b>\n\n"
                f"<b>Пользователь:</b> {user_id}\n"
                f"<b>Подписка:</b> {payload.product.title}\n"
                f"<b>Причина:</b> {payload.errorMessage}"
            )
        
        return {"status": "success", "message": "Webhook processed successfully"}
    
    except Exception as e:
        logger.error(f"Ошибка при обработке веб-хука: {str(e)}")
        return {"status": "error", "message": str(e)}

@app.post("/admin/reset_db")
async def reset_database(request: Request, username: str = Depends(verify_credentials)):
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        
        # Удаляем все таблицы
        cursor.execute("DROP TABLE IF EXISTS payments")
        cursor.execute("DROP TABLE IF EXISTS channel_members")
        
        # Создаем таблицу payments
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
        
        # Создаем таблицу channel_members с обновленной структурой
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS channel_members (
            user_id TEXT PRIMARY KEY,
            status TEXT NOT NULL,
            joined_at TEXT NOT NULL,
            expires_at TEXT,
            subscription_end_date TEXT,
            last_payment_id INTEGER,
            FOREIGN KEY (last_payment_id) REFERENCES payments(id)
        )
        ''')
        
        conn.commit()
        conn.close()
        
        logger.info("База данных успешно сброшена администратором")
        return {"status": "success", "message": "База данных успешно сброшена"}
        
    except Exception as e:
        logger.error(f"Ошибка при сбросе базы данных: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(e)
        )

@app.post("/shorten")
async def shorten_url(request: ShortenLinkRequest, username: str = Depends(verify_credentials)):
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        
        # Генерируем короткий код
        short_code = generate_short_code(request.original_url)
        
        # Сохраняем в базу данных
        cursor.execute('''
        INSERT INTO shortened_links (short_code, original_url, created_at)
        VALUES (?, ?, ?)
        ''', (short_code, request.original_url, datetime.now().isoformat()))
        
        conn.commit()
        conn.close()
        
        # Возвращаем короткий код
        return {"short_code": short_code}
        
    except Exception as e:
        logger.error(f"Ошибка при сокращении ссылки: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(e)
        )

@app.get("/payment/{short_code}")
async def redirect_to_original(short_code: str):
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        
        # Получаем оригинальный URL
        cursor.execute('SELECT original_url FROM shortened_links WHERE short_code = ?', (short_code,))
        result = cursor.fetchone()
        conn.close()
        
        if result:
            return RedirectResponse(url=result[0])
        else:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Ссылка не найдена"
            )
            
    except Exception as e:
        logger.error(f"Ошибка при перенаправлении: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(e)
        ) 