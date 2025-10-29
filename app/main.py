import os
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional, Dict, Any, Union
import asyncio

from fastapi import FastAPI, Depends, HTTPException, Request, status, BackgroundTasks
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

# Вспомогательная функция для нормализации строковых представлений дат
def normalize_datetime_string(dt_str: Optional[str]) -> Optional[str]:
    if not dt_str:
        return None
    try:
        # Сначала пробуем парсить как ISO с часовым поясом
        dt_obj = datetime.fromisoformat(dt_str.replace('Z', '+00:00'))
    except ValueError:
        try:
            # Если не удалось, пробуем парсить как naive datetime и делаем его aware в UTC
            dt_obj = datetime.strptime(dt_str, '%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone.utc)
        except ValueError:
            logger.warning(f"Не удалось распарсить дату: {dt_str}. Возвращаем исходную строку.")
            return dt_str # В случае полной неудачи возвращаем исходную строку
    return dt_obj.isoformat() # Всегда возвращаем в ISO формате с часовым поясом

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
def save_to_db(payload: WebhookPayload, raw_data: str) -> Optional[int]:
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    payment_id = None

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
            normalize_datetime_string(payload.cancelledAt), # Нормализуем дату
            'cancelled',
            raw_data,
            datetime.now().isoformat(),
            0,  # amount для отмены не важен
            'RUB'  # валюта для отмены не важна
        ))
        conn.commit()
        payment_id = cursor.lastrowid # Получаем ID только что вставленной записи
        
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
            normalize_datetime_string(payload.timestamp), # Нормализуем дату
            payload.status,
            payload.errorMessage,
            raw_data,
            datetime.now(timezone.utc).isoformat() # Используем aware datetime
        ))
        conn.commit()
        payment_id = cursor.lastrowid # Получаем ID только что вставленной записи
    
    conn.close()
    logger.info(f"Данные сохранены в БД: {payload.eventType}, contractId: {payload.contractId}, Payment ID: {payment_id}")
    return payment_id

# Функция для генерации короткого кода
def generate_short_code(url: str) -> str:
    # Создаем хеш из URL и текущего времени
    hash_input = f"{url}{time.time()}"
    hash_object = hashlib.sha256(hash_input.encode())
    # Берем первые 8 символов base64-encoded хеша
    short_code = base64.urlsafe_b64encode(hash_object.digest())[:8].decode()
    return short_code

# Функция для очистки старых сокращенных ссылок
def cleanup_old_shortened_links(days_to_keep=7, force=False):
    """
    Удаляет сокращенные ссылки старше указанного количества дней.
    Параметр force=True игнорирует проверку количества и всегда выполняет очистку.
    """
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        
        # Получаем общее количество ссылок
        cursor.execute('SELECT COUNT(*) FROM shortened_links')
        total_links = cursor.fetchone()[0]
        
        # Очищаем только если количество ссылок превышает порог или установлен force=True
        if total_links > 1000 or force:
            # Рассчитываем дату, старше которой ссылки будут удалены
            cutoff_date = (datetime.now() - timedelta(days=days_to_keep)).isoformat()
            
            # Получаем количество ссылок до очистки
            cursor.execute('SELECT COUNT(*) FROM shortened_links')
            count_before = cursor.fetchone()[0]
            
            # Удаляем старые ссылки
            cursor.execute('DELETE FROM shortened_links WHERE created_at < ?', (cutoff_date,))
            
            # Получаем количество ссылок после очистки
            cursor.execute('SELECT COUNT(*) FROM shortened_links')
            count_after = cursor.fetchone()[0]
            
            deleted_count = count_before - count_after
            
            conn.commit()
            conn.close()
            
            if deleted_count > 0:
                logger.info(f"Очищено {deleted_count} устаревших сокращенных ссылок")
            
            return deleted_count
        else:
            conn.close()
            return 0
    
    except Exception as e:
        logger.error(f"Ошибка при очистке старых сокращенных ссылок: {str(e)}")
        return 0

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

# Фоновая задача для периодической очистки ссылок
async def periodic_cleanup_task():
    while True:
        try:
            # Проверяем количество ссылок
            conn = sqlite3.connect(DB_PATH)
            cursor = conn.cursor()
            cursor.execute('SELECT COUNT(*) FROM shortened_links')
            total_links = cursor.fetchone()[0]
            conn.close()
            
            # Определяем интервал проверки в зависимости от размера базы
            if total_links > 5000:
                # Много ссылок - короткий интервал (каждые 3 часа)
                cleanup_interval = 10800
                cleanup_count = cleanup_old_shortened_links(days_to_keep=3)
            elif total_links > 1000:
                # Средний размер базы - средний интервал (каждые 12 часов)
                cleanup_interval = 43200
                cleanup_count = cleanup_old_shortened_links(days_to_keep=5)
            else:
                # Малый размер базы - длинный интервал (раз в день)
                cleanup_interval = 86400
                cleanup_count = cleanup_old_shortened_links(days_to_keep=7, force=False)
            
            if cleanup_count > 0:
                logger.info(f"Плановая очистка завершена, удалено {cleanup_count} ссылок. Следующая через {cleanup_interval // 3600} ч.")
            
            # Ждем до следующей проверки
            await asyncio.sleep(cleanup_interval)
            
        except Exception as e:
            logger.error(f"Ошибка в фоновой задаче очистки ссылок: {str(e)}")
            # Ждем 1 час перед повторной попыткой в случае ошибки
            await asyncio.sleep(3600)

# Запуск фоновой задачи
@app.on_event("startup")
async def start_cleanup_task():
    asyncio.create_task(periodic_cleanup_task())

# Маршруты
@app.on_event("startup")
async def startup_event():
    init_db()
    # Первоначальная очистка старых ссылок при запуске сервера
    cleanup_old_shortened_links(days_to_keep=30, force=True)  # При первом запуске выполняем принудительную очистку
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
        payment_id = save_to_db(payload, raw_data)
        
        # Получаем user_id из email
        user_id = payload.buyer.email.split('@')[0]
        
        # Импортируем функции из bot.py
        from bot import add_user_to_channel, notify_admin, bot, get_periodicity_by_amount, PERIOD_DAYS
        
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
                
        # Обрабатываем автоматическое продление подписки
        elif payload.eventType == "subscription.recurring.payment.success":
            # Получаем текущую дату окончания подписки из БД
            conn = sqlite3.connect(DB_PATH)
            cursor = conn.cursor()
            cursor.execute("SELECT subscription_end_date FROM channel_members WHERE user_id = ?", (user_id,))
            current_end_date_row = cursor.fetchone()
            conn.close()

            current_end_date: datetime
            try:
                if current_end_date_row and current_end_date_row[0]:
                    current_end_date = datetime.fromisoformat(str(current_end_date_row[0]).replace('Z', '+00:00'))
                else:
                    current_end_date = datetime.now(timezone.utc)
            except Exception:
                # В случае некорректного формата даты в БД — начинаем от текущего момента
                current_end_date = datetime.now(timezone.utc)

            # Время события продления (если отсутствует — используем текущее время)
            try:
                event_time = datetime.fromisoformat(normalize_datetime_string(payload.timestamp).replace('Z', '+00:00')) if payload.timestamp else datetime.now(timezone.utc)
            except Exception:
                event_time = datetime.now(timezone.utc)

            # Определяем периодичность по сумме и рассчитываем длительность периода
            periodicity = get_periodicity_by_amount(payload.amount)
            days_to_add = PERIOD_DAYS.get(periodicity, 30)

            # Продлеваем от максимума между текущим окончанием и временем события
            base_date = current_end_date if current_end_date > event_time else event_time
            new_end_date_dt = base_date + timedelta(days=days_to_add)
            new_end_date = new_end_date_dt.replace(tzinfo=new_end_date_dt.tzinfo or timezone.utc).isoformat()

            # Обновляем статус подписки в channel_members
            conn = sqlite3.connect(DB_PATH)
            cursor = conn.cursor()
            cursor.execute('''
            UPDATE channel_members 
            SET status = 'active', 
                subscription_end_date = ?,
                last_payment_id = ?
            WHERE user_id = ?
            ''', (new_end_date, payment_id, user_id))
            conn.commit()
            conn.close()

            # Отправляем уведомление пользователю
            from bot import types, CHANNEL_LINK, show_main_menu

            markup = types.InlineKeyboardMarkup(row_width=1)
            btn_channel = types.InlineKeyboardButton('📺 Войти в канал', url=CHANNEL_LINK)
            btn_menu = types.InlineKeyboardButton('🔙 Главное меню', callback_data='show_menu')
            markup.add(btn_channel, btn_menu)

            bot.send_message(
                user_id,
                f"✅ Ваша подписка '{payload.product.title}' автоматически продлена!\n"
                f"Новая дата окончания: {new_end_date_dt.strftime('%d.%m.%Y')}",
                reply_markup=markup
            )

            # Уведомляем администратора
            formatted_end_date = new_end_date_dt.strftime('%d.%m.%Y')
            notify_admin(
                f"🔄 <b>Автопродление подписки</b>\n\n"
                f"<b>Пользователь:</b> {user_id}\n"
                f"<b>Подписка:</b> {payload.product.title}\n"
                f"<b>Сумма:</b> {payload.amount} {payload.currency}\n"
                f"<b>Новая дата окончания:</b> {formatted_end_date}"
            )
            logger.info(f"Подписка пользователя {user_id} успешно продлена до {new_end_date}")

        elif payload.eventType == "subscription.cancelled": # Добавляем обработку отмены подписки
            conn = sqlite3.connect(DB_PATH)
            cursor = conn.cursor()
            cursor.execute('''
            UPDATE channel_members 
            SET status = 'cancelled',
                subscription_end_date = ?
            WHERE user_id = ? AND status = 'active'
            ''', (
                normalize_datetime_string(payload.willExpireAt), # Нормализуем дату
                user_id
            ))
            conn.commit()
            conn.close()
            logger.info(f"Статус подписки пользователя {user_id} обновлен на 'cancelled' (webhook)")

            # Отправляем уведомление пользователю, если есть willExpireAt
            if payload.willExpireAt:
                from bot import bot, types, show_main_menu
                # Используем normalize_datetime_string для получения корректной даты для отображения
                normalized_will_expire_at = normalize_datetime_string(payload.willExpireAt)
                end_date_str = datetime.fromisoformat(normalized_will_expire_at.replace('Z', '+00:00')).strftime("%d.%m.%Y") if normalized_will_expire_at else "не определена"
                bot.send_message(
                    user_id,
                    f"ℹ️ Автопродление подписки отключено.\n\n"
                    f"Доступ к каналу будет действовать до: {end_date_str}."
                )
                menu_message = bot.send_message(user_id, "⠀⠀⠀⠀⠀Меню подписчика⠀⠀⠀⠀⠀")
                show_main_menu(menu_message)
                notify_admin(
                    f"🔔 <b>Отмена подписки (через webhook)</b>\n\n"
                    f"Пользователь: {user_id}\n"
                    f"Доступ активен до: {end_date_str}"
                )
            else:
                logger.warning(f"Отмена подписки для {user_id} через webhook, но без willExpireAt.")
                bot.send_message(
                    user_id,
                    "ℹ️ Автопродление подписки отключено."
                )
                menu_message = bot.send_message(user_id, "⠀⠀⠀⠀⠀Меню подписчика⠀⠀⠀⠀⠀")
                show_main_menu(menu_message)
                notify_admin(
                    f"🔔 <b>Отмена подписки (через webhook)</b>\n\n"
                    f"Пользователь: {user_id}\n"
                    f"Доступ был отменен. (Дата окончания не указана)"
                )

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
        # Убираем запуск очистки при каждом запросе
        # cleanup_old_shortened_links()
        
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