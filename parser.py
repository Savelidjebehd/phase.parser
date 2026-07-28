"""
phase.parser — агрегатор публичных вакансий из Telegram-групп
Telethon (UserBot) + Aiogram 3.x (Bot) + SQLite + DeepSeek API
"""
from __future__ import annotations
import asyncio, io, json, logging, os, random, re, sqlite3, time
from dataclasses import dataclass
from datetime import datetime, timedelta
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
)
from dotenv import load_dotenv
from telethon import TelegramClient, events
from telethon.errors import (
    PhoneCodeExpiredError, PhoneCodeInvalidError,
    PasswordHashInvalidError, SessionPasswordNeededError,
)
from telethon.sessions import StringSession
from telethon.tl.functions.channels import JoinChannelRequest
from telethon.tl.types import MessageMediaDocument, MessageMediaPhoto, MessageMediaWebPage

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
PAYMENT_BANK     = os.getenv("PAYMENT_BANK", "Озон банк")
FREE_DAYS        = int(os.getenv("FREE_DAYS", "3"))
REF_DAYS         = int(os.getenv("REF_DAYS", "3"))

# Цены: до первой оплаты (скидка) / после
PRICES = {
    "week":   {"label": "1 нед.",  "days": 7,   "sale": 169,  "full": 199},
    "month":  {"label": "1 мес.",  "days": 30,  "sale": 424,  "full": 499},
    "3month": {"label": "3 мес.",  "days": 90,  "sale": 1019, "full": 1119},
}

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

# ── Dataclasses ───────────────────────────────────────────────
@dataclass
class Vacancy:
    chat_id: int; message_id: int; text: str; author_username: str
    author_id: int; source_title: str; message_link: str; timestamp: datetime

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
            CREATE TABLE IF NOT EXISTS stats_daily (
                date TEXT PRIMARY KEY, vacancies_found INTEGER DEFAULT 0,
                vacancies_failed INTEGER DEFAULT 0, replies_sent INTEGER DEFAULT 0,
                ai_errors INTEGER DEFAULT 0, subs_bought INTEGER DEFAULT 0);
        """)
        self._c().commit()
        self._c().execute("INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)", (
            "ds_system_prompt",
            "Ты фильтр вакансий для видеомонтажёра/дизайнера. "
            "Определи является ли текст вакансией/заказом для исполнителя. "
            "ВАЖНО: найди контакт для связи — это @username или телефон после слов 'писать', 'пишите', 'контакт', 'обращаться'. "
            "Это НЕ контакт: ссылки на портфолио, референсы, примеры работ. "
            "Если контакт не найден — верни username автора. "
            "Верни JSON без markdown: {\"suitable\": bool, \"reason\": \"до 5 слов\", \"contact\": \"@username или пусто\"}"))
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

    # ── Шаблоны ───────────────────────────────────────────────
    def get_templates(self, active_only: bool = False) -> list[dict]:
        q = "SELECT * FROM templates" + (" WHERE active=1" if active_only else "") + " ORDER BY id"
        return [dict(r) for r in self._c().execute(q).fetchall()]

    def get_template(self, tid: int) -> Optional[dict]:
        row = self._c().execute("SELECT * FROM templates WHERE id=?", (tid,)).fetchone()
        return dict(row) if row else None

    def get_active_template(self) -> Optional[dict]:
        row = self._c().execute("SELECT * FROM templates WHERE active=1 ORDER BY id LIMIT 1").fetchone()
        return dict(row) if row else None

    def create_empty_template(self) -> int:
        name = f"Шаблон #{self._c().execute('SELECT COUNT(*)+1 FROM templates').fetchone()[0]}"
        cur = self._c().execute(
            "INSERT INTO templates(name,variant1,variant2,variant3) VALUES(?,?,?,?)", (name,"","",""))
        self._c().commit(); return cur.lastrowid or 0

    def update_template_variant(self, tid: int, vnum: int, text: str) -> None:
        self._c().execute(f"UPDATE templates SET variant{vnum}=? WHERE id=?", (text, tid))
        self._c().commit()

    def toggle_template(self, tid: int) -> None:
        self._c().execute("UPDATE templates SET active=1-active WHERE id=?", (tid,)); self._c().commit()

    def delete_template(self, tid: int) -> None:
        self._c().execute("DELETE FROM templates WHERE id=?", (tid,)); self._c().commit()

    def set_active_template(self, tid: int) -> None:
        self._c().execute("UPDATE templates SET active=0")
        self._c().execute("UPDATE templates SET active=1 WHERE id=?", (tid,)); self._c().commit()

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
                "author_id,source_title,message_link,ts) VALUES(?,?,?,?,?,?,?,?)",
                (v.chat_id, v.message_id, v.text, v.author_username, v.author_id,
                 v.source_title, v.message_link, v.timestamp.isoformat()))
            self._c().commit()
            if cur.lastrowid: return cur.lastrowid
            row = self._c().execute("SELECT id FROM vacancies WHERE chat_id=? AND message_id=?",
                                    (v.chat_id, v.message_id)).fetchone()
            return row["id"] if row else None
        except Exception as e:
            log.error(f"save_vacancy: {e}"); return None

    def is_duplicate(self, text: str) -> bool:
        return bool(self._c().execute("SELECT id FROM vacancies WHERE text=?", (text,)).fetchone())

    def update_vacancy_ds(self, vid: int, suitable: bool, reason: str,
                          contact: str, contact_id: Optional[int] = None) -> None:
        self._c().execute(
            "UPDATE vacancies SET suitable=?,ds_reason=?,ds_contact=?,ds_contact_id=? WHERE id=?",
            (1 if suitable else 0, reason, contact, contact_id, vid)); self._c().commit()

    def get_vacancy(self, vid: int) -> Optional[dict]:
        row = self._c().execute("SELECT * FROM vacancies WHERE id=?", (vid,)).fetchone()
        return dict(row) if row else None

    # ── Отклики ───────────────────────────────────────────────
    def save_reply(self, vacancy_id: int, template_id: int, variant_num: int,
                   text_sent: str, tg_msg_id: Optional[int] = None) -> int:
        cur = self._c().execute(
            "INSERT INTO replies(vacancy_id,template_id,variant_num,text_sent,tg_message_id) VALUES(?,?,?,?,?)",
            (vacancy_id, template_id, variant_num, text_sent, tg_msg_id))
        self._c().commit(); return cur.lastrowid or 0

    def mark_reply_deleted(self, reply_id: int) -> None:
        self._c().execute("UPDATE replies SET deleted=1 WHERE id=?", (reply_id,)); self._c().commit()

    def get_reply(self, reply_id: int) -> Optional[dict]:
        row = self._c().execute(
            "SELECT r.*,v.message_link,v.source_title,v.ds_contact,v.ds_contact_id,"
            "v.text as vacancy_text,v.author_id,v.author_username "
            "FROM replies r JOIN vacancies v ON r.vacancy_id=v.id WHERE r.id=?",
            (reply_id,)).fetchone()
        return dict(row) if row else None

    def get_replies(self, limit: int = 8, offset: int = 0) -> list[dict]:
        return [dict(r) for r in self._c().execute(
            "SELECT r.*,v.message_link,v.source_title,v.ds_contact,v.text as vacancy_text "
            "FROM replies r JOIN vacancies v ON r.vacancy_id=v.id "
            "ORDER BY r.id DESC LIMIT ? OFFSET ?", (limit, offset)).fetchall()]

    def count_replies(self) -> int:
        return self._c().execute("SELECT COUNT(*) FROM replies").fetchone()[0]

    def save_delivery(self, vacancy_id: int, client_id: int, msg_id: Optional[int],
                      skipped: bool = False, reason: str = "") -> None:
        self._c().execute(
            "INSERT OR IGNORE INTO client_deliveries(vacancy_id,client_id,msg_id,skipped,skip_reason)"
            " VALUES(?,?,?,?,?)", (vacancy_id, client_id, msg_id, 1 if skipped else 0, reason))
        self._c().commit()

    # ── Платежи ───────────────────────────────────────────────
    def create_payment(self, client_id: int, tariff: str, amount: int, days: int) -> str:
        ticket = f"PAY-{int(time.time())}"
        self._c().execute(
            "INSERT INTO payments(client_id,tariff,amount,days,ticket) VALUES(?,?,?,?,?)",
            (client_id, tariff, amount, days, ticket)); self._c().commit(); return ticket

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
        cutoff = (datetime.now() - timedelta(days=2)).isoformat()
        self._c().execute("DELETE FROM logs WHERE ts<?", (cutoff,))
        self._c().execute("DELETE FROM vacancies WHERE suitable=0 AND created_at<?", (cutoff,))
        self._c().commit(); log.info("Авто-очистка выполнена")
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
              'Верни JSON без markdown: {"suitable": bool, "reason": "макс 5 слов", "contact": "@username или пусто"}')
    payload = {
        "model": DEEPSEEK_MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": f"Автор: @{author_username}\n\nТекст:\n{text[:3000]}"},
        ],
        "max_tokens": 150,
        "temperature": 0.0,
        "response_format": {"type": "json_object"},
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
        contact  = str(parsed.get("contact", "")).strip()
        if not contact or contact.lower() in ("null", "none", ""):
            contact = f"@{author_username}" if author_username else ""
        log.info(f"DeepSeek → suitable={suitable} reason='{reason}' contact='{contact}'")
        db.add_log("INFO", f"DS: suitable={suitable} reason={reason}")
        return DeepSeekResult(suitable=suitable, reason=reason, contact=contact)
    except json.JSONDecodeError as e:
        log.error(f"DeepSeek JSON ошибка: {e}")
        db.stat_inc("ai_errors"); return _DS_FAIL
    except Exception as e:
        log.error(f"DeepSeek ошибка: {e}", exc_info=True)
        db.stat_inc("ai_errors"); return _DS_FAIL

async def check_deepseek_status() -> str:
    """
    Возвращает: 'ok', 'no_key', 'wrong_model', 'error:<msg>'
    """
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
def extract_text(message) -> str:
    caption = message.text or message.message or ""
    if message.media is None: return caption
    if isinstance(message.media, MessageMediaPhoto): marker = "[image]"
    elif isinstance(message.media, MessageMediaDocument):
        mime = getattr(getattr(message.media,"document",None),"mime_type","") or ""
        marker = "[pdf]" if "pdf" in mime else "[voice]" if "audio" in mime or "ogg" in mime else "[file]"
    elif isinstance(message.media, MessageMediaWebPage): return caption
    else: marker = "[file]"
    return f"{marker}\n\nПодпись:\n{caption}" if caption else marker

def hide_contact(text: str, contact: str) -> str:
    if not contact: return text
    result = re.sub(re.escape(contact), "—", text, flags=re.IGNORECASE)
    if contact.startswith("@"):
        result = re.sub(r"@" + re.escape(contact[1:]), "—", result, flags=re.IGNORECASE)
    return result

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
        [("♨️ Источники","admin_sources"), ("🗣 Отклики","admin_replies")],
        [("📈 Статистика","admin_stats"),   ("🖥️ Мониторинг","admin_monitoring")],
        [(client_label,"admin_clients"),   ("📜 Логи","admin_logs")],
        [("⚙️ Настройки","admin_settings")],
    ])

def kb_client_main() -> InlineKeyboardMarkup:
    return mkb([
        [("👤 Профиль","client_profile"), ("💳 Тарифы","client_tariffs")],
        [("⚙️ Настройки","client_settings")],
    ])

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
            except Exception as e: log.error(f"Воркер: {e}", exc_info=True)

    async def _process(self, event) -> None:
        try:
            if self.db.get_setting("monitoring_active","1") != "1": return
            msg    = event.message
            chat   = await event.get_chat()
            sender = await event.get_sender()
            chat_id      = event.chat_id
            message_id   = msg.id
            source_title = getattr(chat,"title",None) or str(chat_id)
            username     = getattr(sender,"username",None) or ""
            sender_id    = getattr(sender,"id",0) or 0
            message_link = make_msg_link(event, chat)
            text         = extract_text(msg)
            if not text.strip(): return

            log.info(f"📥 [{source_title}] @{username}")
            self.db.add_log("INFO", f"Получено: {message_link} @{username}")

            # Ключевые слова
            kw_common = self.db.get_keywords("common")
            text_low  = text.lower()
            found_kw  = [kw for kw in kw_common if kw in text_low]
            if not found_kw: return
            log.info(f"✅ КС: {found_kw}"); self.db.add_log("INFO", f"КС: {found_kw}")

            # Дубликат
            if self.db.is_duplicate(text):
                log.info("🔁 Дубликат"); return

            # Чёрный список
            found_bl = [w for w in self.db.get_blacklist("common") if w in text_low]
            if found_bl:
                log.info(f"⛔ ЧС: {found_bl}"); return

            vacancy = Vacancy(chat_id=chat_id, message_id=message_id, text=text,
                              author_username=username, author_id=sender_id,
                              source_title=source_title, message_link=message_link,
                              timestamp=datetime.now())
            vid = self.db.save_vacancy(vacancy)
            if not vid: return
            self.db.stat_inc("vacancies_found")

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

            await self._handle_suitable(vacancy, ds, vid)
        except Exception as e:
            log.error(f"_process: {e}", exc_info=True)

    async def _handle_suitable(self, vacancy: Vacancy, ds: DeepSeekResult, vid: int) -> None:
        tmpl = self.db.get_active_template()
        if not tmpl:
            await self._notify_admin_no_template(vacancy)
            await self._broadcast(vacancy, ds, vid); return

        variants = [v for v in [tmpl.get("variant1",""), tmpl.get("variant2",""), tmpl.get("variant3","")] if v.strip()]
        if not variants:
            await self._notify_admin_no_template(vacancy)
            await self._broadcast(vacancy, ds, vid); return

        variant_num = random.randint(1, len(variants))
        reply_text  = variants[variant_num - 1]

        delay = random.randint(5, 15)
        log.info(f"⏳ {delay}с до отклика"); await asyncio.sleep(delay)

        sent_msg_id = None
        if ds.contact and ds.contact.startswith("@"):
            try:
                sent = await self.userbot.send_message(ds.contact, reply_text)
                sent_msg_id = sent.id
                log.info(f"📤 Отклик → {ds.contact}")
                self.db.add_log("INFO", f"Отклик: {ds.contact}")
                self.db.stat_inc("replies_sent")
            except Exception as e:
                log.error(f"Отклик: {e}"); self.db.add_log("ERROR", f"Отклик: {e}")

        reply_id = self.db.save_reply(vid, tmpl["id"], variant_num, reply_text, sent_msg_id)
        await self._notify_admin_reply(vacancy, ds, tmpl, variant_num, reply_id)
        await self._broadcast(vacancy, ds, vid)

    async def _broadcast(self, vacancy: Vacancy, ds: DeepSeekResult, vid: int) -> None:
        if self.db.get_setting("client_bot_active","1") != "1": return
        clients = self.db.get_active_clients()
        log.info(f"📢 Рассылка {len(clients)} клиентам")
        for cl in clients:
            try:
                cl_id = cl["id"]
                text_low = vacancy.text.lower()
                stop_words = self.db.get_client_stopwords(cl_id)
                hit = [w for w in stop_words if w in text_low]
                if hit:
                    self.db.save_delivery(vid, cl_id, None, skipped=True, reason=f"sw:{hit[0]}"); continue

                hidden   = hide_contact(vacancy.text, ds.contact)
                msg_text = (f"<b>Новая вакансия</b>\n<i>{vacancy.source_title}</i>\n\n"
                            f"{hidden}\n\n"
                            f"<i>🕐 {vacancy.timestamp.strftime('%d.%m.%Y %H:%M')}</i>")
                markup = mkb([[("👁 Показать контакты", f"show_contact:{vid}:{cl_id}")]])
                sent   = await self.bot.send_message(cl["tg_id"], msg_text,
                                                     parse_mode=ParseMode.HTML, reply_markup=markup)
                self.db.save_delivery(vid, cl_id, sent.message_id)

                # Проверка истечения подписки — уведомить за 24ч
                sub_until = cl.get("sub_until","")
                if sub_until:
                    remaining = datetime.fromisoformat(sub_until) - datetime.now()
                    if timedelta(hours=0) < remaining <= timedelta(hours=24):
                        await self.bot.send_message(
                            cl["tg_id"],
                            "❗<b>Подписка истекает</b>❗\nОсталось 24 часа\n\n"
                            "Оплатите тариф сейчас чтобы не упустить новые вакансии!",
                            parse_mode=ParseMode.HTML,
                            reply_markup=mkb([[("💳 Тарифы","client_tariffs")]]))
            except Exception as e:
                log.error(f"Рассылка {cl.get('tg_id')}: {e}")

    async def _notify_admin_reply(self, vacancy: Vacancy, ds: DeepSeekResult,
                                   tmpl: dict, variant_num: int, reply_id: int) -> None:
        num   = f"#{tmpl['id']}.{variant_num}"
        short = vacancy.text[:600]
        # Определяем contact_id для ссылки
        contact = ds.contact
        if contact.startswith("@"):
            client_link = f"https://t.me/{contact.lstrip('@')}"
        else:
            client_link = f"tg://user?id={vacancy.author_id}"

        text = (
            f"✅ <b>Новый отклик! #{num}</b>\n\n"
            f"<blockquote expandable>{short}</blockquote>\n\n"
            f"Отклик: <code>{num}</code>\n"
            f"<a href='{vacancy.message_link}'>Сообщение</a> | "
            f"<a href='{client_link}'>Клиент</a>"
        )
        markup = mkb([
            [("⚠️ Ошибка", f"admin_error:{reply_id}"),
             ("🗑 Удалить", f"admin_del_reply:{reply_id}")],
        ])
        try:
            await self.bot.send_message(ADMIN_ID, text, parse_mode=ParseMode.HTML, reply_markup=markup)
        except Exception as e: log.error(f"notify_reply: {e}")

    async def _notify_admin_rejected(self, vacancy: Vacancy, ds: DeepSeekResult, vid: int) -> None:
        contact = ds.contact
        client_link = f"https://t.me/{contact.lstrip('@')}" if contact.startswith("@") else f"tg://user?id={vacancy.author_id}"
        short = vacancy.text[:600]
        text = (
            f"❌ <b>Не прошло проверку!</b>\n\n"
            f"<blockquote expandable>{short}</blockquote>\n\n"
            f"<b>Причина:</b> {ds.reason}\n\n"
            f"<a href='{vacancy.message_link}'>Сообщение</a> | "
            f"<a href='{client_link}'>Клиент</a>"
        )
        markup = mkb([
            [("✅ Проверен", f"admin_manual_approve:{vid}"),
             ("⚠️ Ошибка",  f"admin_error_vac:{vid}")],
        ])
        try:
            await self.bot.send_message(ADMIN_ID, text, parse_mode=ParseMode.HTML, reply_markup=markup)
        except Exception as e: log.error(f"notify_rejected: {e}")

    async def _notify_admin_no_template(self, vacancy: Vacancy) -> None:
        try:
            await self.bot.send_message(ADMIN_ID,
                f"⚠️ <b>Нет активного шаблона!</b>\n<a href='{vacancy.message_link}'>К вакансии</a>",
                parse_mode=ParseMode.HTML)
        except Exception as e: log.error(f"notify_no_tmpl: {e}")
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
_tmpl_wizard: dict[int, dict] = {}  # uid -> {tid, step}

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
        [("➕ Добавить","admin_src_add"), ("📋 Все источники","admin_src_list")],
        [("◀️ Главное меню","admin_main")],
    ])
    await safe_edit(call, text, markup)

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
# ADMIN — ОТКЛИКИ / ШАБЛОНЫ
# ═══════════════════════════════════════════════════════════════
@admin_router.callback_query(F.data == "admin_replies")
async def admin_replies_cb(call: CallbackQuery):
    templates = _db.get_templates()
    active    = _db.get_active_template()
    act_id    = active["id"] if active else None
    rows      = []
    for t in templates:
        star = "★ " if t["id"] == act_id else ""
        rows.append([(f"{star}#{t['id']} {t['name']}", f"admin_tmpl:{t['id']}")])
    rows.append([("➕ Добавить","admin_tmpl_add_start")])
    rows.append([("◀️ Главное меню","admin_main")])
    act_name = f"#{active['id']} {active['name']}" if active else "нет"
    await safe_edit(call,
        f"<b>🗣 Отклики</b>\n\nАктивный шаблон: <b>{act_name}</b>",
        mkb(rows))

@admin_router.callback_query(F.data.startswith("admin_tmpl:"))
async def admin_tmpl_cb(call: CallbackQuery):
    tid = int(call.data.split(":")[1]); t = _db.get_template(tid)
    if not t: await call.answer("Не найден"); return
    await _show_tmpl_variant(call, t, 1)

async def _show_tmpl_variant(call: CallbackQuery, t: dict, vnum: int) -> None:
    tid = t["id"]
    text_v = t.get(f"variant{vnum}","") or "<i>пусто</i>"
    text = f"<b>#{tid} {t['name']}</b>\n\n<b>Вариант {vnum}</b>\n{text_v}"
    # Кнопки для переключения вариантов
    var_row = []
    for v in [1,2,3]:
        lbl = f"[{v}]" if v == vnum else str(v)
        var_row.append((lbl, f"admin_tmpl_var:{tid}:{v}"))
    markup = mkb([
        var_row,
        [("👁 Предпросмотр",f"admin_tmpl_preview:{tid}:{vnum}"),
         ("✏️ Изменить",    f"admin_tmpl_edit_menu:{tid}")],
        [("🗑 Удалить шаблон",f"admin_tmpl_delete:{tid}")],
        [("◀️ Назад","admin_replies")],
    ])
    await safe_edit(call, text, markup)

@admin_router.callback_query(F.data.startswith("admin_tmpl_var:"))
async def admin_tmpl_var_cb(call: CallbackQuery):
    _, tid, vnum = call.data.split(":"); t = _db.get_template(int(tid))
    if not t: return
    await _show_tmpl_variant(call, t, int(vnum))

@admin_router.callback_query(F.data.startswith("admin_tmpl_preview:"))
async def admin_tmpl_preview_cb(call: CallbackQuery):
    _, tid, vnum = call.data.split(":"); t = _db.get_template(int(tid))
    if not t: return
    text_v = t.get(f"variant{vnum}","") or "Вариант пуст"
    await call.message.answer(text_v, parse_mode=ParseMode.HTML,
                              reply_markup=mkb([[("◀️ Назад","admin_replies")]]))
    await call.answer()

@admin_router.callback_query(F.data.startswith("admin_tmpl_edit_menu:"))
async def admin_tmpl_edit_menu_cb(call: CallbackQuery):
    tid = call.data.split(":")[1]
    markup = mkb([
        [("1","admin_tmpl_edit:"+tid+":1"),
         ("2","admin_tmpl_edit:"+tid+":2"),
         ("3","admin_tmpl_edit:"+tid+":3")],
        [("◀️ Назад",f"admin_tmpl:{tid}")],
    ])
    await safe_edit(call, "Какой вариант вы хотите изменить?", markup)

@admin_router.callback_query(F.data.startswith("admin_tmpl_edit:"))
async def admin_tmpl_edit_cb(call: CallbackQuery):
    _, tid, vnum = call.data.split(":")
    _admin_pending[call.from_user.id] = f"edit_variant:{tid}:{vnum}"
    await safe_edit(call, f"✏️ Пришлите текст отклика для <b>Варианта {vnum}</b>:",
                    kb_back(f"admin_tmpl:{tid}"))

@admin_router.callback_query(F.data.startswith("admin_tmpl_delete:"))
async def admin_tmpl_delete_cb(call: CallbackQuery):
    tid = int(call.data.split(":")[1]); _db.delete_template(tid)
    await call.answer(f"Шаблон #{tid} удалён"); await admin_replies_cb(call)

# Wizard добавления шаблона (вариант 1 → 2 → 3)
@admin_router.callback_query(F.data == "admin_tmpl_add_start")
async def admin_tmpl_add_start_cb(call: CallbackQuery):
    tid = _db.create_empty_template()
    _tmpl_wizard[call.from_user.id] = {"tid": tid, "step": 1}
    _admin_pending[call.from_user.id] = "tmpl_wizard"
    await safe_edit(call,
        f"<b>Добавьте отклик для Шаблона #{tid}</b>\n\n<b>Вариант 1</b>\n\nПришлите текст:",
        mkb([[("◀️ Назад","admin_replies")]]))

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
    text = (
        f"<b>📈 Статистика — {labels.get(period,'?')}</b>\n\n"
        f"Найдено вакансий: <b>{s['vacancies_found']}</b>\n"
        f"Не прошло проверку: <b>{s['vacancies_failed']}</b>\n"
        f"Написано откликов: <b>{s['replies_sent']}</b>\n"
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
    icon   = "🟢 Активен" if mon_on else "🔴 Остановлен"
    text   = f"<b>🖥️ Мониторинг</b>\n\nСтатус: {icon}\nИсточники отслеживаются: <b>{srcs}</b>"
    markup = mkb([
        [("🔑 Ключевые слова","admin_kw"), ("🚫 Чёрный список","admin_bl")],
        [("🤖 ИИ","admin_deepseek")],
        [("◀️ Главное меню","admin_main")],
    ])
    await safe_edit(call, text, markup)

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
    return f"<b>🔑 Ключевые слова — {'Общий' if category=='common' else 'Мои'}</b>\n\nКол-во: <b>{cnt}</b>", markup

@admin_router.callback_query(F.data == "admin_kw")
async def admin_kw_cb(call: CallbackQuery):
    markup = mkb([
        [("Общий","admin_kw_cat:common"), ("Только себе","admin_kw_cat:admin")],
        [("◀️ Назад","admin_monitoring")],
    ])
    await safe_edit(call, "<b>🔑 Ключевые слова</b>\n\nКуда хотите добавить ключевые слова?", markup)

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
        f"<b>➕ Ключевые слова — {'Общий' if cat=='common' else 'Мои'}</b>\n\n"
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
        f"<b>➖ Удалить ключевые слова — {'Общий' if cat=='common' else 'Мои'}</b>\n\n"
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
        f"Расход токенов в день: ↑<code>{tok['tokens_in']}</code> ↓<code>{tok['tokens_out']}</code>\n"
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
    rows.append([("📋 Все клиенты", "admin_clients_list")])
    rows.append([("➕ Выдать подписку", "admin_give_sub")])
    rows.append([("📤 Рассылка", "admin_broadcast")])
    rows.append([("◀️ Главное меню", "admin_main")])
    await safe_edit(call, "\n".join(lines), mkb(rows))


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
                    f"❌ Платёж <code>{ticket}</code> отклонён.\n"
                    f"Если вы уже оплатили — обратитесь в поддержку.",
                    parse_mode=ParseMode.HTML,
                    reply_markup=mkb([[("💬 Поддержка", f"https://t.me/{SUPPORT_USERNAME}")]]),
                )
            except Exception:
                pass
    await call.answer("Отклонено")
    await safe_edit(call, f"❌ Платёж <code>{ticket}</code> отклонён", kb_back("admin_clients"))


@admin_router.callback_query(F.data == "admin_clients_list")
async def admin_clients_list_cb(call: CallbackQuery):
    clients = _db.get_all_clients()
    now     = datetime.now().isoformat()
    lines   = []
    for c in clients[:30]:
        icon  = "💎" if (c.get("sub_until") or "") > now else "👤"
        until = fmt_date(c.get("sub_until"))
        uname = c.get("username") or "—"
        lines.append(f"{icon} <code>{c['tg_id']}</code> @{uname} до {until}")
    text = f"<b>📋 Клиенты ({len(clients)})</b>\n\n" + ("\n".join(lines) if lines else "<i>Нет</i>")
    await safe_edit(call, text, kb_back("admin_clients"))


@admin_router.callback_query(F.data == "admin_give_sub")
async def admin_give_sub_cb(call: CallbackQuery):
    _admin_pending[call.from_user.id] = "give_sub"
    await safe_edit(call,
        "➕ <b>Выдача подписки</b>\n\nВведите: <code>TG_ID количество_дней</code>\n"
        "Пример: <code>123456789 30</code>",
        kb_back("admin_clients"))


@admin_router.callback_query(F.data == "admin_broadcast")
async def admin_broadcast_cb(call: CallbackQuery):
    _admin_pending[call.from_user.id] = "broadcast"
    await safe_edit(call,
        "📤 <b>Рассылка всем активным клиентам</b>\n\nВведите текст:",
        kb_back("admin_clients"))


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
        [("◀️ Главное меню","admin_main")],
    ])
    await safe_edit(call, text, markup)

@admin_router.callback_query(F.data.startswith("admin_logs_export:"))
async def admin_logs_export_cb(call: CallbackQuery):
    date = call.data.split(":")[1]
    data = _db.export_logs(date)
    await call.message.answer_document(
        BufferedInputFile(data, filename=f"logs_{date}.txt"),
        caption=f"📜 Логи за {date}")
    await call.answer()

# ═══════════════════════════════════════════════════════════════
# ADMIN — НАСТРОЙКИ
# ═══════════════════════════════════════════════════════════════
@admin_router.callback_query(F.data == "admin_settings")
async def admin_settings_cb(call: CallbackQuery):
    ai_on  = _db.get_setting("ai_active","1") == "1"
    mon_on = _db.get_setting("monitoring_active","1") == "1"
    cb_on  = _db.get_setting("client_bot_active","1") == "1"
    markup = mkb([
        [(f"🤖 ИИ {'включено' if ai_on else 'выключено'}", "admin_settings_toggle:ai_active")],
        [(f"🖥️ Мониторинг {'включен' if mon_on else 'выключен'}", "admin_settings_toggle:monitoring_active")],
        [(f"👥 Клиент бот {'включен' if cb_on else 'выключен'}", "admin_settings_toggle:client_bot_active")],
        [("◀️ Главное меню","admin_main")],
    ])
    await safe_edit(call, "<b>⚙️ Настройки</b>", markup)

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
@admin_router.callback_query(F.data.startswith("admin_error:"))
async def admin_error_reply_cb(call: CallbackQuery):
    reply_id = call.data.split(":")[1]
    markup   = mkb([
        [("🔑 Ключевые слова",f"admin_err_kw:{reply_id}"),
         ("🚫 Чёрный список",  f"admin_err_bl:{reply_id}")],
        [("🤖 ИИ",             f"admin_err_ds:{reply_id}")],
    ])
    await safe_edit(call, "⚠️ <b>Выберите категорию ошибки:</b>", markup)

@admin_router.callback_query(F.data.startswith("admin_error_vac:"))
async def admin_error_vac_cb(call: CallbackQuery):
    vid    = call.data.split(":")[1]
    markup = mkb([
        [("🔑 Ключевые слова",f"admin_err_kw:{vid}"),
         ("🚫 Чёрный список",  f"admin_err_bl:{vid}")],
        [("🤖 ИИ",             f"admin_err_ds:{vid}")],
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

@admin_router.callback_query(F.data.startswith("admin_del_reply:"))
async def admin_del_reply_cb(call: CallbackQuery):
    reply_id = int(call.data.split(":")[1]); reply = _db.get_reply(reply_id)
    if not reply: await call.answer("Не найден"); return
    num = f"{reply['template_id']}.{reply['variant_num']}"
    if reply.get("tg_message_id") and reply.get("ds_contact"):
        try: await _userbot.delete_messages(reply["ds_contact"], [reply["tg_message_id"]])
        except Exception as e: log.warning(f"Удаление: {e}")
    _db.mark_reply_deleted(reply_id)
    await safe_edit(call, f"🗑 <b>Удалено! #<code>{num}</code></b>", None)

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
        message_link=vacancy_row["message_link"] or "", timestamp=datetime.now())
    ds = DeepSeekResult(suitable=True, reason="Вручную", contact=vacancy_row.get("ds_contact",""))
    await _pipeline._handle_suitable(v, ds, vid)
    await call.answer("✅ Вакансия одобрена и отправлена в рассылку")
    await safe_edit(call, call.message.html_text + "\n\n✅ <b>Одобрено вручную</b>", None)

@admin_router.callback_query(F.data == "noop")
async def noop_cb(call: CallbackQuery): await call.answer()
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

    # ── Добавление источников (ссылки с новой строки) ─────────
    if action == "add_source_links":
        lines = [l.strip() for l in text.splitlines() if l.strip()]
        added = []; not_joined = []
        await safe_answer(msg, "⏳ <b>Проверка подписки...</b>")
        for raw in lines:
            raw = raw.replace("https://t.me/","").lstrip("@").strip()
            try:
                entity  = await _userbot.get_entity(raw)
                chat_id = entity.id
                title   = getattr(entity,"title",raw)
                uname   = getattr(entity,"username",None)
                link    = f"https://t.me/{uname}" if uname else None
                # Проверяем подписку
                try:
                    await _userbot.get_permissions(entity, await _userbot.get_me())
                    _db.add_source(chat_id, title, uname, link)
                    added.append(title)
                except Exception:
                    not_joined.append((raw, link or f"https://t.me/{raw}"))
            except Exception as e:
                not_joined.append((raw, raw))
                log.warning(f"get_entity {raw}: {e}")

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

    # ── Шаблоны: wizard ───────────────────────────────────────
    if action == "tmpl_wizard":
        wiz = _tmpl_wizard.get(uid)
        if not wiz:
            await safe_answer(msg, "❌ Сессия истекла", kb_back("admin_replies")); return
        tid  = wiz["tid"]; step = wiz["step"]
        _db.update_template_variant(tid, step, text)
        if step < 3:
            wiz["step"] = step + 1
            _admin_pending[uid] = "tmpl_wizard"
            await safe_answer(msg,
                f"<b>Добавьте отклик для Шаблона #{tid}</b>\n\n<b>Вариант {step+1}</b>\n\nПришлите текст:",
                mkb([[("◀️ Назад",f"admin_tmpl:{tid}")]]))
        else:
            _tmpl_wizard.pop(uid, None)
            await safe_answer(msg,
                f"✅ Шаблон <b>#{tid}</b> добавлен!",
                kb_back("admin_replies"))
        return

    # ── Редактирование варианта шаблона ───────────────────────
    if action.startswith("edit_variant:"):
        _, tid, vnum = action.split(":")
        _db.update_template_variant(int(tid), int(vnum), text)
        await safe_answer(msg, f"✅ Вариант {vnum} обновлён", kb_back(f"admin_tmpl:{tid}"))
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

    # ── Глобальное правило DeepSeek ───────────────────────────
    if action == "add_ds_rule":
        key = f"ds_rule_{int(time.time())}"
        _db.set_setting(key, text)
        await safe_answer(msg, f"✅ Правило добавлено:\n<code>{text}</code>",
                         kb_back("admin_deepseek"))
        return

    # ── Выдача подписки ───────────────────────────────────────
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

    # ── Рассылка ──────────────────────────────────────────────
    if action == "broadcast":
        clients = _db.get_active_clients(); sent = 0
        for cl in clients:
            try:
                await msg.bot.send_message(cl["tg_id"], text, parse_mode=ParseMode.HTML)
                sent += 1; await asyncio.sleep(0.05)
            except Exception as e: log.warning(f"Рассылка {cl['tg_id']}: {e}")
        await safe_answer(msg,
            f"✅ Рассылка завершена: <b>{sent}/{len(clients)}</b>",
            kb_back("admin_clients"))
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
    sub = fmt_date(cl.get("sub_until"))
    bonus = "\n<b>Тебе начислено +3 бесплатных дня</b>" if is_new else ""
    return (
        f"Это <b>phase.parser</b>👔{bonus}\n\n"
        f"Подписка активна до: {sub}"
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

        # Реферал
        args = msg.text.split() if msg.text else []
        if len(args) > 1 and args[1].startswith("ref"):
            try:
                ref_tg_id = int(args[1][3:])
                ref_cl    = _db.get_client_by_tg(ref_tg_id)
                if ref_cl and ref_cl["id"] != cl["id"]:
                    _db.extend_subscription(ref_cl["id"], REF_DAYS)
                    _db._c().execute("UPDATE clients SET ref_by=? WHERE id=?", (ref_cl["id"], cl["id"]))
                    _db._c().commit()
                    await msg.bot.send_message(ref_tg_id,
                        f"🎉 По вашей ссылке зарегистрировался новый пользователь!\n"
                        f"Вам начислено <b>+{REF_DAYS} дня</b> к подписке.",
                        parse_mode=ParseMode.HTML)
            except Exception as e: log.warning(f"Реферал: {e}")

    await msg.answer(_client_main_text(cl, is_new), reply_markup=kb_client_main())

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
    sub = fmt_date(cl.get("sub_until"))
    ref_link = f"https://t.me/{(await call.bot.get_me()).username}?start=ref{uid}"
    invited  = _db._c().execute("SELECT COUNT(*) FROM clients WHERE ref_by=?", (cl["id"],)).fetchone()[0]
    text = (
        f"Вы используете <b>phase.parser</b>👔\n\n"
        f"Подписка активна до: {sub}\n\n"
        f"Реферальная ссылка:\n<code>{ref_link}</code>\n"
        f"<b>+{REF_DAYS} дня</b> вам и <b>+{REF_DAYS} дня</b> ему\n"
        f"Приглашено: {invited}"
    )
    await safe_edit(call, text, mkb([[("◀️ Главное меню","client_main")]]))

# ── Тарифы ─────────────────────────────────────────────────────
async def _tariffs_text(cl: dict) -> str:
    sub = fmt_date(cl.get("sub_until"))
    has_paid = bool(cl.get("first_payment"))
    disc = " нет" if has_paid else ""
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
    ticket  = _db.create_payment(cl["id"], tariff, amount, p["days"])
    fire    = "" if has_paid else "🔥"
    text    = (
        f"Тариф <b>{p['label']}</b>\n\n"
        f"<code>{PAYMENT_PHONE}</code>\n"
        f"{PAYMENT_BANK}\n\n"
        f"К оплате: <b>{amount}₽</b>{fire}\n\n"
        f"⚠️ <b>После оплаты нажмите Оплатил(а)</b> ⚠️"
    )
    markup = mkb([
        [("✅ Оплатил(а)", f"client_paid:{ticket}")],
        [("◀️ Главное меню","client_main")],
    ])
    await safe_edit(call, text, markup)

@client_router.callback_query(F.data.startswith("client_paid:"))
async def client_paid_cb(call: CallbackQuery):
    ticket = call.data.split(":", 1)[1]
    p      = _db.get_payment_by_ticket(ticket)
    if not p: await call.answer("Тикет не найден"); return
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
    # Уведомление админу
    cl = _db.get_client_by_tg(call.from_user.id)
    try:
        await call.bot.send_message(ADMIN_ID,
            f"💳 <b>Новый платёж!</b>\n\n"
            f"Тикет: <code>{ticket}</code>\n"
            f"Клиент: @{call.from_user.username or call.from_user.id}\n"
            f"Тариф: {p['tariff']} | Сумма: <b>{p['amount']}₽</b>",
            parse_mode=ParseMode.HTML,
            reply_markup=mkb([[("✅ Подтвердить", f"admin_confirm_pay:{ticket}")]]))
    except Exception as e: log.error(f"Уведомление об оплате: {e}")

# ── Подтверждение оплаты администратором ───────────────────────
@admin_router.callback_query(F.data.startswith("admin_confirm_pay:"))
async def admin_confirm_pay_cb(call: CallbackQuery):
    ticket = call.data.split(":", 1)[1]
    log.info(f"Подтверждение оплаты: {ticket}")

    p = _db.get_payment_by_ticket(ticket)
    if not p:
        await call.answer("❌ Тикет не найден", show_alert=True); return
    cl = _db.get_client_by_id(p["client_id"])
    if not cl:
        await call.answer("❌ Клиент не найден", show_alert=True); return

    already = p.get("status") == "confirmed"
    if not already:
        _db.confirm_payment(ticket)
        until = _db.extend_subscription(cl["id"], p["days"])
        _db._c().execute("UPDATE clients SET first_payment=1 WHERE id=?", (cl["id"],))
        _db._c().commit()
        _db.stat_inc("subs_bought")
        log.info(f"Оплата OK: {ticket} клиент={cl['tg_id']}")
    else:
        raw = cl.get("sub_until") or datetime.now().isoformat()
        until = datetime.fromisoformat(raw)

    until_fmt = fmt_date(until.isoformat())
    text = (
        f"✅ <b>Оплата подтверждена</b>\n\n"
        f"Тикет: <code>{ticket}</code>\n"
        f"Клиент: <code>{cl['tg_id']}</code> @{cl.get('username') or '—'}\n"
        f"Тариф: <b>{p['tariff']}</b> ({p['days']} дн.)\n"
        f"Подписка до: <b>{until_fmt}</b>"
    )
    await call.answer("✅ Готово")
    try:
        await call.message.edit_text(text, parse_mode=ParseMode.HTML,
            reply_markup=mkb([[("◀️ К клиентам", "admin_clients")]]))
    except Exception:
        await call.message.answer(text, parse_mode=ParseMode.HTML,
            reply_markup=mkb([[("◀️ К клиентам", "admin_clients")]]))

    if not already:
        try:
            await call.bot.send_message(cl["tg_id"],
                f"✅ <b>Оплата подтверждена!</b>\n\n"
                f"Подписка активна до: <b>{until_fmt}</b>",
                parse_mode=ParseMode.HTML,
                reply_markup=kb_client_main())
        except Exception as e:
            log.error(f"Уведомление клиента: {e}")

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
        f"Введите стоп-слова через Enter:\n\n"
        f"<i>Пример:\nТестовое\nШтат\nБез оплаты</i>\n\n"
        f"<b>Текущие ({len(words)}):</b>\n" +
        ("\n".join(f"<code>{w}</code>" for w in words) if words else "<i>нет</i>")
    )
    _client_pending[call.from_user.id] = "add_stopwords"
    markup = mkb([[("◀️ Главное меню","client_main")]])
    await safe_edit(call, text, markup)

# ── Открыть контакты ───────────────────────────────────────────
@client_router.callback_query(F.data.startswith("show_contact:"))
async def show_contact_cb(call: CallbackQuery):
    parts   = call.data.split(":")
    vid     = int(parts[1])
    uid     = call.from_user.id

    if not _db.is_subscribed(uid):
        await safe_edit(call,
            "❌ <b>Ваша подписка закончилась!</b>\n\nОплатите чтобы получать новые заказы",
            mkb([[("💳 Тарифы","client_tariffs")]]))
        return

    vacancy = _db.get_vacancy(vid)
    if not vacancy: await call.answer("Не найдена"); return
    contact    = vacancy.get("ds_contact","")
    old_text   = call.message.html_text or call.message.text or ""
    new_text   = old_text.replace("—", contact) if contact else old_text

    if contact.startswith("@"):
        uname    = contact.lstrip("@")
        client_link = f"https://t.me/{uname}"
        new_text += f"\n\n<a href='{client_link}'>💬 Написать клиенту</a>"

    try:
        await call.message.edit_text(new_text, parse_mode=ParseMode.HTML)
    except TelegramBadRequest: pass
    await call.answer("✅ Контакты открыты")

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
            mkb([[("⚙️ Настройки","client_settings")]]))
        return

    await msg.answer(_client_main_text(cl), reply_markup=kb_client_main())
# ═══════════════════════════════════════════════════════════════
# USERBOT
# ═══════════════════════════════════════════════════════════════
def _register_userbot(userbot: TelegramClient, pipeline: VacancyPipeline) -> None:
    @userbot.on(events.NewMessage())
    async def _handler(event):
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
        """Перезапускает userbot при TypeNotFoundError вместо падения."""
        while True:
            try:
                await _userbot.run_until_disconnected()
                break  # нормальное завершение
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
        dp.start_polling(bot, allowed_updates=["message","callback_query"]),
        _pipeline.run_worker(),
        _periodic_cleanup(),
        _check_expired_subs(bot),
        _safe_userbot_run(),
    )

if __name__ == "__main__":
    try: asyncio.run(main())
    except KeyboardInterrupt: log.info("phase.parser остановлен")
    except Exception as e: log.critical(f"Критическая ошибка: {e}", exc_info=True)
