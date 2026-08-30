"""
phase.parser — агрегатор публичных вакансий из Telegram-групп
Telethon (UserBot) + Aiogram 3.x (Bot) + SQLite + DeepSeek API
"""
from __future__ import annotations
import asyncio, html, json, logging, os, re, sqlite3, time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from logging.handlers import TimedRotatingFileHandler
from typing import Optional

import aiohttp
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, Filter
from aiogram.types import (
    BufferedInputFile, CallbackQuery, InlineKeyboardButton,
    InlineKeyboardMarkup, Message, BotCommand, BotCommandScopeChat,
    InlineQuery, InlineQueryResultArticle, InputTextMessageContent,
)
from dotenv import load_dotenv
from telethon import TelegramClient, events
from telethon.extensions import html as tg_html
from telethon.errors import (
    PhoneCodeExpiredError, PhoneCodeInvalidError,
    PasswordHashInvalidError, SessionPasswordNeededError,
    UserAlreadyParticipantError, InviteHashExpiredError, InviteHashInvalidError,
    ChannelsTooMuchError, FloodWaitError,
)
from telethon.sessions import StringSession
from telethon.tl.functions.messages import ImportChatInviteRequest, CheckChatInviteRequest
from telethon.tl.functions.users import GetFullUserRequest
from telethon.tl.types import (
    MessageMediaDocument, MessageMediaPhoto, MessageMediaWebPage, ChatInviteAlready,
)

# ── Конфигурация ──────────────────────────────────────────────
load_dotenv()
BOT_TOKEN      = os.getenv("BOT_TOKEN", "")
API_ID         = int(os.getenv("API_ID", "0"))
API_HASH       = os.getenv("API_HASH", "")
STRING_SESSION = os.getenv("STRING_SESSION", "")
ADMIN_ID       = int(os.getenv("ADMIN_ID", "7605695437"))
DEEPSEEK_KEY   = os.getenv("DEEPSEEK_API_KEY", "")
DEEPSEEK_URL   = os.getenv("DEEPSEEK_URL", "https://api.deepseek.com/v1/chat/completions")
DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")
DB_PATH        = os.getenv("DATABASE_PATH", os.getenv("DB_PATH", "parser.db"))
SUPPORT_USERNAME = os.getenv("SUPPORT_USERNAME", "savelimontaj")
PAYMENT_PHONE    = os.getenv("PAYMENT_PHONE", "+79132696007")
PAYMENT_NAME     = os.getenv("PAYMENT_NAME", "Савелий Сергеевич С.")
PAYMENT_BANK     = os.getenv("PAYMENT_BANK", "Озон банк")
CRYPTO_WALLET    = os.getenv("CRYPTO_WALLET", "")            # адрес USDT-кошелька
CRYPTO_NETWORK   = os.getenv("CRYPTO_NETWORK", "TRC20")       # сеть (TRC20/BEP20/TON и т.п.)
CRYPTO_RATE_BUFFER = float(os.getenv("CRYPTO_RATE_BUFFER", "1.5"))  # % запаса на случай расхождения курса с BingX
MSK = timezone(timedelta(hours=3))  # Москва — фикс. UTC+3, без перехода на летнее/зимнее
FREE_DAYS        = int(os.getenv("FREE_DAYS", "3"))
REF_DAYS         = int(os.getenv("REF_DAYS", "5"))          # бонус купившему рефералу
REF_BONUS_DAYS   = int(os.getenv("REF_BONUS_DAYS", "15"))   # бонус пригласившему

# Цены: до первой оплаты (скидка) / после
PRICES = {
    "week":   {"label": "1 нед.",  "days": 7,   "sale": 169,  "full": 199},
    "month":  {"label": "1 мес.",  "days": 30,  "sale": 424,  "full": 499},
    "3month": {"label": "3 мес.",  "days": 90,  "sale": 1019, "full": 1119},
}

# Мягкие корни-слова (слишком общие сами по себе, ловят случайный чат) —
# засчитываются как совпадение по ключевым словам, ТОЛЬКО если в тексте
# рядом есть слово-триггер запроса (нужен/ищу/требуется и т.п.), неважно
# на каком расстоянии друг от друга. Так вакансия "нужен эдитор для канала"
# и "ищу того кто сделает мувик" ловятся, а болтовня "я с эдитами не связан" — нет.
# Это только НАЧАЛЬНЫЕ значения для первого запуска — дальше оба списка
# редактируются через админ-бота (🔑 Ключевые слова → Мягкие корни / Триггеры),
# как обычные ключевые слова.
SOFT_ROOT_SEED = ["эдит", "мувик"]
REQUEST_TRIGGER_SEED = [
    "нужен", "нужна", "нужны", "нужно",
    "ищу", "ищем",
    "требуется", "требуются",
    "кто сможет", "кто сделает", "кто может", "кто готов",
    "в команду", "на постоянку", "на проект",
]

KW_CATEGORY_LABELS = {
    "common": "Общий", "admin": "Мои",
    "soft_root": "Мягкие корни", "trigger": "Триггеры запроса",
}
def kw_cat_label(cat: str) -> str:
    return KW_CATEGORY_LABELS.get(cat, cat)

# ── Логирование ───────────────────────────────────────────────
def setup_logging() -> logging.Logger:
    logger = logging.getLogger("phase.parser")
    logger.setLevel(logging.DEBUG)
    fmt = logging.Formatter("%(asctime)s | %(levelname)-8s | %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    ch = logging.StreamHandler(); ch.setLevel(logging.INFO); ch.setFormatter(fmt); logger.addHandler(ch)
    fh = TimedRotatingFileHandler("parser.log", when="D", interval=2, backupCount=1, encoding="utf-8")
    fh.setLevel(logging.DEBUG); fh.setFormatter(fmt); logger.addHandler(fh)
    return logger
log = setup_logging()

async def notify_admin_error(bot: Optional["Bot"], context: str, err: Exception) -> None:
    """Дублирует критическую ошибку в админ-бот (в дополнение к логам в файл)."""
    if bot is None:
        return
    try:
        text = f"🐞 <b>Ошибка: {context}</b>\n\n<code>{str(err)[:800]}</code>"
        await bot.send_message(ADMIN_ID, text, parse_mode=ParseMode.HTML)
    except Exception as notify_err:
        log.error(f"notify_admin_error не смог отправить сообщение: {notify_err}")

# ── Dataclasses ───────────────────────────────────────────────
@dataclass
class Vacancy:
    chat_id: int; message_id: int; text: str; author_username: str
    author_id: int; source_title: str; message_link: str; timestamp: datetime
    html_text: str = ""

@dataclass
class DeepSeekResult:
    suitable: bool; reason: str; contact: str; contact_id: Optional[int] = None
# ═══════════════════════════════════════════════════════════════
# БАЗА ДАННЫХ
# ═══════════════════════════════════════════════════════════════
class Database:
    def __init__(self, path: str):
        self.path = path; self._conn: Optional[sqlite3.Connection] = None

    def connect(self) -> None:
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        log.info(f"SQLite: {self.path}")

    def _c(self) -> sqlite3.Connection:
        assert self._conn; return self._conn

    def init_tables(self) -> None:
        self._c().executescript("""
            CREATE TABLE IF NOT EXISTS sources (
                id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER UNIQUE NOT NULL,
                title TEXT NOT NULL, username TEXT, link TEXT, active INTEGER DEFAULT 1,
                added_at TEXT DEFAULT (datetime('now')));
            CREATE TABLE IF NOT EXISTS keywords (
                id INTEGER PRIMARY KEY AUTOINCREMENT, word TEXT NOT NULL,
                type TEXT NOT NULL DEFAULT 'common', UNIQUE(word,type));
            CREATE TABLE IF NOT EXISTS blacklist (
                id INTEGER PRIMARY KEY AUTOINCREMENT, word TEXT NOT NULL,
                type TEXT NOT NULL DEFAULT 'common', UNIQUE(word,type));
            CREATE TABLE IF NOT EXISTS clients (
                id INTEGER PRIMARY KEY AUTOINCREMENT, tg_id INTEGER UNIQUE NOT NULL,
                username TEXT, sub_until TEXT, first_payment INTEGER DEFAULT 0,
                ref_by INTEGER, search_active INTEGER DEFAULT 1,
                created_at TEXT DEFAULT (datetime('now')));
            CREATE TABLE IF NOT EXISTS client_stopwords (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                client_id INTEGER NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
                word TEXT NOT NULL, UNIQUE(client_id,word));
            CREATE TABLE IF NOT EXISTS templates (
                id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
                variant1 TEXT NOT NULL DEFAULT '', variant2 TEXT NOT NULL DEFAULT '',
                variant3 TEXT NOT NULL DEFAULT '', active INTEGER DEFAULT 1,
                created_at TEXT DEFAULT (datetime('now')));
            CREATE TABLE IF NOT EXISTS vacancies (
                id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL, text TEXT NOT NULL, author_username TEXT,
                author_id INTEGER, source_title TEXT, message_link TEXT, ts TEXT,
                suitable INTEGER, ds_reason TEXT, ds_contact TEXT, ds_contact_id INTEGER,
                created_at TEXT DEFAULT (datetime('now')), UNIQUE(chat_id,message_id));
            CREATE TABLE IF NOT EXISTS replies (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                vacancy_id INTEGER NOT NULL REFERENCES vacancies(id) ON DELETE CASCADE,
                template_id INTEGER NOT NULL, variant_num INTEGER NOT NULL,
                text_sent TEXT NOT NULL, tg_message_id INTEGER,
                sent_at TEXT DEFAULT (datetime('now')), deleted INTEGER DEFAULT 0);
            CREATE TABLE IF NOT EXISTS client_deliveries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                vacancy_id INTEGER NOT NULL REFERENCES vacancies(id) ON DELETE CASCADE,
                client_id INTEGER NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
                msg_id INTEGER, skipped INTEGER DEFAULT 0, skip_reason TEXT,
                sent_at TEXT DEFAULT (datetime('now')), UNIQUE(vacancy_id,client_id));
            CREATE TABLE IF NOT EXISTS payments (
                id INTEGER PRIMARY KEY AUTOINCREMENT, client_id INTEGER NOT NULL,
                tariff TEXT NOT NULL, amount INTEGER NOT NULL, days INTEGER NOT NULL,
                ticket TEXT UNIQUE NOT NULL, status TEXT DEFAULT 'pending',
                created_at TEXT DEFAULT (datetime('now')));
            CREATE TABLE IF NOT EXISTS ds_tokens (
                date TEXT PRIMARY KEY, tokens_in INTEGER DEFAULT 0, tokens_out INTEGER DEFAULT 0);
            CREATE TABLE IF NOT EXISTS logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL DEFAULT (datetime('now')),
                level TEXT NOT NULL, message TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS blocked_senders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sender_id INTEGER UNIQUE NOT NULL,
                sender_username TEXT,
                reason TEXT,
                blocked_until TEXT,
                added_at TEXT DEFAULT (datetime('now')));
            CREATE TABLE IF NOT EXISTS stats_daily (
                date TEXT PRIMARY KEY, vacancies_found INTEGER DEFAULT 0,
                vacancies_failed INTEGER DEFAULT 0, replies_sent INTEGER DEFAULT 0,
                ai_errors INTEGER DEFAULT 0, subs_bought INTEGER DEFAULT 0);
        """)
        self._c().commit()
        # Миграция: добавляем поля для крипто-оплаты в уже существующую БД
        for col_sql in (
            "ALTER TABLE payments ADD COLUMN method TEXT NOT NULL DEFAULT 'rub'",
            "ALTER TABLE payments ADD COLUMN crypto_amount REAL",
            "ALTER TABLE payments ADD COLUMN crypto_rate REAL",
        ):
            try: self._c().execute(col_sql)
            except sqlite3.OperationalError: pass  # колонка уже есть
        self._c().commit()
        # Миграция: колонка для отметки «вакансия удалена админом у всех клиентов»
        try: self._c().execute("ALTER TABLE vacancies ADD COLUMN deleted_by_admin INTEGER DEFAULT 0")
        except sqlite3.OperationalError: pass
        self._c().commit()
        # Миграция: HTML-версия текста вакансии (с сохранённым форматированием —
        # жирный, ссылки и т.п.), отдельно от plain-текста для ключевых слов/ИИ
        try: self._c().execute("ALTER TABLE vacancies ADD COLUMN html_text TEXT")
        except sqlite3.OperationalError: pass
        self._c().commit()
        # Миграция: причина отсева ДО DeepSeek (чёрный список и т.п.) — чтобы
        # можно было выгрузить и посмотреть, что именно отсеяло конкретное слово,
        # вместо того чтобы терять текст вакансии безвозвратно
        try: self._c().execute("ALTER TABLE vacancies ADD COLUMN block_reason TEXT")
        except sqlite3.OperationalError: pass
        self._c().commit()
        # Сидинг мягких корней/триггеров — только если их ещё нет (не перезатирает правки админа)
        if not self._c().execute("SELECT 1 FROM keywords WHERE type='soft_root' LIMIT 1").fetchone():
            for w in SOFT_ROOT_SEED:
                self._c().execute("INSERT OR IGNORE INTO keywords(word,type) VALUES(?,?)", (w, "soft_root"))
        if not self._c().execute("SELECT 1 FROM keywords WHERE type='trigger' LIMIT 1").fetchone():
            for w in REQUEST_TRIGGER_SEED:
                self._c().execute("INSERT OR IGNORE INTO keywords(word,type) VALUES(?,?)", (w, "trigger"))
        self._c().commit()
        self._c().execute("INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)", (
            "ds_system_prompt",
            "Ты фильтр вакансий для видеомонтажёра. "
            "Определи, является ли текст вакансией/заказом именно на монтаж видео для исполнителя-монтажёра. "
            "Не считай подходящими вакансии на другие роли (дизайнер, оператор, видеограф, сценарист, SMM, "
            "копирайтер, контент-менеджер и т.п.), даже если они упомянуты рядом — главной задачей вакансии "
            "должен быть именно монтаж видео. "
            "ВАЖНО: если текст написан от первого лица (монтирую, работал с..., сдаю в срок, ни разу не подвёл, "
            "моё портфолио и т.п.) — это самореклама фрилансера, а не вакансия, ВСЕГДА suitable=false, даже если "
            "где-то в тексте отдельно встречается слово 'нужен'/'вакансия'/'ищу' — авторы таких сообщений "
            "иногда специально вставляют такие слова (в том числе как текст скрытой ссылки), чтобы обмануть "
            "автоматические фильтры. Оценивай текст целиком по смыслу, а не по наличию отдельных слов. "
            "Верни JSON без markdown: {\"suitable\": bool, \"reason\": \"до 5 слов\"}"))
        self._c().execute("INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)", (
            "msg_reminder_24h",
            "❗<b>Подписка истекает</b>❗\nОсталось 24 часа\n\n"
            "Оплатите тариф сейчас чтобы не упустить новые вакансии!"))
        self._c().execute("INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)", (
            "broadcast_text",
            "👋 <b>phase.parser</b>\n\nНапоминаем: у нас каждый день новые вакансии на монтаж видео!"))
        self._c().execute("INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)", (
            "msg_welcome_search_started",
            "🔍 <b>Поиск вакансий уже запущен!</b>\n\n"
            "Ничего дополнительно нажимать не нужно — как только найдём подходящую "
            "вакансию на монтаж видео, она сразу придёт вам сюда, в этот чат.\n\n"
            "Обычно это занимает от нескольких минут до пары часов, в зависимости "
            "от того, как часто публикуют подходящие вакансии — просто ждите 🙂"))
        for k, v in (
            ("reminder_hours_before", "24"),
            ("broadcast_enabled", "0"),
            ("broadcast_time_msk", "10:00"),
            ("sender_cooldown_min", "30"),
        ):
            self._c().execute("INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)", (k, v))
        self._c().execute("INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)",
                          ("monitoring_active", "1"))
        self._c().execute("INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)",
                          ("ai_active", "1"))
        self._c().execute("INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)",
                          ("client_bot_active", "1"))
        self._c().commit()
        log.info("Таблицы инициализированы")

    # ── Настройки ─────────────────────────────────────────────
    def get_setting(self, key: str, default: str = "") -> str:
        row = self._c().execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def set_setting(self, key: str, value: str) -> None:
        self._c().execute("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)", (key, value))
        self._c().commit()

    def del_setting(self, key: str) -> None:
        self._c().execute("DELETE FROM settings WHERE key=?", (key,)); self._c().commit()

    def get_ds_rules(self) -> list[dict]:
        return [dict(r) for r in self._c().execute(
            "SELECT key,value FROM settings WHERE key LIKE 'ds_rule_%' ORDER BY key").fetchall()]

    # ── Источники ─────────────────────────────────────────────
    def add_source(self, chat_id: int, title: str, username: Optional[str], link: Optional[str]) -> bool:
        try:
            self._c().execute("INSERT OR IGNORE INTO sources(chat_id,title,username,link) VALUES(?,?,?,?)",
                              (chat_id, title, username, link)); self._c().commit(); return True
        except Exception as e:
            log.error(f"add_source: {e}"); return False

    def get_sources(self, active_only: bool = True) -> list[dict]:
        q = "SELECT * FROM sources" + (" WHERE active=1" if active_only else "") + " ORDER BY id"
        return [dict(r) for r in self._c().execute(q).fetchall()]

    def toggle_source(self, sid: int) -> None:
        self._c().execute("UPDATE sources SET active=1-active WHERE id=?", (sid,)); self._c().commit()

    def delete_source(self, sid: int) -> None:
        self._c().execute("DELETE FROM sources WHERE id=?", (sid,)); self._c().commit()

    # ── Ключевые слова / ЧС ───────────────────────────────────
    def get_keywords(self, ktype: str = "common") -> list[str]:
        return [r["word"] for r in self._c().execute(
            "SELECT word FROM keywords WHERE type=? ORDER BY word", (ktype,)).fetchall()]

    def add_keyword(self, word: str, ktype: str = "common") -> None:
        self._c().execute("INSERT OR IGNORE INTO keywords(word,type) VALUES(?,?)",
                          (word.lower().strip(), ktype)); self._c().commit()

    def delete_keyword(self, word: str, ktype: str = "common") -> None:
        self._c().execute("DELETE FROM keywords WHERE word=? AND type=?", (word, ktype))
        self._c().commit()

    def get_blacklist(self, btype: str = "common") -> list[str]:
        return [r["word"] for r in self._c().execute(
            "SELECT word FROM blacklist WHERE type=? ORDER BY word", (btype,)).fetchall()]

    def add_to_blacklist(self, word: str, btype: str = "common") -> None:
        self._c().execute("INSERT OR IGNORE INTO blacklist(word,type) VALUES(?,?)",
                          (word.lower().strip(), btype)); self._c().commit()

    def delete_from_blacklist(self, word: str, btype: str = "common") -> None:
        self._c().execute("DELETE FROM blacklist WHERE word=? AND type=?", (word, btype))
        self._c().commit()

    # ── Клиенты ───────────────────────────────────────────────
    def get_or_create_client(self, tg_id: int, username: Optional[str]) -> dict:
        row = self._c().execute("SELECT * FROM clients WHERE tg_id=?", (tg_id,)).fetchone()
        if row:
            row = dict(row)
            if username and row.get("username") != username:
                self._c().execute("UPDATE clients SET username=? WHERE tg_id=?", (username, tg_id))
                self._c().commit(); row["username"] = username
            return row
        self._c().execute("INSERT INTO clients(tg_id,username) VALUES(?,?)", (tg_id, username))
        self._c().commit()
        return dict(self._c().execute("SELECT * FROM clients WHERE tg_id=?", (tg_id,)).fetchone())

    def get_client_by_tg(self, tg_id: int) -> Optional[dict]:
        row = self._c().execute("SELECT * FROM clients WHERE tg_id=?", (tg_id,)).fetchone()
        return dict(row) if row else None

    def get_client_by_id(self, client_id: int) -> Optional[dict]:
        row = self._c().execute("SELECT * FROM clients WHERE id=?", (client_id,)).fetchone()
        return dict(row) if row else None

    def get_client_by_username(self, username: str) -> Optional[dict]:
        row = self._c().execute(
            "SELECT * FROM clients WHERE username=? COLLATE NOCASE", (username.lstrip("@"),)
        ).fetchone()
        return dict(row) if row else None

    def get_all_clients(self) -> list[dict]:
        return [dict(r) for r in self._c().execute("SELECT * FROM clients ORDER BY id DESC").fetchall()]

    def get_active_clients(self) -> list[dict]:
        now = datetime.now().isoformat()
        return [dict(r) for r in self._c().execute(
            "SELECT * FROM clients WHERE sub_until IS NOT NULL AND sub_until > ? AND search_active=1",
            (now,)).fetchall()]

    def set_subscription(self, client_id: int, until: datetime, is_payment: bool = False) -> None:
        self._c().execute("UPDATE clients SET sub_until=? WHERE id=?", (until.isoformat(), client_id))
        if is_payment:
            self._c().execute("UPDATE clients SET first_payment=1 WHERE id=?", (client_id,))
        self._c().commit()

    def extend_subscription(self, client_id: int, days: int) -> datetime:
        row = self._c().execute("SELECT sub_until FROM clients WHERE id=?", (client_id,)).fetchone()
        now = datetime.now()
        base = datetime.fromisoformat(row["sub_until"]) if row and row["sub_until"] and row["sub_until"] > now.isoformat() else now
        until = base + timedelta(days=days)
        self._c().execute("UPDATE clients SET sub_until=? WHERE id=?", (until.isoformat(), client_id))
        self._c().commit(); return until

    def is_subscribed(self, tg_id: int) -> bool:
        now = datetime.now().isoformat()
        return bool(self._c().execute(
            "SELECT id FROM clients WHERE tg_id=? AND sub_until IS NOT NULL AND sub_until > ?",
            (tg_id, now)).fetchone())

    def has_first_payment(self, tg_id: int) -> bool:
        row = self._c().execute("SELECT first_payment FROM clients WHERE tg_id=?", (tg_id,)).fetchone()
        return bool(row and row["first_payment"])

    def toggle_search(self, client_id: int) -> bool:
        row = self._c().execute("SELECT search_active FROM clients WHERE id=?", (client_id,)).fetchone()
        new_val = 0 if (row and row["search_active"]) else 1
        self._c().execute("UPDATE clients SET search_active=? WHERE id=?", (new_val, client_id))
        self._c().commit(); return bool(new_val)

    def get_client_stopwords(self, client_id: int) -> list[str]:
        return [r["word"] for r in self._c().execute(
            "SELECT word FROM client_stopwords WHERE client_id=?", (client_id,)).fetchall()]

    def add_client_stopwords(self, client_id: int, words: list[str]) -> None:
        for w in words:
            w = w.lower().strip()
            if w:
                self._c().execute("INSERT OR IGNORE INTO client_stopwords(client_id,word) VALUES(?,?)",
                                  (client_id, w))
        self._c().commit()

    def delete_client_stopwords(self, client_id: int, words: list[str]) -> None:
        for w in words:
            self._c().execute("DELETE FROM client_stopwords WHERE client_id=? AND word=?",
                              (client_id, w.lower().strip()))
        self._c().commit()

    # ── Вакансии ──────────────────────────────────────────────
    def save_vacancy(self, v: Vacancy) -> Optional[int]:
        try:
            cur = self._c().execute(
                "INSERT OR IGNORE INTO vacancies(chat_id,message_id,text,author_username,"
                "author_id,source_title,message_link,ts,html_text) VALUES(?,?,?,?,?,?,?,?,?)",
                (v.chat_id, v.message_id, v.text, v.author_username, v.author_id,
                 v.source_title, v.message_link, v.timestamp.isoformat(), v.html_text))
            self._c().commit()
            if cur.lastrowid: return cur.lastrowid
            row = self._c().execute("SELECT id FROM vacancies WHERE chat_id=? AND message_id=?",
                                    (v.chat_id, v.message_id)).fetchone()
            return row["id"] if row else None
        except Exception as e:
            log.error(f"save_vacancy: {e}"); return None

    def is_duplicate(self, text: str) -> bool:
        return bool(self._c().execute("SELECT id FROM vacancies WHERE text=?", (text,)).fetchone())

    def set_block_reason(self, vid: int, reason: str) -> None:
        """Помечает вакансию как отсеянную ДО DeepSeek (чёрным списком и т.п.),
        сохраняя причину — suitable остаётся NULL (не «не прошла ИИ», а вообще
        не дошла до ИИ), чтобы не путать со статистикой vacancies_failed."""
        self._c().execute("UPDATE vacancies SET block_reason=? WHERE id=?", (reason, vid))
        self._c().commit()

    def recent_vacancy_from_sender(self, author_id: int, minutes: int = 30) -> bool:
        """Есть ли уже вакансия от этого же отправителя за последние N минут —
        не даём одному и тому же автору спамить повторными постами вакансии.
        Не считаем записи, отсеянные чёрным списком (block_reason) — иначе
        одно неудачное сообщение блокировало бы кулдауном совсем другой,
        нормальный пост того же человека в течение получаса."""
        if not author_id: return False
        since = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).strftime("%Y-%m-%d %H:%M:%S")
        return bool(self._c().execute(
            "SELECT id FROM vacancies WHERE author_id=? AND created_at >= ? "
            "AND block_reason IS NULL LIMIT 1",
            (author_id, since)).fetchone())

    def update_vacancy_ds(self, vid: int, suitable: bool, reason: str,
                          contact: str, contact_id: Optional[int] = None) -> None:
        self._c().execute(
            "UPDATE vacancies SET suitable=?,ds_reason=?,ds_contact=?,ds_contact_id=? WHERE id=?",
            (1 if suitable else 0, reason, contact, contact_id, vid)); self._c().commit()

    def get_vacancy(self, vid: int) -> Optional[dict]:
        row = self._c().execute("SELECT * FROM vacancies WHERE id=?", (vid,)).fetchone()
        return dict(row) if row else None

    def get_recent_vacancies(self, limit: int = 8, offset: int = 0, suitable_only: bool = True) -> list[dict]:
        q = "SELECT * FROM vacancies"
        if suitable_only: q += " WHERE suitable=1"
        q += " ORDER BY id DESC LIMIT ? OFFSET ?"
        return [dict(r) for r in self._c().execute(q, (limit, offset)).fetchall()]

    def save_delivery(self, vacancy_id: int, client_id: int, msg_id: Optional[int],
                      skipped: bool = False, reason: str = "") -> None:
        self._c().execute(
            "INSERT OR IGNORE INTO client_deliveries(vacancy_id,client_id,msg_id,skipped,skip_reason)"
            " VALUES(?,?,?,?,?)", (vacancy_id, client_id, msg_id, 1 if skipped else 0, reason))
        self._c().commit()

    def get_deliveries(self, vacancy_id: int) -> list[dict]:
        return [dict(r) for r in self._c().execute(
            "SELECT d.*, c.tg_id FROM client_deliveries d JOIN clients c ON d.client_id=c.id "
            "WHERE d.vacancy_id=? AND d.msg_id IS NOT NULL", (vacancy_id,)).fetchall()]

    def mark_vacancy_deleted(self, vid: int) -> None:
        self._c().execute("UPDATE vacancies SET deleted_by_admin=1 WHERE id=?", (vid,))
        self._c().commit()

    # ── Платежи ───────────────────────────────────────────────
    def create_payment(self, client_id: int, tariff: str, amount: int, days: int,
                       method: str = "rub", crypto_amount: Optional[float] = None,
                       crypto_rate: Optional[float] = None) -> str:
        ticket = f"PAY-{int(time.time())}"
        self._c().execute(
            "INSERT INTO payments(client_id,tariff,amount,days,ticket,method,crypto_amount,crypto_rate) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (client_id, tariff, amount, days, ticket, method, crypto_amount, crypto_rate))
        self._c().commit(); return ticket

    def get_payment_by_ticket(self, ticket: str) -> Optional[dict]:
        row = self._c().execute("SELECT * FROM payments WHERE ticket=?", (ticket,)).fetchone()
        return dict(row) if row else None

    def confirm_payment(self, ticket: str) -> Optional[dict]:
        p = self.get_payment_by_ticket(ticket)
        if not p: return None
        if p.get("status") == "confirmed":
            return p
        self._c().execute("UPDATE payments SET status='confirmed' WHERE ticket=?", (ticket,))
        self._c().commit(); return p

    # ── Статистика ────────────────────────────────────────────
    def _today(self) -> str: return datetime.now().strftime("%Y-%m-%d")

    def stat_inc(self, field: str, n: int = 1) -> None:
        today = self._today()
        self._c().execute(
            f"INSERT INTO stats_daily(date,{field}) VALUES(?,?) "
            f"ON CONFLICT(date) DO UPDATE SET {field}={field}+excluded.{field}",
            (today, n)); self._c().commit()

    def get_stats(self, period: str = "today") -> dict:
        c = self._c(); now = datetime.now()
        if period == "today":
            rows = c.execute("SELECT * FROM stats_daily WHERE date=?", (self._today(),)).fetchall()
        elif period == "week":
            since = (now - timedelta(days=7)).strftime("%Y-%m-%d")
            rows = c.execute("SELECT * FROM stats_daily WHERE date>=?", (since,)).fetchall()
        elif period == "month":
            since = (now - timedelta(days=30)).strftime("%Y-%m-%d")
            rows = c.execute("SELECT * FROM stats_daily WHERE date>=?", (since,)).fetchall()
        else:  # year
            since = (now - timedelta(days=365)).strftime("%Y-%m-%d")
            rows = c.execute("SELECT * FROM stats_daily WHERE date>=?", (since,)).fetchall()
        totals = {"vacancies_found":0,"vacancies_failed":0,"replies_sent":0,"ai_errors":0,"subs_bought":0}
        for r in rows:
            for k in totals: totals[k] += r[k] if r[k] else 0
        now_iso = now.isoformat()
        totals["clients"] = c.execute("SELECT COUNT(*) FROM clients").fetchone()[0]
        totals["active_subs"] = c.execute(
            "SELECT COUNT(*) FROM clients WHERE sub_until>?", (now_iso,)).fetchone()[0]
        tok = self.get_tokens_today()
        totals["tokens_in"] = tok["tokens_in"]; totals["tokens_out"] = tok["tokens_out"]
        return totals

    def add_tokens(self, ti: int, to_: int) -> None:
        today = self._today()
        self._c().execute(
            "INSERT INTO ds_tokens(date,tokens_in,tokens_out) VALUES(?,?,?) "
            "ON CONFLICT(date) DO UPDATE SET tokens_in=tokens_in+excluded.tokens_in,"
            "tokens_out=tokens_out+excluded.tokens_out", (today, ti, to_)); self._c().commit()

    def get_tokens_today(self) -> dict:
        row = self._c().execute("SELECT * FROM ds_tokens WHERE date=?", (self._today(),)).fetchone()
        return dict(row) if row else {"tokens_in": 0, "tokens_out": 0}

    # ── Логи ──────────────────────────────────────────────────
    def add_log(self, level: str, message: str) -> None:
        try:
            self._c().execute("INSERT INTO logs(level,message) VALUES(?,?)", (level, message))
            self._c().commit()
        except Exception: pass

    def get_logs(self, date: str, limit: int = 200) -> list[dict]:
        return [dict(r) for r in self._c().execute(
            "SELECT * FROM logs WHERE ts LIKE ? ORDER BY id DESC LIMIT ?",
            (f"{date}%", limit)).fetchall()]

    def count_logs(self, date: str) -> int:
        return self._c().execute(
            "SELECT COUNT(*) FROM logs WHERE ts LIKE ?", (f"{date}%",)).fetchone()[0]

    def export_logs(self, date: str) -> bytes:
        rows = self._c().execute(
            "SELECT * FROM logs WHERE ts LIKE ? ORDER BY id ASC", (f"{date}%",)).fetchall()
        return "\n".join(f"{r['ts']} | {r['level']:<8} | {r['message']}" for r in rows).encode("utf-8")

    def cleanup(self) -> None:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=2)).strftime("%Y-%m-%d %H:%M:%S")
        self._c().execute("DELETE FROM logs WHERE ts<?", (cutoff,))
        self._c().execute("DELETE FROM vacancies WHERE suitable=0 AND created_at<?", (cutoff,))
        self._c().commit(); log.info("Авто-очистка выполнена")

    def clear_logs(self) -> None:
        self._c().execute("DELETE FROM logs")
        self._c().commit(); log.info("Логи очищены вручную")

    def is_sender_blocked(self, sender_id: int) -> bool:
        row = self._c().execute(
            "SELECT blocked_until FROM blocked_senders WHERE sender_id=?", (sender_id,)
        ).fetchone()
        if not row: return False
        if row["blocked_until"]:
            return datetime.now().isoformat() < row["blocked_until"]
        return True

    def block_sender(self, sender_id: int, username: str, reason: str, until: str) -> None:
        self._c().execute(
            "INSERT OR REPLACE INTO blocked_senders(sender_id,sender_username,reason,blocked_until) VALUES(?,?,?,?)",
            (sender_id, username, reason, until)); self._c().commit()

    def get_blocked_senders(self) -> list[dict]:
        return [dict(r) for r in self._c().execute(
            "SELECT * FROM blocked_senders ORDER BY added_at DESC").fetchall()]

    def unblock_sender(self, sender_id: int) -> None:
        self._c().execute("DELETE FROM blocked_senders WHERE sender_id=?", (sender_id,))
        self._c().commit()

    def delete_ds_rules(self) -> None:
        self._c().execute("DELETE FROM settings WHERE key LIKE 'ds_rule_%'")
        self._c().commit()

    def get_client_payments(self, client_id: int) -> list[dict]:
        return [dict(r) for r in self._c().execute(
            "SELECT * FROM payments WHERE client_id=? ORDER BY created_at DESC", (client_id,)
        ).fetchall()]
# ═══════════════════════════════════════════════════════════════
# КУРС USDT/RUB (для крипто-оплаты)
# ═══════════════════════════════════════════════════════════════
_rate_cache: dict = {"value": None, "ts": 0.0}
RATE_CACHE_TTL = 90  # секунд — не долбим API на каждый клик, но курс остаётся свежим

async def get_usdt_rub_rate() -> Optional[float]:
    """Курс USDT→RUB с P2P-рынка (продажа USDT за рубли).
    Источник — публичный API Binance P2P: цены на P2P синхронизированы между
    биржами (мейкеры арбитражат), поэтому курс близок к тому, что покажет BingX P2P.
    К курсу применяется небольшой запас (CRYPTO_RATE_BUFFER, по умолчанию 1.5%)
    вниз, чтобы не потерять на реальном выводе через BingX P2P.
    Результат кэшируется на RATE_CACHE_TTL секунд."""
    now = time.time()
    if _rate_cache["value"] and (now - _rate_cache["ts"]) < RATE_CACHE_TTL:
        return _rate_cache["value"]
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(
                "https://p2p.binance.com/bapi/c2c/v2/friendly/c2c/adv/search",
                json={"asset": "USDT", "fiat": "RUB", "tradeType": "SELL",
                      "page": 1, "rows": 10, "payTypes": [], "publisherType": None},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                data = await resp.json()
        prices = [float(row["adv"]["price"]) for row in data.get("data", [])]
        if not prices:
            raise ValueError("Пустой ответ от Binance P2P")
        # Берём среднее по топ-5 объявлений (устойчивее к разовым выбросам)
        market_rate = sum(prices[:5]) / len(prices[:5])
        rate = round(market_rate * (1 - CRYPTO_RATE_BUFFER / 100), 2)
        _rate_cache["value"] = rate; _rate_cache["ts"] = now
        return rate
    except Exception as e:
        log.error(f"get_usdt_rub_rate: {e}")
        if _pipeline: await notify_admin_error(_pipeline.bot, "get_usdt_rub_rate", e)
        return _rate_cache["value"]  # отдаём последний известный курс, если API недоступен

# ═══════════════════════════════════════════════════════════════
# DEEPSEEK
# ═══════════════════════════════════════════════════════════════
_DS_FAIL = DeepSeekResult(suitable=False, reason="Ошибка API", contact="")

async def call_deepseek(text: str, author_username: str, db: Database) -> DeepSeekResult:
    if not DEEPSEEK_KEY:
        log.error("DeepSeek: DEEPSEEK_API_KEY не задан в .env")
        return _DS_FAIL
    rules = db.get_ds_rules()
    rules_block = ("ГЛОБАЛЬНЫЕ ПРАВИЛА:\n" + "\n".join(f"- {r['value']}" for r in rules) + "\n\n") if rules else ""
    system = (rules_block + db.get_setting("ds_system_prompt") + "\n\n"
              'Верни JSON без markdown: {"suitable": bool, "reason": "макс 5 слов"}')
    payload = {
        "model": DEEPSEEK_MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": f"Автор: @{author_username}\n\nТекст:\n{text[:3000]}"},
        ],
        "max_tokens": 150,
        "temperature": 0.0,
        "response_format": {"type": "json_object"},
        # Явно отключаем режим рассуждений (thinking/CoT) — задаче классификации
        # он не нужен, а его скрытые токены могут в разы раздувать счёт.
        # Без явного отключения актуальные модели DeepSeek могут включать
        # reasoning по умолчанию (reasoning_effort=high), даже под именем
        # легаси-алиаса deepseek-chat.
        "thinking": {"type": "disabled"},
    }
    log.debug(f"DeepSeek запрос: url={DEEPSEEK_URL} model={DEEPSEEK_MODEL} key={DEEPSEEK_KEY[:8]}...")
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(
                DEEPSEEK_URL, json=payload,
                headers={"Authorization": f"Bearer {DEEPSEEK_KEY}", "Content-Type": "application/json"},
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                body = await resp.text()
                log.debug(f"DeepSeek ответ HTTP {resp.status}: {body[:300]}")
                if resp.status != 200:
                    log.error(f"DeepSeek HTTP {resp.status}: {body[:300]}")
                    db.stat_inc("ai_errors"); return _DS_FAIL
                data = json.loads(body)
        usage = data.get("usage", {})
        db.add_tokens(usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0))
        raw_content = data["choices"][0]["message"]["content"]
        log.debug(f"DeepSeek content: {raw_content}")
        parsed   = json.loads(raw_content)
        suitable = bool(parsed.get("suitable", False))
        reason   = str(parsed.get("reason", ""))[:60]
        # Автор — ВСЕГДА тот, кто реально написал сообщение (кто его отправил в
        # чат-источник), а не то, что ИИ мог бы найти в тексте по словам
        # "писать"/"пишите"/"контакт" — такой текст легко подделать/ввести в заблуждение.
        contact  = f"@{author_username}" if author_username else ""
        log.info(f"DeepSeek → suitable={suitable} reason='{reason}' contact='{contact}'")
        db.add_log("INFO", f"DS: suitable={suitable} reason={reason}")
        return DeepSeekResult(suitable=suitable, reason=reason, contact=contact)
    except json.JSONDecodeError as e:
        log.error(f"DeepSeek JSON ошибка: {e}")
        db.stat_inc("ai_errors")
        if _pipeline: await notify_admin_error(_pipeline.bot, "DeepSeek JSON", e)
        return _DS_FAIL
    except Exception as e:
        log.error(f"DeepSeek ошибка: {e}", exc_info=True)
        db.stat_inc("ai_errors")
        if _pipeline: await notify_admin_error(_pipeline.bot, "DeepSeek", e)
        return _DS_FAIL

_ds_status_cache: dict = {"value": None, "ts": 0.0}
DS_STATUS_CACHE_TTL = 120  # секунд

async def check_deepseek_status() -> str:
    """
    Возвращает: 'ok', 'no_key', 'wrong_model', 'error:<msg>'
    Кэшируется на DS_STATUS_CACHE_TTL секунд — без кэша каждое открытие
    главного меню админа (и раздела ИИ) делало живой платный запрос к DeepSeek
    просто ради иконки статуса, при частой навигации это накручивало лишние
    вызовы API и шумело в логах, не имея отношения к реальной проверке вакансий.
    """
    now = time.time()
    if _ds_status_cache["value"] and (now - _ds_status_cache["ts"]) < DS_STATUS_CACHE_TTL:
        return _ds_status_cache["value"]
    result = await _fetch_deepseek_status()
    _ds_status_cache["value"] = result
    _ds_status_cache["ts"] = now
    return result

async def _fetch_deepseek_status() -> str:
    if not DEEPSEEK_KEY:
        log.warning("DeepSeek: DEEPSEEK_API_KEY не задан в .env")
        return "no_key"
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(
                DEEPSEEK_URL,
                json={"model": DEEPSEEK_MODEL, "messages": [{"role": "user", "content": "ok"}], "max_tokens": 1},
                headers={"Authorization": f"Bearer {DEEPSEEK_KEY}", "Content-Type": "application/json"},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                body = await resp.text()
                if resp.status == 200:
                    log.info(f"DeepSeek OK: model={DEEPSEEK_MODEL}")
                    return "ok"
                # Разбираем ошибку
                log.warning(f"DeepSeek HTTP {resp.status}: {body[:300]}")
                try:
                    err_data = json.loads(body)
                    err_msg  = err_data.get("error", {}).get("message", body[:100])
                except Exception:
                    err_msg = body[:100]
                if "model" in err_msg.lower() or "supported" in err_msg.lower():
                    log.error(
                        f"DeepSeek: неверное имя модели '{DEEPSEEK_MODEL}'\n"
                        f"Сообщение от API: {err_msg}\n"
                        f"Измени DEEPSEEK_MODEL в .env"
                    )
                    return f"wrong_model:{err_msg[:80]}"
                return f"error:{err_msg[:80]}"
    except Exception as e:
        log.warning(f"DeepSeek недоступен: {e}")
        return f"error:{e}"

# ═══════════════════════════════════════════════════════════════
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ═══════════════════════════════════════════════════════════════
def norm_yo(s: str) -> str:
    """Приводит 'ё'→'е' (и 'Ё'→'Е'), чтобы сравнение ключевых слов/чёрного
    списка с текстом сообщения не зависело от того, как автор написал букву."""
    return s.replace("ё", "е").replace("Ё", "Е")

def now_sql() -> str:
    """Текущее время в формате и часовом поясе SQLite datetime('now') (UTC,
    'YYYY-MM-DD HH:MM:SS'). Колонки created_at/ts везде в БД используют именно
    этот формат по умолчанию — datetime.now().isoformat() даёт другой формат
    (буква 'T', микросекунды) и локальное время сервера вместо UTC, из-за чего
    строковое сравнение вида "created_at >= since" в SQLite ломается молча:
    не бросает ошибку, просто никогда не находит совпадений. Всегда сравнивать
    с этой функцией, а не с datetime.now().isoformat()."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

# Мусорные шаблонные приписки, которые некоторые боты-публикаторы (например,
# @mari_pro_vakansii_bot) автоматически добавляют к каждому посту. Ищем по
# устойчивому паттерну текста, а не по конкретному боту — надёжнее и не
# зависит от того, как именно называется аккаунт-публикатор.
_SOURCE_BOILERPLATE_PATTERNS = [
    re.compile(r"🔻?\s*Прочтите.*?правила безопасности.*?объявлени[ий]\.?\s*", re.DOTALL | re.IGNORECASE),
]

def strip_source_boilerplate(text: str) -> str:
    for pat in _SOURCE_BOILERPLATE_PATTERNS:
        text = pat.sub("", text)
    return text.strip()

def extract_text(message) -> tuple[str, str]:
    """Возвращает (plain, html) — plain для БД/ключевых слов/ИИ (без разметки),
    html — с сохранённым форматированием (жирный, ссылки и т.п.) для показа
    людям в клиент-боте. Раньше message.text отдавал markdown-реконструкцию
    (буквальные звёздочки **жирный**), теперь берём raw_text+entities и сами
    конвертируем в HTML — так форматирование доходит как настоящее, а не текстом."""
    plain = message.raw_text or ""
    try:
        html_body = tg_html.unparse(plain, message.entities or [])
    except Exception:
        html_body = html.escape(plain)
    plain     = strip_source_boilerplate(plain)
    html_body = strip_source_boilerplate(html_body)

    if message.media is None: return plain, html_body
    if isinstance(message.media, MessageMediaPhoto): marker = "[image]"
    elif isinstance(message.media, MessageMediaDocument):
        mime = getattr(getattr(message.media,"document",None),"mime_type","") or ""
        marker = "[pdf]" if "pdf" in mime else "[voice]" if "audio" in mime or "ogg" in mime else "[file]"
    elif isinstance(message.media, MessageMediaWebPage): return plain, html_body
    else: marker = "[file]"
    if not plain: return marker, marker
    return f"{marker}\n\nПодпись:\n{plain}", f"{marker}\n\nПодпись:\n{html_body}"

def make_msg_link(event, chat) -> str:
    uname = getattr(chat, "username", None); mid = event.message.id
    if uname: return f"https://t.me/{uname}/{mid}"
    raw = str(abs(event.chat_id))
    if raw.startswith("100"): raw = raw[3:]
    return f"https://t.me/c/{raw}/{mid}"

def fmt_date(iso: Optional[str]) -> str:
    if not iso: return "—"
    try: return datetime.fromisoformat(iso).strftime("%d.%m.%Y")
    except Exception: return iso[:10]

# ═══════════════════════════════════════════════════════════════
# KEYBOARD BUILDER
# ═══════════════════════════════════════════════════════════════
def mkb(buttons: list[list[tuple[str,str]]]) -> InlineKeyboardMarkup:
    """Строит клавиатуру. Если cd начинается с 'http' или 'tg://' — url-кнопка."""
    rows = []
    for row in buttons:
        btns = []
        for t, cd in row:
            if cd.startswith("http") or cd.startswith("tg://"):
                btns.append(InlineKeyboardButton(text=t, url=cd))
            else:
                btns.append(InlineKeyboardButton(text=t, callback_data=cd))
        rows.append(btns)
    return InlineKeyboardMarkup(inline_keyboard=rows)

def kb_back(dest: str = "admin_main") -> InlineKeyboardMarkup:
    return mkb([[("◀️ Назад", dest)]])

def kb_admin_main() -> InlineKeyboardMarkup:
    # Проверяем есть ли платежи на подтверждение
    pending = 0
    if _db:
        try:
            pending = _db._c().execute(
                "SELECT COUNT(*) FROM payments WHERE status='pending'").fetchone()[0]
        except Exception:
            pass
    client_label = f"👥 Клиент 🔴" if pending > 0 else "👥 Клиент"
    return mkb([
        [("♨️ Источники","admin_sources"), ("📋 Вакансии","admin_replies")],
        [("📈 Статистика","admin_stats"),   ("🖥️ Мониторинг","admin_monitoring")],
        [(client_label,"admin_clients"),   ("📜 Логи","admin_logs")],
        [("⚙️ Настройки","admin_settings")],
    ])

def kb_client_main() -> InlineKeyboardMarkup:
    return mkb([
        [("👥 Реферальная программа","client_referral")],
        [("💳 Тарифы","client_tariffs")],
        [("⚙️ Настройки","client_settings")],
    ])

def render_vacancy_client(v_html: str, contact: str, author_id: int, message_link: str) -> tuple[str, InlineKeyboardMarkup]:
    """Единый рендер вакансии для клиента. v_html — уже готовый HTML-текст
    (Vacancy.html_text, форматирование сохранено), НЕ экранируем его повторно —
    вызывающий код отвечает за то, что это безопасный HTML или escape-плейн."""
    contact = contact or ""
    if contact.lower().endswith("bot"):
        # Автор — бот (например @istochnik_bot, публикующий вакансии в канал
        # от своего имени) — писать ему по вакансии бессмысленно, строку об
        # авторе просто не показываем вообще (не пишем даже плейсхолдер)
        author_line = ""
    elif contact.startswith("@"):
        author_line = f"\n\nАвтор: {html.escape(contact)}"
    elif author_id:
        # У автора нет юзернейма — tg://user?id= открывает профиль напрямую,
        # но работает ТОЛЬКО в десктопном приложении Telegram
        link = f"tg://user?id={author_id}"
        author_line = (
            f"\n\nАвтор: <a href='{link}'>{link}</a>\n"
            f"⚠️ <i>Ссылка работает только в десктопном приложении Telegram. "
            f"Если вы не в приложении — перейдите в сообщение по кнопке ниже</i> ⚠️"
        )
    else:
        author_line = "\n\n⚠️ Автор не определён, перейдите к сообщению по кнопке ниже ⚠️"
    msg_text = f"📢 <b>Новая вакансия</b>\n\n{v_html}{author_line}"
    markup = mkb([[("🔗 Перейти к сообщению", message_link)]]) if message_link else None
    return msg_text, markup

def render_vacancy_admin(v: dict, back_to: Optional[str] = None) -> tuple[str, InlineKeyboardMarkup]:
    """Единый рендер уведомления/просмотра вакансии для админа — используется и
    при первой отправке, и при повторном открытии («Назад» из под-экранов, просмотр
    из списка «📋 Вакансии»). Один источник правды — не даёт экранам разъезжаться."""
    vid       = v["id"]
    contact   = v.get("ds_contact") or ""
    author_id = v.get("author_id") or 0
    if contact.startswith("@"):
        client_link = f"https://t.me/{contact.lstrip('@')}"
    else:
        client_link = f"tg://user?id={author_id}"
    short   = html.escape((v.get("text") or "")[:600])
    deleted = bool(v.get("deleted_by_admin"))
    rows: list = []

    if v.get("suitable"):
        title = "🗑 <b>Вакансия удалена</b>" if deleted else "✅ <b>Новая вакансия</b>"
        text = (
            f"{title}\n\n"
            f"<blockquote expandable>{short}</blockquote>\n\n"
            f"<a href='{v.get('message_link') or '#'}'>Сообщение</a> | "
            f"<a href='{client_link}'>Автор</a>"
        )
        if not deleted:
            rows.append([("⚠️ Ошибка", f"admin_error_vac:{vid}")])
            rows.append([("🚫 Заблокировать отправителя", f"admin_block_sender:{vid}")])
            rows.append([("🗑 Удалить у всех", f"admin_vac_delete_confirm:{vid}")])
    else:
        text = (
            f"❌ <b>Не прошло проверку!</b>\n\n"
            f"<blockquote expandable>{short}</blockquote>\n\n"
            f"<b>Причина:</b> {html.escape(v.get('ds_reason') or '')}\n\n"
            f"<a href='{v.get('message_link') or '#'}'>Сообщение</a> | "
            f"<a href='{client_link}'>Клиент</a>"
        )
        rows.append([("✅ Проверен", f"admin_manual_approve:{vid}"),
                     ("⚠️ Ошибка",  f"admin_error_vac:{vid}")])

    if back_to:
        rows.append([("◀️ Назад", back_to)])
    return text, mkb(rows)

# ═══════════════════════════════════════════════════════════════
# PIPELINE
# ═══════════════════════════════════════════════════════════════
class VacancyPipeline:
    def __init__(self, db: Database, userbot: TelegramClient, bot: Bot):
        self.db = db; self.userbot = userbot; self.bot = bot
        self.queue: asyncio.Queue = asyncio.Queue()

    async def enqueue(self, event) -> None:
        await self.queue.put(event)

    async def run_worker(self) -> None:
        log.info("Воркер очереди запущен")
        while True:
            try:
                event = await self.queue.get()
                await self._process(event)
                self.queue.task_done()
            except Exception as e:
                log.error(f"Воркер: {e}", exc_info=True)
                await notify_admin_error(self.bot, "run_worker", e)

    async def _process(self, event) -> None:
        try:
            if self.db.get_setting("monitoring_active","1") != "1": return
            msg    = event.message
            try:
                chat = await event.get_chat()
            except Exception:
                chat = None
            try:
                sender = await event.get_sender()
            except Exception:
                sender = None
            chat_id      = event.chat_id
            message_id   = msg.id
            source_title = getattr(chat,"title",None) or str(chat_id)
            username     = getattr(sender,"username",None) or ""
            sender_id    = getattr(sender,"id",0) or event.sender_id or 0
            message_link = make_msg_link(event, chat)
            text, html_text = extract_text(msg)
            if not text.strip(): return

            log.info(f"📥 [{source_title}] @{username}")
            self.db.add_log("INFO", f"Получено: {message_link} @{username}")

            # Фильтр заблокированных отправителей
            if sender_id and self.db.is_sender_blocked(sender_id):
                log.debug(f"⛔ Заблокированный отправитель: {sender_id}")
                self.db.add_log("INFO", f"⛔ Отправитель в блоке: {sender_id}")
                return

            # Фильтр: сообщение из 1 слова — невалидная вакансия
            word_count = len(text.split())
            if word_count < 2:
                log.debug(f"⛔ Слишком короткое сообщение ({word_count} сл.): пропуск")
                return

            # Ключевые слова (сравнение без разницы «ё»/«е»)
            kw_common = self.db.get_keywords("common")
            text_low  = norm_yo(text.lower())
            found_kw  = [kw for kw in kw_common if norm_yo(kw.lower()) in text_low]

            # Мягкие корни (эдит/мувик и т.п.) — засчитываются, только если
            # в тексте есть ещё и слово-триггер запроса. Оба списка редактируются
            # через админ-бота (🔑 Ключевые слова → Мягкие корни / Триггеры).
            if not found_kw:
                soft_roots = self.db.get_keywords("soft_root")
                triggers   = self.db.get_keywords("trigger")
                found_root    = [r for r in soft_roots if norm_yo(r.lower()) in text_low]
                found_trigger = [t for t in triggers if norm_yo(t.lower()) in text_low]
                if found_root and found_trigger:
                    found_kw = [f"{found_trigger[0]}+{found_root[0]}"]

            if not found_kw: return
            log.info(f"✅ КС: {found_kw}"); self.db.add_log("INFO", f"КС: {found_kw}")

            # Дубликат
            if self.db.is_duplicate(text):
                log.info("🔁 Дубликат"); self.db.add_log("INFO", "🔁 Дубликат (уже была такая вакансия)"); return

            # Слишком часто от одного отправителя (не чаще 1 вакансии в N минут)
            cooldown_min = int(self.db.get_setting("sender_cooldown_min", "30") or "30")
            if self.db.recent_vacancy_from_sender(sender_id, cooldown_min):
                log.info(f"⏱ Повтор от отправителя {sender_id} (< {cooldown_min} мин)")
                self.db.add_log("INFO", f"⏱ Пропущено: повтор от того же отправителя (< {cooldown_min} мин)")
                return

            # Чёрный список (тоже без разницы «ё»/«е»)
            found_bl = [w for w in self.db.get_blacklist("common") if norm_yo(w.lower()) in text_low]
            if found_bl:
                log.info(f"⛔ ЧС: {found_bl}")
                self.db.add_log("INFO", f"⛔ Отсеяно чёрным списком: {found_bl}")
                # Сохраняем текст, а не теряем его безвозвратно — чтобы потом
                # можно было выгрузить и проверить, что именно отсеяло слово
                blocked_v = Vacancy(chat_id=chat_id, message_id=message_id, text=text,
                                    author_username=username, author_id=sender_id,
                                    source_title=source_title, message_link=message_link,
                                    timestamp=datetime.now(), html_text=html_text)
                bvid = self.db.save_vacancy(blocked_v)
                if bvid: self.db.set_block_reason(bvid, f"чёрный список: {found_bl}")
                return

            # Дозапрос username, только если он не пришёл сразу — сюда доходят уже
            # ТОЛЬКО сообщения, прошедшие все дешёвые фильтры (раньше это делалось
            # для каждого входящего сообщения, включая весь спам-поток от ботов —
            # десятки тысяч лишних запросов к серверам Telegram в день, риск
            # FloodWait на юзерботе). Причина пропуска username чаще всего не в
            # том, что сессия юзербота вообще не видела этого юзера, а в том, что
            # Telegram присылает т.н. "min"-конструктор User (без username/bio —
            # сервер экономит трафик, предполагая что клиент якобы уже всё знает).
            # get_entity() тогда просто отдаёт тот же урезанный кэш повторно, не
            # помогает. GetFullUserRequest всегда идёт на сервер за полными
            # данными и обходит именно эту проблему.
            if not username and sender_id:
                # 1) InputUser напрямую из данных этого конкретного апдейта —
                # содержит свежий access_hash именно из этого сообщения, а не
                # потенциально устаревший/несовместимый по контексту кэш по
                # голому ID. Самый надёжный вариант, пробуем первым.
                try:
                    input_sender = await msg.get_input_sender()
                    if input_sender:
                        full = await self.userbot(GetFullUserRequest(input_sender))
                        if full.users:
                            username = getattr(full.users[0], "username", None) or ""
                except Exception as e:
                    log.debug(f"GetFullUserRequest(input_sender): {e}")
                # 2) Резолв по голому ID через кэш сессии
                if not username:
                    try:
                        full_sender = await self.userbot.get_entity(sender_id)
                        username = getattr(full_sender, "username", None) or ""
                    except Exception:
                        pass
                # 3) GetFullUserRequest по голому ID (может сработать, если
                # кэш содержит хотя бы устаревший access_hash того же юзера)
                if not username:
                    try:
                        full = await self.userbot(GetFullUserRequest(sender_id))
                        if full.users:
                            username = getattr(full.users[0], "username", None) or ""
                    except Exception as e:
                        log.debug(f"GetFullUserRequest({sender_id}): {e}")

            vacancy = Vacancy(chat_id=chat_id, message_id=message_id, text=text,
                              author_username=username, author_id=sender_id,
                              source_title=source_title, message_link=message_link,
                              timestamp=datetime.now(), html_text=html_text)
            vid = self.db.save_vacancy(vacancy)
            if not vid: return

            # DeepSeek
            if self.db.get_setting("ai_active","1") != "1":
                ds = DeepSeekResult(suitable=True, reason="AI выключен", contact=f"@{username}")
            else:
                ds = await call_deepseek(text, username, self.db)

            self.db.update_vacancy_ds(vid, ds.suitable, ds.reason, ds.contact)

            if not ds.suitable:
                self.db.stat_inc("vacancies_failed")
                log.info(f"❌ Не подходит: {ds.reason}")
                await self._notify_admin_rejected(vacancy, ds, vid); return

            self.db.stat_inc("vacancies_found")
            await self._handle_suitable(vacancy, ds, vid)
        except Exception as e:
            log.error(f"_process: {e}", exc_info=True)
            await notify_admin_error(self.bot, "_process", e)

    async def _handle_suitable(self, vacancy: Vacancy, ds: DeepSeekResult, vid: int) -> None:
        """Вакансия прошла проверку: авто-отклик больше не отправляется —
        только уведомление админу и рассылка клиентам."""
        await self._notify_admin_new_vacancy(vacancy, ds, vid)
        await self._broadcast(vacancy, ds, vid)

    async def _broadcast(self, vacancy: Vacancy, ds: DeepSeekResult, vid: int) -> None:
        if self.db.get_setting("client_bot_active","1") != "1": return
        clients = self.db.get_active_clients()
        log.info(f"📢 Рассылка {len(clients)} клиентам")
        for cl in clients:
            try:
                cl_id = cl["id"]
                text_low = norm_yo(vacancy.text.lower())
                stop_words = self.db.get_client_stopwords(cl_id)
                hit = [w for w in stop_words if norm_yo(w.lower()) in text_low]
                if hit:
                    self.db.save_delivery(vid, cl_id, None, skipped=True, reason=f"sw:{hit[0]}"); continue

                # Подписка уже гарантирована (get_active_clients фильтрует по ней) —
                # контакт можно показывать сразу, без отдельной кнопки-раскрытия
                msg_text, markup = render_vacancy_client(
                    vacancy.html_text or html.escape(vacancy.text),
                    ds.contact, vacancy.author_id, vacancy.message_link)
                sent   = await self.bot.send_message(cl["tg_id"], msg_text,
                                                     parse_mode=ParseMode.HTML, reply_markup=markup)
                self.db.save_delivery(vid, cl_id, sent.message_id)
            except Exception as e:
                log.error(f"Рассылка {cl.get('tg_id')}: {e}")

    async def _notify_admin_new_vacancy(self, vacancy: Vacancy, ds: DeepSeekResult, vid: int) -> None:
        """Уведомляет админа о новой подходящей вакансии (без авто-отклика)."""
        if self.db.get_setting("notify_reply","1") != "1":
            return
        v = self.db.get_vacancy(vid)
        if not v: return
        text, markup = render_vacancy_admin(v)
        try:
            await self.bot.send_message(ADMIN_ID, text, parse_mode=ParseMode.HTML, reply_markup=markup)
        except Exception as e:
            log.error(f"notify_new_vacancy: {e}")
            await notify_admin_error(self.bot, "notify_new_vacancy", e)

    async def _notify_admin_rejected(self, vacancy: Vacancy, ds: DeepSeekResult, vid: int) -> None:
        if self.db.get_setting("notify_rejected","1") != "1":
            return
        v = self.db.get_vacancy(vid)
        if not v: return
        text, markup = render_vacancy_admin(v)
        try:
            await self.bot.send_message(ADMIN_ID, text, parse_mode=ParseMode.HTML, reply_markup=markup)
        except Exception as e:
            log.error(f"notify_rejected: {e}")
            await notify_admin_error(self.bot, "notify_rejected", e)
# ═══════════════════════════════════════════════════════════════
# ГЛОБАЛЬНОЕ СОСТОЯНИЕ
# ═══════════════════════════════════════════════════════════════
_db:       Optional[Database]        = None
_pipeline: Optional[VacancyPipeline] = None
_userbot:  Optional[TelegramClient]  = None
_admin_pending:  dict[int, str] = {}
_client_pending: dict[int, str] = {}
_auth_state: dict = {}
_src_picker: dict[int, dict] = {}
_broadcast_draft: dict[int, dict] = {}  # uid -> {"text": str, "photo": Optional[str]}
_payment_drafts: dict[str, dict] = {}  # ticket -> детали платежа до подтверждения клиентом

admin_router  = Router()
client_router = Router()

class _IsAdmin(Filter):
    async def __call__(self, event) -> bool:
        u = getattr(event, "from_user", None)
        return u is not None and u.id == ADMIN_ID

admin_router.message.filter(_IsAdmin())
admin_router.callback_query.filter(_IsAdmin())

def is_admin(uid: int) -> bool: return uid == ADMIN_ID

async def safe_edit(call: CallbackQuery, text: str, markup=None) -> None:
    try:
        await call.message.edit_text(text, parse_mode=ParseMode.HTML,
                                     reply_markup=markup, disable_web_page_preview=True)
    except TelegramBadRequest: pass

async def safe_answer(msg: Message, text: str, markup=None) -> None:
    await msg.answer(text, parse_mode=ParseMode.HTML, reply_markup=markup,
                     disable_web_page_preview=True)

# ═══════════════════════════════════════════════════════════════
# ADMIN — ГЛАВНОЕ МЕНЮ
# ═══════════════════════════════════════════════════════════════
async def _admin_main_text() -> str:
    ds_status = await check_deepseek_status()
    ds_ok  = ds_status == "ok"
    ub_ok  = _userbot is not None and await _userbot.is_user_authorized()
    ai_on  = _db.get_setting("ai_active","1") == "1"
    mon_on = _db.get_setting("monitoring_active","1") == "1"
    cb_on  = _db.get_setting("client_bot_active","1") == "1"
    srcs   = len(_db.get_sources())
    def ico(v): return "🟢" if v else "🔴"

    # Детальный статус DeepSeek
    if ds_status == "ok":
        ds_label = "Активна"
    elif ds_status == "no_key":
        ds_label = "⚠️ Нет ключа API"
    elif ds_status.startswith("wrong_model"):
        ds_label = f"⚠️ Неверная модель ({DEEPSEEK_MODEL})"
    else:
        ds_label = "Недоступна"

    return (
        f"<b>phase.parser</b>\n\n"
        f"Статус бота: {ico(ub_ok)} {'Активен' if ub_ok else 'Не авторизован'}\n"
        f"ИИ проверка: {ico(ds_ok and ai_on)} {ds_label if not (ds_ok and ai_on) else 'Активна'}\n"
        f"Клиент бот: {ico(cb_on)} {'Активен' if cb_on else 'Выключен'}\n"
        f"📡 Источников: <b>{srcs}</b>"
    )

@admin_router.message(Command("start"))
async def admin_cmd_start(msg: Message):
    await safe_answer(msg, await _admin_main_text(), kb_admin_main())

@admin_router.callback_query(F.data == "admin_main")
async def admin_main_cb(call: CallbackQuery):
    await safe_edit(call, await _admin_main_text(), kb_admin_main())

# ═══════════════════════════════════════════════════════════════
# ADMIN — ИСТОЧНИКИ
# ═══════════════════════════════════════════════════════════════
@admin_router.callback_query(F.data == "admin_sources")
async def admin_sources_cb(call: CallbackQuery):
    srcs  = _db.get_sources(active_only=False)
    text  = f"<b>♨️ Источники</b>\n\nВсего источников: <b>{len(srcs)}</b>"
    markup = mkb([
        [("➕ Добавить из чатов","admin_src_add"), ("🔗 По ссылке","admin_src_add_link")],
        [("📋 Все источники","admin_src_list")],
        [("◀️ Главное меню","admin_main")],
    ])
    await safe_edit(call, text, markup)

@admin_router.callback_query(F.data == "admin_src_add_link")
async def admin_src_add_link_cb(call: CallbackQuery):
    _admin_pending[call.from_user.id] = "add_source_links"
    await safe_edit(call,
        "🔗 <b>Добавление по ссылке</b>\n\n"
        "Пришлите ссылки на чаты/каналы, каждую с новой строки.\n\n"
        "Поддерживаются:\n"
        "• Публичные: <code>https://t.me/username</code> или <code>@username</code>\n"
        "• Приватные (по инвайту): <code>https://t.me/+AbCdEfGh</code>\n\n"
        "<i>Для приватных групп юзербот автоматически вступит по ссылке.</i>",
        kb_back("admin_sources"))

@admin_router.callback_query(F.data == "admin_src_add")
async def admin_src_add_cb(call: CallbackQuery):
    uid = call.from_user.id
    # Если кэш уже есть — сразу рисуем без перезагрузки
    if uid in _src_picker and _src_picker[uid].get("chats"):
        await _draw_src_picker(call, uid)
        return
    await safe_edit(call, "⏳ <b>Загружаю список чатов...</b>", None)
    try:
        chats = []
        async for dialog in _userbot.iter_dialogs(limit=300):
            e = dialog.entity
            cls = e.__class__.__name__
            # Только группы и супергруппы — без каналов и личных чатов
            is_supergroup = getattr(e, "megagroup", False) or getattr(e, "gigagroup", False)
            is_basic_chat = cls == "Chat"
            is_broadcast  = getattr(e, "broadcast", False)
            is_user       = cls == "User"
            if is_user or is_broadcast:
                continue
            if not (is_supergroup or is_basic_chat):
                continue
            chats.append({
                "id":       str(dialog.id),
                "title":    dialog.name or str(dialog.id),
                "username": getattr(e, "username", None),
            })
            if len(chats) >= 60:
                break
    except Exception as ex:
        log.error(f"iter_dialogs: {ex}")
        await safe_edit(call, f"❌ Ошибка загрузки: <code>{ex}</code>", kb_back("admin_sources"))
        return

    if not chats:
        await safe_edit(call,
            "😕 <b>Групп не найдено</b>\n\nUserBot не состоит ни в одной группе.",
            kb_back("admin_sources"))
        return

    _src_picker[uid] = {"chats": {c["id"]: c for c in chats}, "selected": set()}
    await _draw_src_picker(call, uid)


async def _draw_src_picker(call: CallbackQuery, uid: int) -> None:
    state = _src_picker.get(uid)
    if not state:
        await safe_edit(call, "❌ Сессия устарела. Нажмите «Добавить» снова.", kb_back("admin_sources"))
        return

    chats    = state["chats"]
    selected = state["selected"]
    existing = {str(s["chat_id"]) for s in _db.get_sources(active_only=False)}

    rows = []
    for cid, c in chats.items():
        in_db   = cid in existing
        checked = cid in selected
        if in_db:
            icon = "✅"
            cd   = "noop"
        elif checked:
            icon = "☑️"
            cd   = f"src_pick:{cid}"
        else:
            icon = "  "
            cd   = f"src_pick:{cid}"
        rows.append([(f"{icon} {c['title'][:38]}", cd)])

    rows.append([(f"✔️ Готово ({len(selected)} выбрано)", "src_pick_done")])
    rows.append([("🔄 Обновить список", "src_pick_reload"), ("◀️ Назад", "admin_sources")])

    await safe_edit(call,
        f"<b>📡 Добавьте источники:</b>\n\n"
        f"☑️ — выбрано  |  ✅ — уже добавлен\n"
        f"Выбрано: <b>{len(selected)}</b>",
        mkb(rows))


@admin_router.callback_query(F.data.startswith("src_pick:"))
async def src_pick_cb(call: CallbackQuery):
    await call.answer()
    uid = call.from_user.id
    cid = call.data.split(":", 1)[1]
    if uid not in _src_picker:
        await safe_edit(call, "❌ Сессия устарела. Начните заново.", kb_back("admin_sources"))
        return
    selected = _src_picker[uid]["selected"]
    if cid in selected:
        selected.discard(cid)
    else:
        selected.add(cid)
    await _draw_src_picker(call, uid)


@admin_router.callback_query(F.data == "src_pick_reload")
async def src_pick_reload_cb(call: CallbackQuery):
    _src_picker.pop(call.from_user.id, None)
    await admin_src_add_cb(call)


@admin_router.callback_query(F.data == "src_pick_done")
async def src_pick_done_cb(call: CallbackQuery):
    uid   = call.from_user.id
    state = _src_picker.pop(uid, None)
    if not state or not state["selected"]:
        await call.answer("Ничего не выбрано", show_alert=True)
        return

    chats    = state["chats"]
    selected = state["selected"]
    added = 0; errors = []

    for cid in selected:
        c = chats.get(cid)
        if not c: continue
        try:
            uname = c.get("username")
            link  = f"https://t.me/{uname}" if uname else None
            ok    = _db.add_source(int(cid), c["title"], uname, link)
            if ok:
                added += 1
                log.info(f"Источник добавлен: {c['title']} ({cid})")
        except Exception as e:
            errors.append(c["title"][:20])
            log.error(f"add_source {cid}: {e}")

    result = f"✅ Добавлено источников: <b>{added}</b>"
    if errors:
        result += f"\n❌ Ошибки: {', '.join(errors)}"

    # Редактируем и через 2 сек переходим в главное меню
    try:
        await call.message.edit_text(result, parse_mode="HTML")
    except Exception:
        pass
    await asyncio.sleep(2)
    await admin_sources_cb(call)

@admin_router.callback_query(F.data == "admin_src_list")
async def admin_src_list_cb(call: CallbackQuery):
    srcs  = _db.get_sources(active_only=False)
    lines = []
    for s in srcs:
        icon = "✅" if s["active"] else "❌"
        link = s.get("link") or f"tg://openmessage?chat_id={s['chat_id']}"
        lines.append(f"{icon} <a href='{link}'>{s['title']}</a>")
    text   = f"<b>📋 Все источники ({len(srcs)})</b>\n\n" + ("\n".join(lines) if lines else "<i>Нет источников</i>")
    markup = mkb([
        [("📤 Импорт базы","admin_src_export")],
        [("⚙️ Управление","admin_src_manage")],
        [("◀️ Назад","admin_sources")],
    ])
    await safe_edit(call, text, markup)

@admin_router.callback_query(F.data == "admin_src_export")
async def admin_src_export_cb(call: CallbackQuery):
    srcs  = _db.get_sources(active_only=False)
    lines = [f"{'[ON]' if s['active'] else '[OFF]'} {s['title']} | {s.get('link','') or s['chat_id']}" for s in srcs]
    await call.message.answer_document(
        BufferedInputFile("\n".join(lines).encode("utf-8"), filename="sources.txt"),
        caption="📤 Список источников")
    await call.answer()

@admin_router.callback_query(F.data == "admin_src_manage")
async def admin_src_manage_cb(call: CallbackQuery):
    srcs = _db.get_sources(active_only=False)
    if not srcs: await call.answer("Нет источников"); return
    rows = []
    for s in srcs:
        icon = "✅" if s["active"] else "❌"
        rows.append([(f"{icon} {s['title'][:30]}", f"admin_src_toggle:{s['id']}")])
        rows.append([(f"🗑 {s['title'][:28]}", f"admin_src_del:{s['id']}")])
    rows.append([("◀️ Назад","admin_src_list")])
    await safe_edit(call, "<b>⚙️ Управление источниками</b>", mkb(rows))

@admin_router.callback_query(F.data.startswith("admin_src_toggle:"))
async def admin_src_toggle_cb(call: CallbackQuery):
    _db.toggle_source(int(call.data.split(":")[1])); await admin_src_manage_cb(call)

@admin_router.callback_query(F.data.startswith("admin_src_del:"))
async def admin_src_del_cb(call: CallbackQuery):
    _db.delete_source(int(call.data.split(":")[1]))
    await call.answer("Удалено"); await admin_src_manage_cb(call)

# ═══════════════════════════════════════════════════════════════
# ADMIN — ВАКАНСИИ (авто-отклик убран, только просмотр найденного)
# ═══════════════════════════════════════════════════════════════
@admin_router.callback_query(F.data == "admin_replies")
async def admin_vacancies_cb(call: CallbackQuery):
    await _show_vacancies_page(call, 0)

@admin_router.callback_query(F.data.startswith("admin_vac_page:"))
async def admin_vac_page_cb(call: CallbackQuery):
    await _show_vacancies_page(call, int(call.data.split(":")[1]))

async def _show_vacancies_page(call: CallbackQuery, page: int) -> None:
    limit  = 8
    items  = _db.get_recent_vacancies(limit=limit, offset=page * limit, suitable_only=True)
    rows   = []
    for v in items:
        title = (v["text"] or "")[:40].replace("\n", " ")
        rows.append([(f"#{v['id']} {title}", f"admin_vac_view:{v['id']}")])
    nav = []
    if page > 0: nav.append(("◀️", f"admin_vac_page:{page-1}"))
    if len(items) == limit: nav.append(("▶️", f"admin_vac_page:{page+1}"))
    if nav: rows.append(nav)
    rows.append([("📤 Экспорт лога (файлом)","admin_vac_export")])
    rows.append([("◀️ Главное меню","admin_main")])
    text = "<b>📋 Найденные вакансии</b>" if items else "<b>📋 Найденные вакансии</b>\n\n<i>Пока пусто</i>"
    await safe_edit(call, text, mkb(rows))

@admin_router.callback_query(F.data == "admin_vac_export")
async def admin_vac_export_cb(call: CallbackQuery):
    markup = mkb([
        [("Сегодня","admin_vac_export_do:1"), ("3 дня","admin_vac_export_do:3")],
        [("7 дней","admin_vac_export_do:7"), ("30 дней","admin_vac_export_do:30")],
        [("Всё время","admin_vac_export_do:0")],
        [("◀️ Назад","admin_replies")],
    ])
    await safe_edit(call,
        "<b>📤 Экспорт лога вакансий</b>\n\n"
        "Файл со ВСЕМИ вакансиями за период — и прошедшими проверку, и нет "
        "(текст, причина, контакт). Удобно скидывать на разбор раз в пару дней.\n\n"
        "За какой период?",
        markup)

@admin_router.callback_query(F.data.startswith("admin_vac_export_do:"))
async def admin_vac_export_do_cb(call: CallbackQuery):
    days = int(call.data.split(":")[1])
    if days > 0:
        since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
        rows = _db._c().execute(
            "SELECT * FROM vacancies WHERE created_at >= ? ORDER BY id", (since,)).fetchall()
        period_label = f"{days}d"
    else:
        rows = _db._c().execute("SELECT * FROM vacancies ORDER BY id").fetchall()
        period_label = "all"

    if not rows:
        await call.answer("За этот период вакансий нет", show_alert=True); return

    lines = []
    ok_count = fail_count = blocked_count = 0
    for r in rows:
        v = dict(r)
        suitable     = v.get("suitable")
        block_reason = v.get("block_reason")
        if suitable == 1:
            status = "✅ ПРОШЛА"; ok_count += 1
        elif suitable == 0:
            status = "❌ НЕ ПРОШЛА ИИ"; fail_count += 1
        elif block_reason:
            status = f"🚫 ОТСЕЯНО ДО ИИ ({block_reason})"; blocked_count += 1
        else:
            status = "⏳ НЕ ОБРАБОТАНА"
        lines.append(
            f"=== #{v['id']} | {status} | {v.get('created_at','')} ===\n"
            f"Источник: {v.get('source_title','')}\n"
            f"Причина: {v.get('ds_reason','') or block_reason or '—'}\n"
            f"Контакт: {v.get('ds_contact','') or '—'}\n"
            f"Ссылка: {v.get('message_link','') or '—'}\n"
            f"Текст:\n{v.get('text','')}\n"
        )
    header = (f"Экспорт вакансий phase.parser | период: {period_label} | всего: {len(rows)} "
              f"(прошло: {ok_count}, не прошло ИИ: {fail_count}, отсеяно до ИИ: {blocked_count})\n\n")
    content = header + ("\n" + "-"*60 + "\n\n").join(lines)

    file = BufferedInputFile(content.encode("utf-8"), filename=f"vacancies_{period_label}_{datetime.now().strftime('%Y%m%d_%H%M')}.txt")
    await call.message.answer_document(file, caption=f"📤 {len(rows)} вакансий за период «{period_label}» (✅{ok_count} / ❌{fail_count} / 🚫{blocked_count})")
    await call.answer()

@admin_router.callback_query(F.data.startswith("admin_vac_view:"))
async def admin_vac_view_cb(call: CallbackQuery):
    vid = int(call.data.split(":")[1])
    v   = _db.get_vacancy(vid)
    if not v: await call.answer("Не найдена"); return
    text, markup = render_vacancy_admin(v, back_to="admin_replies")
    await safe_edit(call, text, markup)

@admin_router.callback_query(F.data.startswith("admin_vac_notif_view:"))
async def admin_vac_notif_view_cb(call: CallbackQuery):
    """Возврат к уведомлению о вакансии из под-экранов (ошибка/блокировка)."""
    vid = int(call.data.split(":")[1])
    v   = _db.get_vacancy(vid)
    if not v: await call.answer("Вакансия не найдена"); return
    text, markup = render_vacancy_admin(v)
    await safe_edit(call, text, markup)

@admin_router.callback_query(F.data.startswith("admin_vac_delete_confirm:"))
async def admin_vac_delete_confirm_cb(call: CallbackQuery):
    vid = int(call.data.split(":")[1])
    await safe_edit(call,
        "🗑 <b>Удалить эту вакансию у всех клиентов?</b>\n\nСообщение пропадёт из чата у каждого, кому оно приходило. Действие необратимо.",
        mkb([[("✅ Да, удалить у всех", f"admin_vac_delete:{vid}"),
              ("❌ Отмена", f"admin_vac_notif_view:{vid}")]]))

@admin_router.callback_query(F.data.startswith("admin_vac_delete:"))
async def admin_vac_delete_cb(call: CallbackQuery):
    vid = int(call.data.split(":")[1])
    v   = _db.get_vacancy(vid)
    if not v: await call.answer("Не найдена"); return
    deliveries = _db.get_deliveries(vid)
    ok = fail = 0
    for d in deliveries:
        try:
            await call.bot.delete_message(d["tg_id"], d["msg_id"])
            ok += 1
        except Exception:
            fail += 1  # уже удалено получателем/бот не может удалить — не критично
    _db.mark_vacancy_deleted(vid)
    v = _db.get_vacancy(vid)
    text, markup = render_vacancy_admin(v)
    await safe_edit(call, text, markup)
    await call.answer(f"✅ Удалено у {ok} из {len(deliveries)}")

# ═══════════════════════════════════════════════════════════════
# ADMIN — СТАТИСТИКА
# ═══════════════════════════════════════════════════════════════
@admin_router.callback_query(F.data == "admin_stats")
async def admin_stats_cb(call: CallbackQuery):
    await _show_stats(call, "today")

@admin_router.callback_query(F.data.startswith("admin_stats_period:"))
async def admin_stats_period_cb(call: CallbackQuery):
    await _show_stats(call, call.data.split(":")[1])

async def _show_stats(call: CallbackQuery, period: str) -> None:
    labels = {"today":"Сегодня","week":"Неделя","month":"Месяц","year":"Год"}
    s = _db.get_stats(period)
    total = s['vacancies_found'] + s['vacancies_failed']
    text = (
        f"<b>📈 Статистика — {labels.get(period,'?')}</b>\n\n"
        f"Найдено вакансий: <b>{s['vacancies_found']}</b>\n"
        f"Не прошло проверку: <b>{s['vacancies_failed']}</b>\n"
        f"Всего вакансий: <b>{total}</b>\n"
        f"Ошибки ИИ: <b>{s['ai_errors']}</b>\n"
        f"Куплено подписок: <b>{s['subs_bought']}</b>\n\n"
        f"👥 Клиентов: <b>{s['clients']}</b>\n"
        f"💎 С подпиской: <b>{s['active_subs']}</b>"
    )
    markup = mkb([
        [("Неделя","admin_stats_period:week"), ("Месяц","admin_stats_period:month")],
        [("Год","admin_stats_period:year")],
        [("◀️ Главное меню","admin_main")],
    ])
    await safe_edit(call, text, markup)

# ═══════════════════════════════════════════════════════════════
# ADMIN — МОНИТОРИНГ
# ═══════════════════════════════════════════════════════════════
@admin_router.callback_query(F.data == "admin_monitoring")
async def admin_monitoring_cb(call: CallbackQuery):
    mon_on = _db.get_setting("monitoring_active","1") == "1"
    srcs   = len(_db.get_sources())
    cooldown = _db.get_setting("sender_cooldown_min", "30")
    icon   = "🟢 Активен" if mon_on else "🔴 Остановлен"
    try:
        ub_ok = _userbot is not None and _userbot.is_connected() and await _userbot.is_user_authorized()
    except Exception:
        ub_ok = False
    ub_icon = "🟢 На связи" if ub_ok else "🔴 Не в сети!"
    silent_min = int((time.time() - _last_event_at["ts"]) // 60)
    flow_icon  = "🟢" if silent_min < 15 else "🔴"
    text   = (f"<b>🖥️ Мониторинг</b>\n\nСтатус: {icon}\nUserBot: {ub_icon}\n"
              f"Поток сообщений: {flow_icon} последнее {silent_min} мин. назад\n"
              f"Источники отслеживаются: <b>{srcs}</b>\n"
              f"Антиспам от отправителя: не чаще 1 вакансии в <b>{cooldown} мин.</b>")
    markup = mkb([
        [("🔑 Ключевые слова","admin_kw"), ("🚫 Чёрный список","admin_bl")],
        [("🤖 ИИ","admin_deepseek")],
        [("⏱ Антиспам-период","admin_edit_cooldown"), ("📥 Импорт","admin_import")],
        [("🚫 Заблокированные","admin_blocked_list")],
        [("◀️ Главное меню","admin_main")],
    ])
    await safe_edit(call, text, markup)

@admin_router.callback_query(F.data == "admin_blocked_list")
async def admin_blocked_list_cb(call: CallbackQuery):
    blocked = _db.get_blocked_senders()
    if not blocked:
        await safe_edit(call, "<b>🚫 Заблокированные отправители</b>\n\n<i>Список пуст</i>",
                        kb_back("admin_monitoring"))
        return
    rows = []
    for b in blocked[:20]:
        label = f"@{b['sender_username']}" if b.get("sender_username") else f"ID {b['sender_id']}"
        rows.append([(f"❌ {label}", f"admin_unblock:{b['sender_id']}")])
    rows.append([("◀️ Назад","admin_monitoring")])
    await safe_edit(call,
        f"<b>🚫 Заблокированные отправители ({len(blocked)})</b>\n\nНажмите, чтобы разблокировать:",
        mkb(rows))

@admin_router.callback_query(F.data.startswith("admin_unblock:"))
async def admin_unblock_cb(call: CallbackQuery):
    sender_id = int(call.data.split(":")[1])
    _db.unblock_sender(sender_id)
    await call.answer("✅ Разблокирован")
    await admin_blocked_list_cb(call)

@admin_router.callback_query(F.data == "admin_edit_cooldown")
async def admin_edit_cooldown_cb(call: CallbackQuery):
    _admin_pending[call.from_user.id] = "edit_sender_cooldown"
    await safe_edit(call,
        "⏱ Не чаще 1 вакансии от одного отправителя в сколько минут?\n\nПришлите число, например <code>30</code>:",
        kb_back("admin_monitoring"))

@admin_router.callback_query(F.data == "admin_import")
async def admin_import_cb(call: CallbackQuery):
    kw       = _db.get_keywords("common")
    roots    = _db.get_keywords("soft_root")
    triggers = _db.get_keywords("trigger")
    bl       = _db.get_blacklist("common")
    prompt   = _db.get_setting("ds_system_prompt", "")
    rules    = _db.get_ds_rules()

    def block(title: str, items: list[str]) -> str:
        body = "\n".join(items) if items else "(пусто)"
        return f"=== {title} ===\n{body}\n"

    parts = [
        block("Ключевые слова", kw),
        block("Мягкие корни", roots),
        block("Триггеры", triggers),
        block("Стоп-слова", bl),
        f"=== Промт нейросети ===\n{prompt or '(пусто)'}\n",
        block("Глобальные правила", [r["value"] for r in rules]),
    ]
    content = "\n".join(parts)
    file = BufferedInputFile(content.encode("utf-8"),
                             filename=f"monitoring_{datetime.now().strftime('%Y%m%d_%H%M')}.txt")
    await call.message.answer_document(file, caption="📥 Экспорт раздела «Мониторинг» — можно переслать на разбор")
    await call.answer()

# ── Ключевые слова ─────────────────────────────────────────────
async def _kw_text_and_markup(category: str) -> tuple[str, InlineKeyboardMarkup]:
    words = _db.get_keywords(category)
    cnt   = len(words)
    markup = mkb([
        [("➕ Добавить",f"admin_kw_add:{category}"),
         ("➖ Удалить", f"admin_kw_del:{category}"),
         ("👁 Смотреть",f"admin_kw_view:{category}")],
        [("◀️ Назад","admin_kw")],
    ])
    return f"<b>🔑 Ключевые слова — {kw_cat_label(category)}</b>\n\nКол-во: <b>{cnt}</b>", markup

@admin_router.callback_query(F.data == "admin_kw")
async def admin_kw_cb(call: CallbackQuery):
    markup = mkb([
        [("Общий","admin_kw_cat:common"), ("Только себе","admin_kw_cat:admin")],
        [("🧩 Мягкие корни","admin_kw_cat:soft_root"), ("⚡ Триггеры","admin_kw_cat:trigger")],
        [("◀️ Назад","admin_monitoring")],
    ])
    await safe_edit(call,
        "<b>🔑 Ключевые слова</b>\n\nКуда хотите добавить ключевые слова?\n\n"
        "<i>«Мягкие корни» и «Триггеры» — особая пара: вакансия засчитывается, "
        "если в тексте есть слово из одного списка И слово из другого (не обязательно рядом). "
        "Годится для коротких общих слов вроде «эдит»/«мувик», которые опасно добавлять как обычное ключевое слово.</i>",
        markup)

@admin_router.callback_query(F.data.startswith("admin_kw_cat:"))
async def admin_kw_cat_cb(call: CallbackQuery):
    cat = call.data.split(":")[1]
    text, markup = await _kw_text_and_markup(cat)
    await safe_edit(call, text, markup)

@admin_router.callback_query(F.data.startswith("admin_kw_add:"))
async def admin_kw_add_cb(call: CallbackQuery):
    cat = call.data.split(":")[1]
    _admin_pending[call.from_user.id] = f"add_kw:{cat}"
    await safe_edit(call,
        f"<b>➕ Ключевые слова — {kw_cat_label(cat)}</b>\n\n"
        "Напишите ключевые слова через Enter:\n\n"
        "<i>Пример:\nМонтаж\nОплата\nMotion</i>",
        mkb([[("◀️ Назад",f"admin_kw_cat:{cat}")]]))

@admin_router.callback_query(F.data.startswith("admin_kw_view:"))
async def admin_kw_view_cb(call: CallbackQuery):
    cat   = call.data.split(":")[1]
    words = _db.get_keywords(cat)
    text  = ("<b>🔑 Все ключевые слова:</b>\n\n" +
             "\n".join(f"<code>{w}</code>" for w in words) if words else "<i>Список пуст</i>")
    markup = mkb([
        [("➕ Добавить",f"admin_kw_add:{cat}"), ("➖ Удалить",f"admin_kw_del:{cat}")],
        [("◀️ Назад",f"admin_kw_cat:{cat}")],
    ])
    await safe_edit(call, text, markup)

@admin_router.callback_query(F.data.startswith("admin_kw_del:"))
async def admin_kw_del_cb(call: CallbackQuery):
    cat = call.data.split(":")[1]
    _admin_pending[call.from_user.id] = f"del_kw:{cat}"
    await safe_edit(call,
        f"<b>➖ Удалить ключевые слова — {kw_cat_label(cat)}</b>\n\n"
        "Напишите слова через Enter:",
        mkb([[("◀️ Назад",f"admin_kw_cat:{cat}")]]))

# ── Чёрный список ──────────────────────────────────────────────
@admin_router.callback_query(F.data == "admin_bl")
async def admin_bl_cb(call: CallbackQuery):
    markup = mkb([
        [("Общий","admin_bl_cat:common"), ("Только себе","admin_bl_cat:admin")],
        [("◀️ Назад","admin_monitoring")],
    ])
    await safe_edit(call, "<b>🚫 Чёрный список</b>\n\nКуда хотите добавить стоп-слова?", markup)

@admin_router.callback_query(F.data.startswith("admin_bl_cat:"))
async def admin_bl_cat_cb(call: CallbackQuery):
    cat   = call.data.split(":")[1]
    words = _db.get_blacklist(cat)
    markup = mkb([
        [("➕ Добавить",f"admin_bl_add:{cat}"),
         ("➖ Удалить", f"admin_bl_del:{cat}"),
         ("👁 Смотреть",f"admin_bl_view:{cat}")],
        [("◀️ Назад","admin_bl")],
    ])
    await safe_edit(call,
        f"<b>🚫 ЧС — {'Общий' if cat=='common' else 'Мои'}</b>\n\nКол-во: <b>{len(words)}</b>",
        markup)

@admin_router.callback_query(F.data.startswith("admin_bl_add:"))
async def admin_bl_add_cb(call: CallbackQuery):
    cat = call.data.split(":")[1]
    _admin_pending[call.from_user.id] = f"add_bl:{cat}"
    await safe_edit(call,
        "➕ Напишите стоп-слова через Enter:\n\n<i>Пример:\nОкна\nТестовое\nШтат</i>",
        mkb([[("◀️ Назад",f"admin_bl_cat:{cat}")]]))

@admin_router.callback_query(F.data.startswith("admin_bl_view:"))
async def admin_bl_view_cb(call: CallbackQuery):
    cat   = call.data.split(":")[1]
    words = _db.get_blacklist(cat)
    text  = ("<b>🚫 Все стоп-слова:</b>\n\n" +
             "\n".join(f"<code>{w}</code>" for w in words)) if words else "<b>🚫 Список пуст</b>"
    markup = mkb([
        [("➕ Добавить",f"admin_bl_add:{cat}"), ("➖ Удалить",f"admin_bl_del:{cat}")],
        [("◀️ Назад",f"admin_bl_cat:{cat}")],
    ])
    await safe_edit(call, text, markup)

@admin_router.callback_query(F.data.startswith("admin_bl_del:"))
async def admin_bl_del_cb(call: CallbackQuery):
    cat = call.data.split(":")[1]
    _admin_pending[call.from_user.id] = f"del_bl:{cat}"
    await safe_edit(call,
        "➖ Напишите стоп-слова для удаления через Enter:",
        mkb([[("◀️ Назад",f"admin_bl_cat:{cat}")]]))

# ── DeepSeek ───────────────────────────────────────────────────
@admin_router.callback_query(F.data == "admin_deepseek")
async def admin_deepseek_cb(call: CallbackQuery):
    tok    = _db.get_tokens_today()
    s      = _db.get_stats("today")
    calls_today = s.get("vacancies_found",0) + s.get("vacancies_failed",0)
    avg_tok = (tok['tokens_in']+tok['tokens_out']) // calls_today if calls_today else 0
    ds_status = await check_deepseek_status()
    ds_ok  = ds_status == "ok"
    rules  = _db.get_ds_rules()
    if ds_status == "ok":
        status = "🟢 Активен"
    elif ds_status == "no_key":
        status = "🔴 Нет ключа API"
    elif ds_status.startswith("wrong_model"):
        status = f"🔴 Неверная модель\nИзмени DEEPSEEK_MODEL в .env"
    else:
        status = f"🔴 Недоступен"
    text   = (
        f"<b>🤖 {DEEPSEEK_MODEL}</b>\n\n"
        f"Статус: {status}\n"
        f"Запросов сегодня: <b>{calls_today}</b>\n"
        f"Расход токенов в день: ↑<code>{tok['tokens_in']}</code> ↓<code>{tok['tokens_out']}</code>\n"
        f"В среднем на запрос: <b>~{avg_tok}</b> токенов\n"
        f"Глобальных правил: <b>{len(rules)}</b>"
    )
    markup = mkb([
        [("📋 Смотреть промт","admin_ds_prompt"), ("🌐 Глобальное правило","admin_ds_rule_add")],
        [("◀️ Назад","admin_monitoring")],
    ])
    await safe_edit(call, text, markup)

@admin_router.callback_query(F.data == "admin_ds_prompt")
async def admin_ds_prompt_cb(call: CallbackQuery):
    prompt = _db.get_setting("ds_system_prompt")
    await safe_edit(call,
        f"<b>📋 Системный промт:</b>\n\n<blockquote>{prompt[:1000]}</blockquote>",
        mkb([[("✏️ Изменить","admin_ds_prompt_edit")],[("◀️ Назад","admin_deepseek")]]))

@admin_router.callback_query(F.data == "admin_ds_prompt_edit")
async def admin_ds_prompt_edit_cb(call: CallbackQuery):
    _admin_pending[call.from_user.id] = "edit_ds_prompt"
    await safe_edit(call, "✏️ Вставьте новый промт:", kb_back("admin_ds_prompt"))

@admin_router.callback_query(F.data == "admin_ds_rule_add")
async def admin_ds_rule_add_cb(call: CallbackQuery):
    rules = _db.get_ds_rules()
    rules_text = "\n".join(f"{i+1}. {r['value']}" for i,r in enumerate(rules)) if rules else "нет правил"
    _admin_pending[call.from_user.id] = "add_ds_rule"
    await safe_edit(call,
        f"<b>🌐 Глобальное правило</b>\n\n"
        f"<b>Инструкция:</b>\n"
        f"• Пишите кратко и однозначно\n"
        f"• Одно правило — одно условие\n"
        f"• Используйте: «Если X → suitable=false»\n\n"
        f"<b>Текущие правила:</b>\n{rules_text}\n\n"
        f"Введите новое правило:",
        kb_back("admin_deepseek"))


# ═══════════════════════════════════════════════════════════════
# ADMIN — КЛИЕНТЫ
# ═══════════════════════════════════════════════════════════════
@admin_router.callback_query(F.data == "admin_clients")
async def admin_clients_cb(call: CallbackQuery):
    clients  = _db.get_all_clients()
    pending_rows = _db._c().execute(
        "SELECT p.*, c.tg_id, c.username FROM payments p "
        "JOIN clients c ON p.client_id=c.id WHERE p.status='pending' ORDER BY p.id DESC"
    ).fetchall()
    pending = [dict(r) for r in pending_rows]
    now     = datetime.now().isoformat()
    active  = sum(1 for c in clients if (c.get("sub_until") or "") > now)
    total   = len(clients)
    lines   = [
        f"👥 Всего клиентов: <b>{total}</b>",
        f"💎 Активных подписок: <b>{active}</b>",
    ]
    if pending:
        lines.append(f"🔴 Ожидают подтверждения: <b>{len(pending)}</b>")
    rows = []
    if pending:
        rows.append([("🔴 Подтвердить оплаты", "admin_pending_payments")])
    rows.append([("📋 Список (текстом)", "admin_clients_grouped"), ("⚙️ Управление", "admin_clients_list")])
    rows.append([("➕ Выдать подписку", "admin_give_sub"), ("🔍 Найти клиента", "admin_find_client")])
    rows.append([("📤 Рассылка", "admin_broadcast")])
    rows.append([("◀️ Главное меню", "admin_main")])
    await safe_edit(call, "\n".join(lines), mkb(rows))

@admin_router.callback_query(F.data == "admin_find_client")
async def admin_find_client_cb(call: CallbackQuery):
    _admin_pending[call.from_user.id] = "find_client"
    await safe_edit(call,
        "🔍 <b>Поиск клиента</b>\n\nПришлите @юзернейм или Telegram ID:",
        kb_back("admin_clients"))


@admin_router.callback_query(F.data == "admin_pending_payments")
async def admin_pending_payments_cb(call: CallbackQuery):
    rows = _db._c().execute(
        "SELECT p.*, c.tg_id, c.username FROM payments p "
        "JOIN clients c ON p.client_id=c.id WHERE p.status='pending' ORDER BY p.id DESC"
    ).fetchall()
    pending = [dict(r) for r in rows]
    if not pending:
        await safe_edit(call, "✅ Нет ожидающих платежей", kb_back("admin_clients"))
        return
    btns = []
    for p in pending:
        uname = p.get("username") or str(p["tg_id"])
        lbl   = f"@{uname} | {p['tariff']} | {p['amount']}₽ | {p['ticket']}"
        btns.append([(lbl[:55], f"admin_pay_detail:{p['ticket']}")])
    btns.append([("◀️ Назад", "admin_clients")])
    await safe_edit(call, f"🔴 <b>Ожидают: {len(pending)}</b>", mkb(btns))


@admin_router.callback_query(F.data.startswith("admin_pay_detail:"))
async def admin_pay_detail_cb(call: CallbackQuery):
    ticket = call.data.split(":", 1)[1]
    p  = _db.get_payment_by_ticket(ticket)
    if not p:
        await call.answer("Тикет не найден"); return
    cl = _db.get_client_by_id(p["client_id"])
    if not cl:
        await call.answer("Клиент не найден"); return
    uname = cl.get("username") or str(cl["tg_id"])
    lines = [
        "💳 <b>Платёж</b>",
        f"Тикет: <code>{ticket}</code>",
        f"Клиент: @{uname} (<code>{cl['tg_id']}</code>)",
        f"Тариф: <b>{p['tariff']}</b>",
        f"Сумма: <b>{p['amount']}₽</b>",
        f"Дней: <b>{p['days']}</b>",
        f"Создан: {p['created_at'][:16]}",
    ]
    markup = mkb([
        [("✅ Подтвердить", f"admin_confirm_pay:{ticket}"),
         ("❌ Отклонить",   f"admin_reject_pay:{ticket}")],
        [("◀️ Назад", "admin_pending_payments")],
    ])
    await safe_edit(call, "\n".join(lines), markup)


@admin_router.callback_query(F.data.startswith("admin_reject_pay:"))
async def admin_reject_pay_cb(call: CallbackQuery):
    ticket = call.data.split(":", 1)[1]
    _db._c().execute("UPDATE payments SET status='rejected' WHERE ticket=?", (ticket,))
    _db._c().commit()
    p = _db.get_payment_by_ticket(ticket)
    if p:
        cl = _db.get_client_by_id(p["client_id"])
        if cl:
            try:
                await call.bot.send_message(
                    cl["tg_id"],
                    f"❌ Платёж <code>{ticket}</code> не подтверждён.\n\n"
                    f"Проверьте перевод и отправьте чек аккаунт-менеджеру: @{SUPPORT_USERNAME}",
                    parse_mode=ParseMode.HTML,
                    reply_markup=mkb([[("💬 Аккаунт-менеджер", f"https://t.me/{SUPPORT_USERNAME}")]]),
                )
            except Exception:
                pass
    await call.answer("Отклонено")
    await safe_edit(call, f"❌ Платёж <code>{ticket}</code> отклонён", kb_back("admin_clients"))


@admin_router.callback_query(F.data == "admin_clients_grouped")
async def admin_clients_grouped_cb(call: CallbackQuery):
    clients = _db.get_all_clients()
    now     = datetime.now().isoformat()
    with_sub, without_sub = [], []
    for c in clients:
        if c.get("username"):
            label = f"@{c['username']}"
        else:
            link = f"tg://user?id={c['tg_id']}"
            label = f"<a href='{link}'>{link}</a>"
        (with_sub if (c.get("sub_until") or "") > now else without_sub).append(label)

    def build_block(title: str, items: list[str], budget: int) -> tuple[str, int]:
        """Возвращает (текст блока, сколько символов израсходовано). Обрезаем
        по границе целых строк — никогда не разрываем HTML-тег посередине,
        иначе Telegram откажется парсить сообщение целиком."""
        header = f"<b>{title} ({len(items)})</b>\n"
        used   = len(header)
        lines  = []
        for item in items:
            if used + len(item) + 1 > budget:
                lines.append(f"<i>...и ещё {len(items) - len(lines)}</i>")
                break
            lines.append(item); used += len(item) + 1
        body = "\n".join(lines) if lines else "<i>пусто</i>"
        return header + body, used

    budget = 3800  # с запасом от лимита в 4096
    sub_block, used1   = build_block(f"С подпиской", with_sub, budget // 2)
    free_block, used2  = build_block(f"Без подписки", without_sub, budget - used1)

    text = f"<b>👥 Все клиенты ({len(clients)})</b>\n\n{sub_block}\n\n{free_block}"
    await safe_edit(call, text, kb_back("admin_clients"))

@admin_router.callback_query(F.data == "admin_clients_list")
async def admin_clients_list_cb(call: CallbackQuery):
    await _show_clients_page(call, 0)

@admin_router.callback_query(F.data.startswith("admin_clients_page:"))
async def admin_clients_page_cb(call: CallbackQuery):
    await _show_clients_page(call, int(call.data.split(":")[1]))

async def _show_clients_page(call: CallbackQuery, page: int) -> None:
    limit   = 30
    clients = _db.get_all_clients()
    now     = datetime.now().isoformat()
    chunk   = clients[page*limit:(page+1)*limit]
    rows    = []
    for c in chunk:
        icon  = "💎" if (c.get("sub_until") or "") > now else "👤"
        uname = c.get("username")
        label = f"@{uname}" if uname else f"id:{c['tg_id']}"
        rows.append([(f"{icon} {label}", f"admin_client_detail:{c['id']}")])
    nav = []
    if page > 0: nav.append(("◀️", f"admin_clients_page:{page-1}"))
    if (page+1)*limit < len(clients): nav.append(("▶️", f"admin_clients_page:{page+1}"))
    if nav: rows.append(nav)
    rows.append([("◀️ Назад","admin_clients")])
    await safe_edit(call, f"<b>📋 Клиенты ({len(clients)})</b>\nСтраница {page+1}", mkb(rows))

def render_client_detail(client_id: int) -> Optional[tuple[str, InlineKeyboardMarkup]]:
    c = _db.get_client_by_id(client_id)
    if not c: return None
    now    = datetime.now().isoformat()
    active = (c.get("sub_until") or "") > now
    icon   = "💎" if active else "👤"
    uname  = c.get("username") or ""
    until  = fmt_date(c.get("sub_until"))
    display_name = f"@{uname}" if uname else f"<code>{c['tg_id']}</code>"

    # История пополнений
    payments = _db.get_client_payments(client_id)
    hist_lines = [f"{p['created_at'][:10]} {p['tariff']} {p['amount']}₽" for p in payments]
    hist_block = "<blockquote>" + "\n".join(hist_lines) + "</blockquote>" if hist_lines else "<i>нет</i>"

    text = (
        f"{icon} <b>📋 Клиент</b> {display_name}\n"
        f"ID: <code>{c['tg_id']}</code>\n\n"
        f"Тариф: {until}\n\n"
        f"История пополнений:\n{hist_block}"
    )
    markup = mkb([
        [("➕ Выдать подписку", f"admin_give_sub_client:{client_id}"),
         ("🚫 Заблокировать",  f"admin_block_client:{client_id}")],
        [("◀️ Назад","admin_clients_list")],
    ])
    return text, markup

@admin_router.callback_query(F.data.startswith("admin_client_detail:"))
async def admin_client_detail_cb(call: CallbackQuery):
    client_id = int(call.data.split(":")[1])
    result = render_client_detail(client_id)
    if not result: await call.answer("Не найден"); return
    text, markup = result
    await safe_edit(call, text, markup)

@admin_router.callback_query(F.data.startswith("admin_give_sub_client:"))
async def admin_give_sub_client_cb(call: CallbackQuery):
    client_id = call.data.split(":")[1]
    _admin_pending[call.from_user.id] = f"give_sub_id:{client_id}"
    await safe_edit(call,
        "➕ Введите количество дней:",
        kb_back(f"admin_client_detail:{client_id}"))

@admin_router.callback_query(F.data.startswith("admin_block_client:"))
async def admin_block_client_cb(call: CallbackQuery):
    client_id = call.data.split(":")[1]
    c = _db.get_client_by_id(int(client_id))
    if not c: await call.answer("Клиент не найден"); return
    tg_id = c["tg_id"]; uname = c.get("username") or ""
    display = f"@{uname}" if uname else f"ID: <code>{tg_id}</code>"
    markup = mkb([
        [("✅ Да, заблокировать", f"admin_block_client_confirm:{client_id}"),
         ("❌ Нет",               f"admin_client_detail:{client_id}")],
    ])
    await safe_edit(call,
        f"🚫 <b>Заблокировать?</b>\n\n{display}", markup)

@admin_router.callback_query(F.data.startswith("admin_block_client_confirm:"))
async def admin_block_client_confirm_cb(call: CallbackQuery):
    client_id = call.data.split(":")[1]
    c = _db.get_client_by_id(int(client_id))
    if not c: await call.answer("Клиент не найден"); return
    tg_id = c["tg_id"]; uname = c.get("username") or ""
    display = f"@{uname}" if uname else f"id:{tg_id}"
    _db.block_sender(tg_id, uname, "Заблокирован администратором", None)
    log.info(f"Отправитель заблокирован (из карточки клиента): {tg_id} {display}")
    await safe_edit(call,
        f"✅ <b>Заблокирован</b>\n\n{display}",
        kb_back(f"admin_client_detail:{client_id}"))


@admin_router.callback_query(F.data == "admin_give_sub")
async def admin_give_sub_cb(call: CallbackQuery):
    _admin_pending[call.from_user.id] = "give_sub"
    await safe_edit(call,
        "➕ <b>Выдача подписки</b>\n\nВведите: <code>TG_ID количество_дней</code>\n"
        "Пример: <code>123456789 30</code>",
        kb_back("admin_clients"))


@admin_router.callback_query(F.data == "admin_broadcast")
async def admin_broadcast_cb(call: CallbackQuery):
    _admin_pending[call.from_user.id] = "broadcast_text"
    _broadcast_draft.pop(call.from_user.id, None)
    await safe_edit(call,
        "📤 <b>Рассылка всем активным клиентам</b>\n\nВведите текст:",
        kb_back("admin_clients"))

@admin_router.callback_query(F.data == "admin_bcast_add_photo")
async def admin_bcast_add_photo_cb(call: CallbackQuery):
    if call.from_user.id not in _broadcast_draft:
        await call.answer("Сессия истекла, начните заново", show_alert=True); return
    _admin_pending[call.from_user.id] = "broadcast_photo"
    await safe_edit(call, "📷 Пришлите фото:", kb_back("admin_clients"))

@admin_router.callback_query(F.data == "admin_bcast_preview")
async def admin_bcast_preview_cb(call: CallbackQuery):
    await _show_broadcast_preview(call.bot, call.from_user.id)
    await call.answer()

async def _show_broadcast_preview(bot: Bot, uid: int) -> None:
    draft = _broadcast_draft.get(uid)
    if not draft: return
    text  = draft["text"]; photo = draft.get("photo")
    # Показываем ровно то же сообщение, которое получит клиент
    if photo:
        await bot.send_photo(uid, photo, caption=text, parse_mode=ParseMode.HTML)
    else:
        await bot.send_message(uid, text, parse_mode=ParseMode.HTML)
    await bot.send_message(uid,
        "☝️ Именно так это увидит клиент. Отправляем всем активным клиентам?",
        reply_markup=mkb([[("✅ Отправить всем","admin_bcast_send"), ("❌ Отмена","admin_bcast_cancel")]]))

@admin_router.callback_query(F.data == "admin_bcast_send")
async def admin_bcast_send_cb(call: CallbackQuery):
    uid   = call.from_user.id
    draft = _broadcast_draft.pop(uid, None)
    if not draft:
        await call.answer("Сессия истекла, начните заново", show_alert=True); return
    text = draft["text"]; photo = draft.get("photo")
    clients = _db.get_active_clients(); sent = 0
    for cl in clients:
        try:
            if photo:
                await call.bot.send_photo(cl["tg_id"], photo, caption=text, parse_mode=ParseMode.HTML)
            else:
                await call.bot.send_message(cl["tg_id"], text, parse_mode=ParseMode.HTML)
            sent += 1; await asyncio.sleep(0.05)
        except Exception as e: log.warning(f"Рассылка {cl['tg_id']}: {e}")
    await safe_edit(call, f"✅ Рассылка завершена: <b>{sent}/{len(clients)}</b>", kb_back("admin_clients"))

@admin_router.callback_query(F.data == "admin_bcast_cancel")
async def admin_bcast_cancel_cb(call: CallbackQuery):
    _broadcast_draft.pop(call.from_user.id, None)
    await safe_edit(call, "❌ Рассылка отменена", kb_back("admin_clients"))


# ═══════════════════════════════════════════════════════════════
# ADMIN — ЛОГИ
# ═══════════════════════════════════════════════════════════════
@admin_router.callback_query(F.data == "admin_logs")
async def admin_logs_cb(call: CallbackQuery):
    today = datetime.now().strftime("%Y-%m-%d")
    cnt   = _db.count_logs(today)
    text  = f"<b>📜 Логи за {today}</b>\nСтрок: <b>{cnt}</b>"
    markup = mkb([
        [("📤 Экспорт",f"admin_logs_export:{today}")],
        [("🗑 Очистить все логи","admin_clear_logs")],
        [("◀️ Главное меню","admin_main")],
    ])
    await safe_edit(call, text, markup)

@admin_router.callback_query(F.data.startswith("admin_logs_export:"))
async def admin_logs_export_cb(call: CallbackQuery):
    date = call.data.split(":")[1]
    data = _db.export_logs(date)
    if not data:
        await call.answer(f"За {date} логов нет (пусто)", show_alert=True)
        return
    await call.message.answer_document(
        BufferedInputFile(data, filename=f"logs_{date}.txt"),
        caption=f"📜 Логи за {date}")
    await call.answer()

# ═══════════════════════════════════════════════════════════════
# ADMIN — НАСТРОЙКИ
# ═══════════════════════════════════════════════════════════════
@admin_router.callback_query(F.data == "admin_settings")
async def admin_settings_cb(call: CallbackQuery):
    ai_on       = _db.get_setting("ai_active","1") == "1"
    mon_on      = _db.get_setting("monitoring_active","1") == "1"
    cb_on       = _db.get_setting("client_bot_active","1") == "1"
    markup = mkb([
        [(f"🤖 ИИ {'включено' if ai_on else 'выключено'}", "admin_settings_toggle:ai_active")],
        [(f"🖥️ Мониторинг {'включен' if mon_on else 'выключен'}", "admin_settings_toggle:monitoring_active")],
        [(f"👥 Клиент бот {'включен' if cb_on else 'выключен'}", "admin_settings_toggle:client_bot_active")],
        [("📣 Оповещения", "admin_notifications")],
        [("✉️ Тексты сообщений","admin_msg_texts"), ("📨 Рассылки","admin_broadcast_settings")],
        [("🗑 Очистить логи", "admin_clear_logs"), ("🌐 Удалить все правила", "admin_clear_ds_rules")],
        [("◀️ Главное меню","admin_main")],
    ])
    await safe_edit(call, "<b>⚙️ Настройки</b>", markup)

@admin_router.callback_query(F.data == "admin_broadcast_settings")
async def admin_broadcast_settings_cb(call: CallbackQuery):
    hours   = _db.get_setting("reminder_hours_before", "24")
    b_on    = _db.get_setting("broadcast_enabled", "0") == "1"
    b_time  = _db.get_setting("broadcast_time_msk", "10:00")
    b_photo = _db.get_setting("broadcast_photo_file_id", "")
    now_msk = datetime.now(MSK).strftime("%H:%M")
    text = (
        f"<b>📨 Настройки рассылок</b>\n\n"
        f"⏰ Напоминание об окончании подписки: за <b>{hours} ч.</b> до конца\n\n"
        f"📢 Общая рассылка по расписанию: {'✅ включена' if b_on else '❌ выключена'}\n"
        f"Время отправки: <b>{b_time} МСК</b> (каждый день)\n"
        f"Фото: {'✅ прикреплено' if b_photo else '— нет'}\n\n"
        f"<i>Сейчас в Москве: {now_msk}</i>"
    )
    markup = mkb([
        [("⏰ Изменить период напоминания","admin_bc_edit_hours")],
        [(f"📢 Рассылка: {'выключить' if b_on else 'включить'}","admin_bc_toggle")],
        [("🕐 Изменить время рассылки (МСК)","admin_bc_edit_time")],
        [("🖼 Изменить фото" if b_photo else "🖼 Прикрепить фото","admin_bc_set_photo")] +
        ([("🗑 Убрать фото","admin_bc_remove_photo")] if b_photo else []),
        [("✉️ Текст напоминания","admin_msg_view:msg_reminder_24h"),
         ("✉️ Текст рассылки","admin_msg_view:broadcast_text")],
        [("◀️ Назад","admin_settings")],
    ])
    await safe_edit(call, text, markup)

@admin_router.callback_query(F.data == "admin_bc_set_photo")
async def admin_bc_set_photo_cb(call: CallbackQuery):
    _admin_pending[call.from_user.id] = "set_broadcast_photo"
    await safe_edit(call,
        "🖼 Пришлите фото для рассылки (можно с подписью — подпись не используется, текст берётся из «✉️ Текст рассылки»):",
        kb_back("admin_broadcast_settings"))

@admin_router.callback_query(F.data == "admin_bc_remove_photo")
async def admin_bc_remove_photo_cb(call: CallbackQuery):
    _db.set_setting("broadcast_photo_file_id", "")
    await call.answer("✅ Фото убрано")
    await admin_broadcast_settings_cb(call)

@admin_router.callback_query(F.data == "admin_bc_toggle")
async def admin_bc_toggle_cb(call: CallbackQuery):
    cur = _db.get_setting("broadcast_enabled", "0")
    _db.set_setting("broadcast_enabled", "0" if cur == "1" else "1")
    await admin_broadcast_settings_cb(call)

@admin_router.callback_query(F.data == "admin_bc_edit_hours")
async def admin_bc_edit_hours_cb(call: CallbackQuery):
    _admin_pending[call.from_user.id] = "edit_reminder_hours"
    await safe_edit(call,
        "⏰ За сколько часов до окончания подписки слать напоминание?\n\nПришлите число, например <code>24</code> или <code>12</code>:",
        kb_back("admin_broadcast_settings"))

@admin_router.callback_query(F.data == "admin_bc_edit_time")
async def admin_bc_edit_time_cb(call: CallbackQuery):
    _admin_pending[call.from_user.id] = "edit_broadcast_time"
    await safe_edit(call,
        "🕐 В какое время по Москве слать общую рассылку каждый день?\n\nФормат <code>ЧЧ:ММ</code>, например <code>10:00</code>:",
        kb_back("admin_broadcast_settings"))

# ═══════════════════════════════════════════════════════════════
# ADMIN — ТЕКСТЫ СООБЩЕНИЙ КЛИЕНТАМ (редактируемые + тест)
# ═══════════════════════════════════════════════════════════════
CLIENT_MSG_TEMPLATES = {
    "msg_reminder_24h": {
        "label": "⏰ Напоминание об окончании подписки",
        "default": ("❗<b>Подписка истекает</b>❗\nОсталось 24 часа\n\n"
                    "Оплатите тариф сейчас чтобы не упустить новые вакансии!"),
    },
    "broadcast_text": {
        "label": "📨 Общая рассылка (по расписанию)",
        "default": ("👋 <b>phase.parser</b>\n\nНапоминаем: у нас каждый день новые вакансии на монтаж видео!"),
    },
    "msg_welcome_search_started": {
        "label": "🔍 Приветствие новому клиенту («поиск запущен»)",
        "default": (
            "🔍 <b>Поиск вакансий уже запущен!</b>\n\n"
            "Ничего дополнительно нажимать не нужно — как только найдём подходящую "
            "вакансию на монтаж видео, она сразу придёт вам сюда, в этот чат.\n\n"
            "Обычно это занимает от нескольких минут до пары часов, в зависимости "
            "от того, как часто публикуют подходящие вакансии — просто ждите 🙂"
        ),
    },
}

@admin_router.callback_query(F.data == "admin_msg_texts")
async def admin_msg_texts_cb(call: CallbackQuery):
    rows = [[(v["label"], f"admin_msg_view:{k}")] for k, v in CLIENT_MSG_TEMPLATES.items()]
    rows.append([("◀️ Назад","admin_settings")])
    await safe_edit(call,
        "<b>✉️ Тексты сообщений клиентам</b>\n\n"
        "Всё, что бот шлёт клиентам автоматически (не разовые уведомления, "
        "а повторяющиеся шаблоны) — можно посмотреть, изменить и протестировать (придёт вам в личку).",
        mkb(rows))

@admin_router.callback_query(F.data.startswith("admin_msg_view:"))
async def admin_msg_view_cb(call: CallbackQuery):
    key  = call.data.split(":")[1]
    tpl  = CLIENT_MSG_TEMPLATES.get(key)
    if not tpl: await call.answer("Не найден"); return
    text = _db.get_setting(key, tpl["default"])
    await safe_edit(call,
        f"<b>{tpl['label']}</b>\n\n<blockquote>{html.escape(text)[:1000]}</blockquote>",
        mkb([
            [("✏️ Изменить", f"admin_msg_edit:{key}"), ("🧪 Тест себе", f"admin_msg_test:{key}")],
            [("◀️ Назад","admin_msg_texts")],
        ]))

@admin_router.callback_query(F.data.startswith("admin_msg_edit:"))
async def admin_msg_edit_cb(call: CallbackQuery):
    key = call.data.split(":")[1]
    _admin_pending[call.from_user.id] = f"edit_msg_text:{key}"
    await safe_edit(call,
        "✏️ Пришлите новый текст (поддерживается HTML-разметка Telegram: &lt;b&gt;, &lt;i&gt; и т.п.):",
        kb_back(f"admin_msg_view:{key}"))

@admin_router.callback_query(F.data.startswith("admin_msg_test:"))
async def admin_msg_test_cb(call: CallbackQuery):
    key = call.data.split(":")[1]
    tpl = CLIENT_MSG_TEMPLATES.get(key)
    if not tpl: await call.answer("Не найден"); return
    text   = _db.get_setting(key, tpl["default"])
    markup = mkb([[("💳 Тарифы","client_tariffs")]]) if key == "msg_reminder_24h" else None
    photo_id = _db.get_setting("broadcast_photo_file_id", "") if key == "broadcast_text" else ""
    try:
        if photo_id:
            await call.bot.send_photo(call.from_user.id, photo_id, caption=text, parse_mode=ParseMode.HTML)
        else:
            await call.bot.send_message(call.from_user.id, text, parse_mode=ParseMode.HTML, reply_markup=markup)
        await call.answer("✅ Отправлено вам в личку")
    except Exception as e:
        await call.answer(f"Ошибка: {e}", show_alert=True)

@admin_router.callback_query(F.data == "admin_notifications")
async def admin_notifications_cb(call: CallbackQuery):
    ntf_reply  = _db.get_setting("notify_reply","1") == "1"
    ntf_reject = _db.get_setting("notify_rejected","1") == "1"
    markup = mkb([
        [(f"{'✅' if ntf_reply else '❌'} Новая подходящая вакансия", "admin_settings_toggle:notify_reply")],
        [(f"{'✅' if ntf_reject else '❌'} Вакансия не прошла проверку", "admin_settings_toggle:notify_rejected")],
        [("◀️ Назад", "admin_settings")],
    ])
    await safe_edit(call,
        "<b>📣 Оповещения</b>\n\nКакие уведомления присылать вам в этот чат по каждой обработанной вакансии:",
        markup)


@admin_router.callback_query(F.data == "admin_clear_logs")
async def admin_clear_logs_cb(call: CallbackQuery):
    await safe_edit(call, "Вы уверены что хотите очистить все логи?",
        mkb([[("✅ Да, очистить","admin_clear_logs_confirm"),("❌ Нет","admin_logs")]]))

@admin_router.callback_query(F.data == "admin_clear_logs_confirm")
async def admin_clear_logs_confirm_cb(call: CallbackQuery):
    _db.clear_logs()
    await call.answer("✅ Логи очищены")
    await admin_logs_cb(call)

@admin_router.callback_query(F.data == "admin_clear_ds_rules")
async def admin_clear_ds_rules_cb(call: CallbackQuery):
    await safe_edit(call, "Удалить все глобальные правила DeepSeek?",
        mkb([[("✅ Да","admin_clear_ds_rules_confirm"),("❌ Нет","admin_settings")]]))

@admin_router.callback_query(F.data == "admin_clear_ds_rules_confirm")
async def admin_clear_ds_rules_confirm_cb(call: CallbackQuery):
    _db.delete_ds_rules()
    await call.answer("✅ Правила удалены")
    await admin_settings_cb(call)

@admin_router.callback_query(F.data.startswith("admin_settings_toggle:"))
async def admin_settings_toggle_cb(call: CallbackQuery):
    key = call.data.split(":")[1]
    _admin_pending[call.from_user.id] = f"confirm_toggle:{key}"
    await safe_edit(call, "Вы уверены?",
        mkb([[("✅ Да",f"admin_settings_confirm:{key}"),("❌ Нет","admin_settings")]]))

@admin_router.callback_query(F.data.startswith("admin_settings_confirm:"))
async def admin_settings_confirm_cb(call: CallbackQuery):
    key    = call.data.split(":")[1]
    cur    = _db.get_setting(key,"1")
    newval = "0" if cur == "1" else "1"
    _db.set_setting(key, newval)
    await call.answer("Настройка изменена")
    await admin_settings_cb(call)

# ═══════════════════════════════════════════════════════════════
# ADMIN — ОШИБКИ В ОТКЛИКЕ / УДАЛЕНИЕ
# ═══════════════════════════════════════════════════════════════

@admin_router.callback_query(F.data.startswith("admin_block_sender:"))
async def admin_block_sender_cb(call: CallbackQuery):
    vid = call.data.split(":")[1]
    v   = _db.get_vacancy(int(vid))
    if not v: await call.answer("Вакансия не найдена"); return
    sender_id    = v.get("author_id") or 0
    sender_uname = v.get("author_username") or ""
    # Показываем кто это — username или просто ID
    display = f"@{sender_uname}" if sender_uname else f"ID: <code>{sender_id}</code>"
    markup = mkb([
        [("✅ Да, заблокировать", f"admin_block_vac_confirm:{vid}"),
         ("❌ Нет",               f"admin_vac_notif_view:{vid}")],
    ])
    await safe_edit(call,
        f"🚫 <b>Заблокировать отправителя?</b>\n\n{display}",
        markup)

@admin_router.callback_query(F.data.startswith("admin_block_vac_confirm:"))
async def admin_block_vac_confirm_cb(call: CallbackQuery):
    vid = call.data.split(":")[1]
    v   = _db.get_vacancy(int(vid))
    if not v: await call.answer("Вакансия не найдена"); return
    sender_id    = v.get("author_id") or 0
    sender_uname = v.get("author_username") or ""
    display      = f"@{sender_uname}" if sender_uname else f"id:{sender_id}"
    _db.block_sender(sender_id, sender_uname, "Заблокирован администратором", None)
    log.info(f"Отправитель заблокирован: {sender_id} {display}")
    # Никаких уведомлений блокируемому не шлём — он может дальше пользоваться
    # клиент-ботом как обычно, просто его сообщения в отслеживаемых источниках
    # больше не проверяются (фильтр в _process, is_sender_blocked).
    await safe_edit(call,
        f"✅ <b>Заблокирован</b>\n\n{display}",
        kb_back(f"admin_vac_notif_view:{vid}"))

@admin_router.callback_query(F.data.startswith("admin_error_vac:"))
async def admin_error_vac_cb(call: CallbackQuery):
    vid    = call.data.split(":")[1]
    markup = mkb([
        [("🔑 Ключевые слова",f"admin_err_kw:{vid}"),
         ("🚫 Чёрный список",  f"admin_err_bl:{vid}")],
        [("🤖 ИИ",             f"admin_err_ds:{vid}")],
        [("◀️ Назад", f"admin_vac_notif_view:{vid}")],
    ])
    await safe_edit(call, "⚠️ <b>Выберите категорию ошибки:</b>", markup)

@admin_router.callback_query(F.data.startswith("admin_err_kw:"))
async def admin_err_kw_cb(call: CallbackQuery):
    _admin_pending[call.from_user.id] = "add_kw:common"
    await safe_edit(call, "🔑 Введите слово для ключевых слов:", kb_back("admin_kw"))

@admin_router.callback_query(F.data.startswith("admin_err_bl:"))
async def admin_err_bl_cb(call: CallbackQuery):
    _admin_pending[call.from_user.id] = "add_bl:common"
    await safe_edit(call, "🚫 Введите слово для чёрного списка:", kb_back("admin_bl"))

@admin_router.callback_query(F.data.startswith("admin_err_ds:"))
async def admin_err_ds_cb(call: CallbackQuery):
    _admin_pending[call.from_user.id] = "add_ds_rule"
    await safe_edit(call, "🤖 Введите правило для DeepSeek:", kb_back("admin_deepseek"))

@admin_router.callback_query(F.data.startswith("admin_manual_approve:"))
async def admin_manual_approve_cb(call: CallbackQuery):
    vid     = int(call.data.split(":")[1])
    vacancy_row = _db.get_vacancy(vid)
    if not vacancy_row: await call.answer("Не найдена"); return
    _db.update_vacancy_ds(vid, True, "Вручную", vacancy_row.get("ds_contact",""))
    # Отправляем через pipeline
    v = Vacancy(
        chat_id=vacancy_row["chat_id"], message_id=vacancy_row["message_id"],
        text=vacancy_row["text"], author_username=vacancy_row["author_username"] or "",
        author_id=vacancy_row["author_id"] or 0, source_title=vacancy_row["source_title"] or "",
        message_link=vacancy_row["message_link"] or "", timestamp=datetime.now(),
        html_text=vacancy_row.get("html_text") or "")
    ds = DeepSeekResult(suitable=True, reason="Вручную", contact=vacancy_row.get("ds_contact",""))
    await _pipeline._handle_suitable(v, ds, vid)
    await call.answer("✅ Вакансия одобрена и отправлена в рассылку")
    await safe_edit(call, call.message.html_text + "\n\n✅ <b>Одобрено вручную</b>", None)

@admin_router.callback_query(F.data == "noop")
async def noop_cb(call: CallbackQuery): await call.answer()
# ═══════════════════════════════════════════════════════════════
# ADMIN — ОБРАБОТЧИК ФОТО (только для настроек рассылки)
# ═══════════════════════════════════════════════════════════════
@admin_router.message(F.photo)
async def admin_photo_handler(msg: Message):
    uid    = msg.from_user.id
    action = _admin_pending.pop(uid, None)
    if action == "set_broadcast_photo":
        # Храним только file_id — маленькую строку-ссылку на уже загруженный
        # на серверы Telegram файл. Сам файл никогда не попадает в нашу БД —
        # место не расходуется вообще, независимо от размера/количества фото.
        file_id = msg.photo[-1].file_id
        _db.set_setting("broadcast_photo_file_id", file_id)
        await safe_answer(msg, "✅ Фото для рассылки сохранено", kb_back("admin_broadcast_settings"))
        return
    if action == "broadcast_photo":
        draft = _broadcast_draft.get(uid)
        if not draft:
            await safe_answer(msg, "Сессия истекла, начните заново", kb_back("admin_clients")); return
        draft["photo"] = msg.photo[-1].file_id
        await _show_broadcast_preview(msg.bot, uid)
        return
    await safe_answer(msg, "Не понял, что делать с этим фото", kb_back("admin_main"))
# ═══════════════════════════════════════════════════════════════
# ADMIN — ОБРАБОТЧИК ТЕКСТОВЫХ СООБЩЕНИЙ
# ═══════════════════════════════════════════════════════════════
@admin_router.message(F.text)
async def admin_text_handler(msg: Message):
    uid    = msg.from_user.id
    action = _admin_pending.pop(uid, None)
    if not action: return
    text = msg.text.strip()

    # ── Авторизация UserBot ────────────────────────────────────
    if action == "userbot_auth_phone":
        phone = text if text.startswith("+") else "+" + text
        try:
            result = await _auth_state["client"].send_code_request(phone)
            _auth_state.update({"phone": phone, "phone_code_hash": result.phone_code_hash})
            _admin_pending[uid] = "userbot_auth_code"
            await safe_answer(msg, f"📱 Код отправлен на <code>{phone}</code>\n\nВведите код:",
                              kb_back("admin_main"))
        except Exception as e:
            await safe_answer(msg, f"❌ Ошибка: <code>{e}</code>\n\nВведите номер снова:")
            _admin_pending[uid] = "userbot_auth_phone"
        return

    if action == "userbot_auth_code":
        code = text.replace(" ","")
        try:
            await _auth_state["client"].sign_in(
                phone=_auth_state["phone"], code=code,
                phone_code_hash=_auth_state["phone_code_hash"])
            await _finish_userbot_auth(msg)
        except SessionPasswordNeededError:
            _admin_pending[uid] = "userbot_auth_2fa"
            await safe_answer(msg, "🔐 Введите пароль 2FA:")
        except PhoneCodeInvalidError:
            _admin_pending[uid] = "userbot_auth_code"
            await safe_answer(msg, "❌ Неверный код. Попробуйте ещё раз:")
        except PhoneCodeExpiredError:
            _admin_pending[uid] = "userbot_auth_phone"
            await safe_answer(msg, "❌ Код устарел. Введите номер снова:")
        except Exception as e:
            await safe_answer(msg, f"❌ Ошибка: <code>{e}</code>")
        return

    if action == "userbot_auth_2fa":
        try:
            await _auth_state["client"].sign_in(password=text)
            await _finish_userbot_auth(msg)
        except PasswordHashInvalidError:
            _admin_pending[uid] = "userbot_auth_2fa"
            await safe_answer(msg, "❌ Неверный пароль. Попробуйте ещё раз:")
        except Exception as e:
            await safe_answer(msg, f"❌ Ошибка: <code>{e}</code>")
        return

    # ── Период антиспама от одного отправителя ──────────────────
    if action == "edit_sender_cooldown":
        t = text.strip()
        if not t.isdigit() or not (1 <= int(t) <= 1440):
            await safe_answer(msg, "❌ Введите число от 1 до 1440 (минут)", kb_back("admin_monitoring"))
            return
        _db.set_setting("sender_cooldown_min", t)
        await safe_answer(msg, f"✅ Теперь не чаще 1 вакансии от отправителя в {t} мин.",
                          kb_back("admin_monitoring"))
        return

    # ── Поиск клиента по ID/юзернейму ────────────────────────────
    if action == "find_client":
        q = text.strip()
        found = None
        if q.lstrip("-").isdigit():
            found = _db.get_client_by_tg(int(q))
        if not found:
            found = _db.get_client_by_username(q)
        if not found:
            await safe_answer(msg, f"❌ Клиент «{html.escape(q)}» не найден", kb_back("admin_clients"))
            return
        result = render_client_detail(found["id"])
        detail_text, markup = result
        await safe_answer(msg, detail_text, markup)
        return

    # ── Добавление источников (ссылки с новой строки) ─────────
    if action == "add_source_links":
        lines = [l.strip() for l in text.splitlines() if l.strip()]
        added = []; not_joined = []
        await safe_answer(msg, "⏳ <b>Проверка/подключение...</b>")
        for raw_orig in lines:
            raw = raw_orig.replace("https://t.me/","").replace("http://t.me/","").strip()
            try:
                # ── Приватная группа по инвайт-ссылке (+hash или joinchat/hash) ──
                if raw.startswith("+") or raw.startswith("joinchat/"):
                    invite_hash = raw[1:] if raw.startswith("+") else raw.split("joinchat/",1)[1]
                    invite_hash = invite_hash.strip("/")
                    try:
                        updates = await _userbot(ImportChatInviteRequest(invite_hash))
                        entity  = updates.chats[0]
                    except UserAlreadyParticipantError:
                        check = await _userbot(CheckChatInviteRequest(invite_hash))
                        if isinstance(check, ChatInviteAlready):
                            entity = check.chat
                        else:
                            raise
                    chat_id = entity.id
                    title   = getattr(entity, "title", raw_orig)
                    uname   = getattr(entity, "username", None)
                    link    = f"https://t.me/{uname}" if uname else raw_orig
                    _db.add_source(chat_id, title, uname, link)
                    added.append(title)
                    continue

                # ── Публичный чат/канал по username ──
                uname_raw = raw.lstrip("@").strip()
                entity  = await _userbot.get_entity(uname_raw)
                chat_id = entity.id
                title   = getattr(entity,"title",uname_raw)
                uname   = getattr(entity,"username",None)
                link    = f"https://t.me/{uname}" if uname else None
                # Проверяем подписку
                try:
                    await _userbot.get_permissions(entity, await _userbot.get_me())
                    _db.add_source(chat_id, title, uname, link)
                    added.append(title)
                except Exception:
                    not_joined.append((title, link or f"https://t.me/{uname_raw}"))
            except (InviteHashExpiredError, InviteHashInvalidError) as e:
                not_joined.append((raw_orig, raw_orig))
                log.warning(f"Инвайт-ссылка недействительна {raw_orig}: {e}")
            except ChannelsTooMuchError:
                not_joined.append((raw_orig, raw_orig))
                log.error("UserBot состоит в максимальном числе групп/каналов — Telegram не даёт вступить в новые")
            except FloodWaitError as e:
                not_joined.append((raw_orig, raw_orig))
                log.warning(f"FloodWait при добавлении {raw_orig}: подождите {e.seconds}с")
            except Exception as e:
                not_joined.append((raw_orig, raw_orig))
                log.warning(f"Добавление источника {raw_orig}: {e}")

        if not_joined:
            links_text = "\n".join(f"<a href='{lnk}'>{nm}</a>" for nm,lnk in not_joined)
            markup = mkb([
                [("⏭ Пропустить","admin_sources"),
                 ("🔄 Проверить подписки","admin_src_recheck")],
            ])
            result = ""
            if added:
                result = f"✔ Добавлено: <b>{len(added)}</b>\n\n"
            await safe_answer(msg,
                f"{result}Подпишитесь на эти источники:\n\n{links_text}", markup)
        else:
            await safe_answer(msg,
                f"✔ <b>{len(added)} источников добавлено</b>",
                kb_back("admin_sources"))
        return

    # ── Ключевые слова ────────────────────────────────────────
    if action.startswith("add_kw:"):
        ktype = action.split(":")[1]
        words = [w.strip() for w in text.splitlines() if w.strip()]
        for w in words: _db.add_keyword(w, ktype)
        await safe_answer(msg,
            f"✅ Добавлено ключевых слов: <b>{len(words)}</b>",
            mkb([[("◀️ Назад",f"admin_kw_cat:{ktype}")]]))
        return

    if action.startswith("del_kw:"):
        ktype = action.split(":")[1]
        words = [w.strip() for w in text.splitlines() if w.strip()]
        for w in words: _db.delete_keyword(w.lower(), ktype)
        await safe_answer(msg,
            f"✅ Удалено: <b>{len(words)}</b>",
            mkb([[("◀️ Назад",f"admin_kw_cat:{ktype}")]]))
        return

    # ── Чёрный список ─────────────────────────────────────────
    if action.startswith("add_bl:"):
        btype = action.split(":")[1]
        words = [w.strip() for w in text.splitlines() if w.strip()]
        for w in words: _db.add_to_blacklist(w, btype)
        await safe_answer(msg,
            f"✅ Добавлено стоп-слов: <b>{len(words)}</b>",
            mkb([[("◀️ Назад",f"admin_bl_cat:{btype}")]]))
        return

    if action.startswith("del_bl:"):
        btype = action.split(":")[1]
        words = [w.strip() for w in text.splitlines() if w.strip()]
        for w in words: _db.delete_from_blacklist(w.lower(), btype)
        await safe_answer(msg,
            f"✅ Удалено: <b>{len(words)}</b>",
            mkb([[("◀️ Назад",f"admin_bl_cat:{btype}")]]))
        return

    # ── Системный промт ───────────────────────────────────────
    if action == "edit_ds_prompt":
        _db.set_setting("ds_system_prompt", text)
        await safe_answer(msg, "✅ Промт обновлён", kb_back("admin_deepseek"))
        return

    # ── Тексты сообщений клиентам ──────────────────────────────
    if action.startswith("edit_msg_text:"):
        key = action.split(":",1)[1]
        _db.set_setting(key, text)
        await safe_answer(msg, "✅ Текст обновлён", kb_back(f"admin_msg_view:{key}"))
        return

    # ── Период напоминания об окончании подписки ───────────────
    if action == "edit_reminder_hours":
        t = text.strip()
        if not t.isdigit() or not (1 <= int(t) <= 168):
            await safe_answer(msg, "❌ Введите число от 1 до 168 (часов)", kb_back("admin_broadcast_settings"))
            return
        _db.set_setting("reminder_hours_before", t)
        await safe_answer(msg, f"✅ Теперь напоминание шлётся за {t} ч. до конца подписки",
                          kb_back("admin_broadcast_settings"))
        return

    # ── Время общей рассылки (МСК) ──────────────────────────────
    if action == "edit_broadcast_time":
        t = text.strip()
        try:
            hh, mm = t.split(":")
            hh, mm = int(hh), int(mm)
            assert 0 <= hh <= 23 and 0 <= mm <= 59
        except Exception:
            await safe_answer(msg, "❌ Формат ЧЧ:ММ, например 10:00", kb_back("admin_broadcast_settings"))
            return
        _db.set_setting("broadcast_time_msk", f"{hh:02d}:{mm:02d}")
        # Сбрасываем дедуп на сегодня — чтобы новое время могло сработать в тот же день
        _db.set_setting("broadcast_last_sent", "")
        await safe_answer(msg, f"✅ Рассылка теперь в {hh:02d}:{mm:02d} МСК",
                          kb_back("admin_broadcast_settings"))
        return

    # ── Глобальное правило DeepSeek ───────────────────────────
    if action == "add_ds_rule":
        key = f"ds_rule_{int(time.time())}"
        _db.set_setting(key, text)
        await safe_answer(msg, f"✅ Правило добавлено:\n<code>{text}</code>",
                         kb_back("admin_deepseek"))
        return

    # ── Выдача подписки по TG_ID ─────────────────────────────
    if action == "give_sub":
        parts = text.split()
        if len(parts) != 2 or not all(p.lstrip("-").isdigit() for p in parts):
            await safe_answer(msg, "❌ Формат: <code>TG_ID дней</code>")
            _admin_pending[uid] = "give_sub"; return
        tg_id = int(parts[0]); days = int(parts[1])
        cl    = _db.get_or_create_client(tg_id, None)
        until = _db.extend_subscription(cl["id"], days)
        await safe_answer(msg,
            f"✅ Подписка выдана: <code>{tg_id}</code> до <b>{fmt_date(until.isoformat())}</b>",
            kb_back("admin_clients"))
        try:
            await msg.bot.send_message(tg_id,
                f"🎁 Вам выдана подписка до <b>{fmt_date(until.isoformat())}</b>!",
                parse_mode=ParseMode.HTML, reply_markup=kb_client_main())
        except Exception: pass
        return

    # ── Выдача подписки по client_id (из карточки клиента) ──
    if action and action.startswith("give_sub_id:"):
        client_id = int(action.split(":")[1])
        days = int(text.strip()) if text.strip().isdigit() else 0
        if days <= 0:
            await safe_answer(msg, "❌ Введите количество дней (число)")
            _admin_pending[uid] = action; return
        cl    = _db.get_client_by_id(client_id)
        if not cl:
            await safe_answer(msg, "❌ Клиент не найден"); return
        until = _db.extend_subscription(client_id, days)
        await safe_answer(msg,
            f"✅ Подписка выдана до <b>{fmt_date(until.isoformat())}</b>",
            kb_back(f"admin_client_detail:{client_id}"))
        try:
            await msg.bot.send_message(cl["tg_id"],
                f"🎁 Вам выдана подписка до <b>{fmt_date(until.isoformat())}</b>!",
                parse_mode=ParseMode.HTML, reply_markup=kb_client_main())
        except Exception: pass
        return

    # Блокировка теперь через кнопки admin_block_vac_confirm/admin_block_client_confirm — ввод текста не нужен

    # ── Рассылка ──────────────────────────────────────────────
    if action == "broadcast_text":
        _broadcast_draft[uid] = {"text": text, "photo": None}
        await safe_answer(msg,
            "Прикрепить фото к рассылке?",
            mkb([
                [("📷 Да, прикрепить фото","admin_bcast_add_photo")],
                [("➡️ Без фото, дальше","admin_bcast_preview")],
            ]))
        return


async def _finish_userbot_auth(msg: Message) -> None:
    global _userbot
    client = _auth_state.get("client")
    if not client: return
    me  = await client.get_me()
    ss  = client.session.save()
    log.info(f"UserBot авторизован: @{me.username} ({me.id})")
    _db.set_setting("string_session", ss)
    _userbot = client
    if _pipeline:
        _pipeline.userbot = client
        _register_userbot(client, _pipeline)
    await safe_answer(msg,
        f"✅ <b>UserBot авторизован!</b>\n\n"
        f"Аккаунт: @{me.username} (<code>{me.id}</code>)\n\n"
        f"Добавь в .env:\n<code>STRING_SESSION={ss}</code>",
        kb_admin_main())


@admin_router.callback_query(F.data == "admin_src_recheck")
async def admin_src_recheck_cb(call: CallbackQuery):
    await call.answer("Проверяю...")
    srcs = _db.get_sources(); not_joined = []
    for s in srcs:
        try: await _userbot.get_entity(s["chat_id"])
        except Exception: not_joined.append(s)
    if not_joined:
        names  = "\n".join(f"• {s['title']}" for s in not_joined)
        markup = mkb([[("⏭ Пропустить","admin_sources"),
                       ("🔄 Проверить снова","admin_src_recheck")]])
        await safe_edit(call, f"⚠️ Не вступили в:\n\n{names}", markup)
    else:
        await safe_edit(call, "✅ Вступили во все источники", kb_back("admin_sources"))
# ═══════════════════════════════════════════════════════════════
# CLIENT BOT
# ═══════════════════════════════════════════════════════════════

def _client_main_text(cl: dict, is_new: bool = False) -> str:
    sub   = fmt_date(cl.get("sub_until"))
    bonus = "\n<b>Тебе начислено +3 бесплатных дня</b>" if is_new else ""
    # Вакансий за сегодня — только те, что прошли проверку ИИ
    today = datetime.now().strftime("%Y-%m-%d")
    vac_today = 0
    try:
        row = _db._c().execute(
            "SELECT SUM(vacancies_found) FROM stats_daily WHERE date=?", (today,)
        ).fetchone()
        vac_today = row[0] or 0
    except Exception:
        pass
    stop_words = []
    try:
        stop_words = _db.get_client_stopwords(cl["id"])
    except Exception:
        pass
    return (
        f"Вы используете <b>phase.parser</b>👔{bonus}\n\n"
        f"Вакансий за сегодня: <b>{vac_today}</b>\n"
        f"Ваши стоп слова: <b>{len(stop_words)}</b>\n\n"
        f"📅 Срок действия подписки: {sub}"
    )

@client_router.message(Command("start"))
async def client_start(msg: Message):
    uid   = msg.from_user.id
    uname = msg.from_user.username
    exists = _db.get_client_by_tg(uid)
    cl     = _db.get_or_create_client(uid, uname)
    is_new = exists is None

    if is_new:
        # Бесплатные дни
        until = _db.extend_subscription(cl["id"], FREE_DAYS)
        cl    = _db.get_client_by_tg(uid)

        # Реферал — только запоминаем, кто кого пригласил.
        # Дни начисляются позже, только когда приглашённый купит любой тариф (см. admin_confirm_pay_cb).
        args = msg.text.split() if msg.text else []
        if len(args) > 1 and args[1].startswith("ref"):
            try:
                ref_tg_id = int(args[1][3:])
                ref_cl    = _db.get_client_by_tg(ref_tg_id)
                if ref_cl and ref_cl["id"] != cl["id"]:
                    _db._c().execute("UPDATE clients SET ref_by=? WHERE id=?", (ref_cl["id"], cl["id"]))
                    _db._c().commit()
            except Exception as e: log.warning(f"Реферал: {e}")

    await msg.answer(_client_main_text(cl, is_new), reply_markup=kb_client_main())
    if is_new:
        # Отдельным сообщением, явно и заметно — часть новых пользователей не
        # понимает, что поиск вакансий уже идёт автоматически, и ждёт какого-то
        # дополнительного действия от себя, чтобы «запустить» его. Текст
        # редактируется через ⚙️ Настройки → ✉️ Тексты сообщений.
        text = _db.get_setting("msg_welcome_search_started",
                               CLIENT_MSG_TEMPLATES["msg_welcome_search_started"]["default"])
        await msg.answer(text, parse_mode=ParseMode.HTML)

@client_router.message(Command("settings"))
async def client_cmd_settings(msg: Message):
    uid = msg.from_user.id; uname = msg.from_user.username
    cl  = _db.get_or_create_client(uid, uname)
    await _show_client_settings(msg, cl)

@client_router.message(Command("pay"))
async def client_cmd_pay(msg: Message):
    uid = msg.from_user.id; uname = msg.from_user.username
    cl  = _db.get_or_create_client(uid, uname)
    await msg.answer(await _tariffs_text(cl), reply_markup=_tariffs_kb(cl), parse_mode=ParseMode.HTML)

# ── Профиль ────────────────────────────────────────────────────
@client_router.callback_query(F.data == "client_profile")
async def client_profile_cb(call: CallbackQuery):
    uid = call.from_user.id; uname = call.from_user.username
    cl  = _db.get_or_create_client(uid, uname)
    await client_referral_cb(call)

@client_router.callback_query(F.data == "client_referral")
async def client_referral_cb(call: CallbackQuery):
    uid = call.from_user.id; uname = call.from_user.username
    cl  = _db.get_or_create_client(uid, uname)
    bot_me   = await call.bot.get_me()
    ref_link = f"https://t.me/{bot_me.username}?start=ref{uid}"
    invited  = _db._c().execute("SELECT COUNT(*) FROM clients WHERE ref_by=?", (cl["id"],)).fetchone()[0]
    text = (
        f"Реферальная программа <b>phase.parser</b>\n\n"
        f"🔗 <b>Ваша ссылка:</b>\n"
        f"<code>{ref_link}</code>\n\n"
        f"<b>Вам +{REF_BONUS_DAYS} дней</b>, другу <b>+{REF_DAYS} дней</b>\n"
        f"Дни начисляются, когда приглашённый оплатит первый любой тариф\n\n"
        f"Приглашено: <b>{invited}</b>"
    )
    markup = mkb([
        [("👥 Пригласить друга", f"client_invite_friend:{uid}")],
        [("◀️ Главное меню","client_main")],
    ])
    await safe_edit(call, text, markup)

@client_router.callback_query(F.data.startswith("client_invite_friend:"))
async def client_invite_friend_cb(call: CallbackQuery):
    ref_uid = call.data.split(":")[1]
    # Кнопка switch_inline_query открывает системный выбор чата (как «Переслать»),
    # а после выбора чата бот вставит готовую карточку через инлайн-режим —
    # ровно то же поведение, что и у кнопки «Пригласить друзей» в других ботах.
    share_btn = InlineKeyboardButton(text="📤 Поделиться карточкой",
                                     switch_inline_query=f"ref{ref_uid}")
    back_btn  = InlineKeyboardButton(text="◀️ Назад", callback_data="client_referral")
    markup = InlineKeyboardMarkup(inline_keyboard=[[share_btn], [back_btn]])
    ref_link = f"https://t.me/{(await call.bot.get_me()).username}?start=ref{ref_uid}"
    await safe_edit(call,
        f"Нажмите кнопку ниже, выберите чат или друга — карточка с приглашением "
        f"отправится автоматически.\n\n"
        f"Или перешлите ссылку вручную:\n<code>{ref_link}</code>",
        markup)

# ── Инлайн-режим: карточка приглашения (нужно включить /setinline у @BotFather) ──
@client_router.inline_query(F.query.startswith("ref"))
async def client_invite_inline_query(iq: InlineQuery):
    ref_uid = iq.query[len("ref"):].strip()
    if not ref_uid.isdigit():
        await iq.answer([], cache_time=1); return
    bot_me   = await iq.bot.get_me()
    ref_link = f"https://t.me/{bot_me.username}?start=ref{ref_uid}"
    text = (
        f"Привет. Получай клиентов с помощью <b>phase.parser</b>\n\n"
        f"{ref_link}\n\n"
        f"20+ вакансий для монтажа в день. Работает быстро и стабильно."
    )
    result = InlineQueryResultArticle(
        id=f"invite_{ref_uid}",
        title="Пригласить в phase.parser",
        description="Отправить карточку с приглашением",
        thumbnail_url="https://telegram.org/img/t_logo.png",
        input_message_content=InputTextMessageContent(
            message_text=text, parse_mode=ParseMode.HTML, disable_web_page_preview=False),
        reply_markup=mkb([[("🤝 Подключиться", ref_link)]]),
    )
    await iq.answer([result], cache_time=30, is_personal=True)

# ── Тарифы ─────────────────────────────────────────────────────
async def _tariffs_text(cl: dict) -> str:
    sub = fmt_date(cl.get("sub_until"))
    has_paid = bool(cl.get("first_payment"))
    sale_line = "" if has_paid else "\n<b>Скидка на первую оплату 15%</b>🔥"
    return f"Подписка активна до: {sub}{sale_line}"

def _tariffs_kb(cl: dict) -> InlineKeyboardMarkup:
    has_paid = bool(cl.get("first_payment"))
    rows = []
    for key, p in PRICES.items():
        price = p["full"] if has_paid else p["sale"]
        fire  = "" if has_paid else "🔥"
        rows.append([(f"{price}₽{fire} за {p['label']}", f"client_buy:{key}")])
    rows.append([("◀️ Главное меню","client_main")])
    return mkb(rows)

@client_router.callback_query(F.data == "client_tariffs")
async def client_tariffs_cb(call: CallbackQuery):
    uid = call.from_user.id; uname = call.from_user.username
    cl  = _db.get_or_create_client(uid, uname)
    await safe_edit(call, await _tariffs_text(cl), _tariffs_kb(cl))

@client_router.callback_query(F.data.startswith("client_buy:"))
async def client_buy_cb(call: CallbackQuery):
    uid     = call.from_user.id
    tariff  = call.data.split(":")[1]
    p       = PRICES.get(tariff)
    if not p: await call.answer("Неверный тариф"); return
    cl      = _db.get_or_create_client(uid, call.from_user.username)
    has_paid = bool(cl.get("first_payment"))
    amount  = p["full"] if has_paid else p["sale"]
    ticket  = f"DRAFT-{int(time.time()*1000)}"
    _payment_drafts[ticket] = {"client_id": cl["id"], "tariff": tariff, "amount": amount,
                              "days": p["days"], "method": "rub"}
    log.info(f"Черновик оплаты создан: {ticket} (клиент {uid}, тариф {tariff}) — в БД пока не пишем, "
             f"пока клиент не нажмёт «Оплатил(а)»")
    fire    = "" if has_paid else "🔥"
    uname_hint = f"@{call.from_user.username}" if call.from_user.username else f"<code>{uid}</code>"
    text    = (
        f"Тариф <b>{p['label']}</b>\n\n"
        f"<code>{PAYMENT_PHONE}</code> | {PAYMENT_NAME}\n"
        f"{PAYMENT_BANK}\n"
        f"(укажите в комментарии {uname_hint})\n\n"
        f"К оплате: <b>{amount}₽</b>{fire}\n"
        f"⚠️ <b>После оплаты нажмите Оплатил(а)</b> ⚠️"
    )
    markup = mkb([
        [("✅ Оплатил(а)", f"client_paid:{ticket}")],
        [("💱 Оплатить в USDT", f"client_buy_crypto:{tariff}")],
        [("◀️ Главное меню","client_main")],
    ])
    await safe_edit(call, text, markup)

@client_router.callback_query(F.data.startswith("client_buy_crypto:"))
async def client_buy_crypto_cb(call: CallbackQuery):
    uid    = call.from_user.id
    tariff = call.data.split(":")[1]
    p      = PRICES.get(tariff)
    if not p: await call.answer("Неверный тариф"); return
    if not CRYPTO_WALLET:
        await call.answer("Оплата в USDT временно недоступна", show_alert=True); return
    cl       = _db.get_or_create_client(uid, call.from_user.username)
    has_paid = bool(cl.get("first_payment"))
    amount   = p["full"] if has_paid else p["sale"]
    rate     = await get_usdt_rub_rate()
    if not rate:
        await call.answer("Не удалось получить курс, попробуйте ещё раз через минуту", show_alert=True)
        return
    usdt_amount = round(amount / rate, 2)
    ticket = f"DRAFT-{int(time.time()*1000)}"
    _payment_drafts[ticket] = {"client_id": cl["id"], "tariff": tariff, "amount": amount,
                              "days": p["days"], "method": "usdt",
                              "crypto_amount": usdt_amount, "crypto_rate": rate}
    log.info(f"Черновик оплаты создан: {ticket} (клиент {uid}, тариф {tariff}, USDT) — в БД пока не пишем")
    fire = "" if has_paid else "🔥"
    text = (
        f"Тариф <b>{p['label']}</b>\n\n"
        f"Сеть: <b>{CRYPTO_NETWORK}</b>\n"
        f"Адрес: <code>{CRYPTO_WALLET}</code>\n\n"
        f"К оплате: <b>{usdt_amount} USDT</b>{fire}\n"
        f"<i>(≈ {amount}₽ по курсу {rate}₽ за USDT)</i>\n\n"
        f"⚠️ <b>Отправьте точную сумму, после оплаты нажмите Оплатил(а)</b> ⚠️\n"
        f"<i>Курс актуален короткое время — если не успели, вернитесь на этот экран заново, чтобы обновить сумму</i>"
    )
    markup = mkb([
        [("✅ Оплатил(а)", f"client_paid:{ticket}")],
        [("◀️ Главное меню","client_main")],
    ])
    await safe_edit(call, text, markup)

@client_router.callback_query(F.data.startswith("client_paid:"))
async def client_paid_cb(call: CallbackQuery):
    draft_ticket = call.data.split(":", 1)[1]
    draft = _payment_drafts.pop(draft_ticket, None)
    if not draft:
        await call.answer("Сессия истекла, выберите тариф заново", show_alert=True)
        return
    # Запись в БД (и, соответственно, индикатор 🔴 у админа) появляется
    # ИМЕННО ТЕПЕРЬ — по факту подтверждения клиентом, а не при выборе тарифа
    ticket = _db.create_payment(draft["client_id"], draft["tariff"], draft["amount"], draft["days"],
                                method=draft["method"], crypto_amount=draft.get("crypto_amount"),
                                crypto_rate=draft.get("crypto_rate"))
    p = _db.get_payment_by_ticket(ticket)
    text = (
        f"⏳ <b>Проверка оплаты...</b>\n\n"
        f"Оплата проверяется вручную\n"
        f"Время проверки до 24 часов, обычно намного быстрее\n\n"
        f"Тикет: <code>{ticket}</code>"
    )
    markup = mkb([
        [(f"💬 Поддержка", f"https://t.me/{SUPPORT_USERNAME}")],
        [("◀️ Главное меню","client_main")],
    ])
    await safe_edit(call, text, markup)
    # Уведомление админу — отправляется ИСКЛЮЧИТЕЛЬНО здесь, по нажатию «Оплатил(а)»
    log.info(f"Клиент нажал «Оплатил(а)», создан тикет {ticket} — уведомляю админа")
    if p.get("method") == "usdt":
        pay_line = (f"Тариф: {p['tariff']} | Сумма: <b>{p['crypto_amount']} USDT</b> "
                    f"(курс {p['crypto_rate']}₽, ≈{p['amount']}₽)\n"
                    f"Сеть: {CRYPTO_NETWORK} | Кошелёк: <code>{CRYPTO_WALLET}</code>")
    else:
        pay_line = f"Тариф: {p['tariff']} | Сумма: <b>{p['amount']}₽</b>"
    try:
        await call.bot.send_message(ADMIN_ID,
            f"💳 <b>Новый платёж!</b>\n\n"
            f"Тикет: <code>{ticket}</code>\n"
            f"Клиент: @{call.from_user.username or call.from_user.id}\n"
            f"{pay_line}",
            parse_mode=ParseMode.HTML,
            reply_markup=mkb([
                [("✅ Подтвердить", f"admin_confirm_pay:{ticket}"),
                 ("❌ Отклонить",   f"admin_reject_pay:{ticket}")],
            ]))
    except Exception as e: log.error(f"Уведомление об оплате: {e}")

# ── Подтверждение оплаты администратором ───────────────────────
@admin_router.callback_query(F.data.startswith("admin_confirm_pay:"))
async def admin_confirm_pay_cb(call: CallbackQuery):
    # Отвечаем ПЕРВЫМ ДЕЛОМ — иначе Telegram видит таймаут (3 сек)
    await call.answer("⏳ Обрабатываю...")
    ticket = call.data.split(":", 1)[1]
    log.info(f"Подтверждение оплаты: {ticket}")
    try:
        p = _db.get_payment_by_ticket(ticket)
        if not p:
            log.warning(f"Тикет не найден: {ticket}")
            await call.message.answer("❌ Тикет не найден")
            return
        cl = _db.get_client_by_id(p["client_id"])
        if not cl:
            log.warning(f"Клиент не найден: {ticket}")
            await call.message.answer("❌ Клиент не найден")
            return
        log.info(f"Платёж: status={p['status']} days={p['days']} client={cl['tg_id']}")
        already = p.get("status") == "confirmed"
        if not already:
            was_first_payment = not bool(cl.get("first_payment"))
            _db.confirm_payment(ticket)
            until = _db.extend_subscription(cl["id"], p["days"])
            _db._c().execute("UPDATE clients SET first_payment=1 WHERE id=?", (cl["id"],))
            _db._c().commit()
            _db.stat_inc("subs_bought")
            log.info(f"Оплата OK: {ticket} клиент={cl['tg_id']} до={until}")

            # ── Реферальные бонусы: только за ПЕРВУЮ покупку приглашённого ──
            if was_first_payment and cl.get("ref_by"):
                try:
                    ref_cl = _db.get_client_by_id(cl["ref_by"])
                    if ref_cl:
                        _db.extend_subscription(cl["id"], REF_DAYS)          # купившему рефералу
                        ref_until = _db.extend_subscription(ref_cl["id"], REF_BONUS_DAYS)  # пригласившему
                        until = _db.get_client_by_id(cl["id"])["sub_until"]  # обновили выше — берём актуальную дату
                        try:
                            await call.bot.send_message(cl["tg_id"],
                                f"🎁 Вам начислено <b>+{REF_DAYS} дн.</b> к подписке — бонус за переход по реферальной ссылке!",
                                parse_mode=ParseMode.HTML)
                        except Exception as e: log.warning(f"Уведомление рефералу: {e}")
                        try:
                            await call.bot.send_message(ref_cl["tg_id"],
                                f"🎉 Ваш реферал купил подписку!\n"
                                f"Вам начислено <b>+{REF_BONUS_DAYS} дн.</b> к подписке (до {fmt_date(ref_until.isoformat() if hasattr(ref_until,'isoformat') else str(ref_until))}).",
                                parse_mode=ParseMode.HTML)
                        except Exception as e: log.warning(f"Уведомление пригласившему: {e}")
                except Exception as e:
                    log.error(f"Реферальный бонус: {e}")
                    await notify_admin_error(call.bot, "Реферальный бонус", e)
        else:
            raw   = cl.get("sub_until") or datetime.now().isoformat()
            until = datetime.fromisoformat(raw) if isinstance(raw, str) else raw
            log.info(f"Уже подтверждён: {ticket}")
        until_iso = until.isoformat() if hasattr(until, "isoformat") else str(until)
        until_fmt = fmt_date(until_iso)
        text = (
            f"✅ <b>Оплата подтверждена</b>\n\n"
            f"Тикет: <code>{ticket}</code>\n"
            f"Клиент: <code>{cl['tg_id']}</code> @{cl.get('username') or '—'}\n"
            f"Тариф: <b>{p['tariff']}</b> ({p['days']} дн.)\n"
            f"Подписка до: <b>{until_fmt}</b>"
        )
        try:
            await call.message.edit_text(
                text, parse_mode=ParseMode.HTML,
                reply_markup=mkb([[("◀️ К клиентам", "admin_clients")]]))
        except Exception as edit_err:
            log.warning(f"edit_text: {edit_err}")
            await call.message.answer(
                text, parse_mode=ParseMode.HTML,
                reply_markup=mkb([[("◀️ К клиентам", "admin_clients")]]))
        if not already:
            try:
                await call.bot.send_message(
                    cl["tg_id"],
                    f"✅ <b>Оплата подтверждена!</b>\n\n"
                    f"Подписка активна до: <b>{until_fmt}</b>",
                    parse_mode=ParseMode.HTML,
                    reply_markup=kb_client_main())
                log.info(f"Клиент {cl['tg_id']} уведомлён")
            except Exception as e:
                log.error(f"Уведомление клиента {cl['tg_id']}: {e}")
    except Exception as e:
        log.error(f"admin_confirm_pay_cb: {e}", exc_info=True)
        try:
            await call.message.answer(f"❌ Ошибка: <code>{e}</code>", parse_mode=ParseMode.HTML)
        except Exception:
            pass

# ── Настройки клиента ──────────────────────────────────────────
async def _show_client_settings(msg_or_call, cl: dict) -> None:
    search_on = bool(cl.get("search_active", 1))
    icon      = "🟢" if search_on else "🔴"
    text      = f"Настройки <b>phase.parser</b>\n"
    markup    = mkb([
        [("🚫 Стоп-слова","client_stopwords")],
        [(f"🔍 Поиск: {'Активен' if search_on else 'Выключен'}{icon}", "client_toggle_search")],
        [("◀️ Главное меню","client_main")],
    ])
    if isinstance(msg_or_call, CallbackQuery):
        await safe_edit(msg_or_call, text, markup)
    else:
        await safe_answer(msg_or_call, text, markup)

@client_router.callback_query(F.data == "client_settings")
async def client_settings_cb(call: CallbackQuery):
    cl = _db.get_or_create_client(call.from_user.id, call.from_user.username)
    await _show_client_settings(call, cl)

@client_router.callback_query(F.data == "client_toggle_search")
async def client_toggle_search_cb(call: CallbackQuery):
    cl       = _db.get_or_create_client(call.from_user.id, call.from_user.username)
    is_on    = bool(cl.get("search_active", 1))
    action   = "отключить" if is_on else "включить"
    freeze   = "\n❄️<b>Подписка будет заморожена</b>❄️" if is_on else ""
    markup   = mkb([
        [("✅ Да",f"client_toggle_search_confirm:{1 if not is_on else 0}"),
         ("❌ Нет","client_settings")],
    ])
    await safe_edit(call, f"Вы уверены что хотите {action} поиск?{freeze}", markup)

@client_router.callback_query(F.data.startswith("client_toggle_search_confirm:"))
async def client_toggle_search_confirm_cb(call: CallbackQuery):
    val = int(call.data.split(":")[1])
    cl  = _db.get_or_create_client(call.from_user.id, call.from_user.username)
    _db._c().execute("UPDATE clients SET search_active=? WHERE id=?", (val, cl["id"]))
    _db._c().commit()
    cl  = _db.get_client_by_tg(call.from_user.id)
    await _show_client_settings(call, cl)

# ── Стоп-слова клиента ─────────────────────────────────────────
@client_router.callback_query(F.data == "client_stopwords")
async def client_stopwords_cb(call: CallbackQuery):
    cl    = _db.get_or_create_client(call.from_user.id, call.from_user.username)
    words = _db.get_client_stopwords(cl["id"])
    text  = (
        f"<b>🚫 Стоп-слова</b>\n\n"
        f"Вакансии с этими словами не будут вам приходить.\n\n"
        f"<b>Текущие ({len(words)}):</b>\n" +
        ("\n".join(f"<code>{w}</code>" for w in words) if words else "<i>нет</i>")
    )
    markup = mkb([
        [("➕ Добавить","client_stopwords_add"), ("➖ Удалить","client_stopwords_del")],
        [("◀️ Главное меню","client_main")],
    ])
    await safe_edit(call, text, markup)

@client_router.callback_query(F.data == "client_stopwords_add")
async def client_stopwords_add_cb(call: CallbackQuery):
    _client_pending[call.from_user.id] = "add_stopwords"
    await safe_edit(call,
        "➕ <b>Добавить стоп-слова</b>\n\nВведите слова через Enter (каждое с новой строки):\n\n"
        "<i>Пример:\nТестовое\nШтат\nБез оплаты</i>",
        kb_back("client_stopwords"))

@client_router.callback_query(F.data == "client_stopwords_del")
async def client_stopwords_del_cb(call: CallbackQuery):
    cl    = _db.get_or_create_client(call.from_user.id, call.from_user.username)
    words = _db.get_client_stopwords(cl["id"])
    if not words:
        await call.answer("Список пуст"); return
    _client_pending[call.from_user.id] = "del_stopwords"
    await safe_edit(call,
        "➖ <b>Удалить стоп-слова</b>\n\nВведите слова через Enter — как написаны в списке (можно скопировать оттуда):\n\n" +
        "\n".join(f"<code>{w}</code>" for w in words),
        kb_back("client_stopwords"))

# ── Главное меню клиента ───────────────────────────────────────
@client_router.callback_query(F.data == "client_main")
async def client_main_cb(call: CallbackQuery):
    uid = call.from_user.id; uname = call.from_user.username
    cl  = _db.get_or_create_client(uid, uname)
    await safe_edit(call, _client_main_text(cl), kb_client_main())

# ── Текстовый обработчик клиента ──────────────────────────────
@client_router.message(F.text)
async def client_text_handler(msg: Message):
    uid    = msg.from_user.id
    uname  = msg.from_user.username
    action = _client_pending.pop(uid, None)
    cl     = _db.get_or_create_client(uid, uname)

    if action == "add_stopwords":
        words = [w.strip() for w in msg.text.splitlines() if w.strip()]
        _db.add_client_stopwords(cl["id"], words)
        await safe_answer(msg,
            f"✅ Стоп-слова добавлены: <b>{len(words)}</b>",
            mkb([[("🚫 К списку","client_stopwords")]]))
        return

    if action == "del_stopwords":
        words = [w.strip() for w in msg.text.splitlines() if w.strip()]
        _db.delete_client_stopwords(cl["id"], words)
        await safe_answer(msg,
            f"✅ Стоп-слова удалены: <b>{len(words)}</b>",
            mkb([[("🚫 К списку","client_stopwords")]]))
        return

    await msg.answer(_client_main_text(cl), reply_markup=kb_client_main())
# ═══════════════════════════════════════════════════════════════
# USERBOT
# ═══════════════════════════════════════════════════════════════
def _register_userbot(userbot: TelegramClient, pipeline: VacancyPipeline) -> None:
    @userbot.on(events.NewMessage())
    async def _handler(event):
        _last_event_at["ts"] = time.time()  # фиксируем ДО любой фильтрации —
        # нужно знать, что MTProto-сессия вообще получает апдейты от Telegram,
        # даже если это сообщение потом отфильтруется по источнику/настройкам
        try:
            if _db.get_setting("monitoring_active","1") != "1": return
            srcs = _db.get_sources(active_only=True)
            ids  = {s["chat_id"] for s in srcs}
            cid  = event.chat_id
            if cid not in ids and abs(cid) not in ids: return
            await pipeline.enqueue(event)
        except Exception as e:
            err_str = str(e)
            # TypeNotFoundError — Telethon устарел, игнорируем конкретное сообщение
            if "TypeNotFoundError" in type(e).__name__ or "Constructor ID" in err_str:
                log.warning(f"UserBot: неизвестный тип TL-объекта (обновите Telethon): {err_str[:100]}")
                return
            log.error(f"UserBot: {e}", exc_info=True)
            await notify_admin_error(pipeline.bot, "UserBot handler", e)
    log.info("UserBot: обработчик зарегистрирован")

async def _init_userbot(bot: Bot) -> TelegramClient:
    ss      = STRING_SESSION or _db.get_setting("string_session","")
    session = StringSession(ss) if ss else StringSession()
    client  = TelegramClient(session, API_ID, API_HASH)
    _auth_state["client"] = client
    await client.connect()
    if await client.is_user_authorized():
        me = await client.get_me()
        log.info(f"UserBot: @{me.username} ({me.id})")
        _db.set_setting("string_session", client.session.save())
        return client
    log.warning("UserBot не авторизован")
    try:
        await bot.send_message(ADMIN_ID,
            "⚠️ <b>UserBot не авторизован</b>\n\n"
            "Введите номер телефона: <code>+79001234567</code>",
            parse_mode=ParseMode.HTML)
        _admin_pending[ADMIN_ID] = "userbot_auth_phone"
    except Exception as e: log.error(f"Уведомление авторизации: {e}")
    return client

# ═══════════════════════════════════════════════════════════════
# ПРОВЕРКА ЗДОРОВЬЯ USERBOT-СЕССИИ
# ═══════════════════════════════════════════════════════════════
_userbot_health = {"was_ok": True}

async def _check_userbot_health(bot: Bot) -> None:
    """Раз в 15 минут проверяет, что юзербот-сессия жива. Если аккаунт
    разлогинило/забанило — единственный видимый симптом обычно «вакансии просто
    перестали приходить», и это можно не заметить сутками. Уведомляем сразу,
    как только сессия отваливается, и отдельно — когда снова оживает."""
    while True:
        await asyncio.sleep(900)
        try:
            ok = False
            if _userbot is not None:
                try:
                    ok = _userbot.is_connected() and await _userbot.is_user_authorized()
                except Exception:
                    ok = False
            was_ok = _userbot_health["was_ok"]
            if not ok and was_ok:
                _userbot_health["was_ok"] = False
                log.error("UserBot health check: сессия недоступна")
                try:
                    await bot.send_message(ADMIN_ID,
                        "🔴 <b>UserBot отключён или разлогинен!</b>\n\n"
                        "Мониторинг источников остановлен — новые вакансии не приходят.\n"
                        "Зайдите в бот и переавторизуйте юзербота заново (номер телефона).",
                        parse_mode=ParseMode.HTML)
                except Exception: pass
            elif ok and not was_ok:
                _userbot_health["was_ok"] = True
                log.info("UserBot health check: сессия восстановлена")
                try:
                    await bot.send_message(ADMIN_ID,
                        "🟢 <b>UserBot снова в сети</b> — мониторинг восстановлен.",
                        parse_mode=ParseMode.HTML)
                except Exception: pass
        except Exception as e:
            log.error(f"_check_userbot_health: {e}")

# ═══════════════════════════════════════════════════════════════
# ПРОВЕРКА ПОТОКА СООБЩЕНИЙ (не только «жив ли коннект», а «идут ли апдейты»)
# ═══════════════════════════════════════════════════════════════
_last_event_at: dict = {"ts": time.time()}
_message_flow_health = {"was_ok": True, "notified": False}

async def _check_message_flow(bot: Bot) -> None:
    """_check_userbot_health проверяет только is_connected()/is_user_authorized() —
    оба могут быть True, даже когда MTProto-сессия «зависла» и реально перестала
    получать апдейты от Telegram (известная особенность долгоживущих Telethon-сессий:
    сокет формально жив, а обновления не идут). Эта проверка ловит именно такой
    случай — по факту прихода событий, а не по статусу объекта клиента, и сама
    пытается переподключить ТОТ ЖЕ САМЫЙ юзербот (disconnect+connect), без создания
    второго аккаунта — обычно это чинит зависшую сессию за секунды.
    При обычном объёме источников (сообщения каждые несколько секунд) пауза
    дольше STALL_MINUTES без единого апдейта — почти наверняка сбой, а не
    настоящее затишье."""
    STALL_MINUTES = 15
    while True:
        await asyncio.sleep(300)
        try:
            if _db.get_setting("monitoring_active","1") != "1": continue
            if not _db.get_sources(active_only=True): continue
            silent_for = time.time() - _last_event_at["ts"]
            ok = silent_for < STALL_MINUTES * 60

            if ok:
                if not _message_flow_health["was_ok"]:
                    _message_flow_health["was_ok"]  = True
                    _message_flow_health["notified"] = False
                    log.info("Поток сообщений восстановился")
                    try:
                        await bot.send_message(ADMIN_ID,
                            "🟢 <b>Сообщения снова поступают</b> — мониторинг восстановлен.",
                            parse_mode=ParseMode.HTML)
                    except Exception: pass
                continue

            # Тишина дольше STALL_MINUTES — пробуем переподключить юзербота.
            # Делаем это на КАЖДОМ цикле проверки, пока не восстановится (не
            # только один раз при первом обнаружении) — если первая попытка
            # не помогла, вторая через 5 минут вполне может.
            _message_flow_health["was_ok"] = False
            minutes = int(silent_for // 60)
            log.error(f"Поток сообщений остановился: {minutes} мин. — пробую переподключить UserBot")
            reconnect_ok = False
            if _userbot is not None:
                try:
                    await _userbot.disconnect()
                    await asyncio.sleep(2)
                    await _userbot.connect()
                    reconnect_ok = await _userbot.is_user_authorized()
                    if reconnect_ok:
                        log.info("UserBot переподключен автоматически")
                except Exception as e:
                    log.error(f"Переподключение UserBot не удалось: {e}")

            # Уведомляем только один раз за весь простой — иначе спам каждые 5 минут
            if not _message_flow_health["notified"]:
                _message_flow_health["notified"] = True
                try:
                    if reconnect_ok:
                        await bot.send_message(ADMIN_ID,
                            f"🔄 <b>UserBot завис на {minutes} мин. — переподключил автоматически.</b>\n\n"
                            f"Если сообщения не появятся в течение 15-20 минут — буду пробовать ещё, "
                            f"но возможно аккаунт ограничен/забанен Telegram, тогда переподключение "
                            f"не поможет, нужно проверить аккаунт вручную.",
                            parse_mode=ParseMode.HTML)
                    else:
                        await bot.send_message(ADMIN_ID,
                            f"🔴 <b>UserBot не получает сообщения уже {minutes} мин., "
                            f"автопереподключение не удалось!</b>\n\n"
                            f"Похоже на бан/ограничение аккаунта в Telegram, а не на обычное "
                            f"зависание сессии. Буду пробовать переподключить каждые 5 минут, "
                            f"но стоит проверить аккаунт вручную (переавторизация в боте либо "
                            f"в официальном приложении Telegram).",
                            parse_mode=ParseMode.HTML)
                except Exception: pass
        except Exception as e:
            log.error(f"_check_message_flow: {e}")

# ═══════════════════════════════════════════════════════════════
# УВЕДОМЛЕНИЕ О КОНЦЕ ПОДПИСКИ (раз в неделю после окончания)
# ═══════════════════════════════════════════════════════════════
async def _check_expired_subs(bot: Bot) -> None:
    while True:
        await asyncio.sleep(3600)
        try:
            now  = datetime.now()
            week = (now - timedelta(days=7)).isoformat()
            rows = _db._c().execute(
                "SELECT * FROM clients WHERE sub_until IS NOT NULL AND sub_until < ? AND sub_until > ?",
                (now.isoformat(), week)).fetchall()
            for row in rows:
                cl = dict(row)
                # Проверяем что не отправляли сегодня
                sent_key = f"expired_notif_{cl['id']}"
                last = _db.get_setting(sent_key,"")
                if last and last == now.strftime("%Y-%m-%d"): continue
                try:
                    await bot.send_message(cl["tg_id"],
                        "Дарим скидку <b>15%</b> на все тарифы!\n"
                        "Подключитесь чтобы получать новые заказы",
                        parse_mode=ParseMode.HTML,
                        reply_markup=_tariffs_kb(cl))
                    _db.set_setting(sent_key, now.strftime("%Y-%m-%d"))
                except Exception: pass
        except Exception as e: log.error(f"_check_expired_subs: {e}")

async def _send_expiry_reminders(bot: Bot) -> None:
    """Напоминание «подписка истекает» — раз в 30 минут проверяем, но
    каждому клиенту шлём РОВНО ОДИН раз за текущий период подписки (иначе,
    если вакансий за день много, клиента бы заваливало дублями напоминания —
    была именно эта жалоба). За сколько часов до окончания слать — настраивается
    (⚙️ Настройки → 📨 Рассылки)."""
    while True:
        await asyncio.sleep(1800)
        try:
            hours_before = int(_db.get_setting("reminder_hours_before", "24") or "24")
            now  = datetime.now()
            soon = (now + timedelta(hours=hours_before)).isoformat()
            rows = _db._c().execute(
                "SELECT * FROM clients WHERE sub_until IS NOT NULL AND sub_until > ? AND sub_until <= ?",
                (now.isoformat(), soon)).fetchall()
            for row in rows:
                cl = dict(row)
                # Ключ дедупа привязан к конкретной дате окончания подписки —
                # как только клиент продлит её, sub_until изменится и напоминание придёт снова
                sent_key = f"reminder_sub_{cl['id']}_{cl['sub_until']}"
                if _db.get_setting(sent_key, ""): continue
                try:
                    text = _db.get_setting("msg_reminder_24h", CLIENT_MSG_TEMPLATES["msg_reminder_24h"]["default"])
                    await bot.send_message(cl["tg_id"], text, parse_mode=ParseMode.HTML,
                                           reply_markup=mkb([[("💳 Тарифы","client_tariffs")]]))
                    _db.set_setting(sent_key, "1")
                except Exception: pass
        except Exception as e: log.error(f"_send_expiry_reminders: {e}")

async def _scheduled_broadcast(bot: Bot) -> None:
    """Общая рассылка по расписанию — раз в день в заданное московское время
    (⚙️ Настройки → 📨 Рассылки). Проверяем раз в минуту, шлём один раз в
    сутки (дедуп по дате МСК)."""
    while True:
        await asyncio.sleep(60)
        try:
            if _db.get_setting("broadcast_enabled", "0") != "1": continue
            now_msk    = datetime.now(MSK)
            today_key  = now_msk.strftime("%Y-%m-%d")
            if _db.get_setting("broadcast_last_sent", "") == today_key: continue
            target_hm  = _db.get_setting("broadcast_time_msk", "10:00")
            try:
                th, tm = map(int, target_hm.split(":"))
            except Exception:
                th, tm = 10, 0
            # Окно в минуту — проверка раз в 60с, так что точного совпадения достаточно
            if not (now_msk.hour == th and now_msk.minute == tm): continue
            text = _db.get_setting("broadcast_text", CLIENT_MSG_TEMPLATES["broadcast_text"]["default"])
            photo_id = _db.get_setting("broadcast_photo_file_id", "")
            clients = _db.get_active_clients()
            sent_count = 0
            for cl in clients:
                try:
                    if photo_id:
                        await bot.send_photo(cl["tg_id"], photo_id, caption=text, parse_mode=ParseMode.HTML)
                    else:
                        await bot.send_message(cl["tg_id"], text, parse_mode=ParseMode.HTML)
                    sent_count += 1
                except Exception: pass
            _db.set_setting("broadcast_last_sent", today_key)
            log.info(f"📨 Плановая рассылка отправлена: {sent_count} клиентам")
            _db.add_log("INFO", f"📨 Плановая рассылка: {sent_count} клиентам")
        except Exception as e:
            log.error(f"_scheduled_broadcast: {e}")
            await notify_admin_error(bot, "_scheduled_broadcast", e)

# ═══════════════════════════════════════════════════════════════
# АВТО-ОЧИСТКА
# ═══════════════════════════════════════════════════════════════
async def _periodic_cleanup() -> None:
    while True:
        await asyncio.sleep(3600)
        try: _db.cleanup()
        except Exception as e: log.error(f"cleanup: {e}")

# ═══════════════════════════════════════════════════════════════
# УСТАНОВКА КОМАНД БОТА
# ═══════════════════════════════════════════════════════════════
async def _set_bot_commands(bot: Bot) -> None:
    # Команды для администратора
    await bot.set_my_commands(
        [BotCommand(command="start", description="Главная")],
        scope=BotCommandScopeChat(chat_id=ADMIN_ID))
    # Команды для остальных
    from aiogram.types import BotCommandScopeDefault
    await bot.set_my_commands([
        BotCommand(command="start",    description="Главная"),
        BotCommand(command="settings", description="Настройки"),
        BotCommand(command="pay",      description="Тарифы"),
    ], scope=BotCommandScopeDefault())
    log.info("Команды бота установлены")

# ═══════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════
async def main() -> None:
    global _db, _pipeline, _userbot

    log.info("=" * 60)
    log.info("phase.parser запускается")
    log.info("=" * 60)

    # БД
    _db = Database(DB_PATH); _db.connect(); _db.init_tables()

    # Bot
    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp  = Dispatcher()
    dp.include_router(admin_router)   # Обрабатывает только ADMIN_ID (фильтр на уровне роутера)
    dp.include_router(client_router)  # Все остальные

    # UserBot
    _userbot  = await _init_userbot(bot)

    # Pipeline
    _pipeline = VacancyPipeline(_db, _userbot, bot)

    if await _userbot.is_user_authorized():
        _register_userbot(_userbot, _pipeline)

    # Проверка DS
    ds_status = await check_deepseek_status()
    ds_ok = ds_status == "ok"
    if not ds_ok:
        log.warning(f"DeepSeek не готов: {ds_status}")
    else:
        log.info("DeepSeek: ✅")

    # Команды бота
    try: await _set_bot_commands(bot)
    except Exception as e: log.warning(f"set_commands: {e}")

    # Стартовое сообщение
    try:
        ub_ok = await _userbot.is_user_authorized()
        await bot.send_message(ADMIN_ID,
            f"🚀 <b>phase.parser запущен</b>\n\n"
            f"UserBot:  {'✅' if ub_ok else '❌ требуется авторизация'}\n"
            f"DeepSeek: {'✅' if ds_ok else f'❌ {ds_status}'}\n\n"
            f"/start — открыть панель управления")
    except Exception as e: log.warning(f"Стартовое сообщение: {e}")

    log.info("Запуск задач...")
    async def _safe_userbot_run():
        """Перезапускает userbot при TypeNotFoundError вместо падения.
        Также штатно переживает намеренные disconnect()/connect() от
        _check_message_flow (авто-переподключение зависшей сессии) — раньше
        такой принудительный disconnect() заставлял run_until_disconnected()
        завершиться «нормально», после чего этот цикл выходил насовсем (break)
        и переставал следить за клиентом вообще. Теперь после любого
        завершения run_until_disconnected() просто проверяем: если клиент
        всё ещё должен работать (снова подключён кем-то другим или мониторинг
        включён) — ждём и заходим в него заново, а не завершаем задачу."""
        while True:
            try:
                await _userbot.run_until_disconnected()
                if _db.get_setting("monitoring_active","1") != "1":
                    break  # мониторинг реально выключен — можно выйти
                await asyncio.sleep(3)
                continue  # клиента, скорее всего, переподключили — ждём заново
            except Exception as e:
                if "TypeNotFoundError" in type(e).__name__ or "Constructor ID" in str(e):
                    log.warning(
                        "UserBot: TypeNotFoundError — Telegram обновил протокол.\n"
                        "Обновите Telethon: pip install telethon==1.44.0\n"
                        f"Детали: {str(e)[:200]}"
                    )
                    try:
                        await bot.send_message(
                            ADMIN_ID,
                            "⚠️ <b>UserBot: ошибка протокола</b>\n\n"
                            "Telegram обновил протокол, Telethon устарел.\n"
                            "Обновите: <code>pip install telethon==1.44.0</code>\n"
                            "затем перезапустите бот.",
                            parse_mode="HTML")
                    except Exception:
                        pass
                    await asyncio.sleep(30)  # пауза перед переподключением
                else:
                    raise

    await asyncio.gather(
        dp.start_polling(bot, allowed_updates=["message","callback_query","inline_query"]),
        _pipeline.run_worker(),
        _periodic_cleanup(),
        _check_expired_subs(bot),
        _send_expiry_reminders(bot),
        _scheduled_broadcast(bot),
        _check_userbot_health(bot),
        _check_message_flow(bot),
        _safe_userbot_run(),
    )

if __name__ == "__main__":
    try: asyncio.run(main())
    except KeyboardInterrupt: log.info("phase.parser остановлен")
    except Exception as e: log.critical(f"Критическая ошибка: {e}", exc_info=True)
