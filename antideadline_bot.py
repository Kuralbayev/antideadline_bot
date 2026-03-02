import asyncio
import logging
import sqlite3
import calendar
import json
import aiohttp # pyright: ignore[reportMissingImports]
from dotenv import load_dotenv
load_dotenv()
from datetime import datetime, timedelta
from typing import Optional, Tuple
import pytz # pyright: ignore[reportMissingModuleSource]
from aiogram import Bot, Dispatcher, Router, F # pyright: ignore[reportMissingImports]
from aiogram.filters import Command # pyright: ignore[reportMissingImports]
from aiogram.fsm.context import FSMContext # pyright: ignore[reportMissingImports]
from aiogram.fsm.state import State, StatesGroup # pyright: ignore[reportMissingImports]
from aiogram.fsm.storage.memory import MemoryStorage # pyright: ignore[reportMissingImports]
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton # pyright: ignore[reportMissingImports]

# ══════════════════════════════════════════════════════════════════════════════
# КОНСТАНТЫ
# ══════════════════════════════════════════════════════════════════════════════

TOKEN = "8290811750:AAE2L-SbrizzIesbtuyRKNCnHh39efU6wCw"
ADMIN_ID = 7124275081
TIMEZONE = "Asia/Almaty"
DB_NAME = "deadlines.db"

# AI Grok
import os
GROK_API_KEY = os.getenv("GROK_API_KEY")
GROK_API_URL = "https://api.x.ai/v1/chat/completions"
GROK_MODEL = "grok-2-1212"
AI_TIMEOUT = 30
AI_MAX_RETRIES = 2
AI_CONFIDENCE_THRESHOLD = 0.7

# Payment
KASPI_PHONE = "+77751875748"
PREMIUM_PRICE = 990
PREMIUM_DAYS = 30

# Validation
MIN_SUBJECT_LENGTH = 2
MAX_SUBJECT_LENGTH = 100
MAX_NOTE_LENGTH = 500
MIN_HOUR = 9
MAX_HOUR = 18
ITEMS_PER_PAGE = 5

NO_WORDS = ["нет", "no", "н", "-", "skip", "пропустить"]

# ══════════════════════════════════════════════════════════════════════════════
# LOGGING
# ══════════════════════════════════════════════════════════════════════════════

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# ══════════════════════════════════════════════════════════════════════════════
# DATABASE
# ══════════════════════════════════════════════════════════════════════════════

def init_database():
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS deadlines (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        subject TEXT NOT NULL,
        date TEXT NOT NULL,
        note TEXT,
        created_at TEXT NOT NULL,
        status TEXT DEFAULT 'pending'
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS sent_reminders (
        deadline_id INTEGER NOT NULL,
        reminder_type TEXT NOT NULL,
        sent_at TEXT NOT NULL,
        PRIMARY KEY (deadline_id, reminder_type),
        FOREIGN KEY (deadline_id) REFERENCES deadlines(id) ON DELETE CASCADE
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS subjects (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        name TEXT NOT NULL,
        created_at TEXT NOT NULL,
        UNIQUE (user_id, name)
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS premium_subscriptions (
        user_id INTEGER PRIMARY KEY,
        is_premium INTEGER DEFAULT 0,
        subscribed_at TEXT,
        expires_at TEXT,
        ai_requests_count INTEGER DEFAULT 0,
        last_request_date TEXT
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS pending_payments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        username TEXT,
        full_name TEXT,
        created_at TEXT NOT NULL,
        status TEXT DEFAULT 'pending',
        screenshot_file_id TEXT,
        rejected_reason TEXT
    )""")
    c.execute("CREATE INDEX IF NOT EXISTS idx_user_date ON deadlines(user_id, date)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_status_date ON deadlines(status, date)")
    conn.commit()
    conn.close()
    logger.info("Database initialized")

init_database()

def db():
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    return conn

# ══════════════════════════════════════════════════════════════════════════════
# FSM STATES
# ══════════════════════════════════════════════════════════════════════════════

class AddDeadlineStates(StatesGroup):
    waiting_new_subject = State()
    waiting_date = State()
    waiting_hour = State()
    waiting_minute = State()
    waiting_note = State()

class EditDeadlineStates(StatesGroup):
       waiting_edit_date = State()
       waiting_edit_hour = State()
       waiting_edit_minute = State()
       waiting_edit_note = State()

class AIAssistantStates(StatesGroup):
    waiting_input = State()
    waiting_confirmation = State()
    waiting_screenshot = State()

class AdminStates(StatesGroup):
    waiting_reject_reason = State()

# ══════════════════════════════════════════════════════════════════════════════
# HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def validate_subject(subject: str) -> Tuple[bool, str]:
    subject = subject.strip()
    if len(subject) < MIN_SUBJECT_LENGTH:
        return False, f"❌ Слишком короткое (мин. {MIN_SUBJECT_LENGTH})"
    if len(subject) > MAX_SUBJECT_LENGTH:
        return False, f"❌ Слишком длинное (макс. {MAX_SUBJECT_LENGTH})"
    reserved = ["мой дедлайн", "мои дедлайны", "предметы", "назад"]
    if subject.lower() in reserved:
        return False, "❌ Название зарезервировано"
    return True, subject

def validate_time(dt: datetime) -> Tuple[bool, str]:
    tz = pytz.timezone(TIMEZONE)
    now = datetime.now(tz)
    if dt.tzinfo is None:
        dt = tz.localize(dt)
    if dt < now:
        return False, "❌ Дедлайн в прошлом"
    if not (MIN_HOUR <= dt.hour < MAX_HOUR):
        return False, f"⏰ Время {MIN_HOUR:02d}:00-{MAX_HOUR-1:02d}:59"
    return True, ""

def fmt_deadline(row) -> str:
    emoji = {"pending": "⏳", "reminded": "🔔", "overdue": "🔴"}.get(row["status"], "❓")
    try:
        ds = datetime.strptime(row["date"], "%Y-%m-%d %H:%M").strftime("%d.%m.%Y в %H:%M")
    except:
        ds = row["date"]
    t = f"{emoji} <b>{row['subject']}</b>\n📅 {ds}"
    if row["note"]:
        note = row["note"][:100] + ("..." if len(row["note"]) > 100 else "")
        t += f"\n💬 {note}"
    return t

async def safe_edit(msg: Message, text: str, markup=None, parse_mode="HTML"):
    try:
        await msg.edit_text(text, reply_markup=markup, parse_mode=parse_mode)
    except:
        try:
            await msg.edit_reply_markup(reply_markup=None)
        except:
            pass
        await msg.answer(text, reply_markup=markup, parse_mode=parse_mode)

# ══════════════════════════════════════════════════════════════════════════════
# AI FUNCTIONS
# ══════════════════════════════════════════════════════════════════════════════

async def check_premium(user_id: int) -> bool:
    try:
        conn = db()
        c = conn.cursor()
        c.execute("SELECT is_premium, expires_at FROM premium_subscriptions WHERE user_id=?", (user_id,))
        row = c.fetchone()
        conn.close()
        
        if not row:
            conn = db()
            c = conn.cursor()
            c.execute("INSERT INTO premium_subscriptions (user_id) VALUES (?)", (user_id,))
            conn.commit()
            conn.close()
            return False
        
        if row["is_premium"] and row["expires_at"]:
            if datetime.strptime(row["expires_at"], "%Y-%m-%d %H:%M:%S") < datetime.now():
                conn = db()
                c = conn.cursor()
                c.execute("UPDATE premium_subscriptions SET is_premium=0 WHERE user_id=?", (user_id,))
                conn.commit()
                conn.close()
                return False
        
        return bool(row["is_premium"])
    except Exception as e:
        logger.error(f"check_premium: {e}")
        return False

async def check_ai_limit(user_id: int) -> Tuple[bool, int, int]:
    """Returns: (allowed, used, limit)"""
    try:
        is_prem = await check_premium(user_id)
        limit = 100 if is_prem else 3
        today = datetime.now().strftime("%Y-%m-%d")
        
        conn = db()
        c = conn.cursor()
        c.execute(
            "SELECT ai_requests_count FROM premium_subscriptions WHERE user_id=? AND last_request_date=?",
            (user_id, today)
        )
        row = c.fetchone()
        conn.close()
        
        used = row['ai_requests_count'] if row else 0
        return used < limit, used, limit
    except Exception as e:
        logger.error(f"check_ai_limit: {e}")
        return True, 0, 100

async def increment_ai_usage(user_id: int):
    try:
        today = datetime.now().strftime("%Y-%m-%d")
        conn = db()
        c = conn.cursor()
        
        # Ensure user exists
        c.execute("INSERT OR IGNORE INTO premium_subscriptions (user_id) VALUES (?)", (user_id,))
        
        # Reset if new day
        c.execute(
            "UPDATE premium_subscriptions SET ai_requests_count=0 WHERE user_id=? AND last_request_date<?",
            (user_id, today)
        )
        
        # Increment
        c.execute(
            "UPDATE premium_subscriptions SET ai_requests_count=ai_requests_count+1, last_request_date=? WHERE user_id=?",
            (today, user_id)
        )
        
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"increment_ai_usage: {e}")

async def call_grok(prompt: str, system: str = None, max_tokens: int = 1000) -> dict:
    msgs = []
    if system:
        msgs.append({"role": "system", "content": system})
    msgs.append({"role": "user", "content": prompt})
    
    headers = {
        "Authorization": f"Bearer {GROK_API_KEY}",
        "Content-Type": "application/json"
    }
    payload = {
        "model": GROK_MODEL,
        "messages": msgs,
        "temperature": 0.3,
        "max_tokens": max_tokens
    }
    
    for attempt in range(AI_MAX_RETRIES):
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    GROK_API_URL,
                    json=payload,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=AI_TIMEOUT)
                ) as response:
                    if response.status == 200:
                        data = await response.json()
                        return {"success": True, "content": data["choices"][0]["message"]["content"]}
                    
                    error = await response.text()
                    logger.error(f"Grok {response.status}: {error}")
                    
                    if attempt < AI_MAX_RETRIES - 1:
                        await asyncio.sleep(2 ** attempt)
                        continue
                    
                    return {"success": False, "error": f"HTTP {response.status}"}
        
        except asyncio.TimeoutError:
            if attempt < AI_MAX_RETRIES - 1:
                await asyncio.sleep(2 ** attempt)
                continue
            return {"success": False, "error": "Timeout"}
        
        except Exception as e:
            if attempt < AI_MAX_RETRIES - 1:
                await asyncio.sleep(2 ** attempt)
                continue
            return {"success": False, "error": str(e)}
    
    return {"success": False, "error": "Max retries"}

async def ai_parse_deadline(text: str, user_id: int) -> dict:
    try:
        conn = db()
        c = conn.cursor()
        c.execute("SELECT name FROM subjects WHERE user_id=? ORDER BY name", (user_id,))
        subs = [r["name"] for r in c.fetchall()]
        conn.close()
    except:
        subs = []
    
    now = datetime.now(pytz.timezone(TIMEZONE))
    
    sys_prompt = f"""Ты AI для парсинга дедлайнов студентов.
Текущая дата: {now.strftime("%Y-%m-%d %H:%M")} (TZ: {TIMEZONE})
Извлеки: предмет, дату (YYYY-MM-DD), время (HH:MM), заметку.
Время: {MIN_HOUR:02d}:00-{MAX_HOUR-1:02d}:59. Дата - только будущее.
Предметы: {", ".join(subs) if subs else "нет"}
ОТВЕТ ТОЛЬКО JSON:
{{"success":true/false,"subject":"...","date":"YYYY-MM-DD","time":"HH:MM","note":"...","confidence":0.0-1.0,"message":"...","need_subject_selection":false,"suggested_subjects":[]}}"""
    
    resp = await call_grok(text, sys_prompt, 500)
    
    if not resp["success"]:
        return {
            "success": False,
            "subject": None,
            "date": None,
            "time": None,
            "note": None,
            "confidence": 0.0,
            "message": f"❌ AI Error: {resp.get('error')}",
            "need_subject_selection": False,
            "suggested_subjects": []
        }
    
    try:
        raw = resp["content"].strip()
        raw = raw.replace("```json", "").replace("```", "").strip()
        result = json.loads(raw)
        
        # Validate date
        if result.get("date"):
            d = datetime.strptime(result["date"], "%Y-%m-%d")
            if d.date() < now.date():
                result["success"] = False
                result["message"] = "❌ Дата в прошлом"
                result["confidence"] = 0.0
        
        # Validate time
        if result.get("time"):
            h = int(result["time"].split(":")[0])
            if not (MIN_HOUR <= h < MAX_HOUR):
                result["message"] = f"⚠️ Время {MIN_HOUR:02d}:00-{MAX_HOUR-1:02d}:59"
                result["confidence"] = max(0.0, result.get("confidence", 0.5) - 0.2)
        
        # Match subject
        if result.get("subject") and subs:
            if result["subject"] not in subs:
                matches = [s for s in subs if s.lower() == result["subject"].lower()]
                if matches:
                    result["subject"] = matches[0]
                else:
                    result["suggested_subjects"] = subs[:5]
        
        return result
    
    except json.JSONDecodeError:
        return {
            "success": False,
            "subject": None,
            "date": None,
            "time": None,
            "note": None,
            "confidence": 0.0,
            "message": "❌ JSON parse error",
            "need_subject_selection": False,
            "suggested_subjects": []
        }

async def ai_prioritize(user_id: int) -> str:
    try:
        conn = db()
        c = conn.cursor()
        now = datetime.now(pytz.timezone(TIMEZONE))
        c.execute(
            """SELECT subject, date, note FROM deadlines
               WHERE user_id=? AND status='pending' AND datetime(date)>=datetime(?)
               ORDER BY datetime(date) LIMIT 10""",
            (user_id, now.strftime("%Y-%m-%d %H:%M"))
        )
        rows = c.fetchall()
        conn.close()
        
        if not rows:
            return "📋 Нет активных дедлайнов для анализа."
        
        dl_text = "\n".join(
            f"{i+1}. {r['subject']} — {r['date']}" + (f" ({r['note']})" if r["note"] else "")
            for i, r in enumerate(rows)
        )
    except Exception as e:
        logger.error(e)
        return "❌ Ошибка загрузки"
    
    sys_prompt = "Ты AI ассистент. Проанализируй дедлайны, дай рекомендации. 200-400 слов, русский."
    resp = await call_grok(
        f"Дата: {now.strftime('%Y-%m-%d %H:%M')}\n\n{dl_text}\n\nРекомендации:",
        sys_prompt,
        1200
    )
    
    if resp["success"]:
        return f"🎯 <b>Приоритизация:</b>\n\n{resp['content']}"
    
    return f"❌ Ошибка: {resp.get('error')}"

# ══════════════════════════════════════════════════════════════════════════════
# KEYBOARDS
# ══════════════════════════════════════════════════════════════════════════════

def kb_main():
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="➕ Добавить дедлайн", callback_data="add_deadline"),
            InlineKeyboardButton(text="📋 Мои дедлайны", callback_data="my_deadlines:1")
        ],
        [
            InlineKeyboardButton(text="🤖 AI Помощник", callback_data="ai_assistant"),
            InlineKeyboardButton(text="📚 Предметы", callback_data="subjects_menu")
        ]
    ])

def kb_cancel():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Отменить", callback_data="main_menu")]
    ])

def kb_skip_note():
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="⏭ Пропустить", callback_data="skip_note"),
            InlineKeyboardButton(text="❌ Отменить", callback_data="main_menu")
        ]
    ])

def kb_calendar(year: int, month: int) -> InlineKeyboardMarkup:
    now = datetime.now()
    mnames = [
        "Январь", "Февраль", "Март", "Апрель", "Май", "Июнь",
        "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь"
    ]
    
    rows = [
        [
            InlineKeyboardButton(text="◀️", callback_data=f"cal_prev:{year}:{month}"),
            InlineKeyboardButton(text=f"{mnames[month-1]} {year}", callback_data="ignore"),
            InlineKeyboardButton(text="▶️", callback_data=f"cal_next:{year}:{month}")
        ],
        [
            InlineKeyboardButton(text=d, callback_data="ignore")
            for d in ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
        ]
    ]
    
    cal = calendar.monthcalendar(year, month)
    while len(cal) < 6:
        cal.append([0] * 7)
    
    for week in cal:
        row = []
        for day in week:
            if day == 0 or datetime(year, month, day).date() < now.date():
                row.append(InlineKeyboardButton(text=" ", callback_data="ignore"))
            else:
                row.append(
                    InlineKeyboardButton(text=str(day), callback_data=f"cal_day:{year}:{month}:{day}")
                )
        rows.append(row)
    
    rows.append([InlineKeyboardButton(text="❌ Отменить", callback_data="main_menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def kb_hours() -> InlineKeyboardMarkup:
    hrs = [
        InlineKeyboardButton(text=f"{h:02d}", callback_data=f"time_h:{h}")
        for h in range(MIN_HOUR, MAX_HOUR)
    ]
    rows = [[InlineKeyboardButton(text="🕐 Выберите час:", callback_data="ignore")]]
    for i in range(0, len(hrs), 5):
        rows.append(hrs[i:i+5])
    rows.append([InlineKeyboardButton(text="❌ Отменить", callback_data="main_menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def kb_subjects(user_id: int, page: int = 1, action: str = "select") -> InlineKeyboardMarkup:
    conn = db()
    c = conn.cursor()
    c.execute("SELECT name FROM subjects WHERE user_id=? ORDER BY name", (user_id,))
    subjects = [r["name"] for r in c.fetchall()]
    conn.close()
    
    rows = []
    
    if not subjects:
        rows.append([
            InlineKeyboardButton(
                text="➕ Создать первый предмет",
                callback_data=f"subject_new:{action}"
            )
        ])
    else:
        total = len(subjects)
        total_pages = (total + ITEMS_PER_PAGE - 1) // ITEMS_PER_PAGE
        start = (page - 1) * ITEMS_PER_PAGE
        end = start + ITEMS_PER_PAGE
        
        for s in subjects[start:end]:
            cb = f"subject_{action}:{s}"
            rows.append([InlineKeyboardButton(text=s, callback_data=cb)])
        
        nav = []
        if page > 1:
            nav.append(InlineKeyboardButton(text="◀️", callback_data=f"subjects_page:{action}:{page-1}"))
        if page < total_pages:
            nav.append(InlineKeyboardButton(text="▶️", callback_data=f"subjects_page:{action}:{page+1}"))
        if nav:
            rows.append(nav)
        
        if action == "select":
            rows.append([
                InlineKeyboardButton(text="➕ Создать новый предмет", callback_data="subject_new:select")
            ])
    
    rows.append([InlineKeyboardButton(text="❌ Отменить", callback_data="main_menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def kb_subjects_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📋 Список предметов", callback_data="subjects_list_view")],
        [InlineKeyboardButton(text="➕ Добавить предмет", callback_data="subject_add")],
        [InlineKeyboardButton(text="🏠 Главное меню", callback_data="main_menu")]
    ])

def kb_deadline_actions(did: int, page: int = 1):
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="📅 Изменить дату/время", callback_data=f"edit_datetime:{did}:{page}"),
            InlineKeyboardButton(text="💬 Изменить комментарий", callback_data=f"edit_note:{did}:{page}")
        ],
        [InlineKeyboardButton(text="🗑 Удалить", callback_data=f"delete_deadline:{did}")],
        [InlineKeyboardButton(text="◀️ К списку", callback_data=f"my_deadlines:{page}")]
    ])

def kb_ai_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✍️ Добавить дедлайн", callback_data="ai_add_deadline")],
        [InlineKeyboardButton(text="🎯 Приоритизация", callback_data="ai_prioritize")],
        [
            InlineKeyboardButton(text="ℹ️ Помощь", callback_data="ai_help"),
            InlineKeyboardButton(text="🏠 Главное меню", callback_data="main_menu")
        ]
    ])

def kb_subjects_for_ai(subjects: list):
    rows = [
        [InlineKeyboardButton(text=s, callback_data=f"ai_select_subject:{s}")]
        for s in subjects[:10]
    ]
    rows.append([InlineKeyboardButton(text="➕ Новый предмет", callback_data="ai_new_subject")])
    rows.append([InlineKeyboardButton(text="❌ Отмена", callback_data="ai_assistant")])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def kb_payment():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📸 Я оплатил — отправить скриншот", callback_data="payment_sent")],
        [InlineKeyboardButton(text="🏠 Главное меню", callback_data="main_menu")]
    ])

def kb_admin_verify(user_id: int, pay_id: int):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Подтвердить оплату", callback_data=f"admin_approve:{user_id}:{pay_id}")],
        [InlineKeyboardButton(text="❌ Отклонить", callback_data=f"admin_reject_start:{user_id}:{pay_id}")]
    ])

def kb_rejected_payment():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💬 Написать администратору", url=f"tg://user?id={ADMIN_ID}")],
        [InlineKeyboardButton(text="🔄 Попробовать снова", callback_data="buy_premium")],
        [InlineKeyboardButton(text="🏠 Главное меню", callback_data="main_menu")]
    ])

# ══════════════════════════════════════════════════════════════════════════════
# HANDLERS
# ══════════════════════════════════════════════════════════════════════════════

router = Router()

# ──────────────────────────────────────────────────────────────────────────────
# COMMANDS
# ──────────────────────────────────────────────────────────────────────────────

@router.message(Command("start", "help"))
async def cmd_start(msg: Message, state: FSMContext):
    await state.clear()
    await msg.answer(
        "👋 <b>АнтиДедлайн Бот</b>\n\n"
        "🎯 <b>Возможности:</b>\n"
        "• Управление дедлайнами\n"
        "• Автоматические напоминания\n"
        "• 🤖 AI помощник (Premium)\n\n"
        "🔔 <b>Напоминания:</b> за 1 день, 2 часа, 15 минут\n\n"
        "Выберите действие:",
        reply_markup=kb_main(),
        parse_mode="HTML"
    )

@router.message(Command("menu"))
async def cmd_menu(msg: Message, state: FSMContext):
    await state.clear()
    await msg.answer("🏠 Главное меню:", reply_markup=kb_main())

# ──────────────────────────────────────────────────────────────────────────────
# MAIN MENU
# ──────────────────────────────────────────────────────────────────────────────

@router.callback_query(F.data == "main_menu")
async def cb_main_menu(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await safe_edit(cb.message, "🏠 Главное меню:", kb_main())
    await cb.answer()

@router.callback_query(F.data == "ignore")
async def cb_ignore(cb: CallbackQuery):
    await cb.answer()

# ──────────────────────────────────────────────────────────────────────────────
# ADD DEADLINE
# ──────────────────────────────────────────────────────────────────────────────

@router.callback_query(F.data == "add_deadline")
async def cb_add_deadline(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    kbd = kb_subjects(cb.from_user.id, action="select")
    await safe_edit(cb.message, "📚 <b>Шаг 1/5: Выберите предмет</b>", kbd)
    await cb.answer()

@router.callback_query(F.data.startswith("subject_select:"))
async def cb_subject_selected(cb: CallbackQuery, state: FSMContext):
    subj = cb.data.split(":", 1)[1]
    await state.update_data(subject=subj)
    now = datetime.now()
    await safe_edit(
        cb.message,
        f"📚 Предмет: <b>{subj}</b>\n\n📅 <b>Шаг 2/5: Выберите дату</b>",
        kb_calendar(now.year, now.month)
    )
    await state.set_state(AddDeadlineStates.waiting_date)
    await cb.answer()

@router.callback_query(F.data.startswith("subject_new:"))
async def cb_subject_new(cb: CallbackQuery, state: FSMContext):
    action = cb.data.split(":")[1]
    await state.update_data(return_action=action)
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except:
        pass
    await cb.message.answer(
        f"📝 <b>Новый предмет</b>\n\nВведите название ({MIN_SUBJECT_LENGTH}-{MAX_SUBJECT_LENGTH} символов):",
        reply_markup=kb_cancel(),
        parse_mode="HTML"
    )
    await state.set_state(AddDeadlineStates.waiting_new_subject)
    await cb.answer()

@router.message(AddDeadlineStates.waiting_new_subject)
async def msg_new_subject(msg: Message, state: FSMContext):
    ok, res = validate_subject(msg.text)
    if not ok:
        await msg.answer(f"{res}\n\nПопробуйте ещё раз:", reply_markup=kb_cancel())
        return
    
    try:
        conn = db()
        c = conn.cursor()
        c.execute(
            "INSERT INTO subjects (user_id, name, created_at) VALUES (?, ?, ?)",
            (msg.from_user.id, res, datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        )
        conn.commit()
        conn.close()
        
        await state.update_data(subject=res)
        now = datetime.now()
        await msg.answer(
            f"✅ Предмет создан!\n\n📚 <b>{res}</b>\n\n📅 <b>Шаг 2/5: Выберите дату</b>",
            reply_markup=kb_calendar(now.year, now.month),
            parse_mode="HTML"
        )
        await state.set_state(AddDeadlineStates.waiting_date)
    
    except sqlite3.IntegrityError:
        await msg.answer("❌ Предмет уже существует!", reply_markup=kb_cancel())
    except Exception as e:
        logger.error(e)
        await msg.answer("❌ Ошибка. Попробуйте ещё раз:", reply_markup=kb_cancel())

@router.callback_query(
    F.data.startswith("cal_prev:") | F.data.startswith("cal_next:"),
    AddDeadlineStates.waiting_date
)
async def cb_cal_nav(cb: CallbackQuery):
    d, y, m = cb.data.split(":")
    y, m = int(y), int(m)
    
    if d == "cal_prev":
        m -= 1
        if m < 1:
            m, y = 12, y - 1
    else:
        m += 1
        if m > 12:
            m, y = 1, y + 1
    
    now = datetime.now()
    if y < now.year or (y == now.year and m < now.month):
        await cb.answer("❌ Нельзя в прошлое")
        return
    
    try:
        await cb.message.edit_reply_markup(reply_markup=kb_calendar(y, m))
    except:
        pass
    await cb.answer()

@router.callback_query(F.data.startswith("cal_day:"), AddDeadlineStates.waiting_date)
async def cb_cal_day(cb: CallbackQuery, state: FSMContext):
    _, y, m, day = cb.data.split(":")
    sel = datetime(int(y), int(m), int(day))
    await state.update_data(date=sel.strftime("%Y-%m-%d"))
    data = await state.get_data()
    subj = data.get("subject", "?")
    
    await safe_edit(
        cb.message,
        f"📚 <b>{subj}</b>\n📅 {sel.strftime('%d.%m.%Y')}\n\n🕐 <b>Шаг 3/5: Выберите час</b>",
        kb_hours()
    )
    await state.set_state(AddDeadlineStates.waiting_hour)
    await cb.answer()

@router.callback_query(F.data.startswith("time_h:"), AddDeadlineStates.waiting_hour)
async def cb_hour(cb: CallbackQuery, state: FSMContext):
    h = int(cb.data.split(":")[1])
    await state.update_data(hour=h)
    data = await state.get_data()
    subj, ds = data.get("subject", "?"), data.get("date", "")
    
    await safe_edit(
        cb.message,
        f"📚 <b>{subj}</b>\n📅 {ds}\n🕐 {h:02d}\n\n⏱ <b>Шаг 4/5: Введите минуты</b> (00-59):",
        kb_cancel()
    )
    await state.set_state(AddDeadlineStates.waiting_minute)
    await cb.answer()

@router.message(AddDeadlineStates.waiting_minute)
async def msg_minute(msg: Message, state: FSMContext):
    try:
        m = int(msg.text.strip())
        if not (0 <= m <= 59):
            await msg.answer("❌ Минуты 00-59", reply_markup=kb_cancel())
            return
        
        await state.update_data(minute=m)
        data = await state.get_data()
        subj, ds, h = data.get("subject", "?"), data.get("date", ""), data.get("hour", 0)
        dt = datetime.strptime(f"{ds} {h:02d}:{m:02d}", "%Y-%m-%d %H:%M")
        
        ok, err = validate_time(dt)
        if not ok:
            await msg.answer(f"{err}\n\n⚠️ Выберите другое время:", reply_markup=kb_cancel())
            await state.set_state(AddDeadlineStates.waiting_hour)
            await msg.answer(
                f"📚 <b>{subj}</b>\n\n🕐 <b>Шаг 3/5: Выберите час</b>",
                reply_markup=kb_hours(),
                parse_mode="HTML"
            )
            return
        
        await msg.answer(
            f"📚 <b>{subj}</b>\n📅 {ds} {h:02d}:{m:02d}\n\n💬 <b>Шаг 5/5: Комментарий</b>\n\nВведите или пропустите (макс. {MAX_NOTE_LENGTH}):",
            reply_markup=kb_skip_note(),
            parse_mode="HTML"
        )
        await state.set_state(AddDeadlineStates.waiting_note)
    
    except ValueError:
        await msg.answer("❌ Введите число 00-59", reply_markup=kb_cancel())

@router.callback_query(F.data == "skip_note", AddDeadlineStates.waiting_note)
async def cb_skip_note(cb: CallbackQuery, state: FSMContext):
    await _save_deadline_cb(cb, state, None)

@router.message(AddDeadlineStates.waiting_note)
async def msg_note(msg: Message, state: FSMContext):
    note = msg.text.strip()
    if note.lower() in NO_WORDS:
        note = None
    elif len(note) > MAX_NOTE_LENGTH:
        await msg.answer(f"❌ Макс. {MAX_NOTE_LENGTH} символов", reply_markup=kb_skip_note())
        return
    await _save_deadline_msg(msg, state, note)

async def _save_deadline_cb(cb: CallbackQuery, state: FSMContext, note):
    data = await state.get_data()
    subj, ds, h, mi = data.get("subject"), data.get("date"), data.get("hour"), data.get("minute")
    dts = f"{ds} {h:02d}:{mi:02d}"
    
    try:
        conn = db()
        c = conn.cursor()
        c.execute(
            "INSERT INTO deadlines (user_id, subject, date, note, created_at, status) VALUES (?, ?, ?, ?, ?, ?)",
            (cb.from_user.id, subj, dts, note, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "pending")
        )
        conn.commit()
        conn.close()
        
        display_dt = datetime.strptime(dts, "%Y-%m-%d %H:%M").strftime("%d.%m.%Y в %H:%M")
        text = f"✅ <b>Дедлайн добавлен!</b>\n\n📚 <b>{subj}</b>\n📅 {display_dt}\n"
        if note:
            text += f"💬 {note}\n"
        text += "\n🔔 Напоминания: за 1 день, 2 часа, 15 минут"
        
        await state.clear()
        await safe_edit(cb.message, text, kb_main())
        await cb.answer("✅ Добавлено!")
    except Exception as e:
        logger.error(e)
        await cb.answer("❌ Ошибка сохранения", show_alert=True)

async def _save_deadline_msg(msg: Message, state: FSMContext, note):
    data = await state.get_data()
    subj, ds, h, mi = data.get("subject"), data.get("date"), data.get("hour"), data.get("minute")
    dts = f"{ds} {h:02d}:{mi:02d}"
    
    try:
        conn = db()
        c = conn.cursor()
        c.execute(
            "INSERT INTO deadlines (user_id, subject, date, note, created_at, status) VALUES (?, ?, ?, ?, ?, ?)",
            (msg.from_user.id, subj, dts, note, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "pending")
        )
        conn.commit()
        conn.close()
        
        display_dt = datetime.strptime(dts, "%Y-%m-%d %H:%M").strftime("%d.%m.%Y в %H:%M")
        text = f"✅ <b>Дедлайн добавлен!</b>\n\n📚 <b>{subj}</b>\n📅 {display_dt}\n"
        if note:
            text += f"💬 {note}\n"
        text += "\n🔔 Напоминания: за 1 день, 2 часа, 15 минут"
        
        await state.clear()
        await msg.answer(text, reply_markup=kb_main(), parse_mode="HTML")
    except Exception as e:
        logger.error(e)
        await msg.answer("❌ Ошибка сохранения", reply_markup=kb_main())

# ──────────────────────────────────────────────────────────────────────────────
# VIEW DEADLINES
# ──────────────────────────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("my_deadlines:"))
async def cb_my_deadlines(cb: CallbackQuery):
    page = int(cb.data.split(":")[1])
    
    conn = db()
    c = conn.cursor()
    c.execute(
        "SELECT id, subject, date, note, status FROM deadlines WHERE user_id=? ORDER BY datetime(date)",
        (cb.from_user.id,)
    )
    rows = c.fetchall()
    conn.close()
    
    if not rows:
        await safe_edit(
            cb.message,
            "📋 <b>Мои дедлайны</b>\n\nПока нет дедлайнов.\nНажмите «➕ Добавить»!",
            kb_main()
        )
        await cb.answer()
        return
    
    total = len(rows)
    total_pages = (total + ITEMS_PER_PAGE - 1) // ITEMS_PER_PAGE
    start = (page - 1) * ITEMS_PER_PAGE
    end = start + ITEMS_PER_PAGE
    
    kbd = []
    for r in rows[start:end]:
        btxt = fmt_deadline(r).replace("<b>", "").replace("</b>", "")[:50]
        kbd.append([
            InlineKeyboardButton(text=btxt, callback_data=f"view_deadline:{r['id']}:{page}")
        ])
    
    nav = []
    if page > 1:
        nav.append(InlineKeyboardButton(text="◀️", callback_data=f"my_deadlines:{page-1}"))
    if page < total_pages:
        nav.append(InlineKeyboardButton(text="▶️", callback_data=f"my_deadlines:{page+1}"))
    if nav:
        kbd.append(nav)
    
    kbd.append([InlineKeyboardButton(text="🏠 Главное меню", callback_data="main_menu")])
    
    await safe_edit(
        cb.message,
        f"📋 <b>Мои дедлайны</b> (стр. {page}/{total_pages})\n\nВсего: {total}",
        InlineKeyboardMarkup(inline_keyboard=kbd)
    )
    await cb.answer()

@router.callback_query(F.data.startswith("view_deadline:"))
async def cb_view_deadline(cb: CallbackQuery):
    parts = cb.data.split(":")
    did, page = int(parts[1]), int(parts[2])
    
    conn = db()
    c = conn.cursor()
    c.execute("SELECT id, subject, date, note, status FROM deadlines WHERE id=?", (did,))
    row = c.fetchone()
    conn.close()
    
    if not row:
        await cb.answer("❌ Не найден", show_alert=True)
        return
    
    await safe_edit(
        cb.message,
        f"📋 <b>Детали</b>\n\n{fmt_deadline(row)}",
        kb_deadline_actions(did, page)
    )
    await cb.answer()

@router.callback_query(F.data.startswith("delete_deadline:"))
async def cb_delete_confirm(cb: CallbackQuery):
    did = int(cb.data.split(":")[1])
    kbd = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Да, удалить", callback_data=f"delete_yes:{did}"),
            InlineKeyboardButton(text="❌ Отмена", callback_data=f"view_deadline:{did}:1")
        ]
    ])
    await safe_edit(cb.message, "🗑 <b>Удалить дедлайн?</b>", kbd)
    await cb.answer()

@router.callback_query(F.data.startswith("delete_yes:"))
async def cb_delete_exec(cb: CallbackQuery):
    did = int(cb.data.split(":")[1])
    
    try:
        conn = db()
        c = conn.cursor()
        c.execute("DELETE FROM deadlines WHERE id=?", (did,))
        c.execute("DELETE FROM sent_reminders WHERE deadline_id=?", (did,))
        conn.commit()
        conn.close()
        
        await safe_edit(cb.message, "✅ Удалён!", kb_main())
        await cb.answer("✅")
    except Exception as e:
        logger.error(e)
        await cb.answer("❌ Ошибка", show_alert=True)

@router.callback_query(F.data.startswith("edit_datetime:"))
async def cb_edit_datetime(cb: CallbackQuery, state: FSMContext):
    parts = cb.data.split(":")
    did, page = int(parts[1]), int(parts[2])
    
    conn = db()
    c = conn.cursor()
    c.execute("SELECT subject, date FROM deadlines WHERE id=?", (did,))
    row = c.fetchone()
    conn.close()
    
    if not row:
        await cb.answer("❌ Не найден", show_alert=True)
        return
    
    await state.update_data(edit_deadline_id=did, edit_page=page, edit_subject=row["subject"])
    now = datetime.now()
    
    await safe_edit(
        cb.message,
        f"📚 <b>{row['subject']}</b>\n\n📅 <b>Выберите новую дату:</b>",
        kb_calendar(now.year, now.month)
    )
    await state.set_state(EditDeadlineStates.waiting_edit_date)
    await cb.answer()

@router.callback_query(
    F.data.startswith("cal_prev:") | F.data.startswith("cal_next:"),
    EditDeadlineStates.waiting_edit_date
)
async def cb_edit_cal_nav(cb: CallbackQuery):
    d, y, m = cb.data.split(":")
    y, m = int(y), int(m)
    
    if d == "cal_prev":
        m -= 1
        if m < 1:
            m, y = 12, y - 1
    else:
        m += 1
        if m > 12:
            m, y = 1, y + 1
    
    now = datetime.now()
    if y < now.year or (y == now.year and m < now.month):
        await cb.answer("❌ Нельзя в прошлое")
        return
    
    try:
        await cb.message.edit_reply_markup(reply_markup=kb_calendar(y, m))
    except:
        pass
    await cb.answer()

@router.callback_query(F.data.startswith("cal_day:"), EditDeadlineStates.waiting_edit_date)
async def cb_edit_cal_day(cb: CallbackQuery, state: FSMContext):
    _, y, m, day = cb.data.split(":")
    sel = datetime(int(y), int(m), int(day))
    await state.update_data(edit_date=sel.strftime("%Y-%m-%d"))
    data = await state.get_data()
    subj = data.get("edit_subject", "?")
    
    await safe_edit(
        cb.message,
        f"📚 <b>{subj}</b>\n📅 {sel.strftime('%d.%m.%Y')}\n\n🕐 <b>Выберите час:</b>",
        kb_hours()
    )
    await state.set_state(EditDeadlineStates.waiting_edit_hour)
    await cb.answer()

@router.callback_query(F.data.startswith("time_h:"), EditDeadlineStates.waiting_edit_hour)
async def cb_edit_hour(cb: CallbackQuery, state: FSMContext):
    h = int(cb.data.split(":")[1])
    await state.update_data(edit_hour=h)
    data = await state.get_data()
    subj, ds = data.get("edit_subject", "?"), data.get("edit_date", "")
    
    await safe_edit(
        cb.message,
        f"📚 <b>{subj}</b>\n📅 {ds}\n🕐 {h:02d}\n\n⏱ <b>Введите минуты</b> (00-59):",
        kb_cancel()
    )
    await state.set_state(EditDeadlineStates.waiting_edit_minute)
    await cb.answer()

@router.message(EditDeadlineStates.waiting_edit_minute)
async def msg_edit_minute(msg: Message, state: FSMContext):
    try:
        m = int(msg.text.strip())
        if not (0 <= m <= 59):
            await msg.answer("❌ Минуты 00-59", reply_markup=kb_cancel())
            return
        
        data = await state.get_data()
        did = data.get("edit_deadline_id")
        page = data.get("edit_page", 1)
        subj = data.get("edit_subject", "?")
        ds = data.get("edit_date", "")
        h = data.get("edit_hour", 0)
        
        dts = f"{ds} {h:02d}:{m:02d}"
        dt = datetime.strptime(dts, "%Y-%m-%d %H:%M")
        
        ok, err = validate_time(dt)
        if not ok:
            await msg.answer(f"{err}\n\n⚠️ Выберите другое время:", reply_markup=kb_cancel())
            await state.set_state(EditDeadlineStates.waiting_edit_hour)
            await msg.answer(
                f"📚 <b>{subj}</b>\n\n🕐 <b>Выберите час:</b>",
                reply_markup=kb_hours(),
                parse_mode="HTML"
            )
            return
        
        conn = db()
        c = conn.cursor()
        c.execute("UPDATE deadlines SET date=? WHERE id=?", (dts, did))
        conn.commit()
        conn.close()
        
        display_dt = dt.strftime("%d.%m.%Y в %H:%M")
        await msg.answer(
            f"✅ <b>Дата изменена!</b>\n\n📚 <b>{subj}</b>\n📅 <b>{display_dt}</b>",
            reply_markup=kb_main(),
            parse_mode="HTML"
        )
        await state.clear()
    
    except ValueError:
        await msg.answer("❌ Введите число 00-59", reply_markup=kb_cancel())
    except Exception as e:
        logger.error(e)
        await msg.answer("❌ Ошибка сохранения", reply_markup=kb_main())
        await state.clear()

@router.callback_query(F.data.startswith("edit_note:"))
async def cb_edit_note(cb: CallbackQuery, state: FSMContext):
    parts = cb.data.split(":")
    did, page = int(parts[1]), int(parts[2])
    
    conn = db()
    c = conn.cursor()
    c.execute("SELECT subject, note FROM deadlines WHERE id=?", (did,))
    row = c.fetchone()
    conn.close()
    
    if not row:
        await cb.answer("❌ Не найден", show_alert=True)
        return
    
    await state.update_data(edit_deadline_id=did, edit_page=page, edit_subject=row["subject"])
    
    current = f"\n\n<b>Текущий:</b>\n{row['note']}" if row["note"] else ""
    
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except:
        pass
    
    await cb.message.answer(
        f"📚 <b>{row['subject']}</b>\n\n💬 <b>Введите новый комментарий</b>{current}\n\n"
        f"Или отправьте «нет» для удаления (макс. {MAX_NOTE_LENGTH}):",
        reply_markup=kb_cancel(),
        parse_mode="HTML"
    )
    await state.set_state(EditDeadlineStates.waiting_edit_note)
    await cb.answer()

@router.message(EditDeadlineStates.waiting_edit_note)
async def msg_edit_note(msg: Message, state: FSMContext):
    note = msg.text.strip()
    
    if note.lower() in NO_WORDS:
        note = None
    elif len(note) > MAX_NOTE_LENGTH:
        await msg.answer(f"❌ Макс. {MAX_NOTE_LENGTH} символов", reply_markup=kb_cancel())
        return
    
    data = await state.get_data()
    did = data.get("edit_deadline_id")
    subj = data.get("edit_subject", "?")
    
    try:
        conn = db()
        c = conn.cursor()
        c.execute("UPDATE deadlines SET note=? WHERE id=?", (note, did))
        conn.commit()
        conn.close()
        
        text = f"✅ <b>Комментарий изменён!</b>\n\n📚 <b>{subj}</b>\n"
        if note:
            text += f"💬 {note}"
        else:
            text += "💬 <i>Комментарий удалён</i>"
        
        await msg.answer(text, reply_markup=kb_main(), parse_mode="HTML")
        await state.clear()
    
    except Exception as e:
        logger.error(e)
        await msg.answer("❌ Ошибка сохранения", reply_markup=kb_main())
        await state.clear()

# ──────────────────────────────────────────────────────────────────────────────
# SUBJECTS
# ──────────────────────────────────────────────────────────────────────────────

@router.callback_query(F.data == "subjects_menu")
async def cb_subjects_menu(cb: CallbackQuery):
    conn = db()
    c = conn.cursor()
    c.execute("SELECT COUNT(*) as n FROM subjects WHERE user_id=?", (cb.from_user.id,))
    cnt = c.fetchone()["n"]
    conn.close()
    
    await safe_edit(
        cb.message,
        f"📚 <b>Управление предметами</b>\n\nВсего: {cnt}",
        kb_subjects_menu()
    )
    await cb.answer()

@router.callback_query(F.data == "subjects_list_view")
async def cb_subjects_list(cb: CallbackQuery):
    conn = db()
    c = conn.cursor()
    c.execute("SELECT name FROM subjects WHERE user_id=? ORDER BY name", (cb.from_user.id,))
    subjects = [r["name"] for r in c.fetchall()]
    conn.close()
    
    if not subjects:
        text = "📚 <b>Список предметов</b>\n\nУ вас пока нет предметов.\nСоздайте первый!"
    else:
        text = "📚 <b>Список предметов</b>\n\n"
        for s in subjects:
            text += f"• {s}\n"
    
    kbd = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить предмет", callback_data="subject_add")],
        [InlineKeyboardButton(text="🏠 Главное меню", callback_data="main_menu")]
    ])
    
    await safe_edit(cb.message, text, kbd)
    await cb.answer()

@router.callback_query(F.data == "subject_add")
async def cb_subject_add(cb: CallbackQuery, state: FSMContext):
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except:
        pass
    
    await cb.message.answer(
        f"📝 <b>Новый предмет</b>\n\nВведите название ({MIN_SUBJECT_LENGTH}-{MAX_SUBJECT_LENGTH} символов):",
        reply_markup=kb_cancel(),
        parse_mode="HTML"
    )
    await state.set_state(AddDeadlineStates.waiting_new_subject)
    await cb.answer()

# ──────────────────────────────────────────────────────────────────────────────
# AI ASSISTANT
# ──────────────────────────────────────────────────────────────────────────────

@router.callback_query(F.data == "ai_assistant")
async def cb_ai_assistant(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    uid = cb.from_user.id
    is_prem = await check_premium(uid)
    
    if not is_prem:
        kbd = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=f"💳 Купить Premium - {PREMIUM_PRICE}₸/мес (СКОРО)", callback_data="buy_premium")],
            [InlineKeyboardButton(text="🏠 Главное меню", callback_data="main_menu")]
        ])
        text = (
            f"🤖 <b>AI Помощник (СКОРО)</b>\n\n"
            f"Доступно только для <b>Premium</b>.\n\n"
            f"<b>Возможности:</b>\n"
            f"✍️ Добавление одной фразой\n"
            f"🎯 Приоритизация\n"
            f"💎 <b>{PREMIUM_PRICE}₸/месяц</b>\n"
            f"✅ Оплата через Kaspi Bank"
        )
        await safe_edit(cb.message, text, kbd)
        await cb.answer()
        return
    
    await safe_edit(
    cb.message,
    "🤖 <b>AI Помощник</b>\n\nВыберите действие:\n\n"
    "✍️ <b>Добавить дедлайн</b> — одной фразой\n"
    "🎯 <b>Приоритизация</b> — срочные задачи",
    kb_ai_menu()
    )
    await cb.answer()

@router.callback_query(F.data == "ai_add_deadline")
async def cb_ai_add(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    uid = cb.from_user.id
    allowed, used, limit = await check_ai_limit(uid)
    
    if not allowed:
        await cb.answer(f"⚠️ Лимит {limit}/день исчерпан", show_alert=True)
        return
    
    await safe_edit(
        cb.message,
        "✍️ <b>AI Добавление</b>\n\n"
        "Напишите одной фразой:\n\n"
        "• <i>«Математика завтра 12:30»</i>\n"
        "• <i>«Физика 15 марта 14:00»</i>\n\n"
        f"💡 Осталось: {limit - used} запросов",
        kb_cancel()
    )
    await state.set_state(AIAssistantStates.waiting_input)
    await cb.answer()

@router.message(AIAssistantStates.waiting_input)
async def msg_ai_input(msg: Message, state: FSMContext):
    uid = msg.from_user.id
    txt = msg.text.strip()
    
    if not txt:
        await msg.answer("❌ Пустое сообщение", reply_markup=kb_cancel())
        return
    
    proc = await msg.answer("🤖 Анализирую...")
    
    try:
        result = await ai_parse_deadline(txt, uid)
        await increment_ai_usage(uid)
        
        try:
            await proc.delete()
        except:
            pass
        
        if not result.get("success"):
            await msg.answer(f"{result.get('message')}\n\nПереформулируйте:", reply_markup=kb_cancel())
            return
        
        if result.get("need_subject_selection") or not result.get("subject"):
            conn = db()
            c = conn.cursor()
            c.execute("SELECT name FROM subjects WHERE user_id=? ORDER BY name LIMIT 10", (uid,))
            subs = [r["name"] for r in c.fetchall()]
            conn.close()
            
            if not subs:
                await msg.answer("❓ Предмет не указан. Введите название:", reply_markup=kb_cancel())
                await state.update_data(ai_deadline_data=result)
                return
            
            await state.update_data(ai_deadline_data=result)
            await msg.answer("❓ Выберите предмет:", reply_markup=kb_subjects_for_ai(result.get("suggested_subjects", subs)))
            return
        
        conf = result.get("confidence", 0.0)
        ds, ts = result.get("date", "?"), result.get("time", "?")
        
        try:
            fmt = datetime.strptime(f"{ds} {ts}", "%Y-%m-%d %H:%M").strftime("%d.%m.%Y в %H:%M")
        except:
            fmt = f"{ds} {ts}"
        
        text = f"✅ <b>Распознано!</b>\n\n📚 <b>{result['subject']}</b>\n📅 <b>{fmt}</b>\n"
        if result.get("note"):
            text += f"💬 {result['note']}\n"
        text += f"\n🎯 Уверенность: {int(conf*100)}%\n\n"
        text += "Всё верно?" if conf >= AI_CONFIDENCE_THRESHOLD else "⚠️ Проверьте данные"
        
        dl_data = {"subject": result["subject"], "date": ds, "time": ts, "note": result.get("note", "")}
        await state.update_data(ai_deadline_data=dl_data)
        
        kbd = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Создать", callback_data="ai_confirm_create")],
            [InlineKeyboardButton(text="✏️ Заново", callback_data="ai_add_deadline")],
            [InlineKeyboardButton(text="❌ Отмена", callback_data="ai_assistant")]
        ])
        
        await msg.answer(text, reply_markup=kbd, parse_mode="HTML")
        await state.set_state(AIAssistantStates.waiting_confirmation)
    
    except Exception as e:
        logger.error(e)
        try:
            await proc.delete()
        except:
            pass
        await msg.answer("❌ Ошибка. Попробуйте ещё раз.", reply_markup=kb_ai_menu())
        await state.clear()

@router.callback_query(F.data.startswith("ai_select_subject:"))
async def cb_ai_select_subj(cb: CallbackQuery, state: FSMContext):
    subj = cb.data.split(":", 1)[1]
    data = await state.get_data()
    dl = data.get("ai_deadline_data", {})
    dl["subject"] = subj
    await state.update_data(ai_deadline_data=dl)
    
    ds, ts = dl.get("date", "?"), dl.get("time", "?")
    try:
        fmt = datetime.strptime(f"{ds} {ts}", "%Y-%m-%d %H:%M").strftime("%d.%m.%Y в %H:%M")
    except:
        fmt = f"{ds} {ts}"
    
    text = f"✅ <b>Готово!</b>\n\n📚 <b>{subj}</b>\n📅 <b>{fmt}</b>\n"
    if dl.get("note"):
        text += f"💬 {dl['note']}\n"
    text += "\nВсё верно?"
    
    kbd = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Создать", callback_data="ai_confirm_create")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="ai_assistant")]
    ])
    
    await safe_edit(cb.message, text, kbd)
    await state.set_state(AIAssistantStates.waiting_confirmation)
    await cb.answer()

@router.callback_query(F.data == "ai_confirm_create")
async def cb_ai_confirm(cb: CallbackQuery, state: FSMContext):
    uid = cb.from_user.id
    data = await state.get_data()
    dl = data.get("ai_deadline_data", {})
    
    if not dl or not dl.get("subject") or not dl.get("date"):
        await cb.answer("❌ Ошибка данных", show_alert=True)
        await state.clear()
        return
    
    try:
        subj, ds, ts = dl["subject"], dl["date"], dl.get("time", "12:00")
        note = dl.get("note") or None
        dts = f"{ds} {ts}"
        
        conn = db()
        c = conn.cursor()
        
        # Ensure subject exists
        try:
            c.execute(
                "INSERT INTO subjects (user_id, name, created_at) VALUES (?, ?, ?)",
                (uid, subj, datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
            )
        except sqlite3.IntegrityError:
            pass
        
        c.execute(
            "INSERT INTO deadlines (user_id, subject, date, note, created_at, status) VALUES (?, ?, ?, ?, ?, ?)",
            (uid, subj, dts, note, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "pending")
        )
        conn.commit()
        conn.close()
        
        fmt = datetime.strptime(dts, "%Y-%m-%d %H:%M").strftime("%d.%m.%Y в %H:%M")
        text = f"✅ <b>Дедлайн создан через AI!</b>\n\n📚 <b>{subj}</b>\n📅 <b>{fmt}</b>\n"
        if note:
            text += f"💬 {note}\n"
        text += "\n🔔 Напоминания: за 1 день, 2 часа, 15 минут"
        
        kbd = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="➕ Ещё", callback_data="ai_add_deadline")],
            [InlineKeyboardButton(text="📋 Мои", callback_data="my_deadlines:1")],
            [InlineKeyboardButton(text="🤖 AI", callback_data="ai_assistant")]
        ])
        
        await safe_edit(cb.message, text, kbd)
        await state.clear()
        await cb.answer("✅ Создан!")
    
    except Exception as e:
        logger.error(e)
        await cb.answer("❌ Ошибка создания", show_alert=True)
        await state.clear()

@router.callback_query(F.data == "ai_prioritize")
async def cb_ai_prioritize(cb: CallbackQuery):
    uid = cb.from_user.id
    allowed, used, limit = await check_ai_limit(uid)
    
    if not allowed:
        await cb.answer(f"⚠️ Лимит {limit}/день", show_alert=True)
        return
    
    await cb.answer("🤖 Анализирую...")
    proc = await cb.message.answer("⏳ Анализ...")
    
    try:
        result = await ai_prioritize(uid)
        await increment_ai_usage(uid)
        
        try:
            await proc.delete()
        except:
            pass
        
        await cb.message.answer(result, reply_markup=kb_ai_menu(), parse_mode="HTML")
    
    except Exception as e:
        logger.error(e)
        try:
            await proc.delete()
        except:
            pass
        await cb.message.answer("❌ Ошибка анализа", reply_markup=kb_ai_menu())

@router.callback_query(F.data == "ai_help")
async def cb_ai_help(cb: CallbackQuery):
    text = (
        "ℹ️ <b>Справка AI</b>\n\n"
        "<b>✍️ Добавление:</b>\n"
        "Пишите естественно:\n"
        "• «Математика завтра 12:00»\n"
        "• «Физика 15 марта 14:30»\n\n"
        "<b>🎯 Приоритизация:</b>\n"
        "Анализ срочных задач"
    )
    await safe_edit(cb.message, text, kb_ai_menu())
    await cb.answer()

# ──────────────────────────────────────────────────────────────────────────────
# PAYMENT
# ──────────────────────────────────────────────────────────────────────────────

@router.callback_query(F.data == "buy_premium")
async def cb_buy_premium(cb: CallbackQuery):
    text = (
        f"💳 <b>Premium подписка</b>\n\n"
        f"📌 <b>Инструкция:</b>\n\n"
        f"1️⃣ Откройте <b>Kaspi Bank</b>\n"
        f"2️⃣ Переводы → По номеру телефона\n"
        f"3️⃣ Номер: <code>{KASPI_PHONE}</code>\n"
        f"4️⃣ Сумма: <b>{PREMIUM_PRICE} ₸</b>\n"
        f"5️⃣ Комментарий: <code></code>\n"
        f"6️⃣ Сделайте скриншот\n\n"
        f"После оплаты нажмите кнопку ниже"
    )
    await safe_edit(cb.message, text, kb_payment())
    await cb.answer()

@router.callback_query(F.data == "payment_sent")
async def cb_payment_sent(cb: CallbackQuery, state: FSMContext):
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except:
        pass
    
    await cb.message.answer(
        "📸 <b>Отправьте скриншот оплаты</b>\n\n"
        "Пришлите фото из Kaspi Bank.\n\n"
        "❗ Без скриншота Premium не активируется.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="❌ Отмена", callback_data="main_menu")]
        ]),
        parse_mode="HTML"
    )
    await state.set_state(AIAssistantStates.waiting_screenshot)
    await cb.answer()

@router.message(AIAssistantStates.waiting_screenshot, F.photo)
async def msg_payment_screenshot(msg: Message, state: FSMContext, bot: Bot):
    uid = msg.from_user.id
    username = msg.from_user.username or "нет"
    full_name = msg.from_user.full_name or "нет"
    photo_id = msg.photo[-1].file_id
    
    try:
        conn = db()
        c = conn.cursor()
        c.execute(
            "INSERT INTO pending_payments (user_id, username, full_name, created_at, screenshot_file_id) VALUES (?, ?, ?, ?, ?)",
            (uid, username, full_name, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), photo_id)
        )
        conn.commit()
        pay_id = c.lastrowid
        conn.close()
    except Exception as e:
        logger.error(f"Payment save: {e}")
        await msg.answer("❌ Ошибка сохранения", reply_markup=kb_main())
        await state.clear()
        return
    
    await msg.answer(
        "✅ <b>Скриншот получен!</b>\n\n"
        "Заявка на проверке. Обычно до <b>30 минут</b>.\n\n"
        "Спасибо! 🙏",
        reply_markup=kb_main(),
        parse_mode="HTML"
    )
    
    # Notify admin
    try:
        admin_txt = (
            f"💰 <b>Новая заявка</b>\n\n"
            f"👤 {full_name}\n"
            f"📱 @{username}\n"
            f"🆔 <code>{uid}</code>\n"
            f"📋 #{pay_id}\n"
            f"🕐 {datetime.now().strftime('%d.%m.%Y %H:%M')}\n"
            f"💰 {PREMIUM_PRICE}₸ / {PREMIUM_DAYS} дней"
        )
        await bot.send_photo(
            ADMIN_ID,
            photo=photo_id,
            caption=admin_txt,
            reply_markup=kb_admin_verify(uid, pay_id),
            parse_mode="HTML"
        )
    except Exception as e:
        logger.error(f"Admin notify: {e}")
    
    await state.clear()

@router.message(AIAssistantStates.waiting_screenshot)
async def msg_payment_not_photo(msg: Message):
    await msg.answer(
        "❌ <b>Нужен скриншот</b>\n\nПришлите фото.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="❌ Отмена", callback_data="main_menu")]
        ]),
        parse_mode="HTML"
    )

# ──────────────────────────────────────────────────────────────────────────────
# ADMIN
# ──────────────────────────────────────────────────────────────────────────────

@router.message(Command("admin"))
async def cmd_admin(msg: Message):
    if msg.from_user.id != ADMIN_ID:
        return
    
    kbd = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 Статистика", callback_data="admin_stats")],
        [InlineKeyboardButton(text="⏳ Ожидающие", callback_data="admin_pending")]
    ])
    await msg.answer("👨‍💼 <b>Админ-панель</b>", reply_markup=kbd, parse_mode="HTML")

@router.callback_query(F.data == "admin_stats")
async def cb_admin_stats(cb: CallbackQuery):
    if cb.from_user.id != ADMIN_ID:
        await cb.answer("⛔ Нет доступа", show_alert=True)
        return
    
    try:
        conn = db()
        c = conn.cursor()
        
        c.execute("SELECT COUNT(DISTINCT user_id) as total FROM deadlines")
        total_users = c.fetchone()["total"]
        
        c.execute("SELECT COUNT(*) as n FROM premium_subscriptions WHERE is_premium=1")
        premium_users = c.fetchone()["n"]
        
        c.execute("SELECT COUNT(*) as n FROM deadlines")
        total_deadlines = c.fetchone()["n"]
        
        c.execute("SELECT COUNT(*) as n FROM pending_payments WHERE status='approved'")
        approved = c.fetchone()["n"]
        
        conn.close()
        
        text = (
            f"📊 <b>Статистика</b>\n\n"
            f"👥 Всего пользователей: <b>{total_users}</b>\n"
            f"💎 Premium: <b>{premium_users}</b>\n"
            f"📋 Всего дедлайнов: <b>{total_deadlines}</b>\n\n"
            f"💰 Выручка: <b>{approved * PREMIUM_PRICE}₸</b>"
        )
        
        await cb.message.answer(text, parse_mode="HTML")
        await cb.answer()
    
    except Exception as e:
        logger.error(e)
        await cb.answer("❌ Ошибка", show_alert=True)

@router.callback_query(F.data == "admin_pending")
async def cb_admin_pending(cb: CallbackQuery):
    if cb.from_user.id != ADMIN_ID:
        await cb.answer("⛔ Нет доступа", show_alert=True)
        return
    
    try:
        conn = db()
        c = conn.cursor()
        c.execute(
            "SELECT id, user_id, full_name, created_at FROM pending_payments WHERE status='pending' ORDER BY created_at DESC LIMIT 20"
        )
        rows = c.fetchall()
        conn.close()
        
        if not rows:
            await cb.message.answer("⏳ <b>Ожидающие</b>\n\nНет заявок.", parse_mode="HTML")
            await cb.answer()
            return
        
        text = "⏳ <b>Ожидающие</b>\n\n"
        for r in rows:
            text += f"#{r['id']} | {r['full_name']}\n🆔 <code>{r['user_id']}</code>\n\n"
        
        await cb.message.answer(text, parse_mode="HTML")
        await cb.answer()
    
    except Exception as e:
        logger.error(e)
        await cb.answer("❌ Ошибка", show_alert=True)

@router.callback_query(F.data.startswith("admin_approve:"))
async def cb_admin_approve(cb: CallbackQuery, bot: Bot):
    if cb.from_user.id != ADMIN_ID:
        await cb.answer("⛔ Нет доступа", show_alert=True)
        return
    
    parts = cb.data.split(":")
    user_id, pay_id = int(parts[1]), int(parts[2])
    
    try:
        exp = (datetime.now() + timedelta(days=PREMIUM_DAYS)).strftime("%Y-%m-%d %H:%M:%S")
        now_s = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
        conn = db()
        c = conn.cursor()
        
        c.execute("SELECT 1 FROM premium_subscriptions WHERE user_id=?", (user_id,))
        if c.fetchone():
            c.execute(
                "UPDATE premium_subscriptions SET is_premium=1, subscribed_at=?, expires_at=? WHERE user_id=?",
                (now_s, exp, user_id)
            )
        else:
            c.execute(
                "INSERT INTO premium_subscriptions (user_id, is_premium, subscribed_at, expires_at) VALUES (?, 1, ?, ?)",
                (user_id, now_s, exp)
            )
        
        c.execute("UPDATE pending_payments SET status='approved' WHERE id=?", (pay_id,))
        conn.commit()
        conn.close()
        
        exp_date = (datetime.now() + timedelta(days=PREMIUM_DAYS)).strftime("%d.%m.%Y")
        
        try:
            await bot.send_message(
                user_id,
                f"🎉 <b>Premium активирован!</b>\n\n✅ Оплата подтверждена.\n📅 До: <b>{exp_date}</b>\n\nДобро пожаловать! 🤖",
                reply_markup=kb_main(),
                parse_mode="HTML"
            )
        except Exception as e:
            logger.error(f"User notify: {e}")
        
        try:
            await cb.message.edit_caption(
                caption=(cb.message.caption or "") + f"\n\n✅ <b>ОДОБРЕНО</b> — до {exp_date}",
                reply_markup=None,
                parse_mode="HTML"
            )
        except:
            pass
        
        await cb.answer("✅ Premium активирован!", show_alert=True)
    
    except Exception as e:
        logger.error(f"Approve error: {e}")
        await cb.answer("❌ Ошибка", show_alert=True)

@router.callback_query(F.data.startswith("admin_reject_start:"))
async def cb_admin_reject_start(cb: CallbackQuery, state: FSMContext):
    if cb.from_user.id != ADMIN_ID:
        await cb.answer("⛔ Нет доступа", show_alert=True)
        return
    
    parts = cb.data.split(":")
    user_id, pay_id = int(parts[1]), int(parts[2])
    await state.update_data(reject_user_id=user_id, reject_payment_id=pay_id)
    
    await cb.message.answer("✍️ <b>Причина отклонения</b>\n\nНапишите причину:", parse_mode="HTML")
    await state.set_state(AdminStates.waiting_reject_reason)
    await cb.answer()

@router.message(AdminStates.waiting_reject_reason)
async def msg_admin_reject_reason(msg: Message, state: FSMContext, bot: Bot):
    if msg.from_user.id != ADMIN_ID:
        return
    
    reason = msg.text.strip()
    data = await state.get_data()
    user_id, pay_id = data.get("reject_user_id"), data.get("reject_payment_id")
    
    try:
        conn = db()
        c = conn.cursor()
        c.execute("UPDATE pending_payments SET status='rejected', rejected_reason=? WHERE id=?", (reason, pay_id))
        conn.commit()
        conn.close()
        
        try:
            await bot.send_message(
                user_id,
                f"❌ <b>Оплата не подтверждена</b>\n\n<b>Причина:</b>\n{reason}\n\nСвяжитесь с администратором.",
                reply_markup=kb_rejected_payment(),
                parse_mode="HTML"
            )
        except Exception as e:
            logger.error(f"User notify: {e}")
        
        await msg.answer(f"✅ Заявка #{pay_id} отклонена")
        await state.clear()
    
    except Exception as e:
        logger.error(f"Reject error: {e}")
        await msg.answer(f"❌ Ошибка: {e}")

@router.message(Command("grant"))
async def cmd_grant(msg: Message):
    if msg.from_user.id != ADMIN_ID:
        return
    
    parts = msg.text.split()
    if len(parts) < 2:
        await msg.answer("Использование: /grant <user_id> [дней]\nПример: /grant 123456789 30")
        return
    
    try:
        target_id = int(parts[1])
        days = int(parts[2]) if len(parts) >= 3 else PREMIUM_DAYS
        
        exp = (datetime.now() + timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
        now_s = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
        conn = db()
        c = conn.cursor()
        
        c.execute("SELECT 1 FROM premium_subscriptions WHERE user_id=?", (target_id,))
        if c.fetchone():
            c.execute(
                "UPDATE premium_subscriptions SET is_premium=1, subscribed_at=?, expires_at=? WHERE user_id=?",
                (now_s, exp, target_id)
            )
        else:
            c.execute(
                "INSERT INTO premium_subscriptions (user_id, is_premium, subscribed_at, expires_at) VALUES (?, 1, ?, ?)",
                (target_id, now_s, exp)
            )
        
        conn.commit()
        conn.close()
        
        exp_date = (datetime.now() + timedelta(days=days)).strftime("%d.%m.%Y")
        await msg.answer(f"✅ Premium выдан {target_id} до {exp_date}")
        
        try:
            await msg.bot.send_message(
                target_id,
                f"🎉 <b>Premium активирован!</b>\n\n📅 До: <b>{exp_date}</b>",
                reply_markup=kb_main(),
                parse_mode="HTML"
            )
        except:
            pass
    
    except (ValueError, IndexError):
        await msg.answer("❌ Неверный формат. Пример: /grant 123456789 30")
    except Exception as e:
        logger.error(e)
        await msg.answer(f"❌ Ошибка: {e}")

# ──────────────────────────────────────────────────────────────────────────────
# REMINDERS
# ──────────────────────────────────────────────────────────────────────────────

async def check_reminders(bot: Bot):
    tz = pytz.timezone(TIMEZONE)
    now = datetime.now(tz)
    now_str = now.strftime("%Y-%m-%d %H:%M")
    
    try:
        conn = db()
        c = conn.cursor()
        
        # Mark overdue
        c.execute(
            "UPDATE deadlines SET status='overdue' WHERE datetime(date)<datetime(?) AND status!='overdue'",
            (now_str,)
        )
        conn.commit()
        
        # Get pending reminders
        c.execute(
            "SELECT id, user_id, subject, date, note, status FROM deadlines WHERE status IN ('pending', 'reminded') AND datetime(date)>=datetime(?)",
            (now_str,)
        )
        rows = c.fetchall()
        
        for row in rows:
            did, uid = row["id"], row["user_id"]
            
            try:
                dl_time = tz.localize(datetime.strptime(row["date"], "%Y-%m-%d %H:%M"))
            except:
                continue
            
            if (dl_time - now).total_seconds() < 0:
                continue
            
            for rtype, td, rmsg in [
                ("1d", timedelta(days=1), "⏰ <b>Напоминание:</b> дедлайн завтра!"),
                ("2h", timedelta(hours=2), "⏰ <b>Напоминание:</b> дедлайн через 2 часа!"),
                ("15m", timedelta(minutes=15), "🔥 <b>СРОЧНО!</b> Дедлайн через 15 минут!")
            ]:
                remind_at = dl_time - td
                ws, we = remind_at - timedelta(seconds=30), remind_at + timedelta(seconds=30)
                
                if not (ws <= now <= we):
                    continue
                
                c.execute(
                    "SELECT 1 FROM sent_reminders WHERE deadline_id=? AND reminder_type=?",
                    (did, rtype)
                )
                if c.fetchone():
                    continue
                
                ts = dl_time.strftime("%d.%m.%Y в %H:%M")
                text = f"{rmsg}\n\n📚 <b>{row['subject']}</b>\n📅 <b>{ts}</b>\n"
                if row["note"]:
                    text += f"💬 {row['note']}\n"
                if rtype == "15m":
                    text += "\n🔥 Последнее напоминание. Удачи! 💪"
                
                try:
                    await bot.send_message(uid, text, parse_mode="HTML")
                    c.execute(
                        "INSERT INTO sent_reminders (deadline_id, reminder_type, sent_at) VALUES (?, ?, ?)",
                        (did, rtype, now.strftime("%Y-%m-%d %H:%M:%S"))
                    )
                    
                    if rtype == "15m":
                        c.execute("DELETE FROM deadlines WHERE id=?", (did,))
                        c.execute("DELETE FROM sent_reminders WHERE deadline_id=?", (did,))
                    
                    conn.commit()
                
                except Exception as e:
                    logger.error(f"Send reminder error (user={uid}, deadline={did}): {e}")
        
        conn.close()
    
    except Exception as e:
        logger.error(f"Check reminders error: {e}")

async def reminder_loop(bot: Bot):
    logger.info("🔔 Система напоминаний запущена")
    while True:
        try:
            await check_reminders(bot)
        except Exception as e:
            logger.error(f"Reminder loop: {e}")
        await asyncio.sleep(60)

# ──────────────────────────────────────────────────────────────────────────────
# MAIN
# ──────────────────────────────────────────────────────────────────────────────

async def main():
    bot = Bot(token=TOKEN)
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    
    asyncio.create_task(reminder_loop(bot))
    
    logger.info("=" * 80)
    logger.info("🤖 АнтиДедлайн Бот - Запуск")
    logger.info(f"💰 Premium: {PREMIUM_PRICE}₸")
    logger.info(f"🤖 AI: {GROK_MODEL}")
    logger.info("=" * 80)
    
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    except KeyboardInterrupt:
        logger.info("⚠️ Остановка (Ctrl+C)")
    finally:
        await bot.session.close()
        logger.info("✅ Бот остановлен")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass