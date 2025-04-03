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
MAIN_MESSAGE = bytes(os.getenv("MAIN_MESSAGE", 
    r"👋 Добро пожаловать!\n\n"
    r"Этот бот поможет вам управлять подпиской на закрытый канал.\n"
    r"Выберите действие из меню ниже:"
), 'utf-8').decode('unicode_escape')

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
    amount: float
    currency: str
    timestamp: str
    status: str
    errorMessage: str = ""

# Инициализация базы данных
def init_db():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
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
    conn.close()
    logger.info(f"База данных инициализирована по пути: {DB_PATH}")

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

# Маршруты
@app.on_event("startup")
async def startup_event():
    init_db()
    logger.info("Сервер запущен")

@app.get("/")
async def root(_: str = Depends(verify_credentials)):
    return {"status": "ok", "message": "Lava.top webhook service is running"}

@app.post("/webhook/lava")
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
        
        # Обрабатываем различные типы событий
        if payload.eventType == "payment.success":
            logger.info(f"Успешная оплата подписки: {payload.contractId}")
        elif payload.eventType == "payment.failed":
            logger.warning(f"Ошибка при оформлении подписки: {payload.contractId}, причина: {payload.errorMessage}")
        elif payload.eventType == "subscription.recurring.payment.success":
            logger.info(f"Успешное продление подписки: {payload.contractId}, родительский контракт: {payload.parentContractId}")
        elif payload.eventType == "subscription.recurring.payment.failed":
            logger.warning(f"Ошибка при продлении подписки: {payload.contractId}, причина: {payload.errorMessage}")
        
        return {"status": "success", "message": "Webhook processed successfully"}
    
    except Exception as e:
        logger.error(f"Ошибка при обработке веб-хука: {str(e)}", exc_info=True)
        return {"status": "error", "message": str(e)}

@app.post("/admin/reset_db")
async def reset_database(request: Request, username: str = Depends(verify_credentials)):
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        
        # Удаляем все таблицы
        cursor.execute("DROP TABLE IF EXISTS payments")
        cursor.execute("DROP TABLE IF EXISTS channel_members")
        
        # Пересоздаем таблицы
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
        
        cursor.execute('''
        CREATE TABLE IF NOT EXISTS channel_members (
            user_id TEXT PRIMARY KEY,
            status TEXT NOT NULL,
            joined_at TEXT NOT NULL,
            expires_at TEXT
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