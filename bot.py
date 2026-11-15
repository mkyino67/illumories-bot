"""
Telegram-бот приёма заказов (тапы) с голосованием-опросом.
aiogram 3.x + SQLite. Запуск: python bot.py
"""
import asyncio
import csv
import io
import logging
import math
import os
import re
import time
from datetime import datetime
from html import escape as esc

import aiosqlite
from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import (TelegramBadRequest, TelegramForbiddenError,
                                TelegramRetryAfter)
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (BufferedInputFile, CallbackQuery, LabeledPrice,
                           LinkPreviewOptions, Message, Poll, PreCheckoutQuery,
                           ReplyParameters, User, PollAnswer, ReactionTypeEmoji)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from dotenv import load_dotenv

load_dotenv()
TOKEN = os.environ["BOT_TOKEN"]
ADMINS = [int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()]
# Группы привязываются прямо в боте (/admin → Группы и темы); значения из .env — стартовые
ENV_GROUPS = {"pay_chat": os.getenv("PAYMENTS_CHAT_ID", "0"), "pay_topic": os.getenv("PAYMENTS_TOPIC_ID", "0"),
              "ord_chat": os.getenv("ORDERS_CHAT_ID", "0"), "ord_topic": os.getenv("ORDERS_TOPIC_ID", "0"),
              "sup_chat": "0", "sup_topic": "0", "bkc_chat": "0", "bkc_topic": "0",
              "bkt_chat": "0", "bkt_topic": "0"}
DB_PATH = os.getenv("DB_PATH", "taps.db")

POLL_Q = "сколько тапнули?"
POLL_OPTS = ["20", "40", "60", "80", "100", "тапали"]  # числа суммируются, «тапали» не считается

logging.basicConfig(level=logging.INFO)
bot = Bot(TOKEN, default=DefaultBotProperties(
    parse_mode=ParseMode.HTML, link_preview_is_disabled=True))
db: aiosqlite.Connection

STATUS = {
    "pending_approval": "🆕 ожидает подтверждения",
    "awaiting_payment": "💳 ожидает оплаты",
    "review": "🕓 проверка оплаты",
    "voting": "🗳 идёт опрос",
    "voted": "⏳ опрос закрыт",
    "done": "✅ выполнен",
    "rejected": "❌ отклонён",
    "cancelled": "🚫 отменён",
}
PAID = "('voting','voted','done')"

# Прайс из твоего скриншота: (количество, звёзды, рубли)
SEED_TIERS = [(60, 15, 25), (100, 30, 45), (160, 45, 65), (200, 70, 95), (260, 80, 110),
              (300, 110, 145), (360, 120, 160), (400, 130, 180), (460, 150, 205),
              (500, 165, 225), (560, 180, 240), (600, 200, 265), (660, 215, 280),
              (700, 225, 300), (760, 250, 315), (800, 275, 350), (860, 300, 375),
              (900, 325, 400), (960, 365, 425), (1000, 400, 475)]

# ───────────────────────── БАЗА ДАННЫХ ─────────────────────────
async def fetch(sql, args=()):
    async with db.execute(sql, args) as c:
        return await c.fetchall()


async def fetchone(sql, args=()):
    async with db.execute(sql, args) as c:
        return await c.fetchone()


async def run(sql, args=()):
    c = await db.execute(sql, args)
    await db.commit()
    return c.lastrowid


async def count(sql, args=()):
    return (await fetchone(sql, args))[0]


async def get_setting(key):
    r = await fetchone("SELECT value FROM settings WHERE key=?", (key,))
    return r["value"] if r else ""


TEXTS = {  # ключ: (название, текст по умолчанию, доступные подстановки)
    "welcome": ("👋 Приветствие /start", "👋 Привет! Что хочешь сделать — купить тапы или поставить бронь?", ""),
    "requisites": ("💳 Реквизиты для оплаты", "Реквизиты не заданы. Админ: /admin → Тексты бота → Реквизиты.", ""),
    "paused": ("⏸ Приём приостановлен", "⏸ Приём заказов временно приостановлен. Загляните позже!", ""),
    "ask_link": ("🔗 Запрос ссылки", "🔗 Отправьте ссылку на пост:", ""),
    "ask_target": ("💬 Запрос юзернейма", "💬 Отправьте юзернейм (например, @username):", ""),
    "proof_sent": ("📨 Скрин отправлен", "📨 Скрин отправлен на проверку. Я сообщу, как только оплату подтвердят.", ""),
    "pay_ok": ("✅ Оплата подтверждена", "✅ Оплата заказа #{id} подтверждена! Заказ запущен.", "{id}"),
    "pay_no": ("❌ Оплата не найдена", "❌ Оплата заказа #{id} не найдена. Проверьте перевод и отправьте скрин ещё раз.", "{id}"),
    "done": ("🎉 Заказ выполнен", "🎉 Ваш заказ <b>#{id}</b> выполнен! Спасибо за покупку.", "{id}"),
    "support_ask": ("✉️ Запрос сообщения админам", "✉️ Напишите сообщение админам — текстом, фото или файлом. Ответ придёт сюда, в этот чат.", ""),
    "support_sent": ("✅ Обращение отправлено", "✅ Сообщение отправлено админам. Ответим сюда, как только сможем.", ""),
    "review_request": ("⭐ Просьба об отзыве (копипаста)",
                       "🙏 Спасибо, что выбрали нас! Если всё понравилось — оставьте, пожалуйста, отзыв.\n\n"
                       "📝 Можно скопировать и отправить (нажмите на текст):\n"
                       "<code>Заказывал(а) в illumories shop — всё быстро и чётко, рекомендую! ⭐️</code>", "{id}"),
    "book_qty": ("🔖 Бронь: выбор количества", "🔖 <b>Бронь.</b> Выберите количество тапов (по основному прайсу, от 100 до 1000):", ""),
    "book_fandom": ("🔖 Бронь: запрос фандома", "🎭 Введите название фандома на английском языке:", ""),
    "book_en": ("🔖 Бронь: персонаж (EN)", "👤 Теперь введите имя персонажа на английском языке:", ""),
    "book_ru": ("🔖 Бронь: персонаж (RU)", "👤 А теперь имя персонажа на русском языке:", ""),
    "book_user": ("🔖 Бронь: юзернейм", "💬 Отправьте юзернейм (например, @username):", ""),
    "book_paid": ("🔖 Бронь оформлена", "✅ Оплата получена — бронь #{id} оформлена!", "{id}"),
}


class _Safe(dict):
    def __missing__(self, k):
        return "{" + k + "}"


async def T(key, **kw):
    """Текст из настроек (или по умолчанию) с подстановками {id}."""
    v = await get_setting(key) or TEXTS[key][1]
    try:
        return v.format_map(_Safe(kw))
    except (ValueError, IndexError, KeyError):
        return v


async def init_db():
    await db.executescript("""
    CREATE TABLE IF NOT EXISTS users(user_id INTEGER PRIMARY KEY, username TEXT,
        first_name TEXT, joined_at INTEGER, last_active INTEGER, banned INTEGER DEFAULT 0);
    CREATE TABLE IF NOT EXISTS products(id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT,
        active INTEGER DEFAULT 1, min_qty INTEGER DEFAULT 60, max_qty INTEGER DEFAULT 800,
        step INTEGER DEFAULT 20, sort INTEGER DEFAULT 0);
    CREATE TABLE IF NOT EXISTS tiers(product_id INTEGER, qty INTEGER, stars INTEGER, rub INTEGER,
        PRIMARY KEY(product_id, qty));
    CREATE TABLE IF NOT EXISTS orders(id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER,
        product_id INTEGER, title TEXT, qty INTEGER, is_max INTEGER DEFAULT 0,
        price_rub INTEGER, price_stars INTEGER, currency TEXT, post_link TEXT, target_user TEXT,
        promo TEXT, status TEXT, charge_id TEXT, adm_chat INTEGER, adm_msg INTEGER,
        poll_id TEXT, poll_chat INTEGER, poll_msg INTEGER, votes INTEGER DEFAULT 0,
        created_at INTEGER, paid_at INTEGER, done_at INTEGER);
    CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
    CREATE TABLE IF NOT EXISTS promos(code TEXT PRIMARY KEY, percent INTEGER, uses INTEGER);
    CREATE TABLE IF NOT EXISTS buttons(id INTEGER PRIMARY KEY AUTOINCREMENT, label TEXT, kind TEXT,
        value TEXT, sort INTEGER DEFAULT 0, active INTEGER DEFAULT 1);
    CREATE TABLE IF NOT EXISTS tickets(id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, created_at INTEGER);
    CREATE TABLE IF NOT EXISTS ticket_msgs(chat_id INTEGER, msg_id INTEGER, ticket_id INTEGER,
        PRIMARY KEY(chat_id, msg_id));
    CREATE TABLE IF NOT EXISTS voters(poll_id TEXT, user_id INTEGER, label TEXT, taps INTEGER, opt INTEGER,
        PRIMARY KEY(poll_id, user_id));
    CREATE TABLE IF NOT EXISTS bookings(id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, fandom TEXT,
        qty INTEGER, char_en TEXT, char_ru TEXT, username TEXT, price_rub INTEGER, price_stars INTEGER,
        currency TEXT, status TEXT, charge_id TEXT, adm_chat INTEGER, adm_msg INTEGER,
        created_at INTEGER, paid_at INTEGER, done_at INTEGER);
    """)
    cols = [r["name"] for r in await fetch("PRAGMA table_info(bookings)")]
    if "qty" not in cols:
        await db.execute("ALTER TABLE bookings ADD COLUMN qty INTEGER")
    if "username" not in cols:
        await db.execute("ALTER TABLE bookings ADD COLUMN username TEXT")
    if "sort" not in [r["name"] for r in await fetch("PRAGMA table_info(products)")]:
        await db.execute("ALTER TABLE products ADD COLUMN sort INTEGER DEFAULT 0")
    defaults = {"pre_approve": "0", "intake": "1", "auto_done": "0", "review_on": "1",
                "book_min": "100", "book_max": "1000", "book_unique": "0", **ENV_GROUPS}
    for k, v in defaults.items():
        await db.execute("INSERT OR IGNORE INTO settings VALUES(?,?)", (k, v))
    if not await fetchone("SELECT 1 FROM products"):
        c = await db.execute("INSERT INTO products(title) VALUES('👆 Тапы')")
        await db.executemany("INSERT INTO tiers VALUES(?,?,?,?)",
                             [(c.lastrowid, q, s, r) for q, s, r in SEED_TIERS])
    if not await fetchone("SELECT 1 FROM buttons"):
        await db.executemany("INSERT INTO buttons(label,kind,sort) VALUES(?,?,?)",
                             [("💰 Прайс", "price", 10), ("📦 Мои заказы", "my", 20)])
    if not await fetchone("SELECT 1 FROM buttons WHERE kind='support'"):
        await db.execute("INSERT INTO buttons(label,kind,sort) VALUES('✉️ Связаться с админом','support',30)")
    mn = (await fetchone("SELECT COALESCE(MIN(sort),10) FROM buttons"))[0]
    for lbl, kind, off in (("🛒 Купить тапы", "buy", 2), ("🔖 Поставить бронь", "book", 1)):
        if not await fetchone("SELECT 1 FROM buttons WHERE kind=?", (kind,)):
            await db.execute("INSERT INTO buttons(label,kind,sort) VALUES(?,?,?)", (lbl, kind, mn - off))
    await db.commit()


# ───────────────────────── ПРАЙС ─────────────────────────
O = "OoОо"  # в прайсе нули часто набраны буквой «О»
PRICE_RE = re.compile(rf"(\d[\d{O}]*)[^\d\n]*?(\d[\d{O}]*)\s*(?:зв|⭐|звезд\w*|stars?)"
                      rf"[^\d\n]*?(\d[\d{O}]*)\s*(?:₽|р\b|руб\w*)", re.I)
PLAIN_RE = re.compile(r"^\s*(\d+)[^\d\n]+(\d+)[^\d\n]+(\d+)\s*$", re.M)


def _num(s):
    return int(re.sub(f"[{O}]", "0", s))


def parse_price(text):
    """'60 тапов — 15зв & 25₽' или '60 15 25' → [(qty, stars, rub), ...]"""
    rows = PRICE_RE.findall(text) or PLAIN_RE.findall(text)
    d = {_num(q): (_num(s), _num(r)) for q, s, r in rows}
    return [(q, s, r) for q, (s, r) in sorted(d.items())]


async def price_for(pid, qty):
    """Цена из прайса; для промежуточных количеств — линейная интерполяция (вверх)."""
    t = await fetch("SELECT qty,stars,rub FROM tiers WHERE product_id=? ORDER BY qty", (pid,))
    for r in t:
        if r["qty"] == qty:
            return r["stars"], r["rub"]
    lo = [r for r in t if r["qty"] < qty]
    hi = [r for r in t if r["qty"] > qty]
    if not lo or not hi:
        return None
    a, b = lo[-1], hi[0]
    k = (qty - a["qty"]) / (b["qty"] - a["qty"])
    return (math.ceil(a["stars"] + (b["stars"] - a["stars"]) * k),
            math.ceil(a["rub"] + (b["rub"] - a["rub"]) * k))


async def exact_price(pid, qty):
    """Строго цена из прайса (без интерполяции). Для брони."""
    r = await fetchone("SELECT stars,rub FROM tiers WHERE product_id=? AND qty=?", (pid, qty))
    return (r["stars"], r["rub"]) if r else None


def discount(stars, rub, percent):
    rub = max(1, round(rub * (100 - percent) / 100)) if rub else 0
    stars = max(1, round(stars * (100 - percent) / 100)) if stars else 0
    return stars, rub


def fmt_price(stars, rub):
    parts = ([f"{stars}⭐"] if stars else []) + ([f"{rub}₽"] if rub else [])
    return " / ".join(parts)


# ───────────────────────── ПОМОЩНИКИ ─────────────────────────
class Track(BaseMiddleware):
    """Запоминает пользователей, время активности и блокирует забаненных."""

    async def __call__(self, handler, event, data):
        u = data.get("event_from_user")
        chat = data.get("event_chat")
        if u and not u.is_bot and chat and chat.type == "private":
            now = int(time.time())
            await run("""INSERT INTO users(user_id,username,first_name,joined_at,last_active)
                VALUES(?,?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET
                username=excluded.username, first_name=excluded.first_name,
                last_active=excluded.last_active""",
                      (u.id, u.username, u.first_name, now, now))
            r = await fetchone("SELECT banned FROM users WHERE user_id=?", (u.id,))
            if r["banned"] and u.id not in ADMINS:
                return
        return await handler(event, data)


async def target(kind):
    """kind: 'pay' (оплаты) или 'ord' (заказы). Не настроено — первому админу в личку."""
    chat = int(await get_setting(f"{kind}_chat") or 0)
    topic = int(await get_setting(f"{kind}_topic") or 0) or None
    if not chat and kind in ("sup", "bkc", "bkt"):  # отдельная тема не задана — бронь идёт в тему оплат
        return await target("pay")
    return (chat, topic) if chat else (ADMINS[0], None)


async def send_to(kind, text, reply_to=None, **kw):
    """Отправка в группу/тему; если группа не настроена — первому админу."""
    chat, topic = await target(kind)
    rp = ReplyParameters(message_id=reply_to) if reply_to else None
    try:
        return await bot.send_message(chat, text, message_thread_id=topic, reply_parameters=rp, **kw)
    except TelegramBadRequest:
        if reply_to:  # сообщение для ответа удалено — шлём без ответа
            return await bot.send_message(chat, text, message_thread_id=topic, **kw)
        raise


def label_of(u: User):
    return f"@{u.username}" if u.username else f'<a href="tg://user?id={u.id}">{esc(u.full_name)}</a>'


async def label_by_id(uid):
    r = await fetchone("SELECT username, first_name FROM users WHERE user_id=?", (uid,))
    if r and r["username"]:
        return f"@{r['username']}"
    return f'<a href="tg://user?id={uid}">{esc((r and r["first_name"]) or str(uid))}</a>'


def qty_label(o):
    return f"{o['qty']}" + (" (макс)" if o["is_max"] else "")


def adm_text(o, buyer, head, footer=""):
    return (f"{head}\n\n🧾 Заказ #{o['id']}\n👤 {buyer}\n"
            f"📦 Количество: {qty_label(o)}\n🔗 Ссылка: {esc(o['post_link'])}\n"
            f"💬 Юзернейм: {esc(o['target_user'])}\n"
            f"💰 Цена: {fmt_price(o['price_stars'], o['price_rub'])}"
            + (f"\n\n{footer}" if footer else ""))


def to_int(s):
    s = (s or "").strip()
    return int(s) if s.isdigit() else None


def kb_of(rows):
    """rows: список рядов [(текст, callback_data), ...]"""
    b = InlineKeyboardBuilder()
    for row in rows:
        for t, d in row:
            if d.startswith("http"):
                b.button(text=t, url=d)
            else:
                b.button(text=t, callback_data=d)
    b.adjust(*[len(r) for r in rows])
    return b.as_markup()


CANCEL_KB = kb_of([[("❌ Отмена", "cancel")]])
A_CANCEL = kb_of([[("❌ Отмена", "a:menu")]])


def work_kb(oid, close=True):
    rows = [[("📣 Сообщить о выполнении", f"done:{oid}")]]
    if close:
        rows.append([("🔒 Закрыть опрос", f"close:{oid}")])
    return kb_of(rows)


async def edit(cb: CallbackQuery, text, kb=None):
    try:
        await cb.message.edit_text(text, reply_markup=kb)
    except TelegramBadRequest as e:
        if "not modified" in str(e):
            return
        await cb.message.answer(text, reply_markup=kb)


async def edit_card(msg: Message, text, kb=None):
    try:
        if msg.caption is not None or msg.photo or msg.document:
            await msg.edit_caption(caption=text, reply_markup=kb)
        else:
            await msg.edit_text(text, reply_markup=kb)
    except TelegramBadRequest:
        pass


# ───────────────────────── СОСТОЯНИЯ ─────────────────────────
class Order(StatesGroup):
    qty = State()
    link = State()
    target = State()
    confirm = State()
    promo = State()


class Proof(StatesGroup):
    wait = State()


class AddP(StatesGroup):
    title = State()


class EditP(StatesGroup):
    value = State()


class Prices(StatesGroup):
    bulk = State()
    row = State()
    delete = State()
    pct = State()


class SetVal(StatesGroup):
    value = State()


class Bc(StatesGroup):
    msg = State()
    confirm = State()


class Book(StatesGroup):
    qty = State()
    fandom = State()
    en = State()
    ru = State()
    username = State()
    confirm = State()


class BProof(StatesGroup):
    wait = State()


class BookPrice(StatesGroup):
    value = State()


class Support(StatesGroup):
    msg = State()


class BtnAdd(StatesGroup):
    label = State()
    value = State()


class BtnEdit(StatesGroup):
    value = State()


class PromoAdd(StatesGroup):
    data = State()


user = Router()
admin = Router()
admin.message.filter(F.from_user.id.in_(ADMINS))
admin.callback_query.filter(F.from_user.id.in_(ADMINS))


# ───────────────────────── ПОЛЬЗОВАТЕЛЬ: МЕНЮ ─────────────────────────
async def menu_content():
    b = InlineKeyboardBuilder()
    kinds = []
    for x in await fetch("SELECT * FROM buttons WHERE active=1 ORDER BY sort, id"):
        if x["kind"] == "url":
            b.button(text=x["label"], url=x["value"])
        else:
            b.button(text=x["label"], callback_data={"buy": "shop", "book": "book", "price": "price", "my": "my",
                                                     "support": "support"}.get(x["kind"], f"info:{x['id']}"))
        kinds.append(x["kind"])
    if kinds:
        top = 2 if kinds[:2] == ["buy", "book"] else 0  # «Купить тапы» и «Поставить бронь» — в одном ряду
        b.adjust(*(([2] if top else []) + [1] * (len(kinds) - top)))
    return await T("welcome"), b.as_markup()


@user.callback_query(F.data.startswith("info:"))
async def info_button(cb: CallbackQuery):
    r = await fetchone("SELECT * FROM buttons WHERE id=?", (int(cb.data.split(":")[1]),))
    if r:
        await edit(cb, r["value"] or "—", kb_of([[("⬅️ В меню", "menu")]]))
    await cb.answer()


@user.message(CommandStart())
@user.message(Command("cancel"))
async def start(m: Message, state: FSMContext):
    await state.clear()
    text, kb = await menu_content()
    await m.answer(text, reply_markup=kb)


@user.callback_query(F.data.in_({"cancel", "menu"}))
async def cancel(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    text, kb = await menu_content()
    await edit(cb, text, kb)
    await cb.answer()


@user.callback_query(F.data == "price")
async def price_list(cb: CallbackQuery):
    out = []
    for p in await fetch("SELECT * FROM products WHERE active=1 ORDER BY sort, id"):
        t = await fetch("SELECT * FROM tiers WHERE product_id=? ORDER BY qty", (p["id"],))
        out.append(f"<b>{esc(p['title'])}</b>\n" + "\n".join(
            f"{r['qty']} — {fmt_price(r['stars'], r['rub'])}" for r in t))
    await edit(cb, "💰 <b>Прайс</b>\n\n" + "\n\n".join(out), kb_of([[("⬅️ В меню", "menu")]]))
    await cb.answer()


def bar(done, total, n=10):
    k = min(n, int(n * done / total)) if total else 0
    return "▰" * k + "▱" * (n - k)


async def voters_text(o):
    """Список участников заказа (кто голосовал в опросе) или None, если пока никого."""
    if not o["poll_id"]:
        return None
    rows = await fetch("SELECT * FROM voters WHERE poll_id=? ORDER BY taps DESC, label", (o["poll_id"],))
    if not rows:
        return None
    lines = [f"{esc(r['label'])} — {r['taps'] or 'тапали'}" for r in rows[:60]]
    more = len(rows) - 60
    return (f"👥 <b>Участники заказа #{o['id']}</b> ({len(rows)})\n\n" + "\n".join(lines)
            + (f"\n… и ещё {more}" if more > 0 else "")
            + f"\n\n🎯 Всего: {sum(r['taps'] for r in rows)}")


@user.callback_query(F.data == "my")
async def my_orders(cb: CallbackQuery):
    rows = await fetch("SELECT * FROM orders WHERE user_id=? ORDER BY id DESC LIMIT 10", (cb.from_user.id,))
    b = InlineKeyboardBuilder()
    for o in rows:
        b.button(text=f"{STATUS.get(o['status'], '•').split()[0]} #{o['id']} · {qty_label(o)} · {o['target_user']}",
                 callback_data=f"mo:{o['id']}")
    bks = await fetch("SELECT * FROM bookings WHERE user_id=? AND status IN ('review','active','done') "
                      "ORDER BY id DESC LIMIT 5", (cb.from_user.id,))
    for k in bks:
        b.button(text=f"🔖 Бронь #{k['id']} · {k['qty'] or '—'} · {k['char_en']}", callback_data=f"mb:{k['id']}")
    b.button(text="⬅️ В меню", callback_data="menu")
    b.adjust(1)
    await edit(cb, "📦 <b>Ваши заказы и брони</b>\nНажмите, чтобы увидеть статус и промежуточный результат:"
               if (rows or bks) else "У вас пока нет заказов.", b.as_markup())
    await cb.answer()


@user.callback_query(F.data.startswith("mo:"))
async def my_order_card(cb: CallbackQuery):
    oid = int(cb.data.split(":")[1])
    o = await fetchone("SELECT * FROM orders WHERE id=? AND user_id=?", (oid, cb.from_user.id))
    if not o:
        return await cb.answer("Заказ не найден", show_alert=True)
    st = o["status"]
    text = (f"📦 <b>Заказ #{oid}</b> · {STATUS.get(st, st)}\n\n"
            f"Количество: {qty_label(o)}\n🔗 {esc(o['post_link'])}\n💬 {esc(o['target_user'])}\n"
            f"💰 {fmt_price(o['price_stars'], o['price_rub'])}")
    rows = []
    if st in ("voting", "voted", "done"):
        taps = o["votes"]
        pct = min(100, int(taps * 100 / o["qty"])) if o["qty"] else 0
        text += f"\n\n📊 <b>Промежуточный результат:</b> оттапано {taps} из {o['qty']} ({pct}%)\n{bar(taps, o['qty'])}"
        if st == "voting":
            text += f"\n🕒 Обновлено: {datetime.now().strftime('%H:%M:%S')}"
            rows.append([("🔄 Обновить результат", f"mo:{oid}")])
        rows.append([("👥 Кто выполняет", f"vt:{oid}")])
    elif st == "awaiting_payment":
        rows.append([("💳 К оплате", f"pay_menu:{oid}")])
    rows.append([("⬅️ К заказам", "my")])
    await edit(cb, text, kb_of(rows))
    await cb.answer()


@user.callback_query(F.data.startswith("pay_menu:"))
async def pay_menu(cb: CallbackQuery):
    o = await fetchone("SELECT * FROM orders WHERE id=? AND user_id=? AND status='awaiting_payment'",
                       (int(cb.data.split(":")[1]), cb.from_user.id))
    if not o:
        return await cb.answer("Заказ недоступен для оплаты", show_alert=True)
    await offer_payment(o)
    await cb.answer()


@user.callback_query(F.data.startswith("vt:"))
async def my_voters(cb: CallbackQuery):
    oid = int(cb.data.split(":")[1])
    o = await fetchone("SELECT * FROM orders WHERE id=? AND user_id=?", (oid, cb.from_user.id))
    if not o:
        return await cb.answer("Заказ не найден", show_alert=True)
    await edit(cb, await voters_text(o) or "Пока никто не участвовал.", kb_of([[("⬅️ К заказу", f"mo:{oid}")]]))
    await cb.answer()


# ── Обратная связь с админами
_last_fb = {}


@user.callback_query(F.data == "support")
async def support_start(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await state.set_state(Support.msg)
    await cb.message.answer(await T("support_ask"), reply_markup=CANCEL_KB)
    await cb.answer()


@user.message(Support.msg)
async def support_msg(m: Message, state: FSMContext):
    now = time.time()
    if now - _last_fb.get(m.from_user.id, 0) < 8:
        return await m.answer("Не так быстро 🙂 Подождите несколько секунд.")
    _last_fb[m.from_user.id] = now
    last = await fetchone("SELECT id, status FROM orders WHERE user_id=? ORDER BY id DESC LIMIT 1", (m.from_user.id,))
    tid = await run("INSERT INTO tickets(user_id, created_at) VALUES(?,?)", (m.from_user.id, int(now)))
    card = (f"💬 <b>Обращение #{tid}</b>\n👤 {label_of(m.from_user)}\n"
            + (f"📦 Последний заказ: #{last['id']} · {STATUS.get(last['status'], last['status'])}\n" if last else "")
            + "\n↩️ Ответьте (Reply) на это сообщение — ответ уйдёт клиенту.")
    chat, topic = await target("sup")
    sent = await bot.send_message(chat, card, message_thread_id=topic)
    copy = await bot.copy_message(chat, m.chat.id, m.message_id, message_thread_id=topic,
                                  reply_parameters=ReplyParameters(message_id=sent.message_id))
    for mid in (sent.message_id, copy.message_id):
        await run("INSERT OR REPLACE INTO ticket_msgs VALUES(?,?,?)", (chat, mid, tid))
    await state.clear()
    await m.answer(await T("support_sent"), reply_markup=kb_of([[("⬅️ В меню", "menu")]]))


# ── Бронь тапов на персонажа
BSTATUS = {"awaiting_payment": "💳 не оплачена", "review": "🕓 проверка оплаты", "active": "🟢 активна",
           "done": "✅ выполнена", "cancelled": "🚫 отменена"}
LAT = re.compile(r"[A-Za-z]")
CYR = re.compile(r"[А-Яа-яЁё]")


def ok_en(t):
    return 2 <= len(t) <= 80 and bool(LAT.search(t)) and not CYR.search(t)


def ok_ru(t):
    return 2 <= len(t) <= 80 and bool(CYR.search(t))


async def book_product():
    """Основной прайс — первая активная позиция (если нет активных — первая вообще)."""
    return (await fetchone("SELECT * FROM products WHERE active=1 ORDER BY sort, id LIMIT 1")
            or await fetchone("SELECT * FROM products ORDER BY sort, id LIMIT 1"))


async def book_price(qty):
    """(звёзды, рубли) строго по основному прайсу или None."""
    p = await book_product()
    return await exact_price(p["id"], qty) if p else None


async def book_picker():
    p = await book_product()
    lo = int(await get_setting("book_min") or 100)
    hi = int(await get_setting("book_max") or 1000)
    step = p["step"] if p else 20
    b = InlineKeyboardBuilder()
    presets = [q for q in range(lo, hi + 1, 100) if q % step == 0]
    for q in presets:
        b.button(text=str(q), callback_data=f"bq:{q}")
    b.button(text="✍️ Ввести своё количество", callback_data="bq:custom")
    b.button(text="❌ Отмена", callback_data="cancel")
    n = len(presets)
    b.adjust(*([3] * (n // 3) + ([n % 3] if n % 3 else []) + [1, 1]))
    return f"{await T('book_qty')}\n(кратно {step}, от {lo} до {hi})", b.as_markup(), lo, hi, step


def bk_text(b, buyer, head, footer=""):
    return (f"{head}\n\n🧾 Бронь #{b['id']}\n👤 {buyer}\n"
            f"💬 Юзернейм: @{esc(b['username'] or '—')}\n"
            f"📦 Количество: {b['qty'] or '—'} тапов\n"
            f"🎭 Фандом: {esc(b['fandom'])}\n"
            f"👤 Персонаж (EN): {esc(b['char_en'])}\n"
            f"👤 Персонаж (RU): {esc(b['char_ru'])}\n"
            f"💰 Цена: {fmt_price(b['price_stars'], b['price_rub'])}"
            + (f"\n\n{footer}" if footer else ""))


def bwork_kb(bid):
    return kb_of([[("✅ Бронь выполнена", f"bdone:{bid}")], [("🚫 Отменить бронь", f"bcancel:{bid}")]])


@user.callback_query(F.data == "book")
async def book_start(cb: CallbackQuery, state: FSMContext):
    if await get_setting("intake") == "0":
        await cb.message.answer(await T("paused"))
        return await cb.answer()
    await state.clear()
    await state.update_data(edit=False)
    await state.set_state(Book.qty)
    text, kb, *_ = await book_picker()
    await cb.message.answer(text, reply_markup=kb)
    await cb.answer()


@user.callback_query(Book.qty, F.data.startswith("bq:"))
async def book_qty_pick(cb: CallbackQuery, state: FSMContext):
    v = cb.data[3:]
    await cb.answer()
    if v == "custom":
        _, _, lo, hi, step = await book_picker()
        return await cb.message.answer(f"Введите количество тапов (кратно {step}, от {lo} до {hi}):",
                                       reply_markup=CANCEL_KB)
    await apply_book_qty(cb.message, state, int(v))


@user.message(Book.qty, F.text)
async def book_qty_text(m: Message, state: FSMContext):
    _, _, lo, hi, step = await book_picker()
    q = to_int(m.text)
    if q is None or not lo <= q <= hi or q % step:
        return await m.answer(f"Введите число, кратное {step}, от {lo} до {hi}.")
    await apply_book_qty(m, state, q)


async def apply_book_qty(m: Message, state: FSMContext, qty):
    if not await book_price(qty):
        return await m.answer("Для этого количества нет цены в основном прайсе, выберите другое.")
    await state.update_data(qty=qty)
    await book_next(m, state, Book.fandom, await T("book_fandom"))


async def book_next(m: Message, state: FSMContext, nxt, prompt):
    if (await state.get_data()).get("edit"):
        return await book_summary(m, state)
    await state.set_state(nxt)
    await m.answer(prompt, reply_markup=CANCEL_KB)


@user.message(Book.fandom, F.text)
async def book_fandom(m: Message, state: FSMContext):
    t = m.text.strip()
    if not ok_en(t):
        return await m.answer("Название фандома нужно написать <b>на английском языке</b> (латиницей). Попробуйте ещё раз:")
    await state.update_data(fandom=t)
    await book_next(m, state, Book.en, await T("book_en"))


@user.message(Book.en, F.text)
async def book_en(m: Message, state: FSMContext):
    t = m.text.strip()
    if not ok_en(t):
        return await m.answer("Имя персонажа здесь нужно написать <b>на английском языке</b> (латиницей). Попробуйте ещё раз:")
    await state.update_data(en=t)
    await book_next(m, state, Book.ru, await T("book_ru"))


@user.message(Book.ru, F.text)
async def book_ru(m: Message, state: FSMContext):
    t = m.text.strip()
    if not ok_ru(t):
        return await m.answer("Имя персонажа здесь нужно написать <b>на русском языке</b>. Попробуйте ещё раз:")
    await state.update_data(ru=t)
    await book_next(m, state, Book.username, await T("book_user"))


@user.message(Book.username, F.text)
async def book_username(m: Message, state: FSMContext):
    t = m.text.strip().removeprefix("https://t.me/").lstrip("@")
    if not re.fullmatch(r"[A-Za-z0-9_]{4,32}", t):
        return await m.answer("Юзернейм должен выглядеть как @username (латиница, цифры, _).")
    await state.update_data(username=t)
    await book_summary(m, state)


async def book_summary(m: Message, state: FSMContext):
    d = await state.get_data()
    stars, rub = await book_price(d["qty"])
    await state.update_data(edit=False)
    await state.set_state(Book.confirm)
    await m.answer("Проверьте данные брони:\n\n"
                   f"📦 Количество: {d['qty']} тапов\n"
                   f"🎭 Фандом: {esc(d['fandom'])}\n"
                   f"👤 Персонаж (EN): {esc(d['en'])}\n"
                   f"👤 Персонаж (RU): {esc(d['ru'])}\n"
                   f"💬 Юзернейм: @{esc(d['username'])}\n"
                   f"💰 Стоимость брони: {fmt_price(stars, rub)}\n\n"
                   "⚠️ Бронь оплачивается сразу — без оплаты она не будет оформлена.\n\nВсё верно?",
                   reply_markup=kb_of([[("✅ Всё верно", "bk_ok")],
                                       [("✏️ Изменить", "bk_edit"), ("❌ Отменить", "cancel")]]))


@user.callback_query(Book.confirm, F.data == "bk_edit")
async def book_edit_menu(cb: CallbackQuery):
    await edit(cb, "Что изменить?", kb_of([[("📦 Количество", "bke:q")], [("🎭 Фандом", "bke:f")],
                                           [("👤 Персонаж (EN)", "bke:en"), ("👤 Персонаж (RU)", "bke:ru")],
                                           [("💬 Юзернейм", "bke:u")],
                                           [("⬅️ Назад", "bke:back")]]))
    await cb.answer()


@user.callback_query(Book.confirm, F.data.startswith("bke:"))
async def book_edit_field(cb: CallbackQuery, state: FSMContext):
    what = cb.data[4:]
    await cb.answer()
    if what == "back":
        return await book_summary(cb.message, state)
    if what == "q":
        await state.update_data(edit=True)
        await state.set_state(Book.qty)
        text, kb, *_ = await book_picker()
        return await cb.message.answer(text, reply_markup=kb)
    st, prompt = {"f": (Book.fandom, "🎭 Введите название фандома на английском:"),
                  "en": (Book.en, "👤 Введите имя персонажа на английском:"),
                  "ru": (Book.ru, "👤 Введите имя персонажа на русском:"),
                  "u": (Book.username, "💬 Введите юзернейм (например, @username):")}[what]
    await state.update_data(edit=True)
    await state.set_state(st)
    await cb.message.answer(prompt, reply_markup=CANCEL_KB)


@user.callback_query(Book.confirm, F.data == "bk_ok")
async def book_confirm(cb: CallbackQuery, state: FSMContext):
    if await get_setting("intake") == "0":
        await state.clear()
        await cb.message.answer(await T("paused"))
        return await cb.answer()
    d = await state.get_data()
    price = await book_price(d["qty"])
    if not price:
        return await cb.answer("Для этого количества цена не задана — выберите другое", show_alert=True)
    stars, rub = price
    if await get_setting("book_unique") == "1" and await fetchone(
            "SELECT 1 FROM bookings WHERE status IN ('review','active') AND lower(fandom)=? AND lower(char_en)=?",
            (d["fandom"].lower(), d["en"].lower())):
        await state.clear()
        await cb.message.answer("😔 На этого персонажа уже есть бронь.")
        return await cb.answer()
    # неоплаченные черновики старше суток чистим
    await db.execute("DELETE FROM bookings WHERE status='awaiting_payment' AND created_at<?", (int(time.time()) - 86400,))
    bid = await run("""INSERT INTO bookings(user_id,qty,fandom,char_en,char_ru,username,price_rub,price_stars,status,created_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?)""", (cb.from_user.id, d["qty"], d["fandom"], d["en"], d["ru"],
                                                       d["username"], rub, stars, "awaiting_payment", int(time.time())))
    await state.clear()
    await cb.message.edit_reply_markup(reply_markup=None)
    rows = []
    if rub:
        rows.append([(f"💳 Оплатить {rub}₽ (реквизиты)", f"bpay:rub:{bid}")])
    if stars:
        rows.append([(f"⭐ Оплатить {stars} Stars", f"bpay:xtr:{bid}")])
    rows.append([("❌ Отмена", "menu")])
    await cb.message.answer(f"🔖 <b>Бронь #{bid}</b> оформляется только после оплаты — оплатите сразу.\n"
                            f"💰 К оплате: {fmt_price(stars, rub)}\n\nВыберите способ оплаты:", reply_markup=kb_of(rows))
    await cb.answer()


@user.callback_query(F.data.startswith("bpay:"))
async def book_pay(cb: CallbackQuery, state: FSMContext):
    _, method, bid = cb.data.split(":")
    b = await fetchone("SELECT * FROM bookings WHERE id=? AND user_id=? AND status='awaiting_payment'",
                       (int(bid), cb.from_user.id))
    if not b:
        return await cb.answer("Бронь недоступна для оплаты", show_alert=True)
    if method == "rub" and b["price_rub"]:
        await run("UPDATE bookings SET currency='rub' WHERE id=?", (b["id"],))
        await state.set_state(BProof.wait)
        await state.update_data(bid=b["id"])
        await cb.message.answer(f"💳 <b>Оплата брони #{b['id']}</b>\n\nСумма: <b>{b['price_rub']}₽</b>\n\n"
                                f"{await T('requisites')}\n\nПосле оплаты отправьте сюда <b>скриншот</b> перевода.",
                                reply_markup=CANCEL_KB)
    elif method == "xtr" and b["price_stars"]:
        await cb.message.answer_invoice(
            title=f"Бронь #{b['id']}", description=f"{b['fandom']} — {b['char_en']}",
            payload=f"booking:{b['id']}", currency="XTR", provider_token="",
            prices=[LabeledPrice(label=f"Бронь #{b['id']}", amount=b["price_stars"])])
    else:
        return await cb.answer("Этот способ оплаты недоступен", show_alert=True)
    await cb.answer()


@user.message(BProof.wait, F.photo | F.document)
async def book_proof(m: Message, state: FSMContext):
    bid = (await state.get_data())["bid"]
    b = await fetchone("SELECT * FROM bookings WHERE id=? AND status='awaiting_payment'", (bid,))
    if not b:
        await state.clear()
        return await m.answer("Эта бронь уже обработана.")
    caption = bk_text(b, label_of(m.from_user), "🔖 Бронь — скриншот оплаты")
    kb = kb_of([[("✅ Одобрить", f"bpay_ok:{bid}"), ("❌ Отклонить", f"bpay_no:{bid}")]])
    chat, topic = await target("bkc")
    if m.photo:
        sent = await bot.send_photo(chat, m.photo[-1].file_id, caption=caption, message_thread_id=topic, reply_markup=kb)
    else:
        sent = await bot.send_document(chat, m.document.file_id, caption=caption, message_thread_id=topic, reply_markup=kb)
    await run("UPDATE bookings SET status='review', adm_chat=?, adm_msg=? WHERE id=?", (sent.chat.id, sent.message_id, bid))
    await state.clear()
    await m.answer(await T("proof_sent"))


@user.message(BProof.wait)
async def book_proof_wrong(m: Message):
    await m.answer("Пришлите, пожалуйста, скриншот (фото) оплаты.", reply_markup=CANCEL_KB)


async def activate_booking(bid):
    """Бронь оплачена: сообщаем клиенту и публикуем в теме «Брони тапов»."""
    b = await fetchone("SELECT * FROM bookings WHERE id=?", (bid,))
    try:
        await bot.send_message(b["user_id"], await T("book_paid", id=bid) +
                               f"\n\n📦 {b['qty']} тапов\n🎭 {esc(b['fandom'])}\n"
                               f"👤 {esc(b['char_en'])} / {esc(b['char_ru'])}\n"
                               f"💬 @{esc(b['username'] or '—')}",
                               reply_markup=kb_of([[("📦 Мои заказы и брони", "my")]]))
    except (TelegramForbiddenError, TelegramBadRequest):
        pass
    # после подтверждения — сообщение в тему «Брони тапов»
    await send_to("bkt", f"🔖 <b>Бронь #{bid} — {b['qty']} тапов</b>\n\n"
                         f"💬 Юзернейм: @{esc(b['username'] or '—')}\n"
                         f"👤 Персонаж: {esc(b['char_en'])} / {esc(b['char_ru'])}\n"
                         f"🎭 Фандом: {esc(b['fandom'])}\n"
                         f"💰 {fmt_price(b['price_stars'], b['price_rub'])}")


async def booking_paid_stars(m: Message):
    sp = m.successful_payment
    bid = int(sp.invoice_payload.split(":")[1])
    await run("UPDATE bookings SET status='active', currency='xtr', charge_id=?, paid_at=? WHERE id=?",
              (sp.telegram_payment_charge_id, int(time.time()), bid))
    b = await fetchone("SELECT * FROM bookings WHERE id=?", (bid,))
    sent = await send_to("bkc", bk_text(b, label_of(m.from_user), "⭐ Бронь — оплата Stars", "✅ Оплачено автоматически"),
                         reply_markup=bwork_kb(bid))
    await run("UPDATE bookings SET adm_chat=?, adm_msg=? WHERE id=?", (sent.chat.id, sent.message_id, bid))
    await activate_booking(bid)


@user.callback_query(F.data.startswith("mb:"))
async def my_booking(cb: CallbackQuery):
    b = await fetchone("SELECT * FROM bookings WHERE id=? AND user_id=?", (int(cb.data.split(":")[1]), cb.from_user.id))
    if not b:
        return await cb.answer("Бронь не найдена", show_alert=True)
    await edit(cb, f"🔖 <b>Бронь #{b['id']}</b> · {BSTATUS.get(b['status'], b['status'])}\n\n"
                   f"📦 Количество: {b['qty'] or '—'} тапов\n🎭 Фандом: {esc(b['fandom'])}\n"
                   f"👤 Персонаж (EN): {esc(b['char_en'])}\n"
                   f"👤 Персонаж (RU): {esc(b['char_ru'])}\n"
                   f"💬 @{esc(b['username'] or '—')}\n"
                   f"💰 {fmt_price(b['price_stars'], b['price_rub'])}",
               kb_of([[("⬅️ К заказам", "my")]]))
    await cb.answer()


# ───────────────────────── ПОЛЬЗОВАТЕЛЬ: ОФОРМЛЕНИЕ ─────────────────────────
def qty_text(p):
    return f"Выберите количество (кратно {p['step']}, от {p['min_qty']} до {p['max_qty']}):"


def qty_kb(p):
    b = InlineKeyboardBuilder()
    presets = [q for q in range(100, p["max_qty"] + 1, 100) if q >= p["min_qty"]]
    for q in presets:
        b.button(text=str(q), callback_data=f"q:{q}")
    b.button(text="Максимальное количество", callback_data="q:max")
    b.button(text="✍️ Ввести своё количество", callback_data="q:custom")
    b.button(text="❌ Отмена", callback_data="cancel")
    n = len(presets)
    b.adjust(*([3] * (n // 3) + ([n % 3] if n % 3 else []) + [1, 1, 1]))
    return b.as_markup()


async def start_buy(cb: CallbackQuery, state: FSMContext, pid):
    if await get_setting("intake") == "0":
        await cb.message.answer(await T("paused"))
        return await cb.answer()
    p = await fetchone("SELECT * FROM products WHERE id=? AND active=1", (pid,))
    if not p:
        return await cb.answer("Эта позиция недоступна", show_alert=True)
    await state.clear()
    await state.update_data(pid=p["id"], promo=None, percent=0, edit=False)
    await state.set_state(Order.qty)
    await cb.message.answer(qty_text(p), reply_markup=qty_kb(p))
    await cb.answer()


@user.callback_query(F.data.startswith("buy:"))
async def buy(cb: CallbackQuery, state: FSMContext):
    await start_buy(cb, state, int(cb.data.split(":")[1]))


@user.callback_query(F.data == "shop")
async def shop(cb: CallbackQuery, state: FSMContext):
    prods = await fetch("SELECT * FROM products WHERE active=1 ORDER BY sort, id")
    if not prods:
        return await cb.answer("Пока нет доступных позиций", show_alert=True)
    if len(prods) == 1:
        return await start_buy(cb, state, prods[0]["id"])
    b = InlineKeyboardBuilder()
    for p in prods:
        b.button(text=p["title"], callback_data=f"buy:{p['id']}")
    b.button(text="⬅️ В меню", callback_data="menu")
    b.adjust(1)
    await edit(cb, "🛒 Что хотите заказать?", b.as_markup())
    await cb.answer()


@user.callback_query(Order.qty, F.data.startswith("q:"))
async def qty_pick(cb: CallbackQuery, state: FSMContext):
    d = await state.get_data()
    p = await fetchone("SELECT * FROM products WHERE id=?", (d["pid"],))
    v = cb.data[2:]
    await cb.answer()
    if v == "custom":
        return await cb.message.answer(
            f"Введите количество (кратно {p['step']}, от {p['min_qty']} до {p['max_qty']}):",
            reply_markup=CANCEL_KB)
    if v == "max":
        return await apply_qty(cb.message, state, p["max_qty"], 1)
    await apply_qty(cb.message, state, int(v), 0)


@user.message(Order.qty, F.text)
async def qty_text_input(m: Message, state: FSMContext):
    p = await fetchone("SELECT * FROM products WHERE id=?", ((await state.get_data())["pid"],))
    q = to_int(m.text)
    if q is None or not p["min_qty"] <= q <= p["max_qty"] or q % p["step"]:
        return await m.answer(f"Введите число, кратное {p['step']}, от {p['min_qty']} до {p['max_qty']}.")
    await apply_qty(m, state, q, 0)


async def apply_qty(m: Message, state: FSMContext, qty, is_max):
    d = await state.get_data()
    if not await price_for(d["pid"], qty):
        return await m.answer("Для этого количества цена не задана, выберите другое.")
    await state.update_data(qty=qty, is_max=is_max)
    if d.get("edit"):
        return await send_summary(m, state)
    await state.set_state(Order.link)
    await m.answer(await T("ask_link"), reply_markup=CANCEL_KB)


@user.message(Order.link, F.text)
async def get_link(m: Message, state: FSMContext):
    if not re.match(r"^(https?://|t\.me/)\S+$", m.text.strip()):
        return await m.answer("Это не похоже на ссылку. Отправьте ссылку на пост (https://t.me/...)")
    await state.update_data(link=m.text.strip())
    if (await state.get_data()).get("edit"):
        return await send_summary(m, state)
    await state.set_state(Order.target)
    await m.answer(await T("ask_target"), reply_markup=CANCEL_KB)


@user.message(Order.target, F.text)
async def get_target(m: Message, state: FSMContext):
    t = m.text.strip().removeprefix("https://t.me/").lstrip("@")
    if not re.fullmatch(r"[A-Za-z0-9_]{4,32}", t):
        return await m.answer("Юзернейм должен выглядеть как @username (латиница, цифры, _).")
    await state.update_data(target="@" + t)
    await send_summary(m, state)


async def send_summary(m: Message, state: FSMContext):
    d = await state.get_data()
    stars, rub = await price_for(d["pid"], d["qty"])
    stars, rub = discount(stars, rub, d.get("percent", 0))
    await state.update_data(edit=False)
    await state.set_state(Order.confirm)
    text = ("Проверьте данные:\n\n"
            f"📦 Количество: {d['qty']}{' (макс)' if d.get('is_max') else ''}\n"
            f"🔗 Ссылка: {esc(d['link'])}\n"
            f"💬 Юзернейм: {esc(d['target'])}\n"
            + (f"🎟 Промокод: {esc(d['promo'])} (−{d['percent']}%)\n" if d.get("promo") else "")
            + f"💰 Цена: {fmt_price(stars, rub)}\n\nВсё верно?")
    await m.answer(text, link_preview_options=LinkPreviewOptions(is_disabled=False),
                   reply_markup=kb_of([[("✅ Всё верно", "o_ok")],
                                       [("✏️ Изменить", "o_edit"), ("❌ Отменить", "cancel")]]))


@user.callback_query(Order.confirm, F.data == "o_edit")
async def o_edit(cb: CallbackQuery):
    await edit(cb, "Что изменить?", kb_of([
        [("📦 Количество", "e:qty"), ("🔗 Ссылка", "e:link")],
        [("💬 Юзернейм", "e:target"), ("🎟 Промокод", "e:promo")],
        [("⬅️ Назад", "e:back")]]))
    await cb.answer()


@user.callback_query(Order.confirm, F.data.startswith("e:"))
async def o_edit_field(cb: CallbackQuery, state: FSMContext):
    what = cb.data[2:]
    await cb.answer()
    if what == "back":
        return await send_summary(cb.message, state)
    await state.update_data(edit=True)
    if what == "qty":
        p = await fetchone("SELECT * FROM products WHERE id=?", ((await state.get_data())["pid"],))
        await state.set_state(Order.qty)
        return await cb.message.answer(qty_text(p), reply_markup=qty_kb(p))
    prompt = {"link": ("🔗 Отправьте новую ссылку на пост:", Order.link),
              "target": ("💬 Отправьте новый юзернейм:", Order.target),
              "promo": ("🎟 Введите промокод:", Order.promo)}[what]
    await state.set_state(prompt[1])
    await cb.message.answer(prompt[0], reply_markup=CANCEL_KB)


@user.message(Order.promo, F.text)
async def get_promo(m: Message, state: FSMContext):
    code = m.text.strip().upper()
    pr = await fetchone("SELECT * FROM promos WHERE code=? AND uses>0", (code,))
    if not pr:
        await m.answer("Промокод не найден или закончился.")
    else:
        await state.update_data(promo=code, percent=pr["percent"])
        await m.answer(f"✅ Промокод применён: −{pr['percent']}%")
    await send_summary(m, state)


@user.callback_query(Order.confirm, F.data == "o_ok")
async def confirm_order(cb: CallbackQuery, state: FSMContext):
    if await get_setting("intake") == "0":
        await state.clear()
        await cb.message.answer(await T("paused"))
        return await cb.answer()
    d = await state.get_data()
    p = await fetchone("SELECT * FROM products WHERE id=? AND active=1", (d["pid"],))
    price = await price_for(d["pid"], d["qty"]) if p else None
    if not price:
        await state.clear()
        return await cb.answer("Позиция больше недоступна", show_alert=True)
    percent, promo = d.get("percent", 0), d.get("promo")
    if promo:
        c = await db.execute("UPDATE promos SET uses=uses-1 WHERE code=? AND uses>0", (promo,))
        await db.commit()
        if c.rowcount == 0:
            percent, promo = 0, None
    stars, rub = discount(*price, percent)
    pre = await get_setting("pre_approve") == "1"
    oid = await run("""INSERT INTO orders(user_id,product_id,title,qty,is_max,price_rub,price_stars,
        post_link,target_user,promo,status,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (cb.from_user.id, p["id"], p["title"], d["qty"], d.get("is_max", 0), rub, stars,
                     d["link"], d["target"], promo, "pending_approval" if pre else "awaiting_payment",
                     int(time.time())))
    await state.clear()
    o = await fetchone("SELECT * FROM orders WHERE id=?", (oid,))
    await cb.message.edit_reply_markup(reply_markup=None)
    if pre:
        await cb.message.answer(f"📨 Заказ <b>#{oid}</b> отправлен на подтверждение. "
                                "Как только его примут, я пришлю реквизиты для оплаты.")
        await send_to("pay", adm_text(o, label_of(cb.from_user), "🆕 Новый заказ на подтверждение"),
                      reply_markup=kb_of([[("✅ Принять", f"ord_ok:{oid}"), ("❌ Отклонить", f"ord_no:{oid}")]]))
    else:
        await offer_payment(o)
    await cb.answer()


async def offer_payment(o):
    rows = []
    if o["price_rub"]:
        rows.append([(f"💳 Оплатить {o['price_rub']}₽ (реквизиты)", f"pay:rub:{o['id']}")])
    if o["price_stars"]:
        rows.append([(f"⭐ Оплатить {o['price_stars']} Stars", f"pay:xtr:{o['id']}")])
    rows.append([("❌ Отмена", "menu")])
    try:
        await bot.send_message(o["user_id"], f"✅ Заказ <b>#{o['id']}</b> принят.\n"
                                             f"💰 К оплате: {fmt_price(o['price_stars'], o['price_rub'])}\n\n"
                                             "Выберите способ оплаты:", reply_markup=kb_of(rows))
    except (TelegramForbiddenError, TelegramBadRequest):
        pass


@user.callback_query(F.data.startswith("pay:"))
async def pay(cb: CallbackQuery, state: FSMContext):
    _, method, oid = cb.data.split(":")
    o = await fetchone("SELECT * FROM orders WHERE id=? AND user_id=? AND status='awaiting_payment'",
                       (int(oid), cb.from_user.id))
    if not o:
        return await cb.answer("Заказ недоступен для оплаты", show_alert=True)
    if method == "rub":
        await run("UPDATE orders SET currency='rub' WHERE id=?", (o["id"],))
        await state.set_state(Proof.wait)
        await state.update_data(oid=o["id"])
        await cb.message.answer(
            f"💳 <b>Оплата заказа #{o['id']}</b>\n\nСумма: <b>{o['price_rub']}₽</b>\n\n"
            f"{await T('requisites')}\n\n"
            "После оплаты отправьте сюда <b>скриншот</b> перевода.", reply_markup=CANCEL_KB)
    else:
        await cb.message.answer_invoice(
            title=f"Заказ #{o['id']}", description=f"Количество: {qty_label(o)}",
            payload=f"order:{o['id']}", currency="XTR", provider_token="",
            prices=[LabeledPrice(label=f"Заказ #{o['id']}", amount=o["price_stars"])])
    await cb.answer()


@user.message(Proof.wait, F.photo | F.document)
async def got_proof(m: Message, state: FSMContext):
    oid = (await state.get_data())["oid"]
    o = await fetchone("SELECT * FROM orders WHERE id=? AND status='awaiting_payment'", (oid,))
    if not o:
        await state.clear()
        return await m.answer("Этот заказ уже обработан.")
    caption = adm_text(o, label_of(m.from_user), "💳 Скриншот оплаты")
    kb = kb_of([[("✅ Одобрить", f"pay_ok:{oid}"), ("❌ Отклонить", f"pay_no:{oid}")]])
    chat, topic = await target("pay")
    if m.photo:
        sent = await bot.send_photo(chat, m.photo[-1].file_id, caption=caption,
                                    message_thread_id=topic, reply_markup=kb)
    else:
        sent = await bot.send_document(chat, m.document.file_id, caption=caption,
                                       message_thread_id=topic, reply_markup=kb)
    await run("UPDATE orders SET status='review', adm_chat=?, adm_msg=? WHERE id=?",
              (sent.chat.id, sent.message_id, oid))
    await state.clear()
    await m.answer(await T("proof_sent"))


@user.message(Proof.wait)
async def proof_wrong(m: Message):
    await m.answer("Пришлите, пожалуйста, скриншот (фото) оплаты.", reply_markup=CANCEL_KB)


# ───────────────────────── ОПЛАТА STARS ─────────────────────────
@user.pre_checkout_query()
async def pre_checkout(q: PreCheckoutQuery):
    try:
        kind, raw = q.invoice_payload.split(":")
        oid = int(raw)
    except ValueError:
        return await q.answer(ok=False, error_message="Ошибка заказа")
    table = "bookings" if kind == "booking" else "orders"
    o = await fetchone(f"SELECT status FROM {table} WHERE id=?", (oid,))
    ok = bool(o and o["status"] == "awaiting_payment")
    await q.answer(ok=ok, error_message=None if ok else "Заказ уже оплачен или недоступен")


@user.message(F.successful_payment)
async def stars_paid(m: Message):
    sp = m.successful_payment
    if sp.invoice_payload.startswith("booking:"):
        return await booking_paid_stars(m)
    oid = int(sp.invoice_payload.split(":")[1])
    await run("UPDATE orders SET currency='xtr', charge_id=?, paid_at=? WHERE id=?",
              (sp.telegram_payment_charge_id, int(time.time()), oid))
    o = await fetchone("SELECT * FROM orders WHERE id=?", (oid,))
    await m.answer(await T("pay_ok", id=oid), reply_markup=kb_of([[("📊 Промежуточный результат", f"mo:{oid}")]]))
    sent = await send_to("pay",
                         adm_text(o, label_of(m.from_user), "⭐ Оплата Stars", "✅ Оплачено автоматически"),
                         reply_markup=work_kb(oid))
    await run("UPDATE orders SET adm_chat=?, adm_msg=? WHERE id=?", (sent.chat.id, sent.message_id, oid))
    await start_voting(oid)


# ───────────────────────── ОПРОС ─────────────────────────
async def start_voting(oid):
    o = await fetchone("SELECT * FROM orders WHERE id=?", (oid,))
    await send_to("ord",
                  f"🔗 {esc(o['post_link'])}\n💬 {esc(o['target_user'])}\n📦 {o['qty']}")
    chat, topic = await target("ord")
    poll = await bot.send_poll(chat, POLL_Q, POLL_OPTS, is_anonymous=False, message_thread_id=topic)
    await run("UPDATE orders SET status='voting', poll_id=?, poll_chat=?, poll_msg=? WHERE id=?",
              (poll.poll.id, chat, poll.message_id, oid))


def taps_of(poll: Poll):
    return sum(int(o.text) * o.voter_count for o in poll.options if o.text.isdigit())


@user.poll()
async def on_poll(poll: Poll):
    o = await fetchone("SELECT * FROM orders WHERE poll_id=? AND status='voting'", (poll.id,))
    if not o:
        return
    taps = taps_of(poll)
    if taps != o["votes"]:
        await run("UPDATE orders SET votes=? WHERE id=?", (taps, o["id"]))
    if not o["is_max"] and taps >= o["qty"]:
        if await finish_poll(o["id"]) and await get_setting("auto_done") == "1":
            await complete_order(o["id"])


async def finish_poll(oid):
    """Закрывает опрос и пишет итог в обе группы. True, если закрыт именно сейчас."""
    c = await db.execute("UPDATE orders SET status='voted' WHERE id=? AND status='voting'", (oid,))
    await db.commit()
    if c.rowcount == 0:
        return False
    o = await fetchone("SELECT * FROM orders WHERE id=?", (oid,))
    taps = o["votes"]
    try:
        taps = taps_of(await bot.stop_poll(o["poll_chat"], o["poll_msg"]))
    except TelegramBadRequest:
        pass
    await run("UPDATE orders SET votes=? WHERE id=?", (taps, oid))
    text = (f"✅ Заказ #{oid} выполнен: оттапано {taps} из {o['qty']}" if taps >= o["qty"] or o["is_max"]
            else f"🔒 Заказ #{oid}: опрос закрыт, оттапано {taps} из {o['qty']}")
    await send_to("ord", text, reply_to=o["poll_msg"])
    await send_to("pay", text, reply_to=o["adm_msg"])
    if o["adm_chat"] and o["adm_msg"]:
        try:
            await bot.edit_message_reply_markup(o["adm_chat"], o["adm_msg"], reply_markup=work_kb(oid, close=False))
        except TelegramBadRequest:
            pass
    return True


@user.poll_answer()
async def on_poll_answer(a: PollAnswer):
    """Запоминаем, кто и сколько тапнул (опрос не анонимный)."""
    o = await fetchone("SELECT id FROM orders WHERE poll_id=?", (a.poll_id,))
    if not o or not a.user:
        return
    if not a.option_ids:
        return await run("DELETE FROM voters WHERE poll_id=? AND user_id=?", (a.poll_id, a.user.id))
    i = a.option_ids[0]
    txt = POLL_OPTS[i] if i < len(POLL_OPTS) else ""
    label = f"@{a.user.username}" if a.user.username else a.user.full_name
    await run("INSERT OR REPLACE INTO voters VALUES(?,?,?,?,?)",
              (a.poll_id, a.user.id, label, int(txt) if txt.isdigit() else 0, i))


async def complete_order(oid):
    """Закрывает заказ и сам присылает клиенту: итог, опрос с результатами, участников, просьбу об отзыве."""
    o = await fetchone("SELECT * FROM orders WHERE id=?", (oid,))
    if not o or o["status"] not in ("voting", "voted"):
        return False
    if o["status"] == "voting":
        await finish_poll(oid)
        o = await fetchone("SELECT * FROM orders WHERE id=?", (oid,))
    await run("UPDATE orders SET status='done', done_at=? WHERE id=?", (int(time.time()), oid))
    if o["adm_chat"] and o["adm_msg"]:
        try:
            await bot.edit_message_reply_markup(o["adm_chat"], o["adm_msg"], reply_markup=None)
        except TelegramBadRequest:
            pass
    uid = o["user_id"]
    try:
        await bot.send_message(uid, await T("done", id=oid), reply_markup=kb_of([[("🛒 Заказать ещё", "menu")]]))
        try:
            await bot.forward_message(uid, o["poll_chat"], o["poll_msg"])
        except TelegramBadRequest:
            pass
        vt = await voters_text(o)
        if vt:
            await bot.send_message(uid, vt)
        if await get_setting("review_on") == "1":
            await bot.send_message(uid, await T("review_request", id=oid))
    except (TelegramForbiddenError, TelegramBadRequest):
        pass
    return True


# ───────────────────────── АДМИН: ДЕЙСТВИЯ С ЗАКАЗАМИ ─────────────────────────
def who(u: User):
    return f"@{u.username}" if u.username else esc(u.full_name)


@admin.callback_query(F.data.startswith("ord_ok:"))
async def ord_ok(cb: CallbackQuery):
    oid = int(cb.data.split(":")[1])
    c = await db.execute("UPDATE orders SET status='awaiting_payment' WHERE id=? AND status='pending_approval'", (oid,))
    await db.commit()
    if c.rowcount == 0:
        return await cb.answer("Заказ уже обработан", show_alert=True)
    await edit_card(cb.message, cb.message.html_text + f"\n\n✅ Принят ({who(cb.from_user)})")
    await offer_payment(await fetchone("SELECT * FROM orders WHERE id=?", (oid,)))
    await cb.answer("Реквизиты отправлены клиенту")


@admin.callback_query(F.data.startswith("ord_no:"))
async def ord_no(cb: CallbackQuery):
    oid = int(cb.data.split(":")[1])
    c = await db.execute("UPDATE orders SET status='rejected' WHERE id=? AND status='pending_approval'", (oid,))
    await db.commit()
    if c.rowcount == 0:
        return await cb.answer("Заказ уже обработан", show_alert=True)
    o = await fetchone("SELECT * FROM orders WHERE id=?", (oid,))
    await edit_card(cb.message, cb.message.html_text + f"\n\n❌ Отклонён ({who(cb.from_user)})")
    try:
        await bot.send_message(o["user_id"], f"❌ К сожалению, заказ #{oid} отклонён.")
    except (TelegramForbiddenError, TelegramBadRequest):
        pass
    await cb.answer()


@admin.callback_query(F.data.startswith("pay_ok:"))
async def pay_ok(cb: CallbackQuery):
    oid = int(cb.data.split(":")[1])
    c = await db.execute("UPDATE orders SET paid_at=? WHERE id=? AND status='review'", (int(time.time()), oid))
    await db.commit()
    if c.rowcount == 0:
        return await cb.answer("Заказ уже обработан", show_alert=True)
    o = await fetchone("SELECT * FROM orders WHERE id=?", (oid,))
    await edit_card(cb.message, cb.message.html_text + f"\n\n✅ Одобрено ({who(cb.from_user)})", work_kb(oid))
    try:
        await bot.send_message(o["user_id"], await T("pay_ok", id=oid),
                               reply_markup=kb_of([[("📊 Промежуточный результат", f"mo:{oid}")]]))
    except (TelegramForbiddenError, TelegramBadRequest):
        pass
    await start_voting(oid)
    await cb.answer("Запущено")


@admin.callback_query(F.data.startswith("pay_no:"))
async def pay_no(cb: CallbackQuery):
    oid = int(cb.data.split(":")[1])
    c = await db.execute("UPDATE orders SET status='awaiting_payment' WHERE id=? AND status='review'", (oid,))
    await db.commit()
    if c.rowcount == 0:
        return await cb.answer("Заказ уже обработан", show_alert=True)
    o = await fetchone("SELECT * FROM orders WHERE id=?", (oid,))
    await edit_card(cb.message, cb.message.html_text + f"\n\n❌ Отклонено ({who(cb.from_user)})")
    try:
        await bot.send_message(o["user_id"], await T("pay_no", id=oid),
                               reply_markup=kb_of([[("📤 Отправить скрин заново", f"pay:rub:{oid}")]]))
    except (TelegramForbiddenError, TelegramBadRequest):
        pass
    await cb.answer("Клиент уведомлён")


@admin.callback_query(F.data.startswith("close:"))
async def close_poll(cb: CallbackQuery):
    oid = int(cb.data.split(":")[1])
    ok = await finish_poll(oid)
    if cb.message.chat.type == "private":
        await edit(cb, *await order_view(oid))
    await cb.answer("Опрос закрыт" if ok else "Опрос уже закрыт", show_alert=not ok)


@admin.callback_query(F.data.startswith("done:"))
async def order_done(cb: CallbackQuery):
    if not await complete_order(int(cb.data.split(":")[1])):
        return await cb.answer("Заказ уже закрыт", show_alert=True)
    await edit_card(cb.message, cb.message.html_text + "\n\n📬 Клиент уведомлён о выполнении")
    await cb.answer("Готово")


@user.callback_query(F.data.regexp(r"^(pay_ok|pay_no|ord_ok|ord_no|done|close|ocancel|bpay_ok|bpay_no|bdone|bcancel|a):"))
async def no_access(cb: CallbackQuery):
    await cb.answer("Нет доступа", show_alert=True)


# ───────────────────────── АДМИН-ПАНЕЛЬ ─────────────────────────
async def admin_home():
    today = int(datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
    r = await revenue(today)
    pend = await count("SELECT COUNT(*) FROM orders WHERE status='pending_approval'")
    rev = await count("SELECT COUNT(*) FROM orders WHERE status='review'")
    bk_rev = await count("SELECT COUNT(*) FROM bookings WHERE status='review'")
    bk_act = await count("SELECT COUNT(*) FROM bookings WHERE status='active'")
    on = await get_setting("intake") != "0"
    text = ("⚙️ <b>Админ-панель</b>\n\n"
            f"Сегодня: оплат {r['n']} · {r['rub']} ₽ / {r['xtr']}⭐\n"
            f"Ждут принятия: {pend} · проверки оплаты: {rev}\n"
            f"Брони: на проверке {bk_rev} · активных {bk_act}\n"
            f"Приём заказов: {'🟢 включён' if on else '🔴 приостановлен'}")
    kb = kb_of([
        [("📊 Статистика", "a:stats"), ("📋 Заказы", "a:orders")],
        [("📦 Позиции и цены", "a:prods"), ("✏️ Тексты бота", "a:texts")],
        [("📣 Рассылка", "a:bc"), ("🛠 Группы и темы", "a:groups")],
        [("🛍 Товары витрины", "a:show"), ("🧩 Редактор кнопок", "a:btns")],
        [("🔖 Брони", "a:books"), ("🎟 Промокоды", "a:promos")],
        [("⚙️ Настройки", "a:set")],
        [("⏸ Приостановить приём" if on else "▶️ Возобновить приём", "a:intake")]])
    return text, kb


@admin.message(Command("admin"))
async def admin_cmd(m: Message, state: FSMContext):
    await state.clear()
    text, kb = await admin_home()
    
    if m.chat.type == "private":
        return await m.answer(text, reply_markup=kb)
        
    # В группе: отправляем панель только тому, кто написал /admin
    try:
        await bot.send_message(m.from_user.id, text, reply_markup=kb)
        await m.reply("⚙️ Админ-панель отправлена вам в личные сообщения.")
    except TelegramForbiddenError:
        await m.reply("⚠️ Не удалось отправить панель в личку. Напишите боту /start в личных сообщениях и попробуйте снова.")


@admin.callback_query(F.data == "a:menu")
async def admin_menu(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await edit(cb, *await admin_home())
    await cb.answer()


@admin.callback_query(F.data == "a:intake")
async def toggle_intake(cb: CallbackQuery):
    new = "1" if await get_setting("intake") == "0" else "0"
    await run("INSERT OR REPLACE INTO settings VALUES('intake',?)", (new,))
    await edit(cb, *await admin_home())
    await cb.answer("Приём заказов возобновлён" if new == "1" else "Приём заказов приостановлен")


@admin.message(Command("id"))
async def get_ids(m: Message):
    topic = m.message_thread_id if m.is_topic_message else 0
    await m.reply(f"chat_id: <code>{m.chat.id}</code>\ntopic_id: <code>{topic}</code>")


@admin.message(Command("ban"))
async def ban(m: Message):
    uid = to_int(m.text.partition(" ")[2])
    if uid is None:
        return await m.answer("Использование: /ban 123456789")
    await run("UPDATE users SET banned=1 WHERE user_id=?", (uid,))
    await m.answer(f"🚫 {uid} заблокирован")


@admin.message(Command("unban"))
async def unban(m: Message):
    uid = to_int(m.text.partition(" ")[2])
    if uid is None:
        return await m.answer("Использование: /unban 123456789")
    await run("UPDATE users SET banned=0 WHERE user_id=?", (uid,))
    await m.answer(f"✅ {uid} разблокирован")


async def move(table, rid, step):
    """Сдвиг элемента вверх/вниз в списке (table — из белого списка)."""
    assert table in ("products", "buttons")
    ids = [r["id"] for r in await fetch(f"SELECT id FROM {table} ORDER BY sort, id")]
    i = ids.index(rid)
    j = i + step
    if 0 <= j < len(ids):
        ids[i], ids[j] = ids[j], ids[i]
    for n, x in enumerate(ids):
        await db.execute(f"UPDATE {table} SET sort=? WHERE id=?", (n, x))
    await db.commit()


# ── Группы и темы
def msg_url(chat, mid):
    s_ = str(chat or 0)
    return f"https://t.me/c/{s_[4:]}/{mid}" if s_.startswith("-100") and mid else None


async def groups_view():
    lines = []
    for kind, name in (("pay", "💳 Оплаты"), ("ord", "🗳 Заказы"), ("sup", "💬 Обратная связь"),
                       ("bkc", "🔖 Брони: подтверждение"), ("bkt", "📌 Брони тапов")):
        chat = int(await get_setting(f"{kind}_chat") or 0)
        topic = int(await get_setting(f"{kind}_topic") or 0)
        lines.append(f"{name}: " + (f"✅ чат <code>{chat}</code>, тема <code>{topic or 'основная'}</code>"
                                    if chat else ("❌ не привязано (идёт в тему оплат)" if kind in ("sup", "bkc", "bkt") else "❌ не привязано (сообщения идут вам в личку)")))
    text = ("🛠 <b>Группы и темы</b>\n\n" + "\n".join(lines) + "\n\n"
            "Как привязать: добавьте бота в группу <b>админом</b>, зайдите в нужную тему и отправьте там команду:\n"
            "<code>/setpay</code> — тема оплат (скрины, кнопки «выполнено»)\n"
            "<code>/setorders</code> — тема заказов (ссылка и опрос)\n"
            "<code>/setsupport</code> — тема обратной связи (необязательно)\n"
            "<code>/setbookconfirm</code> — тема, куда приходят брони на подтверждение\n"
            "<code>/setbooks</code> — тема «Брони тапов»: сюда бот пишет подтверждённые брони")
    return text, kb_of([[("🧪 Отправить тестовое сообщение", "a:gtest")],
                        [("🧹 Сбросить привязки", "a:greset")], [("⬅️ В админ-меню", "a:menu")]])


@admin.callback_query(F.data == "a:groups")
async def groups(cb: CallbackQuery):
    await edit(cb, *await groups_view())
    await cb.answer()


@admin.callback_query(F.data == "a:gtest")
async def groups_test(cb: CallbackQuery):
    res = []
    for kind, name in (("pay", "Оплаты"), ("ord", "Заказы"), ("sup", "Обратная связь"),
                       ("bkc", "Брони: подтверждение"), ("bkt", "Брони тапов")):
        try:
            await send_to(kind, f"🧪 Тест: сюда приходят сообщения раздела «{name}»")
            res.append(f"✅ {name}")
        except (TelegramBadRequest, TelegramForbiddenError) as e:
            res.append(f"❌ {name}: {e.message[:80]}")
    await cb.message.answer("Результат теста:\n" + "\n".join(res))
    await cb.answer()


@admin.callback_query(F.data == "a:greset")
async def groups_reset(cb: CallbackQuery):
    for k in ("pay_chat", "pay_topic", "ord_chat", "ord_topic", "sup_chat", "sup_topic",
              "bkc_chat", "bkc_topic", "bkt_chat", "bkt_topic"):
        await run("INSERT OR REPLACE INTO settings VALUES(?, '0')", (k,))
    await edit(cb, *await groups_view())
    await cb.answer("Сброшено")


async def bind_group(m: Message, kind, name):
    if m.chat.type == "private":
        return await m.answer("Эту команду нужно отправить в группе, в нужной теме.")
    topic = m.message_thread_id if m.is_topic_message else 0
    await run("INSERT OR REPLACE INTO settings VALUES(?,?)", (f"{kind}_chat", str(m.chat.id)))
    await run("INSERT OR REPLACE INTO settings VALUES(?,?)", (f"{kind}_topic", str(topic)))
    await m.reply(f"✅ Эта тема привязана: {name}")


@admin.message(Command("setpay"))
async def set_pay(m: Message):
    await bind_group(m, "pay", "💳 Оплаты")


@admin.message(Command("setorders"))
async def set_orders(m: Message):
    await bind_group(m, "ord", "🗳 Заказы")


@admin.message(Command("setsupport"))
async def set_support(m: Message):
    await bind_group(m, "sup", "💬 Обратная связь")


@admin.message(Command("setbookconfirm"))
async def set_book_confirm(m: Message):
    await bind_group(m, "bkc", "🔖 Брони: подтверждение")


@admin.message(Command("setbooks"))
async def set_books(m: Message):
    await bind_group(m, "bkt", "📌 Брони тапов")


# ── Заказы
ORDER_FILTERS = [("pending_approval", "🆕 Ждут принятия"), ("review", "🕓 Проверка оплаты"),
                 ("voting", "🗳 Опрос идёт"), ("voted", "⏳ Опрос закрыт"),
                 ("awaiting_payment", "💳 Ждут оплаты"), ("done", "✅ Выполненные"), ("all", "📋 Все")]


@admin.callback_query(F.data == "a:orders")
async def orders_menu(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    rows = []
    for key, label in ORDER_FILTERS:
        n = (await count("SELECT COUNT(*) FROM orders") if key == "all"
             else await count("SELECT COUNT(*) FROM orders WHERE status=?", (key,)))
        rows.append([(f"{label} ({n})", f"a:ol:{key}")])
    nb = await count("SELECT COUNT(*) FROM bookings WHERE status IN ('review','active')")
    rows += [[(f"🔖 Брони ({nb})", "a:books")], [("📤 Экспорт CSV", "a:export")], [("⬅️ В админ-меню", "a:menu")]]
    await edit(cb, "📋 <b>Заказы</b>", kb_of(rows))
    await cb.answer()


@admin.callback_query(F.data.startswith("a:ol:"))
async def orders_list(cb: CallbackQuery):
    key = cb.data.split(":")[2]
    if key not in dict(ORDER_FILTERS):
        return await cb.answer()
    rows = (await fetch("SELECT * FROM orders ORDER BY id DESC LIMIT 15") if key == "all" else
            await fetch("SELECT * FROM orders WHERE status=? ORDER BY id DESC LIMIT 15", (key,)))
    b = InlineKeyboardBuilder()
    for o in rows:
        b.button(text=f"{STATUS.get(o['status'], '•').split()[0]} #{o['id']} · {qty_label(o)} · {o['target_user']}",
                 callback_data=f"a:o:{o['id']}")
    b.button(text="⬅️ К заказам", callback_data="a:orders")
    b.adjust(1)
    await edit(cb, f"{dict(ORDER_FILTERS)[key]}\n" + ("Последние 15:" if rows else "Пока пусто."), b.as_markup())
    await cb.answer()


async def order_view(oid):
    o = await fetchone("SELECT * FROM orders WHERE id=?", (oid,))
    if not o:
        return "Заказ не найден.", kb_of([[("⬅️ К заказам", "a:orders")]])
    st = o["status"]
    pay = {"rub": "₽ (реквизиты)", "xtr": "⭐ Stars"}.get(o["currency"], "—")
    text = (f"🧾 <b>Заказ #{oid}</b> · {STATUS.get(st, st)}\n\n"
            f"👤 {await label_by_id(o['user_id'])}\n📦 Количество: {qty_label(o)}\n"
            f"🔗 {esc(o['post_link'])}\n💬 {esc(o['target_user'])}\n"
            f"💰 {fmt_price(o['price_stars'], o['price_rub'])} · оплата: {pay}\n"
            + (f"🗳 Оттапано: {o['votes']}/{o['qty']}\n" if st in ("voting", "voted", "done") else "")
            + f"🕒 Создан: {datetime.fromtimestamp(o['created_at']).strftime('%d.%m %H:%M')}")
    rows = []
    if st == "pending_approval":
        rows.append([("✅ Принять", f"ord_ok:{oid}"), ("❌ Отклонить", f"ord_no:{oid}")])
    if st == "voting":
        rows.append([("🔒 Закрыть опрос", f"close:{oid}")])
    if st in ("voting", "voted"):
        rows.append([("📣 Сообщить о выполнении", f"done:{oid}")])
    if st in ("pending_approval", "awaiting_payment", "review"):
        rows.append([("🚫 Отменить заказ", f"ocancel:{oid}")])
    if st in ("voting", "voted", "done"):
        rows.append([("👥 Участники", f"a:vt:{oid}")])
    url = msg_url(o["adm_chat"], o["adm_msg"])
    if url and st in ("review", "voting", "voted", "done"):
        rows.append([("🔗 Карточка в группе", url)])
    rows.append([("⬅️ К заказам", "a:orders")])
    return text, kb_of(rows)


@admin.callback_query(F.data.regexp(r"^a:o:\d+$"))
async def order_open(cb: CallbackQuery):
    await edit(cb, *await order_view(int(cb.data.split(":")[2])))
    await cb.answer()


@admin.callback_query(F.data.startswith("ocancel:"))
async def order_cancel(cb: CallbackQuery):
    oid = int(cb.data.split(":")[1])
    c = await db.execute("""UPDATE orders SET status='cancelled' WHERE id=?
                            AND status IN ('pending_approval','awaiting_payment','review')""", (oid,))
    await db.commit()
    if c.rowcount == 0:
        return await cb.answer("Этот заказ уже нельзя отменить", show_alert=True)
    o = await fetchone("SELECT user_id FROM orders WHERE id=?", (oid,))
    try:
        await bot.send_message(o["user_id"], f"🚫 Заказ #{oid} отменён.")
    except (TelegramForbiddenError, TelegramBadRequest):
        pass
    await edit(cb, *await order_view(oid))
    await cb.answer("Отменён")


# ── Товары витрины (видимость и порядок в /start)
async def show_view():
    rows = []
    for p in await fetch("SELECT * FROM products ORDER BY sort, id"):
        rows.append([(f"{'🟢' if p['active'] else '🔴'} {p['title']}", f"sv:t:{p['id']}"),
                     ("⬆️", f"sv:u:{p['id']}"), ("⬇️", f"sv:d:{p['id']}")])
    rows.append([("⬅️ В админ-меню", "a:menu")])
    return ("🛍 <b>Товары витрины</b>\n\nНажмите на название — показать/скрыть кнопку в /start, "
            "стрелки — порядок. Цены и количество — в «Позиции и цены».", kb_of(rows))


@admin.callback_query(F.data == "a:show")
async def show_open(cb: CallbackQuery):
    await edit(cb, *await show_view())
    await cb.answer()


@admin.callback_query(F.data.startswith("sv:"))
async def show_action(cb: CallbackQuery):
    _, act, pid = cb.data.split(":")
    pid = int(pid)
    if act == "t":
        await run("UPDATE products SET active=1-active WHERE id=?", (pid,))
    else:
        await move("products", pid, -1 if act == "u" else 1)
    await edit(cb, *await show_view())
    await cb.answer()


# ── Редактор кнопок /start
KIND = {"url": "🔗 ссылка", "text": "💬 текст", "price": "💰 прайс (встроенная)", "my": "📦 мои заказы (встроенная)",
        "buy": "🛒 покупка тапов (встроенная)", "book": "🔖 бронь (встроенная)",
        "support": "✉️ связь (встроенная)"}


def norm_url(v):
    v = v.strip()
    if v.startswith("t.me/"):
        v = "https://" + v
    return v if re.match(r"^(https?://|tg://)\S+$", v) else None


async def btns_view():
    rows = [[(f"{'🟢' if x['active'] else '🔴'} {x['label']} · {KIND[x['kind']].split()[1]}", f"a:b:{x['id']}")]
            for x in await fetch("SELECT * FROM buttons ORDER BY sort, id")]
    rows += [[("➕ Кнопка-ссылка", "badd:url"), ("➕ Кнопка с текстом", "badd:text")],
             [("⬅️ В админ-меню", "a:menu")]]
    return ("🧩 <b>Редактор кнопок</b>\n\nКнопки под товарами в /start. Сначала идут товары витрины, "
            "затем эти кнопки в порядке списка.", kb_of(rows))


async def btn_card(bid):
    x = await fetchone("SELECT * FROM buttons WHERE id=?", (bid,))
    text = (f"🧩 <b>{esc(x['label'])}</b>\nТип: {KIND[x['kind']]}\n"
            f"Статус: {'🟢 показывается' if x['active'] else '🔴 скрыта'}"
            + (f"\nЗначение: {esc((x['value'] or '')[:300])}" if x["kind"] in ("url", "text") else ""))
    rows = [[("✏️ Название", f"be:{bid}:label")]]
    if x["kind"] in ("url", "text"):
        rows[0].append(("✏️ Значение", f"be:{bid}:value"))
    rows += [[("👁 Показать/скрыть", f"bt:{bid}"), ("⬆️", f"bm:{bid}:u"), ("⬇️", f"bm:{bid}:d")]]
    if x["kind"] in ("url", "text"):
        rows.append([("🗑 Удалить", f"bd:{bid}")])
    rows.append([("⬅️ К списку", "a:btns")])
    return text, kb_of(rows)


@admin.callback_query(F.data == "a:btns")
async def btns_open(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await edit(cb, *await btns_view())
    await cb.answer()


@admin.callback_query(F.data.regexp(r"^a:b:\d+$"))
async def btn_open(cb: CallbackQuery):
    await edit(cb, *await btn_card(int(cb.data.split(":")[2])))
    await cb.answer()


@admin.callback_query(F.data.startswith("bt:"))
async def btn_toggle(cb: CallbackQuery):
    bid = int(cb.data.split(":")[1])
    await run("UPDATE buttons SET active=1-active WHERE id=?", (bid,))
    await edit(cb, *await btn_card(bid))
    await cb.answer()


@admin.callback_query(F.data.startswith("bm:"))
async def btn_move(cb: CallbackQuery):
    _, bid, d = cb.data.split(":")
    await move("buttons", int(bid), -1 if d == "u" else 1)
    await edit(cb, *await btn_card(int(bid)))
    await cb.answer()


@admin.callback_query(F.data.startswith("bd:"))
async def btn_delete(cb: CallbackQuery):
    await run("DELETE FROM buttons WHERE id=? AND kind IN ('url','text')", (int(cb.data.split(":")[1]),))
    await edit(cb, *await btns_view())
    await cb.answer("Удалено")


@admin.callback_query(F.data.startswith("be:"))
async def btn_edit(cb: CallbackQuery, state: FSMContext):
    _, bid, field = cb.data.split(":")
    if field not in ("label", "value"):
        return await cb.answer()
    await state.set_state(BtnEdit.value)
    await state.update_data(bid=int(bid), field=field)
    await cb.message.answer("Отправьте новое название кнопки:" if field == "label" else
                            "Отправьте новое значение (ссылка или текст):", reply_markup=A_CANCEL)
    await cb.answer()


@admin.message(BtnEdit.value, F.text)
async def btn_edit_value(m: Message, state: FSMContext):
    d = await state.get_data()
    x = await fetchone("SELECT * FROM buttons WHERE id=?", (d["bid"],))
    if d["field"] == "label":
        v = m.text.strip()[:60]
    elif x["kind"] == "url":
        v = norm_url(m.text)
        if not v:
            return await m.answer("Нужна ссылка вида https://... или t.me/...")
    else:
        v = m.html_text
    await run(f"UPDATE buttons SET {d['field']}=? WHERE id=?", (v, d["bid"]))
    await state.clear()
    await m.answer(*await btn_card(d["bid"]))


@admin.callback_query(F.data.startswith("badd:"))
async def btn_add(cb: CallbackQuery, state: FSMContext):
    kind = cb.data.split(":")[1]
    if kind not in ("url", "text"):
        return await cb.answer()
    await state.set_state(BtnAdd.label)
    await state.update_data(kind=kind)
    await cb.message.answer("Название кнопки (то, что видит клиент):", reply_markup=A_CANCEL)
    await cb.answer()


@admin.message(BtnAdd.label, F.text)
async def btn_add_label(m: Message, state: FSMContext):
    await state.update_data(label=m.text.strip()[:60])
    await state.set_state(BtnAdd.value)
    kind = (await state.get_data())["kind"]
    await m.answer("Отправьте ссылку (https://... или t.me/...):" if kind == "url" else
                   "Отправьте текст, который бот покажет по нажатию:", reply_markup=A_CANCEL)


@admin.message(BtnAdd.value, F.text)
async def btn_add_value(m: Message, state: FSMContext):
    d = await state.get_data()
    v = norm_url(m.text) if d["kind"] == "url" else m.html_text
    if not v:
        return await m.answer("Нужна ссылка вида https://... или t.me/...")
    top = await count("SELECT COALESCE(MAX(sort),0) FROM buttons")
    bid = await run("INSERT INTO buttons(label,kind,value,sort) VALUES(?,?,?,?)", (d["label"], d["kind"], v, top + 1))
    await state.clear()
    await m.answer("✅ Кнопка добавлена")
    await m.answer(*await btn_card(bid))


# ── Ответы админов на обращения
async def is_ticket_reply(m: Message) -> bool:
    if not m.reply_to_message:
        return False
    return bool(await fetchone("SELECT 1 FROM ticket_msgs WHERE chat_id=? AND msg_id=?",
                               (m.chat.id, m.reply_to_message.message_id)))


@admin.message(is_ticket_reply)
async def ticket_reply(m: Message):
    t = await fetchone("""SELECT t.id, t.user_id FROM ticket_msgs x JOIN tickets t ON t.id=x.ticket_id
                          WHERE x.chat_id=? AND x.msg_id=?""", (m.chat.id, m.reply_to_message.message_id))
    kb = kb_of([[("✉️ Написать ещё", "support")]])
    head = f"💬 <b>Ответ администрации</b> (обращение #{t['id']})"
    try:
        if m.text:
            await bot.send_message(t["user_id"], f"{head}\n\n{m.html_text}", reply_markup=kb)
        else:
            await bot.send_message(t["user_id"], head)
            await bot.copy_message(t["user_id"], m.chat.id, m.message_id, reply_markup=kb)
    except (TelegramForbiddenError, TelegramBadRequest):
        return await m.reply("⚠️ Не удалось доставить: клиент заблокировал бота.")
    try:
        await m.react([ReactionTypeEmoji(emoji="👍")])
    except TelegramBadRequest:
        await m.reply("✅ Отправлено клиенту")


@admin.callback_query(F.data.regexp(r"^a:vt:\d+$"))
async def admin_voters(cb: CallbackQuery):
    oid = int(cb.data.split(":")[2])
    o = await fetchone("SELECT * FROM orders WHERE id=?", (oid,))
    await edit(cb, (o and await voters_text(o)) or "Пока нет участников.", kb_of([[("⬅️ К заказу", f"a:o:{oid}")]]))
    await cb.answer()


# ── Брони (админ)
async def drop_kb(b):
    if b["adm_chat"] and b["adm_msg"]:
        try:
            await bot.edit_message_reply_markup(b["adm_chat"], b["adm_msg"], reply_markup=None)
        except TelegramBadRequest:
            pass


async def book_view(bid):
    b = await fetchone("SELECT * FROM bookings WHERE id=?", (bid,))
    if not b:
        return "Бронь не найдена.", kb_of([[("⬅️ К броням", "a:books")]])
    st = b["status"]
    pay = {"rub": "₽ (реквизиты)", "xtr": "⭐ Stars"}.get(b["currency"], "—")
    text = (f"🔖 <b>Бронь #{bid}</b> · {BSTATUS.get(st, st)}\n\n👤 {await label_by_id(b['user_id'])}\n"
            f"💬 Юзернейм: @{esc(b['username'] or '—')}\n"
            f"📦 Количество: {b['qty'] or '—'} тапов\n🎭 Фандом: {esc(b['fandom'])}\n"
            f"👤 Персонаж (EN): {esc(b['char_en'])}\n"
            f"👤 Персонаж (RU): {esc(b['char_ru'])}\n💰 {fmt_price(b['price_stars'], b['price_rub'])} · оплата: {pay}\n"
            f"🕒 Создана: {datetime.fromtimestamp(b['created_at']).strftime('%d.%m %H:%M')}")
    rows = []
    if st == "active":
        rows.append([("✅ Бронь выполнена", f"bdone:{bid}")])
    if st in ("active", "review"):
        rows.append([("🚫 Отменить бронь", f"bcancel:{bid}")])
    url = msg_url(b["adm_chat"], b["adm_msg"])
    if url and st != "awaiting_payment":
        rows.append([("🔗 Карточка в группе", url)])
    rows.append([("⬅️ К броням", "a:books")])
    return text, kb_of(rows)


async def refresh_booking(cb: CallbackQuery, bid, note):
    if cb.message.chat.type == "private":
        await edit(cb, *await book_view(bid))
    else:
        await edit_card(cb.message, cb.message.html_text + f"\n\n{note} ({who(cb.from_user)})")


@admin.callback_query(F.data.startswith("bpay_ok:"))
async def bpay_ok(cb: CallbackQuery):
    bid = int(cb.data.split(":")[1])
    c = await db.execute("UPDATE bookings SET status='active', paid_at=? WHERE id=? AND status='review'",
                         (int(time.time()), bid))
    await db.commit()
    if c.rowcount == 0:
        return await cb.answer("Бронь уже обработана", show_alert=True)
    await edit_card(cb.message, cb.message.html_text + f"\n\n✅ Одобрено ({who(cb.from_user)})", bwork_kb(bid))
    await activate_booking(bid)
    await cb.answer("Бронь оформлена")


@admin.callback_query(F.data.startswith("bpay_no:"))
async def bpay_no(cb: CallbackQuery):
    bid = int(cb.data.split(":")[1])
    c = await db.execute("UPDATE bookings SET status='awaiting_payment' WHERE id=? AND status='review'", (bid,))
    await db.commit()
    if c.rowcount == 0:
        return await cb.answer("Бронь уже обработана", show_alert=True)
    b = await fetchone("SELECT * FROM bookings WHERE id=?", (bid,))
    await edit_card(cb.message, cb.message.html_text + f"\n\n❌ Отклонено ({who(cb.from_user)})")
    try:
        await bot.send_message(b["user_id"], await T("pay_no", id=bid) + "\n(бронь не оформлена, пока оплата не подтверждена)",
                               reply_markup=kb_of([[("📤 Отправить скрин заново", f"bpay:rub:{bid}")]]))
    except (TelegramForbiddenError, TelegramBadRequest):
        pass
    await cb.answer("Клиент уведомлён")


@admin.callback_query(F.data.startswith("bdone:"))
async def book_done(cb: CallbackQuery):
    bid = int(cb.data.split(":")[1])
    c = await db.execute("UPDATE bookings SET status='done', done_at=? WHERE id=? AND status='active'",
                         (int(time.time()), bid))
    await db.commit()
    if c.rowcount == 0:
        return await cb.answer("Бронь уже закрыта", show_alert=True)
    b = await fetchone("SELECT * FROM bookings WHERE id=?", (bid,))
    await drop_kb(b)
    try:
        await bot.send_message(b["user_id"], f"✅ Ваша бронь <b>#{bid}</b> ({esc(b['char_en'])}) выполнена. Спасибо!",
                               reply_markup=kb_of([[("🛒 В меню", "menu")]]))
    except (TelegramForbiddenError, TelegramBadRequest):
        pass
    await refresh_booking(cb, bid, "✅ Выполнена")
    await cb.answer("Готово")


@admin.callback_query(F.data.startswith("bcancel:"))
async def book_cancel(cb: CallbackQuery):
    bid = int(cb.data.split(":")[1])
    before = await fetchone("SELECT * FROM bookings WHERE id=?", (bid,))
    c = await db.execute("UPDATE bookings SET status='cancelled' WHERE id=? AND status IN ('active','review')", (bid,))
    await db.commit()
    if c.rowcount == 0:
        return await cb.answer("Бронь уже закрыта", show_alert=True)
    await drop_kb(before)
    note = ""
    if before["status"] == "active" and before["currency"] == "xtr" and before["charge_id"]:
        try:
            await bot.refund_star_payment(before["user_id"], before["charge_id"])
            note = " Оплата в Stars возвращена."
        except (TelegramBadRequest, TelegramForbiddenError):
            await cb.message.answer(f"⚠️ Бронь #{bid}: Stars вернуть автоматически не удалось, верните вручную.")
    elif before["status"] == "active":
        await cb.message.answer(f"💳 Бронь #{bid} отменена. Оплата по реквизитам: верните деньги клиенту вручную.")
    try:
        await bot.send_message(before["user_id"], f"🚫 Бронь #{bid} отменена.{note}")
    except (TelegramForbiddenError, TelegramBadRequest):
        pass
    await refresh_booking(cb, bid, "🚫 Отменена")
    await cb.answer("Отменена")


BOOK_FILTERS = [("review", "🕓 На проверке"), ("active", "🟢 Активные"),
                ("done", "✅ Выполненные"), ("cancelled", "🚫 Отменённые")]


@admin.callback_query(F.data == "a:books")
async def books_menu(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    lo, hi = await get_setting("book_min"), await get_setting("book_max")
    rows = []
    for key, label in BOOK_FILTERS:
        n = await count("SELECT COUNT(*) FROM bookings WHERE status=?", (key,))
        rows.append([(f"{label} ({n})", f"a:bl:{key}")])
    rows += [[("✏️ Мин. тапов", "bp:min"), ("✏️ Макс. тапов", "bp:max")], [("⬅️ В админ-меню", "a:menu")]]
    await edit(cb, f"🔖 <b>Брони</b>\n\n💰 Цена — строго по основному прайсу\n📦 Количество: от {lo} до {hi} тапов\n\n"
                   "Неоплаченные брони здесь не показываются — бронь появляется после отправки скрина оплаты "
                   "или оплаты Stars.", kb_of(rows))
    await cb.answer()


@admin.callback_query(F.data.startswith("a:bl:"))
async def books_list(cb: CallbackQuery):
    key = cb.data.split(":")[2]
    if key not in dict(BOOK_FILTERS):
        return await cb.answer()
    rows = await fetch("SELECT * FROM bookings WHERE status=? ORDER BY id DESC LIMIT 15", (key,))
    b = InlineKeyboardBuilder()
    for k in rows:
        b.button(text=f"#{k['id']} · {k['qty'] or '—'} · {k['fandom']} · {k['char_en']}", callback_data=f"a:bk:{k['id']}")
    b.button(text="⬅️ К броням", callback_data="a:books")
    b.adjust(1)
    await edit(cb, f"{dict(BOOK_FILTERS)[key]}\n" + ("Последние 15:" if rows else "Пока пусто."), b.as_markup())
    await cb.answer()


@admin.callback_query(F.data.regexp(r"^a:bk:\d+$"))
async def book_open(cb: CallbackQuery):
    await edit(cb, *await book_view(int(cb.data.split(":")[2])))
    await cb.answer()


@admin.callback_query(F.data.startswith("bp:"))
async def book_price_edit(cb: CallbackQuery, state: FSMContext):
    field = cb.data.split(":")[1]
    if field not in ("min", "max"):
        return await cb.answer()
    await state.set_state(BookPrice.value)
    await state.update_data(field=field)
    await cb.message.answer(f"Отправьте {'минимальное' if field == 'min' else 'максимальное'} "
                            "количество тапов для брони (число):", reply_markup=A_CANCEL)
    await cb.answer()


@admin.message(BookPrice.value, F.text)
async def book_price_save(m: Message, state: FSMContext):
    v = to_int(m.text)
    if v is None:
        return await m.answer("Нужно целое число.")
    field = (await state.get_data())["field"]
    other = int(await get_setting("book_max" if field == "min" else "book_min") or 0)
    if v < 1 or (field == "min" and v > other) or (field == "max" and v < other):
        return await m.answer("Минимум не может быть больше максимума. Введите другое число.")
    await run("INSERT OR REPLACE INTO settings VALUES(?,?)", (f"book_{field}", str(v)))
    await state.clear()
    await m.answer("✅ Сохранено", reply_markup=kb_of([[("🔖 К броням", "a:books")]]))


# ── Статистика
async def revenue(since):
    r = await fetchone(f"""SELECT
        COALESCE(SUM(CASE WHEN currency='rub' THEN price_rub END),0) rub,
        COALESCE(SUM(CASE WHEN currency='xtr' THEN price_stars END),0) xtr,
        COALESCE(SUM(qty),0) qty, COUNT(*) n
        FROM orders WHERE status IN {PAID} AND paid_at>=?""", (since,))
    k = await fetchone("""SELECT
        COALESCE(SUM(CASE WHEN currency='rub' THEN price_rub END),0) rub,
        COALESCE(SUM(CASE WHEN currency='xtr' THEN price_stars END),0) xtr, COUNT(*) n
        FROM bookings WHERE status IN ('active','done') AND paid_at>=?""", (since,))
    return {"rub": r["rub"] + k["rub"], "xtr": r["xtr"] + k["xtr"], "qty": r["qty"], "n": r["n"] + k["n"]}


@admin.callback_query(F.data == "a:stats")
async def stats(cb: CallbackQuery):
    now = int(time.time())
    today = int(datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
    lines = ["📊 <b>Статистика</b>\n\n💰 <b>Выручка</b>"]
    for label, since in [("Сегодня", today), ("7 дней", now - 7 * 86400),
                         ("30 дней", now - 30 * 86400), ("Всего", 0)]:
        r = await revenue(since)
        lines.append(f"{label}: <b>{r['rub']} ₽</b> · <b>{r['xtr']} ⭐</b> · {r['n']} зак. · {r['qty']} шт.")
    st = {s: await count("SELECT COUNT(*) FROM orders WHERE status=?", (s,)) for s in STATUS}
    lines += ["\n📦 <b>Заказы</b>",
              f"Создано всего: {await count('SELECT COUNT(*) FROM orders')}",
              f"🆕 Ждут подтверждения: {st['pending_approval']}",
              f"💳 Ждут оплаты: {st['awaiting_payment']}",
              f"🕓 На проверке оплаты: {st['review']}",
              f"🗳 Опрос идёт / закрыт: {st['voting']} / {st['voted']}",
              f"✅ Выполнено: {st['done']}",
              "\n👥 <b>Пользователи</b>",
              f"Всего: {await count('SELECT COUNT(*) FROM users')}",
              f"Активны 24ч: {await count('SELECT COUNT(*) FROM users WHERE last_active>=?', (now - 86400,))}",
              f"Активны 7 дн: {await count('SELECT COUNT(*) FROM users WHERE last_active>=?', (now - 7 * 86400,))}",
              f"Активны 30 дн: {await count('SELECT COUNT(*) FROM users WHERE last_active>=?', (now - 30 * 86400,))}",
              f"Новых сегодня: {await count('SELECT COUNT(*) FROM users WHERE joined_at>=?', (today,))}",
              f"Покупателей: {await count(f'SELECT COUNT(DISTINCT user_id) FROM orders WHERE status IN {PAID}')}",
              f"Заблокировано: {await count('SELECT COUNT(*) FROM users WHERE banned=1')}"]
    top = await fetch(f"""SELECT user_id, COUNT(*) n FROM orders WHERE status IN {PAID}
                          GROUP BY user_id ORDER BY n DESC LIMIT 3""")
    if top:
        lines.append("\n🏆 <b>Топ клиентов</b>")
        for i, t in enumerate(top, 1):
            lines.append(f"{i}. {await label_by_id(t['user_id'])} — {t['n']} зак.")
    await edit(cb, "\n".join(lines), kb_of([[("🔄 Обновить", "a:stats")], [("⬅️ В админ-меню", "a:menu")]]))
    await cb.answer()


# ── Позиции
async def prods_view():
    b = InlineKeyboardBuilder()
    for p in await fetch("SELECT * FROM products ORDER BY sort, id"):
        b.button(text=f"{'🟢' if p['active'] else '🔴'} {p['title']}", callback_data=f"a:p:{p['id']}")
    b.button(text="➕ Добавить позицию", callback_data="a:padd")
    b.button(text="⬅️ В админ-меню", callback_data="a:menu")
    b.adjust(1)
    return "🛍 <b>Позиции</b>\n🟢 есть кнопка в /start · 🔴 скрыта", b.as_markup()


@admin.callback_query(F.data == "a:prods")
async def prods(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await edit(cb, *await prods_view())
    await cb.answer()


async def prod_card(pid):
    p = await fetchone("SELECT * FROM products WHERE id=?", (pid,))
    t = await fetchone("SELECT COUNT(*) n, MIN(qty) a, MAX(qty) b FROM tiers WHERE product_id=?", (pid,))
    text = (f"📦 <b>{esc(p['title'])}</b>\n"
            f"Статус: {'🟢 показывается' if p['active'] else '🔴 скрыта'}\n"
            f"Количество: от {p['min_qty']} до {p['max_qty']}, шаг {p['step']}\n"
            f"В прайсе строк: {t['n']}" + (f" ({t['a']}–{t['b']})" if t["n"] else ""))
    return text, kb_of([
        [("✏️ Название", f"pe:{pid}:title"), ("👁 Показать/скрыть", f"ptog:{pid}")],
        [("💰 Прайс", f"pr:{pid}")],
        [("Мин", f"pe:{pid}:min_qty"), ("Макс", f"pe:{pid}:max_qty"), ("Шаг", f"pe:{pid}:step")],
        [("🗑 Удалить", f"pdel:{pid}"), ("⬅️ К позициям", "a:prods")]])


@admin.callback_query(F.data.regexp(r"^a:p:\d+$"))
async def prod_open(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await edit(cb, *await prod_card(int(cb.data.split(":")[2])))
    await cb.answer()


@admin.callback_query(F.data.startswith("ptog:"))
async def prod_toggle(cb: CallbackQuery):
    pid = int(cb.data.split(":")[1])
    await run("UPDATE products SET active=1-active WHERE id=?", (pid,))
    await edit(cb, *await prod_card(pid))
    await cb.answer("Готово")


@admin.callback_query(F.data.startswith("pdel:"))
async def prod_del(cb: CallbackQuery):
    pid = int(cb.data.split(":")[1])
    await edit(cb, "Удалить позицию вместе с прайсом? Старые заказы останутся в истории.",
               kb_of([[("🗑 Да, удалить", f"pdel2:{pid}"), ("Отмена", f"a:p:{pid}")]]))
    await cb.answer()


@admin.callback_query(F.data.startswith("pdel2:"))
async def prod_del2(cb: CallbackQuery):
    pid = int(cb.data.split(":")[1])
    await run("DELETE FROM tiers WHERE product_id=?", (pid,))
    await run("DELETE FROM products WHERE id=?", (pid,))
    await edit(cb, *await prods_view())
    await cb.answer("Удалено")


@admin.callback_query(F.data.startswith("pe:"))
async def prod_edit(cb: CallbackQuery, state: FSMContext):
    _, pid, field = cb.data.split(":")
    if field not in {"title", "min_qty", "max_qty", "step"}:
        return await cb.answer()
    await state.set_state(EditP.value)
    await state.update_data(pid=int(pid), field=field)
    hint = {"title": "новое название (текст кнопки)", "min_qty": "минимальное количество",
            "max_qty": "максимальное количество", "step": "шаг количества"}
    await cb.message.answer(f"Отправьте {hint[field]}:", reply_markup=A_CANCEL)
    await cb.answer()


@admin.message(EditP.value, F.text)
async def prod_edit_value(m: Message, state: FSMContext):
    d = await state.get_data()
    v = m.text.strip() if d["field"] == "title" else to_int(m.text)
    if not v:
        return await m.answer("Нужно целое число больше 0." if d["field"] != "title" else "Пустое название.")
    await run(f"UPDATE products SET {d['field']}=? WHERE id=?", (v, d["pid"]))
    await state.clear()
    await m.answer("✅ Сохранено")
    await m.answer(*await prod_card(d["pid"]))


@admin.callback_query(F.data == "a:padd")
async def padd(cb: CallbackQuery, state: FSMContext):
    await state.set_state(AddP.title)
    await cb.message.answer("Название новой позиции (будет текстом кнопки в /start):", reply_markup=A_CANCEL)
    await cb.answer()


@admin.message(AddP.title, F.text)
async def padd_title(m: Message, state: FSMContext):
    pid = await run("INSERT INTO products(title) VALUES(?)", (m.text.strip(),))
    await state.set_state(Prices.bulk)
    await state.update_data(pid=pid)
    await m.answer(PRICE_HINT, reply_markup=A_CANCEL)


# ── Прайс-лист позиции
PRICE_HINT = ("📥 Вставьте прайс текстом — можно прямо как в посте:\n"
              "<code>60 тапов — 15зв &amp; 25₽</code>\nили коротко: <code>60 15 25</code> "
              "(количество, звёзды, рубли), по строке на каждую цену.")


async def price_view(pid):
    p = await fetchone("SELECT title FROM products WHERE id=?", (pid,))
    t = await fetch("SELECT * FROM tiers WHERE product_id=? ORDER BY qty", (pid,))
    text = f"💰 <b>Прайс · {esc(p['title'])}</b>\n\n" + (
        "\n".join(f"{r['qty']} — {r['stars']}⭐ / {r['rub']}₽" for r in t) or "пусто")
    return text, kb_of([
        [("📥 Вставить новый прайс", f"prb:{pid}")],
        [("✏️ Изменить/добавить строку", f"prr:{pid}"), ("🗑 Удалить строку", f"prd:{pid}")],
        [("📈 Изменить все цены на %", f"prp:{pid}")],
        [("⬅️ Назад", f"a:p:{pid}")]])


@admin.callback_query(F.data.startswith("pr:"))
async def price_open(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await edit(cb, *await price_view(int(cb.data.split(":")[1])))
    await cb.answer()


@admin.callback_query(F.data.regexp(r"^pr[brdp]:\d+$"))
async def price_action(cb: CallbackQuery, state: FSMContext):
    act, pid = cb.data[2], int(cb.data.split(":")[1])
    st, prompt = {
        "b": (Prices.bulk, PRICE_HINT + "\n\n⚠️ Старый прайс будет заменён."),
        "r": (Prices.row, "Отправьте: <code>количество звёзды рубли</code>\nНапример: <code>80 25 40</code>"),
        "d": (Prices.delete, "Отправьте количество строки, которую удалить (например, 80):"),
        "p": (Prices.pct, "На сколько % изменить все цены? Например <code>10</code> (подорожание) или <code>-15</code>:"),
    }[act]
    await state.set_state(st)
    await state.update_data(pid=pid)
    await cb.message.answer(prompt, reply_markup=A_CANCEL)
    await cb.answer()


@admin.message(Prices.bulk, F.text)
async def price_bulk(m: Message, state: FSMContext):
    rows = parse_price(m.text)
    if not rows:
        return await m.answer("Не нашёл ни одной строки. Формат: <code>60 15 25</code> или «60 тапов — 15зв & 25₽».")
    pid = (await state.get_data())["pid"]
    await db.execute("DELETE FROM tiers WHERE product_id=?", (pid,))
    await db.executemany("INSERT INTO tiers VALUES(?,?,?,?)", [(pid, q, s, r) for q, s, r in rows])
    await db.commit()
    await state.clear()
    await m.answer(f"✅ Прайс обновлён: {len(rows)} строк")
    await m.answer(*await price_view(pid))


@admin.message(Prices.row, F.text)
async def price_row(m: Message, state: FSMContext):
    nums = re.findall(r"\d+", m.text)
    if len(nums) != 3:
        return await m.answer("Нужно три числа: количество, звёзды, рубли.")
    pid = (await state.get_data())["pid"]
    await run("INSERT OR REPLACE INTO tiers VALUES(?,?,?,?)", (pid, *map(int, nums)))
    await state.clear()
    await m.answer(*await price_view(pid))


@admin.message(Prices.delete, F.text)
async def price_del(m: Message, state: FSMContext):
    q = to_int(m.text)
    if q is None:
        return await m.answer("Нужно число.")
    pid = (await state.get_data())["pid"]
    await run("DELETE FROM tiers WHERE product_id=? AND qty=?", (pid, q))
    await state.clear()
    await m.answer(*await price_view(pid))


@admin.message(Prices.pct, F.text)
async def price_pct(m: Message, state: FSMContext):
    if not re.fullmatch(r"[+-]?\d{1,3}", m.text.strip()):
        return await m.answer("Нужно число, например 10 или -15.")
    pid, k = (await state.get_data())["pid"], 100 + int(m.text.strip())
    if k <= 0:
        return await m.answer("Слишком большая скидка.")
    await run("""UPDATE tiers SET stars=MAX(1,CAST(ROUND(stars*?/100.0) AS INTEGER)),
                 rub=MAX(1,CAST(ROUND(rub*?/100.0) AS INTEGER)) WHERE product_id=?""", (k, k, pid))
    await state.clear()
    await m.answer("✅ Цены пересчитаны")
    await m.answer(*await price_view(pid))


# ── Промокоды
@admin.callback_query(F.data == "a:promos")
async def promos(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    b = InlineKeyboardBuilder()
    for p in await fetch("SELECT * FROM promos ORDER BY code"):
        b.button(text=f"🗑 {p['code']} · −{p['percent']}% · осталось {p['uses']}",
                 callback_data=f"promodel:{p['code']}")
    b.button(text="➕ Создать", callback_data="a:promoadd")
    b.button(text="⬅️ В админ-меню", callback_data="a:menu")
    b.adjust(1)
    await edit(cb, "🎟 <b>Промокоды</b>\nНажмите на код, чтобы удалить.", b.as_markup())
    await cb.answer()


@admin.callback_query(F.data.startswith("promodel:"))
async def promo_del(cb: CallbackQuery, state: FSMContext):
    await run("DELETE FROM promos WHERE code=?", (cb.data.split(":", 1)[1],))
    await promos(cb, state)


@admin.callback_query(F.data == "a:promoadd")
async def promo_add(cb: CallbackQuery, state: FSMContext):
    await state.set_state(PromoAdd.data)
    await cb.message.answer("Отправьте: <code>КОД ПРОЦЕНТ ИСПОЛЬЗОВАНИЙ</code>\nНапример: <code>SALE10 10 50</code>",
                            reply_markup=A_CANCEL)
    await cb.answer()


@admin.message(PromoAdd.data, F.text)
async def promo_save(m: Message, state: FSMContext):
    parts = m.text.split()
    if len(parts) != 3 or not parts[1].isdigit() or not parts[2].isdigit() or not 1 <= int(parts[1]) <= 100:
        return await m.answer("Формат: КОД ПРОЦЕНТ(1-100) ИСПОЛЬЗОВАНИЙ")
    await run("INSERT OR REPLACE INTO promos VALUES(?,?,?)", (parts[0].upper(), int(parts[1]), int(parts[2])))
    await state.clear()
    await m.answer(f"✅ Промокод {parts[0].upper()} создан", reply_markup=kb_of([[("🎟 К промокодам", "a:promos")]]))


# ── Тексты бота
@admin.callback_query(F.data == "a:texts")
async def texts_menu(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    rows = [[(name, f"tx:{k}")] for k, (name, _, _) in TEXTS.items()] + [[("⬅️ В админ-меню", "a:menu")]]
    await edit(cb, "✏️ <b>Тексты бота</b>\nВыберите, что изменить:", kb_of(rows))
    await cb.answer()


async def text_card(key):
    name, _, hint = TEXTS[key]
    text = f"✏️ <b>{name}</b>\n\n{await T(key)}"
    if hint:
        text += f"\n\n<i>Можно использовать: {esc(hint)} (номер заказа)</i>"
    return text, kb_of([[("✏️ Изменить", f"txe:{key}"), ("↩️ По умолчанию", f"txr:{key}")],
                        [("⬅️ К текстам", "a:texts")]])


@admin.callback_query(F.data.startswith("tx:"))
async def text_open(cb: CallbackQuery, state: FSMContext):
    key = cb.data.split(":")[1]
    if key not in TEXTS:
        return await cb.answer()
    await state.clear()
    await edit(cb, *await text_card(key))
    await cb.answer()


@admin.callback_query(F.data.startswith("txe:"))
async def text_edit(cb: CallbackQuery, state: FSMContext):
    key = cb.data.split(":")[1]
    if key not in TEXTS:
        return await cb.answer()
    await state.set_state(SetVal.value)
    await state.update_data(key=key)
    await cb.message.answer("Отправьте новый текст (поддерживается форматирование Telegram):", reply_markup=A_CANCEL)
    await cb.answer()


@admin.callback_query(F.data.startswith("txr:"))
async def text_reset(cb: CallbackQuery):
    key = cb.data.split(":")[1]
    if key not in TEXTS:
        return await cb.answer()
    await run("DELETE FROM settings WHERE key=?", (key,))
    await edit(cb, *await text_card(key))
    await cb.answer("Сброшено")


@admin.message(SetVal.value, F.text)
async def text_save(m: Message, state: FSMContext):
    key = (await state.get_data())["key"]
    if key not in TEXTS:
        return await state.clear()
    await run("INSERT OR REPLACE INTO settings VALUES(?,?)", (key, m.html_text))
    await state.clear()
    await m.answer("✅ Сохранено")
    await m.answer(*await text_card(key))


# ── Настройки
TOGGLES = {
    "pre_approve": ("Подтверждать заказ вручную",
                    "новый заказ сначала приходит вам с кнопками «Принять»/«Отклонить», реквизиты — после принятия."),
    "auto_done": ("Сообщать о выполнении автоматически",
                  "когда опрос сам набрал нужное число, бот сразу закрывает заказ и пишет клиенту (без вашей кнопки)."),
    "review_on": ("Просить отзыв после заказа",
                  "после выполнения бот присылает клиенту текст-просьбу об отзыве (меняется в «Тексты бота»)."),
    "book_unique": ("Одна бронь на персонажа",
                    "нельзя забронировать персонажа (фандом + имя EN), на которого уже есть оплаченная бронь."),
}


async def settings_view():
    lines, rows = ["⚙️ <b>Настройки</b>\n"], []
    for k, (name, desc) in TOGGLES.items():
        lines.append(f"<b>{name}</b> — {desc}\n")
        rows.append([(f"{'✅' if await get_setting(k) == '1' else '⬜️'} {name}", f"sett:{k}")])
    rows.append([("⬅️ В админ-меню", "a:menu")])
    return "\n".join(lines), kb_of(rows)


@admin.callback_query(F.data == "a:set")
async def settings(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await edit(cb, *await settings_view())
    await cb.answer()


@admin.callback_query(F.data.startswith("sett:"))
async def toggle_setting(cb: CallbackQuery):
    key = cb.data.split(":")[1]
    if key not in TOGGLES:
        return await cb.answer()
    await run("INSERT OR REPLACE INTO settings VALUES(?,?)", (key, "0" if await get_setting(key) == "1" else "1"))
    await edit(cb, *await settings_view())
    await cb.answer("Готово")


# ── Рассылка
@admin.callback_query(F.data == "a:bc")
async def bc_start(cb: CallbackQuery, state: FSMContext):
    await state.set_state(Bc.msg)
    n = await count("SELECT COUNT(*) FROM users WHERE banned=0")
    await cb.message.answer(f"📢 Отправьте сообщение для рассылки (текст, фото, видео — что угодно).\n"
                            f"Получателей: <b>{n}</b>", reply_markup=A_CANCEL)
    await cb.answer()


@admin.message(Bc.msg)
async def bc_preview(m: Message, state: FSMContext):
    await state.update_data(chat_id=m.chat.id, mid=m.message_id)
    await state.set_state(Bc.confirm)
    await m.answer("👆 Так увидят пользователи.")
    await m.copy_to(chat_id=m.chat.id, message_id=m.message_id)
    await m.answer("Отправить всем?", reply_markup=kb_of([[("✅ Отправить", "bc:go"), ("❌ Отмена", "a:menu")]]))


@admin.callback_query(F.data == "bc:go")
async def bc_go(cb: CallbackQuery, state: FSMContext):
    d = await state.get_data()
    if not d.get("mid"):
        await state.clear()
        return await cb.answer("Нет сообщения для рассылки", show_alert=True)
    await state.clear()
    users = await fetch("SELECT user_id FROM users WHERE banned=0")
    await cb.message.answer("Начинаю рассылку...")
    ok = fail = 0
    for u in users:
        try:
            await bot.copy_message(u["user_id"], d["chat_id"], d["mid"])
            ok += 1
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after)
            try:
                await bot.copy_message(u["user_id"], d["chat_id"], d["mid"])
                ok += 1
            except Exception:
                fail += 1
        except Exception:
            fail += 1
        await asyncio.sleep(0.03)
    await cb.message.answer(f"📢 Рассылка завершена\n✅ Доставлено: {ok}\n❌ Не доставлено: {fail}")
    await cb.answer()


# ── CSV экспорт
@admin.callback_query(F.data == "a:export")
async def export(cb: CallbackQuery):
    orders = await fetch("SELECT * FROM orders ORDER BY id")
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(["id", "user_id", "title", "qty", "price_rub", "price_stars", "currency", "status", "created_at"])
    for o in orders:
        w.writerow([o["id"], o["user_id"], o["title"], o["qty"], o["price_rub"], o["price_stars"],
                    o["currency"], o["status"], o["created_at"]])
    data = buf.getvalue().encode("utf-8-sig")
    await cb.message.answer_document(BufferedInputFile(data, filename="orders.csv"))
    await cb.answer()


# ───────────────────────── ЗАПУСК ─────────────────────────
async def main():
    global db
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    await init_db()
    dp = Dispatcher(storage=MemoryStorage())
    dp.message.middleware(Track())
    dp.callback_query.middleware(Track())
    dp.include_router(admin)
    dp.include_router(user)
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())