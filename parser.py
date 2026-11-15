"""
phase.parser — агрегатор публичных вакансий из Telegram-групп
Telethon (UserBot) + Aiogram 3.x (Bot) + SQLite + DeepSeek API
"""
from __future__ import annotations
import asyncio, difflib, html, json, logging, os, random, re, sqlite3, time
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
from telethon.tl.functions.channels import GetParticipantRequest
from telethon.tl.functions.messages import ImportChatInviteRequest, CheckChatInviteRequest, GetFullChatRequest
from telethon.tl.functions.users import GetFullUserRequest
from telethon.tl.types import (
    MessageMediaDocument, MessageMediaPhoto, MessageMediaWebPage, ChatInviteAlready,
    Chat, ChannelParticipantsAdmins,
)

# ── Конфигурация ──────────────────────────────────────────────
load_dotenv()
_BOT_TOKEN_FALLBACK = "8729193907:AAFn1D41mUgFIIa_pmcUdN37llLSGOwcMg4"
# os.getenv(key, default) подставляет default ТОЛЬКО если переменной нет вовсе —
# если BOT_TOKEN объявлена в окружении (например, на Ботхосте), но пустая
# строка, getenv вернёт именно "", а не запасное значение. "or" ниже добивает
# именно этот случай: пустая/отсутствующая переменная — используем запасной токен.
BOT_TOKEN      = os.getenv("BOT_TOKEN") or _BOT_TOKEN_FALLBACK
API_ID         = int(os.getenv("API_ID", "0"))
API_HASH       = os.getenv("API_HASH", "")
STRING_SESSION = os.getenv("STRING_SESSION", "")
ADMIN_ID       = int(os.getenv("ADMIN_ID", "7605695437"))
# Версия сборки — меняется вручную при каждой правке кода. Показывается в
# главном меню админ-бота и пишется в лог при старте, чтобы можно было
# проверить визуально, что на Ботхосте реально запущена свежая версия после
# пересборки образа (git push сам по себе бота не обновляет).
BOT_VERSION    = "2026-11-15 ads-3"
DEEPSEEK_KEY   = os.getenv("DEEPSEEK_API_KEY", "")
DEEPSEEK_URL   = os.getenv("DEEPSEEK_URL", "https://api.deepseek.com/v1/chat/completions")
DEEPSEEK_MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-v4-flash")
DB_PATH        = os.getenv("DATABASE_PATH", os.getenv("DB_PATH", "parser.db"))
SUPPORT_USERNAME = os.getenv("SUPPORT_USERNAME", "savelimontaj")
PAYMENT_PHONE    = os.getenv("PAYMENT_PHONE", "+79132696007")
PAYMENT_NAME     = os.getenv("PAYMENT_NAME", "Савелий Сергеевич С.")
PAYMENT_BANK     = os.getenv("PAYMENT_BANK", "Озон банк")
CRYPTO_WALLETS   = {
    "trc20": {"address": os.getenv("CRYPTO_WALLET_TRC20", "TN4Aj7mEENEZcyEgNxEf3VthtkvZk2ASSQ"),
              "label": "USDT · TRC20 (Tron)", "icon": "🔺"},
    "ton":   {"address": os.getenv("CRYPTO_WALLET_TON", "UQB2JknVgMhdjHj0ddqEuNoxmDcf8U3k4w6Q0Ldvh6NA4NIV"),
              "label": "USDT · TON", "icon": "💎"},
}
CRYPTO_RATE_BUFFER = float(os.getenv("CRYPTO_RATE_BUFFER", "1.5"))  # % запаса на случай расхождения курса с BingX
MSK = timezone(timedelta(hours=3))  # Москва — фикс. UTC+3, без перехода на летнее/зимнее
FREE_DAYS        = int(os.getenv("FREE_DAYS", "3"))
# Нечёткая дедупликация вакансий: порог похожести текстов (0..1) и глубина
# поиска в днях. 0.9 — ловит репост с правкой смайлика/опечатки/контакта.
DUP_SIMILARITY   = float(os.getenv("DUP_SIMILARITY", "0.9"))
DUP_WINDOW_DAYS  = int(os.getenv("DUP_WINDOW_DAYS", "14"))
REF_DAYS         = int(os.getenv("REF_DAYS", "5"))          # бонус купившему рефералу
REF_BONUS_DAYS   = int(os.getenv("REF_BONUS_DAYS", "15"))   # бонус пригласившему
FREE_NUDGE_THRESHOLD = int(os.getenv("FREE_NUDGE_THRESHOLD", "8"))  # после скольки закрытых вакансий слать разовую скидку никогда не платившим

# Цены: full — обычная цена; sale — цена со скидкой 15% (первая оплата / win-back).
# sale ВСЕГДА считается от full (15%), а не задаётся вручную — чтобы не разъехалось.
# Скидки НЕ суммируются: дешевле, чем full × 0.85, клиент не заплатит ни при каких условиях.
DISCOUNT_PCT = 15
PRICES = {
    "week":   {"label": "1 нед.",  "name": "Неделя",   "days": 7,   "full": 199},
    "month":  {"label": "1 мес.",  "name": "Месяц",    "days": 30,  "full": 499},
    "3month": {"label": "3 мес.",  "name": "3 месяца", "days": 90,  "full": 1199},
}
for _p in PRICES.values():
    _p["sale"] = round(_p["full"] * (100 - DISCOUNT_PCT) / 100)

def _tariff_btn_label(key: str, price: int, fire: str = "") -> str:
    """Текст кнопки тарифа: 'Неделя 199₽ • 28 ₽/день' (цена в день — округлённо)."""
    p = PRICES[key]
    per_day = round(price / p["days"])
    return f"{p['name']} {price}₽{fire} • {per_day} ₽/день"

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
    is_channel_source: bool = False  # источник — канал (не группа): показывать
                                       # "Автор:" бессмысленно, там нет живого автора

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
            CREATE TABLE IF NOT EXISTS chat_authors (
                chat_id INTEGER NOT NULL, sender_id INTEGER NOT NULL,
                username TEXT, name TEXT, last_seen TEXT,
                PRIMARY KEY(chat_id, sender_id));
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
            CREATE TABLE IF NOT EXISTS broadcasts (
                id INTEGER PRIMARY KEY AUTOINCREMENT, text TEXT NOT NULL,
                photo_file_id TEXT, segment TEXT NOT NULL,
                sent_count INTEGER DEFAULT 0, total_count INTEGER DEFAULT 0,
                created_at TEXT DEFAULT (datetime('now')));
            CREATE TABLE IF NOT EXISTS free_pack_files (
                id INTEGER PRIMARY KEY AUTOINCREMENT, file_id TEXT NOT NULL,
                file_name TEXT NOT NULL, added_at TEXT DEFAULT (datetime('now')));
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
        # Миграция: какую рассылку клиент получил последней — чтобы свободное
        # текстовое сообщение от него в чате с ботом можно было связать именно
        # с той рассылкой, на которую он отвечает
        try: self._c().execute("ALTER TABLE clients ADD COLUMN last_broadcast_id INTEGER")
        except sqlite3.OperationalError: pass
        self._c().commit()
        # Миграция: была ли эта конкретная доставка отправлена клиенту как
        # подписчику (полная версия) или как бесплатному (урезанная, "Как
        # откликнуться?") — нужно, чтобы при оформлении подписки апгрейднуть
        # именно старые бесплатные сообщения, а не трогать остальные
        try: self._c().execute("ALTER TABLE client_deliveries ADD COLUMN sent_as_subscribed INTEGER DEFAULT 0")
        except sqlite3.OperationalError: pass
        self._c().commit()
        # Миграция: счётчик вакансий с закрытым контактом, увиденных клиентом
        # без подписки — для воронки конверсии (напоминание с накопленным
        # числом пропущенного, разовый пуш со скидкой после N вакансий)
        try: self._c().execute("ALTER TABLE clients ADD COLUMN free_vacancies_seen INTEGER DEFAULT 0")
        except sqlite3.OperationalError: pass
        self._c().commit()
        # Миграция: когда клиент забрал бесплатный пак исходников (лид-магнит
        # при /start) — чтобы не предлагать повторно и считать конверсию
        try: self._c().execute("ALTER TABLE clients ADD COLUMN free_pack_claimed_at TEXT")
        except sqlite3.OperationalError: pass
        self._c().commit()
        # Миграция: заморозка подписки. frozen_at — когда клиент поставил
        # подписку на паузу (NULL = не заморожена). Формат тот же, что и у
        # sub_until (datetime.now().isoformat(), БЕЗ now_sql()!) — это
        # намеренно: разница frozen_at/sub_until считается как обычная
        # разница datetime, и обе колонки должны быть в одной и той же
        # "системе отсчёта", иначе получим тот же баг с несовпадением
        # форматов дат, что уже был в истории проекта.
        try: self._c().execute("ALTER TABLE clients ADD COLUMN frozen_at TEXT")
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
            ("skip_no_username", "0"),
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

    def record_author(self, chat_id: int, sender_id: int, username: str, name: str) -> None:
        """Фиксирует, что этот человек написал в этом чате — по факту
        пойманного юзерботом сообщения, а не по списку участников Telegram
        (тот бывает скрыт настройками приватности группы). Вызывается на
        КАЖДОЕ входящее сообщение из немодерируемых источников, до всех
        фильтров ключевых слов/ЧС — иначе список "кто писал" будет неполным."""
        self._c().execute(
            "INSERT INTO chat_authors(chat_id,sender_id,username,name,last_seen) VALUES(?,?,?,?,?) "
            "ON CONFLICT(chat_id,sender_id) DO UPDATE SET "
            "username=excluded.username, name=excluded.name, last_seen=excluded.last_seen",
            (chat_id, sender_id, username or None, name or None, now_sql()))
        self._c().commit()

    def get_chat_authors(self, chat_id: int) -> list[dict]:
        return [dict(r) for r in self._c().execute(
            "SELECT * FROM chat_authors WHERE chat_id=? ORDER BY last_seen DESC", (chat_id,)).fetchall()]

    def get_source_stats(self, chat_id: int) -> dict:
        row = self._c().execute(
            "SELECT COUNT(*) AS total, "
            "SUM(CASE WHEN suitable=1 THEN 1 ELSE 0 END) AS suitable, "
            "SUM(CASE WHEN block_reason IS NOT NULL THEN 1 ELSE 0 END) AS blocked, "
            "MAX(created_at) AS last_at FROM vacancies WHERE chat_id=?", (chat_id,)).fetchone()
        authors = self._c().execute(
            "SELECT COUNT(*) FROM chat_authors WHERE chat_id=?", (chat_id,)).fetchone()[0]
        return {
            "total": row["total"] or 0, "suitable": row["suitable"] or 0,
            "blocked": row["blocked"] or 0, "last_at": row["last_at"], "authors": authors or 0,
        }

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

    # ── Бесплатный пак исходников (лид-магнит при /start) ────
    def add_free_pack_file(self, file_id: str, file_name: str) -> None:
        self._c().execute("INSERT INTO free_pack_files(file_id, file_name) VALUES(?,?)", (file_id, file_name))
        self._c().commit()

    def list_free_pack_files(self) -> list[dict]:
        return [dict(r) for r in self._c().execute(
            "SELECT * FROM free_pack_files ORDER BY id").fetchall()]

    def delete_free_pack_file(self, file_id: int) -> None:
        self._c().execute("DELETE FROM free_pack_files WHERE id=?", (file_id,))
        self._c().commit()

    def mark_free_pack_claimed(self, client_id: int) -> None:
        self._c().execute("UPDATE clients SET free_pack_claimed_at=? WHERE id=?",
                           (now_sql(), client_id))
        self._c().commit()

    def count_free_pack_claims(self) -> int:
        return self._c().execute(
            "SELECT COUNT(*) FROM clients WHERE free_pack_claimed_at IS NOT NULL").fetchone()[0]

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

    def get_search_active_clients(self) -> list[dict]:
        """Все клиенты с включённым поиском (search_active=1) — и с подпиской,
        и без неё. Используется для рассылки вакансий бесплатным клиентам
        (с урезанным контактом) вместе с платными (с полным контактом)."""
        return [dict(r) for r in self._c().execute(
            "SELECT * FROM clients WHERE search_active=1").fetchall()]

    def get_clients_by_segment(self, segment: str) -> list[dict]:
        """Гибкий таргетинг для ручной рассылки (не для вакансий — это отдельная
        рассылка объявлений/новостей). segment: 'all' | 'subscribed' | 'free'.
        В отличие от get_active_clients/get_search_active_clients здесь НЕ
        учитывается search_active — это тумблер "получать вакансии", он не
        должен влиять на то, дойдёт ли до человека объявление от админа."""
        now = datetime.now().isoformat()
        if segment == "subscribed":
            return [dict(r) for r in self._c().execute(
                "SELECT * FROM clients WHERE sub_until IS NOT NULL AND sub_until > ?", (now,)).fetchall()]
        if segment == "free":
            return [dict(r) for r in self._c().execute(
                "SELECT * FROM clients WHERE sub_until IS NULL OR sub_until <= ?", (now,)).fetchall()]
        return [dict(r) for r in self._c().execute("SELECT * FROM clients").fetchall()]

    # ── Рассылки и обратная связь ─────────────────────────────
    def create_broadcast(self, text: str, photo: Optional[str], segment: str, total: int) -> int:
        cur = self._c().execute(
            "INSERT INTO broadcasts(text,photo_file_id,segment,total_count) VALUES(?,?,?,?)",
            (text, photo, segment, total))
        self._c().commit(); return cur.lastrowid

    def finish_broadcast(self, broadcast_id: int, sent_count: int, recipient_client_ids: list[int]) -> None:
        self._c().execute("UPDATE broadcasts SET sent_count=? WHERE id=?", (sent_count, broadcast_id))
        if recipient_client_ids:
            self._c().executemany(
                "UPDATE clients SET last_broadcast_id=? WHERE id=?",
                [(broadcast_id, cid) for cid in recipient_client_ids])
        self._c().commit()

    def set_subscription(self, client_id: int, until: datetime, is_payment: bool = False) -> None:
        self._c().execute("UPDATE clients SET sub_until=? WHERE id=?", (until.isoformat(), client_id))
        if is_payment:
            self._c().execute("UPDATE clients SET first_payment=1 WHERE id=?", (client_id,))
        self._c().commit()

    def revoke_subscription(self, client_id: int) -> None:
        """Досрочно завершает подписку (например, если её выдали по ошибке) —
        переводит sub_until в прошлое, клиент сразу перестаёт считаться
        подписчиком во всех проверках (везде сравнение sub_until > now).
        История платежей и first_payment не трогаются — это не "отменяет"
        оплату, просто снимает текущий активный период."""
        self._c().execute("UPDATE clients SET sub_until=? WHERE id=?",
                           (datetime.now().isoformat(), client_id))
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

    def inc_free_vacancy_seen(self, client_id: int) -> int:
        """Считает вакансии с закрытым контактом, показанные клиенту без
        подписки — используется для воронки конверсии (усиленный текст в
        карточке вакансии каждые N штук + разовый пуш со скидкой после
        достижения порога). Обнуляется при первой оплате (см. create_payment)."""
        self._c().execute("UPDATE clients SET free_vacancies_seen = COALESCE(free_vacancies_seen,0) + 1 WHERE id=?",
                          (client_id,))
        self._c().commit()
        return self._c().execute("SELECT free_vacancies_seen FROM clients WHERE id=?",
                                 (client_id,)).fetchone()[0]

    def toggle_search(self, client_id: int) -> bool:
        row = self._c().execute("SELECT search_active FROM clients WHERE id=?", (client_id,)).fetchone()
        new_val = 0 if (row and row["search_active"]) else 1
        self._c().execute("UPDATE clients SET search_active=? WHERE id=?", (new_val, client_id))
        self._c().commit(); return bool(new_val)

    def freeze_subscription(self, client_id: int) -> None:
        """Ставит подписку на паузу: вакансии переставай приходить
        (search_active=0, как и раньше), а отсчёт времени до sub_until
        фиксируется — см. unfreeze_subscription, где он сдвигается вперёд
        на длительность заморозки."""
        self._c().execute(
            "UPDATE clients SET search_active=0, frozen_at=? WHERE id=?",
            (datetime.now().isoformat(), client_id))
        self._c().commit()

    def unfreeze_subscription(self, client_id: int) -> None:
        """Снимает паузу: sub_until сдвигается вперёд ровно на то время, что
        клиент был заморожен — оплаченные/бесплатные дни не теряются."""
        row = self._c().execute(
            "SELECT frozen_at, sub_until FROM clients WHERE id=?", (client_id,)).fetchone()
        if row and row["frozen_at"]:
            frozen_since = datetime.fromisoformat(row["frozen_at"])
            paused_for   = datetime.now() - frozen_since
            if row["sub_until"]:
                new_until = datetime.fromisoformat(row["sub_until"]) + paused_for
                self._c().execute("UPDATE clients SET sub_until=? WHERE id=?",
                                  (new_until.isoformat(), client_id))
        self._c().execute(
            "UPDATE clients SET search_active=1, frozen_at=NULL WHERE id=?", (client_id,))
        self._c().commit()

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

    def find_duplicate(self, text: str) -> Optional[tuple]:
        """Ищет уже виденную вакансию: сначала точное совпадение текста, потом
        нечёткое — среди вакансий за последние DUP_WINDOW_DAYS дней.
        Возвращает (id_вакансии, похожесть 0..1) или None.
        Похожесть считается по нормализованному тексту (см. norm_for_dup);
        совсем короткие тексты (<40 знаков) сравниваются только на полное
        совпадение — иначе разные короткие объявления легко принять за одно."""
        row = self._c().execute("SELECT id FROM vacancies WHERE text=?", (text,)).fetchone()
        if row: return (row["id"], 1.0)
        norm = norm_for_dup(text)[:2000]
        if not norm: return None
        n = len(norm)
        threshold = 1.0 if n < 40 else DUP_SIMILARITY
        words = set(norm.split())
        since = (datetime.now(timezone.utc) - timedelta(days=DUP_WINDOW_DAYS)).strftime("%Y-%m-%d %H:%M:%S")
        rows = self._c().execute(
            "SELECT id, text FROM vacancies WHERE created_at >= ? ORDER BY id DESC LIMIT 3000",
            (since,)).fetchall()
        raw_len = len(text)
        my_prices = extract_prices(text)
        price_rejected = None
        for r in rows:
            cand_raw = r["text"] or ""
            # Дешёвые предфильтры — чтобы не гонять тяжёлое сравнение по всей базе
            if min(raw_len, len(cand_raw)) < 0.6 * max(raw_len, len(cand_raw)): continue
            cand = norm_for_dup(cand_raw)[:2000]
            if not cand: continue
            if min(n, len(cand)) < 0.85 * max(n, len(cand)): continue
            cw = set(cand.split())
            if len(words & cw) < 0.5 * len(words | cw): continue
            ratio = difflib.SequenceMatcher(None, norm, cand, autojunk=False).ratio()
            if ratio >= threshold:
                # Текст почти тот же, но цена другая (обе указаны и не совпадают) —
                # это новое предложение, а не репост: не считаем дубликатом.
                cand_prices = extract_prices(cand_raw)
                if my_prices and cand_prices and my_prices != cand_prices:
                    if price_rejected is None:
                        price_rejected = (r["id"], sorted(cand_prices), sorted(my_prices))
                    continue
                return (r["id"], ratio)
        if price_rejected:
            rid, old_p, new_p = price_rejected
            log.info(f"Похожа на #{rid}, но цена другая ({old_p} → {new_p}) — не дубликат")
            self.add_log("INFO", f"💰 Похожа на #{rid}, но цена другая ({old_p} → {new_p}) — не дубликат")
        return None

    def is_duplicate(self, text: str) -> bool:
        return self.find_duplicate(text) is not None

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
                      skipped: bool = False, reason: str = "", subscribed: bool = False) -> None:
        self._c().execute(
            "INSERT OR IGNORE INTO client_deliveries(vacancy_id,client_id,msg_id,skipped,skip_reason,sent_as_subscribed)"
            " VALUES(?,?,?,?,?,?)", (vacancy_id, client_id, msg_id, 1 if skipped else 0, reason, 1 if subscribed else 0))
        self._c().commit()

    def get_deliveries(self, vacancy_id: int) -> list[dict]:
        return [dict(r) for r in self._c().execute(
            "SELECT d.*, c.tg_id FROM client_deliveries d JOIN clients c ON d.client_id=c.id "
            "WHERE d.vacancy_id=? AND d.msg_id IS NOT NULL", (vacancy_id,)).fetchall()]

    def count_deliveries(self, client_id: int, subscribed_only: bool = False) -> int:
        """Сколько вакансий реально отправлено этому клиенту (msg_id IS NOT
        NULL — пропущенные из-за ошибки отправки не считаем). В отличие от
        free_vacancies_seen (который считает только показы БЕЗ подписки),
        это общее число за всё время — нужно для сообщений, актуальных и
        во время активного триала/подписки (когда free_vacancies_seen ещё 0)."""
        q = "SELECT COUNT(*) FROM client_deliveries WHERE client_id=? AND msg_id IS NOT NULL"
        args = [client_id]
        if subscribed_only:
            q += " AND sent_as_subscribed=1"
        return self._c().execute(q, args).fetchone()[0]

    def get_upgradeable_deliveries(self, client_id: int, limit: int = 100) -> list[dict]:
        """Доставки этому клиенту, отправленные как бесплатные (урезанный
        контакт, кнопка "Как откликнуться?"), пока у него не было подписки —
        нужны, чтобы при её оформлении заменить в этих СТАРЫХ сообщениях
        кнопку на обычную "Перейти к сообщению"/контакт, как у подписчика."""
        return [dict(r) for r in self._c().execute(
            "SELECT d.id AS delivery_id, d.msg_id, v.* FROM client_deliveries d "
            "JOIN vacancies v ON v.id=d.vacancy_id "
            "WHERE d.client_id=? AND d.msg_id IS NOT NULL AND d.sent_as_subscribed=0 "
            "ORDER BY d.id DESC LIMIT ?", (client_id, limit)).fetchall()]

    def mark_delivery_upgraded(self, delivery_id: int) -> None:
        self._c().execute("UPDATE client_deliveries SET sent_as_subscribed=1 WHERE id=?", (delivery_id,))
        self._c().commit()

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
    def _today(self) -> str: return datetime.now(MSK).strftime("%Y-%m-%d")

    def stat_inc(self, field: str, n: int = 1) -> None:
        today = self._today()
        self._c().execute(
            f"INSERT INTO stats_daily(date,{field}) VALUES(?,?) "
            f"ON CONFLICT(date) DO UPDATE SET {field}={field}+excluded.{field}",
            (today, n)); self._c().commit()

    def get_stats(self, period: str = "today") -> dict:
        c = self._c()
        now = datetime.now()          # для sub_until — там везде naive UTC, не трогаем
        now_msk = datetime.now(MSK)   # только для дат-ключей stats_daily (московские сутки)
        if period == "today":
            rows = c.execute("SELECT * FROM stats_daily WHERE date=?", (self._today(),)).fetchall()
        elif period == "week":
            since = (now_msk - timedelta(days=7)).strftime("%Y-%m-%d")
            rows = c.execute("SELECT * FROM stats_daily WHERE date>=?", (since,)).fetchall()
        elif period == "month":
            since = (now_msk - timedelta(days=30)).strftime("%Y-%m-%d")
            rows = c.execute("SELECT * FROM stats_daily WHERE date>=?", (since,)).fetchall()
        else:  # year
            since = (now_msk - timedelta(days=365)).strftime("%Y-%m-%d")
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
    # ts в таблице logs хранится в UTC (datetime('now') в SQLite). Чтобы кнопка
    # "Экспорт" и счётчик "Логи за <дата>" резались по московским суткам (а не
    # съезжали на 3 часа), везде ниже сравниваем не сам ts, а datetime(ts,'+3
    # hours') — то есть сдвигаем ТОЛЬКО на время сравнения/вывода, само хранение
    # в БД не меняем (это важно для остальной части кода, которая тоже читает ts).
    def add_log(self, level: str, message: str) -> None:
        try:
            self._c().execute("INSERT INTO logs(level,message) VALUES(?,?)", (level, message))
            self._c().commit()
        except Exception: pass

    def get_logs(self, date: str, limit: int = 200) -> list[dict]:
        return [dict(r) for r in self._c().execute(
            "SELECT * FROM logs WHERE datetime(ts,'+3 hours') LIKE ? ORDER BY id DESC LIMIT ?",
            (f"{date}%", limit)).fetchall()]

    def count_logs(self, date: str) -> int:
        return self._c().execute(
            "SELECT COUNT(*) FROM logs WHERE datetime(ts,'+3 hours') LIKE ?", (f"{date}%",)).fetchone()[0]

    def export_logs(self, date: str) -> bytes:
        rows = self._c().execute(
            "SELECT * FROM logs WHERE datetime(ts,'+3 hours') LIKE ? ORDER BY id ASC", (f"{date}%",)).fetchall()
        return "\n".join(f"{fmt_msk(r['ts'])} | {r['level']:<8} | {r['message']}" for r in rows).encode("utf-8")

    def cleanup(self) -> None:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=2)).strftime("%Y-%m-%d %H:%M:%S")
        self._c().execute("DELETE FROM logs WHERE ts<?", (cutoff,))
        # Не трогаем вакансии, ожидающие довыполнения ИИ-проверки (см.
        # recheck_skipped_vacancies) — иначе долгий сбой DeepSeek (>2 суток)
        # приведёт к тому, что они удалятся раньше, чем будут перепроверены
        self._c().execute(
            "DELETE FROM vacancies WHERE suitable=0 AND created_at<? "
            "AND (ds_reason IS NULL OR ds_reason NOT IN (?, ?, ?))",
            (cutoff, "ИИ выключен — не проверено", "ИИ недоступен — не проверено", "Ошибка API"))
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

async def _fetch_binance_p2p_rate() -> float:
    async with aiohttp.ClientSession() as s:
        async with s.post(
            "https://p2p.binance.com/bapi/c2c/v2/friendly/c2c/adv/search",
            json={"asset": "USDT", "fiat": "RUB", "tradeType": "SELL",
                  "page": 1, "rows": 10, "payTypes": [], "publisherType": None},
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            data = await resp.json()
    prices = [float(row["adv"]["price"]) for row in data.get("data", [])]
    if not prices: raise ValueError("Пустой ответ от Binance P2P")
    return sum(prices[:5]) / len(prices[:5])

async def _fetch_bybit_p2p_rate() -> float:
    async with aiohttp.ClientSession() as s:
        async with s.post(
            "https://api2.bybit.com/fiat/otc/item/online",
            json={"userId": "", "tokenId": "USDT", "currencyId": "RUB",
                  "payment": [], "side": "1", "size": "10", "page": "1",
                  "amount": "", "authMaker": False, "canTrade": False},
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            data = await resp.json()
    items  = ((data.get("result") or {}).get("items")) or []
    prices = [float(it["price"]) for it in items]
    if not prices: raise ValueError("Пустой ответ от Bybit P2P")
    return sum(prices[:5]) / len(prices[:5])

async def get_usdt_rub_rate() -> Optional[float]:
    """Курс USDT→RUB с P2P-рынка (продажа USDT за рубли).
    Источник — публичный API Binance P2P: цены на P2P синхронизированы между
    биржами (мейкеры арбитражат), поэтому курс близок к тому, что покажет BingX P2P.
    К курсу применяется небольшой запас (CRYPTO_RATE_BUFFER, по умолчанию 1.5%)
    вниз, чтобы не потерять на реальном выводе через BingX P2P.
    Результат кэшируется на RATE_CACHE_TTL секунд.

    Binance для запросов с российских IP (в т.ч. хостингов) периодически
    режет доступ — поэтому при сбое Binance пробуем Bybit P2P как второй
    источник, а если недоступны оба — отдаём последний известный курс из
    кэша, и только в самом крайнем случае (кэша тоже нет — например, бот
    только что перезапущен) — ручной резервный курс из настроек
    (usdt_manual_rate, задаётся в ⚙️ Настройки)."""
    now = time.time()
    if _rate_cache["value"] and (now - _rate_cache["ts"]) < RATE_CACHE_TTL:
        return _rate_cache["value"]

    market_rate = None; last_err = None
    for name, fetcher in (("Binance P2P", _fetch_binance_p2p_rate), ("Bybit P2P", _fetch_bybit_p2p_rate)):
        try:
            market_rate = await fetcher()
            break
        except Exception as e:
            last_err = e
            log.warning(f"get_usdt_rub_rate ({name}): {e}")

    if market_rate is not None:
        rate = round(market_rate * (1 - CRYPTO_RATE_BUFFER / 100), 2)
        _rate_cache["value"] = rate; _rate_cache["ts"] = now
        return rate

    # Оба публичных источника недоступны
    if _rate_cache["value"]:
        return _rate_cache["value"]
    manual = _db.get_setting("usdt_manual_rate", "")
    if manual:
        try: return float(manual)
        except ValueError: pass
    log.error(f"get_usdt_rub_rate: оба источника недоступны, кэша и резервного курса нет ({last_err})")
    if _pipeline and last_err: await notify_admin_error(_pipeline.bot, "get_usdt_rub_rate", last_err)
    return None

# ═══════════════════════════════════════════════════════════════
# DEEPSEEK
# ═══════════════════════════════════════════════════════════════
_DS_FAIL = DeepSeekResult(suitable=False, reason="ИИ недоступен — не проверено", contact="")
_DS_RETRY_STATUSES = {429, 500, 502, 503, 504}

async def _upgrade_old_deliveries(bot: Bot, cl: dict) -> None:
    """После оформления/продления подписки — старые вакансии, которые пришли
    этому клиенту ещё бесплатным (с прочерком вместо контакта и кнопкой "Как
    откликнуться?"), заменяем на обычный вид подписчика: полный контакт,
    "Перейти к сообщению" — то же самое сообщение, просто перерисованное.
    Best-effort: Telegram может отказать редактировать очень старое или уже
    удалённое сообщение — в этом случае просто пропускаем его и всё равно
    помечаем как обработанное, чтобы не пытаться бесконечно при каждой
    следующей подписке."""
    rows = _db.get_upgradeable_deliveries(cl["id"])
    for v in rows:
        try:
            new_text, new_markup = render_vacancy_client(
                v.get("html_text") or html.escape(v.get("text","")),
                v.get("ds_contact") or "", v.get("author_id"), v.get("message_link"),
                subscribed=True)
            await bot.edit_message_text(chat_id=cl["tg_id"], message_id=v["msg_id"],
                                        text=new_text, parse_mode=ParseMode.HTML, reply_markup=new_markup)
            await asyncio.sleep(0.05)
        except Exception as e:
            log.debug(f"_upgrade_old_deliveries {v.get('msg_id')}: {e}")
        finally:
            _db.mark_delivery_upgraded(v["delivery_id"])

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
        "max_tokens": 300,
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
        # Один мгновенный повтор при разовом сбое (таймаут, обрыв соединения,
        # 429/5xx "Service is too busy") — раньше такая вакансия сразу уходила
        # в "не проверено" и ждала следующей плановой проверки. Ошибки вида
        # 400/401/402 (неверный запрос/ключ/нет денег) НЕ повторяем — бессмысленно.
        data = None
        for attempt in (1, 2):
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
                            if attempt == 1 and resp.status in _DS_RETRY_STATUSES:
                                db.add_log("WARNING", f"⚠️ DeepSeek HTTP {resp.status} — повторяю запрос")
                                await asyncio.sleep(1.5)
                                continue
                            db.stat_inc("ai_errors"); return _DS_FAIL
                        data = json.loads(body)
                break
            except (asyncio.TimeoutError, aiohttp.ClientError) as e:
                if attempt == 1:
                    log.warning(f"DeepSeek: разовый сбой сети ({type(e).__name__}: {e}) — повторяю запрос")
                    db.add_log("WARNING", f"⚠️ DeepSeek: сбой сети ({type(e).__name__}) — повторяю запрос")
                    await asyncio.sleep(1.5)
                    continue
                raise
        usage = data.get("usage", {}) or {}
        # Реальные названия полей из официального API DeepSeek (api-docs.deepseek.com):
        # prompt_tokens/completion_tokens/total_tokens — плоские поля верхнего уровня,
        # prompt_cache_hit_tokens/prompt_cache_miss_tokens — тоже плоские (не вложены
        # в prompt_tokens_details, как у OpenAI). Если поля нет в ответе — 0, чтобы
        # не падать на разных версиях API.
        prompt_tokens      = usage.get("prompt_tokens", 0) or 0
        completion_tokens  = usage.get("completion_tokens", 0) or 0
        total_tokens       = usage.get("total_tokens", 0) or 0
        cache_hit_tokens   = usage.get("prompt_cache_hit_tokens", 0) or 0
        cache_miss_tokens  = usage.get("prompt_cache_miss_tokens", 0) or 0
        # Бонус, не входит в исходное задание, но напрямую относится к багу с
        # thinking-режимом: если reasoning_tokens > 0 — модель всё-таки считает
        # скрытые рассуждения несмотря на "thinking": {"type": "disabled"} в
        # запросе, и именно это раздувает счёт. completion_tokens_details может
        # отсутствовать целиком на некоторых версиях API — тогда None/0.
        reasoning_tokens = ((usage.get("completion_tokens_details") or {}).get("reasoning_tokens", 0)) or 0
        log.info(
            f"DeepSeek usage: prompt={prompt_tokens} completion={completion_tokens} "
            f"total={total_tokens} cache_hit={cache_hit_tokens} cache_miss={cache_miss_tokens} "
            f"reasoning={reasoning_tokens}"
        )
        # То же самое — в db.add_log, чтобы попадало в экспорт "📥 Мониторинг"
        # (log.info выше уходит только в docker logs, которых у пользователя
        # обычно нет под рукой — а именно экспорт он присылает на разбор)
        db.add_log("INFO", f"DS usage: in={prompt_tokens} out={completion_tokens} "
                            f"hit={cache_hit_tokens} miss={cache_miss_tokens} reasoning={reasoning_tokens}")
        if reasoning_tokens:
            log.warning(
                f"DeepSeek: reasoning_tokens={reasoning_tokens} > 0 несмотря на "
                f"thinking:disabled в запросе — модель всё равно считает скрытые "
                f"рассуждения, это раздувает счёт"
            )
            db.add_log("WARNING", f"⚠️ ИИ считает скрытые рассуждения (reasoning={reasoning_tokens}) "
                                   f"несмотря на thinking:disabled — раздувает счёт")
        db.add_tokens(prompt_tokens, completion_tokens)
        raw_content = data["choices"][0]["message"]["content"]
        log.debug(f"DeepSeek content: {raw_content}")
        try:
            parsed = json.loads(raw_content)
        except json.JSONDecodeError:
            # DeepSeek сам предупреждает в документации: в режиме JSON Output
            # есть шанс получить не строго чистый JSON (пустой content, обёртку
            # в ```json ... ``` и т.п.), особенно на новых моделях линейки V4,
            # где response_format не даёт жёсткой гарантии схемы. Вместо того
            # чтобы сразу сдаваться (и терять токены впустую), пробуем вытащить
            # первый {...} блок из текста регуляркой.
            m = re.search(r'\{.*\}', raw_content, re.DOTALL)
            if not m:
                raise
            parsed = json.loads(m.group(0))
            log.warning(f"DeepSeek: JSON пришлось вытаскивать регуляркой из ответа: {raw_content[:200]!r}")
            db.add_log("WARNING", f"⚠️ ИИ вернул не чистый JSON, вытащили регуляркой: {raw_content[:120]!r}")
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
        db.add_log("ERROR", f"❌ DeepSeek не вернул валидный JSON: {e}")
        db.stat_inc("ai_errors")
        if _pipeline: await notify_admin_error(_pipeline.bot, "DeepSeek JSON", e)
        return _DS_FAIL
    except Exception as e:
        log.error(f"DeepSeek ошибка: {e}", exc_info=True)
        db.add_log("ERROR", f"❌ DeepSeek ошибка: {e}")
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
                json={
                    "model": DEEPSEEK_MODEL,
                    "messages": [{"role": "user", "content": "ok"}],
                    "max_tokens": 1,
                    # То же самое, что и в call_deepseek: без явного отключения
                    # thinking-режима модель может считать скрытые
                    # рассуждения даже на этот пустяковый пинг "ok" — а он
                    # летает каждые 5 минут (288 раз/сутки), так что раздутие
                    # токенов здесь бьёт по счёту ещё сильнее, чем в основной
                    # проверке вакансий.
                    "thinking": {"type": "disabled"},
                },
                headers={"Authorization": f"Bearer {DEEPSEEK_KEY}", "Content-Type": "application/json"},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                body = await resp.text()
                if resp.status == 200:
                    log.info(f"DeepSeek OK: model={DEEPSEEK_MODEL}")
                    # Логируем usage и здесь же — пинг летает 288 раз/сутки,
                    # раньше именно он оказался причиной раздутого счёта из-за
                    # забытого thinking:disabled. Теперь параметр стоит, но
                    # стоит убедиться цифрами, а не считать это как о факте.
                    try:
                        u = (json.loads(body).get("usage", {})) or {}
                        r = ((u.get("completion_tokens_details") or {}).get("reasoning_tokens", 0)) or 0
                        if _db:
                            _db.add_log("INFO", f"DS health-ping usage: in={u.get('prompt_tokens',0)} "
                                                 f"out={u.get('completion_tokens',0)} "
                                                 f"hit={u.get('prompt_cache_hit_tokens',0)} "
                                                 f"miss={u.get('prompt_cache_miss_tokens',0)} reasoning={r}")
                    except Exception:
                        pass
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
def norm_for_dup(s: str) -> str:
    """Нормализация текста для поиска дубликатов: без регистра, ё=е, без
    ссылок, @упоминаний, эмодзи и знаков препинания, пробелы схлопнуты.
    Так репост с добавленным смайликом, другой пунктуацией, другим контактом
    или опечаткой в пару букв всё равно распознаётся как тот же текст."""
    s = norm_yo(s.lower())
    s = re.sub(r"https?://\S+|t\.me/\S+", " ", s)
    s = re.sub(r"@\w+", " ", s)
    s = re.sub(r"[\W_]+", " ", s)
    return " ".join(s.split())

_PRICE_MARK = r"(?:тыс\w*|к|k|₽|руб\w*|р|\$|usd|долл\w*|€|eur|евро)"
_PRICE_AFTER = re.compile(
    r"(\d[\d\s.,]*?(?:\s*(?:[-–—]|до)\s*\d[\d\s.,]*?)?)\s*(" + _PRICE_MARK + r")(?![a-zа-яё0-9])")
_PRICE_BEFORE = re.compile(r"([$€₽])\s*(\d[\d\s.,]*)")

def extract_prices(text: str) -> frozenset:
    """Суммы денег из текста вакансии: числа рядом со знаком валюты/«руб»/«р»/
    «к»/«тыс» ('1500 руб', '1 500 ₽', '50-70 тыс', '1,5к', '$200'). 'к' и 'тыс'
    умножают на 1000. Нужна для дедупликации: если цена в репосте другая —
    это уже не дубликат, а новое предложение."""
    t = text.lower()
    found = set()

    def add(chunk: str, mult: bool) -> None:
        chunk = re.sub(r"(?<=\d)[\s.](?=\d{3}(?!\d))", "", chunk)   # 1 500 / 1.500 -> 1500
        chunk = chunk.replace(",", ".")
        for num in re.findall(r"\d+(?:\.\d+)?", chunk):
            v = float(num)
            if mult and v < 1000: v *= 1000
            if v > 0: found.add(int(round(v)))

    for m in _PRICE_AFTER.finditer(t):
        mark = m.group(2)
        add(m.group(1), mark.startswith("тыс") or mark in ("к", "k"))
    for m in _PRICE_BEFORE.finditer(t):
        add(m.group(2), False)
    return frozenset(found)

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

def fmt_msk(sql_or_iso: Optional[str], fmt: str = "%Y-%m-%d %H:%M:%S") -> str:
    """ТОЛЬКО для показа времени человеку. В БД везде хранится UTC (through
    now_sql()/datetime('now')) — эта функция сдвигает его на +3 часа (Москва,
    без перехода на летнее/зимнее) и форматирует под нужный вид. Никогда не
    использовать результат для сравнений/фильтров в SQL — там как хранилось
    в UTC, так и должно сравниваться с UTC (см. now_sql), иначе получим тот
    же баг, что уже чинили с несовпадением форматов дат."""
    if not sql_or_iso: return ""
    try:
        dt = datetime.fromisoformat(sql_or_iso)
    except Exception:
        return sql_or_iso
    return (dt + timedelta(hours=3)).strftime(fmt)

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
    try: return (datetime.fromisoformat(iso) + timedelta(hours=3)).strftime("%d.%m.%Y")
    except Exception: return iso[:10]

def fmt_remaining(iso: Optional[str]) -> str:
    """Остаток времени до sub_until в днях/часах/минутах — для настроек
    клиента (там нужна точность до минуты, не просто дата, как на главном
    экране). '3 дн 5 ч 12 мин' / '5 ч 12 мин' / '12 мин' / '—' если нет
    подписки или она уже закончилась."""
    if not iso: return "—"
    try: until = datetime.fromisoformat(iso)
    except Exception: return "—"
    delta = until - datetime.now()
    total_min = int(delta.total_seconds() // 60)
    if total_min <= 0: return "подписка закончилась"
    days, rem = divmod(total_min, 24 * 60)
    hours, minutes = divmod(rem, 60)
    parts = []
    if days: parts.append(f"{days} дн")
    if hours or days: parts.append(f"{hours} ч")
    parts.append(f"{minutes} мин")
    return " ".join(parts)

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

REVIEWS_CHANNEL_URL = "https://t.me/phasereviews"

def kb_client_main() -> InlineKeyboardMarkup:
    return mkb([
        [("👥 Реферальная программа","client_referral")],
        [("💳 Тарифы","client_tariffs"), ("⭐ Отзывы", REVIEWS_CHANNEL_URL)],
        [("⚙️ Настройки","client_settings")],
    ])

_MENTION_RE = re.compile(r'(?<!\w)@\w{4,32}')

def _partial_mask(username: str) -> str:
    """Прячет юзернейм не полностью, а частично: @iv*** вместо голого "—".
    Так видно, что контакт реальный и есть кому писать, а не абстрактный
    прочерк, который мог бы значить что угодно (в том числе что автора нет
    вообще) — это ощутимо убедительнее для конверсии из бесплатных."""
    name = username.lstrip("@")
    if len(name) <= 2: return "@" + "*" * len(name)
    return "@" + name[:2] + "*" * min(len(name) - 2, 5)

def censor_mentions(text: str) -> str:
    """Заменяет любые упоминания вида @username на частично скрытый вид —
    используется в тексте вакансии для клиентов без подписки."""
    return _MENTION_RE.sub(lambda m: _partial_mask(m.group(0)), text)

def render_vacancy_client(v_html: str, contact: str, author_id: int, message_link: str,
                          subscribed: bool = True, is_channel_source: bool = False) -> tuple[str, InlineKeyboardMarkup]:
    """Единый рендер вакансии для клиента. v_html — уже готовый HTML-текст
    (Vacancy.html_text, форматирование сохранено), НЕ экранируем его повторно —
    вызывающий код отвечает за то, что это безопасный HTML или escape-плейн.

    subscribed=False — версия для клиентов без подписки: любой контакт
    (юзернейм в тексте вакансии, строка "Автор:") частично скрывается
    (@iv*** — видно, что контакт реальный, но не полностью), а вместо
    кнопки-перехода к исходному сообщению (там виден настоящий контакт) —
    кнопка "Как откликнуться?". Ссылка на профиль без юзернейма (только
    по ID) без подписки не показывается вообще — там скрывать нечего.

    is_channel_source=True — вакансия из канала, а не из группы: у поста нет
    живого автора (отправитель в апдейте — сам канал), поэтому строку "Автор:"
    не показываем вообще, независимо от подписки и прочих условий."""
    contact = contact or ""
    if not subscribed:
        v_html = censor_mentions(v_html)

    if is_channel_source:
        # У канала нет живого автора — писать вообще нечего и некому
        author_line = ""
    elif contact.lower().endswith("bot"):
        # Автор — бот (например @istochnik_bot, публикующий вакансии в канал
        # от своего имени) — писать ему по вакансии бессмысленно, строку об
        # авторе просто не показываем вообще (не пишем даже плейсхолдер)
        author_line = ""
    elif contact.startswith("@"):
        author_line = f"\n\n<blockquote>Автор: {_partial_mask(contact) if not subscribed else html.escape(contact)}</blockquote>"
    elif author_id:
        if not subscribed:
            # Контакт (в любом виде) без подписки не показываем точно так же,
            # как юзернейм — иначе подписка теряет смысл
            author_line = "\n\n<blockquote>Автор: —</blockquote>"
        else:
            # У автора нет юзернейма — единственный по-настоящему надёжный
            # способ написать ему это перейти в сообщение-источник и написать
            # там (Telegram резолвит его правильно в контексте общего чата).
            # Прямая ссылка tg://user?id= иногда тоже открывает профиль, но
            # без каких-либо гарантий — оставляем как запасной вариант.
            link = f"tg://user?id={author_id}"
            author_line = (
                f"\n\n<blockquote>Автор: <a href='{link}'>{link}</a></blockquote>"
                f"\n<blockquote><i>⚠️ У автора нет юзернейма. Надёжный способ написать ему — "
                f"перейти в сообщение по кнопке ниже и написать прямо там (работает "
                f"на любом устройстве). Также можно попробовать прямую ссылку выше "
                f"— иногда открывает профиль, но не гарантированно ⚠️</i></blockquote>"
            )
    else:
        author_line = "\n\n<blockquote>⚠️ Автор не определён, перейдите к сообщению по кнопке ниже ⚠️</blockquote>"

    msg_text = f"📢 <b>Новая вакансия</b>\n\n{v_html}{author_line}"

    if subscribed:
        markup = mkb([[("🔗 Перейти к сообщению", message_link)]]) if message_link else None
    else:
        # Без подписки кнопку-переход не даём — по ней видно настоящий
        # контакт в канале-источнике, это как раз то, что должна закрывать
        # подписка. Вместо неё — кнопка с объяснением и тарифами.
        markup = mkb([[("Как откликнуться?", "client_how_reply")]])
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
            is_channel_source = bool(getattr(chat, "broadcast", False))
            # Для постов из канала "отправитель" в апдейте — это сам канал, а
            # не живой человек. Его username тут ни к чему — это не контакт
            # автора, а страница канала, писать туда как "автору" бессмысленно.
            # sender_id (ID канала) всё равно оставляем настоящим — он нужен
            # для антиспам-кулдауна и чёрного списка по отправителю, зануление
            # слило бы разные каналы-источники в одного "отправителя".
            username     = "" if is_channel_source else (getattr(sender,"username",None) or "")
            sender_id    = getattr(sender,"id",0) or event.sender_id or 0
            message_link = make_msg_link(event, chat)
            text, html_text = extract_text(msg)
            if not text.strip(): return

            log.info(f"📥 [{source_title}] @{username}")
            self.db.add_log("INFO", f"Получено: {message_link} @{username}")

            # Фиксируем автора для выгрузки "кто писал" — до любых фильтров
            # (ключевые слова, ЧС, антиспам), чтобы список не зависел от
            # того, оказалось сообщение вакансией или нет.
            if sender_id and not is_channel_source:
                full_name = " ".join(filter(None, [getattr(sender, "first_name", None),
                                                     getattr(sender, "last_name", None)])) if sender else ""
                self.db.record_author(chat_id, sender_id, username, full_name)

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
            dup = self.db.find_duplicate(text)
            if dup:
                dup_id, dup_sim = dup
                how = "точная копия" if dup_sim >= 1.0 else f"похожа на {dup_sim:.0%}"
                log.info(f"🔁 Дубликат ({how}, вакансия #{dup_id})")
                self.db.add_log("INFO", f"🔁 Дубликат (уже была такая вакансия: {how}, #{dup_id})")
                return

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
            if not username and sender_id and not is_channel_source:
                # 1) InputUser напрямую из данных этого конкретного апдейта —
                # содержит свежий access_hash именно из этого сообщения, а не
                # потенциально устаревший/несовместимый по контексту кэш по
                # голому ID. Самый надёжный вариант, пробуем первым.
                try:
                    input_sender = await msg.get_input_sender()
                    if not input_sender:
                        self.db.add_log("DEBUG", "⚠️ Резолв username (шаг 1): get_input_sender() вернул пусто")
                    else:
                        full = await self.userbot(GetFullUserRequest(input_sender))
                        if full.users:
                            username = getattr(full.users[0], "username", None) or ""
                            if not username:
                                self.db.add_log("DEBUG", "⚠️ Резолв username (шаг 1): пользователь получен, "
                                                          "но username пуст")
                        else:
                            self.db.add_log("DEBUG", "⚠️ Резолв username (шаг 1): GetFullUserRequest без users")
                except Exception as e:
                    log.debug(f"GetFullUserRequest(input_sender): {e}")
                    self.db.add_log("DEBUG", f"⚠️ Резолв username (шаг 1, input_sender) не удался: {e}")
                # 2) Резолв по голому ID через кэш сессии
                if not username:
                    try:
                        full_sender = await self.userbot.get_entity(sender_id)
                        username = getattr(full_sender, "username", None) or ""
                    except Exception as e:
                        self.db.add_log("DEBUG", f"⚠️ Резолв username (шаг 2, get_entity) не удался: {e}")
                # 3) GetFullUserRequest по голому ID (может сработать, если
                # кэш содержит хотя бы устаревший access_hash того же юзера)
                if not username:
                    try:
                        full = await self.userbot(GetFullUserRequest(sender_id))
                        if full.users:
                            username = getattr(full.users[0], "username", None) or ""
                    except Exception as e:
                        log.debug(f"GetFullUserRequest({sender_id}): {e}")
                        self.db.add_log("DEBUG", f"⚠️ Резолв username (шаг 3, по ID) не удался: {e}")
                # 4) Шаги 1-3 упираются в отсутствие access_hash в кэше — если
                # его там просто нет (Telegram не докинул полные данные
                # отправителя в само live-событие, так бывает с нечастыми в
                # этом чате юзерами в больших группах), по голому ID ничего не
                # резолвится в принципе. Явный повторный запрос ЭТОГО ЖЕ
                # сообщения с сервера (а не из уже полученного апдейта) обычно
                # приходит с полными данными отправителя — Telegram досылает
                # их при точечном запросе иначе, чем в live-потоке.
                if not username:
                    try:
                        chat_ent = await event.get_input_chat()
                        fresh = await self.userbot.get_messages(chat_ent, ids=message_id)
                        if not fresh:
                            self.db.add_log("DEBUG", "⚠️ Резолв username (шаг 4): get_messages вернул пусто")
                        else:
                            fresh_sender = await fresh.get_sender()
                            if not fresh_sender:
                                self.db.add_log("DEBUG", "⚠️ Резолв username (шаг 4): get_sender() у свежего "
                                                          "сообщения вернул пусто")
                            else:
                                username = getattr(fresh_sender, "username", None) or ""
                                if not username:
                                    self.db.add_log("DEBUG", f"⚠️ Резолв username (шаг 4): отправитель получен "
                                                              f"({type(fresh_sender).__name__}), но username пуст")
                    except Exception as e:
                        self.db.add_log("DEBUG", f"⚠️ Резолв username (шаг 4, get_messages) не удался: {e}")
                # 5) Точечный запрос ЭТОГО КОНКРЕТНОГО участника у самого чата
                # (channels.getParticipant) — не весь список участников (в
                # больших публичных группах Telegram и не отдаёт полный список
                # обычным юзерам), а именно одного человека по ID. Если чат —
                # супергруппа/канал и этот человек в нём состоит, Telegram
                # обязан прислать его полные данные, откуда бы ни взялся ID.
                # Не гарантия (не сработает для базовых Chat, не сработает,
                # если человек уже вышел из чата) — но ещё один честный шанс.
                if not username:
                    try:
                        chat_ent = await event.get_input_chat()
                        part = await self.userbot(GetParticipantRequest(chat_ent, sender_id))
                        if part.users:
                            username = getattr(part.users[0], "username", None) or ""
                            if not username:
                                self.db.add_log("DEBUG", "⚠️ Резолв username (шаг 5): участник получен, "
                                                          "но username пуст")
                        else:
                            self.db.add_log("DEBUG", "⚠️ Резолв username (шаг 5): GetParticipant без users")
                    except Exception as e:
                        self.db.add_log("DEBUG", f"⚠️ Резолв username (шаг 5, GetParticipant) не удался: {e}")
                if not username:
                    self.db.add_log("INFO", f"ℹ️ У автора {sender_id} нет юзернейма "
                                             f"(все 5 способов резолва отработали, юзернейма действительно нет)")

            # Если юзернейма так и не нашли — по настройке можно вообще не
            # присылать такую вакансию клиентам (пропала последняя надёжная
            # возможность автору написать напрямую по клику на юзернейм — а
            # канал-прокладка/tg://user не гарантированы, см. предыдущие
            # разборы). ВАЖНО: это отсеет не только тех, у кого юзернейма
            # реально нет, но и тех, у кого он ЕСТЬ, но резолв не смог его
            # найти (Telegram иногда просто не досылает данные) — то есть
            # часть настоящих вакансий будет молча теряться.
            if not username and not is_channel_source and self.db.get_setting("skip_no_username", "0") == "1":
                self.db.add_log("INFO", f"⏭ Пропущено (нет юзернейма, настройка включена): {message_link}")
                skip_v = Vacancy(chat_id=chat_id, message_id=message_id, text=text,
                                 author_username="", author_id=sender_id, source_title=source_title,
                                 message_link=message_link, timestamp=datetime.now(), html_text=html_text)
                svid = self.db.save_vacancy(skip_v)
                if svid: self.db.update_vacancy_ds(svid, False, "Пропущено: нет юзернейма", "")
                return

            vacancy = Vacancy(chat_id=chat_id, message_id=message_id, text=text,
                              author_username=username, author_id=sender_id,
                              source_title=source_title, message_link=message_link,
                              timestamp=datetime.now(), html_text=html_text,
                              is_channel_source=is_channel_source)
            vid = self.db.save_vacancy(vacancy)
            if not vid: return

            # DeepSeek — если ИИ выключен или недоступен, вакансия НЕ уходит
            # клиенту ни при каких условиях (отказ по умолчанию, а не одобрение
            # по умолчанию) — раньше выключенный ИИ автоматически одобрял любое
            # совпадение по ключевым словам без единой проверки, из-за чего
            # клиентам доходил откровенный мусор.
            if self.db.get_setting("ai_active","1") != "1":
                ds = DeepSeekResult(suitable=False, reason="ИИ выключен — не проверено", contact="")
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

    async def recheck_skipped_vacancies(self) -> int:
        """Когда ИИ снова заработал (после отключения/сбоя) — дозапускает
        проверку по всем вакансиям, которые совпали по ключевым/стоп-словам,
        но не были проверены, пока ИИ был выключен/недоступен (см. отметку
        'ИИ выключен — не проверено' в _process). Возвращает число обработанных."""
        rows = self.db._c().execute(
            "SELECT * FROM vacancies WHERE ds_reason IN (?, ?, ?) ORDER BY id",
            ("ИИ выключен — не проверено", "ИИ недоступен — не проверено", "Ошибка API")).fetchall()
        if not rows: return 0
        log.info(f"Довыполняю ИИ-проверку для {len(rows)} пропущенных вакансий")
        for row in rows:
            v = dict(row)
            try:
                ds = await call_deepseek(v["text"], v.get("author_username") or "", self.db)
                self.db.update_vacancy_ds(v["id"], ds.suitable, ds.reason, ds.contact)
                if ds.suitable:
                    self.db.stat_inc("vacancies_found")
                    vacancy = Vacancy(
                        chat_id=v["chat_id"], message_id=v["message_id"], text=v["text"],
                        author_username=v.get("author_username") or "", author_id=v.get("author_id") or 0,
                        source_title=v.get("source_title") or "", message_link=v.get("message_link") or "",
                        timestamp=datetime.now(), html_text=v.get("html_text") or "")
                    await self._handle_suitable(vacancy, ds, v["id"])
                else:
                    self.db.stat_inc("vacancies_failed")
            except Exception as e:
                log.error(f"Довыполнение проверки вакансии #{v['id']}: {e}")
            await asyncio.sleep(1)  # не долбим DeepSeek пачкой разом
        return len(rows)

    async def _broadcast(self, vacancy: Vacancy, ds: DeepSeekResult, vid: int) -> None:
        if self.db.get_setting("client_bot_active","1") != "1": return
        # Рассылаем ВСЕМ клиентам с включённым поиском — и подписчикам, и без
        # подписки. Без подписки контакт автора скрывается (render_vacancy_client
        # сам решает по subscribed=False), это не отдельная выборка получателей.
        now = datetime.now().isoformat()
        clients = self.db.get_search_active_clients()
        log.info(f"📢 Рассылка {len(clients)} клиентам")
        for cl in clients:
            try:
                cl_id = cl["id"]
                text_low = norm_yo(vacancy.text.lower())
                stop_words = self.db.get_client_stopwords(cl_id)
                hit = [w for w in stop_words if norm_yo(w.lower()) in text_low]
                if hit:
                    self.db.save_delivery(vid, cl_id, None, skipped=True, reason=f"sw:{hit[0]}"); continue

                subscribed = bool(cl.get("sub_until") and cl["sub_until"] > now)
                msg_text, markup = render_vacancy_client(
                    vacancy.html_text or html.escape(vacancy.text),
                    ds.contact, vacancy.author_id, vacancy.message_link,
                    subscribed=subscribed, is_channel_source=vacancy.is_channel_source)

                if not subscribed:
                    seen = self.db.inc_free_vacancy_seen(cl_id)
                    # Каждую 3-ю закрытую вакансию усиливаем текст прямо в
                    # карточке — не полагаемся только на то, что клиент сам
                    # нажмёт "Как откликнуться?"
                    if seen % 3 == 0:
                        msg_text += (f"\n\n<i>💡 Это уже {seen}-я вакансия с закрытым контактом — "
                                     f"с подпиской вы бы увидели его сразу.</i>")
                    # Разовый пуш со скидкой тем, кто долго остаётся на бесплатном —
                    # отдельно от win-back после истечения подписки (тот бьёт по
                    # только что отвалившимся, этот — по тем, кто вообще никогда
                    # не платил и уже накопил ощутимое количество вакансий)
                    if seen == FREE_NUDGE_THRESHOLD and not cl.get("first_payment"):
                        nudge_key = f"free_nudge_sent_{cl_id}"
                        if not self.db.get_setting(nudge_key, ""):
                            self.db.set_setting(nudge_key, "1")
                            _set_winback_offer(cl_id)
                            try:
                                await self.bot.send_message(cl["tg_id"],
                                    f"👀 Вы уже получили <b>{seen}</b> вакансий, но не смогли посмотреть контакты.\n\n"
                                    f"Ловите скидку <b>15%</b> на первую подписку — предложение "
                                    f"действует <b>{WINBACK_VALID_HOURS} часа</b>:",
                                    parse_mode=ParseMode.HTML,
                                    reply_markup=_tariffs_kb_winback(cl))
                            except Exception: pass

                sent   = await self.bot.send_message(cl["tg_id"], msg_text,
                                                     parse_mode=ParseMode.HTML, reply_markup=markup)
                self.db.save_delivery(vid, cl_id, sent.message_id, subscribed=subscribed)
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
class _PersistentPendingDict:
    """Ведёт себя как dict[int, str] (только [] для записи и .pop для чтения —
    больше нигде в коде с этими двумя словарями ничего другого не делают), но
    хранит значения в БД (settings), а не только в памяти процесса.

    Раньше _admin_pending/_client_pending были обычными dict — и именно это
    было настоящей причиной "Сессия истекла, начните заново" при выдаче
    подписки всем (и потенциально в любом другом многошаговом сценарии: ввёл
    число → ответил на вопрос → подтвердил): сам факт "жду от админа дни
    подписки" терялся при перезапуске бота между первым и вторым сообщением,
    ПЕРЕД тем как успевал создаться черновик в _give_sub_all_draft — тот уже
    был переведён на БД, но это не помогало, если до него дело не доходило.
    Если бот перезапускается часто — это стоит отдельно смотреть в логах
    Ботхоста (эта правка лечит симптом, а не причину частых рестартов)."""
    def __init__(self, prefix: str):
        self._prefix = prefix
    def __setitem__(self, uid: int, value: str) -> None:
        _db.set_setting(f"{self._prefix}_{uid}", value)
    def pop(self, uid: int, default=None):
        val = _db.get_setting(f"{self._prefix}_{uid}", "")
        if val: _db.set_setting(f"{self._prefix}_{uid}", "")
        return val if val else default

_admin_pending:  _PersistentPendingDict = _PersistentPendingDict("admin_pending")
_client_pending: _PersistentPendingDict = _PersistentPendingDict("client_pending")
_auth_state: dict = {}
_src_picker: dict[int, dict] = {}
_broadcast_draft: dict[int, dict] = {}  # uid -> {"text": str, "photo": Optional[str]}
# Черновик "выдать подписку всем" хранится В БАЗЕ (settings), а не просто в
# памяти процесса — раньше лежал в обычном dict и слетал при любом перезапуске
# бота между вводом дней и нажатием "Выдать всем" ("Сессия истекла, начните
# заново" даже когда админ ничего не делал неправильно).
def _give_sub_all_draft_get(uid: int) -> Optional[dict]:
    raw = _db.get_setting(f"give_sub_all_draft_{uid}", "")
    if not raw: return None
    try: return json.loads(raw)
    except Exception: return None

def _give_sub_all_draft_set(uid: int, draft: Optional[dict]) -> None:
    _db.set_setting(f"give_sub_all_draft_{uid}", json.dumps(draft) if draft else "")
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
        f"<b>phase.parser</b>  <code>{BOT_VERSION}</code>\n\n"
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
async def _get_admin_ids(userbot: TelegramClient, entity) -> set[int]:
    """Только ID админов, без полного списка участников. Список админов
    супергруппы/канала Telegram почти всегда отдаёт даже обычным
    участникам — в отличие от полного списка участников, который может
    быть скрыт настройками приватности группы. Поэтому эта функция
    отдельная и используется и там, где полный список работает, и там,
    где нет (выгрузка "кто писал")."""
    if isinstance(entity, Chat):
        try:
            full = await userbot(GetFullChatRequest(entity.id))
            return {p.user_id for p in full.full_chat.participants.participants
                    if p.__class__.__name__ in ("ChatParticipantAdmin", "ChatParticipantCreator")}
        except Exception as e:
            log.warning(f"_get_admin_ids (Chat) {getattr(entity,'id','?')}: {e}")
            return set()
    try:
        admins = await userbot.get_participants(entity, filter=ChannelParticipantsAdmins)
        return {u.id for u in admins}
    except Exception as e:
        log.warning(f"_get_admin_ids (Channel) {getattr(entity,'id','?')}: {e}")
        return set()


async def _fetch_members_with_roles(userbot: TelegramClient, entity) -> list[dict]:
    """Собирает ПОЛНЫЙ список участников чата/канала с ролью (Админ/
    Участник) через API списка участников Telegram. Работает только там,
    где Telegram вообще отдаёт этот список не-владельцу чата — в закрытых
    супергруппах или при определённых настройках приватности он может
    быть недоступен либо неполным (см. _get_admin_ids и выгрузку "кто
    писал" — тот способ не зависит от этого ограничения).
    Создатель считается администратором — отдельная категория не нужна
    (в задаче их всего две: Админ / Участник)."""
    result: list[dict] = []
    if isinstance(entity, Chat):
        full = await userbot(GetFullChatRequest(entity.id))
        admin_ids = {p.user_id for p in full.full_chat.participants.participants
                     if p.__class__.__name__ in ("ChatParticipantAdmin", "ChatParticipantCreator")}
        for u in full.users:
            if getattr(u, "bot", False): continue
            result.append({
                "username": getattr(u, "username", None),
                "name": " ".join(filter(None, [getattr(u, "first_name", None), getattr(u, "last_name", None)])) or None,
                "id": u.id, "role": "Админ" if u.id in admin_ids else "Участник",
            })
    else:
        admin_ids = await _get_admin_ids(userbot, entity)
        members   = await userbot.get_participants(entity, aggressive=True)
        for u in members:
            if getattr(u, "bot", False): continue
            result.append({
                "username": getattr(u, "username", None),
                "name": " ".join(filter(None, [getattr(u, "first_name", None), getattr(u, "last_name", None)])) or None,
                "id": u.id, "role": "Админ" if u.id in admin_ids else "Участник",
            })
    return result

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
            # Группы, супергруппы и каналы — без личных чатов с людьми
            is_supergroup = getattr(e, "megagroup", False) or getattr(e, "gigagroup", False)
            is_basic_chat = cls == "Chat"
            is_broadcast  = getattr(e, "broadcast", False)
            is_user       = cls == "User"
            if is_user:
                continue
            if not (is_supergroup or is_basic_chat or is_broadcast):
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
            "😕 <b>Ничего не найдено</b>\n\nUserBot не состоит ни в одной группе или канале.",
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
    await _show_sources_page(call, 0)

@admin_router.callback_query(F.data.startswith("admin_src_page:"))
async def admin_src_page_cb(call: CallbackQuery):
    await _show_sources_page(call, int(call.data.split(":")[1]))

async def _show_sources_page(call: CallbackQuery, page: int) -> None:
    """Список источников кнопками — по образцу «Все клиенты»: нажатие на
    источник открывает его карточку со статистикой и экспортом, вместо
    отдельного текстового списка со ссылками."""
    limit = 30
    srcs  = _db.get_sources(active_only=False)
    chunk = srcs[page*limit:(page+1)*limit]
    rows  = []
    for s in chunk:
        icon = "✅" if s["active"] else "❌"
        rows.append([(f"{icon} {s['title'][:38]}", f"admin_source_detail:{s['id']}")])
    nav = []
    if page > 0: nav.append(("◀️", f"admin_src_page:{page-1}"))
    if (page+1)*limit < len(srcs): nav.append(("▶️", f"admin_src_page:{page+1}"))
    if nav: rows.append(nav)
    rows.append([("📤 Экспорт списка","admin_src_export")])
    rows.append([("◀️ Назад","admin_sources")])
    await safe_edit(call,
        f"<b>📋 Все источники ({len(srcs)})</b>" + (f"\nСтраница {page+1}" if len(srcs) > limit else ""),
        mkb(rows))

@admin_router.callback_query(F.data == "admin_src_export")
async def admin_src_export_cb(call: CallbackQuery):
    srcs  = _db.get_sources(active_only=False)
    lines = [f"{'[ON]' if s['active'] else '[OFF]'} {s['title']} | {s.get('link','') or s['chat_id']}" for s in srcs]
    await call.message.answer_document(
        BufferedInputFile("\n".join(lines).encode("utf-8"), filename="sources.txt"),
        caption="📤 Список источников")
    await call.answer()

def render_source_detail(source_id: int) -> Optional[tuple[str, InlineKeyboardMarkup]]:
    s = next((x for x in _db.get_sources(active_only=False) if x["id"] == source_id), None)
    if not s: return None
    stats = _db.get_source_stats(s["chat_id"])
    link  = s.get("link") or f"tg://openmessage?chat_id={s['chat_id']}"
    added = fmt_msk(s.get("added_at"), "%Y-%m-%d") if s.get("added_at") else "—"
    last  = fmt_msk(stats["last_at"], "%Y-%m-%d %H:%M") if stats["last_at"] else "—"
    status = "✅ Активен" if s["active"] else "❌ Отключён"
    text = (
        f"<b>♨️ <a href='{link}'>{html.escape(s['title'])}</a></b>\n"
        f"Статус: {status}\n"
        f"Добавлен: {added}\n\n"
        f"<b>📊 Статистика</b>\n"
        f"Вакансий найдено: <b>{stats['total']}</b>\n"
        f"  ✅ одобрено ИИ: {stats['suitable']}\n"
        f"  🚫 отсеяно чёрным списком: {stats['blocked']}\n"
        f"Писали в чате: <b>{stats['authors']}</b> чел.\n"
        f"Последняя активность: {last}"
    )
    toggle_label = "🔴 Отключить" if s["active"] else "🟢 Включить"
    markup = mkb([
        [(toggle_label, f"admin_src_toggle:{source_id}"), ("🗑 Удалить", f"admin_src_del:{source_id}")],
        [("👥 Все участники", f"admin_src_members:{source_id}"), ("✍️ Кто писал", f"admin_src_authors:{source_id}")],
        [("◀️ Назад","admin_src_list")],
    ])
    return text, markup

@admin_router.callback_query(F.data.startswith("admin_source_detail:"))
async def admin_source_detail_cb(call: CallbackQuery):
    result = render_source_detail(int(call.data.split(":")[1]))
    if not result: await call.answer("Источник не найден"); return
    text, markup = result
    await safe_edit(call, text, markup)

@admin_router.callback_query(F.data.startswith("admin_src_toggle:"))
async def admin_src_toggle_cb(call: CallbackQuery):
    source_id = int(call.data.split(":")[1])
    _db.toggle_source(source_id)
    result = render_source_detail(source_id)
    if not result: await call.answer("Источник не найден"); return
    text, markup = result
    await safe_edit(call, text, markup)

@admin_router.callback_query(F.data.startswith("admin_src_del:"))
async def admin_src_del_cb(call: CallbackQuery):
    _db.delete_source(int(call.data.split(":")[1]))
    await call.answer("Удалено")
    await _show_sources_page(call, 0)

@admin_router.callback_query(F.data.startswith("admin_src_members:"))
async def admin_src_members_cb(call: CallbackQuery):
    """Выгружает список участников источника с ролью (Админ/Участник)
    файлом — прямо в сообщении список может быть на тысячи строк и не
    влезет в лимит одного сообщения Telegram."""
    src_id = int(call.data.split(":")[1])
    src = next((s for s in _db.get_sources(active_only=False) if s["id"] == src_id), None)
    if not src: await call.answer("Источник не найден"); return
    if _userbot is None:
        await call.answer("Юзербот не подключён", show_alert=True); return
    await call.answer()
    await safe_edit(call, f"⏳ <b>Загружаю участников:</b> {html.escape(src['title'])}...", None)
    try:
        entity  = await _userbot.get_entity(src["chat_id"])
        members = await _fetch_members_with_roles(_userbot, entity)
    except Exception as e:
        log.error(f"admin_src_members {src['chat_id']}: {e}")
        await safe_edit(call, f"❌ Не удалось загрузить участников: <code>{html.escape(str(e))}</code>",
                         kb_back(f"admin_source_detail:{src_id}"))
        return

    if not members:
        await safe_edit(call, "😕 Участников не найдено (или недостаточно прав на просмотр).",
                         kb_back(f"admin_source_detail:{src_id}"))
        return

    def _label(m: dict) -> str:
        if m["username"]: return f"@{m['username']}"
        return f"id:{m['id']}" + (f" ({m['name']})" if m["name"] else "")

    lines   = [f"{_label(m)} {m['role']}" for m in members]
    admins  = sum(1 for m in members if m["role"] == "Админ")
    caption = f"👥 {src['title']} — {len(members)} участников, из них админов: {admins}"
    await call.message.answer_document(
        BufferedInputFile("\n".join(lines).encode("utf-8"), filename=f"members_{src_id}.txt"),
        caption=caption)
    result = render_source_detail(src_id)
    if result:
        text, markup = result
        await safe_edit(call, text, markup)

@admin_router.callback_query(F.data.startswith("admin_src_authors:"))
async def admin_src_authors_cb(call: CallbackQuery):
    """Выгрузка "кто писал" — строится из сообщений, реально пойманных
    юзерботом в этом чате (таблица chat_authors), а не из списка участников
    Telegram. Работает даже там, где полный список участников API не
    отдаёт (закрытые группы, настройки приватности) — единственное, что
    для этого нужно, это чтобы человек хотя бы раз написал в чате после
    того, как источник был добавлен.

    Отправляется отдельными сообщениями (не файлом) — так у каждого
    @юзернейма кликабельна ссылка прямо в чате, не нужно скачивать файл
    и переходить куда-то отдельно. Тем, у кого нет юзернейма, ссылка
    делается через tg://user?id= (может не сработать — тот же нюанс, что
    и с контактом автора вакансии: юзернейм в Telegram всегда публичный,
    а вот адресный переход по ID гарантий не даёт)."""
    src_id = int(call.data.split(":")[1])
    src = next((s for s in _db.get_sources(active_only=False) if s["id"] == src_id), None)
    if not src: await call.answer("Источник не найден"); return
    authors = _db.get_chat_authors(src["chat_id"])
    if not authors:
        await call.answer("Пока никто не писал (или источник только что добавлен)", show_alert=True)
        return
    await call.answer()

    admin_ids: set[int] = set()
    if _userbot is not None:
        try:
            entity = await _userbot.get_entity(src["chat_id"])
            admin_ids = await _get_admin_ids(_userbot, entity)
        except Exception as e:
            log.warning(f"admin_src_authors admin_ids {src['chat_id']}: {e}")

    def _label(a: dict) -> str:
        if a["username"]: return f"@{html.escape(a['username'])}"
        name = html.escape(a["name"]) if a["name"] else "без имени"
        return f'<a href="tg://user?id={a["sender_id"]}">{name}</a>'

    lines  = [f"{_label(a)} — {'Админ' if a['sender_id'] in admin_ids else 'Участник'}" for a in authors]
    admins = sum(1 for a in authors if a["sender_id"] in admin_ids)
    header = f"✍️ <b>{html.escape(src['title'])}</b> — писали {len(authors)} чел., из них админов: {admins}\n"

    # Лимит Telegram — 4096 символов на сообщение, режем по строкам, чтобы
    # не разорвать ссылку посередине
    chunks, cur, cur_len = [], [header], len(header)
    for line in lines:
        if cur_len + len(line) + 1 > 3800 and cur:
            chunks.append("\n".join(cur)); cur = []; cur_len = 0
        cur.append(line); cur_len += len(line) + 1
    if cur: chunks.append("\n".join(cur))

    for chunk in chunks:
        await call.message.answer(chunk, parse_mode=ParseMode.HTML, disable_web_page_preview=True)

    result = render_source_detail(src_id)
    if result:
        text, markup = result
        await safe_edit(call, text, markup)


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
            f"=== #{v['id']} | {status} | {fmt_msk(v.get('created_at'))} ===\n"
            f"Источник: {v.get('source_title','')}\n"
            f"Причина: {v.get('ds_reason','') or block_reason or '—'}\n"
            f"Контакт: {v.get('ds_contact','') or '—'}\n"
            f"Ссылка: {v.get('message_link','') or '—'}\n"
            f"Текст:\n{v.get('text','')}\n"
        )
    header = (f"Экспорт вакансий phase.parser | период: {period_label} | всего: {len(rows)} "
              f"(прошло: {ok_count}, не прошло ИИ: {fail_count}, отсеяно до ИИ: {blocked_count})\n\n")
    content = header + ("\n" + "-"*60 + "\n\n").join(lines)

    file = BufferedInputFile(content.encode("utf-8"), filename=f"vacancies_{period_label}_{datetime.now(MSK).strftime('%Y%m%d_%H%M')}.txt")
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
    ai_icon    = "🟢 Работает" if _ai_health["was_ok"] else "🔴 Не работает!"
    text   = (f"<b>🖥️ Мониторинг</b>\n\nСтатус: {icon}\nUserBot: {ub_icon}\n"
              f"Поток сообщений: {flow_icon} последнее {silent_min} мин. назад\n"
              f"ИИ-проверка: {ai_icon}\n"
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
                             filename=f"monitoring_{datetime.now(MSK).strftime('%Y%m%d_%H%M')}.txt")
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
    rows.append([("🎁 Выдать подписку всем", "admin_give_sub_all")])
    rows.append([("📤 Рассылка", "admin_broadcast")])
    rows.append([("📣 Реклама", "ad_menu")])
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
    method_labels = {"rub": "💳 Рубли"}
    method_labels.update({f"usdt_{k}": f"{w['icon']} {w['label']}" for k, w in CRYPTO_WALLETS.items()})
    lines = [
        "💳 <b>Платёж</b>",
        f"Тикет: <code>{ticket}</code>",
        f"Клиент: @{uname} (<code>{cl['tg_id']}</code>)",
        f"Тариф: <b>{p['tariff']}</b>",
        f"Сумма: <b>{p['amount']}₽</b>",
        f"Способ: {method_labels.get(p.get('method'), p.get('method',''))}",
        f"Дней: <b>{p['days']}</b>",
        f"Создан: {fmt_msk(p['created_at'], '%Y-%m-%d %H:%M')}",
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
    # Подписчики — первыми (среди них тоже новые сверху), затем остальные
    clients.sort(key=lambda c: (c.get("sub_until") or "") <= now)
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

def _pay_method_label(p: dict) -> str:
    """Способ оплаты для истории: '💳 Рубли' или '🔺 USDT · TRC20 (Tron), 12.5 USDT'."""
    method = p.get("method") or "rub"
    if method == "rub":
        return "💳 Рубли"
    if method.startswith("usdt_"):
        w = CRYPTO_WALLETS.get(method.split("_", 1)[1])
        label = f"{w['icon']} {w['label']}" if w else method
        if p.get("crypto_amount"):
            label += f", {p['crypto_amount']} USDT"
        return label
    return method

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
    def _hist_line(p: dict) -> str:
        mark = {"pending": "⏳ ", "rejected": "❌ "}.get(p.get("status"), "")
        return (f"{mark}{fmt_msk(p['created_at'], '%Y-%m-%d')} {p['tariff']} {p['amount']}₽ · "
                f"{_pay_method_label(p)}")
    hist_lines = [_hist_line(p) for p in payments]
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
        *([[("🗑 Забрать подписку", f"admin_revoke_sub_ask:{client_id}")]] if active else []),
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

@admin_router.callback_query(F.data.startswith("admin_revoke_sub_ask:"))
async def admin_revoke_sub_ask_cb(call: CallbackQuery):
    """Шаг подтверждения перед отзывом подписки — само действие необратимо
    (старый sub_until не сохраняется нигде), поэтому спрашиваем явно, как и
    при блокировке клиента."""
    client_id = call.data.split(":")[1]
    c = _db.get_client_by_id(int(client_id))
    if not c: await call.answer("Клиент не найден"); return
    uname   = c.get("username") or ""
    display = f"@{uname}" if uname else f"ID: <code>{c['tg_id']}</code>"
    until   = fmt_date(c.get("sub_until"))
    markup  = mkb([
        [("✅ Да, забрать", f"admin_revoke_sub_confirm:{client_id}"),
         ("❌ Отмена", f"admin_client_detail:{client_id}")],
    ])
    await safe_edit(call,
        f"🗑 <b>Забрать подписку?</b>\n\n{display}\nДействует до: {until}\n\n"
        f"Клиент сразу перестанет считаться подписчиком. Уведомление ему отправлено не будет.",
        markup)

@admin_router.callback_query(F.data.startswith("admin_revoke_sub_confirm:"))
async def admin_revoke_sub_confirm_cb(call: CallbackQuery):
    client_id = int(call.data.split(":")[1])
    c = _db.get_client_by_id(client_id)
    if not c: await call.answer("Клиент не найден"); return
    _db.revoke_subscription(client_id)
    uname   = c.get("username") or ""
    display = f"@{uname}" if uname else f"ID: <code>{c['tg_id']}</code>"
    log.info(f"Подписка отозвана администратором (карточка клиента): {c['tg_id']} {display}")
    await safe_edit(call,
        f"✅ <b>Подписка забрана</b>\n\n{display}",
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

@admin_router.callback_query(F.data == "admin_give_sub_all")
async def admin_give_sub_all_cb(call: CallbackQuery):
    uid = call.from_user.id
    _give_sub_all_draft_set(uid, None)
    _admin_pending[uid] = "give_sub_all_days"
    total = len(_db.get_all_clients())
    await safe_edit(call,
        f"🎁 <b>Выдать подписку всем клиентам ({total} чел.)</b>\n\nНа сколько дней продлить каждому?",
        kb_back("admin_clients"))

async def _show_give_sub_all_preview(bot: Bot, uid: int) -> None:
    draft = _give_sub_all_draft_get(uid)
    if not draft: return
    days    = draft["days"]
    comment = draft.get("comment", "")
    fake_until = fmt_date((datetime.now() + timedelta(days=days)).isoformat())
    text = f"🎁 Вам начислено <b>+{days} дн.</b> к подписке!\nАктивна до: <b>{fake_until}</b>"
    if comment:
        text += f"\n\n{comment}"
    total = len(_db.get_all_clients())
    await bot.send_message(uid, text, parse_mode=ParseMode.HTML)
    await bot.send_message(uid,
        f"☝️ Именно так это увидит каждый клиент (дата у каждого будет своя, "
        f"в зависимости от текущей подписки). Выдать <b>{total}</b> клиентам?",
        parse_mode=ParseMode.HTML,
        reply_markup=mkb([[("✅ Выдать всем","admin_give_sub_all_send"), ("❌ Отмена","admin_give_sub_all_cancel")]]))

@admin_router.callback_query(F.data == "admin_give_sub_all_send")
async def admin_give_sub_all_send_cb(call: CallbackQuery):
    uid   = call.from_user.id
    draft = _give_sub_all_draft_get(uid)
    _give_sub_all_draft_set(uid, None)
    if not draft:
        await call.answer("Сессия истекла, начните заново", show_alert=True); return
    days    = draft["days"]; comment = draft.get("comment","")
    clients = _db.get_all_clients()
    ok = 0
    for c in clients:
        try:
            until = _db.extend_subscription(c["id"], days)
            text  = f"🎁 Вам начислено <b>+{days} дн.</b> к подписке!\nАктивна до: <b>{fmt_date(until.isoformat())}</b>"
            if comment: text += f"\n\n{comment}"
            await call.bot.send_message(c["tg_id"], text, parse_mode=ParseMode.HTML, reply_markup=kb_client_main())
            await _upgrade_old_deliveries(call.bot, c)
            ok += 1; await asyncio.sleep(0.05)
        except Exception as e:
            log.warning(f"Выдача подписки всем {c.get('tg_id')}: {e}")
    await safe_edit(call, f"✅ Подписка выдана: <b>{ok}/{len(clients)}</b> клиентам (+{days} дн.)", kb_back("admin_clients"))

@admin_router.callback_query(F.data == "admin_give_sub_all_cancel")
async def admin_give_sub_all_cancel_cb(call: CallbackQuery):
    _give_sub_all_draft_set(call.from_user.id, None)
    await safe_edit(call, "❌ Отменено", kb_back("admin_clients"))


@admin_router.callback_query(F.data == "admin_broadcast")
async def admin_broadcast_cb(call: CallbackQuery):
    _broadcast_draft[call.from_user.id] = {}
    await safe_edit(call,
        "📤 <b>Рассылка</b>\n\nКому отправить?",
        mkb([
            [("👥 Всем клиентам", "admin_bcast_segment:all")],
            [("💎 Только с подпиской", "admin_bcast_segment:subscribed")],
            [("🆓 Только без подписки", "admin_bcast_segment:free")],
            [("◀️ Назад", "admin_clients")],
        ]))

@admin_router.callback_query(F.data.startswith("admin_bcast_segment:"))
async def admin_bcast_segment_cb(call: CallbackQuery):
    segment = call.data.split(":")[1]
    uid = call.from_user.id
    _broadcast_draft[uid] = {"segment": segment}
    _admin_pending[uid] = "broadcast_text"
    count = len(_db.get_clients_by_segment(segment))
    label = {"all": "всем клиентам", "subscribed": "клиентам с подпиской", "free": "клиентам без подписки"}[segment]
    await safe_edit(call,
        f"📤 <b>Рассылка {label}</b> (получателей: {count})\n\nВведите текст:",
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
    segment = draft.get("segment", "all")
    label = {"all": "всем клиентам", "subscribed": "клиентам с подпиской", "free": "клиентам без подписки"}[segment]
    count = len(_db.get_clients_by_segment(segment))
    # Показываем ровно то же сообщение, которое получит клиент
    if photo:
        await bot.send_photo(uid, photo, caption=text, parse_mode=ParseMode.HTML)
    else:
        await bot.send_message(uid, text, parse_mode=ParseMode.HTML)
    await bot.send_message(uid,
        f"☝️ Именно так это увидит клиент.\n\nОтправляем {label} (получателей: {count})?",
        reply_markup=mkb([[("✅ Отправить","admin_bcast_send"), ("❌ Отмена","admin_bcast_cancel")]]))

@admin_router.callback_query(F.data == "admin_bcast_send")
async def admin_bcast_send_cb(call: CallbackQuery):
    uid   = call.from_user.id
    draft = _broadcast_draft.pop(uid, None)
    if not draft:
        await call.answer("Сессия истекла, начните заново", show_alert=True); return
    text    = draft["text"]; photo = draft.get("photo")
    segment = draft.get("segment", "all")
    clients = _db.get_clients_by_segment(segment)
    bc_id   = _db.create_broadcast(text, photo, segment, len(clients))
    sent = 0; delivered_ids: list[int] = []
    for cl in clients:
        try:
            if photo:
                await call.bot.send_photo(cl["tg_id"], photo, caption=text, parse_mode=ParseMode.HTML)
            else:
                await call.bot.send_message(cl["tg_id"], text, parse_mode=ParseMode.HTML)
            sent += 1; delivered_ids.append(cl["id"]); await asyncio.sleep(0.05)
        except Exception as e: log.warning(f"Рассылка {cl['tg_id']}: {e}")
    _db.finish_broadcast(bc_id, sent, delivered_ids)
    await safe_edit(call, f"✅ Рассылка завершена: <b>{sent}/{len(clients)}</b>\n\n"
                          f"Ответы клиентов появятся в «💬 Обратная связь».", kb_back("admin_clients"))

@admin_router.callback_query(F.data == "admin_bcast_cancel")
async def admin_bcast_cancel_cb(call: CallbackQuery):
    _broadcast_draft.pop(call.from_user.id, None)
    await safe_edit(call, "❌ Рассылка отменена", kb_back("admin_clients"))


# ═══════════════════════════════════════════════════════════════
# ADMIN — ЛОГИ
# ═══════════════════════════════════════════════════════════════
@admin_router.callback_query(F.data == "admin_logs")
async def admin_logs_cb(call: CallbackQuery):
    today = datetime.now(MSK).strftime("%Y-%m-%d")
    cnt   = _db.count_logs(today)
    text  = f"<b>📜 Логи за {today} (МСК)</b>\nСтрок: <b>{cnt}</b>"
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
    skip_nouser = _db.get_setting("skip_no_username","0") == "1"
    markup = mkb([
        [(f"🤖 ИИ {'включено' if ai_on else 'выключено'}", "admin_settings_toggle:ai_active")],
        [(f"🖥️ Мониторинг {'включен' if mon_on else 'выключен'}", "admin_settings_toggle:monitoring_active")],
        [(f"👥 Клиент бот {'включен' if cb_on else 'выключен'}", "admin_settings_toggle:client_bot_active")],
        [(f"📵 Вакансии без юзернейма: {'пропускаем' if skip_nouser else 'присылаем'}",
          "admin_settings_toggle:skip_no_username")],
        [("📣 Оповещения", "admin_notifications")],
        [("🎁 Бесплатный пак", "admin_free_pack")],
        [("✉️ Тексты сообщений","admin_msg_texts"), ("📨 Рассылки","admin_broadcast_settings")],
        [("💱 Резервный курс USDT","admin_usdt_rate")],
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

@admin_router.callback_query(F.data == "admin_usdt_rate")
async def admin_usdt_rate_cb(call: CallbackQuery):
    """Резервный курс USDT→RUB — используется ТОЛЬКО когда недоступны оба
    публичных источника (Binance P2P и Bybit P2P) и нет ни одного
    закэшированного курса (например, сразу после перезапуска бота). Без
    этого в таком случае клиент вообще не смог бы увидеть сумму к оплате
    в USDT — сейчас именно так и произошло."""
    current = _db.get_setting("usdt_manual_rate", "")
    _admin_pending[call.from_user.id] = "edit_usdt_manual_rate"
    text = (
        f"💱 <b>Резервный курс USDT→RUB</b>\n\n"
        f"Сейчас: <b>{current or '— не задан'}</b>\n\n"
        f"Используется только если недоступны оба публичных источника курса "
        f"(Binance P2P и Bybit P2P) и нет ни одного сохранённого курса за "
        f"последние {RATE_CACHE_TTL} сек. Держите его близко к реальному "
        f"рыночному, чтобы не терять на разнице при выводе.\n\n"
        f"Пришлите число, например <code>95.5</code>, или <code>-</code> чтобы убрать:"
    )
    await safe_edit(call, text, kb_back("admin_settings"))

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
    "msg_free_pack_offer": {
        "label": "🎁 Предложение бесплатного пака при /start",
        "default": (
            "🎁 <b>Дарим бесплатный пак для монтажа</b> — LUT'ы, пресеты и "
            "проектные файлы.\n\nЗаберите, пока настраиваем поиск вакансий под вас:"
        ),
    },
    "msg_free_pack_caption": {
        "label": "🎁 Подпись к файлам бесплатного пака",
        "default": "🎁 Ваш бесплатный пак — держите! Пользуйтесь на здоровье 🙌",
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

@admin_router.callback_query(F.data == "admin_free_pack")
async def admin_free_pack_cb(call: CallbackQuery):
    on      = _db.get_setting("free_pack_enabled", "1") == "1"
    files   = _db.list_free_pack_files()
    claims  = _db.count_free_pack_claims()
    rows = [[(f"🎁 Предложение при /start: {'включено' if on else 'выключено'}",
              "admin_settings_toggle:free_pack_enabled")]]
    for f in files:
        rows.append([(f"📄 {f['file_name']}"[:40], "noop"),
                     ("🗑", f"admin_freepack_del:{f['id']}")])
    rows.append([("➕ Добавить файл", "admin_freepack_add")])
    rows.append([("✉️ Тексты (предложение / подпись)", "admin_msg_texts")])
    rows.append([("◀️ Назад", "admin_settings")])
    files_txt = "\n".join(f"• {f['file_name']}" for f in files) or "— пока пусто —"
    await safe_edit(call,
        f"<b>🎁 Бесплатный пак</b>\n\n"
        f"Предлагается новым клиентам при /start, до старта воронки подписки "
        f"(LUT'ы, пресеты, проектные файлы — что загрузите, то и отправится).\n\n"
        f"<b>Файлы в паке:</b>\n{files_txt}\n\n"
        f"Забрали пак: <b>{claims}</b> клиентов.",
        mkb(rows))

@admin_router.callback_query(F.data == "admin_freepack_add")
async def admin_freepack_add_cb(call: CallbackQuery):
    _admin_pending[call.from_user.id] = "add_free_pack_file"
    await safe_edit(call,
        "📎 Пришлите файл (LUT, пресет, проектный файл — любым документом, "
        "не как фото) — он добавится в пак.",
        kb_back("admin_free_pack"))

@admin_router.callback_query(F.data.startswith("admin_freepack_del:"))
async def admin_freepack_del_cb(call: CallbackQuery):
    fid = int(call.data.split(":")[1])
    _db.delete_free_pack_file(fid)
    await call.answer("Удалено")
    await admin_free_pack_cb(call)

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
    if key == "free_pack_enabled":
        await admin_free_pack_cb(call)
    else:
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
# ADMIN — ОБРАБОТЧИК ДОКУМЕНТОВ (бесплатный пак)
# ═══════════════════════════════════════════════════════════════
@admin_router.message(F.document)
async def admin_document_handler(msg: Message):
    uid    = msg.from_user.id
    action = _admin_pending.pop(uid, None)
    if action == "add_free_pack_file":
        doc = msg.document
        _db.add_free_pack_file(doc.file_id, doc.file_name or "файл")
        await safe_answer(msg, f"✅ Добавлено: {doc.file_name or 'файл'}",
                           kb_back("admin_free_pack"))
        return
    if action:
        _admin_pending[uid] = action  # вернуть — этот файл не по теме, ждём дальше

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
    if action.startswith("ad:"):
        await ad_text_input(msg, action)
        return
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
        _db.set_setting(key, msg.html_text)
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

    if action == "edit_usdt_manual_rate":
        t = text.strip()
        if t == "-":
            _db.set_setting("usdt_manual_rate", "")
            await safe_answer(msg, "✅ Резервный курс убран", kb_back("admin_settings"))
            return
        try:
            val = float(t.replace(",", "."))
            assert val > 0
        except Exception:
            await safe_answer(msg, "❌ Введите число, например 95.5, или - чтобы убрать",
                              kb_back("admin_settings"))
            return
        _db.set_setting("usdt_manual_rate", str(val))
        await safe_answer(msg, f"✅ Резервный курс сохранён: {val}₽ за USDT", kb_back("admin_settings"))
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
            mkb([[("↩️ Отменить", f"admin_revoke_sub_confirm:{cl['id']}")],
                 [("◀️ Назад", "admin_clients")]]))
        try:
            await msg.bot.send_message(tg_id,
                f"🎁 Вам выдана подписка до <b>{fmt_date(until.isoformat())}</b>!",
                parse_mode=ParseMode.HTML, reply_markup=kb_client_main())
            await _upgrade_old_deliveries(msg.bot, cl)
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
            mkb([[("↩️ Отменить", f"admin_revoke_sub_confirm:{client_id}")],
                 [("◀️ Назад", f"admin_client_detail:{client_id}")]]))
        try:
            await msg.bot.send_message(cl["tg_id"],
                f"🎁 Вам выдана подписка до <b>{fmt_date(until.isoformat())}</b>!",
                parse_mode=ParseMode.HTML, reply_markup=kb_client_main())
            await _upgrade_old_deliveries(msg.bot, cl)
        except Exception: pass
        return

    # Блокировка теперь через кнопки admin_block_vac_confirm/admin_block_client_confirm — ввод текста не нужен

    # ── Выдача подписки всем: шаг 1 — дни ────────────────────
    if action == "give_sub_all_days":
        if not text.strip().isdigit() or int(text.strip()) <= 0:
            await safe_answer(msg, "❌ Введите положительное число дней")
            _admin_pending[uid] = "give_sub_all_days"; return
        _give_sub_all_draft_set(uid, {"days": int(text.strip())})
        _admin_pending[uid] = "give_sub_all_comment"
        await safe_answer(msg,
            "💬 Прикрепить комментарий к уведомлению? Пришлите текст, или отправьте <code>-</code> чтобы без комментария:")
        return

    # ── Выдача подписки всем: шаг 2 — комментарий → предпросмотр ──
    if action == "give_sub_all_comment":
        draft = _give_sub_all_draft_get(uid)
        if not draft:
            await safe_answer(msg, "Сессия истекла, начните заново", kb_back("admin_clients")); return
        draft["comment"] = "" if text.strip() == "-" else msg.html_text
        _give_sub_all_draft_set(uid, draft)
        await _show_give_sub_all_preview(msg.bot, uid)
        return

    # ── Рассылка ──────────────────────────────────────────────
    if action == "broadcast_text":
        draft = _broadcast_draft.get(uid) or {}
        draft["text"] = msg.html_text; draft["photo"] = None
        _broadcast_draft[uid] = draft
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

def _free_days_phrase(n: int) -> str:
    """'3 бесплатных дня' / '1 бесплатный день' / '5 бесплатных дней' —
    склонение по числу, чтобы текст не врал, если FREE_DAYS поменяют
    через переменную окружения."""
    n = int(n)
    last2, last1 = n % 100, n % 10
    if last1 == 1 and last2 != 11:
        word = "бесплатный день"
    elif 2 <= last1 <= 4 and not (12 <= last2 <= 14):
        word = "бесплатных дня"
    else:
        word = "бесплатных дней"
    return f"{n} {word}"

def _client_main_text(cl: dict, is_new: bool = False) -> str:
    has_sub = bool(cl.get("sub_until")) and cl["sub_until"] > datetime.now().isoformat()
    if cl.get("frozen_at"):
        sub = "❄️ <b>Заморожена</b> (перейдите в настройки)"
    elif has_sub:
        sub = f"📅 Срок действия подписки: {fmt_date(cl.get('sub_until'))}"
    else:
        sub = "📅 Срок действия подписки: <b>Истекла</b>"
    bonus = f"\n<b>Тебе начислено +{_free_days_phrase(FREE_DAYS)}</b>" if is_new else ""
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
        f"{sub}"
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
        await _upgrade_old_deliveries(msg.bot, cl)

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

    try: _ad_track_start(msg, cl, is_new)
    except Exception as e: log.warning(f"ad_track_start: {e}")

    await msg.answer(_client_main_text(cl, is_new), reply_markup=kb_client_main())
    if is_new:
        # Лид-магнит: бесплатный пак исходников (LUT'ы/пресеты/проекты) —
        # предлагаем ДО воронки подписки, чтобы новый клиент получил
        # ценность сразу, а не только после того как захочет платить.
        if _db.get_setting("free_pack_enabled", "1") == "1" and _db.list_free_pack_files():
            offer = _db.get_setting("msg_free_pack_offer",
                                     CLIENT_MSG_TEMPLATES["msg_free_pack_offer"]["default"])
            await msg.answer(offer, parse_mode=ParseMode.HTML,
                              reply_markup=mkb([[("🎁 Забрать бесплатный пак","client_freepack_claim")]]))
        # Между приветствием и сообщением "поиск запущен" — предложение
        # пройти короткое обучение по функциям бота. Часть новых
        # пользователей засыпает вопросами в личку вместо того, чтобы
        # разобраться самим — обучение снимает большинство таких вопросов
        # заранее, а кто и так всё понимает — жмёт "Пропустить" за секунду.
        await msg.answer(
            "❓ <b>Если у вас есть вопросы по работе бота</b> — пройдите "
            "короткое обучение по кнопке ниже. Это займёт меньше минуты.",
            parse_mode=ParseMode.HTML,
            reply_markup=mkb([
                [("🎓 Пройти обучение","client_onb_start")],
                [("⏭ Пропустить","client_onb_skip")],
            ]))

async def _send_search_started_message(bot: Bot, tg_id: int) -> None:
    """Отдельным сообщением, явно и заметно — часть новых пользователей не
    понимает, что поиск вакансий уже идёт автоматически, и ждёт какого-то
    дополнительного действия от себя, чтобы «запустить» его. Текст
    редактируется через ⚙️ Настройки → ✉️ Тексты сообщений. Отправляется
    после обучения (или сразу, если его пропустили) — см. client_onb_*."""
    text = _db.get_setting("msg_welcome_search_started",
                           CLIENT_MSG_TEMPLATES["msg_welcome_search_started"]["default"])
    try:
        await bot.send_message(tg_id, text, parse_mode=ParseMode.HTML)
    except Exception: pass

# ── Обучение для новых клиентов ──────────────────────────────
# Контент шагов: (заголовок, текст). Порядок соответствует реальному
# функционалу бота — если добавляется новая функция клиенту, стоит
# добавить/поправить шаг здесь же.
TUTORIAL_STEPS: list[tuple[str, str]] = [
    ("🔍 Как это работает",
     "Юзербот круглосуточно мониторит десятки чатов и каналов с вакансиями "
     "на видеомонтаж.\n\nВам не нужно ничего искать вручную — как только "
     "найдётся подходящая вакансия, она сама придёт сюда, в этот чат."),
    ("🔓 Что открывает подписка",
     "Без подписки вакансии тоже приходят — но контакт автора скрыт частично "
     "(например, <code>@iv***</code>), а вместо перехода к сообщению — "
     "кнопка «Как откликнуться?».\n\n"
     "С подпиской контакт открыт полностью, и доступна кнопка "
     "«Перейти к сообщению» — прямой переход в чат-источник."),
    ("💳 Тарифы и оплата",
     "Оплатить подписку можно переводом по номеру телефона или в USDT — "
     "на выбор сеть <b>TRC20 (Tron)</b> или <b>TON</b>.\n\n"
     "Посмотреть тарифы можно в любой момент — кнопка «💳 Тарифы» "
     "в главном меню бота."),
    ("👥 Реферальная программа",
     f"Приглашайте друзей по своей персональной ссылке. Когда приглашённый "
     f"оплатит любой тариф:\n\n"
     f"• вам начислится <b>+{REF_BONUS_DAYS} дней</b>\n"
     f"• другу — <b>+{REF_DAYS} дней</b>\n\n"
     f"Ссылка — в разделе «👥 Реферальная программа»."),
    ("⚙️ Настройки",
     "В разделе «⚙️ Настройки» можно:\n\n"
     "• добавить свои <b>стоп-слова</b> — вакансии с ними попадать не будут\n"
     "• полностью <b>включить или выключить поиск</b>, если нужен перерыв"),
]

def _onboarding_text(idx: int) -> str:
    title, body = TUTORIAL_STEPS[idx]
    return f"<b>{title}</b>\n<i>Шаг {idx + 1} из {len(TUTORIAL_STEPS)}</i>\n\n{body}"

def _onboarding_markup(idx: int) -> InlineKeyboardMarkup:
    is_last  = idx == len(TUTORIAL_STEPS) - 1
    next_btn = ("🏁 Завершить обучение", "client_onb_finish") if is_last \
               else ("➡️ Следующий шаг", f"client_onb_next:{idx + 1}")
    return mkb([[next_btn], [("🚪 Выйти из обучения","client_onb_finish")]])

@client_router.callback_query(F.data == "client_freepack_claim")
async def client_freepack_claim_cb(call: CallbackQuery):
    cl = _db.get_client_by_tg(call.from_user.id)
    if not cl: await call.answer(); return
    files = _db.list_free_pack_files()
    if not files:
        await call.answer("Пак сейчас пуст, загляните позже 🙂", show_alert=True)
        return
    await call.answer()
    caption = _db.get_setting("msg_free_pack_caption",
                               CLIENT_MSG_TEMPLATES["msg_free_pack_caption"]["default"])
    try:
        for i, f in enumerate(files):
            await call.bot.send_document(
                call.from_user.id, f["file_id"],
                caption=caption if i == 0 else None,
                parse_mode=ParseMode.HTML if i == 0 else None)
        if not cl.get("free_pack_claimed_at"):
            _db.mark_free_pack_claimed(cl["id"])
        await safe_edit(call, "🎁 <b>Пак отправлен выше!</b>", None)
    except Exception as e:
        log.warning(f"Отправка бесплатного пака клиенту {call.from_user.id}: {e}")
        await call.message.answer("⚠️ Не получилось отправить пак, напишите нам в поддержку.")

@client_router.callback_query(F.data == "client_onb_start")
async def client_onb_start_cb(call: CallbackQuery):
    await safe_edit(call, _onboarding_text(0), _onboarding_markup(0))

@client_router.callback_query(F.data.startswith("client_onb_next:"))
async def client_onb_next_cb(call: CallbackQuery):
    idx = int(call.data.split(":")[1])
    if not (0 <= idx < len(TUTORIAL_STEPS)): await call.answer(); return
    await safe_edit(call, _onboarding_text(idx), _onboarding_markup(idx))

@client_router.callback_query(F.data == "client_onb_skip")
async def client_onb_skip_cb(call: CallbackQuery):
    await call.answer()
    await safe_edit(call, "Хорошо, пропускаем 🙂", None)
    await _send_search_started_message(call.bot, call.from_user.id)

@client_router.callback_query(F.data == "client_onb_finish")
async def client_onb_finish_cb(call: CallbackQuery):
    await call.answer()
    await safe_edit(call, "✅ <b>Обучение пройдено!</b>", None)
    await _send_search_started_message(call.bot, call.from_user.id)


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
        f"30+ вакансий для монтажа в день. Работает быстро и стабильно."
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
        rows.append([(_tariff_btn_label(key, price, fire), f"client_buy:{key}")])
    rows.append([("◀️ Главное меню","client_main")])
    return mkb(rows)

def _winback_price(cl: dict, tariff: str) -> int:
    """Цена тарифа со скидкой 15% для win-back сообщения (напоминание после
    окончания подписки, см. _check_expired_subs). Скидки не суммируются:
    это всегда full − 15% (= sale), одинаково для новых и для платившего
    клиента. Клиенту, который ещё не платил, это та же цена, что и так у него
    на кнопках, — ниже неё не бывает."""
    return PRICES[tariff]["sale"]

def _tariffs_kb_winback(cl: dict) -> InlineKeyboardMarkup:
    """Тарифы со скидкой 15% — специально для win-back напоминания клиентам,
    у которых закончилась подписка (текст "Дарим скидку 15%..." в
    _check_expired_subs). client_buy:{tariff}:wb15 — client_buy_cb по этому
    суффиксу знает, что применить скидку, а не обычную цену."""
    rows = []
    for key, p in PRICES.items():
        price = _winback_price(cl, key)
        rows.append([(_tariff_btn_label(key, price, "🔥"), f"client_buy:{key}:wb15")])
    rows.append([("◀️ Главное меню","client_main")])
    return mkb(rows)

@client_router.callback_query(F.data == "client_tariffs")
async def client_tariffs_cb(call: CallbackQuery):
    uid = call.from_user.id; uname = call.from_user.username
    cl  = _db.get_or_create_client(uid, uname)
    await safe_edit(call, await _tariffs_text(cl), _tariffs_kb(cl))

@client_router.callback_query(F.data == "client_how_reply")
async def client_how_reply_cb(call: CallbackQuery):
    """Кнопка "Как откликнуться?" под вакансией у клиентов без подписки —
    объясняет, зачем нужна подписка, и сразу даёт тарифы. Отправляем отдельным
    новым сообщением (не редактируем вакансию), чтобы она осталась как есть."""
    uid = call.from_user.id; uname = call.from_user.username
    cl  = _db.get_or_create_client(uid, uname)
    await call.answer()
    seen = cl.get("free_vacancies_seen") or 0
    extra = f" Вы уже получили <b>{seen}</b> таких вакансий." if seen >= 2 else ""
    await call.message.answer(
        f"Чтобы откликнуться на вакансию, нужна подписка.{extra}\n"
        f"С подпиской контакт автора виден сразу, без ожидания.",
        reply_markup=_tariffs_kb(cl), parse_mode=ParseMode.HTML)

WINBACK_VALID_HOURS = 24  # срок действия скидочного предложения (см. _set_winback_offer/_winback_valid)

def _set_winback_offer(client_id: int) -> None:
    """Отмечает момент отправки скидочного предложения (win-back после
    истечения подписки или разовый пуш для долго-бесплатных) — от этой
    отметки отсчитывается срок действия скидки (см. _winback_valid).
    Без дедлайна скидка висела бы бессрочно, и эффект срочности не работал бы."""
    _db.set_setting(f"wb15_offer_ts_{client_id}", datetime.now().isoformat())

def _winback_valid(client_id: int) -> bool:
    ts = _db.get_setting(f"wb15_offer_ts_{client_id}", "")
    if not ts: return False
    try:
        sent = datetime.fromisoformat(ts)
    except Exception:
        return False
    return (datetime.now() - sent) < timedelta(hours=WINBACK_VALID_HOURS)

@client_router.callback_query(F.data.startswith("client_buy:"))
async def client_buy_cb(call: CallbackQuery):
    uid     = call.from_user.id
    parts   = call.data.split(":")
    tariff  = parts[1]
    winback = len(parts) > 2 and parts[2] == "wb15"
    p       = PRICES.get(tariff)
    if not p: await call.answer("Неверный тариф"); return
    cl      = _db.get_or_create_client(uid, call.from_user.username)
    if winback and not _winback_valid(cl["id"]):
        await call.answer("Скидка уже истекла", show_alert=True)
        await safe_edit(call, "⏰ Срок скидочного предложения истёк — но вот актуальные тарифы:",
                        _tariffs_kb(cl))
        return
    has_paid = bool(cl.get("first_payment"))
    amount  = _winback_price(cl, tariff) if winback else (p["full"] if has_paid else p["sale"])
    ticket  = f"DRAFT-{int(time.time()*1000)}"
    _payment_drafts[ticket] = {"client_id": cl["id"], "tariff": tariff, "amount": amount,
                              "days": p["days"], "method": "rub"}
    log.info(f"Черновик оплаты создан: {ticket} (клиент {uid}, тариф {tariff}) — в БД пока не пишем, "
             f"пока клиент не нажмёт «Оплатил(а)»")
    fire    = "" if (has_paid and not winback) else "🔥"
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
        [("💱 Оплатить в USDT", f"client_crypto_choose:{tariff}" + (":wb15" if winback else ""))],
        [("◀️ Главное меню","client_main")],
    ])
    await safe_edit(call, text, markup)

@client_router.callback_query(F.data.startswith("client_crypto_choose:"))
async def client_crypto_choose_cb(call: CallbackQuery):
    """Шаг выбора сети USDT — реквизиты разные для TRC20 и TON, показывать
    сразу оба варианта одним экраном было бы легко перепутать по невнимательности."""
    parts   = call.data.split(":")
    tariff  = parts[1]
    winback = len(parts) > 2 and parts[2] == "wb15"
    if tariff not in PRICES: await call.answer("Неверный тариф"); return
    if winback:
        cl = _db.get_or_create_client(call.from_user.id, call.from_user.username)
        if not _winback_valid(cl["id"]):
            await call.answer("Скидка уже истекла", show_alert=True)
            await safe_edit(call, "⏰ Срок скидочного предложения истёк — но вот актуальные тарифы:",
                            _tariffs_kb(cl))
            return
    suffix = ":wb15" if winback else ""
    rows = [[(f"{w['icon']} {w['label']}", f"client_buy_crypto:{tariff}:{key}{suffix}")]
            for key, w in CRYPTO_WALLETS.items()]
    rows.append([("◀️ Назад", f"client_buy:{tariff}{suffix}")])
    await safe_edit(call, "💱 <b>Выберите сеть для оплаты USDT:</b>", mkb(rows))

@client_router.callback_query(F.data.startswith("client_buy_crypto:"))
async def client_buy_crypto_cb(call: CallbackQuery):
    uid     = call.from_user.id
    parts   = call.data.split(":")
    tariff  = parts[1]
    network = parts[2] if len(parts) > 2 else "trc20"
    winback = len(parts) > 3 and parts[3] == "wb15"
    p       = PRICES.get(tariff)
    wallet  = CRYPTO_WALLETS.get(network)
    if not p: await call.answer("Неверный тариф"); return
    if not wallet:
        await call.answer("Оплата в USDT временно недоступна", show_alert=True); return
    cl       = _db.get_or_create_client(uid, call.from_user.username)
    if winback and not _winback_valid(cl["id"]):
        await call.answer("Скидка уже истекла", show_alert=True)
        await safe_edit(call, "⏰ Срок скидочного предложения истёк — но вот актуальные тарифы:",
                        _tariffs_kb(cl))
        return
    has_paid = bool(cl.get("first_payment"))
    amount   = _winback_price(cl, tariff) if winback else (p["full"] if has_paid else p["sale"])
    rate     = await get_usdt_rub_rate()
    if not rate:
        await call.answer("Не удалось получить курс, попробуйте ещё раз через минуту", show_alert=True)
        return
    usdt_amount = round(amount / rate, 2)
    ticket = f"DRAFT-{int(time.time()*1000)}"
    _payment_drafts[ticket] = {"client_id": cl["id"], "tariff": tariff, "amount": amount,
                              "days": p["days"], "method": f"usdt_{network}",
                              "crypto_amount": usdt_amount, "crypto_rate": rate}
    log.info(f"Черновик оплаты создан: {ticket} (клиент {uid}, тариф {tariff}, USDT/{network}) — в БД пока не пишем")
    fire = "" if has_paid else "🔥"
    text = (
        f"Тариф <b>{p['label']}</b>\n\n"
        f"{wallet['icon']} <b>{wallet['label']}</b>\n"
        f"<blockquote><code>{wallet['address']}</code></blockquote>\n"
        f"К оплате: <b>{usdt_amount} USDT</b>{fire}\n"
        f"<i>(≈ {amount}₽ по курсу {rate}₽ за USDT)</i>\n\n"
        f"⚠️ <b>Отправляйте строго в сети {wallet['label']}</b> — перевод в другой сети до получателя не дойдёт и не восстанавливается\n"
        f"⚠️ <b>После оплаты нажмите Оплатил(а)</b>\n\n"
        f"<i>Курс актуален короткое время — если не успели, вернитесь назад и выберите сеть заново</i>"
    )
    markup = mkb([
        [("✅ Оплатил(а)", f"client_paid:{ticket}")],
        [("◀️ Назад", f"client_crypto_choose:{tariff}" + (":wb15" if winback else ""))],
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
    if p.get("method", "").startswith("usdt"):
        network = p["method"].split("_", 1)[1] if "_" in p["method"] else "trc20"
        wallet  = CRYPTO_WALLETS.get(network, {})
        pay_line = (f"Тариф: {p['tariff']} | Сумма: <b>{p['crypto_amount']} USDT</b> "
                    f"(курс {p['crypto_rate']}₽, ≈{p['amount']}₽)\n"
                    f"Сеть: {wallet.get('label', network)} | Кошелёк: <code>{wallet.get('address','?')}</code>")
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
                            await _upgrade_old_deliveries(call.bot, ref_cl)
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
                await _upgrade_old_deliveries(call.bot, cl)
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
    frozen      = bool(cl.get("frozen_at"))
    has_sub     = bool(cl.get("sub_until")) and cl["sub_until"] > datetime.now().isoformat()
    freeze_line = ""
    freeze_btn  = None
    if frozen:
        # Подписка уже была активна в момент заморозки — кнопка нужна
        # независимо от текущего sub_until (он не двигается, пока заморожено).
        freeze_line = "\n❄️ Подписка заморожена — вакансии не приходят"
        freeze_btn  = ("🔥 Разморозить подписку", "client_unfreeze")
    elif has_sub:
        freeze_line = f"\n⏳ Осталось: {fmt_remaining(cl.get('sub_until'))}"
        freeze_btn  = ("❄️ Заморозить подписку", "client_freeze_confirm")
    # Если подписки нет/закончилась и она не заморожена — кнопки заморозки
    # нет вообще: замораживать нечего, пока не куплена или не выдана новая.
    text   = f"Настройки <b>phase.parser</b>\n{freeze_line}"
    rows   = [[("🚫 Стоп-слова","client_stopwords")]]
    if freeze_btn: rows.append([freeze_btn])
    rows.append([("◀️ Главное меню","client_main")])
    markup = mkb(rows)
    if isinstance(msg_or_call, CallbackQuery):
        await safe_edit(msg_or_call, text, markup)
    else:
        await safe_answer(msg_or_call, text, markup)

@client_router.callback_query(F.data == "client_settings")
async def client_settings_cb(call: CallbackQuery):
    cl = _db.get_or_create_client(call.from_user.id, call.from_user.username)
    await _show_client_settings(call, cl)

@client_router.callback_query(F.data == "client_freeze_confirm")
async def client_freeze_confirm_cb(call: CallbackQuery):
    markup = mkb([
        [("✅ Да","client_freeze_do"), ("❌ Нет","client_settings")],
        [("◀️ Назад","client_settings")],
    ])
    await safe_edit(call,
        "Выключить уведомление о вакансиях?\n"
        "Новые вакансии не будут приходить до разморозки подписки",
        markup)

@client_router.callback_query(F.data == "client_freeze_do")
async def client_freeze_do_cb(call: CallbackQuery):
    cl = _db.get_or_create_client(call.from_user.id, call.from_user.username)
    _db.freeze_subscription(cl["id"])
    cl = _db.get_client_by_tg(call.from_user.id)
    await _show_client_settings(call, cl)

@client_router.callback_query(F.data == "client_unfreeze")
async def client_unfreeze_cb(call: CallbackQuery):
    cl = _db.get_or_create_client(call.from_user.id, call.from_user.username)
    _db.unfreeze_subscription(cl["id"])
    cl = _db.get_client_by_tg(call.from_user.id)
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

    # Свободное сообщение, не относящееся ни к одному сценарию — функция
    # "обратная связь" убрана по просьбе владельца бота: такие сообщения
    # больше никуда не сохраняются и не пересылаются админу, бот их просто
    # игнорирует (клиенту ничего отвечать не нужно).
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
# ПРОВЕРКА ЗДОРОВЬЯ ИИ-ПРОВЕРКИ ВАКАНСИЙ
# ═══════════════════════════════════════════════════════════════
_ai_health = {"was_ok": True}

async def _check_ai_health(bot: Bot) -> None:
    """Проверяет каждые 5 минут, включён ли ИИ и доступен ли DeepSeek. Пока
    ИИ не работает, вакансии не одобряются автоматически (отказ по умолчанию —
    см. _process), а просто копятся с отметкой 'ИИ выключен — не проверено'.
    Как только ИИ снова заработал — автоматически довыполняет проверку по всем
    накопленным пропущенным вакансиям через pipeline.recheck_skipped_vacancies()."""
    while True:
        await asyncio.sleep(300)
        try:
            ai_on  = _db.get_setting("ai_active","1") == "1"
            status = await check_deepseek_status() if ai_on else "disabled"
            ok     = ai_on and status == "ok"
            was_ok = _ai_health["was_ok"]

            if not ok and was_ok:
                _ai_health["was_ok"] = False
                reason = "выключен вручную (⚙️ Настройки)" if not ai_on else f"DeepSeek недоступен ({status})"
                log.error(f"ИИ-проверка не работает: {reason}")
                try:
                    await bot.send_message(ADMIN_ID,
                        f"🔴 <b>ИИ-проверка вакансий не работает!</b>\n\nПричина: {reason}\n\n"
                        f"Вакансии, совпавшие по ключевым словам, сейчас никому не отправляются "
                        f"(отказ по умолчанию, не одобрение) — как только почините, я автоматически "
                        f"довыполню проверку по всем пропущенным за это время.",
                        parse_mode=ParseMode.HTML)
                except Exception: pass

            elif ok and not was_ok:
                _ai_health["was_ok"] = True
                log.info("ИИ-проверка снова работает — довыполняю пропущенные вакансии")
                try:
                    await bot.send_message(ADMIN_ID,
                        "🟢 <b>ИИ-проверка снова работает</b> — довыполняю проверку пропущенных вакансий...",
                        parse_mode=ParseMode.HTML)
                except Exception: pass
                if _pipeline:
                    n = await _pipeline.recheck_skipped_vacancies()
                    if n:
                        try:
                            await bot.send_message(ADMIN_ID,
                                f"✅ Довыполнено: <b>{n}</b> пропущенных вакансий проверено.",
                                parse_mode=ParseMode.HTML)
                        except Exception: pass
        except Exception as e:
            log.error(f"_check_ai_health: {e}")

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
                "SELECT * FROM clients WHERE sub_until IS NOT NULL AND sub_until < ? AND sub_until > ? "
                "AND frozen_at IS NULL",
                (now.isoformat(), week)).fetchall()
            for row in rows:
                cl = dict(row)
                # Проверяем что не отправляли сегодня
                sent_key = f"expired_notif_{cl['id']}"
                last = _db.get_setting(sent_key,"")
                if last and last == now.strftime("%Y-%m-%d"): continue
                try:
                    await bot.send_message(cl["tg_id"],
                        f"Дарим скидку <b>15%</b> на все тарифы!\n"
                        f"Предложение действует <b>{WINBACK_VALID_HOURS} часа</b>.\n"
                        f"Подключитесь чтобы получать новые заказы",
                        parse_mode=ParseMode.HTML,
                        reply_markup=_tariffs_kb_winback(cl))
                    _db.set_setting(sent_key, now.strftime("%Y-%m-%d"))
                    _set_winback_offer(cl["id"])
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
                "SELECT * FROM clients WHERE sub_until IS NOT NULL AND sub_until > ? AND sub_until <= ? "
                "AND frozen_at IS NULL",
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

async def _send_mid_trial_reminders(bot: Bot) -> None:
    """Напоминание в середине бесплатного периода (не путать с напоминанием
    об истечении — это раньше, примерно на середине FREE_DAYS) — чтобы
    клиент не забыл о боте на второй день из трёх и увидел пользу ДО того,
    как включится "паника перед концом". Шлётся только тем, кто ни разу не
    платил (first_payment=0) — то есть именно на бесплатном пробном
    периоде, а не продлившим платную подписку. Ровно один раз за период —
    та же логика дедупа, что и у напоминания об истечении."""
    while True:
        await asyncio.sleep(1800)
        try:
            midpoint_hours = max(1, FREE_DAYS * 24 // 2)
            now  = datetime.now()
            soon = (now + timedelta(hours=midpoint_hours)).isoformat()
            rows = _db._c().execute(
                "SELECT * FROM clients WHERE sub_until IS NOT NULL AND sub_until > ? AND sub_until <= ? "
                "AND (first_payment IS NULL OR first_payment=0) AND frozen_at IS NULL",
                (now.isoformat(), soon)).fetchall()
            for row in rows:
                cl = dict(row)
                sent_key = f"mid_trial_{cl['id']}_{cl['sub_until']}"
                if _db.get_setting(sent_key, ""): continue
                # Во время активного триала контакты УЖЕ открыты (sub_until в
                # будущем) — free_vacancies_seen тут не подходит, он считает
                # только показы БЕЗ подписки и во время триала всегда равен 0.
                # Нужно общее число доставленных вакансий за это время.
                seen = _db.count_deliveries(cl["id"])
                try:
                    await bot.send_message(cl["tg_id"],
                        f"👋 Напоминаем про <b>phase.parser</b> — вы сейчас на пробном периоде "
                        f"с полным доступом к контактам.\n\n"
                        f"Уже получили <b>{seen}</b> вакансий с открытыми контактами. "
                        f"Пробный период скоро закончится — оформите подписку сейчас, "
                        f"чтобы не потерять доступ.",
                        parse_mode=ParseMode.HTML,
                        reply_markup=mkb([[("💳 Тарифы","client_tariffs")]]))
                    _db.set_setting(sent_key, "1")
                except Exception: pass
        except Exception as e: log.error(f"_send_mid_trial_reminders: {e}")

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
# ═══════════════════════════════════════════════════════════════
# РЕКЛАМА — привлечение новых пользователей (Клиенты → 📣 Реклама)
# ───────────────────────────────────────────────────────────────
# Кампания = пул ваших текстов + чаты + аккаунты + расписание.
# Тексты публикуются без изменений (кроме подстановки {переменных}),
# выбираются случайно и не повторяются в одном чате заданное число дней.
# Через заданное время пост удаляется. Все лимиты настраиваются:
# общие / на аккаунт / на чат / на кампанию (см. AD_FIELDS).
# ═══════════════════════════════════════════════════════════════
AD_DAYS_RU = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]

# поле: (короткое имя, подсказка, min, max, области, значение по умолчанию)
# области: g — общие, a — аккаунт, c — чат, k — кампания.
# Приоритет при расчёте: чат → кампания → аккаунт → общие → умолчание.
AD_FIELDS: dict[str, tuple] = {
    "acc_per_day":      ("Постов/сутки с аккаунта", "Сколько постов может сделать один аккаунт за последние 24 часа. 0 — без лимита.", 0, 10000, "ga", 5),
    "acc_per_hour":     ("Постов/час с аккаунта", "Лимит постов аккаунта за последний час. 0 — без лимита.", 0, 1000, "ga", 0),
    "acc_gap_min":      ("Пауза аккаунта, мин", "Минимум минут между двумя постами одного аккаунта (в любые чаты). 0 — без паузы.", 0, 100000, "ga", 30),
    "hour_from":        ("Окно: с часа (МСК)", "Начало окна публикаций, час по Москве 0–23.", 0, 23, "gak", 0),
    "hour_to":          ("Окно: до часа (МСК)", "Конец окна публикаций, 1–24. Если «до» меньше «с», окно переходит через полночь.", 1, 24, "gak", 24),
    "chat_interval_min": ("Пауза в чате, мин", "Минимум минут между нашими постами в одном чате. 0 — без паузы.", 0, 1000000, "gck", 120),
    "chat_per_day":     ("Постов/сутки в чате", "Сколько постов можно сделать в одном чате за 24 часа. 0 — без лимита.", 0, 1000, "gck", 1),
    "jitter_min":       ("Случайная добавка, мин", "К паузе в чате добавляется случайное число минут от 0 до этого значения.", 0, 100000, "gck", 0),
    "delete_after_h":   ("Удалять через, часов", "Через сколько часов после публикации пост удаляется (можно дробное, например 0.5). 0 — не удалять.", 0, 100000, "gck", 24),
    "no_repeat_days":   ("Не повторять текст, дней", "Один и тот же текст не публикуется повторно в том же чате раньше, чем через столько дней. 0 — можно повторять.", 0, 3650, "gck", 7),
    "link_preview":     ("Превью ссылок (1/0)", "1 — показывать превью ссылок в посте, 0 — не показывать.", 0, 1, "gk", 0),
    "silent":           ("Без звука (1/0)", "1 — отправлять без уведомления участникам, 0 — обычная отправка.", 0, 1, "gk", 0),
    "flood_extra_s":    ("Запас после FloodWait, сек", "Сколько секунд добавить к ожиданию, которое требует Telegram.", 0, 86400, "g", 30),
    "del_retry_max":    ("Попыток удаления", "Сколько раз пробовать удалить пост, прежде чем признать неудачей.", 1, 100, "g", 5),
    "tick_s":           ("Проверка расписания, сек", "Как часто планировщик ищет, что пора публиковать.", 5, 3600, "g", 30),
}
AD_SCOPE_TABLE = {"a": "ad_accounts", "c": "ad_chats", "k": "ad_campaigns"}
AD_SCOPE_NAME = {"g": "Общие настройки", "a": "Аккаунт", "c": "Чат", "k": "Кампания"}

AD_SCHEMA = """
CREATE TABLE IF NOT EXISTS ad_accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT, phone TEXT, session TEXT, tg_id INTEGER,
    username TEXT, name TEXT, status TEXT NOT NULL DEFAULT 'active', last_error TEXT,
    cool_until TEXT, cfg TEXT NOT NULL DEFAULT '{}', created_at TEXT DEFAULT (datetime('now')));
CREATE TABLE IF NOT EXISTS ad_chats (
    id INTEGER PRIMARY KEY AUTOINCREMENT, link TEXT NOT NULL, title TEXT,
    enabled INTEGER NOT NULL DEFAULT 1, next_post_at TEXT, cfg TEXT NOT NULL DEFAULT '{}',
    created_at TEXT DEFAULT (datetime('now')));
CREATE TABLE IF NOT EXISTS ad_chat_access (
    chat_id INTEGER NOT NULL, account_id INTEGER NOT NULL, peer TEXT,
    can_write INTEGER, slowmode INTEGER DEFAULT 0, note TEXT, checked_at TEXT,
    PRIMARY KEY (chat_id, account_id));
CREATE TABLE IF NOT EXISTS ad_campaigns (
    id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 0,
    days TEXT NOT NULL DEFAULT '1111111', cfg TEXT NOT NULL DEFAULT '{}',
    created_at TEXT DEFAULT (datetime('now')));
CREATE TABLE IF NOT EXISTS ad_campaign_chats (
    campaign_id INTEGER NOT NULL, chat_id INTEGER NOT NULL, PRIMARY KEY (campaign_id, chat_id));
CREATE TABLE IF NOT EXISTS ad_campaign_accounts (
    campaign_id INTEGER NOT NULL, account_id INTEGER NOT NULL, PRIMARY KEY (campaign_id, account_id));
CREATE TABLE IF NOT EXISTS ad_templates (
    id INTEGER PRIMARY KEY AUTOINCREMENT, campaign_id INTEGER NOT NULL, title TEXT, text TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1, created_at TEXT DEFAULT (datetime('now')));
CREATE TABLE IF NOT EXISTS ad_posts (
    id INTEGER PRIMARY KEY AUTOINCREMENT, campaign_id INTEGER, template_id INTEGER,
    account_id INTEGER, chat_id INTEGER, msg_id INTEGER,
    posted_at TEXT NOT NULL DEFAULT (datetime('now')), delete_at TEXT, deleted_at TEXT,
    status TEXT NOT NULL DEFAULT 'posted', del_attempts INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS ad_starts (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL DEFAULT (datetime('now')),
    tg_id INTEGER, campaign_id INTEGER, chat_id INTEGER, account_id INTEGER, template_id INTEGER,
    is_new INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS ad_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL DEFAULT (datetime('now')),
    level TEXT NOT NULL DEFAULT 'info', kind TEXT NOT NULL DEFAULT 'info',
    account_id INTEGER, chat_id INTEGER, campaign_id INTEGER, text TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS ad_posts_chat  ON ad_posts(chat_id, posted_at);
CREATE INDEX IF NOT EXISTS ad_posts_acc   ON ad_posts(account_id, posted_at);
CREATE INDEX IF NOT EXISTS ad_posts_tpl   ON ad_posts(template_id, chat_id, posted_at);
CREATE INDEX IF NOT EXISTS ad_posts_del   ON ad_posts(status, delete_at);
CREATE INDEX IF NOT EXISTS ad_log_ts      ON ad_log(ts);
CREATE INDEX IF NOT EXISTS ad_starts_ts   ON ad_starts(ts);
"""

_ad_clients: dict[int, "TelegramClient"] = {}   # account_id -> подключённый клиент
_ad_client_locks: dict[int, asyncio.Lock] = {}
_ad_auth: dict[int, dict] = {}                  # admin_uid -> состояние входа в аккаунт
_ad_state: dict = {"bot_username": ""}


def _ad_init_tables(db: "Database") -> None:
    db._c().executescript(AD_SCHEMA)
    db._c().commit()
    try: db._c().execute("ALTER TABLE clients ADD COLUMN ad_source TEXT")
    except sqlite3.OperationalError: pass
    db._c().execute("INSERT OR IGNORE INTO settings(key,value) VALUES('ad_enabled','0')")
    db._c().commit()


# ── Время / БД-хелперы ─────────────────────────────────────────
def _ad_now() -> datetime:
    return datetime.now(timezone.utc)

def _ad_ts(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S")

def _ad_msk() -> datetime:
    return _ad_now() + timedelta(hours=3)

def _ad_one(sql: str, args: tuple = ()) -> Optional[dict]:
    r = _db._c().execute(sql, args).fetchone()
    return dict(r) if r else None

def _ad_all(sql: str, args: tuple = ()) -> list[dict]:
    return [dict(r) for r in _db._c().execute(sql, args).fetchall()]

def _ad_exec(sql: str, args: tuple = ()) -> int:
    cur = _db._c().execute(sql, args)
    _db._c().commit()
    return cur.lastrowid

def ad_log_add(kind: str, text: str, level: str = "info",
               acc: Optional[int] = None, chat: Optional[int] = None,
               camp: Optional[int] = None) -> None:
    try:
        _ad_exec("INSERT INTO ad_log(level,kind,account_id,chat_id,campaign_id,text) VALUES(?,?,?,?,?,?)",
                 (level, kind, acc, chat, camp, text[:1500]))
    except Exception as e:
        log.warning(f"ad_log_add: {e}")

async def _ad_notify(bot: Optional["Bot"], text: str, key: str = "", every_min: int = 60) -> None:
    """Сообщение админу; один и тот же key — не чаще раза в every_min минут."""
    if not bot: return
    if key:
        k = f"ad_notify_{key}"
        last = _db.get_setting(k, "")
        if last and last > _ad_ts(_ad_now() - timedelta(minutes=every_min)): return
        _db.set_setting(k, _ad_ts(_ad_now()))
    try: await bot.send_message(ADMIN_ID, text, parse_mode=ParseMode.HTML)
    except Exception as e: log.warning(f"ad_notify: {e}")


# ── Настройки (общие / аккаунт / чат / кампания) ───────────────
def _ad_cfg(row: Optional[dict]) -> dict:
    if not row: return {}
    try:
        d = json.loads(row.get("cfg") or "{}")
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}

def _ad_fmt(v) -> str:
    try:
        f = float(v)
        return str(int(f)) if f == int(f) else f"{f:.2f}".rstrip("0").rstrip(".")
    except Exception:
        return str(v)

def _ad_global(field: str) -> float:
    raw = _db.get_setting(f"ad_g_{field}", "")
    if raw != "":
        try: return float(raw)
        except ValueError: pass
    return AD_FIELDS[field][5]

def ad_val(field: str, *cfgs: Optional[dict]) -> float:
    """Первое заданное значение из cfgs (по приоритету), иначе общее, иначе умолчание."""
    for c in cfgs:
        if c and c.get(field) is not None:
            try: return float(c[field])
            except (TypeError, ValueError): pass
    return _ad_global(field)

def _ad_set_cfg(scope: str, obj_id: int, field: str, value: Optional[float]) -> None:
    if scope == "g":
        if value is None: _ad_exec("DELETE FROM settings WHERE key=?", (f"ad_g_{field}",))
        else: _db.set_setting(f"ad_g_{field}", _ad_fmt(value))
        return
    table = AD_SCOPE_TABLE[scope]
    row = _ad_one(f"SELECT cfg FROM {table} WHERE id=?", (obj_id,))
    cfg = _ad_cfg(row)
    if value is None: cfg.pop(field, None)
    else: cfg[field] = value
    _ad_exec(f"UPDATE {table} SET cfg=? WHERE id=?", (json.dumps(cfg), obj_id))

def _ad_in_hours(h: int, a: int, b: int) -> bool:
    if a == b: return True
    if a < b: return a <= h < b
    return h >= a or h < b


# ── Переменные в текстах ───────────────────────────────────────
def ad_vars() -> dict[str, str]:
    try:
        d = json.loads(_db.get_setting("ad_vars", "{}") or "{}")
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}

def ad_set_vars(d: dict) -> None:
    _db.set_setting("ad_vars", json.dumps(d, ensure_ascii=False))

def ad_tag(camp_id: int = 0, chat_id: int = 0, acc_id: int = 0, tpl_id: int = 0) -> str:
    return f"ad_{camp_id}_{chat_id}_{acc_id}_{tpl_id}"

def ad_bot_link(camp_id=0, chat_id=0, acc_id=0, tpl_id=0) -> str:
    uname = _ad_state.get("bot_username") or "bot"
    return f"https://t.me/{uname}?start={ad_tag(camp_id, chat_id, acc_id, tpl_id)}"

def ad_render(text: str, camp: Optional[dict] = None, chat: Optional[dict] = None,
              acc: Optional[dict] = None, tpl: Optional[dict] = None) -> str:
    m: dict[str, str] = dict(ad_vars())
    m["bot_link"] = ad_bot_link(camp["id"] if camp else 0, chat["id"] if chat else 0,
                                acc["id"] if acc else 0, tpl["id"] if tpl else 0)
    m["bot_username"] = _ad_state.get("bot_username") or ""
    m["chat"] = html.escape((chat or {}).get("title") or "") if chat else ""
    m["campaign"] = html.escape((camp or {}).get("name") or "") if camp else ""
    m["account"] = ("@" + acc["username"]) if acc and acc.get("username") else (acc or {}).get("name") or ""
    return re.sub(r"\{([A-Za-z_][A-Za-z0-9_]*)\}", lambda mo: m.get(mo.group(1), mo.group(0)), text)

def ad_plain(text: str, n: int = 40) -> str:
    s = html.unescape(re.sub(r"<[^>]+>", "", text or "")).strip().replace("\n", " ")
    return (s[:n] + "…") if len(s) > n else s


# ── Ссылки на чаты ─────────────────────────────────────────────
def ad_parse_link(raw: str) -> Optional[tuple[str, str]]:
    """('user', username) | ('invite', hash) | None (некорректная ссылка)."""
    s = (raw or "").strip()
    s = re.sub(r"^(?:https?://)?(?:t\.me|telegram\.me)/", "", s, flags=re.I)
    s = s.lstrip("@").split("?")[0].strip("/")
    if not s: return None
    if s.startswith("+"): return ("invite", s[1:]) if len(s) > 5 else None
    if s.lower().startswith("joinchat/"): return ("invite", s.split("/", 1)[1])
    s = s.split("/")[0]
    if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{3,31}", s): return ("user", s)
    return None

def ad_norm_link(kind: str, val: str) -> str:
    return f"https://t.me/{val}" if kind == "user" else f"https://t.me/+{val}"


# ── Подключение аккаунтов ──────────────────────────────────────
async def ad_get_client(acc_id: int) -> Optional["TelegramClient"]:
    acc = _ad_one("SELECT * FROM ad_accounts WHERE id=?", (acc_id,))
    if not acc or not acc.get("session"): return None
    lock = _ad_client_locks.setdefault(acc_id, asyncio.Lock())
    async with lock:
        cl = _ad_clients.get(acc_id)
        if cl is not None and cl.is_connected(): return cl
        cl = TelegramClient(StringSession(acc["session"]), API_ID, API_HASH)
        try:
            await cl.connect()
            if not await cl.is_user_authorized():
                await cl.disconnect()
                _ad_exec("UPDATE ad_accounts SET status='error', last_error=? WHERE id=?",
                         ("Сессия недействительна — добавьте аккаунт заново", acc_id))
                ad_log_add("account", "Сессия недействительна", "error", acc=acc_id)
                return None
        except Exception as e:
            try: await cl.disconnect()
            except Exception: pass
            _ad_exec("UPDATE ad_accounts SET last_error=? WHERE id=?", (f"Подключение: {str(e)[:200]}", acc_id))
            ad_log_add("account", f"Не удалось подключиться: {str(e)[:200]}", "error", acc=acc_id)
            return None
        _ad_clients[acc_id] = cl
        return cl

async def ad_drop_client(acc_id: int) -> None:
    cl = _ad_clients.pop(acc_id, None)
    if cl:
        try: await cl.disconnect()
        except Exception: pass

def ad_acc_label(acc: dict) -> str:
    if acc.get("username"): return "@" + acc["username"]
    return acc.get("name") or acc.get("phone") or f"№{acc['id']}"

async def ad_finish_auth(msg: "Message", st: dict) -> None:
    cl = st["client"]
    me = await cl.get_me()
    session = cl.session.save()
    name = " ".join(x for x in (me.first_name, me.last_name) if x) or ""
    old = _ad_one("SELECT id FROM ad_accounts WHERE tg_id=?", (me.id,))
    if old:
        _ad_exec("UPDATE ad_accounts SET session=?, phone=?, username=?, name=?, status='active', last_error=NULL WHERE id=?",
                 (session, st["phone"], me.username, name, old["id"]))
        acc_id = old["id"]
    else:
        acc_id = _ad_exec("INSERT INTO ad_accounts(phone,session,tg_id,username,name) VALUES(?,?,?,?,?)",
                          (st["phone"], session, me.id, me.username, name))
    await ad_drop_client(acc_id)
    _ad_clients[acc_id] = cl
    _ad_auth.pop(msg.from_user.id, None)
    ad_log_add("account", f"Аккаунт подключён: {name} @{me.username or '—'}", acc=acc_id)
    await safe_answer(msg, f"✅ Аккаунт подключён: <b>{html.escape(name)}</b> "
                           f"{'@' + me.username if me.username else ''}",
                      mkb([[("👤 К аккаунту", f"ad_acc_v:{acc_id}")], [("◀️ Аккаунты", "ad_acc")]]))


# ── Доступ аккаунта к чату ─────────────────────────────────────
class _AdNeedJoin(Exception):
    pass

def _ad_peer_to_json(peer) -> Optional[str]:
    if hasattr(peer, "channel_id"):
        return json.dumps({"k": "ch", "id": peer.channel_id, "h": peer.access_hash})
    if hasattr(peer, "chat_id"):
        return json.dumps({"k": "chat", "id": peer.chat_id})
    return None

def _ad_peer_from_json(raw: str):
    from telethon.tl.types import InputPeerChannel, InputPeerChat
    d = json.loads(raw)
    if d.get("k") == "ch": return InputPeerChannel(d["id"], d["h"])
    if d.get("k") == "chat": return InputPeerChat(d["id"])
    return None

def _ad_access(chat_id: int, acc_id: int) -> Optional[dict]:
    return _ad_one("SELECT * FROM ad_chat_access WHERE chat_id=? AND account_id=?", (chat_id, acc_id))

def _ad_save_access(chat_id: int, acc_id: int, peer_json: Optional[str], can_write: Optional[int],
                    slowmode: int, note: str) -> None:
    old = _ad_access(chat_id, acc_id)
    if old:
        if peer_json is None: peer_json = old.get("peer")
        if can_write is None: can_write = old.get("can_write")
    _ad_exec("INSERT OR REPLACE INTO ad_chat_access(chat_id,account_id,peer,can_write,slowmode,note,checked_at) "
             "VALUES(?,?,?,?,?,?,?)", (chat_id, acc_id, peer_json, can_write, slowmode, note[:300], _ad_ts(_ad_now())))

async def _ad_resolve_peer(client, chat: dict, acc_id: int, join: bool = False):
    """InputPeer чата для этого аккаунта. Сохраняется в ad_chat_access (access_hash), чтобы
    после перезапуска не резолвить заново — резолв по username имеет жёсткие лимиты Telegram."""
    acc_row = _ad_access(chat["id"], acc_id)
    if acc_row and acc_row.get("peer"):
        p = _ad_peer_from_json(acc_row["peer"])
        if p is not None: return p
    parsed = ad_parse_link(chat["link"])
    if not parsed: raise ValueError("некорректная ссылка на чат")
    kind, val = parsed
    ent = None
    if kind == "user":
        ent = await client.get_entity(val)
    else:
        info = await client(CheckChatInviteRequest(val))
        if isinstance(info, ChatInviteAlready):
            ent = info.chat
        elif join:
            upd = await client(ImportChatInviteRequest(val))
            ent = upd.chats[0]
        else:
            raise _AdNeedJoin()
    from telethon import utils as tl_utils
    peer = tl_utils.get_input_peer(ent)
    pj = _ad_peer_to_json(peer)
    if pj:
        _ad_save_access(chat["id"], acc_id, pj, None, 0, "peer сохранён")
    if getattr(ent, "title", None) and not chat.get("title"):
        _ad_exec("UPDATE ad_chats SET title=? WHERE id=?", (ent.title, chat["id"]))
        chat["title"] = ent.title
    return peer

async def ad_check_chat(chat_id: int, acc_id: int, join: bool = False) -> dict:
    """Проверка: аккаунт состоит в чате и может ли писать. Возвращает {ok, member, can_write, slowmode, note}."""
    chat = _ad_one("SELECT * FROM ad_chats WHERE id=?", (chat_id,))
    res = {"ok": False, "member": False, "can_write": False, "slowmode": 0, "note": ""}
    if not chat: res["note"] = "чат не найден"; return res
    client = await ad_get_client(acc_id)
    if client is None:
        res["note"] = "аккаунт не подключён"; return res
    try:
        try:
            peer = await _ad_resolve_peer(client, chat, acc_id, join=join)
        except _AdNeedJoin:
            res["note"] = "приватная ссылка: аккаунт ещё не вступил в чат"
            _ad_save_access(chat_id, acc_id, None, 0, 0, res["note"])
            return res
        ent = await client.get_entity(peer)
        if getattr(ent, "title", None):
            _ad_exec("UPDATE ad_chats SET title=? WHERE id=?", (ent.title, chat_id))
        member = not getattr(ent, "left", False)
        if not member and join:
            from telethon.tl.functions.channels import JoinChannelRequest
            await client(JoinChannelRequest(ent))
            member = True
            ent = await client.get_entity(peer)
        res["member"] = member
        admin = bool(getattr(ent, "admin_rights", None) or getattr(ent, "creator", False))
        drb = getattr(ent, "default_banned_rights", None)
        br = getattr(ent, "banned_rights", None)
        muted = bool(drb and getattr(drb, "send_messages", False))
        banned = bool(br and getattr(br, "send_messages", False))
        broadcast = bool(getattr(ent, "broadcast", False))
        slow = 0
        try:
            from telethon.tl.functions.channels import GetFullChannelRequest
            full = await client(GetFullChannelRequest(ent))
            slow = int(getattr(full.full_chat, "slowmode_seconds", 0) or 0)
        except Exception:
            pass
        res["slowmode"] = slow
        if not member:
            res["note"] = "аккаунт не состоит в чате (нужно вступить)"
        elif broadcast and not admin:
            res["note"] = "это канал: публиковать могут только админы"
        elif banned:
            res["note"] = "аккаунту запрещено писать в этом чате"
        elif muted and not admin:
            res["note"] = "в чате запрещено писать обычным участникам"
        else:
            res["can_write"] = True
            res["note"] = "можно публиковать" + (f" (медленный режим {slow} с)" if slow else "")
        res["ok"] = res["can_write"]
        _ad_save_access(chat_id, acc_id, None, 1 if res["can_write"] else 0, slow, res["note"])
    except FloodWaitError as e:
        res["note"] = f"FloodWait {e.seconds} с — попробуйте позже"
    except Exception as e:
        res["note"] = f"{type(e).__name__}: {str(e)[:160]}"
        _ad_save_access(chat_id, acc_id, None, 0, 0, res["note"])
    ad_log_add("chat", f"Проверка доступа: {res['note']}", "info" if res["ok"] else "warn", acc=acc_id, chat=chat_id)
    return res


# ── Выбор текста (случайно, без повторов) ──────────────────────
def ad_pick_template(camp_id: int, chat_id: int, chat_cfg: dict, camp_cfg: dict) -> tuple[Optional[dict], int, int]:
    """(шаблон | None, всего включённых, доступных сейчас)."""
    tpls = _ad_all("SELECT * FROM ad_templates WHERE campaign_id=? AND enabled=1", (camp_id,))
    days = ad_val("no_repeat_days", chat_cfg, camp_cfg)
    used: set[int] = set()
    if days > 0:
        since = _ad_ts(_ad_now() - timedelta(days=days))
        used = {r["template_id"] for r in _ad_all(
            "SELECT DISTINCT template_id FROM ad_posts WHERE chat_id=? AND posted_at>=?", (chat_id, since))}
    avail = [t for t in tpls if t["id"] not in used]
    return (random.choice(avail) if avail else None), len(tpls), len(avail)


# ── Лимиты аккаунта ────────────────────────────────────────────
def ad_account_block_reason(acc: dict, now: datetime, force: bool = False) -> Optional[str]:
    if acc["status"] != "active": return f"статус «{acc['status']}»"
    if acc.get("cool_until") and acc["cool_until"] > _ad_ts(now):
        return f"ждёт до {fmt_msk(acc['cool_until'], '%H:%M')} МСК (ограничение Telegram)"
    cfg = _ad_cfg(acc)
    if not force and not _ad_in_hours(_ad_msk().hour, int(ad_val("hour_from", cfg)), int(ad_val("hour_to", cfg))):
        return "вне окна часов аккаунта"
    per_day = int(ad_val("acc_per_day", cfg))
    if per_day > 0:
        n = _ad_one("SELECT COUNT(*) c FROM ad_posts WHERE account_id=? AND posted_at>=?",
                    (acc["id"], _ad_ts(now - timedelta(hours=24))))["c"]
        if n >= per_day: return f"лимит {per_day} постов за 24 ч"
    per_hour = int(ad_val("acc_per_hour", cfg))
    if per_hour > 0:
        n = _ad_one("SELECT COUNT(*) c FROM ad_posts WHERE account_id=? AND posted_at>=?",
                    (acc["id"], _ad_ts(now - timedelta(hours=1))))["c"]
        if n >= per_hour: return f"лимит {per_hour} постов за час"
    gap = ad_val("acc_gap_min", cfg)
    if gap > 0 and not force:
        last = _ad_one("SELECT MAX(posted_at) m FROM ad_posts WHERE account_id=?", (acc["id"],))["m"]
        if last and last > _ad_ts(now - timedelta(minutes=gap)): return "пауза между постами аккаунта"
    return None


# ── Публикация ─────────────────────────────────────────────────
_AD_ACCOUNT_DEAD = {"AuthKeyUnregisteredError", "UserDeactivatedError", "UserDeactivatedBanError",
                    "SessionRevokedError", "SessionExpiredError", "AuthKeyDuplicatedError"}
_AD_CHAT_BLOCKED = {"ChatWriteForbiddenError", "UserBannedInChannelError", "ChatAdminRequiredError",
                    "ChannelPrivateError", "ChannelInvalidError", "ChatIdInvalidError",
                    "UserNotParticipantError", "ChatRestrictedError", "ChatSendPlainForbiddenError",
                    "PeerIdInvalidError", "InputUserDeactivatedError"}

async def ad_publish_one(bot, camp: dict, chat: dict, acc: dict, tpl: dict) -> tuple[bool, str]:
    cfg_c, cfg_k = _ad_cfg(chat), _ad_cfg(camp)
    client = await ad_get_client(acc["id"])
    if client is None:
        return False, "аккаунт не подключён"
    try:
        peer = await _ad_resolve_peer(client, chat, acc["id"])
        text = ad_render(tpl["text"], camp, chat, acc, tpl)
        sent = await client.send_message(
            peer, text, parse_mode="html",
            link_preview=bool(ad_val("link_preview", cfg_c, cfg_k)),
            silent=bool(ad_val("silent", cfg_c, cfg_k)))
    except FloodWaitError as e:
        wait = int(e.seconds) + int(_ad_global("flood_extra_s"))
        _ad_exec("UPDATE ad_accounts SET cool_until=? WHERE id=?",
                 (_ad_ts(_ad_now() + timedelta(seconds=wait)), acc["id"]))
        ad_log_add("error", f"FloodWait {e.seconds} с — аккаунт на паузе", "warn", acc=acc["id"], chat=chat["id"], camp=camp["id"])
        if wait > 600:
            await _ad_notify(bot, f"⚠️ Реклама: {html.escape(ad_acc_label(acc))} получил FloodWait на "
                                  f"{wait // 60} мин. Аккаунт временно не публикует.", f"flood_{acc['id']}", 120)
        return False, f"FloodWait {e.seconds} с"
    except _AdNeedJoin:
        _ad_save_access(chat["id"], acc["id"], None, 0, 0, "приватная ссылка: нужно вступить")
        ad_log_add("error", "Аккаунт не состоит в приватном чате — нажмите «Проверить доступ» → «Вступить»",
                   "error", acc=acc["id"], chat=chat["id"], camp=camp["id"])
        return False, "нужно вступить в чат"
    except Exception as e:
        name = type(e).__name__
        if name == "SlowModeWaitError":
            sec = int(getattr(e, "seconds", 60)) + 10
            _ad_exec("UPDATE ad_chats SET next_post_at=? WHERE id=?",
                     (_ad_ts(_ad_now() + timedelta(seconds=sec)), chat["id"]))
            ad_log_add("error", f"Медленный режим чата: ждать {sec} с", "warn", acc=acc["id"], chat=chat["id"], camp=camp["id"])
            return False, "медленный режим"
        if name in _AD_ACCOUNT_DEAD:
            _ad_exec("UPDATE ad_accounts SET status='error', last_error=? WHERE id=?", (f"{name}: сессия/аккаунт недоступны", acc["id"]))
            await ad_drop_client(acc["id"])
            ad_log_add("account", f"Аккаунт отключён: {name}", "error", acc=acc["id"])
            await _ad_notify(bot, f"🔴 Реклама: аккаунт {html.escape(ad_acc_label(acc))} отключён ({name}). "
                                  f"Добавьте его заново в 📣 Реклама → Аккаунты.", f"dead_{acc['id']}", 360)
            return False, name
        if name == "PeerFloodError":
            _ad_exec("UPDATE ad_accounts SET cool_until=?, last_error=? WHERE id=?",
                     (_ad_ts(_ad_now() + timedelta(hours=6)), "PeerFlood — Telegram ограничил аккаунт", acc["id"]))
            ad_log_add("error", "PeerFlood: аккаунт на паузе 6 ч", "error", acc=acc["id"], chat=chat["id"], camp=camp["id"])
            await _ad_notify(bot, f"⚠️ Реклама: {html.escape(ad_acc_label(acc))} получил PeerFlood "
                                  f"(ограничение спама). Пауза 6 ч.", f"pf_{acc['id']}", 360)
            return False, name
        if name in _AD_CHAT_BLOCKED:
            _ad_save_access(chat["id"], acc["id"], None, 0, 0, f"{name}")
            ad_log_add("error", f"Нет прав публиковать в чате ({name}). Пара аккаунт–чат отключена до проверки доступа",
                       "error", acc=acc["id"], chat=chat["id"], camp=camp["id"])
            return False, name
        _ad_exec("UPDATE ad_chats SET next_post_at=? WHERE id=?",
                 (_ad_ts(_ad_now() + timedelta(minutes=10)), chat["id"]))
        ad_log_add("error", f"Ошибка публикации: {name}: {str(e)[:200]}", "error", acc=acc["id"], chat=chat["id"], camp=camp["id"])
        return False, f"{name}"

    now = _ad_now()
    dh = ad_val("delete_after_h", cfg_c, cfg_k)
    delete_at = _ad_ts(now + timedelta(hours=dh)) if dh > 0 else None
    _ad_exec("INSERT INTO ad_posts(campaign_id,template_id,account_id,chat_id,msg_id,posted_at,delete_at) VALUES(?,?,?,?,?,?,?)",
             (camp["id"], tpl["id"], acc["id"], chat["id"], sent.id, _ad_ts(now), delete_at))
    interval = ad_val("chat_interval_min", cfg_c, cfg_k)
    jitter = ad_val("jitter_min", cfg_c, cfg_k)
    wait_min = interval + (random.uniform(0, jitter) if jitter > 0 else 0)
    nxt = _ad_ts(now + timedelta(minutes=wait_min)) if wait_min > 0 else None
    _ad_exec("UPDATE ad_chats SET next_post_at=? WHERE id=?", (nxt, chat["id"]))
    ad_log_add("post", f"Опубликовано: «{ad_plain(tpl['text'], 50)}» ({ad_acc_label(acc)} → {chat.get('title') or chat['link']})"
                       + (f", удалится {fmt_msk(delete_at, '%d.%m %H:%M')}" if delete_at else ""),
               acc=acc["id"], chat=chat["id"], camp=camp["id"])
    return True, "ok"


def ad_campaign_open(camp: dict, now_msk: Optional[datetime] = None) -> bool:
    now_msk = now_msk or _ad_msk()
    days = (camp.get("days") or "1111111").ljust(7, "1")
    if days[now_msk.weekday()] != "1": return False
    cfg = _ad_cfg(camp)
    return _ad_in_hours(now_msk.hour, int(ad_val("hour_from", cfg)), int(ad_val("hour_to", cfg)))

async def ad_try_chat(bot, camp: dict, chat: dict, force: bool = False) -> str:
    """Одна попытка публикации кампании в чат. Возвращает код результата (для журнала/кнопки)."""
    now = _ad_now()
    cfg_c, cfg_k = _ad_cfg(chat), _ad_cfg(camp)
    if not force:
        if chat.get("next_post_at") and chat["next_post_at"] > _ad_ts(now): return "wait"
        per_day = int(ad_val("chat_per_day", cfg_c, cfg_k))
        if per_day > 0:
            n = _ad_one("SELECT COUNT(*) c FROM ad_posts WHERE chat_id=? AND posted_at>=?",
                        (chat["id"], _ad_ts(now - timedelta(hours=24))))["c"]
            if n >= per_day: return "chat_limit"
        interval = ad_val("chat_interval_min", cfg_c, cfg_k)
        if interval > 0:
            last = _ad_one("SELECT MAX(posted_at) m FROM ad_posts WHERE chat_id=?", (chat["id"],))["m"]
            if last and last > _ad_ts(now - timedelta(minutes=interval)): return "wait"
    accs = _ad_all("SELECT a.* FROM ad_accounts a JOIN ad_campaign_accounts ca ON ca.account_id=a.id "
                   "WHERE ca.campaign_id=?", (camp["id"],))
    ready = []
    for a in accs:
        acc_row = _ad_access(chat["id"], a["id"])
        if acc_row and acc_row.get("can_write") == 0: continue
        if ad_account_block_reason(a, now, force) is None:
            n = _ad_one("SELECT COUNT(*) c FROM ad_posts WHERE account_id=? AND posted_at>=?",
                        (a["id"], _ad_ts(now - timedelta(hours=24))))["c"]
            ready.append((n, random.random(), a))
    if not ready: return "no_account"
    ready.sort(key=lambda x: (x[0], x[1]))
    acc = ready[0][2]
    tpl, total, avail = ad_pick_template(camp["id"], chat["id"], cfg_c, cfg_k)
    if tpl is None:
        reason = "в кампании нет включённых текстов" if total == 0 else \
                 f"все {total} текст(ов) уже были в этом чате за последние {_ad_fmt(ad_val('no_repeat_days', cfg_c, cfg_k))} дн. — добавьте новые"
        key = f"notext_{camp['id']}_{chat['id']}"
        last = _db.get_setting("ad_" + key, "")
        if not last or last < _ad_ts(now - timedelta(hours=12)):
            _db.set_setting("ad_" + key, _ad_ts(now))
            ad_log_add("error", f"Нечего публиковать в «{chat.get('title') or chat['link']}»: {reason}",
                       "warn", chat=chat["id"], camp=camp["id"])
            await _ad_notify(bot, f"⚠️ Реклама, кампания «{html.escape(camp['name'])}»: {html.escape(reason)}.",
                             key, 720)
        return "no_text"
    ok, note = await ad_publish_one(bot, camp, chat, acc, tpl)
    return "posted" if ok else f"fail:{note}"

async def ad_run_cycle(bot) -> None:
    camps = _ad_all("SELECT * FROM ad_campaigns WHERE enabled=1")
    for camp in camps:
        if not ad_campaign_open(camp): continue
        chats = _ad_all("SELECT c.* FROM ad_chats c JOIN ad_campaign_chats cc ON cc.chat_id=c.id "
                        "WHERE cc.campaign_id=? AND c.enabled=1", (camp["id"],))
        random.shuffle(chats)
        for chat in chats:
            try: await ad_try_chat(bot, camp, chat)
            except Exception as e:
                ad_log_add("error", f"Сбой цикла: {type(e).__name__}: {str(e)[:200]}", "error", chat=chat["id"], camp=camp["id"])
            await asyncio.sleep(1)


# ── Автоудаление ───────────────────────────────────────────────
async def ad_delete_post(p: dict, bot=None) -> bool:
    acc = _ad_one("SELECT * FROM ad_accounts WHERE id=?", (p["account_id"],))
    chat = _ad_one("SELECT * FROM ad_chats WHERE id=?", (p["chat_id"],))
    max_try = int(_ad_global("del_retry_max"))
    def _fail(reason: str, retry_min: int = 10) -> bool:
        n = p["del_attempts"] + 1
        if n >= max_try:
            _ad_exec("UPDATE ad_posts SET status='delete_failed', del_attempts=? WHERE id=?", (n, p["id"]))
            ad_log_add("error", f"Не удалось удалить пост №{p['id']} после {n} попыток: {reason}", "error",
                       acc=p["account_id"], chat=p["chat_id"], camp=p["campaign_id"])
        else:
            _ad_exec("UPDATE ad_posts SET del_attempts=?, delete_at=? WHERE id=?",
                     (n, _ad_ts(_ad_now() + timedelta(minutes=retry_min * n)), p["id"]))
        return False
    if not acc or not chat: return _fail("аккаунт или чат удалены из системы", 1)
    if acc["status"] == "paused": return False          # ждём, пока аккаунт снимут с паузы
    client = await ad_get_client(acc["id"])
    if client is None: return _fail("аккаунт не подключён")
    try:
        peer = await _ad_resolve_peer(client, chat, acc["id"])
        await client.delete_messages(peer, [p["msg_id"]])
    except FloodWaitError as e:
        _ad_exec("UPDATE ad_posts SET delete_at=? WHERE id=?",
                 (_ad_ts(_ad_now() + timedelta(seconds=int(e.seconds) + 30)), p["id"]))
        return False
    except Exception as e:
        name = type(e).__name__
        if name in ("MessageIdInvalidError",):
            pass                                          # уже удалено — считаем успехом
        else:
            return _fail(f"{name}")
    _ad_exec("UPDATE ad_posts SET status='deleted', deleted_at=? WHERE id=?", (_ad_ts(_ad_now()), p["id"]))
    ad_log_add("delete", f"Удалён пост №{p['id']} в «{chat.get('title') or chat['link']}»",
               acc=p["account_id"], chat=p["chat_id"], camp=p["campaign_id"])
    return True

async def ad_delete_due(bot=None, limit: int = 40) -> int:
    rows = _ad_all("SELECT * FROM ad_posts WHERE status='posted' AND delete_at IS NOT NULL AND delete_at<=? "
                   "ORDER BY delete_at LIMIT ?", (_ad_ts(_ad_now()), limit))
    done = 0
    for p in rows:
        if await ad_delete_post(p, bot): done += 1
        await asyncio.sleep(1.5)
    return done


# ── Фоновые циклы (не должны ронять основной процесс) ──────────
async def ad_scheduler_loop(bot) -> None:
    await asyncio.sleep(15)
    try:
        _ad_state["bot_username"] = (await bot.get_me()).username or ""
    except Exception as e:
        log.warning(f"ad: bot.get_me: {e}")
    while True:
        tick = 30
        try:
            if not _ad_state["bot_username"]:
                try: _ad_state["bot_username"] = (await bot.get_me()).username or ""
                except Exception: pass
            if _db.get_setting("ad_enabled", "0") == "1":
                await ad_run_cycle(bot)
            tick = max(5, int(_ad_global("tick_s")))
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.error(f"ad_scheduler_loop: {e}", exc_info=True)
            try: ad_log_add("error", f"Сбой планировщика: {type(e).__name__}: {str(e)[:200]}", "error")
            except Exception: pass
        await asyncio.sleep(tick)

async def ad_cleanup_loop(bot) -> None:
    await asyncio.sleep(25)
    while True:
        try:
            await ad_delete_due(bot)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.error(f"ad_cleanup_loop: {e}", exc_info=True)
        await asyncio.sleep(60)


# ── Метки в /start ─────────────────────────────────────────────
def _ad_track_start(msg: "Message", cl: dict, is_new: bool) -> None:
    args = (msg.text or "").split()
    if len(args) < 2 or not args[1].startswith("ad_"): return
    m = re.fullmatch(r"ad_(\d+)_(\d+)_(\d+)_(\d+)", args[1])
    if not m: return
    camp, chat, acc, tpl = (int(x) or None for x in m.groups())
    _ad_exec("INSERT INTO ad_starts(tg_id,campaign_id,chat_id,account_id,template_id,is_new) VALUES(?,?,?,?,?,?)",
             (msg.from_user.id, camp, chat, acc, tpl, 1 if is_new else 0))
    if is_new:
        _ad_exec("UPDATE clients SET ad_source=? WHERE id=?", (args[1], cl["id"]))
    ad_log_add("start", f"/start по рекламной ссылке ({'новый' if is_new else 'уже был'} пользователь)",
               acc=acc, chat=chat, camp=camp)


# ── Админ-интерфейс раздела «Реклама» ──────────────────────────
_AD_ROUTES: dict = {}
AD_PAGE = 10

def _ad_route(name: str):
    def deco(fn):
        _AD_ROUTES[name] = fn
        return fn
    return deco

@admin_router.callback_query(F.data.startswith("ad_"))
async def ad_dispatch(call: CallbackQuery):
    parts = (call.data or "").split(":")
    fn = _AD_ROUTES.get(parts[0])
    toast = None
    if fn is None:
        toast = "Неизвестная кнопка"
    else:
        try:
            toast = await fn(call, parts[1:])
        except Exception as e:
            log.error(f"ad UI {call.data}: {e}", exc_info=True)
            ad_log_add("error", f"Ошибка интерфейса ({call.data}): {type(e).__name__}: {str(e)[:150]}", "error")
            toast = f"Ошибка: {type(e).__name__}"
    try: await call.answer((toast or "")[:190])
    except Exception: pass

async def _ad_show(call: CallbackQuery, text: str, rows: list) -> None:
    if len(text) > 3900: text = text[:3900] + "…"
    await safe_edit(call, text, mkb(rows))

def _ad_icon_acc(a: dict) -> str:
    if a["status"] == "active":
        if a.get("cool_until") and a["cool_until"] > _ad_ts(_ad_now()): return "🟡"
        return "🟢"
    return "⏸" if a["status"] == "paused" else "🔴"

def _ad_page(items: list, page: int, size: int = AD_PAGE) -> tuple[list, int]:
    pages = max(1, (len(items) + size - 1) // size)
    page = max(0, min(page, pages - 1))
    return items[page * size:(page + 1) * size], pages

def _ad_nav(prefix_fmt: str, page: int, pages: int) -> list:
    nav = []
    if page > 0: nav.append(("◀️", prefix_fmt.format(p=page - 1)))
    if pages > 1: nav.append((f"{page + 1}/{pages}", "ad_noop"))
    if page < pages - 1: nav.append(("▶️", prefix_fmt.format(p=page + 1)))
    return nav

@_ad_route("ad_noop")
async def _ad_noop(call, a): return None


# ── Главное меню ───────────────────────────────────────────────
def _ad_menu_text() -> str:
    on = _db.get_setting("ad_enabled", "0") == "1"
    accs = _ad_all("SELECT status FROM ad_accounts")
    act = sum(1 for x in accs if x["status"] == "active")
    chats = _ad_one("SELECT COUNT(*) c FROM ad_chats WHERE enabled=1")["c"]
    camps = _ad_one("SELECT COUNT(*) c, COALESCE(SUM(enabled),0) e FROM ad_campaigns")
    since = _ad_ts(_ad_now() - timedelta(hours=24))
    p24 = _ad_one("SELECT COUNT(*) c FROM ad_posts WHERE posted_at>=?", (since,))["c"]
    live = _ad_one("SELECT COUNT(*) c FROM ad_posts WHERE status='posted'")["c"]
    st24 = _ad_one("SELECT COUNT(*) c, COALESCE(SUM(is_new),0) n FROM ad_starts WHERE ts>=?", (since,))
    return (
        "📣 <b>Реклама</b>\n\n"
        f"Публикации: {'🟢 <b>включены</b>' if on else '🔴 <b>выключены</b>'}\n"
        f"👤 Аккаунтов: <b>{len(accs)}</b> (активных {act})\n"
        f"💬 Чатов: <b>{chats}</b>\n"
        f"📣 Кампаний: <b>{camps['c']}</b> (включено {camps['e']})\n\n"
        f"За 24 ч: постов <b>{p24}</b> · стартов бота <b>{st24['c']}</b> (новых {st24['n']})\n"
        f"Сейчас висит постов: <b>{live}</b>"
    )

def _ad_menu_rows() -> list:
    on = _db.get_setting("ad_enabled", "0") == "1"
    return [
        [("⏸ Выключить публикации" if on else "▶️ Включить публикации", "ad_toggle")],
        [("👤 Аккаунты", "ad_acc"), ("💬 Чаты", "ad_chats")],
        [("📣 Кампании", "ad_camps"), ("📊 Статистика", "ad_stats:camp:7")],
        [("📋 Посты", "ad_posts:all:0:0"), ("📜 Журнал", "ad_log:all:0")],
        [("⚙️ Общие лимиты", "ad_cfg:g:0"), ("🔤 Переменные", "ad_vars")],
        [("◀️ Клиенты", "admin_clients")],
    ]

@_ad_route("ad_menu")
async def _ad_menu(call, a):
    await _ad_show(call, _ad_menu_text(), _ad_menu_rows())

@_ad_route("ad_toggle")
async def _ad_toggle(call, a):
    on = _db.get_setting("ad_enabled", "0") == "1"
    _db.set_setting("ad_enabled", "0" if on else "1")
    ad_log_add("setting", "Публикации " + ("выключены" if on else "включены"))
    await _ad_show(call, _ad_menu_text(), _ad_menu_rows())
    return "Публикации " + ("выключены" if on else "включены")


# ── Аккаунты ───────────────────────────────────────────────────
@_ad_route("ad_acc")
async def _ad_acc_list(call, a):
    accs = _ad_all("SELECT * FROM ad_accounts ORDER BY id")
    text = "👤 <b>Рекламные аккаунты</b>\n\n" + ("Пока нет аккаунтов. Добавьте первый." if not accs else
           "🟢 работает · 🟡 ждёт (ограничение Telegram) · ⏸ пауза · 🔴 ошибка")
    rows = [[(f"{_ad_icon_acc(x)} {ad_acc_label(x)}", f"ad_acc_v:{x['id']}")] for x in accs]
    rows.append([("➕ Добавить по номеру", "ad_acc_add"), ("📥 Добавить по сессии", "ad_acc_sess")])
    rows.append([("◀️ Реклама", "ad_menu")])
    await _ad_show(call, text, rows)

@_ad_route("ad_acc_add")
async def _ad_acc_add(call, a):
    if not API_ID or not API_HASH:
        return "Не заданы API_ID / API_HASH в окружении"
    _admin_pending[call.from_user.id] = "ad:acc_phone"
    await _ad_show(call,
        "➕ <b>Новый рекламный аккаунт</b>\n\nВведите номер телефона в формате <code>+79991234567</code>.\n\n"
        "⚠️ Не используйте аккаунт, на котором работает мониторинг вакансий.\n"
        "Код из Telegram вводите <b>с разделителями</b> (например <code>1-2-3-4-5</code>) — "
        "иначе Telegram может счесть код «слитым» и отменить вход.\n"
        "Сессия сохраняется в базе; пароль 2FA и коды не сохраняются.",
        [[("◀️ Отмена", "ad_acc")]])

_AD_CODE_TYPES = {
    "SentCodeTypeApp": "в приложение Telegram — откройте служебный чат «Telegram» (с галочкой) на устройстве, где этот аккаунт уже авторизован",
    "SentCodeTypeSms": "по SMS на этот номер",
    "SentCodeTypeCall": "голосовым звонком на этот номер",
    "SentCodeTypeFlashCall": "звонком-сбросом (код — последние цифры входящего номера)",
    "SentCodeTypeMissedCall": "пропущенным звонком (код — последние цифры входящего номера)",
    "SentCodeTypeEmailCode": "на привязанную к аккаунту почту",
    "CodeTypeSms": "по SMS", "CodeTypeCall": "голосовым звонком",
    "CodeTypeFlashCall": "звонком-сбросом", "CodeTypeMissedCall": "пропущенным звонком",
}

def _ad_code_where(res) -> str:
    t = type(getattr(res, "type", None)).__name__
    out = "Куда отправлен код: " + _AD_CODE_TYPES.get(t, t)
    nt = getattr(res, "next_type", None)
    if nt is not None:
        out += "\nПри повторной отправке Telegram может переключить способ: " + _AD_CODE_TYPES.get(type(nt).__name__, type(nt).__name__)
    return out

def _ad_code_rows() -> list:
    return [[("📩 Код не пришёл — отправить повторно", "ad_acc_resend")], [("◀️ Отмена", "ad_acc")]]

@_ad_route("ad_acc_resend")
async def _ad_acc_resend(call, a):
    uid = call.from_user.id
    st = _ad_auth.get(uid)
    if not st: return "Вход истёк, начните заново"
    from telethon.tl.functions.auth import ResendCodeRequest
    try:
        res = await st["client"](ResendCodeRequest(st["phone"], st["hash"]))
    except FloodWaitError as e:
        return f"Telegram просит подождать {e.seconds} с"
    except Exception as e:
        await _ad_show(call, f"❌ Не удалось отправить повторно: <code>{html.escape(type(e).__name__)}</code>\n"
                             "Начните заново: Аккаунты → Добавить по номеру.", [[("◀️ Аккаунты", "ad_acc")]])
        return
    st["hash"] = res.phone_code_hash
    _admin_pending[uid] = "ad:acc_code"
    await _ad_show(call, f"📱 Код отправлен повторно на <code>{html.escape(st['phone'])}</code>.\n{_ad_code_where(res)}\n\n"
                         "Введите код <b>с разделителями</b>, например <code>1-2-3-4-5</code>:", _ad_code_rows())

@_ad_route("ad_acc_sess")
async def _ad_acc_sess(call, a):
    if not API_ID or not API_HASH:
        return "Не заданы API_ID / API_HASH в окружении"
    _admin_pending[call.from_user.id] = "ad:acc_session"
    await _ad_show(call,
        "📥 <b>Добавить аккаунт по сессии</b>\n\n"
        "Пришлите строку сессии <b>Telethon</b> (StringSession) одним сообщением. "
        "Сообщение с сессией бот удалит сразу после чтения.\n\n"
        "⚠️ Строка сессии — это полный доступ к аккаунту. Присылайте только сессии своих аккаунтов.\n"
        "⚠️ Одна сессия не должна одновременно работать в другом месте (другом боте, скрипте, на другом сервере): "
        "Telegram в таком случае отзывает её. Не присылайте сюда сессию основного аккаунта мониторинга.",
        [[("◀️ Отмена", "ad_acc")]])

def _ad_acc_text(x: dict) -> str:
    now = _ad_now()
    d = _ad_one("SELECT COUNT(*) c FROM ad_posts WHERE account_id=? AND posted_at>=?",
                (x["id"], _ad_ts(now - timedelta(hours=24))))["c"]
    tot = _ad_one("SELECT COUNT(*) c FROM ad_posts WHERE account_id=?", (x["id"],))["c"]
    cfg = _ad_cfg(x)
    lim = int(ad_val("acc_per_day", cfg))
    reason = ad_account_block_reason(x, now)
    return (
        f"{_ad_icon_acc(x)} <b>{html.escape(ad_acc_label(x))}</b>\n"
        f"Имя: {html.escape(x.get('name') or '—')}\n"
        f"Телефон: <code>{html.escape(x.get('phone') or '—')}</code>\n"
        f"Статус: <b>{x['status']}</b>\n"
        f"Постов за 24 ч: <b>{d}</b>{f' из {lim}' if lim else ''} · всего: {tot}\n"
        + (f"Сейчас не публикует: {html.escape(reason)}\n" if reason and x["status"] == "active" else "")
        + (f"⚠️ Последняя ошибка: {html.escape(x['last_error'])}\n" if x.get("last_error") else "")
    )

@_ad_route("ad_acc_v")
async def _ad_acc_view(call, a):
    x = _ad_one("SELECT * FROM ad_accounts WHERE id=?", (int(a[0]),))
    if not x: return "Аккаунт не найден"
    i = x["id"]
    rows = [
        [("▶️ Возобновить", f"ad_acc_st:{i}:active")] if x["status"] != "active" else [("⏸ Пауза", f"ad_acc_st:{i}:paused")],
        [("🔁 Проверить подключение", f"ad_acc_chk:{i}"), ("⚙️ Лимиты аккаунта", f"ad_cfg:a:{i}")],
        [("🗑 Удалить из системы", f"ad_acc_del:{i}")],
        [("◀️ Аккаунты", "ad_acc")],
    ]
    await _ad_show(call, _ad_acc_text(x), rows)

@_ad_route("ad_acc_st")
async def _ad_acc_set_status(call, a):
    i, st = int(a[0]), a[1]
    if st not in ("active", "paused"): return None
    _ad_exec("UPDATE ad_accounts SET status=?, last_error=NULL WHERE id=?", (st, i))
    if st == "paused": await ad_drop_client(i)
    ad_log_add("account", "Аккаунт " + ("на паузе" if st == "paused" else "возобновлён"), acc=i)
    return await _ad_acc_view(call, [str(i)]) or ("Пауза" if st == "paused" else "Возобновлён")

@_ad_route("ad_acc_chk")
async def _ad_acc_check(call, a):
    i = int(a[0])
    await safe_edit(call, "⏳ Проверяю подключение…", None)
    cl = await ad_get_client(i)
    if cl is None:
        x = _ad_one("SELECT last_error FROM ad_accounts WHERE id=?", (i,))
        msg = (x or {}).get("last_error") or "не удалось подключиться"
        await _ad_show(call, f"🔴 Подключение не удалось: {html.escape(msg)}",
                       [[("◀️ К аккаунту", f"ad_acc_v:{i}")]])
        return
    me = await cl.get_me()
    _ad_exec("UPDATE ad_accounts SET status=CASE WHEN status='error' THEN 'active' ELSE status END, "
             "last_error=NULL, username=?, name=? WHERE id=?",
             (me.username, " ".join(x for x in (me.first_name, me.last_name) if x), i))
    await _ad_show(call, f"🟢 Подключение работает: {html.escape(me.first_name or '')} "
                         f"{'@' + me.username if me.username else ''}", [[("◀️ К аккаунту", f"ad_acc_v:{i}")]])

@_ad_route("ad_acc_del")
async def _ad_acc_del(call, a):
    i = int(a[0])
    live = _ad_one("SELECT COUNT(*) c FROM ad_posts WHERE account_id=? AND status='posted'", (i,))["c"]
    await _ad_show(call,
        "🗑 <b>Удалить аккаунт из системы?</b>\n\nСессия будет забыта, аккаунт уберётся из кампаний. "
        "В самом Telegram сессия останется активной — завершить её можно в Настройки → Устройства."
        + (f"\n\n⚠️ Сейчас висит постов этого аккаунта: <b>{live}</b>. После удаления бот не сможет их удалить сам." if live else ""),
        [[("✅ Да, удалить", f"ad_acc_del2:{i}")], [("◀️ Отмена", f"ad_acc_v:{i}")]])

@_ad_route("ad_acc_del2")
async def _ad_acc_del2(call, a):
    i = int(a[0])
    await ad_drop_client(i)
    _ad_exec("DELETE FROM ad_campaign_accounts WHERE account_id=?", (i,))
    _ad_exec("DELETE FROM ad_chat_access WHERE account_id=?", (i,))
    _ad_exec("DELETE FROM ad_accounts WHERE id=?", (i,))
    ad_log_add("account", f"Аккаунт №{i} удалён из системы")
    await _ad_acc_list(call, [])
    return "Удалён"


# ── Чаты ───────────────────────────────────────────────────────
@_ad_route("ad_chats")
async def _ad_chats(call, a):
    page = int(a[0]) if a else 0
    chats = _ad_all("SELECT * FROM ad_chats ORDER BY id")
    chunk, pages = _ad_page(chats, page)
    text = "💬 <b>Рекламные чаты</b>\n\n" + ("Чатов пока нет. Добавьте ссылки на чаты, где разрешена реклама." if not chats else
            f"Всего: {len(chats)}. Включённые ✅, отключённые ⛔.")
    rows = [[(f"{'✅' if c['enabled'] else '⛔'} {c.get('title') or c['link']}"[:60], f"ad_chat_v:{c['id']}")] for c in chunk]
    nav = _ad_nav("ad_chats:{p}", page if chats else 0, pages)
    if nav: rows.append(nav)
    rows.append([("➕ Добавить чаты", "ad_chat_add:0")])
    rows.append([("◀️ Реклама", "ad_menu")])
    await _ad_show(call, text, rows)

@_ad_route("ad_chat_add")
async def _ad_chat_add(call, a):
    camp = int(a[0]) if a else 0
    _admin_pending[call.from_user.id] = f"ad:chat_add:{camp}"
    back = f"ad_kc:{camp}:0:0" if camp else "ad_chats"
    await _ad_show(call,
        "➕ <b>Добавить чаты</b>\n\nПришлите ссылки или @username, по одной на строку:\n"
        "<code>https://t.me/chatname\n@another_chat\nhttps://t.me/+AbCdEf123</code>\n\n"
        "Добавляйте только чаты, где реклама разрешена правилами.",
        [[("◀️ Отмена", back)]])

def _ad_chat_text(c: dict) -> str:
    cfg = _ad_cfg(c)
    now = _ad_now()
    n24 = _ad_one("SELECT COUNT(*) c FROM ad_posts WHERE chat_id=? AND posted_at>=?", (c["id"], _ad_ts(now - timedelta(hours=24))))["c"]
    tot = _ad_one("SELECT COUNT(*) c FROM ad_posts WHERE chat_id=?", (c["id"],))["c"]
    camps = _ad_all("SELECT k.name FROM ad_campaigns k JOIN ad_campaign_chats cc ON cc.campaign_id=k.id WHERE cc.chat_id=?", (c["id"],))
    acc_lines = []
    for r in _ad_all("SELECT x.*, a.username, a.name, a.phone FROM ad_chat_access x JOIN ad_accounts a ON a.id=x.account_id WHERE x.chat_id=? LIMIT 8", (c["id"],)):
        ic = "✅" if r["can_write"] == 1 else ("⛔" if r["can_write"] == 0 else "❔")
        acc_lines.append(f"{ic} {html.escape(ad_acc_label(r))}: {html.escape(r.get('note') or '—')}")
    own = ", ".join(f"{AD_FIELDS[k][0]} = {_ad_fmt(v)}" for k, v in cfg.items() if k in AD_FIELDS) or "общие"
    return (
        f"{'✅' if c['enabled'] else '⛔'} <b>{html.escape(c.get('title') or c['link'])}</b>\n"
        f"{html.escape(c['link'])}\n\n"
        f"Кампании: {html.escape(', '.join(k['name'] for k in camps)) or '—'}\n"
        f"Постов за 24 ч: <b>{n24}</b> · всего: {tot}\n"
        + (f"Следующий пост не раньше: {fmt_msk(c['next_post_at'], '%d.%m %H:%M')} МСК\n" if c.get("next_post_at") and c["next_post_at"] > _ad_ts(now) else "")
        + f"Свои настройки: {html.escape(own)}\n"
        + ("\nДоступ аккаунтов:\n" + "\n".join(acc_lines) if acc_lines else "\nДоступ не проверялся.")
    )

@_ad_route("ad_chat_v")
async def _ad_chat_view(call, a):
    c = _ad_one("SELECT * FROM ad_chats WHERE id=?", (int(a[0]),))
    if not c: return "Чат не найден"
    i = c["id"]
    rows = [
        [("⛔ Отключить" if c["enabled"] else "✅ Включить", f"ad_chat_tgl:{i}")],
        [("🔎 Проверить доступ", f"ad_chat_pick:{i}"), ("⚙️ Настройки чата", f"ad_cfg:c:{i}")],
        [("📋 Посты в чате", f"ad_posts:chat:{i}:0"), ("🗑 Удалить чат", f"ad_chat_del:{i}")],
        [("◀️ Чаты", "ad_chats")],
    ]
    await _ad_show(call, _ad_chat_text(c), rows)

@_ad_route("ad_chat_tgl")
async def _ad_chat_toggle(call, a):
    i = int(a[0])
    _ad_exec("UPDATE ad_chats SET enabled=1-enabled WHERE id=?", (i,))
    ad_log_add("chat", "Чат включён/отключён", chat=i)
    await _ad_chat_view(call, [str(i)])

@_ad_route("ad_chat_pick")
async def _ad_chat_pick(call, a):
    i = int(a[0])
    accs = _ad_all("SELECT * FROM ad_accounts WHERE status!='error' ORDER BY id")
    if not accs: return "Сначала добавьте аккаунт"
    rows = [[(f"{_ad_icon_acc(x)} {ad_acc_label(x)}", f"ad_chat_chk:{i}:{x['id']}:0")] for x in accs]
    rows.append([("◀️ Назад", f"ad_chat_v:{i}")])
    await _ad_show(call, "🔎 <b>Проверить доступ</b>\n\nС какого аккаунта проверить чат?", rows)

@_ad_route("ad_chat_chk")
async def _ad_chat_check(call, a):
    i, acc_id, join = int(a[0]), int(a[1]), a[2] == "1"
    await safe_edit(call, "⏳ " + ("Вступаю в чат и проверяю…" if join else "Проверяю доступ…"), None)
    res = await ad_check_chat(i, acc_id, join=join)
    rows = []
    if not res["member"] and not join and res["note"] and "FloodWait" not in res["note"]:
        rows.append([("➕ Вступить этим аккаунтом", f"ad_chat_chk:{i}:{acc_id}:1")])
    rows.append([("◀️ К чату", f"ad_chat_v:{i}")])
    await _ad_show(call, ("✅ " if res["ok"] else "⚠️ ") + html.escape(res["note"]), rows)

@_ad_route("ad_chat_del")
async def _ad_chat_del(call, a):
    i = int(a[0])
    await _ad_show(call, "🗑 <b>Удалить чат из системы?</b>\nИстория постов останется.",
                   [[("✅ Да", f"ad_chat_del2:{i}")], [("◀️ Отмена", f"ad_chat_v:{i}")]])

@_ad_route("ad_chat_del2")
async def _ad_chat_del2(call, a):
    i = int(a[0])
    _ad_exec("DELETE FROM ad_campaign_chats WHERE chat_id=?", (i,))
    _ad_exec("DELETE FROM ad_chat_access WHERE chat_id=?", (i,))
    _ad_exec("DELETE FROM ad_chats WHERE id=?", (i,))
    ad_log_add("chat", f"Чат №{i} удалён")
    await _ad_chats(call, [])


# ── Кампании ───────────────────────────────────────────────────
@_ad_route("ad_camps")
async def _ad_camps(call, a):
    camps = _ad_all("SELECT * FROM ad_campaigns ORDER BY id")
    text = "📣 <b>Кампании</b>\n\nКампания — это набор ваших текстов, чатов и аккаунтов с расписанием." + \
           ("\n\nКампаний пока нет." if not camps else "")
    rows = [[(f"{'🟢' if k['enabled'] else '⚪️'} {k['name']}"[:60], f"ad_camp_v:{k['id']}")] for k in camps]
    rows.append([("➕ Новая кампания", "ad_camp_add")])
    rows.append([("◀️ Реклама", "ad_menu")])
    await _ad_show(call, text, rows)

@_ad_route("ad_camp_add")
async def _ad_camp_add(call, a):
    _admin_pending[call.from_user.id] = "ad:camp_add"
    await _ad_show(call, "➕ <b>Новая кампания</b>\n\nВведите название (например «Монтажёры — основная»):",
                   [[("◀️ Отмена", "ad_camps")]])

def _ad_camp_text(k: dict) -> str:
    cfg = _ad_cfg(k)
    nt = _ad_one("SELECT COUNT(*) c, COALESCE(SUM(enabled),0) e FROM ad_templates WHERE campaign_id=?", (k["id"],))
    nc = _ad_one("SELECT COUNT(*) c FROM ad_campaign_chats WHERE campaign_id=?", (k["id"],))["c"]
    na = _ad_one("SELECT COUNT(*) c FROM ad_campaign_accounts WHERE campaign_id=?", (k["id"],))["c"]
    n24 = _ad_one("SELECT COUNT(*) c FROM ad_posts WHERE campaign_id=? AND posted_at>=?", (k["id"], _ad_ts(_ad_now() - timedelta(hours=24))))["c"]
    live = _ad_one("SELECT COUNT(*) c FROM ad_posts WHERE campaign_id=? AND status='posted'", (k["id"],))["c"]
    hf, ht = int(ad_val("hour_from", cfg)), int(ad_val("hour_to", cfg))
    nr = _ad_fmt(ad_val("no_repeat_days", cfg)); da = _ad_fmt(ad_val("delete_after_h", cfg))
    ci = _ad_fmt(ad_val("chat_interval_min", cfg)); cd = _ad_fmt(ad_val("chat_per_day", cfg))
    state = "🟢 включена" if k["enabled"] else "⚪️ выключена"
    master = "" if _db.get_setting("ad_enabled", "0") == "1" else "\n⚠️ Общий выключатель публикаций сейчас ВЫКЛ (📣 Реклама → «Включить публикации»)."
    return (
        f"📣 <b>{html.escape(k['name'])}</b> — {state}{master}\n\n"
        f"📝 Текстов: <b>{nt['c']}</b> (включено {nt['e']}) · 💬 Чатов: <b>{nc}</b> · 👤 Аккаунтов: <b>{na}</b>\n"
        f"🕐 Окно: {hf:02d}:00–{ht:02d}:00 МСК · дни: " + " ".join(
            (AD_DAYS_RU[i] if k["days"].ljust(7, "1")[i] == "1" else "·") for i in range(7)) + "\n"
        f"Пауза в чате: {ci} мин · постов/сутки в чате: {cd} · без повтора текста: {nr} дн. · удаление через: {da} ч\n"
        f"Постов за 24 ч: <b>{n24}</b> · сейчас висит: {live}"
    )

@_ad_route("ad_camp_v")
async def _ad_camp_view(call, a):
    k = _ad_one("SELECT * FROM ad_campaigns WHERE id=?", (int(a[0]),))
    if not k: return "Кампания не найдена"
    i = k["id"]
    days = k["days"].ljust(7, "1")
    rows = [
        [("⏸ Выключить" if k["enabled"] else "▶️ Включить", f"ad_camp_tgl:{i}")],
        [("📝 Тексты", f"ad_tpls:{i}:0"), ("💬 Чаты", f"ad_kc:{i}:0:0"), ("👤 Аккаунты", f"ad_ka:{i}:0")],
        [((AD_DAYS_RU[d] + ("✅" if days[d] == "1" else "⬜")), f"ad_day:{i}:{d}") for d in range(7)],
        [("⚙️ Лимиты и часы", f"ad_cfg:k:{i}"), ("✏️ Название", f"ad_camp_ren:{i}")],
        [("📋 Посты", f"ad_posts:camp:{i}:0"), ("🚀 Опубликовать 1 пост сейчас", f"ad_camp_now:{i}")],
        [("🗑 Удалить кампанию", f"ad_camp_del:{i}")],
        [("◀️ Кампании", "ad_camps")],
    ]
    await _ad_show(call, _ad_camp_text(k), rows)

@_ad_route("ad_camp_tgl")
async def _ad_camp_toggle(call, a):
    i = int(a[0])
    k = _ad_one("SELECT * FROM ad_campaigns WHERE id=?", (i,))
    if not k: return "Не найдена"
    if not k["enabled"]:
        if not _ad_one("SELECT 1 FROM ad_campaign_chats WHERE campaign_id=?", (i,)) or \
           not _ad_one("SELECT 1 FROM ad_campaign_accounts WHERE campaign_id=?", (i,)) or \
           not _ad_one("SELECT 1 FROM ad_templates WHERE campaign_id=? AND enabled=1", (i,)):
            return "Нужны хотя бы 1 текст, 1 чат и 1 аккаунт"
    _ad_exec("UPDATE ad_campaigns SET enabled=1-enabled WHERE id=?", (i,))
    ad_log_add("campaign", f"Кампания «{k['name']}» {'выключена' if k['enabled'] else 'включена'}", camp=i)
    await _ad_camp_view(call, [str(i)])

@_ad_route("ad_day")
async def _ad_day(call, a):
    i, d = int(a[0]), int(a[1])
    k = _ad_one("SELECT days FROM ad_campaigns WHERE id=?", (i,))
    days = list(k["days"].ljust(7, "1"))
    days[d] = "0" if days[d] == "1" else "1"
    _ad_exec("UPDATE ad_campaigns SET days=? WHERE id=?", ("".join(days), i))
    await _ad_camp_view(call, [str(i)])

@_ad_route("ad_camp_ren")
async def _ad_camp_rename(call, a):
    i = int(a[0])
    _admin_pending[call.from_user.id] = f"ad:camp_ren:{i}"
    await _ad_show(call, "✏️ Введите новое название кампании:", [[("◀️ Отмена", f"ad_camp_v:{i}")]])

@_ad_route("ad_camp_del")
async def _ad_camp_del(call, a):
    i = int(a[0])
    live = _ad_one("SELECT COUNT(*) c FROM ad_posts WHERE campaign_id=? AND status='posted'", (i,))["c"]
    await _ad_show(call, "🗑 <b>Удалить кампанию вместе с её текстами?</b>\nИстория постов останется."
                         + (f"\nУже опубликованные посты ({live}) всё равно будут удалены по таймеру." if live else ""),
                   [[("✅ Да", f"ad_camp_del2:{i}")], [("◀️ Отмена", f"ad_camp_v:{i}")]])

@_ad_route("ad_camp_del2")
async def _ad_camp_del2(call, a):
    i = int(a[0])
    for t in ("ad_campaign_chats", "ad_campaign_accounts"):
        _ad_exec(f"DELETE FROM {t} WHERE campaign_id=?", (i,))
    _ad_exec("DELETE FROM ad_templates WHERE campaign_id=?", (i,))
    _ad_exec("DELETE FROM ad_campaigns WHERE id=?", (i,))
    ad_log_add("campaign", f"Кампания №{i} удалена")
    await _ad_camps(call, [])

@_ad_route("ad_camp_now")
async def _ad_camp_now(call, a):
    i = int(a[0])
    k = _ad_one("SELECT * FROM ad_campaigns WHERE id=?", (i,))
    chats = _ad_all("SELECT c.* FROM ad_chats c JOIN ad_campaign_chats cc ON cc.chat_id=c.id WHERE cc.campaign_id=? AND c.enabled=1", (i,))
    random.shuffle(chats)
    await safe_edit(call, "⏳ Публикую…", None)
    results = []
    for c in chats:
        r = await ad_try_chat(call.bot, k, c, force=True)
        results.append(f"{html.escape(c.get('title') or c['link'])}: {r}")
        if r == "posted": break
    human = {"posted": "✅ опубликовано", "no_account": "нет доступного аккаунта", "no_text": "нет свободных текстов"}
    lines = [x.rsplit(": ", 1)[0] + ": " + human.get(x.rsplit(": ", 1)[1], x.rsplit(": ", 1)[1]) for x in results] or ["нет включённых чатов"]
    await _ad_show(call, "🚀 <b>Результат</b>\n\n" + "\n".join(lines) +
                   "\n\nРучная публикация игнорирует окно часов и паузу в чате, но не лимиты аккаунта.",
                   [[("◀️ К кампании", f"ad_camp_v:{i}")]])

def _ad_toggle_rows(items: list[dict], chosen: set, cb_fmt: str, label) -> list:
    return [[(("✅ " if it["id"] in chosen else "⬜ ") + label(it), cb_fmt.format(id=it["id"]))] for it in items]

@_ad_route("ad_kc")
async def _ad_camp_chats(call, a):
    i, page = int(a[0]), int(a[1])
    chats = _ad_all("SELECT * FROM ad_chats ORDER BY id")
    chosen = {r["chat_id"] for r in _ad_all("SELECT chat_id FROM ad_campaign_chats WHERE campaign_id=?", (i,))}
    chunk, pages = _ad_page(chats, page)
    rows = _ad_toggle_rows(chunk, chosen, f"ad_kct:{i}:{page}:" + "{id}", lambda c: (c.get("title") or c["link"])[:50])
    nav = _ad_nav(f"ad_kc:{i}:" + "{p}", page, pages)
    if nav: rows.append(nav)
    rows.append([("➕ Добавить чаты", f"ad_chat_add:{i}")])
    rows.append([("✅ Выбрать все", f"ad_kcall:{i}:1"), ("⬜ Снять все", f"ad_kcall:{i}:0")])
    rows.append([("◀️ К кампании", f"ad_camp_v:{i}")])
    await _ad_show(call, f"💬 <b>Чаты кампании</b>\nВыбрано: <b>{len(chosen)}</b> из {len(chats)}\n"
                         "Нажмите на чат, чтобы добавить или убрать.", rows)

@_ad_route("ad_kct")
async def _ad_camp_chat_toggle(call, a):
    i, page, c = int(a[0]), int(a[1]), int(a[2])
    if _ad_one("SELECT 1 FROM ad_campaign_chats WHERE campaign_id=? AND chat_id=?", (i, c)):
        _ad_exec("DELETE FROM ad_campaign_chats WHERE campaign_id=? AND chat_id=?", (i, c))
    else:
        _ad_exec("INSERT OR IGNORE INTO ad_campaign_chats VALUES(?,?)", (i, c))
    await _ad_camp_chats(call, [str(i), str(page)])

@_ad_route("ad_kcall")
async def _ad_camp_chat_all(call, a):
    i, on = int(a[0]), a[1] == "1"
    _ad_exec("DELETE FROM ad_campaign_chats WHERE campaign_id=?", (i,))
    if on: _ad_exec("INSERT OR IGNORE INTO ad_campaign_chats SELECT ?, id FROM ad_chats", (i,))
    await _ad_camp_chats(call, [str(i), "0"])

@_ad_route("ad_ka")
async def _ad_camp_accs(call, a):
    i = int(a[0])
    accs = _ad_all("SELECT * FROM ad_accounts ORDER BY id")
    chosen = {r["account_id"] for r in _ad_all("SELECT account_id FROM ad_campaign_accounts WHERE campaign_id=?", (i,))}
    rows = _ad_toggle_rows(accs, chosen, f"ad_kat:{i}:" + "{id}", lambda x: f"{_ad_icon_acc(x)} {ad_acc_label(x)}")
    rows.append([("◀️ К кампании", f"ad_camp_v:{i}")])
    await _ad_show(call, f"👤 <b>Аккаунты кампании</b>\nВыбрано: <b>{len(chosen)}</b> из {len(accs)}\n"
                         "Кампания публикует только с отмеченных аккаунтов." +
                   ("\n\nАккаунтов нет — добавьте их в разделе «Аккаунты»." if not accs else ""), rows)

@_ad_route("ad_kat")
async def _ad_camp_acc_toggle(call, a):
    i, x = int(a[0]), int(a[1])
    if _ad_one("SELECT 1 FROM ad_campaign_accounts WHERE campaign_id=? AND account_id=?", (i, x)):
        _ad_exec("DELETE FROM ad_campaign_accounts WHERE campaign_id=? AND account_id=?", (i, x))
    else:
        _ad_exec("INSERT OR IGNORE INTO ad_campaign_accounts VALUES(?,?)", (i, x))
    await _ad_camp_accs(call, [str(i)])


# ── Тексты (шаблоны) ───────────────────────────────────────────
@_ad_route("ad_tpls")
async def _ad_tpls(call, a):
    i, page = int(a[0]), int(a[1])
    tpls = _ad_all("SELECT * FROM ad_templates WHERE campaign_id=? ORDER BY id", (i,))
    chunk, pages = _ad_page(tpls, page, 8)
    rows = [[(f"{'✅' if t['enabled'] else '⬜'} №{t['id']} {t.get('title') or ad_plain(t['text'], 28)}"[:60], f"ad_tpl_v:{t['id']}")] for t in chunk]
    nav = _ad_nav(f"ad_tpls:{i}:" + "{p}", page, pages)
    if nav: rows.append(nav)
    rows.append([("➕ Добавить тексты", f"ad_tpl_add:{i}")])
    rows.append([("◀️ К кампании", f"ad_camp_v:{i}")])
    en = sum(t["enabled"] for t in tpls)
    await _ad_show(call, f"📝 <b>Тексты кампании</b>\nВсего: {len(tpls)}, включено: {en}\n\n"
                         "Тексты выбираются случайно и не повторяются в чате "
                         f"{_ad_fmt(ad_val('no_repeat_days', _ad_cfg(_ad_one('SELECT cfg FROM ad_campaigns WHERE id=?', (i,)))))} дн. "
                         "(настраивается в «Лимиты»). Чем больше текстов, тем реже придётся ждать.", rows)

@_ad_route("ad_tpl_add")
async def _ad_tpl_add(call, a):
    i = int(a[0])
    _admin_pending[call.from_user.id] = f"ad:tpl_add:{i}"
    await _ad_show(call,
        "➕ <b>Добавление текстов</b>\n\nПрисылайте тексты — каждое сообщение станет отдельным текстом. "
        "Можно прислать сразу несколько в одном сообщении, разделив строкой <code>---</code>.\n"
        "Форматирование (жирный, ссылки, курсив) сохраняется.\n\n"
        "Переменные: <code>{bot_link}</code> — ссылка на бота с меткой источника, <code>{chat}</code>, "
        "<code>{account}</code>, <code>{campaign}</code> и ваши (🔤 Переменные).\n\n"
        "Когда закончите — нажмите «Готово».",
        [[("✅ Готово", f"ad_tpls:{i}:0")]])

def _ad_tpl_text(t: dict) -> str:
    n = _ad_one("SELECT COUNT(*) c, MAX(posted_at) m FROM ad_posts WHERE template_id=?", (t["id"],))
    body = t["text"]
    cut = body if len(body) < 1800 else body[:1800] + "…"
    return (f"📝 <b>Текст №{t['id']}</b> {'✅ включён' if t['enabled'] else '⬜ выключен'}\n"
            f"Публикаций: {n['c']}" + (f" · последняя {fmt_msk(n['m'], '%d.%m %H:%M')}" if n["m"] else "") +
            f" · длина {len(body)}\n\n" + cut)

@_ad_route("ad_tpl_v")
async def _ad_tpl_view(call, a):
    t = _ad_one("SELECT * FROM ad_templates WHERE id=?", (int(a[0]),))
    if not t: return "Текст не найден"
    i = t["id"]
    rows = [
        [("⬜ Выключить" if t["enabled"] else "✅ Включить", f"ad_tpl_tgl:{i}"), ("👁 Предпросмотр", f"ad_tpl_prev:{i}")],
        [("✏️ Изменить", f"ad_tpl_edit:{i}"), ("📄 Копия", f"ad_tpl_copy:{i}")],
        [("🗑 Удалить", f"ad_tpl_del:{i}")],
        [("◀️ Тексты", f"ad_tpls:{t['campaign_id']}:0")],
    ]
    try:
        await _ad_show(call, _ad_tpl_text(t), rows)
    except Exception:
        await _ad_show(call, html.escape(_ad_tpl_text(t)), rows)

@_ad_route("ad_tpl_tgl")
async def _ad_tpl_toggle(call, a):
    _ad_exec("UPDATE ad_templates SET enabled=1-enabled WHERE id=?", (int(a[0]),))
    await _ad_tpl_view(call, a)

@_ad_route("ad_tpl_copy")
async def _ad_tpl_copy(call, a):
    t = _ad_one("SELECT * FROM ad_templates WHERE id=?", (int(a[0]),))
    nid = _ad_exec("INSERT INTO ad_templates(campaign_id,title,text,enabled) VALUES(?,?,?,?)",
                   (t["campaign_id"], t.get("title"), t["text"], t["enabled"]))
    ad_log_add("template", f"Текст №{t['id']} скопирован в №{nid}", camp=t["campaign_id"])
    await _ad_tpl_view(call, [str(nid)])
    return "Копия создана"

@_ad_route("ad_tpl_edit")
async def _ad_tpl_edit(call, a):
    i = int(a[0])
    _admin_pending[call.from_user.id] = f"ad:tpl_edit:{i}"
    await _ad_show(call, "✏️ Пришлите новый текст (заменит старый, форматирование сохранится):",
                   [[("◀️ Отмена", f"ad_tpl_v:{i}")]])

@_ad_route("ad_tpl_del")
async def _ad_tpl_del(call, a):
    i = int(a[0])
    await _ad_show(call, "🗑 Удалить этот текст?", [[("✅ Да", f"ad_tpl_del2:{i}")], [("◀️ Отмена", f"ad_tpl_v:{i}")]])

@_ad_route("ad_tpl_del2")
async def _ad_tpl_del2(call, a):
    t = _ad_one("SELECT * FROM ad_templates WHERE id=?", (int(a[0]),))
    if t:
        _ad_exec("DELETE FROM ad_templates WHERE id=?", (t["id"],))
        ad_log_add("template", f"Текст №{t['id']} удалён", camp=t["campaign_id"])
        await _ad_tpls(call, [str(t["campaign_id"]), "0"])

@_ad_route("ad_tpl_prev")
async def _ad_tpl_preview(call, a):
    t = _ad_one("SELECT * FROM ad_templates WHERE id=?", (int(a[0]),))
    k = _ad_one("SELECT * FROM ad_campaigns WHERE id=?", (t["campaign_id"],))
    chat = _ad_one("SELECT c.* FROM ad_chats c JOIN ad_campaign_chats cc ON cc.chat_id=c.id WHERE cc.campaign_id=? LIMIT 1", (k["id"],))
    acc = _ad_one("SELECT x.* FROM ad_accounts x JOIN ad_campaign_accounts ca ON ca.account_id=x.id WHERE ca.campaign_id=? LIMIT 1", (k["id"],))
    if not _ad_state["bot_username"]:
        try: _ad_state["bot_username"] = (await call.bot.get_me()).username or ""
        except Exception: pass
    out = ad_render(t["text"], k, chat, acc, t)
    try:
        await call.message.answer("👁 <b>Так увидят в чате:</b>\n\n" + out, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
    except TelegramBadRequest as e:
        await call.message.answer("⚠️ Telegram не принял разметку этого текста: " + html.escape(str(e)[:150]) +
                                  "\n\nТекст как есть:\n" + html.escape(out))


# ── История постов ─────────────────────────────────────────────
def _ad_posts_filter(kind: str, oid: int) -> tuple[str, tuple]:
    col = {"camp": "campaign_id", "chat": "chat_id", "acc": "account_id"}.get(kind)
    return (f"WHERE {col}=?", (oid,)) if col else ("", ())

@_ad_route("ad_posts")
async def _ad_posts(call, a):
    kind, oid, page = a[0], int(a[1]), int(a[2])
    w, args = _ad_posts_filter(kind, oid)
    total = _ad_one(f"SELECT COUNT(*) c FROM ad_posts {w}", args)["c"]
    pages = max(1, (total + 9) // 10); page = max(0, min(page, pages - 1))
    rows_db = _ad_all(f"SELECT p.*, c.title ct, c.link cl, a.username au, a.name an FROM ad_posts p "
                      f"LEFT JOIN ad_chats c ON c.id=p.chat_id LEFT JOIN ad_accounts a ON a.id=p.account_id "
                      f"{w.replace('WHERE ', 'WHERE p.') if w else ''} ORDER BY p.id DESC LIMIT 10 OFFSET ?", args + (page * 10,))
    st_ic = {"posted": "🟢", "deleted": "🗑", "delete_failed": "⚠️"}
    lines = []
    for p in rows_db:
        extra = ""
        if p["status"] == "posted" and p["delete_at"]: extra = f" · удалится {fmt_msk(p['delete_at'], '%d.%m %H:%M')}"
        if p["status"] == "deleted": extra = f" · удалён {fmt_msk(p['deleted_at'], '%d.%m %H:%M')}"
        lines.append(f"{st_ic.get(p['status'], '•')} <b>№{p['id']}</b> {fmt_msk(p['posted_at'], '%d.%m %H:%M')} · "
                     f"{html.escape((p['ct'] or p['cl'] or '?')[:28])} · "
                     f"{html.escape(('@' + p['au']) if p['au'] else (p['an'] or '?'))} · текст №{p['template_id']}{extra}")
    back = {"camp": f"ad_camp_v:{oid}", "chat": f"ad_chat_v:{oid}", "acc": f"ad_acc_v:{oid}"}.get(kind, "ad_menu")
    live = _ad_one(f"SELECT COUNT(*) c FROM ad_posts {w + ' AND' if w else 'WHERE'} status='posted'", args)["c"]
    rows = []
    nav = _ad_nav(f"ad_posts:{kind}:{oid}:" + "{p}", page, pages)
    if nav: rows.append(nav)
    if live: rows.append([(f"🗑 Удалить все висящие сейчас ({live})", f"ad_delall:{kind}:{oid}")])
    rows.append([("◀️ Назад", back)])
    await _ad_show(call, f"📋 <b>Опубликованные посты</b> ({total})\n\n" + ("\n".join(lines) or "Пока ничего."), rows)

@_ad_route("ad_delall")
async def _ad_delall(call, a):
    kind, oid = a[0], int(a[1])
    w, args = _ad_posts_filter(kind, oid)
    n = _ad_one(f"SELECT COUNT(*) c FROM ad_posts {w + ' AND' if w else 'WHERE'} status='posted'", args)["c"]
    await _ad_show(call, f"🗑 Удалить прямо сейчас все висящие посты ({n})?\nУдаление идёт с паузой, это займёт время.",
                   [[("✅ Да, удалить", f"ad_delall2:{kind}:{oid}")], [("◀️ Отмена", f"ad_posts:{kind}:{oid}:0")]])

@_ad_route("ad_delall2")
async def _ad_delall2(call, a):
    kind, oid = a[0], int(a[1])
    w, args = _ad_posts_filter(kind, oid)
    _ad_exec(f"UPDATE ad_posts SET delete_at=?, del_attempts=0 {w + ' AND' if w else 'WHERE'} status='posted'",
             (_ad_ts(_ad_now()),) + args)
    ad_log_add("delete", "Запущено ручное удаление всех висящих постов")
    asyncio.create_task(ad_delete_due(call.bot, limit=500))
    await _ad_posts(call, [kind, str(oid), "0"])
    return "Удаление запущено"


# ── Журнал ─────────────────────────────────────────────────────
_AD_LOG_FILTERS = {
    "all": ("Все", ""), "err": ("Ошибки", "AND level IN ('warn','error')"),
    "post": ("Посты", "AND kind IN ('post','delete')"),
    "set": ("Настройки", "AND kind IN ('account','chat','campaign','template','setting')"),
    "start": ("Старты", "AND kind='start'"),
}
_AD_LOG_ICON = {"info": "ℹ️", "warn": "⚠️", "error": "🔴"}

def _ad_log_lines(rows: list[dict]) -> str:
    return "\n".join(f"{_AD_LOG_ICON.get(r['level'], '•')} <code>{fmt_msk(r['ts'], '%d.%m %H:%M')}</code> {html.escape(r['text'][:220])}"
                     for r in rows) or "Пусто."

@_ad_route("ad_log")
async def _ad_log_view(call, a):
    flt, page = a[0], int(a[1])
    cond = _AD_LOG_FILTERS.get(flt, _AD_LOG_FILTERS["all"])[1]
    total = _ad_one(f"SELECT COUNT(*) c FROM ad_log WHERE 1=1 {cond}")["c"]
    pages = max(1, (total + 11) // 12); page = max(0, min(page, pages - 1))
    rows_db = _ad_all(f"SELECT * FROM ad_log WHERE 1=1 {cond} ORDER BY id DESC LIMIT 12 OFFSET ?", (page * 12,))
    rows = [[((("• " if k == flt else "") + v[0]), f"ad_log:{k}:0") for k, v in list(_AD_LOG_FILTERS.items())[:3]],
            [((("• " if k == flt else "") + v[0]), f"ad_log:{k}:0") for k, v in list(_AD_LOG_FILTERS.items())[3:]]]
    nav = _ad_nav(f"ad_log:{flt}:" + "{p}", page, pages)
    if nav: rows.append(nav)
    rows.append([("🔎 Поиск", "ad_logq"), ("🧹 Очистить старше 30 дн.", "ad_logclr")])
    rows.append([("◀️ Реклама", "ad_menu")])
    await _ad_show(call, f"📜 <b>Журнал</b> — {_AD_LOG_FILTERS.get(flt, _AD_LOG_FILTERS['all'])[0]} ({total})\n\n" + _ad_log_lines(rows_db), rows)

@_ad_route("ad_logq")
async def _ad_log_search_prompt(call, a):
    _admin_pending[call.from_user.id] = "ad:log_search"
    await _ad_show(call, "🔎 Введите слово или фразу для поиска по журналу:", [[("◀️ Отмена", "ad_log:all:0")]])

@_ad_route("ad_logclr")
async def _ad_log_clear(call, a):
    _ad_exec("DELETE FROM ad_log WHERE ts<?", (_ad_ts(_ad_now() - timedelta(days=30)),))
    await _ad_log_view(call, ["all", "0"])
    return "Старые записи удалены"


# ── Настройки / лимиты ─────────────────────────────────────────
def _ad_cfg_back(scope: str, oid: int) -> str:
    return {"g": "ad_menu", "a": f"ad_acc_v:{oid}", "c": f"ad_chat_v:{oid}", "k": f"ad_camp_v:{oid}"}[scope]

@_ad_route("ad_cfg")
async def _ad_cfg_screen(call, a):
    scope, oid = a[0], int(a[1])
    cfg = {} if scope == "g" else _ad_cfg(_ad_one(f"SELECT cfg FROM {AD_SCOPE_TABLE[scope]} WHERE id=?", (oid,)))
    rows = []
    for key, (short, hint, mn, mx, scopes, dflt) in AD_FIELDS.items():
        if scope not in scopes: continue
        if scope == "g":
            custom = _db.get_setting(f"ad_g_{key}", "") != ""
            val = f"{_ad_fmt(_ad_global(key))}{'' if custom else ' (умолч.)'}"
        elif cfg.get(key) is not None:
            val = _ad_fmt(cfg[key])
        else:
            val = f"({_ad_fmt(_ad_global(key))})"
        rows.append([(f"{short}: {val}", f"ad_cf:{scope}:{oid}:{key}")])
    rows.append([("◀️ Назад", _ad_cfg_back(scope, oid))])
    legend = ("Значения в скобках берутся из общих настроек. Нажмите на параметр, чтобы изменить; "
              "«-» вернёт значение по умолчанию.\n"
              "Приоритет: чат → кампания → аккаунт → общие.") if scope != "g" else \
             "Эти значения действуют везде, где не заданы свои (на аккаунте, чате или кампании)."
    await _ad_show(call, f"⚙️ <b>{AD_SCOPE_NAME[scope]}: лимиты и параметры</b>\n\n{legend}", rows)

@_ad_route("ad_cf")
async def _ad_cfg_field(call, a):
    scope, oid, key = a[0], int(a[1]), a[2]
    short, hint, mn, mx, scopes, dflt = AD_FIELDS[key]
    _admin_pending[call.from_user.id] = f"ad:set:{scope}:{oid}:{key}"
    await _ad_show(call, f"⚙️ <b>{short}</b>\n\n{hint}\n\nДопустимо: от {_ad_fmt(mn)} до {_ad_fmt(mx)}. "
                         f"По умолчанию: {_ad_fmt(dflt)}.\n\nВведите число (или «-», чтобы сбросить):",
                   [[("◀️ Отмена", f"ad_cfg:{scope}:{oid}")]])


# ── Переменные ─────────────────────────────────────────────────
_AD_RESERVED = {"bot_link", "bot_username", "chat", "campaign", "account"}

@_ad_route("ad_vars")
async def _ad_vars_screen(call, a):
    v = ad_vars()
    lines = "\n".join(f"<code>{{{html.escape(k)}}}</code> = {html.escape(val[:60])}" for k, val in v.items()) or "Своих переменных пока нет."
    rows = [[(f"🗑 {k}", f"ad_vardel:{k}")] for k in list(v)[:20]]
    rows.append([("➕ Добавить", "ad_varadd")])
    rows.append([("◀️ Реклама", "ad_menu")])
    await _ad_show(call,
        "🔤 <b>Переменные</b>\n\nВстроенные: <code>{bot_link}</code> (ссылка на бота с меткой источника — "
        "по ней считается статистика), <code>{bot_username}</code>, <code>{chat}</code>, <code>{account}</code>, "
        f"<code>{{campaign}}</code>.\n\nВаши:\n{lines}", rows)

@_ad_route("ad_varadd")
async def _ad_var_add(call, a):
    _admin_pending[call.from_user.id] = "ad:var_add"
    await _ad_show(call, "➕ Пришлите переменные, по одной на строку в виде <code>имя=значение</code>\n"
                         "Имя: латиница, цифры, подчёркивание. Пример: <code>channel=@mychannel</code>",
                   [[("◀️ Отмена", "ad_vars")]])

@_ad_route("ad_vardel")
async def _ad_var_del(call, a):
    v = ad_vars(); v.pop(a[0], None); ad_set_vars(v)
    await _ad_vars_screen(call, [])


# ── Статистика ─────────────────────────────────────────────────
_AD_DIMS = {"camp": ("campaign_id", "Кампании", "ad_campaigns", "name"),
            "acc": ("account_id", "Аккаунты", "ad_accounts", None),
            "chat": ("chat_id", "Чаты", "ad_chats", None),
            "tpl": ("template_id", "Тексты", "ad_templates", None)}
_AD_PERIODS = {"1": "24 ч", "7": "7 дн.", "30": "30 дн.", "0": "всё время"}

def _ad_stat_name(dim: str, key) -> str:
    if key is None: return "—"
    col, _, table, namecol = _AD_DIMS[dim]
    r = _ad_one(f"SELECT * FROM {table} WHERE id=?", (key,))
    if not r: return f"№{key} (удалён)"
    if dim == "camp": return r["name"]
    if dim == "acc": return ad_acc_label(r)
    if dim == "chat": return r.get("title") or r["link"]
    return f"№{r['id']} {ad_plain(r['text'], 24)}"

@_ad_route("ad_stats")
async def _ad_stats(call, a):
    dim, per = a[0], a[1]
    col = _AD_DIMS[dim][0]
    since = _ad_ts(_ad_now() - timedelta(days=int(per))) if per != "0" else "0000"
    posts = {r["k"]: r["c"] for r in _ad_all(f"SELECT {col} k, COUNT(*) c FROM ad_posts WHERE posted_at>=? GROUP BY {col}", (since,))}
    starts = {r["k"]: (r["c"], r["n"]) for r in _ad_all(f"SELECT {col} k, COUNT(*) c, COALESCE(SUM(is_new),0) n FROM ad_starts WHERE ts>=? GROUP BY {col}", (since,))}
    paid = {r["k"]: r["c"] for r in _ad_all(
        f"SELECT s.{col} k, COUNT(DISTINCT s.tg_id) c FROM ad_starts s JOIN clients cl ON cl.tg_id=s.tg_id "
        f"JOIN payments p ON p.client_id=cl.id WHERE s.is_new=1 AND p.status='confirmed' AND s.ts>=? GROUP BY s.{col}", (since,))}
    tp = sum(posts.values()); ts_ = sum(v[0] for v in starts.values()); tn = sum(v[1] for v in starts.values()); tpd = sum(paid.values())
    def pct(x, y): return f"{x * 100 / y:.1f}%" if y else "—"
    keys = sorted(set(posts) | set(starts), key=lambda k: -(starts.get(k, (0, 0))[1] * 1000 + posts.get(k, 0)))
    lines = []
    for k in keys[:15]:
        p = posts.get(k, 0); s, n = starts.get(k, (0, 0)); pd = paid.get(k, 0)
        lines.append(f"<b>{html.escape(_ad_stat_name(dim, k))[:40]}</b>\n   постов {p} → стартов {s} (новых {n}) → оплат {pd} · конв. старт/пост {pct(s, p)}")
    rows = [[((("• " if d == dim else "") + v[1]), f"ad_stats:{d}:{per}") for d, v in list(_AD_DIMS.items())[:2]],
            [((("• " if d == dim else "") + v[1]), f"ad_stats:{d}:{per}") for d, v in list(_AD_DIMS.items())[2:]],
            [((("• " if p_ == per else "") + n), f"ad_stats:{dim}:{p_}") for p_, n in _AD_PERIODS.items()],
            [("◀️ Реклама", "ad_menu")]]
    head = (f"📊 <b>Статистика — {_AD_DIMS[dim][1]}</b>, период: {_AD_PERIODS[per]}\n\n"
            f"Воронка: постов <b>{tp}</b> → стартов бота <b>{ts_}</b> ({pct(ts_, tp)}) → новых пользователей <b>{tn}</b> "
            f"→ оплат <b>{tpd}</b> ({pct(tpd, tn)})\n\n")
    note = ("\n\n<i>«Старт» — нажатие Start по рекламной ссылке {bot_link} (Telegram не показывает клики до этого). "
            "Оплаты считаются у новых пользователей, пришедших по ссылке.</i>").replace("{bot_link}", "")
    await _ad_show(call, head + ("\n".join(lines) or "Данных за период нет.") + note, rows)


# ── Ввод текста админом (вызывается из admin_text_handler) ─────
async def ad_text_input(msg: "Message", action: str) -> None:
    uid = msg.from_user.id
    p = action.split(":")           # ["ad", "<cmd>", ...]
    cmd = p[1]
    text = (msg.text or "").strip()

    if cmd == "acc_phone":
        phone = re.sub(r"[^\d+]", "", text)
        phone = phone if phone.startswith("+") else "+" + phone
        if len(phone) < 8:
            _admin_pending[uid] = "ad:acc_phone"
            await safe_answer(msg, "❌ Похоже на неверный номер. Введите ещё раз (+79991234567):", kb_back("ad_acc")); return
        old = _ad_auth.pop(uid, None)
        if old:
            try: await old["client"].disconnect()
            except Exception: pass
        cl = TelegramClient(StringSession(), API_ID, API_HASH)
        try:
            await cl.connect()
            res = await cl.send_code_request(phone)
        except Exception as e:
            try: await cl.disconnect()
            except Exception: pass
            _admin_pending[uid] = "ad:acc_phone"
            await safe_answer(msg, f"❌ Ошибка: <code>{html.escape(type(e).__name__)}: {html.escape(str(e)[:150])}</code>\n\nВведите номер снова:", kb_back("ad_acc")); return
        _ad_auth[uid] = {"client": cl, "phone": phone, "hash": res.phone_code_hash}
        _admin_pending[uid] = "ad:acc_code"
        await safe_answer(msg, f"📱 Код отправлен на <code>{phone}</code>.\n{_ad_code_where(res)}\n\n"
                               "Введите его <b>с разделителями</b>, например <code>1-2-3-4-5</code>:", mkb(_ad_code_rows()))
        return

    if cmd == "acc_session":
        sess = re.sub(r"\s+", "", text)
        try: await msg.delete()
        except Exception: pass
        back = kb_back("ad_acc")
        if len(sess) < 100 or not re.fullmatch(r"[A-Za-z0-9_\-=+/]+", sess):
            _admin_pending[uid] = "ad:acc_session"
            await safe_answer(msg, "❌ Это не похоже на строку сессии Telethon. Пришлите её ещё раз целиком "
                                   "(сессии других библиотек, например Pyrogram, не подойдут):", back); return
        if sess == STRING_SESSION or sess == _db.get_setting("string_session", ""):
            await safe_answer(msg, "❌ Это сессия основного аккаунта мониторинга. Для рекламы нужен отдельный аккаунт.", back); return
        cl = TelegramClient(StringSession(sess), API_ID, API_HASH)
        try:
            await cl.connect()
            if not await cl.is_user_authorized():
                await cl.disconnect()
                _admin_pending[uid] = "ad:acc_session"
                await safe_answer(msg, "❌ Сессия недействительна или отозвана. Пришлите другую:", back); return
            me = await cl.get_me()
        except Exception as e:
            try: await cl.disconnect()
            except Exception: pass
            _admin_pending[uid] = "ad:acc_session"
            await safe_answer(msg, f"❌ Не удалось войти: <code>{html.escape(type(e).__name__)}: {html.escape(str(e)[:150])}</code>\n\nПришлите другую сессию:", back); return
        phone = ("+" + me.phone) if getattr(me, "phone", None) else "—"
        await ad_finish_auth(msg, {"client": cl, "phone": phone})
        return

    if cmd == "acc_code":
        st = _ad_auth.get(uid)
        if not st:
            await safe_answer(msg, "Сессия входа истекла, начните заново.", kb_back("ad_acc")); return
        code = re.sub(r"\D", "", text)
        try: await msg.delete()
        except Exception: pass
        try:
            await st["client"].sign_in(phone=st["phone"], code=code, phone_code_hash=st["hash"])
            await ad_finish_auth(msg, st)
        except SessionPasswordNeededError:
            _admin_pending[uid] = "ad:acc_2fa"
            await safe_answer(msg, "🔐 Введите пароль 2FA (сообщение с паролем будет удалено):")
        except PhoneCodeInvalidError:
            _admin_pending[uid] = "ad:acc_code"
            await safe_answer(msg, "❌ Неверный код. Введите ещё раз:")
        except PhoneCodeExpiredError:
            _admin_pending[uid] = "ad:acc_phone"
            await safe_answer(msg, "❌ Код устарел. Введите номер снова:")
        except Exception as e:
            await safe_answer(msg, f"❌ Ошибка: <code>{html.escape(type(e).__name__)}: {html.escape(str(e)[:150])}</code>", kb_back("ad_acc"))
        return

    if cmd == "acc_2fa":
        st = _ad_auth.get(uid)
        if not st:
            await safe_answer(msg, "Сессия входа истекла, начните заново.", kb_back("ad_acc")); return
        try: await msg.delete()
        except Exception: pass
        try:
            await st["client"].sign_in(password=text)
            await ad_finish_auth(msg, st)
        except PasswordHashInvalidError:
            _admin_pending[uid] = "ad:acc_2fa"
            await safe_answer(msg, "❌ Неверный пароль. Введите ещё раз:")
        except Exception as e:
            await safe_answer(msg, f"❌ Ошибка: <code>{html.escape(type(e).__name__)}: {html.escape(str(e)[:150])}</code>", kb_back("ad_acc"))
        return

    if cmd == "chat_add":
        camp = int(p[2]) if len(p) > 2 else 0
        added, dup, bad = [], 0, []
        for line in text.splitlines():
            line = line.strip()
            if not line: continue
            parsed = ad_parse_link(line)
            if not parsed: bad.append(line[:40]); continue
            link = ad_norm_link(*parsed)
            ex = _ad_one("SELECT id FROM ad_chats WHERE lower(link)=lower(?)", (link,))
            if ex:
                dup += 1; cid = ex["id"]
            else:
                cid = _ad_exec("INSERT INTO ad_chats(link) VALUES(?)", (link,))
                added.append(link)
                ad_log_add("chat", f"Чат добавлен: {link}", chat=cid)
            if camp: _ad_exec("INSERT OR IGNORE INTO ad_campaign_chats VALUES(?,?)", (camp, cid))
        out = f"✅ Добавлено: <b>{len(added)}</b>" + (f"\nУже были: {dup}" if dup else "") + \
              (("\n❌ Не распознаны:\n" + "\n".join(html.escape(b) for b in bad)) if bad else "")
        out += "\n\nДальше: «💬 Чаты» → чат → «🔎 Проверить доступ» (для приватных ссылок аккаунт сначала должен вступить)."
        await safe_answer(msg, out, mkb([[("💬 Чаты", "ad_chats")]] + ([[("◀️ К кампании", f"ad_camp_v:{camp}")]] if camp else [])))
        return

    if cmd == "camp_add":
        if not text:
            _admin_pending[uid] = "ad:camp_add"; await safe_answer(msg, "Введите название:"); return
        cid = _ad_exec("INSERT INTO ad_campaigns(name) VALUES(?)", (text[:60],))
        ad_log_add("campaign", f"Кампания создана: {text[:60]}", camp=cid)
        await safe_answer(msg, f"✅ Кампания «{html.escape(text[:60])}» создана. Выключена, пока не добавите тексты, чаты и аккаунты.",
                          mkb([[("📣 Открыть кампанию", f"ad_camp_v:{cid}")]]))
        return

    if cmd == "camp_ren":
        i = int(p[2])
        if text: _ad_exec("UPDATE ad_campaigns SET name=? WHERE id=?", (text[:60], i))
        await safe_answer(msg, "✅ Название обновлено", mkb([[("📣 К кампании", f"ad_camp_v:{i}")]]))
        return

    if cmd in ("tpl_add", "tpl_edit"):
        body = (msg.html_text or "").strip()
        if not body:
            _admin_pending[uid] = action
            await safe_answer(msg, "Пришлите текст сообщением (без вложений)."); return
        if cmd == "tpl_edit":
            i = int(p[2])
            if len(body) > 4000:
                _admin_pending[uid] = action
                await safe_answer(msg, "❌ Текст длиннее 4000 символов — Telegram не позволит его отправить. Сократите."); return
            t = _ad_one("SELECT * FROM ad_templates WHERE id=?", (i,))
            _ad_exec("UPDATE ad_templates SET text=?, title=NULL WHERE id=?", (body, i))
            ad_log_add("template", f"Текст №{i} изменён", camp=t["campaign_id"] if t else None)
            await safe_answer(msg, "✅ Текст обновлён", mkb([[("📝 К тексту", f"ad_tpl_v:{i}")]]))
            return
        camp = int(p[2])
        parts = [x.strip() for x in re.split(r"\n\s*---+\s*\n", body) if x.strip()]
        ok, long_ = 0, 0
        for part in parts:
            if len(part) > 4000: long_ += 1; continue
            _ad_exec("INSERT INTO ad_templates(campaign_id,title,text) VALUES(?,?,?)", (camp, ad_plain(part, 40), part))
            ok += 1
        if ok: ad_log_add("template", f"Добавлено текстов: {ok}", camp=camp)
        total = _ad_one("SELECT COUNT(*) c FROM ad_templates WHERE campaign_id=?", (camp,))["c"]
        _admin_pending[uid] = action                      # режим «много текстов подряд»
        await safe_answer(msg, f"✅ Добавлено: <b>{ok}</b> (всего в кампании {total})"
                               + (f"\n⚠️ Пропущено слишком длинных (>4000 символов): {long_}" if long_ else "")
                               + "\nПрисылайте следующие или нажмите «Готово».",
                          mkb([[("✅ Готово", f"ad_tpls:{camp}:0")]]))
        return

    if cmd == "set":
        scope, oid, key = p[2], int(p[3]), p[4]
        short, hint, mn, mx, scopes, dflt = AD_FIELDS[key]
        back = mkb([[("⚙️ К настройкам", f"ad_cfg:{scope}:{oid}")]])
        if text in ("-", "—", "сброс"):
            _ad_set_cfg(scope, oid, key, None)
            ad_log_add("setting", f"{AD_SCOPE_NAME[scope]} №{oid}: «{short}» сброшено")
            await safe_answer(msg, f"✅ «{short}» сброшено на значение по умолчанию.", back); return
        try: v = float(text.replace(",", "."))
        except ValueError:
            _admin_pending[uid] = action
            await safe_answer(msg, "❌ Нужно число. Попробуйте ещё раз (или «-» для сброса):", back); return
        if not (mn <= v <= mx):
            _admin_pending[uid] = action
            await safe_answer(msg, f"❌ Допустимо от {_ad_fmt(mn)} до {_ad_fmt(mx)}. Попробуйте ещё раз:", back); return
        _ad_set_cfg(scope, oid, key, v)
        ad_log_add("setting", f"{AD_SCOPE_NAME[scope]} №{oid}: «{short}» = {_ad_fmt(v)}")
        await safe_answer(msg, f"✅ «{short}» = <b>{_ad_fmt(v)}</b>", back)
        return

    if cmd == "var_add":
        v = ad_vars(); ok, bad = 0, []
        for line in (msg.html_text or "").splitlines():
            if not line.strip(): continue
            name, sep, val = line.partition("=")
            name = name.strip()
            if not sep or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) or name in _AD_RESERVED:
                bad.append(line[:40]); continue
            v[name] = val.strip(); ok += 1
        ad_set_vars(v)
        if ok: ad_log_add("setting", f"Добавлено переменных: {ok}")
        await safe_answer(msg, f"✅ Сохранено: {ok}" + (("\n❌ Не распознаны (или имя занято):\n" + "\n".join(html.escape(b) for b in bad)) if bad else ""),
                          mkb([[("🔤 Переменные", "ad_vars")]]))
        return

    if cmd == "log_search":
        q = text
        rows_db = _ad_all("SELECT * FROM ad_log WHERE text LIKE ? ORDER BY id DESC LIMIT 25", (f"%{q}%",))
        await safe_answer(msg, f"🔎 <b>«{html.escape(q)}»</b> — найдено {len(rows_db)} (последние 25)\n\n" + _ad_log_lines(rows_db),
                          mkb([[("📜 Журнал", "ad_log:all:0")]]))
        return

    await safe_answer(msg, "Не понял, что делать с этим сообщением.", kb_back("ad_menu"))


async def main() -> None:
    global _db, _pipeline, _userbot

    log.info("=" * 60)
    log.info(f"phase.parser запускается | версия: {BOT_VERSION}")
    log.info("=" * 60)

    # БД
    _db = Database(DB_PATH); _db.connect(); _db.init_tables(); _ad_init_tables(_db)

    # Bot
    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp  = Dispatcher()
    dp.include_router(admin_router)   # Обрабатывает только ADMIN_ID (фильтр на уровне роутера)
    dp.include_router(client_router)  # Все остальные

    # Снимаем вебхук перед polling — если он вдруг остался включён (после
    # ручного вызова setWebhook, тестов или сбойного деплоя), getUpdates()
    # начинает падать с TelegramConflictError на каждой попытке, и бот
    # фактически не отвечает никому, при этом молча уходя в бесконечный
    # цикл повторов. drop_pending_updates=True на всякий случай подчищает
    # накопившиеся за время простоя апдейты, чтобы не разгребать их разом
    # при старте.
    try:
        await bot.delete_webhook(drop_pending_updates=True)
    except Exception as e:
        log.warning(f"delete_webhook: {e}")

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

    async def _webhook_guard() -> None:
        """Разовый delete_webhook() в начале main() не защищает от повторной
        установки вебхука уже ПОСЛЕ старта — а именно это, похоже,
        и происходит (конфликт возвращается уже во время работы, а не сразу
        при запуске). aiogram сам держит getUpdates во внутреннем
        бесконечном ретрае и наружу эту ошибку не выпускает, так что поймать
        её обычным try/except вокруг start_polling нельзя. Поэтому отдельно,
        раз в минуту, проверяем реальный статус через get_webhook_info и
        снимаем вебхук заново, если он снова оказался включён — не важно,
        кто его включил (сторонний скрипт, инфраструктура хостинга, забытый
        старый контейнер того же бота)."""
        while True:
            await asyncio.sleep(60)
            try:
                info = await bot.get_webhook_info()
                if info.url:
                    log.warning(f"UserBot/Bot: обнаружен активный вебхук ({info.url}) — снимаю повторно")
                    _db.add_log("WARNING", f"Обнаружен активный вебхук ({info.url}) — снят повторно")
                    await bot.delete_webhook(drop_pending_updates=True)
            except Exception as e:
                log.warning(f"_webhook_guard: {e}")

    await asyncio.gather(
        dp.start_polling(bot, allowed_updates=["message","callback_query","inline_query"]),
        _pipeline.run_worker(),
        _periodic_cleanup(),
        _check_expired_subs(bot),
        _send_expiry_reminders(bot),
        _send_mid_trial_reminders(bot),
        _scheduled_broadcast(bot),
        _check_userbot_health(bot),
        _check_message_flow(bot),
        _check_ai_health(bot),
        _safe_userbot_run(),
        _webhook_guard(),
        ad_scheduler_loop(bot),
        ad_cleanup_loop(bot),
    )

if __name__ == "__main__":
    try: asyncio.run(main())
    except KeyboardInterrupt: log.info("phase.parser остановлен")
    except Exception as e: log.critical(f"Критическая ошибка: {e}", exc_info=True)
