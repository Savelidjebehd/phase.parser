"""
phase.parser — агрегатор публичных вакансий из Telegram-групп
Стек: Telethon (UserBot) + Aiogram 3.x (Bot) + SQLite + DeepSeek API
"""

from __future__ import annotations
import asyncio, io, json, logging, os, random, re, sqlite3, time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from logging.handlers import TimedRotatingFileHandler
from typing import Optional

import aiohttp
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import BufferedInputFile, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from dotenv import load_dotenv
from telethon import TelegramClient, events
from telethon.errors import PhoneCodeExpiredError, PhoneCodeInvalidError, PasswordHashInvalidError, SessionPasswordNeededError
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
DB_PATH        = os.getenv("DB_PATH", "parser.db")

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
    suitable: bool; reason: str; contact: str

# ── База данных ───────────────────────────────────────────────
class Database:
    def __init__(self, path: str):
        self.path = path
        self._conn: Optional[sqlite3.Connection] = None

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
                username TEXT, sub_until TEXT, created_at TEXT DEFAULT (datetime('now')));
            CREATE TABLE IF NOT EXISTS client_stopwords (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                client_id INTEGER NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
                word TEXT NOT NULL, UNIQUE(client_id,word));
            CREATE TABLE IF NOT EXISTS templates (
                id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
                variant1 TEXT NOT NULL, variant2 TEXT NOT NULL, variant3 TEXT NOT NULL,
                active INTEGER DEFAULT 1, created_at TEXT DEFAULT (datetime('now')));
            CREATE TABLE IF NOT EXISTS vacancies (
                id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL, text TEXT NOT NULL, author_username TEXT,
                author_id INTEGER, source_title TEXT, message_link TEXT, ts TEXT,
                suitable INTEGER, ds_reason TEXT, ds_contact TEXT,
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
            CREATE TABLE IF NOT EXISTS ds_tokens (
                date TEXT PRIMARY KEY, tokens_in INTEGER DEFAULT 0, tokens_out INTEGER DEFAULT 0);
            CREATE TABLE IF NOT EXISTS logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL DEFAULT (datetime('now')),
                level TEXT NOT NULL, message TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        """)
        self._c().commit()
        self._c().execute("INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)", (
            "ds_system_prompt",
            "Ты фильтр вакансий для видеомонтажёра/дизайнера. Определи: является ли текст вакансией/заказом. "
            "Найди контакт для отклика (@username/телефон), но не ссылки на портфолио. "
            "Верни JSON без markdown."))
        self._c().commit()
        log.info("Таблицы инициализированы")

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

    def get_keywords(self, ktype: str = "common") -> list[str]:
        return [r["word"] for r in self._c().execute(
            "SELECT word FROM keywords WHERE type=? ORDER BY word", (ktype,)).fetchall()]

    def add_keyword(self, word: str, ktype: str = "common") -> None:
        self._c().execute("INSERT OR IGNORE INTO keywords(word,type) VALUES(?,?)", (word.lower().strip(), ktype))
        self._c().commit()

    def delete_keyword(self, word: str, ktype: str = "common") -> None:
        self._c().execute("DELETE FROM keywords WHERE word=? AND type=?", (word, ktype)); self._c().commit()

    def get_blacklist(self, btype: str = "common") -> list[str]:
        return [r["word"] for r in self._c().execute(
            "SELECT word FROM blacklist WHERE type=? ORDER BY word", (btype,)).fetchall()]

    def add_to_blacklist(self, word: str, btype: str = "common") -> None:
        self._c().execute("INSERT OR IGNORE INTO blacklist(word,type) VALUES(?,?)", (word.lower().strip(), btype))
        self._c().commit()

    def delete_from_blacklist(self, word: str, btype: str = "common") -> None:
        self._c().execute("DELETE FROM blacklist WHERE word=? AND type=?", (word, btype)); self._c().commit()

    def get_templates(self, active_only: bool = False) -> list[dict]:
        q = "SELECT * FROM templates" + (" WHERE active=1" if active_only else "") + " ORDER BY id"
        return [dict(r) for r in self._c().execute(q).fetchall()]

    def get_template(self, tid: int) -> Optional[dict]:
        row = self._c().execute("SELECT * FROM templates WHERE id=?", (tid,)).fetchone()
        return dict(row) if row else None

    def add_template(self, name: str, v1: str, v2: str, v3: str) -> int:
        cur = self._c().execute("INSERT INTO templates(name,variant1,variant2,variant3) VALUES(?,?,?,?)",
                                (name, v1, v2, v3)); self._c().commit(); return cur.lastrowid or 0

    def update_template_variant(self, tid: int, vnum: int, text: str) -> None:
        self._c().execute(f"UPDATE templates SET variant{vnum}=? WHERE id=?", (text, tid)); self._c().commit()

    def toggle_template(self, tid: int) -> None:
        self._c().execute("UPDATE templates SET active=1-active WHERE id=?", (tid,)); self._c().commit()

    def delete_template(self, tid: int) -> None:
        self._c().execute("DELETE FROM templates WHERE id=?", (tid,)); self._c().commit()

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
            "SELECT * FROM clients WHERE sub_until IS NOT NULL AND sub_until > ?", (now,)).fetchall()]

    def set_subscription(self, client_id: int, until: datetime) -> None:
        self._c().execute("UPDATE clients SET sub_until=? WHERE id=?", (until.isoformat(), client_id))
        self._c().commit()

    def is_subscribed(self, tg_id: int) -> bool:
        now = datetime.now().isoformat()
        return bool(self._c().execute(
            "SELECT id FROM clients WHERE tg_id=? AND sub_until IS NOT NULL AND sub_until > ?",
            (tg_id, now)).fetchone())

    def get_client_stopwords(self, client_id: int) -> list[str]:
        return [r["word"] for r in self._c().execute(
            "SELECT word FROM client_stopwords WHERE client_id=?", (client_id,)).fetchall()]

    def add_client_stopword(self, client_id: int, word: str) -> None:
        self._c().execute("INSERT OR IGNORE INTO client_stopwords(client_id,word) VALUES(?,?)",
                          (client_id, word.lower().strip())); self._c().commit()

    def delete_client_stopword(self, client_id: int, word: str) -> None:
        self._c().execute("DELETE FROM client_stopwords WHERE client_id=? AND word=?", (client_id, word))
        self._c().commit()

    def save_vacancy(self, v: Vacancy) -> Optional[int]:
        try:
            cur = self._c().execute(
                "INSERT OR IGNORE INTO vacancies(chat_id,message_id,text,author_username,author_id,"
                "source_title,message_link,ts) VALUES(?,?,?,?,?,?,?,?)",
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

    def update_vacancy_ds(self, vid: int, suitable: bool, reason: str, contact: str) -> None:
        self._c().execute("UPDATE vacancies SET suitable=?,ds_reason=?,ds_contact=? WHERE id=?",
                          (1 if suitable else 0, reason, contact, vid)); self._c().commit()

    def get_vacancy(self, vid: int) -> Optional[dict]:
        row = self._c().execute("SELECT * FROM vacancies WHERE id=?", (vid,)).fetchone()
        return dict(row) if row else None

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
            "SELECT r.*,v.message_link,v.source_title,v.ds_contact,v.text as vacancy_text,"
            "v.author_id,v.author_username FROM replies r JOIN vacancies v ON r.vacancy_id=v.id WHERE r.id=?",
            (reply_id,)).fetchone()
        return dict(row) if row else None

    def get_replies(self, limit: int = 8, offset: int = 0) -> list[dict]:
        return [dict(r) for r in self._c().execute(
            "SELECT r.*,v.message_link,v.source_title,v.ds_contact,v.text as vacancy_text "
            "FROM replies r JOIN vacancies v ON r.vacancy_id=v.id ORDER BY r.id DESC LIMIT ? OFFSET ?",
            (limit, offset)).fetchall()]

    def count_replies(self) -> int:
        return self._c().execute("SELECT COUNT(*) FROM replies").fetchone()[0]

    def save_delivery(self, vacancy_id: int, client_id: int, msg_id: Optional[int],
                      skipped: bool = False, reason: str = "") -> None:
        self._c().execute(
            "INSERT OR IGNORE INTO client_deliveries(vacancy_id,client_id,msg_id,skipped,skip_reason) VALUES(?,?,?,?,?)",
            (vacancy_id, client_id, msg_id, 1 if skipped else 0, reason)); self._c().commit()

    def add_tokens(self, ti: int, to_: int) -> None:
        today = datetime.now().strftime("%Y-%m-%d")
        self._c().execute(
            "INSERT INTO ds_tokens(date,tokens_in,tokens_out) VALUES(?,?,?) "
            "ON CONFLICT(date) DO UPDATE SET tokens_in=tokens_in+excluded.tokens_in,"
            "tokens_out=tokens_out+excluded.tokens_out", (today, ti, to_)); self._c().commit()

    def get_tokens_today(self) -> dict:
        today = datetime.now().strftime("%Y-%m-%d")
        row = self._c().execute("SELECT * FROM ds_tokens WHERE date=?", (today,)).fetchone()
        return dict(row) if row else {"tokens_in": 0, "tokens_out": 0}

    def add_log(self, level: str, message: str) -> None:
        try:
            self._c().execute("INSERT INTO logs(level,message) VALUES(?,?)", (level, message))
            self._c().commit()
        except Exception: pass

    def get_logs(self, date: str, limit: int = 200) -> list[dict]:
        return [dict(r) for r in self._c().execute(
            "SELECT * FROM logs WHERE ts LIKE ? ORDER BY id DESC LIMIT ?", (f"{date}%", limit)).fetchall()]

    def export_logs(self, date: str) -> bytes:
        rows = self._c().execute(
            "SELECT * FROM logs WHERE ts LIKE ? ORDER BY id ASC", (f"{date}%",)).fetchall()
        return "\n".join(f"{r['ts']} | {r['level']:<8} | {r['message']}" for r in rows).encode("utf-8")

    def get_stats(self) -> dict:
        c = self._c(); now = datetime.now().isoformat(); tok = self.get_tokens_today()
        return {
            "sources":      c.execute("SELECT COUNT(*) FROM sources WHERE active=1").fetchone()[0],
            "total_vac":    c.execute("SELECT COUNT(*) FROM vacancies").fetchone()[0],
            "suitable_vac": c.execute("SELECT COUNT(*) FROM vacancies WHERE suitable=1").fetchone()[0],
            "rejected_vac": c.execute("SELECT COUNT(*) FROM vacancies WHERE suitable=0").fetchone()[0],
            "replies":      c.execute("SELECT COUNT(*) FROM replies WHERE deleted=0").fetchone()[0],
            "clients":      c.execute("SELECT COUNT(*) FROM clients").fetchone()[0],
            "active_subs":  c.execute("SELECT COUNT(*) FROM clients WHERE sub_until>?", (now,)).fetchone()[0],
            "tokens_in":    tok["tokens_in"], "tokens_out": tok["tokens_out"],
        }

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
    rules = db.get_ds_rules()
    rules_block = ("ГЛОБАЛЬНЫЕ ПРАВИЛА:\n" + "\n".join(f"- {r['value']}" for r in rules) + "\n\n") if rules else ""
    system = (
        rules_block + db.get_setting("ds_system_prompt") + "\n\n"
        'Верни JSON: {"suitable": true/false, "reason": "макс 5 слов если не подходит", "contact": "@username или пусто"}'
    )
    payload = {
        "model": DEEPSEEK_MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user",   "content": f"Автор: @{author_username}\n\nТекст:\n{text[:3000]}"},
        ],
        "max_tokens": 120, "temperature": 0.0,
        "response_format": {"type": "json_object"},
    }
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(DEEPSEEK_URL, json=payload,
                headers={"Authorization": f"Bearer {DEEPSEEK_KEY}", "Content-Type": "application/json"},
                timeout=aiohttp.ClientTimeout(total=30)) as resp:
                if resp.status != 200:
                    log.error(f"DeepSeek HTTP {resp.status}: {(await resp.text())[:200]}"); return _DS_FAIL
                data = await resp.json()
        usage = data.get("usage", {})
        db.add_tokens(usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0))
        parsed  = json.loads(data["choices"][0]["message"]["content"])
        suitable = bool(parsed.get("suitable", False))
        reason   = str(parsed.get("reason", ""))[:60]
        contact  = str(parsed.get("contact", "")).strip()
        if not contact or contact.lower() in ("null", "none", ""):
            contact = f"@{author_username}" if author_username else ""
        log.info(f"DeepSeek → suitable={suitable}, reason='{reason}', contact='{contact}'")
        db.add_log("INFO", f"DeepSeek: suitable={suitable}, reason={reason}")
        return DeepSeekResult(suitable=suitable, reason=reason, contact=contact)
    except Exception as e:
        log.error(f"DeepSeek ошибка: {e}"); return _DS_FAIL

async def check_deepseek_status() -> bool:
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(DEEPSEEK_URL,
                json={"model": DEEPSEEK_MODEL, "messages": [{"role": "user", "content": "ok"}], "max_tokens": 1},
                headers={"Authorization": f"Bearer {DEEPSEEK_KEY}"},
                timeout=aiohttp.ClientTimeout(total=10)) as resp:
                return resp.status == 200
    except Exception: return False

# ═══════════════════════════════════════════════════════════════
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ═══════════════════════════════════════════════════════════════

def extract_text(message) -> str:
    caption = message.text or message.message or ""
    if message.media is None: return caption
    if isinstance(message.media, MessageMediaPhoto): marker = "[image]"
    elif isinstance(message.media, MessageMediaDocument):
        mime = getattr(getattr(message.media, "document", None), "mime_type", "") or ""
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
    uname  = getattr(chat, "username", None)
    msg_id = event.message.id
    if uname: return f"https://t.me/{uname}/{msg_id}"
    raw = str(abs(event.chat_id))
    if raw.startswith("100"): raw = raw[3:]
    return f"https://t.me/c/{raw}/{msg_id}"

# ═══════════════════════════════════════════════════════════════
# KEYBOARD BUILDER
# ═══════════════════════════════════════════════════════════════

def mkb(buttons: list[list[tuple[str, str]]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=t, callback_data=cd) for t, cd in row] for row in buttons])

def kb_back(dest: str = "admin_main") -> InlineKeyboardMarkup:
    return mkb([[("◀️ Назад", dest)]])

def kb_admin_main() -> InlineKeyboardMarkup:
    return mkb([
        [("📡 Источники",  "admin_sources"),  ("✍️ Отклики",    "admin_replies")],
        [("📊 Статистика", "admin_stats"),     ("🔍 Мониторинг", "admin_monitoring")],
        [("👥 Клиент-бот", "admin_clients"),   ("📜 Логи",       "admin_logs")],
        [("⚙️ Настройки",  "admin_settings")],
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
        log.debug(f"Очередь: {self.queue.qsize()}")

    async def run_worker(self) -> None:
        log.info("Воркер очереди запущен")
        while True:
            try:
                event = await self.queue.get()
                await self._process(event)
                self.queue.task_done()
            except Exception as e:
                log.error(f"Воркер: {e}", exc_info=True)

    async def _process(self, event) -> None:
        try:
            msg    = event.message
            chat   = await event.get_chat()
            sender = await event.get_sender()

            chat_id      = event.chat_id
            message_id   = msg.id
            source_title = getattr(chat, "title", None) or getattr(chat, "username", None) or str(chat_id)
            username     = getattr(sender, "username", None) or ""
            sender_id    = getattr(sender, "id", 0) or 0
            message_link = make_msg_link(event, chat)
            text         = extract_text(msg)
            if not text.strip(): return

            log.info(f"📥 [{source_title}] от @{username} | {message_link}")
            self.db.add_log("INFO", f"Получено: {message_link} от @{username}")

            # ── 2. Ключевые слова ─────────────────────────────
            keywords = self.db.get_keywords("common")
            text_low = text.lower()
            found_kw = [kw for kw in keywords if kw in text_low]
            if not found_kw:
                log.debug("⛔ Ключевые слова не найдены"); return
            log.info(f"✅ Ключевые слова: {found_kw}")
            self.db.add_log("INFO", f"Ключевые слова: {found_kw}")

            # ── 3. Дубликат ───────────────────────────────────
            if self.db.is_duplicate(text):
                log.info("🔁 Дубликат, пропуск"); self.db.add_log("INFO", "Дубликат"); return
            log.info("✅ Не дубликат")

            # ── 4. Чёрный список ──────────────────────────────
            found_bl = [w for w in self.db.get_blacklist("common") if w in text_low]
            if found_bl:
                log.info(f"⛔ Стоп-слово: {found_bl}")
                self.db.add_log("INFO", f"Стоп-слово: {found_bl}"); return
            log.info("✅ Чёрный список пройден")

            vacancy = Vacancy(chat_id=chat_id, message_id=message_id, text=text,
                              author_username=username, author_id=sender_id,
                              source_title=source_title, message_link=message_link, timestamp=datetime.now())
            vid = self.db.save_vacancy(vacancy)
            if not vid: log.error("Не удалось сохранить вакансию"); return

            # ── 5. DeepSeek ───────────────────────────────────
            log.info(f"🤖 DeepSeek (vid={vid})")
            ds = await call_deepseek(text, username, self.db)
            self.db.update_vacancy_ds(vid, ds.suitable, ds.reason, ds.contact)

            if not ds.suitable:
                log.info(f"❌ Не подходит: {ds.reason}")
                self.db.add_log("INFO", f"Отклонено: {ds.reason}")
                await self._notify_admin_rejected(vacancy, ds); return

            log.info(f"✅ Подходит! Контакт: {ds.contact}")
            self.db.add_log("INFO", f"Подходит: contact={ds.contact}")
            await self._handle_suitable(vacancy, ds, vid)
        except Exception as e:
            log.error(f"_process: {e}", exc_info=True)

    async def _handle_suitable(self, vacancy: Vacancy, ds: DeepSeekResult, vid: int) -> None:
        templates = self.db.get_templates(active_only=True)
        if not templates:
            log.warning("Нет активных шаблонов!")
            self.db.add_log("WARNING", "Нет активных шаблонов")
            await self._notify_admin_no_template(vacancy)
            await self._broadcast(vacancy, ds, vid); return

        tmpl        = random.choice(templates)
        variant_num = random.randint(1, 3)
        reply_text  = tmpl[f"variant{variant_num}"]
        log.info(f"📝 Шаблон #{tmpl['id']}.{variant_num}")
        self.db.add_log("INFO", f"Шаблон #{tmpl['id']}.{variant_num}")

        delay = random.randint(5, 15)
        log.info(f"⏳ Задержка {delay}с"); await asyncio.sleep(delay)

        sent_msg_id = None
        contact     = ds.contact
        if contact and contact.startswith("@"):
            try:
                sent = await self.userbot.send_message(contact, reply_text)
                sent_msg_id = sent.id
                log.info(f"📤 Отклик → {contact}")
                self.db.add_log("INFO", f"Отклик отправлен: {contact}")
            except Exception as e:
                log.error(f"Ошибка отправки: {e}")
                self.db.add_log("ERROR", f"Ошибка отклика: {e}")
        else:
            log.warning(f"Контакт '{contact}' не @username — отклик не отправлен")

        reply_id = self.db.save_reply(vid, tmpl["id"], variant_num, reply_text, sent_msg_id)
        log.info(f"💾 reply_id={reply_id}")
        self.db.add_log("INFO", f"reply_id={reply_id}")
        await self._notify_admin_reply(vacancy, ds, tmpl, variant_num, reply_id)
        await self._broadcast(vacancy, ds, vid)

    async def _broadcast(self, vacancy: Vacancy, ds: DeepSeekResult, vid: int) -> None:
        clients = self.db.get_active_clients()
        log.info(f"📢 Рассылка {len(clients)} клиентам")
        self.db.add_log("INFO", f"Рассылка: {len(clients)} клиентов")
        for cl in clients:
            try:
                cl_id      = cl["id"]
                stop_words = self.db.get_client_stopwords(cl_id)
                text_low   = vacancy.text.lower()
                hit        = [w for w in stop_words if w in text_low]
                if hit:
                    self.db.save_delivery(vid, cl_id, None, skipped=True, reason=f"stopword:{hit[0]}")
                    log.debug(f"Клиент {cl['tg_id']}: стоп '{hit[0]}'"); continue

                hidden   = hide_contact(vacancy.text, ds.contact)
                msg_text = (
                    f"<b>📋 Новая вакансия</b>\n"
                    f"<i>Источник: {vacancy.source_title}</i>\n\n"
                    f"{hidden}\n\n"
                    f"<i>🕐 {vacancy.timestamp.strftime('%d.%m.%Y %H:%M')}</i>"
                )
                markup = mkb([[("🔓 Открыть контакты", f"open_contact:{vid}:{cl_id}")]])
                sent   = await self.bot.send_message(cl["tg_id"], msg_text, parse_mode=ParseMode.HTML, reply_markup=markup)
                self.db.save_delivery(vid, cl_id, sent.message_id)
                self.db.add_log("INFO", f"Клиент {cl['tg_id']}: вакансия vid={vid}")
            except Exception as e:
                log.error(f"Рассылка {cl.get('tg_id')}: {e}")

    async def _notify_admin_reply(self, vacancy: Vacancy, ds: DeepSeekResult,
                                   tmpl: dict, variant_num: int, reply_id: int) -> None:
        reply_num  = f"{tmpl['id']}.{variant_num}"
        short_text = vacancy.text[:400] + ("…" if len(vacancy.text) > 400 else "")
        markup = mkb([
            [("⚠️ Ошибка", f"admin_error:{reply_id}"), ("🗑 Удалить", f"admin_delete_reply:{reply_id}")],
        ])
        text = (
            f"✅ <b>Новый отклик! #<code>{reply_num}</code></b>\n\n"
            f"<blockquote expandable>{short_text}</blockquote>\n\n"
            f"<a href='{vacancy.message_link}'>Сообщение</a> | "
            f"<a href='tg://user?id={vacancy.author_id}'>Клиент</a>\n"
            f"Контакт: <code>{ds.contact}</code>"
        )
        try:
            await self.bot.send_message(ADMIN_ID, text, parse_mode=ParseMode.HTML, reply_markup=markup)
            self.db.add_log("INFO", f"Уведомление: reply #{reply_num}")
        except Exception as e: log.error(f"Уведомление (reply): {e}")

    async def _notify_admin_rejected(self, vacancy: Vacancy, ds: DeepSeekResult) -> None:
        short = vacancy.text[:250] + ("…" if len(vacancy.text) > 250 else "")
        try:
            await self.bot.send_message(ADMIN_ID,
                f"❌ <b>Вакансия отклонена</b>\n\n"
                f"<blockquote expandable>{short}</blockquote>\n\n"
                f"<b>Причина:</b> {ds.reason}\n"
                f"<a href='{vacancy.message_link}'>К сообщению</a>",
                parse_mode=ParseMode.HTML)
        except Exception as e: log.error(f"Уведомление (rejected): {e}")

    async def _notify_admin_no_template(self, vacancy: Vacancy) -> None:
        try:
            await self.bot.send_message(ADMIN_ID,
                f"⚠️ <b>Вакансия найдена, но нет активных шаблонов!</b>\n"
                f"<a href='{vacancy.message_link}'>К вакансии</a>",
                parse_mode=ParseMode.HTML)
        except Exception as e: log.error(f"Уведомление (no_template): {e}")
# ═══════════════════════════════════════════════════════════════
# ГЛОБАЛЬНОЕ СОСТОЯНИЕ
# ═══════════════════════════════════════════════════════════════

_db:       Optional[Database]         = None
_pipeline: Optional[VacancyPipeline]  = None
_userbot:  Optional[TelegramClient]   = None
_admin_pending:  dict[int, str] = {}
_client_pending: dict[int, str] = {}
_auth_state: dict = {}

admin_router  = Router()
client_router = Router()

def is_admin(uid: int) -> bool: return uid == ADMIN_ID

async def safe_edit(call: CallbackQuery, text: str, markup=None) -> None:
    try:
        await call.message.edit_text(text, parse_mode=ParseMode.HTML,
                                     reply_markup=markup, disable_web_page_preview=True)
    except TelegramBadRequest: pass

# ═══════════════════════════════════════════════════════════════
# ADMIN — /start и главное меню
# ═══════════════════════════════════════════════════════════════

@admin_router.message(Command("start"))
async def admin_cmd_start(message: Message):
    if not is_admin(message.from_user.id): return
    await message.answer("<b>👑 phase.parser — Панель администратора</b>", reply_markup=kb_admin_main())

@admin_router.callback_query(F.data == "admin_main")
async def admin_main_cb(call: CallbackQuery):
    if not is_admin(call.from_user.id): return
    await safe_edit(call, "<b>👑 phase.parser — Панель администратора</b>", kb_admin_main())

# ═══════════════════════════════════════════════════════════════
# ADMIN — ИСТОЧНИКИ
# ═══════════════════════════════════════════════════════════════

@admin_router.callback_query(F.data == "admin_sources")
async def admin_sources_cb(call: CallbackQuery):
    if not is_admin(call.from_user.id): return
    sources = _db.get_sources(active_only=False)
    lines   = [f"{'✅' if s['active'] else '❌'} <b>{s['title']}</b> <code>{s['chat_id']}</code>" for s in sources]
    text    = "<b>📡 Источники</b>\n\n" + ("\n".join(lines) if lines else "<i>Нет источников</i>")
    markup  = mkb([
        [("➕ Добавить", "admin_src_add"), ("🔍 Проверить подписки", "admin_src_check")],
        [("⚙️ Управление", "admin_src_manage"), ("📤 Экспорт", "admin_src_export")],
        [("◀️ Назад", "admin_main")],
    ])
    await safe_edit(call, text, markup)

@admin_router.callback_query(F.data == "admin_src_add")
async def admin_src_add_cb(call: CallbackQuery):
    if not is_admin(call.from_user.id): return
    _admin_pending[call.from_user.id] = "add_source"
    await safe_edit(call,
        "📡 <b>Добавление источника</b>\n\n"
        "Отправьте @username или ссылку на группу/канал.\n"
        "<i>Пример: @montage_jobs</i>",
        kb_back("admin_sources"))

@admin_router.callback_query(F.data == "admin_src_check")
async def admin_src_check_cb(call: CallbackQuery):
    if not is_admin(call.from_user.id): return
    await call.answer("Проверяю...")
    sources    = _db.get_sources()
    not_joined = []
    for s in sources:
        try: await _userbot.get_entity(s["chat_id"])
        except Exception: not_joined.append(s)
    if not_joined:
        names  = "\n".join(f"• {s['title']}" for s in not_joined)
        markup = mkb([[("✅ Подписаться на все", "admin_src_join_all")],
                      [("⏭ Пропустить", "admin_sources")]])
        await safe_edit(call, f"⚠️ <b>UserBot не состоит в:</b>\n\n{names}", markup)
    else:
        await safe_edit(call, "✅ <b>UserBot состоит во всех источниках</b>", kb_back("admin_sources"))

@admin_router.callback_query(F.data == "admin_src_join_all")
async def admin_src_join_all_cb(call: CallbackQuery):
    if not is_admin(call.from_user.id): return
    sources = _db.get_sources(); joined = 0
    for s in sources:
        try:
            await _userbot(JoinChannelRequest(s["username"] or s["chat_id"]))
            joined += 1; await asyncio.sleep(1)
        except Exception as e: log.warning(f"Подписка {s['title']}: {e}")
    await safe_edit(call, f"✅ Подписался: <b>{joined}/{len(sources)}</b>", kb_back("admin_sources"))

@admin_router.callback_query(F.data == "admin_src_manage")
async def admin_src_manage_cb(call: CallbackQuery):
    if not is_admin(call.from_user.id): return
    sources = _db.get_sources(active_only=False)
    if not sources: await call.answer("Нет источников"); return
    rows = []
    for s in sources:
        icon = "✅" if s["active"] else "❌"
        rows.append([(f"{icon} {s['title'][:28]}", f"admin_src_toggle:{s['id']}")])
        rows.append([(f"🗑 Удалить: {s['title'][:24]}", f"admin_src_del:{s['id']}")])
    rows.append([("◀️ Назад", "admin_sources")])
    await safe_edit(call, "<b>⚙️ Управление источниками</b>", mkb(rows))

@admin_router.callback_query(F.data.startswith("admin_src_toggle:"))
async def admin_src_toggle_cb(call: CallbackQuery):
    if not is_admin(call.from_user.id): return
    _db.toggle_source(int(call.data.split(":")[1]))
    await admin_src_manage_cb(call)

@admin_router.callback_query(F.data.startswith("admin_src_del:"))
async def admin_src_del_cb(call: CallbackQuery):
    if not is_admin(call.from_user.id): return
    _db.delete_source(int(call.data.split(":")[1]))
    await call.answer("Удалено"); await admin_src_manage_cb(call)

@admin_router.callback_query(F.data == "admin_src_export")
async def admin_src_export_cb(call: CallbackQuery):
    if not is_admin(call.from_user.id): return
    sources = _db.get_sources(active_only=False)
    lines   = [f"{'[ON]' if s['active'] else '[OFF]'} {s['title']} | {s['chat_id']} | {s.get('link','')}" for s in sources]
    await call.message.answer_document(
        document=BufferedInputFile("\n".join(lines).encode("utf-8"), filename="sources.txt"),
        caption="📤 Список источников")
    await call.answer()

# ═══════════════════════════════════════════════════════════════
# ADMIN — ОТКЛИКИ
# ═══════════════════════════════════════════════════════════════

REPLIES_PER_PAGE = 8

@admin_router.callback_query(F.data == "admin_replies")
async def admin_replies_cb(call: CallbackQuery):
    if not is_admin(call.from_user.id): return
    await _show_replies(call, 0)

@admin_router.callback_query(F.data.startswith("admin_replies_page:"))
async def admin_replies_page_cb(call: CallbackQuery):
    if not is_admin(call.from_user.id): return
    await _show_replies(call, int(call.data.split(":")[1]))

async def _show_replies(call: CallbackQuery, page: int) -> None:
    total   = _db.count_replies()
    offset  = page * REPLIES_PER_PAGE
    replies = _db.get_replies(limit=REPLIES_PER_PAGE, offset=offset)
    rows    = []
    for r in replies:
        num  = f"#{r['template_id']}.{r['variant_num']}"
        src  = (r.get("source_title") or "?")[:20]
        icon = "🗑 " if r["deleted"] else ""
        rows.append([(f"{icon}{num} — {src}", f"admin_reply_view:{r['id']}")])
    nav   = []
    pages = max(1, (total + REPLIES_PER_PAGE - 1) // REPLIES_PER_PAGE)
    if page > 0: nav.append(("◀️", f"admin_replies_page:{page-1}"))
    nav.append((f"{page+1}/{pages}", "noop"))
    if offset + REPLIES_PER_PAGE < total: nav.append(("▶️", f"admin_replies_page:{page+1}"))
    if nav: rows.append(nav)
    rows.append([("📋 Шаблоны", "admin_templates"), ("◀️ Назад", "admin_main")])
    await safe_edit(call, f"<b>✍️ Отклики</b> (всего: {total})", mkb(rows))

@admin_router.callback_query(F.data.startswith("admin_reply_view:"))
async def admin_reply_view_cb(call: CallbackQuery):
    if not is_admin(call.from_user.id): return
    reply_id = int(call.data.split(":")[1])
    r        = _db.get_reply(reply_id)
    if not r: await call.answer("Не найден"); return
    num    = f"{r['template_id']}.{r['variant_num']}"
    status = "🗑 Удалён" if r["deleted"] else "✅ Активен"
    short  = (r.get("vacancy_text") or "")[:300]
    text   = (
        f"<b>Отклик #{num}</b>  {status}\n\n"
        f"<b>Источник:</b> {r.get('source_title') or '—'}\n"
        f"<b>Контакт:</b> <code>{r.get('ds_contact') or '—'}</code>\n"
        f"<a href='{r.get('message_link') or ''}'>К вакансии</a>\n\n"
        f"<blockquote expandable>{short}</blockquote>\n\n"
        f"<b>Текст отклика:</b>\n{r['text_sent']}"
    )
    markup = mkb([
        [("⚠️ Ошибка", f"admin_error:{reply_id}"), ("🗑 Удалить", f"admin_delete_reply:{reply_id}")],
        [("◀️ Назад", "admin_replies")],
    ])
    await safe_edit(call, text, markup)

@admin_router.callback_query(F.data == "noop")
async def noop_cb(call: CallbackQuery): await call.answer()

# ═══════════════════════════════════════════════════════════════
# ADMIN — ШАБЛОНЫ
# ═══════════════════════════════════════════════════════════════

@admin_router.callback_query(F.data == "admin_templates")
async def admin_templates_cb(call: CallbackQuery):
    if not is_admin(call.from_user.id): return
    templates = _db.get_templates()
    lines = [f"{'✅' if t['active'] else '❌'} <b>#{t['id']}</b> {t['name']}" for t in templates]
    rows  = [[("➕ Добавить шаблон", "admin_tmpl_add")]]
    for t in templates:
        rows.append([(f"{'✅' if t['active'] else '❌'} #{t['id']} {t['name']}", f"admin_tmpl:{t['id']}")])
    rows.append([("◀️ Назад", "admin_replies")])
    text = "<b>📋 Шаблоны откликов</b>\n\n" + ("\n".join(lines) if lines else "<i>Нет шаблонов</i>")
    await safe_edit(call, text, mkb(rows))

@admin_router.callback_query(F.data.startswith("admin_tmpl:"))
async def admin_tmpl_detail_cb(call: CallbackQuery):
    if not is_admin(call.from_user.id): return
    tid = int(call.data.split(":")[1]); t = _db.get_template(tid)
    if not t: await call.answer("Не найден"); return
    text = (
        f"<b>Шаблон #{t['id']} — {t['name']}</b>\n"
        f"Статус: {'✅ Активен' if t['active'] else '❌ Отключён'}\n\n"
        f"<b>Вариант 1:</b>\n{t['variant1']}\n\n"
        f"<b>Вариант 2:</b>\n{t['variant2']}\n\n"
        f"<b>Вариант 3:</b>\n{t['variant3']}"
    )
    markup = mkb([
        [("✏️ Вариант 1", f"admin_tmpl_edit:{tid}:1"),
         ("✏️ Вариант 2", f"admin_tmpl_edit:{tid}:2"),
         ("✏️ Вариант 3", f"admin_tmpl_edit:{tid}:3")],
        [("👁 Превью", f"admin_tmpl_preview:{tid}"), ("🔄 Вкл/Выкл", f"admin_tmpl_toggle:{tid}")],
        [("🗑 Удалить", f"admin_tmpl_delete:{tid}")],
        [("◀️ Назад", "admin_templates")],
    ])
    await safe_edit(call, text, markup)

@admin_router.callback_query(F.data.startswith("admin_tmpl_edit:"))
async def admin_tmpl_edit_cb(call: CallbackQuery):
    if not is_admin(call.from_user.id): return
    _, tid, vnum = call.data.split(":")
    _admin_pending[call.from_user.id] = f"edit_template:{tid}:{vnum}"
    await safe_edit(call, f"✏️ Введите новый текст для <b>Варианта {vnum}</b> шаблона #{tid}:",
                    kb_back(f"admin_tmpl:{tid}"))

@admin_router.callback_query(F.data.startswith("admin_tmpl_toggle:"))
async def admin_tmpl_toggle_cb(call: CallbackQuery):
    if not is_admin(call.from_user.id): return
    _db.toggle_template(int(call.data.split(":")[1])); await admin_tmpl_detail_cb(call)

@admin_router.callback_query(F.data.startswith("admin_tmpl_delete:"))
async def admin_tmpl_delete_cb(call: CallbackQuery):
    if not is_admin(call.from_user.id): return
    tid = int(call.data.split(":")[1]); _db.delete_template(tid)
    await call.answer(f"Шаблон #{tid} удалён"); await admin_templates_cb(call)

@admin_router.callback_query(F.data.startswith("admin_tmpl_preview:"))
async def admin_tmpl_preview_cb(call: CallbackQuery):
    if not is_admin(call.from_user.id): return
    tid = int(call.data.split(":")[1]); t = _db.get_template(tid)
    if not t: return
    v = random.randint(1, 3)
    await call.message.answer(f"<b>👁 Превью — #{tid}.{v}:</b>\n\n{t[f'variant{v}']}", parse_mode=ParseMode.HTML)
    await call.answer()

@admin_router.callback_query(F.data == "admin_tmpl_add")
async def admin_tmpl_add_cb(call: CallbackQuery):
    if not is_admin(call.from_user.id): return
    _admin_pending[call.from_user.id] = "add_template"
    await safe_edit(call,
        "➕ <b>Добавление шаблона</b>\n\n"
        "Отправьте в формате:\n"
        "<code>Название\n---\nВариант 1\n---\nВариант 2\n---\nВариант 3</code>",
        kb_back("admin_templates"))

# ═══════════════════════════════════════════════════════════════
# ADMIN — МОНИТОРИНГ
# ═══════════════════════════════════════════════════════════════

@admin_router.callback_query(F.data == "admin_monitoring")
async def admin_monitoring_cb(call: CallbackQuery):
    if not is_admin(call.from_user.id): return
    kw_c = _db.get_keywords("common"); kw_a = _db.get_keywords("admin")
    bl_c = _db.get_blacklist("common"); bl_a = _db.get_blacklist("admin")
    text = (
        f"<b>🔍 Мониторинг</b>\n\n"
        f"🔑 Ключевые слова общие: <b>{len(kw_c)}</b>\n"
        f"🔑 Ключевые слова (мои): <b>{len(kw_a)}</b>\n\n"
        f"🚫 Стоп-слова общие: <b>{len(bl_c)}</b>\n"
        f"🚫 Стоп-слова (мои): <b>{len(bl_a)}</b>"
    )
    markup = mkb([
        [("🔑 Ключевые слова", "admin_kw"), ("🚫 Чёрный список", "admin_bl")],
        [("🤖 DeepSeek", "admin_deepseek")],
        [("◀️ Назад", "admin_main")],
    ])
    await safe_edit(call, text, markup)

@admin_router.callback_query(F.data == "admin_kw")
async def admin_kw_cb(call: CallbackQuery):
    if not is_admin(call.from_user.id): return
    kw_c = _db.get_keywords("common"); kw_a = _db.get_keywords("admin")
    text = (
        f"<b>🔑 Ключевые слова</b>\n\n"
        f"<b>Общие ({len(kw_c)}):</b>\n" + (", ".join(f"<code>{w}</code>" for w in kw_c) or "<i>нет</i>") +
        f"\n\n<b>Мои ({len(kw_a)}):</b>\n" + (", ".join(f"<code>{w}</code>" for w in kw_a) or "<i>нет</i>")
    )
    markup = mkb([
        [("➕ Общее",  "admin_kw_add:common"), ("➕ Моё",  "admin_kw_add:admin")],
        [("➖ Общее",  "admin_kw_del:common"), ("➖ Моё",  "admin_kw_del:admin")],
        [("◀️ Назад", "admin_monitoring")],
    ])
    await safe_edit(call, text, markup)

@admin_router.callback_query(F.data.startswith("admin_kw_add:"))
async def admin_kw_add_cb(call: CallbackQuery):
    if not is_admin(call.from_user.id): return
    ktype = call.data.split(":")[1]; _admin_pending[call.from_user.id] = f"add_kw:{ktype}"
    await safe_edit(call, f"➕ Введите ключевое слово (<i>{ktype}</i>):", kb_back("admin_kw"))

@admin_router.callback_query(F.data.startswith("admin_kw_del:"))
async def admin_kw_del_cb(call: CallbackQuery):
    if not is_admin(call.from_user.id): return
    ktype = call.data.split(":")[1]; words = _db.get_keywords(ktype)
    if not words: await call.answer("Список пуст"); return
    rows = [[("❌ " + w, f"admin_kw_del_ok:{ktype}:{w}")] for w in words]
    rows.append([("◀️ Назад", "admin_kw")]); await safe_edit(call, f"➖ Удалить (<i>{ktype}</i>):", mkb(rows))

@admin_router.callback_query(F.data.startswith("admin_kw_del_ok:"))
async def admin_kw_del_ok_cb(call: CallbackQuery):
    if not is_admin(call.from_user.id): return
    _, ktype, word = call.data.split(":", 2); _db.delete_keyword(word, ktype)
    await call.answer(f"Удалено: {word}"); await admin_kw_cb(call)

@admin_router.callback_query(F.data == "admin_bl")
async def admin_bl_cb(call: CallbackQuery):
    if not is_admin(call.from_user.id): return
    bl_c = _db.get_blacklist("common"); bl_a = _db.get_blacklist("admin")
    text = (
        f"<b>🚫 Чёрный список</b>\n\n"
        f"<b>Общий ({len(bl_c)}):</b>\n" + (", ".join(f"<code>{w}</code>" for w in bl_c) or "<i>нет</i>") +
        f"\n\n<b>Мой ({len(bl_a)}):</b>\n" + (", ".join(f"<code>{w}</code>" for w in bl_a) or "<i>нет</i>")
    )
    markup = mkb([
        [("➕ Общее",  "admin_bl_add:common"), ("➕ Моё",  "admin_bl_add:admin")],
        [("➖ Общее",  "admin_bl_del:common"), ("➖ Моё",  "admin_bl_del:admin")],
        [("◀️ Назад", "admin_monitoring")],
    ])
    await safe_edit(call, text, markup)

@admin_router.callback_query(F.data.startswith("admin_bl_add:"))
async def admin_bl_add_cb(call: CallbackQuery):
    if not is_admin(call.from_user.id): return
    btype = call.data.split(":")[1]; _admin_pending[call.from_user.id] = f"add_bl:{btype}"
    await safe_edit(call, f"➕ Введите стоп-слово (<i>{btype}</i>):", kb_back("admin_bl"))

@admin_router.callback_query(F.data.startswith("admin_bl_del:"))
async def admin_bl_del_cb(call: CallbackQuery):
    if not is_admin(call.from_user.id): return
    btype = call.data.split(":")[1]; words = _db.get_blacklist(btype)
    if not words: await call.answer("Список пуст"); return
    rows = [[("❌ " + w, f"admin_bl_del_ok:{btype}:{w}")] for w in words]
    rows.append([("◀️ Назад", "admin_bl")]); await safe_edit(call, f"➖ Удалить (<i>{btype}</i>):", mkb(rows))

@admin_router.callback_query(F.data.startswith("admin_bl_del_ok:"))
async def admin_bl_del_ok_cb(call: CallbackQuery):
    if not is_admin(call.from_user.id): return
    _, btype, word = call.data.split(":", 2); _db.delete_from_blacklist(word, btype)
    await call.answer(f"Удалено: {word}"); await admin_bl_cb(call)

# ═══════════════════════════════════════════════════════════════
# ADMIN — DEEPSEEK
# ═══════════════════════════════════════════════════════════════

@admin_router.callback_query(F.data == "admin_deepseek")
async def admin_deepseek_cb(call: CallbackQuery):
    if not is_admin(call.from_user.id): return
    tok = _db.get_tokens_today(); rules = _db.get_ds_rules()
    text = (
        f"<b>🤖 DeepSeek</b>\n\n"
        f"Модель: <code>{DEEPSEEK_MODEL}</code>\n"
        f"Правил: <b>{len(rules)}</b>\n"
        f"Токены сегодня: ↑<code>{tok['tokens_in']}</code> ↓<code>{tok['tokens_out']}</code>"
    )
    markup = mkb([
        [("📋 Системный промпт", "admin_ds_prompt"), ("✏️ Изменить", "admin_ds_prompt_edit")],
        [("📏 Правила", "admin_ds_rules"), ("➕ Добавить правило", "admin_ds_rule_add")],
        [("◀️ Назад", "admin_monitoring")],
    ])
    await safe_edit(call, text, markup)

@admin_router.callback_query(F.data == "admin_ds_prompt")
async def admin_ds_prompt_cb(call: CallbackQuery):
    if not is_admin(call.from_user.id): return
    prompt = _db.get_setting("ds_system_prompt")
    await safe_edit(call, f"<b>📋 Системный промпт:</b>\n\n<blockquote>{prompt}</blockquote>",
                    kb_back("admin_deepseek"))

@admin_router.callback_query(F.data == "admin_ds_prompt_edit")
async def admin_ds_prompt_edit_cb(call: CallbackQuery):
    if not is_admin(call.from_user.id): return
    _admin_pending[call.from_user.id] = "edit_ds_prompt"
    await safe_edit(call, "✏️ Введите новый системный промпт:", kb_back("admin_deepseek"))

@admin_router.callback_query(F.data == "admin_ds_rules")
async def admin_ds_rules_cb(call: CallbackQuery):
    if not is_admin(call.from_user.id): return
    rules = _db.get_ds_rules()
    lines = [f"<b>{i+1}.</b> {r['value']}" for i, r in enumerate(rules)]
    text  = "<b>📏 Глобальные правила</b>\n\n" + ("\n".join(lines) if lines else "<i>Нет правил</i>")
    rows  = [[("❌ " + r["value"][:35], f"admin_ds_rule_del:{r['key']}")] for r in rules]
    rows.append([("➕ Добавить", "admin_ds_rule_add"), ("◀️ Назад", "admin_deepseek")])
    await safe_edit(call, text, mkb(rows))

@admin_router.callback_query(F.data == "admin_ds_rule_add")
async def admin_ds_rule_add_cb(call: CallbackQuery):
    if not is_admin(call.from_user.id): return
    _admin_pending[call.from_user.id] = "add_ds_rule"
    await safe_edit(call,
        "➕ <b>Добавить правило DeepSeek</b>\n\n"
        "<b>Рекомендации:</b>\n"
        "• Одно правило — одно условие\n"
        "• Пример: «Если упоминается резюме — suitable=false»\n"
        "• Пишите однозначно и кратко\n\n"
        "Введите правило:", kb_back("admin_ds_rules"))

@admin_router.callback_query(F.data.startswith("admin_ds_rule_del:"))
async def admin_ds_rule_del_cb(call: CallbackQuery):
    if not is_admin(call.from_user.id): return
    key = call.data.split(":", 1)[1]; _db.del_setting(key)
    await call.answer("Правило удалено"); await admin_ds_rules_cb(call)

# ═══════════════════════════════════════════════════════════════
# ADMIN — СТАТИСТИКА
# ═══════════════════════════════════════════════════════════════

@admin_router.callback_query(F.data == "admin_stats")
async def admin_stats_cb(call: CallbackQuery):
    if not is_admin(call.from_user.id): return
    s = _db.get_stats()
    text = (
        f"<b>📊 Статистика</b>\n\n"
        f"📡 Источников: <b>{s['sources']}</b>\n\n"
        f"📋 Вакансий найдено: <b>{s['total_vac']}</b>\n"
        f"✅ Подходящих: <b>{s['suitable_vac']}</b>\n"
        f"❌ Отклонённых: <b>{s['rejected_vac']}</b>\n\n"
        f"📤 Откликов: <b>{s['replies']}</b>\n\n"
        f"👥 Клиентов: <b>{s['clients']}</b>\n"
        f"💎 С подпиской: <b>{s['active_subs']}</b>\n\n"
        f"🤖 Токены сегодня:\n"
        f"  ↑ <code>{s['tokens_in']}</code>  ↓ <code>{s['tokens_out']}</code>"
    )
    await safe_edit(call, text, kb_back("admin_main"))

# ═══════════════════════════════════════════════════════════════
# ADMIN — КЛИЕНТЫ
# ═══════════════════════════════════════════════════════════════

@admin_router.callback_query(F.data == "admin_clients")
async def admin_clients_cb(call: CallbackQuery):
    if not is_admin(call.from_user.id): return
    clients = _db.get_all_clients(); now = datetime.now().isoformat()
    lines   = []
    for c in clients[:20]:
        icon  = "💎" if (c.get("sub_until") and c["sub_until"] > now) else "👤"
        until = c["sub_until"][:10] if c.get("sub_until") else "нет"
        lines.append(f"{icon} <code>{c['tg_id']}</code> @{c.get('username') or '—'} | до {until}")
    text   = f"<b>👥 Клиенты</b> (всего: {len(clients)})\n\n" + ("\n".join(lines) if lines else "<i>Нет клиентов</i>")
    markup = mkb([
        [("➕ Выдать подписку", "admin_give_sub"), ("📤 Рассылка", "admin_broadcast")],
        [("◀️ Назад", "admin_main")],
    ])
    await safe_edit(call, text, markup)

@admin_router.callback_query(F.data == "admin_give_sub")
async def admin_give_sub_cb(call: CallbackQuery):
    if not is_admin(call.from_user.id): return
    _admin_pending[call.from_user.id] = "give_sub"
    await safe_edit(call,
        "➕ <b>Выдача подписки</b>\n\n"
        "Введите: <code>TG_ID количество_дней</code>\n"
        "Пример: <code>123456789 30</code>",
        kb_back("admin_clients"))

@admin_router.callback_query(F.data == "admin_broadcast")
async def admin_broadcast_cb(call: CallbackQuery):
    if not is_admin(call.from_user.id): return
    _admin_pending[call.from_user.id] = "broadcast"
    await safe_edit(call, "📤 <b>Рассылка клиентам</b>\n\nВведите текст (HTML):", kb_back("admin_clients"))

# ═══════════════════════════════════════════════════════════════
# ADMIN — ЛОГИ
# ═══════════════════════════════════════════════════════════════

@admin_router.callback_query(F.data == "admin_logs")
async def admin_logs_cb(call: CallbackQuery):
    if not is_admin(call.from_user.id): return
    today = datetime.now().strftime("%Y-%m-%d")
    yest  = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    markup = mkb([
        [(f"📅 Сегодня ({today})", f"admin_logs_date:{today}")],
        [(f"📅 Вчера ({yest})",    f"admin_logs_date:{yest}")],
        [("◀️ Назад", "admin_main")],
    ])
    await safe_edit(call, "<b>📜 Логи</b>\n\nВыберите дату:", markup)

@admin_router.callback_query(F.data.startswith("admin_logs_date:"))
async def admin_logs_date_cb(call: CallbackQuery):
    if not is_admin(call.from_user.id): return
    date = call.data.split(":")[1]; logs = _db.get_logs(date, limit=50)
    lines = [f"<code>{l['ts'][11:19]}</code> [{l['level'][:4]}] {l['message'][:80]}" for l in logs]
    text  = f"<b>📜 Логи за {date}</b> (последние {len(logs)}):\n\n" + ("\n".join(lines) if lines else "<i>Нет записей</i>")
    markup = mkb([[("📤 Экспорт", f"admin_logs_export:{date}")], [("◀️ Назад", "admin_logs")]])
    await safe_edit(call, text[:4000], markup)

@admin_router.callback_query(F.data.startswith("admin_logs_export:"))
async def admin_logs_export_cb(call: CallbackQuery):
    if not is_admin(call.from_user.id): return
    date = call.data.split(":")[1]
    await call.message.answer_document(
        document=BufferedInputFile(_db.export_logs(date), filename=f"logs_{date}.txt"),
        caption=f"📜 Логи за {date}")
    await call.answer()

# ═══════════════════════════════════════════════════════════════
# ADMIN — НАСТРОЙКИ
# ═══════════════════════════════════════════════════════════════

@admin_router.callback_query(F.data == "admin_settings")
async def admin_settings_cb(call: CallbackQuery):
    if not is_admin(call.from_user.id): return
    await safe_edit(call, "<b>⚙️ Настройки</b>", mkb([[("◀️ Назад", "admin_main")]]))

# ═══════════════════════════════════════════════════════════════
# ADMIN — ОШИБКИ В ОТКЛИКЕ / УДАЛЕНИЕ
# ═══════════════════════════════════════════════════════════════

@admin_router.callback_query(F.data.startswith("admin_error:"))
async def admin_error_cb(call: CallbackQuery):
    if not is_admin(call.from_user.id): return
    reply_id = call.data.split(":")[1]
    markup = mkb([
        [("🔑 Ключевые слова", f"admin_err_kw:{reply_id}"),
         ("🚫 Чёрный список",   f"admin_err_bl:{reply_id}")],
        [("🤖 DeepSeek",        f"admin_err_ds:{reply_id}")],
        [("◀️ Назад",           f"admin_reply_view:{reply_id}")],
    ])
    await safe_edit(call, "⚠️ <b>Сообщить об ошибке</b>\n\nВыберите категорию:", markup)

@admin_router.callback_query(F.data.startswith("admin_err_kw:"))
async def admin_err_kw_cb(call: CallbackQuery):
    if not is_admin(call.from_user.id): return
    _admin_pending[call.from_user.id] = "add_kw:common"
    await safe_edit(call, "🔑 Введите слово для добавления в ключевые слова:", kb_back("admin_kw"))

@admin_router.callback_query(F.data.startswith("admin_err_bl:"))
async def admin_err_bl_cb(call: CallbackQuery):
    if not is_admin(call.from_user.id): return
    _admin_pending[call.from_user.id] = "add_bl:common"
    await safe_edit(call, "🚫 Введите слово для чёрного списка:", kb_back("admin_bl"))

@admin_router.callback_query(F.data.startswith("admin_err_ds:"))
async def admin_err_ds_cb(call: CallbackQuery):
    if not is_admin(call.from_user.id): return
    _admin_pending[call.from_user.id] = "add_ds_rule"
    await safe_edit(call, "🤖 Введите правило для DeepSeek:", kb_back("admin_deepseek"))

@admin_router.callback_query(F.data.startswith("admin_delete_reply:"))
async def admin_delete_reply_cb(call: CallbackQuery):
    if not is_admin(call.from_user.id): return
    reply_id = int(call.data.split(":")[1]); reply = _db.get_reply(reply_id)
    if not reply: await call.answer("Не найден"); return
    num = f"{reply['template_id']}.{reply['variant_num']}"
    if reply.get("tg_message_id") and reply.get("ds_contact"):
        try:
            await _userbot.delete_messages(reply["ds_contact"], [reply["tg_message_id"]])
            log.info(f"Отклик удалён из диалога с {reply['ds_contact']}")
        except Exception as e: log.warning(f"Удаление сообщения: {e}")
    _db.mark_reply_deleted(reply_id)
    await safe_edit(call, f"🗑 <b>Удалено! #<code>{num}</code></b>", None)
# ═══════════════════════════════════════════════════════════════
# ADMIN — ОБРАБОТЧИК ТЕКСТОВЫХ СООБЩЕНИЙ
# ═══════════════════════════════════════════════════════════════

@admin_router.message(F.text)
async def admin_text_handler(message: Message):
    if not is_admin(message.from_user.id): return
    uid    = message.from_user.id
    action = _admin_pending.pop(uid, None)
    if not action: return
    text = message.text.strip()

    # ── Авторизация UserBot: телефон ──────────────────────────
    if action == "userbot_auth_phone":
        phone = text if text.startswith("+") else "+" + text
        try:
            result = await _auth_state["client"].send_code_request(phone)
            _auth_state["phone"]           = phone
            _auth_state["phone_code_hash"] = result.phone_code_hash
            _admin_pending[uid] = "userbot_auth_code"
            await message.answer(
                f"📱 Код отправлен на <code>{phone}</code>\n\n"
                "Введите код из Telegram (можно с пробелами: <code>1 2 3 4 5</code>):",
                reply_markup=kb_back("admin_main"))
        except Exception as e:
            await message.answer(f"❌ Ошибка: <code>{e}</code>\n\nПопробуйте снова — введите номер:")
            _admin_pending[uid] = "userbot_auth_phone"
        return

    # ── Авторизация UserBot: код ──────────────────────────────
    if action == "userbot_auth_code":
        code = text.replace(" ", "")
        try:
            await _auth_state["client"].sign_in(
                phone=_auth_state["phone"], code=code,
                phone_code_hash=_auth_state["phone_code_hash"])
            await _finish_userbot_auth(message)
        except SessionPasswordNeededError:
            _admin_pending[uid] = "userbot_auth_2fa"
            await message.answer("🔐 Нужен пароль 2FA. Введите его:")
        except PhoneCodeInvalidError:
            _admin_pending[uid] = "userbot_auth_code"
            await message.answer("❌ Неверный код. Попробуйте ещё раз:")
        except PhoneCodeExpiredError:
            _admin_pending[uid] = "userbot_auth_phone"
            await message.answer("❌ Код устарел. Введите номер заново:")
        except Exception as e:
            await message.answer(f"❌ Ошибка: <code>{e}</code>")
        return

    # ── Авторизация UserBot: 2FA ──────────────────────────────
    if action == "userbot_auth_2fa":
        try:
            await _auth_state["client"].sign_in(password=text)
            await _finish_userbot_auth(message)
        except PasswordHashInvalidError:
            _admin_pending[uid] = "userbot_auth_2fa"
            await message.answer("❌ Неверный пароль 2FA. Попробуйте ещё раз:")
        except Exception as e:
            await message.answer(f"❌ Ошибка: <code>{e}</code>")
        return

    # ── Добавление источника ──────────────────────────────────
    if action == "add_source":
        raw = text.replace("https://t.me/", "").lstrip("@").strip()
        try:
            entity  = await _userbot.get_entity(raw)
            chat_id = entity.id
            title   = getattr(entity, "title", raw)
            uname   = getattr(entity, "username", None)
            link    = f"https://t.me/{uname}" if uname else None
            _db.add_source(chat_id, title, uname, link)
            await message.answer(
                f"✅ Источник добавлен:\n<b>{title}</b> (<code>{chat_id}</code>)",
                reply_markup=kb_back("admin_sources"))
        except Exception as e:
            await message.answer(f"❌ Ошибка: {e}", reply_markup=kb_back("admin_sources"))
        return

    # ── Добавление шаблона ────────────────────────────────────
    if action == "add_template":
        parts = text.split("---")
        if len(parts) != 4:
            await message.answer("❌ Нужны 4 блока через <code>---</code>. Попробуйте снова:")
            _admin_pending[uid] = "add_template"; return
        name, v1, v2, v3 = [p.strip() for p in parts]
        tid = _db.add_template(name, v1, v2, v3)
        await message.answer(f"✅ Шаблон <b>#{tid} {name}</b> добавлен", reply_markup=kb_back("admin_templates"))
        return

    # ── Редактирование варианта шаблона ──────────────────────
    if action.startswith("edit_template:"):
        _, tid, vnum = action.split(":")
        _db.update_template_variant(int(tid), int(vnum), text)
        await message.answer(f"✅ Вариант {vnum} шаблона #{tid} обновлён", reply_markup=kb_back("admin_templates"))
        return

    # ── Добавление ключевого слова ────────────────────────────
    if action.startswith("add_kw:"):
        ktype = action.split(":")[1]; _db.add_keyword(text, ktype)
        await message.answer(f"✅ Ключевое слово: <code>{text}</code>", reply_markup=kb_back("admin_kw"))
        return

    # ── Добавление стоп-слова ─────────────────────────────────
    if action.startswith("add_bl:"):
        btype = action.split(":")[1]; _db.add_to_blacklist(text, btype)
        await message.answer(f"✅ Стоп-слово: <code>{text}</code>", reply_markup=kb_back("admin_bl"))
        return

    # ── Системный промпт ──────────────────────────────────────
    if action == "edit_ds_prompt":
        _db.set_setting("ds_system_prompt", text)
        await message.answer("✅ Системный промпт обновлён", reply_markup=kb_back("admin_deepseek"))
        return

    # ── Добавление правила DeepSeek ───────────────────────────
    if action == "add_ds_rule":
        key = f"ds_rule_{int(time.time())}"; _db.set_setting(key, text)
        await message.answer(f"✅ Правило добавлено: <code>{text}</code>", reply_markup=kb_back("admin_ds_rules"))
        return

    # ── Выдача подписки ───────────────────────────────────────
    if action == "give_sub":
        parts = text.split()
        if len(parts) != 2 or not all(p.lstrip("-").isdigit() for p in parts):
            await message.answer("❌ Формат: <code>TG_ID дней</code>")
            _admin_pending[uid] = "give_sub"; return
        tg_id = int(parts[0]); days = int(parts[1])
        cl    = _db.get_or_create_client(tg_id, None)
        until = datetime.now() + timedelta(days=days)
        _db.set_subscription(cl["id"], until)
        await message.answer(
            f"✅ Подписка выдана:\n<code>{tg_id}</code> до <b>{until.strftime('%d.%m.%Y')}</b>",
            reply_markup=kb_back("admin_clients"))
        return

    # ── Рассылка ──────────────────────────────────────────────
    if action == "broadcast":
        clients = _db.get_active_clients(); sent = 0
        for cl in clients:
            try:
                await message.bot.send_message(cl["tg_id"], text, parse_mode=ParseMode.HTML)
                sent += 1; await asyncio.sleep(0.05)
            except Exception as e: log.warning(f"Рассылка {cl['tg_id']}: {e}")
        await message.answer(
            f"✅ Рассылка завершена: <b>{sent}/{len(clients)}</b>",
            reply_markup=kb_back("admin_clients"))
        return

# ── Завершение авторизации UserBot ────────────────────────────

async def _finish_userbot_auth(message: Message) -> None:
    global _userbot
    client = _auth_state.get("client")
    if not client: return
    me          = await client.get_me()
    session_str = client.session.save()
    log.info(f"UserBot авторизован: @{me.username} ({me.id})")
    _db.set_setting("string_session", session_str)
    _userbot = client
    if _pipeline:
        _pipeline.userbot = client
        register_userbot_handlers(client, _pipeline)
    await message.answer(
        f"✅ <b>UserBot авторизован!</b>\n\n"
        f"Аккаунт: @{me.username} (<code>{me.id}</code>)\n\n"
        f"Строка сессии сохранена в БД.\n"
        f"Добавь в .env:\n<code>STRING_SESSION={session_str}</code>",
        reply_markup=kb_admin_main())

# ═══════════════════════════════════════════════════════════════
# CLIENT BOT
# ═══════════════════════════════════════════════════════════════

@client_router.message(Command("start"))
async def client_start(message: Message):
    uid    = message.from_user.id
    uname  = message.from_user.username
    cl     = _db.get_or_create_client(uid, uname)
    is_sub = _db.is_subscribed(uid)
    if is_sub:
        until  = cl.get("sub_until", "")[:10] if cl.get("sub_until") else "—"
        text   = (
            f"👋 Привет, <b>{message.from_user.first_name}</b>!\n\n"
            f"💎 Подписка активна до: <b>{until}</b>\n\n"
            f"Вы получаете уведомления о новых вакансиях.")
        markup = mkb([[("🚫 Мои стоп-слова", "client_stopwords")]])
    else:
        text   = (
            f"👋 Привет, <b>{message.from_user.first_name}</b>!\n\n"
            f"У вас нет активной подписки.\n"
            f"Оформите подписку чтобы получать вакансии.")
        markup = mkb([[("💳 Тарифы", "client_plans")]])
    await message.answer(text, reply_markup=markup)

@client_router.callback_query(F.data == "client_plans")
async def client_plans_cb(call: CallbackQuery):
    await safe_edit(call,
        "<b>💳 Тарифы</b>\n\nДля оформления подписки обратитесь к администратору.",
        mkb([[("🔄 Проверить подписку", "client_check_sub")]]))

@client_router.callback_query(F.data == "client_check_sub")
async def client_check_sub_cb(call: CallbackQuery):
    uid    = call.from_user.id
    cl     = _db.get_or_create_client(uid, call.from_user.username)
    is_sub = _db.is_subscribed(uid)
    if is_sub:
        until = cl.get("sub_until", "")[:10]
        await safe_edit(call, f"✅ <b>Подписка активна!</b>\nДо: <b>{until}</b>",
                        mkb([[("🚫 Мои стоп-слова", "client_stopwords")]]))
    else:
        await safe_edit(call, "❌ Подписка не активна.", mkb([[("💳 Тарифы", "client_plans")]]))

@client_router.callback_query(F.data == "client_stopwords")
async def client_stopwords_cb(call: CallbackQuery):
    uid  = call.from_user.id; cl = _db.get_client_by_tg(uid)
    if not cl: await call.answer("Профиль не найден"); return
    words = _db.get_client_stopwords(cl["id"])
    text  = (
        "<b>🚫 Мои стоп-слова</b>\n\n"
        "Вакансии с этими словами не будут вам приходить.\n\n" +
        (", ".join(f"<code>{w}</code>" for w in words) if words else "<i>Список пуст</i>"))
    markup = mkb([
        [("➕ Добавить", "client_sw_add"), ("➖ Удалить", "client_sw_del")],
        [("◀️ Назад", "client_check_sub")],
    ])
    await safe_edit(call, text, markup)

@client_router.callback_query(F.data == "client_sw_add")
async def client_sw_add_cb(call: CallbackQuery):
    _client_pending[call.from_user.id] = "add_sw"
    await safe_edit(call, "➕ Введите слово для вашего списка стоп-слов:", kb_back("client_stopwords"))

@client_router.callback_query(F.data == "client_sw_del")
async def client_sw_del_cb(call: CallbackQuery):
    uid = call.from_user.id; cl = _db.get_client_by_tg(uid)
    if not cl: return
    words = _db.get_client_stopwords(cl["id"])
    if not words: await call.answer("Список пуст"); return
    rows  = [[("❌ " + w, f"client_sw_del_ok:{w}")] for w in words]
    rows.append([("◀️ Назад", "client_stopwords")])
    await safe_edit(call, "➖ Выберите слово для удаления:", mkb(rows))

@client_router.callback_query(F.data.startswith("client_sw_del_ok:"))
async def client_sw_del_ok_cb(call: CallbackQuery):
    uid  = call.from_user.id; word = call.data.split(":", 1)[1]
    cl   = _db.get_client_by_tg(uid)
    if cl: _db.delete_client_stopword(cl["id"], word)
    await call.answer(f"Удалено: {word}"); await client_stopwords_cb(call)

@client_router.callback_query(F.data.startswith("open_contact:"))
async def open_contact_cb(call: CallbackQuery):
    parts = call.data.split(":"); vid = int(parts[1]); uid = call.from_user.id
    if not _db.is_subscribed(uid):
        await safe_edit(call,
            "❌ <b>Подписка закончилась</b>\n\nОплатите подписку чтобы открыть контакты.",
            mkb([[("💳 Тарифы", "client_plans")]]))
        return
    vacancy = _db.get_vacancy(vid)
    if not vacancy: await call.answer("Вакансия не найдена"); return
    contact  = vacancy.get("ds_contact") or ""
    old_text = call.message.html_text or call.message.text or ""
    new_text = old_text.replace("—", contact) if contact else old_text
    if contact.startswith("@"):
        clean = contact.lstrip("@")
        new_text += f"\n\n<a href='https://t.me/{clean}'>💬 Написать</a>"
    try: await call.message.edit_text(new_text, parse_mode=ParseMode.HTML)
    except TelegramBadRequest: pass
    await call.answer("✅ Контакты открыты")

@client_router.message(F.text)
async def client_text_handler(message: Message):
    uid    = message.from_user.id
    uname  = message.from_user.username
    action = _client_pending.pop(uid, None)
    if action == "add_sw":
        cl = _db.get_or_create_client(uid, uname)
        _db.add_client_stopword(cl["id"], message.text.strip())
        await message.answer(
            f"✅ Добавлено: <code>{message.text.strip()}</code>",
            reply_markup=mkb([[("🚫 Мои стоп-слова", "client_stopwords")]])); return
    cl     = _db.get_or_create_client(uid, uname)
    is_sub = _db.is_subscribed(uid)
    markup = mkb([[("🚫 Мои стоп-слова", "client_stopwords")]]) if is_sub else mkb([[("💳 Тарифы", "client_plans")]])
    await message.answer("Используйте меню:", reply_markup=markup)

# ═══════════════════════════════════════════════════════════════
# USERBOT — регистрация обработчика
# ═══════════════════════════════════════════════════════════════

def register_userbot_handlers(userbot: TelegramClient, pipeline: VacancyPipeline) -> None:
    @userbot.on(events.NewMessage())
    async def _handler(event):
        try:
            sources    = _db.get_sources(active_only=True)
            source_ids = {s["chat_id"] for s in sources}
            cid        = event.chat_id
            if cid not in source_ids and abs(cid) not in source_ids: return
            await pipeline.enqueue(event)
        except Exception as e: log.error(f"UserBot handler: {e}", exc_info=True)
    log.info("UserBot: обработчик зарегистрирован")

# ═══════════════════════════════════════════════════════════════
# АВТОРИЗАЦИЯ USERBOT
# ═══════════════════════════════════════════════════════════════

async def init_userbot(bot: Bot) -> TelegramClient:
    session_str = STRING_SESSION or _db.get_setting("string_session", "")
    session     = StringSession(session_str) if session_str else StringSession()
    client      = TelegramClient(session, API_ID, API_HASH)
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
            "Отправьте номер телефона в формате <code>+79001234567</code>:")
        _admin_pending[ADMIN_ID] = "userbot_auth_phone"
    except Exception as e: log.error(f"Уведомление авторизации: {e}")
    return client

# ═══════════════════════════════════════════════════════════════
# ПЕРИОДИЧЕСКИЕ ЗАДАЧИ
# ═══════════════════════════════════════════════════════════════

async def periodic_cleanup() -> None:
    while True:
        await asyncio.sleep(3600)
        try: _db.cleanup()
        except Exception as e: log.error(f"cleanup: {e}")

# ═══════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════

async def main() -> None:
    global _db, _pipeline, _userbot

    log.info("=" * 60)
    log.info("phase.parser запускается")
    log.info("=" * 60)

    # 1. БД
    _db = Database(DB_PATH); _db.connect(); _db.init_tables()

    # 2. Bot + Dispatcher
    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp  = Dispatcher()
    dp.include_router(admin_router)
    dp.include_router(client_router)

    # 3. UserBot
    _userbot = await init_userbot(bot)

    # 4. Pipeline
    _pipeline = VacancyPipeline(_db, _userbot, bot)

    # 5. Регистрируем обработчик если уже авторизован
    if await _userbot.is_user_authorized():
        register_userbot_handlers(_userbot, _pipeline)

    # 6. Проверка DeepSeek
    ds_ok = await check_deepseek_status()
    log.info(f"DeepSeek: {'✅' if ds_ok else '❌'}")

    # 7. Стартовое сообщение
    try:
        ub_ok = await _userbot.is_user_authorized()
        await bot.send_message(ADMIN_ID,
            f"🚀 <b>phase.parser запущен</b>\n\n"
            f"UserBot:  {'✅' if ub_ok else '❌ требуется авторизация'}\n"
            f"DeepSeek: {'✅' if ds_ok else '❌ проверьте API ключ'}\n\n"
            f"<i>Используйте /start для открытия панели</i>")
    except Exception as e: log.warning(f"Стартовое сообщение: {e}")

    log.info("Запуск задач...")
    await asyncio.gather(
        dp.start_polling(bot, allowed_updates=["message", "callback_query"]),
        _pipeline.run_worker(),
        periodic_cleanup(),
        _userbot.run_until_disconnected(),
    )

if __name__ == "__main__":
    try: asyncio.run(main())
    except KeyboardInterrupt: log.info("phase.parser остановлен")
    except Exception as e: log.critical(f"Критическая ошибка: {e}", exc_info=True)
