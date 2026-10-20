
import asyncio
import json
from datetime import datetime
from typing import Optional

from telegram import (
    InlineKeyboardButton, InlineKeyboardMarkup,
    KeyboardButton, ReplyKeyboardMarkup,
    Update,
)
from telegram.ext import (
    ApplicationBuilder, CallbackQueryHandler,
    CommandHandler, ContextTypes, filters, MessageHandler,
)
from telethon import TelegramClient
from telethon.errors import (
    FloodWaitError, PhoneCodeExpiredError, PhoneCodeInvalidError,
    PhoneNumberInvalidError, SessionPasswordNeededError,
)
from telethon.sessions import StringSession

TOKEN = "8680431274:AAGhYb7FfTDl8j5ZgJ-oAW4_3Vxskhrczv8"
OWNER_ID = 8758092321
API_ID   = 36167293
API_HASH = "bd3d079dbd782500d57b6063ca33a9fe"

CHANNEL_1_LINK = "https://t.me/TN_OTP_STORE"
CHANNEL_1_ID   = -1004367597240
CHANNEL_2_LINK = "https://t.me/TN_OTP_STORE_GROUP"
CHANNEL_2_ID   = -1004281521383


users            = {}
admins           = {OWNER_ID}
banned_users     = set()
channels         = {
    "CHANNEL 1": {"link": CHANNEL_1_LINK, "user_id": CHANNEL_1_ID},
    "GROUP 2":   {"link": CHANNEL_2_LINK, "user_id": CHANNEL_2_ID},
}

accounts    = {}
ws_accounts = {}   # btn_name → { phone, password, price, btn_name }

WS_SERVICE_URL = "http://127.0.0.1:3000"   # Baileys service

import aiohttp as _aiohttp

# ══ EMBEDDED ws_service.js — no separate file needed ══
_WS_SERVICE_JS = r"""
const { default: makeWASocket, useMultiFileAuthState, DisconnectReason, fetchLatestBaileysVersion } = require("@whiskeysockets/baileys");
const { Boom } = require("@hapi/boom");
const express = require("express");
const fs      = require("fs");
const path    = require("path");
const pino    = require("pino");

const app  = express();
const PORT = 3000;
app.use(express.json());

// ── Storage ──────────────────────────────────────────────────────────────────
const SESSIONS_DIR = path.join(__dirname, "ws_sessions");
const OTP_FILE     = path.join(__dirname, "ws_otps.json");
const STOCK_FILE   = path.join(__dirname, "ws_stock.json");

if (!fs.existsSync(SESSIONS_DIR)) fs.mkdirSync(SESSIONS_DIR);

function loadJson(file, def = {}) {
    try { return JSON.parse(fs.readFileSync(file, "utf8")); }
    catch { return def; }
}
function saveJson(file, data) {
    fs.writeFileSync(file, JSON.stringify(data, null, 2));
}

// otps[phone]  = { code, ts }
// stock[phone] = { phone, password, price, btn_name, session_dir }
let otps  = loadJson(OTP_FILE, {});
let stock = loadJson(STOCK_FILE, {});

// Active sockets — phone → socket
const sockets = {};

// ── Helper: save / load OTP ───────────────────────────────────────────────────
function saveOtp(phone, code) {
    otps[phone] = { code, ts: Date.now() };
    saveJson(OTP_FILE, otps);
}

function getOtp(phone, afterTs = 0) {
    const e = otps[phone];
    if (!e) return null;
    if (e.ts < afterTs) return null;
    return e.code;
}

function clearOtp(phone) {
    delete otps[phone];
    saveJson(OTP_FILE, otps);
}

// ── Connect a WhatsApp session ────────────────────────────────────────────────
async function connectWA(phone, sessionDir, onOtp, onReady, onFail) {
    const { state, saveCreds } = await useMultiFileAuthState(sessionDir);
    const { version }          = await fetchLatestBaileysVersion();

    const sock = makeWASocket({
        version,
        auth:           state,
        logger:         pino({ level: "silent" }),
        printQRInTerminal: false,
        browser:        ["Chrome", "Chrome", "120.0.0"],
        syncFullHistory: false,
    });

    sockets[phone] = sock;

    sock.ev.on("creds.update", saveCreds);

    sock.ev.on("connection.update", async ({ connection, lastDisconnect, qr }) => {
        if (connection === "open") {
            console.log(`[WS] ${phone} connected`);
            if (onReady) onReady();
        }
        if (connection === "close") {
            const code = new Boom(lastDisconnect?.error)?.output?.statusCode;
            if (code !== DisconnectReason.loggedOut) {
                console.log(`[WS] ${phone} reconnecting...`);
                setTimeout(() => connectWA(phone, sessionDir, onOtp, onReady, onFail), 5000);
            } else {
                console.log(`[WS] ${phone} logged out`);
                delete sockets[phone];
                if (onFail) onFail("LOGGED_OUT");
            }
        }
    });

    // ── OTP capture from messages ──────────────────────────────────────────────
    sock.ev.on("messages.upsert", ({ messages, type }) => {
        if (type !== "notify") return;
        for (const msg of messages) {
            if (!msg.message) continue;
            const body =
                msg.message?.conversation ||
                msg.message?.extendedTextMessage?.text ||
                msg.message?.ephemeralMessage?.message?.extendedTextMessage?.text || "";

            // WhatsApp OTP pattern: "Your WhatsApp code: 123-456" or "123456"
            const m = body.match(/(\d{6})/);
            if (m) {
                const code = m[1];
                console.log(`[WS] OTP captured for ${phone}: ${code}`);
                saveOtp(phone, code);
                if (onOtp) onOtp(code);
            }
        }
    });

    return sock;
}

// ── Restore all stock sessions on boot ───────────────────────────────────────
async function restoreStockSessions() {
    for (const phone of Object.keys(stock)) {
        const sessionDir = path.join(SESSIONS_DIR, phone);
        if (!fs.existsSync(sessionDir)) continue;
        console.log(`[WS] Restoring session: ${phone}`);
        connectWA(phone, sessionDir, null, null, null);
    }
}

// ════════════════════════════════════════════════════════════════════════════
//  ADMIN ROUTES
// ════════════════════════════════════════════════════════════════════════════

// POST /admin/add   { phone, password, price, btn_name }
// Step 1 — initiate pairing (sends OTP to phone via WhatsApp itself)
app.post("/admin/add", async (req, res) => {
    const { phone, password, price, btn_name } = req.body;
    if (!phone || !price || !btn_name)
        return res.json({ ok: false, error: "phone, price, btn_name required" });

    if (stock[phone])
        return res.json({ ok: false, error: "ALREADY_EXISTS" });

    const sessionDir = path.join(SESSIONS_DIR, phone);
    if (!fs.existsSync(sessionDir)) fs.mkdirSync(sessionDir, { recursive: true });

    let responded = false;

    await connectWA(
        phone,
        sessionDir,
        null,
        () => {
            // Session already exists and connected — account ready
            if (!responded) {
                responded = true;
                stock[phone] = { phone, password: password || null, price: parseFloat(price), btn_name, session_dir: sessionDir };
                saveJson(STOCK_FILE, stock);
                res.json({ ok: true, status: "ALREADY_AUTHORIZED" });
            }
        },
        (err) => {
            if (!responded) { responded = true; res.json({ ok: false, error: err }); }
        }
    );

    // Give it 3 seconds to check if already authorized
    await new Promise(r => setTimeout(r, 3000));

    if (!responded) {
        responded = true;
        // Need OTP — tell Python bot to ask admin for OTP
        res.json({ ok: true, status: "OTP_REQUIRED", message: `OTP has been sent to ${phone} via WhatsApp. Send it back.` });
    }
});

// POST /admin/verify_otp   { phone, otp, password, price, btn_name }
// OTP is already auto-captured by the messages.upsert listener.
// This route just waits for the session to become active, then saves stock.
app.post("/admin/verify_otp", async (req, res) => {
    const { phone, otp, password, price, btn_name } = req.body;
    if (!phone || !otp) return res.json({ ok: false, error: "phone and otp required" });

    const sock = sockets[phone];
    if (!sock) return res.json({ ok: false, error: "NO_SESSION — call /admin/add first" });

    // Wait up to 15s for the session to go to "open" state (Baileys handles OTP internally)
    let connected = false;
    for (let i = 0; i < 30; i++) {
        if (sock.user) { connected = true; break; }
        await new Promise(r => setTimeout(r, 500));
    }

    stock[phone] = {
        phone,
        password: password || null,
        price:    parseFloat(price || 0),
        btn_name: btn_name || phone,
        session_dir: path.join(SESSIONS_DIR, phone),
    };
    saveJson(STOCK_FILE, stock);
    res.json({ ok: true, status: "ACCOUNT_ADDED", connected });
});

// POST /admin/update   { phone, password, price, btn_name }
// Update price / btn_name / password for an already-added account
app.post("/admin/update", (req, res) => {
    const { phone, password, price, btn_name } = req.body;
    if (!phone) return res.json({ ok: false, error: "phone required" });

    if (!stock[phone]) return res.json({ ok: false, error: "NOT_FOUND — call /admin/add first" });

    if (price    !== undefined) stock[phone].price    = parseFloat(price);
    if (btn_name !== undefined) stock[phone].btn_name = btn_name;
    if (password !== undefined) stock[phone].password = password || null;
    saveJson(STOCK_FILE, stock);
    res.json({ ok: true, status: "UPDATED" });
});

// POST /admin/remove   { phone }
app.post("/admin/remove", async (req, res) => {
    const { phone } = req.body;
    if (!phone) return res.json({ ok: false, error: "phone required" });

    const sock = sockets[phone];
    if (sock) {
        try { await sock.logout(); } catch {}
        delete sockets[phone];
    }

    delete stock[phone];
    saveJson(STOCK_FILE, stock);

    const sessionDir = path.join(SESSIONS_DIR, phone);
    if (fs.existsSync(sessionDir))
        fs.rmSync(sessionDir, { recursive: true, force: true });

    res.json({ ok: true });
});

// GET /admin/stock
app.get("/admin/stock", (req, res) => {
    res.json({ ok: true, stock });
});

// ════════════════════════════════════════════════════════════════════════════
//  BUYER ROUTES
// ════════════════════════════════════════════════════════════════════════════

// GET /buy/stock  — list available accounts for buyers
app.get("/buy/stock", (req, res) => {
    const available = {};
    for (const [phone, data] of Object.entries(stock)) {
        available[phone] = { btn_name: data.btn_name, price: data.price };
    }
    res.json({ ok: true, stock: available });
});

// POST /buy/get_number   { btn_name }
// Buyer selected an account — return the phone number
app.post("/buy/get_number", (req, res) => {
    const { btn_name } = req.body;
    const entry = Object.values(stock).find(s => s.btn_name === btn_name);
    if (!entry) return res.json({ ok: false, error: "NOT_FOUND" });
    res.json({ ok: true, phone: entry.phone, price: entry.price });
});

// POST /buy/get_otp   { phone, after_ts }
// Buyer sent OTP request to the number — bot polls for OTP
app.post("/buy/get_otp", (req, res) => {
    const { phone, after_ts = 0 } = req.body;
    const code = getOtp(phone, after_ts);
    if (!code) return res.json({ ok: false, status: "WAITING" });

    const entry = stock[phone];
    clearOtp(phone);

    // Remove from stock after delivery
    const password = entry?.password || null;
    delete stock[phone];
    saveJson(STOCK_FILE, stock);

    // Disconnect session
    const sock = sockets[phone];
    if (sock) {
        try { sock.ws?.close(); } catch {}
        delete sockets[phone];
    }

    res.json({ ok: true, code, password });
});

// ── Start ─────────────────────────────────────────────────────────────────────
app.listen(PORT, async () => {
    console.log(`[WS Service] Running on port ${PORT}`);
    await restoreStockSessions();
});
"""


def _write_ws_service():
    import os
    p = os.path.join(os.path.dirname(os.path.abspath(__file__)), '_ws_service.js')
    open(p, 'w', encoding='utf-8').write(_WS_SERVICE_JS.lstrip('\n'))
    return p







async def ws_post(endpoint: str, data: dict = None):
    async with _aiohttp.ClientSession() as s:
        async with s.post(f"{WS_SERVICE_URL}{endpoint}", json=data or {}, timeout=_aiohttp.ClientTimeout(total=10)) as r:
            return await r.json()

async def ws_get(endpoint: str):
    async with _aiohttp.ClientSession() as s:
        async with s.get(f"{WS_SERVICE_URL}{endpoint}", timeout=_aiohttp.ClientTimeout(total=10)) as r:
            return await r.json()

_account_locks: dict = {}

def _get_account_lock(name: str):
    if name not in _account_locks:
        _account_locks[name] = asyncio.Lock()
    return _account_locks[name]

add_flow    = {}
ws_add_flow = {}   # uid → { phone, password, price, btn_name, purchase_ts }
ws_buy_flow = {}   # uid → { phone, price, purchase_ts }

buy_flow = {}

pending_referrals = {}
pending_payments  = {}

def ensure_user(u):
    users.setdefault(u.id, {"name": u.first_name or "USER",
                             "username": u.username, "balance": 0, "refer": 0})
    users[u.id].update({"name": u.first_name or users[u.id].get("name","USER"),
                         "username": u.username})
    users[u.id].setdefault("balance", 0)
    users[u.id].setdefault("refer",   0)

is_admin  = lambda uid: uid in admins
is_owner  = lambda uid: uid == OWNER_ID
is_banned = lambda uid: uid in banned_users

def main_kb(uid):
    keyboard = [
        [KeyboardButton("BUY TG"), KeyboardButton("BUY WS")],
        [KeyboardButton("DIPOSIT"), KeyboardButton("PROFILE")],
        [KeyboardButton("BOT OWNER")],
    ]
    if is_admin(uid):
        keyboard.append([KeyboardButton("ADMIN")])
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=False)

def admin_kb():
    keyboard = [
        [KeyboardButton("ADD TG"),    KeyboardButton("REMOVE TG")],
        [KeyboardButton("ADD WS"),    KeyboardButton("REMOVE WS")],
        [KeyboardButton("ADD MONEY"), KeyboardButton("REMOVE MONEY")],
        [KeyboardButton("ADD UPI"),   KeyboardButton("REMOVE UPI")],
        [KeyboardButton("BACK")],
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=False)

def refer_link(bot_username, uid):
    return f"https://t.me/{bot_username}?start=ref_{uid}"

def leaderboard():
    top = sorted(users.items(), key=lambda x: x[1].get("refer",0), reverse=True)[:3]
    lines = ["TOP 3 HIGH REFERRERS NAME",""]
    for i in range(3):
        if i < len(top):
            uid, d = top[i]
            n = str(d.get("name") or "USER").replace("&","&amp;").replace("<","&lt;") \
                                             .replace(">","&gt;").replace('"',"&quot;")
            lines.append(f'{i+1}  <a href="tg://user?id={uid}">{n}</a>    {d.get("refer",0)}')
        else:
            lines.append(f"{i+1}  -    0")
    return "\n".join(lines)

def new_client(session_str=""):
    """Always returns a brand-new disconnected TelegramClient."""
    if not API_ID or not API_HASH:
        raise RuntimeError(
            "API_ID / API_HASH not set.\n"
            "Get them from https://my.telegram.org/apps and fill in app.py"
        )
    return TelegramClient(
        StringSession(session_str), API_ID, API_HASH,
        device_model="Samsung Galaxy S26",
        system_version="Android 15",
        app_version="10.14.0",
    )

async def dc(client):
    """Safe disconnect — never raises."""
    try:
        if client and client.is_connected():
            await client.disconnect()
    except Exception:
        pass

async def kill_buy(uid, ctx=None):
    """Tear down any active buy flow for uid."""
    f = buy_flow.pop(uid, None)
    if f:
        await dc(f.get("client"))
    if ctx:
        ctx.user_data["buy_otp"] = False

async def kill_add(uid, ctx=None):
    """Tear down any active add-account flow for uid."""
    f = add_flow.pop(uid, None)
    if f:
        await dc(f.get("client"))
    if ctx:
        ctx.user_data.pop("add_step", None)
        ctx.user_data.pop("admin_step", None)
        ctx.user_data.pop("admin_tid", None)

ALL_BTNS = {
    "BUY ACCOUNT","BUY TG","BUY WS",
    "DIPOSIT","PROFILE","REFER","BOT OWNER","ADMIN",
    "ADD MONEY","REMOVE MONEY","ADD UPI","REMOVE UPI",
    "ADD TG","REMOVE TG","ADD WS","REMOVE WS",
    "ADD ACCOUNT","REMOVE ACCOUNT","BACK",
}

# Global store for the UPI QR — stored as a Telegram file_id after owner sets it
upi_qr_file_id: Optional[str] = None

async def force_join_check(user, bot):
    if not channels: return True
    miss = []
    for name, d in channels.items():
        try:
            m = await bot.get_chat_member(d["user_id"], user.id)
            if m.status in ("left","kicked"):
                miss.append((name, d["link"]))
        except Exception:
            miss.append((name, d["link"]))
    if miss:
        btns = [[InlineKeyboardButton(n, url=l)] for n, l in miss]
        btns.append([InlineKeyboardButton("CONTINUE ✅", callback_data="cj")])
        return False, InlineKeyboardMarkup(btns)
    return True

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    if is_banned(u.id):
        await update.message.reply_text("YOU ARE BANNED FROM USING THIS BOT"); return
    res = await force_join_check(u, ctx.bot)
    if res is not True:
        await update.message.reply_text(
            "FIRST JOIN ALL CHANNEL TO USE THIS BOT AFTER JOIN CLICK ON CONTINUE 🔥",
            reply_markup=res[1]); return
    ensure_user(u)
    if ctx.args and ctx.args[0].startswith("ref_"):
        try:
            rid = int(ctx.args[0][4:])
            if rid != u.id: ctx.user_data["ref_by"] = rid
        except ValueError: pass
    await update.message.reply_text("WELCOME TO THE BOT 🔥", reply_markup=main_kb(u.id))

async def cb_check_join(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    res = await force_join_check(q.from_user, ctx.bot)
    if res is True:
        await q.answer()
        ensure_user(q.from_user)
        await q.edit_message_text("VERIFIED ✅\n\nWELCOME TO THE BOT 🔥")
        await ctx.bot.send_message(q.from_user.id, "WELCOME TO THE BOT 🔥",
                                   reply_markup=main_kb(q.from_user.id))
    else:
        await q.answer("PLEASE JOIN ALL CHANNELS FIRST", show_alert=True)
        await q.edit_message_reply_markup(reply_markup=res[1])

async def show_profile(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user; ensure_user(u); d = users[u.id]
    ul = f"USERNAME - @{u.username} ⚡" if u.username else f"USER ID - {u.id}"
    await update.message.reply_text(
        f"YOUR PROFILE ❤️‍🔥\n\nNAME - {d['name']} ⚡\n\n{ul}\n\n"
        f"DATE - {datetime.now().strftime('%d/%m/%Y')} 🔥\n\n"
        f"BALANCE - {d['balance']} 💰\n\nREFER - {d['refer']} 👥")

async def show_refer(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user; ensure_user(u)
    bot = await ctx.bot.get_me()
    lnk = refer_link(bot.username, u.id)
    txt = (
        "🎁 REFER $ ERAN REWARD 🎁\n\n"
        f"🔗 YOUR REFERRAL LINK 👇\n\n           {lnk}\n\n\n"
        "🏆 TOP 3 HIGH REFERRERS WILL GET THESE REWARDS 🏆\n\n"
        "🥇 1ST RANK\n\n1  💰 ₹10 RS EVERY MONTH\n\n"
        "2  💰 ₹10 RS CASHBACK WHEN BUYING 3 ACCOUNTS IN ONE DAY\n\n"
        "3  ₹10 RS DISCOUNT ON EVERY ACCOUNT PURCHASE 💵\n\n"
        "🥈 2ND RANK\n\n1  💰 ₹10 RS EVERY MONTH\n\n"
        "2  💰 ₹10 RS CASHBACK WHEN BUYING 3 ACCOUNTS IN ONE DAY\n\n"
        "🥉 3RD RANK\n\n1  💰 ₹10 RS EVERY MONTH 🎟️\n\n\n"
        "      HOW WILL THIS WORK ?\n\n"
        "1 SEND THE LINK TO YOUR FRIEND\n"
        "2 YOUR FRIEND MUST START THE BOT\n"
        "3 TAKE A SCREENSHOT\n"
        "4 SEND THE SCREENSHOT TO THE ADMIN\n"
        "5 ONCE THE ADMIN APPROVES IT\n"
        "6 ONE REFERRAL WILL BE ADDED TO YOUR ACCOUNT !\n\n\n"
        "NOTE - THIS SCHEME WILL START AFTER THE BOT REACHES 100 USERS."
    )
    await update.effective_message.reply_text(txt, reply_markup=InlineKeyboardMarkup([[
        InlineKeyboardButton("📸 SUBMIT PROOF", callback_data="ref_proof"),
        InlineKeyboardButton("🏆 LEADERBOARD",  callback_data="ref_board"),
    ]]))

async def show_deposit(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("SELECT YOUR PAYMENT METHOD",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("INR",  callback_data="dep_inr"),
            InlineKeyboardButton("USDT", callback_data="dep_usdt"),
        ]]))

async def cb_deposit(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    res = await force_join_check(q.from_user, ctx.bot)
    if res is not True:
        await q.answer()
        await q.message.reply_text(
            "FIRST JOIN ALL CHANNEL TO USE THIS BOT AFTER JOIN CLICK ON CONTINUE 🔥",
            reply_markup=res[1]); return
    await q.answer()
    if q.data == "dep_usdt":
        await q.edit_message_text("COMING SOON", reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("🔙 BACK", callback_data="dep_back")]])); return
    if q.data == "dep_back":
        await q.edit_message_text("SELECT YOUR PAYMENT METHOD",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("INR",  callback_data="dep_inr"),
                InlineKeyboardButton("USDT", callback_data="dep_usdt"),
            ]])); return
    if q.data == "dep_inr":
        ensure_user(q.from_user)
        cap = (
            "STEPS\n\n1 PAY THE AMOUNT\n\n2 TAKE A SCREENSHOT\n\n"
            "3 SEND TO THE ADMIN\n\n4 WAIT FOR ADMIN APPROVAL\n\n"
            "5 AFTER APPROVAL YOUR AMOUNT SUCCESSFULLY ADDED\n\n\n"
            "NOTE - SEND SCREENSHOT WITH ACCOUNT NAME AND AMOUNT\n\n"
            "FOR EXAMPLE\n\nACCOUNT NAME - VISHAL\n\nAMOUNT - 500"
        )
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("SUBMIT PROOF", callback_data="pay_submit"),
        ]])
        if upi_qr_file_id:
            await ctx.bot.send_photo(q.from_user.id, upi_qr_file_id,
                                     caption=cap, parse_mode="HTML", reply_markup=kb)
        else:
            await q.message.reply_text(
                cap + "\n\n⚠️ QR NOT SET YET — CONTACT ADMIN",
                parse_mode="HTML", reply_markup=kb)

async def cb_pay_submit(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    res = await force_join_check(q.from_user, ctx.bot)
    if res is not True:
        await q.answer()
        await q.message.reply_text(
            "FIRST JOIN ALL CHANNEL TO USE THIS BOT AFTER JOIN CLICK ON CONTINUE 🔥",
            reply_markup=res[1]); return
    await q.answer()
    ctx.user_data["await_pay_proof"] = True
    ctx.user_data["await_ref_proof"] = False
    await q.message.reply_text("SEND ME SCREENSHOT")

async def handle_upi_qr_set(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Owner sent a photo while in add_upi_qr step — store file_id and confirm."""
    global upi_qr_file_id
    if not update.message.photo:
        await update.message.reply_text("PLEASE SEND A PHOTO ❌"); return
    upi_qr_file_id = update.message.photo[-1].file_id
    ctx.user_data.pop("admin_step", None)
    await update.message.reply_text("QR SUCCESSFULLY SET ✅")

async def cb_upi_remove(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Handle YES / NO on the REMOVE UPI confirmation."""
    global upi_qr_file_id
    q = update.callback_query
    if not is_owner(q.from_user.id):
        await q.answer("OWNER ONLY", show_alert=True); return
    await q.answer()
    if q.data == "upi_rm_yes":
        upi_qr_file_id = None
        await q.edit_message_text("QR REMOVED ✅")
    else:
        await q.edit_message_text("CANCELLED ❌")

async def handle_pay_proof(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not update.message.photo: return
    cap   = (update.message.caption or "").strip()
    lines = [l.strip() for l in cap.splitlines() if l.strip()]
    aname = amt = None
    for line in lines:
        low = line.lower()
        if low.startswith("account name") and "-" in line:
            aname = line.split("-",1)[1].strip()
        elif low.startswith("amount") and "-" in line:
            amt = line.split("-",1)[1].strip()
    if not aname or not amt:
        await update.message.reply_text("SEND ME SCREENSHOT WITH ACCOUNT NAME AND AMOUNT"); return
    u = update.effective_user
    ctx.user_data["await_pay_proof"] = False
    photo = update.message.photo[-1].file_id
    am = await ctx.bot.send_photo(OWNER_ID, photo,
        caption=f"📥 NEW PAYMENT PROOF\n\nSUBMITTED BY - {u.id}\n\n"
                f"ACCOUNT NAME - {aname}\n\nAMOUNT - {amt}")
    await ctx.bot.edit_message_reply_markup(OWNER_ID, am.message_id,
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ ACCEPT", callback_data=f"pa_acc:{am.message_id}"),
            InlineKeyboardButton("❌ REJECT", callback_data=f"pa_rej:{am.message_id}"),
        ]]))
    pending_payments[am.message_id] = {"uid": u.id, "amt": amt}
    await update.message.reply_text(
        "PROOF SUBMITTED ✅\n\nADMIN WILL CHECK AND ADD YOUR AMOUNT SOON 🔥\n\nWAIT 6-12 HOURS ⚡")

async def cb_pay_admin(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if q.from_user.id != OWNER_ID:
        await q.answer("OWNER ONLY", show_alert=True); return
    await q.answer()
    action, mid = q.data.split(":",1); mid = int(mid)
    item = pending_payments.pop(mid, None)
    if not item:
        await q.edit_message_reply_markup(reply_markup=None); return
    uid = item["uid"]
    if action == "pa_acc":
        try:    a = float(item["amt"])
        except: a = 0
        users.setdefault(uid, {"name":"USER","username":None,"balance":0,"refer":0})
        users[uid]["balance"] = users[uid].get("balance",0) + a
        await ctx.bot.send_message(uid,
            f"YOUR PAYMENT OF ₹{a} HAS BEEN ACCEPTED ✅\n\nBALANCE ADDED 🔥")
    else:
        await ctx.bot.send_message(uid,
            "YOUR PAYMENT HAS BEEN REJECTED ❌\n\nCONTACT ADMIN FOR HELP")
    await q.edit_message_reply_markup(reply_markup=None)

async def cb_referral(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    res = await force_join_check(q.from_user, ctx.bot)
    if res is not True:
        await q.answer()
        await q.message.reply_text(
            "FIRST JOIN ALL CHANNEL TO USE THIS BOT AFTER JOIN CLICK ON CONTINUE 🔥",
            reply_markup=res[1]); return
    await q.answer()
    if q.data == "ref_proof":
        ctx.user_data["await_ref_proof"] = True
        ctx.user_data["await_pay_proof"] = False
        await q.message.reply_text(
            "SEND SCREENSHOT WITH THE REFERRED USER ID AS THE PHOTO CAPTION"); return
    if q.data == "ref_board":
        await q.edit_message_text(leaderboard(), parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔙 BACK", callback_data="ref_back")]])); return
    if q.data == "ref_back":
        await show_refer(update, ctx)

async def handle_ref_proof(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    retry = "SEND SCREENSHOT AGAIN WITH THE REFERRED USER ID AS THE PHOTO CAPTION"
    if not update.message.photo:
        await update.message.reply_text(retry); return
    cap = (update.message.caption or "").strip()
    if not cap:
        await update.message.reply_text(retry); return
    try:    rid = int(cap)
    except:
        await update.message.reply_text(retry); return
    u = update.effective_user; ensure_user(u)
    photo = update.message.photo[-1].file_id
    am = await ctx.bot.send_photo(OWNER_ID, photo,
        caption=f"NEW REFER PROOF ⚡\n\nSUBMITTED BY - {u.id} 🔥\n\nREFERRED ID - {rid} 🔥",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ ACCEPT", callback_data=f"ra_acc:{u.id}"),
            InlineKeyboardButton("❌ REJECT", callback_data=f"ra_rej:{u.id}"),
        ]]))
    pending_referrals[u.id] = {"rid": rid, "admin_msg": am.message_id}
    ctx.user_data["await_ref_proof"] = False
    await update.message.reply_text(
        "PROOF SUBMITTED ✅\n\nADMIN WILL CHECK AND ADD YOUR REFER SOON 🔥\n\nWAIT 6-12 HOURS ⚡")

async def cb_ref_admin(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if q.from_user.id != OWNER_ID:
        await q.answer("OWNER ONLY", show_alert=True); return
    await q.answer()
    action, uid = q.data.split(":",1); uid = int(uid)
    item = pending_referrals.pop(uid, None)
    if not item:
        await q.edit_message_reply_markup(reply_markup=None); return
    users.setdefault(uid, {"name":"USER","username":None,"balance":0,"refer":0})
    users[uid].setdefault("refer", 0)
    if action == "ra_acc":
        users[uid]["refer"] += 1
        await ctx.bot.send_message(uid, "YOUR REFER HAS BEEN ACCEPTED ✅")
    else:
        await ctx.bot.send_message(uid, "YOUR REFER HAS BEEN REJECTED ❌")
    await q.edit_message_reply_markup(reply_markup=None)

async def show_admin(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    await kill_add(update.effective_user.id, ctx)
    ctx.user_data.pop("admin_step", None)
    ctx.user_data.pop("admin_tid",  None)
    w = "WELCOME OWNER 🔥" if is_owner(update.effective_user.id) else "WELCOME ADMIN 🔥"
    await update.message.reply_text(w, reply_markup=admin_kb())

async def add_acc_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Called when admin presses ADD ACCOUNT."""
    uid = update.effective_user.id
    await kill_add(uid, ctx)
    ctx.user_data["add_step"] = "number"
    await update.message.reply_text(
        "SEND ME THE NUMBER\n\n(WITH COUNTRY CODE EX +91XXXX")

async def add_acc_input(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> bool:
    """Handle text input during add-account flow. Returns True if consumed."""
    uid   = update.effective_user.id
    step  = ctx.user_data.get("add_step")
    value = (update.message.text or "").strip()
    if not step or not value: return False

    if step == "number":
        for existing_uid, existing_flow in list(add_flow.items()):
            if existing_flow.get("phone") == value:
                await dc(existing_flow.get("client"))
                del add_flow[existing_uid]
                break

        client = new_client()
        try:
            await client.connect()
            res = await client.send_code_request(value, force_sms=False)
            add_flow[uid] = {
                "client":     client,
                "phone":      value,
                "phone_hash": res.phone_code_hash,
                "session_str":"",
                "password":   None,
                "price":      None,
            }
            ctx.user_data["add_step"] = "otp"
            await update.message.reply_text(f"OTP SEND TO {value} ⚡\n\nSEND ME OTP")
        except PhoneNumberInvalidError:
            await dc(client)
            await update.message.reply_text("INVALID PHONE NUMBER ❌\n\nSEND ME THE NUMBER AGAIN")
        except FloodWaitError as e:
            await dc(client)
            await update.message.reply_text(f"FLOOD WAIT ❌ TRY AFTER {e.seconds} SECONDS")
        except Exception as e:
            await dc(client)
            await update.message.reply_text(f"ERROR: {e}\n\nSEND ME THE NUMBER AGAIN")
        return True

    if step == "otp":
        f = add_flow.get(uid)
        if not f:
            ctx.user_data.pop("add_step", None)
            await update.message.reply_text("SESSION EXPIRED. TAP ADD ACCOUNT AGAIN."); return True
        client = f["client"]
        try:
            await client.sign_in(phone=f["phone"], code=value,
                                  phone_code_hash=f["phone_hash"])
            f["session_str"] = client.session.save()
            await dc(client); f["client"] = None
            ctx.user_data["add_step"] = "password"
            await update.message.reply_text(
                "OTP VERIFIED ✅\n\nSEND ME THE BUYER PASSWORD\n\n(No password? Send - NONE)")
        except SessionPasswordNeededError:
            f["session_str"] = client.session.save()
            await dc(client); f["client"] = None
            ctx.user_data["add_step"] = "2fa"
            await update.message.reply_text("PASSWORD NEEDED 🔐\n\nSEND ME THE PASSWORD")
        except PhoneCodeInvalidError:
            await update.message.reply_text("WRONG OTP ❌\n\nSEND ME OTP AGAIN")
        except PhoneCodeExpiredError:
            await kill_add(uid, ctx)
            ctx.user_data["add_step"] = "number"
            await update.message.reply_text("OTP EXPIRED ❌\n\nSEND ME THE NUMBER AGAIN")
        except Exception as e:
            await update.message.reply_text(f"ERROR: {e}\n\nSEND ME OTP AGAIN")
        return True

    if step == "2fa":
        f = add_flow.get(uid)
        if not f:
            ctx.user_data.pop("add_step", None)
            await update.message.reply_text("SESSION EXPIRED. TAP ADD ACCOUNT AGAIN."); return True

        client = new_client(f["session_str"])
        try:
            await client.connect()
            await client.sign_in(password=value)
            f["session_str"] = client.session.save()
            await dc(client); f["client"] = None
            ctx.user_data["add_step"] = "password"
            await update.message.reply_text(
                "PASSWORD VERIFIED ✅\n\nSEND ME THE BUYER PASSWORD\n\n(NO PASSWORD SEND - NONE)")
        except Exception as e:
            await dc(client)
            await update.message.reply_text(
                f"WRONG PASSWORD ❌\n\n{e}\n\nSEND ME THE PASSWORD AGAIN")
        return True

    if step == "password":
        f = add_flow.get(uid)
        if not f:
            ctx.user_data.pop("add_step", None)
            await update.message.reply_text("SESSION EXPIRED. TAP ADD ACCOUNT AGAIN."); return True
        f["password"] = None if value.upper() == "NONE" else value
        ctx.user_data["add_step"] = "price"
        await update.message.reply_text("SEND ME THE AMOUNT")
        return True

    if step == "price":
        f = add_flow.get(uid)
        if not f:
            ctx.user_data.pop("add_step", None)
            await update.message.reply_text("SESSION EXPIRED. TAP ADD ACCOUNT AGAIN."); return True
        try:    price = float(value)
        except:
            await update.message.reply_text("PLEASE SEND A VALID AMOUNT"); return True
        f["price"] = price
        ctx.user_data["add_step"] = "btn_name"
        await update.message.reply_text("SEND ME THE BUTTON NAME")
        return True

    if step == "btn_name":
        f = add_flow.pop(uid, None)
        if not f:
            ctx.user_data.pop("add_step", None)
            await update.message.reply_text("SESSION EXPIRED. TAP ADD ACCOUNT AGAIN."); return True
        if value in accounts:
            add_flow[uid] = f
            await update.message.reply_text(
                f"⚠️ '{value}' ALREADY EXISTS\n\nSEND A DIFFERENT BUTTON NAME"); return True
        accounts[value] = {
            "number":      f["phone"],
            "session_str": f["session_str"],
            "password":    f["password"],
            "price":       f["price"],
        }
        ctx.user_data.pop("add_step", None)
        await update.message.reply_text("ACCOUNT SUCCESSFULLY ADDED ✅")
        return True

    return False

async def cb_rm_select(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not is_admin(q.from_user.id):
        await q.answer("ADMIN ONLY", show_alert=True); return
    await q.answer()
    name = q.data.split(":",1)[1]
    if name not in accounts:
        await q.edit_message_text("ACCOUNT NOT FOUND"); return
    await q.edit_message_text(
        f"REMOVE THIS ACCOUNT?\n\nBUTTON NAME - {name}",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ CONFIRM", callback_data=f"rm_ok:{name}"),
            InlineKeyboardButton("❌ CANCEL",  callback_data="rm_no"),
        ]]))

async def cb_rm_confirm(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not is_admin(q.from_user.id):
        await q.answer("ADMIN ONLY", show_alert=True); return
    await q.answer()
    name = q.data.split(":",1)[1]
    accounts.pop(name, None)
    await q.edit_message_text(f"'{name}' REMOVED ✅")

async def cb_rm_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    await q.edit_message_text("CANCELLED ❌")

# ══════════════════════════════════════════════════════
#  WS ADMIN — ADD FLOW
# ══════════════════════════════════════════════════════

async def ws_add_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    ws_add_flow.pop(uid, None)
    ctx.user_data["ws_add_step"] = "number"
    await update.message.reply_text(
        "SEND ME THE WS NUMBER\n\n(WITH COUNTRY CODE EX +91XXXXXXXXXX)")

async def ws_add_input(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> bool:
    uid   = update.effective_user.id
    step  = ctx.user_data.get("ws_add_step")
    value = (update.message.text or "").strip()
    if not step or not value: return False

    if step == "number":
        ws_add_flow[uid] = {"phone": value}
        ctx.user_data["ws_add_step"] = "otp_wait"
        try:
            result = await ws_post("/admin/add", {
                "phone":    value,
                "password": None,
                "price":    0,
                "btn_name": value,
            })
            if not result.get("ok"):
                err = result.get("error", "")
                # If account already exists in ws service, treat as session ready
                if err == "ALREADY_EXISTS":
                    ctx.user_data["ws_add_step"] = "password"
                    await update.message.reply_text("SESSION READY ✅\n\nSEND BUYER PASSWORD\n\n(No password? Send NONE)")
                    return True
                await update.message.reply_text(f"ERROR ❌\n\n{err}\n\nSEND NUMBER AGAIN")
                ctx.user_data.pop("ws_add_step", None)
                ws_add_flow.pop(uid, None)
                return True
            if result.get("status") == "ALREADY_AUTHORIZED":
                ctx.user_data["ws_add_step"] = "password"
                await update.message.reply_text("SESSION READY ✅\n\nSEND BUYER PASSWORD\n\n(No password? Send NONE)")
                return True
            # OTP_REQUIRED
            await update.message.reply_text(
                f"OTP SENT TO {value} VIA WHATSAPP ⚡\n\nSEND ME THE OTP")
            ctx.user_data["ws_add_step"] = "otp"
        except Exception as e:
            await update.message.reply_text(f"WS SERVICE ERROR ❌\n\n{e}")
            ctx.user_data.pop("ws_add_step", None)
            ws_add_flow.pop(uid, None)
        return True

    if step == "otp":
        f = ws_add_flow.get(uid)
        if not f:
            ctx.user_data.pop("ws_add_step", None)
            await update.message.reply_text("SESSION EXPIRED. TAP ADD WS AGAIN."); return True
        # OTP is auto-captured by the WS service; just confirm and move on
        await update.message.reply_text("OTP VERIFIED ✅\n\nSEND BUYER PASSWORD\n\n(No password? Send NONE)")
        ctx.user_data["ws_add_step"] = "password"
        return True

    if step == "password":
        f = ws_add_flow.get(uid)
        if not f:
            ctx.user_data.pop("ws_add_step", None)
            await update.message.reply_text("SESSION EXPIRED. TAP ADD WS AGAIN."); return True
        f["password"] = None if value.upper() == "NONE" else value
        ctx.user_data["ws_add_step"] = "price"
        await update.message.reply_text("SEND ME THE PRICE ₹")
        return True

    if step == "price":
        f = ws_add_flow.get(uid)
        if not f:
            ctx.user_data.pop("ws_add_step", None)
            await update.message.reply_text("SESSION EXPIRED. TAP ADD WS AGAIN."); return True
        try:    f["price"] = float(value)
        except:
            await update.message.reply_text("SEND A VALID AMOUNT"); return True
        ctx.user_data["ws_add_step"] = "btn_name"
        await update.message.reply_text("SEND ME THE BUTTON NAME")
        return True

    if step == "btn_name":
        f = ws_add_flow.pop(uid, None)
        if not f:
            ctx.user_data.pop("ws_add_step", None)
            await update.message.reply_text("SESSION EXPIRED. TAP ADD WS AGAIN."); return True
        if value in ws_accounts:
            ws_add_flow[uid] = f
            await update.message.reply_text(f"'{value}' ALREADY EXISTS ❌\n\nSEND A DIFFERENT NAME"); return True
        # Update WS service with final price + btn_name
        try:
            await ws_post("/admin/update", {
                "phone":    f["phone"],
                "password": f.get("password"),
                "price":    f["price"],
                "btn_name": value,
            })
        except Exception:
            pass
        ws_accounts[value] = {
            "phone":    f["phone"],
            "password": f.get("password"),
            "price":    f["price"],
            "btn_name": value,
        }
        ctx.user_data.pop("ws_add_step", None)
        await update.message.reply_text("WS ACCOUNT SUCCESSFULLY ADDED ✅")
        return True

    return False

# ── WS Remove callbacks ───────────────────────────────────────────────────────

async def cb_wsrm_select(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not is_admin(q.from_user.id):
        await q.answer("ADMIN ONLY", show_alert=True); return
    await q.answer()
    name = q.data.split(":", 1)[1]
    if name not in ws_accounts:
        await q.edit_message_text("WS ACCOUNT NOT FOUND ❌"); return
    await q.edit_message_text(
        f"REMOVE THIS WS ACCOUNT?\n\nBUTTON NAME - {name}",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ CONFIRM", callback_data=f"wsrm_ok:{name}"),
            InlineKeyboardButton("❌ CANCEL",  callback_data="wsrm_no"),
        ]]))

async def cb_wsrm_confirm(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not is_admin(q.from_user.id):
        await q.answer("ADMIN ONLY", show_alert=True); return
    await q.answer()
    name = q.data.split(":", 1)[1]
    acc  = ws_accounts.pop(name, None)
    if acc:
        try:
            await ws_post("/admin/remove", {"phone": acc["phone"]})
        except Exception:
            pass
    await q.edit_message_text(f"'{name}' WS ACCOUNT REMOVED ✅")

async def cb_wsrm_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    await q.edit_message_text("CANCELLED ❌")

# ══════════════════════════════════════════════════════
#  WS BUYER FLOW
# ══════════════════════════════════════════════════════

async def show_buy_ws(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ws_accounts:
        await update.message.reply_text("AVAILABLE STOCK ⚡\n\nNO ACCOUNTS AVAILABLE RIGHT NOW"); return
    btns = [[InlineKeyboardButton(n, callback_data=f"wsbs:{n}")] for n in ws_accounts]
    await update.message.reply_text("AVAILABLE STOCK ⚡",
                                    reply_markup=InlineKeyboardMarkup(btns))

async def cb_ws_buy_select(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q    = update.callback_query
    res  = await force_join_check(q.from_user, ctx.bot)
    if res is not True:
        await q.answer()
        await q.message.reply_text(
            "FIRST JOIN ALL CHANNEL TO USE THIS BOT AFTER JOIN CLICK ON CONTINUE 🔥",
            reply_markup=res[1]); return
    await q.answer()
    name = q.data.split(":", 1)[1]
    u    = q.from_user; ensure_user(u)
    if name not in ws_accounts:
        await q.edit_message_text("THIS WS ACCOUNT IS NO LONGER AVAILABLE ❌"); return
    acc = ws_accounts[name]
    bal = users[u.id]["balance"]
    if bal < acc["price"]:
        await q.edit_message_text(
            f"YOUR BALANCE - {bal} 💰\n\n"
            f"ACCOUNT PRICE - {acc['price']} 💵\n\n"
            f"YOU NEED {acc['price']-bal:.2f} MORE ❌"); return
    await q.edit_message_text(
        f"WS ACCOUNT PRICE - {acc['price']} 💵\n\n"
        f"YOUR BALANCE - {bal} 💰\n\n"
        "CONFIRM PURCHASE?",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ CONFIRM", callback_data=f"wsbc:{name}"),
            InlineKeyboardButton("❌ CANCEL",  callback_data="wsbx"),
        ]]))

async def cb_ws_buy_confirm(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    import time as _time
    q    = update.callback_query
    res  = await force_join_check(q.from_user, ctx.bot)
    if res is not True:
        await q.answer()
        await q.message.reply_text(
            "FIRST JOIN ALL CHANNEL TO USE THIS BOT AFTER JOIN CLICK ON CONTINUE 🔥",
            reply_markup=res[1]); return
    await q.answer()
    name = q.data.split(":", 1)[1]
    u    = q.from_user; ensure_user(u)

    if name not in ws_accounts:
        await q.edit_message_text("THIS WS ACCOUNT IS NO LONGER AVAILABLE ❌"); return

    acc = ws_accounts[name]
    bal = users[u.id]["balance"]
    if bal < acc["price"]:
        await q.edit_message_text(
            f"YOUR BALANCE - {bal} 💰\n\n"
            f"ACCOUNT PRICE - {acc['price']} 💵\n\n"
            f"YOU NEED {acc['price']-bal:.2f} MORE ❌"); return

    # Deduct balance and reserve account
    users[u.id]["balance"] = bal - acc["price"]
    ws_accounts.pop(name)

    purchase_ts = int(_time.time() * 1000)
    ws_buy_flow[u.id] = {
        "phone":       acc["phone"],
        "price":       acc["price"],
        "password":    acc.get("password"),
        "purchase_ts": purchase_ts,
    }
    ctx.user_data["ws_buy_otp"] = True

    async def _ws_auto_deliver():
        import time as _t
        deadline = _t.time() + 120   # 2 minute window
        code = None
        while _t.time() < deadline:
            await asyncio.sleep(5)
            if u.id not in ws_buy_flow:
                return
            try:
                result = await ws_post("/buy/get_otp", {
                    "phone":    acc["phone"],
                    "after_ts": purchase_ts,
                })
                if result.get("ok"):
                    code     = result["code"]
                    password = result.get("password")
                    break
            except Exception:
                pass

        ws_buy_flow.pop(u.id, None)
        ctx.user_data["ws_buy_otp"] = False

        if code:
            pw_line = f"\n\nPASSWORD - {password} 🔑" if password else ""
            await ctx.bot.send_message(
                u.id,
                f"WS OTP RECEIVED 💎\n\nYOUR OTP - {code} ⚡{pw_line}\n\nTHANKS FOR BUY ❤️")
        else:
            await ctx.bot.send_message(
                u.id,
                "OTP NOT RECEIVED ⏳\n\nCONTACT ADMIN FOR HELP ❌")

    asyncio.ensure_future(_ws_auto_deliver())

    await q.edit_message_text(
        f"YOUR WS NUMBER ⚡\n\nNUMBER - {acc['phone']} 🔥\n\nFAST SEND OTP ON THIS NUMBER VIA WHATSAPP ⚡\n\nOTP WILL BE DELIVERED AUTOMATICALLY 🔥")

async def cb_ws_buy_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    ws_buy_flow.pop(q.from_user.id, None)
    ctx.user_data["ws_buy_otp"] = False
    await q.edit_message_text("PURCHASE CANCELLED ❌")

async def money_admin_input(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> bool:
    """Handle ADD MONEY / REMOVE MONEY / ADD ADMIN / REMOVE ADMIN text steps."""
    uid   = update.effective_user.id
    step  = ctx.user_data.get("admin_step")
    value = (update.message.text or "").strip()
    if not step or not value: return False

    if step == "add_money_uid":
        try:    tid = int(value)
        except:
            await update.message.reply_text("SEND A VALID USER ID"); return True
        users.setdefault(tid, {"name":"USER","username":None,"balance":0,"refer":0})
        ctx.user_data["admin_tid"]  = tid
        ctx.user_data["admin_step"] = "add_money_amt"
        await update.message.reply_text("SEND ME THE AMOUNT"); return True

    if step == "add_money_amt":
        try:    amt = float(value)
        except:
            await update.message.reply_text("SEND A VALID AMOUNT"); return True
        tid = ctx.user_data.pop("admin_tid", None)
        users.setdefault(tid, {"name":"USER","username":None,"balance":0,"refer":0})
        users[tid]["balance"] = users[tid].get("balance",0) + amt
        ctx.user_data.pop("admin_step", None)
        await update.message.reply_text(f"✅ ADDED ₹{amt} TO USER {tid}"); return True

    if step == "rm_money_uid":
        try:    tid = int(value)
        except:
            await update.message.reply_text("SEND A VALID USER ID"); return True
        users.setdefault(tid, {"name":"USER","username":None,"balance":0,"refer":0})
        ctx.user_data["admin_tid"]  = tid
        ctx.user_data["admin_step"] = "rm_money_amt"
        await update.message.reply_text("SEND ME THE AMOUNT"); return True

    if step == "rm_money_amt":
        try:    amt = float(value)
        except:
            await update.message.reply_text("SEND A VALID AMOUNT"); return True
        tid = ctx.user_data.pop("admin_tid", None)
        users.setdefault(tid, {"name":"USER","username":None,"balance":0,"refer":0})
        cur = users[tid].get("balance",0)
        if cur < amt:
            await update.message.reply_text("NOT ENOUGH BALANCE"); return True
        users[tid]["balance"] = cur - amt
        ctx.user_data.pop("admin_step", None)
        await update.message.reply_text(f"✅ REMOVED ₹{amt} FROM USER {tid}"); return True

    if step == "add_admin_uid":
        try:    nid = int(value)
        except:
            await update.message.reply_text("SEND A VALID USER ID"); return True
        admins.add(nid)
        ctx.user_data.pop("admin_step", None)
        await update.message.reply_text(f"✅ USER {nid} IS NOW AN ADMIN"); return True

    if step == "rm_admin_uid":
        try:    aid = int(value)
        except:
            await update.message.reply_text("SEND A VALID USER ID"); return True
        if aid == OWNER_ID:
            await update.message.reply_text("OWNER CANNOT BE REMOVED"); return True
        if aid not in admins:
            await update.message.reply_text("USER IS NOT AN ADMIN"); return True
        admins.discard(aid)
        ctx.user_data.pop("admin_step", None)
        await update.message.reply_text(f"✅ USER {aid} IS NO LONGER AN ADMIN"); return True

    return False

async def show_buy_account(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not accounts:
        await update.message.reply_text(
            "AVAILABLE STOCK ⚡\n\nNO ACCOUNTS AVAILABLE RIGHT NOW"); return
    btns = [[InlineKeyboardButton(n, callback_data=f"bs:{n}")] for n in accounts]
    await update.message.reply_text("AVAILABLE STOCK ⚡",
                                    reply_markup=InlineKeyboardMarkup(btns))

async def cb_buy_select(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    res = await force_join_check(q.from_user, ctx.bot)
    if res is not True:
        await q.answer()
        await q.message.reply_text(
            "FIRST JOIN ALL CHANNEL TO USE THIS BOT AFTER JOIN CLICK ON CONTINUE 🔥",
            reply_markup=res[1]); return
    await q.answer()
    name = q.data.split(":",1)[1]
    u    = q.from_user; ensure_user(u)
    if name not in accounts:
        await q.edit_message_text(
            "ANOTHER USER IS CURRENTLY INTERESTED IN THIS ACCOUNT. IF THEY DO NOT COMPLETE THE PURCHASE, THIS ACCOUNT WILL BE AVAILABLE FOR YOU.\n\n"
            "IF THEY PURCHASE IT, YOU CAN SELECT ANOTHER ACCOUNT FROM THE AVAILABLE LIST.\n\n\n"
            "THANK YOU FOR UNDERSTANDING. ❤️"
        ); return
    acc  = accounts[name]
    bal  = users[u.id]["balance"]
    if bal < acc["price"]:
        await q.edit_message_text(
            f"YOUR BALANCE - {bal} 💰\n\n"
            f"ACCOUNT PRICE - {acc['price']} 💵\n\n"
            f"YOU NEED {acc['price']-bal:.2f} MORE ❌"); return
    await q.edit_message_text(
        f"ACCOUNT PRICE - {acc['price']} 💵\n\n"
        f"YOUR BALANCE - {bal} 💰\n\n"
        "CONFIRM PURCHASE?",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ CONFIRM", callback_data=f"bc:{name}"),
            InlineKeyboardButton("❌ CANCEL",  callback_data="bx"),
        ]]))

async def cb_buy_confirm(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q    = update.callback_query
    res  = await force_join_check(q.from_user, ctx.bot)
    if res is not True:
        await q.answer()
        await q.message.reply_text(
            "FIRST JOIN ALL CHANNEL TO USE THIS BOT AFTER JOIN CLICK ON CONTINUE 🔥",
            reply_markup=res[1]); return
    await q.answer()
    name = q.data.split(":",1)[1]
    u    = q.from_user; ensure_user(u)

    await kill_buy(u.id)

    lock = _get_account_lock(name)
    async with lock:
        if name not in accounts:
            await q.edit_message_text(
                "ANOTHER USER IS CURRENTLY INTERESTED IN THIS ACCOUNT. IF THEY DO NOT COMPLETE THE PURCHASE, THIS ACCOUNT WILL BE AVAILABLE FOR YOU.\n\n"
                "IF THEY PURCHASE IT, YOU CAN SELECT ANOTHER ACCOUNT FROM THE AVAILABLE LIST.\n\n"
                "THANK YOU FOR UNDERSTANDING. ❤️"
            )
            return

        acc = accounts[name]
        bal = users[u.id]["balance"]
        if bal < acc["price"]:
            await q.edit_message_text(
                f"YOUR BALANCE - {bal} 💰\n\n"
                f"ACCOUNT PRICE - {acc['price']} 💵\n\n"
                f"YOU NEED {acc['price']-bal:.2f} MORE ❌"); return

        session_str = acc.get("session_str", "")
        if not session_str:
            await q.edit_message_text(
                "THIS ACCOUNT HAS NO SESSION ❌\n\nCONTACT ADMIN"); return

        client = new_client(session_str)
        try:
            await client.connect()
            if not await client.is_user_authorized():
                await dc(client)
                await q.edit_message_text(
                    "THIS ACCOUNT SESSION IS DEAD ❌\n\nCONTACT ADMIN"); return
        except Exception as e:
            await dc(client)
            await q.edit_message_text(f"ERROR OPENING ACCOUNT ❌\n\n{e}\n\nCONTACT ADMIN"); return

        from datetime import timezone
        users[u.id]["balance"] = bal - acc["price"]

        accounts.pop(name)

        buy_flow[u.id] = {
            "btn_name":     name,
            "client":       client,
            "phone":        acc["number"],
            "price":        acc["price"],
            "acc_data":     acc,
            "purchase_time": datetime.now(timezone.utc),
        }
        ctx.user_data["buy_otp"] = True

    async def _auto_deliver():
        import re as _re
        from datetime import timezone
        cl = client
        purchase_time = datetime.now(timezone.utc)

        _acc = buy_flow.get(u.id, {}).get("acc_data", acc)
        try:
            code = None
            for _ in range(24):
                await asyncio.sleep(5)

                if u.id not in buy_flow:
                    return

                try:
                    if not cl.is_connected():
                        await cl.connect()
                except Exception:
                    pass

                try:
                    async for msg in cl.iter_messages(777000, limit=5):
                        if msg.date and msg.date.replace(tzinfo=timezone.utc) < purchase_time:
                            continue
                        body = msg.message or ""
                        m = _re.search(r"[Ll]ogin code[:\s]*([0-9]{4,6})", body)
                        if not m:
                            m = _re.search(r"\b([0-9]{5,6})\b", body)
                        if m:
                            code = m.group(1)
                            break
                except Exception:
                    pass
                if code:
                    break

            if u.id not in buy_flow:
                return
            buy_flow.pop(u.id, None)
            ctx.user_data["buy_otp"] = False

            if code:
                pw_line = f"\n\nPASSWORD - {_acc['password']} 🔑" if _acc.get("password") else ""
                await ctx.bot.send_message(
                    u.id,
                    f"OTP RECEIVED 💎\n\nYOUR OTP - {code} ⚡{pw_line}\n\nTHANKS FOR BUY ❤️")
            else:
                await ctx.bot.send_message(
                    u.id,
                    "OTP NOT RECEIVED ⏳\n\nCONTACT ADMIN FOR HELP ❌")

            try:
                if cl.is_connected():
                    await cl.log_out()
            except Exception:
                pass
            finally:
                await dc(cl)

        except Exception as e:
            buy_flow.pop(u.id, None)
            ctx.user_data["buy_otp"] = False
            await dc(cl)
            try:
                await ctx.bot.send_message(u.id, f"ERROR ❌\n\n{e}\n\nCONTACT ADMIN")
            except Exception:
                pass

    asyncio.ensure_future(_auto_deliver())

    await q.edit_message_text(
        f"YOUR NUMBER ⚡\n\nNUMBER - {acc['number']} 🔥\n\nFAST SEND OTP ⚡")

async def cb_buy_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    res = await force_join_check(q.from_user, ctx.bot)
    if res is not True:
        await q.answer()
        await q.message.reply_text(
            "FIRST JOIN ALL CHANNEL TO USE THIS BOT AFTER JOIN CLICK ON CONTINUE 🔥",
            reply_markup=res[1]); return
    await q.answer()
    await kill_buy(q.from_user.id, ctx)
    await q.edit_message_text("PURCHASE CANCELLED ❌")

async def _extract_login_code(client, after_dt=None):
    """
    Read the most recent Telegram login code from the account's own
    'Telegram' service chat (user id 777000).
    If after_dt (datetime, UTC-aware) is provided, only messages newer than
    that timestamp are considered — prevents returning a stale/replayed code.
    Returns the code string or None.
    """
    import re as _re
    from datetime import timezone
    try:
        if not client.is_connected():
            await client.connect()

        async for msg in client.iter_messages(777000, limit=10):
            if after_dt and msg.date:
                msg_dt = msg.date.replace(tzinfo=timezone.utc)
                if msg_dt < after_dt:
                    break
            body = msg.message or ""
            m = _re.search(r"[Ll]ogin code[:\s]*([0-9]{4,6})", body)
            if not m:
                m = _re.search(r"\b([0-9]{5,6})\b", body)
            if m:
                return m.group(1)
    except Exception:
        pass
    return None

async def handle_buy_otp(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> bool:
    """
    User has requested a login code in their own Telegram app.
    Bot reads the code from inside the sold account's session and returns it.
    Only codes that arrived after the purchase time are accepted.
    Returns True if the message was consumed.
    """
    from datetime import timezone
    u = update.effective_user
    f = buy_flow.get(u.id)
    if not f: return False

    client    = f["client"]
    name      = f["btn_name"]
    acc       = f.get("acc_data", accounts.get(name, {}))
    after_dt  = f.get("purchase_time")

    try:
        code = await _extract_login_code(client, after_dt=after_dt)
    except Exception as e:
        await update.message.reply_text(
            f"ERROR READING OTP ❌\n\n{e}\n\nTRY AGAIN — SEND ANYTHING"); return True

    if not code:
        await update.message.reply_text(
            "OTP NOT FOUND YET ⏳\n\n"
            "FIRST REQUEST THE LOGIN CODE IN YOUR TELEGRAM APP,\n"
            "THEN SEND ME ANYTHING AGAIN ⚡"); return True

    buy_flow.pop(u.id, None)
    ctx.user_data["buy_otp"] = False

    try:
        if client.is_connected():
            await client.log_out()
    except Exception:
        pass
    finally:
        await dc(client)

    pw = f"\n\nPASSWORD - {acc['password']} 🔑" if acc.get("password") else ""
    await update.message.reply_text(
        f"OTP RECEIVED 💎\n\nYOUR OTP - {code} ⚡{pw}\n\nTHANKS FOR BUY ❤️")
    return True

async def handle_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if is_banned(update.effective_user.id):
        await update.message.reply_text("YOU ARE BANNED FROM USING THIS BOT"); return

    res = await force_join_check(update.effective_user, ctx.bot)
    if res is not True:
        await update.message.reply_text(
            "FIRST JOIN ALL CHANNEL TO USE THIS BOT AFTER JOIN CLICK ON CONTINUE 🔥",
            reply_markup=res[1]); return
    if ctx.user_data.get("admin_step") == "add_upi_qr" and is_owner(update.effective_user.id):
        await handle_upi_qr_set(update, ctx); return
    if ctx.user_data.get("await_pay_proof"):
        await handle_pay_proof(update, ctx); return
    if ctx.user_data.get("await_ref_proof"):
        await handle_ref_proof(update, ctx); return

async def handle_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u    = update.effective_user
    text = (update.message.text or "").strip()

    if is_banned(u.id):
        await update.message.reply_text("YOU ARE BANNED FROM USING THIS BOT"); return

    res = await force_join_check(u, ctx.bot)
    if res is not True:
        await update.message.reply_text(
            "FIRST JOIN ALL CHANNEL TO USE THIS BOT AFTER JOIN CLICK ON CONTINUE 🔥",
            reply_markup=res[1]); return

    if text in ALL_BTNS:
        await kill_buy(u.id, ctx)
        if is_admin(u.id):
            await kill_add(u.id, ctx)
            ctx.user_data.pop("admin_step", None)
            ctx.user_data.pop("admin_tid",  None)
        ctx.user_data["await_pay_proof"] = False
        ctx.user_data["await_ref_proof"] = False

    if ctx.user_data.get("await_ref_proof") and text not in ALL_BTNS:
        await update.message.reply_text(
            "SEND SCREENSHOT WITH THE REFERRED USER ID AS THE PHOTO CAPTION"); return
    if ctx.user_data.get("await_pay_proof") and text not in ALL_BTNS:
        await update.message.reply_text("SEND ME SCREENSHOT WITH ACCOUNT NAME AND AMOUNT"); return

    if ctx.user_data.get("buy_otp") and text not in ALL_BTNS:
        if await handle_buy_otp(update, ctx): return

    if is_admin(u.id):

        if text == "BACK":
            await update.message.reply_text("WELCOME TO THE BOT 🔥",
                                            reply_markup=main_kb(u.id)); return

        if text in ("ADD ACCOUNT", "ADD TG"):
            await add_acc_start(update, ctx); return

        if text in ("REMOVE ACCOUNT", "REMOVE TG"):
            if not accounts:
                await update.message.reply_text("NO TG ACCOUNTS ADDED ❌"); return
            btns = [[InlineKeyboardButton(n, callback_data=f"rm:{n}")] for n in accounts]
            await update.message.reply_text("SELECT TG ACCOUNT TO REMOVE",
                                            reply_markup=InlineKeyboardMarkup(btns)); return

        if text == "ADD WS":
            await ws_add_start(update, ctx); return

        if text == "REMOVE WS":
            if not ws_accounts:
                await update.message.reply_text("NO WS ACCOUNTS ADDED ❌"); return
            btns = [[InlineKeyboardButton(n, callback_data=f"wsrm:{n}")] for n in ws_accounts]
            await update.message.reply_text("SELECT WS ACCOUNT TO REMOVE",
                                            reply_markup=InlineKeyboardMarkup(btns)); return

        if text == "ADD MONEY":
            ctx.user_data["admin_step"] = "add_money_uid"
            await update.message.reply_text("SEND ME THE USER ID"); return

        if text == "REMOVE MONEY":
            ctx.user_data["admin_step"] = "rm_money_uid"
            await update.message.reply_text("SEND ME THE USER ID"); return

        if text == "ADD UPI":
            if not is_owner(u.id):
                await update.message.reply_text("OWNER ONLY"); return
            ctx.user_data["admin_step"] = "add_upi_qr"
            await update.message.reply_text("SEND ME QR"); return

        if text == "REMOVE UPI":
            if not is_owner(u.id):
                await update.message.reply_text("OWNER ONLY"); return
            global upi_qr_file_id
            if not upi_qr_file_id:
                await update.message.reply_text("NO QR IS SET RIGHT NOW ❌"); return
            await update.message.reply_text(
                "ARE YOU SURE YOU WANT TO REMOVE QR",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("YES ✅", callback_data="upi_rm_yes"),
                    InlineKeyboardButton("NO ❌",  callback_data="upi_rm_no"),
                ]])); return

        if ctx.user_data.get("add_step"):
            if await add_acc_input(update, ctx): return

        if ctx.user_data.get("ws_add_step"):
            if await ws_add_input(update, ctx): return

        if ctx.user_data.get("admin_step"):
            if await money_admin_input(update, ctx): return

    if text in ("BUY ACCOUNT", "BUY TG"):
        await show_buy_account(update, ctx); return
    if text == "BUY WS":
        await show_buy_ws(update, ctx); return
    if text == "DIPOSIT":
        await show_deposit(update, ctx); return
    if text == "PROFILE":
        await show_profile(update, ctx); return
    if text == "REFER":
        await show_refer(update, ctx); return
    if text == "BOT OWNER":
        await update.message.reply_text(
            "CLICK THE OWNER BUTTON TO SEE THE OWNER 👑",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("OWNER 👑", url=f"tg://user?id={OWNER_ID}"),
            ]])); return
    if text == "ADMIN" and is_admin(u.id):
        await show_admin(update, ctx); return

async def cmd_ban(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    if len(ctx.args) != 1:
        await update.message.reply_text("USAGE: /ban USERID"); return
    try:    tid = int(ctx.args[0])
    except: await update.message.reply_text("INVALID USER ID"); return
    if tid == OWNER_ID:
        await update.message.reply_text("OWNER CANNOT BE BANNED"); return
    banned_users.add(tid)
    await update.message.reply_text("USER BANNED ✅")

async def cmd_unban(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    if len(ctx.args) != 1:
        await update.message.reply_text("USAGE: /unban USERID"); return
    try:    tid = int(ctx.args[0])
    except: await update.message.reply_text("INVALID USER ID"); return
    banned_users.discard(tid)
    await update.message.reply_text("USER UNBANNED ✅")

async def cmd_balance(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    rich = sorted(
        [(uid, d) for uid, d in users.items() if d.get("balance", 0) > 0],
        key=lambda x: x[1].get("balance", 0),
        reverse=True,
    )
    if not rich:
        await update.message.reply_text("NO USER HAS BALANCE YET ❌"); return
    lines = ["USER BALANCE ⚡\n"]
    for i, (uid, d) in enumerate(rich, 1):
        name = str(d.get("name") or "USER").strip()
        bal  = d.get("balance", 0)
        lines.append(f"{i}  {name}  {bal}")
    await update.message.reply_text("\n".join(lines))

async def cmd_broadcast(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    payload = (update.message.text or "")[len("/broadcast"):].strip()
    if not payload:
        await update.message.reply_text(
            "USAGE: /broadcast MESSAGE\nOR /broadcast USERID MESSAGE"); return
    tid = None; msg = payload
    first, *rest = payload.split(maxsplit=1)
    try:
        pid = int(first)
        if rest: tid = pid; msg = rest[0]
    except ValueError: pass
    if tid is not None:
        if is_banned(tid):
            await update.message.reply_text("USER IS BANNED"); return
        try:
            await ctx.bot.send_message(tid, msg)
            await update.message.reply_text("MESSAGE SENT ✅")
        except:
            await update.message.reply_text("COULD NOT SEND MESSAGE")
        return
    sent = failed = 0
    for uid in list(users):
        if is_banned(uid): continue
        try:
            await ctx.bot.send_message(uid, msg); sent += 1
        except: failed += 1
    await update.message.reply_text(f"BROADCAST DONE\nSENT - {sent}\nFAILED - {failed}")

def main():
    import subprocess, sys, os, atexit, shutil, glob

    # ── Node.js detection ──────────────────────────────────────────
    print(f"[WS] PATH = {os.environ.get('PATH','NOT SET')}", flush=True)

    def _find_node():
        # 1. PATH via shutil
        n = shutil.which("node") or shutil.which("nodejs")
        if n: return n
        # 2. mise installs directory — Railway uses mise
        for p in sorted(glob.glob("/root/.local/share/mise/installs/node/*/bin/node"), reverse=True):
            if os.access(p, os.X_OK): return p
        for p in sorted(glob.glob("/home/**/.local/share/mise/installs/node/*/bin/node"), reverse=True):
            if os.access(p, os.X_OK): return p
        # 3. mise shims
        for p in ["/mise/shims/node", "/root/.local/share/mise/shims/node",
                  "/home/user/.local/share/mise/shims/node"]:
            if os.path.isfile(p) and os.access(p, os.X_OK): return p
        # 4. Standard locations
        for p in ["/usr/bin/node", "/usr/local/bin/node",
                  "/usr/bin/nodejs", "/usr/local/bin/nodejs",
                  "/root/.nix-profile/bin/node",
                  "/nix/var/nix/profiles/default/bin/node"]:
            if os.path.isfile(p) and os.access(p, os.X_OK): return p
        # 5. Nix store glob
        hits = sorted(glob.glob("/nix/store/*/bin/node"))
        if hits: return hits[-1]
        # 6. find entire filesystem — last resort
        try:
            r = subprocess.run(
                ["find", "/", "-name", "node", "-type", "f", "-path", "*/bin/node"],
                capture_output=True, text=True, timeout=15
            )
            found = [l for l in r.stdout.splitlines() if l.endswith("/bin/node") and os.access(l, os.X_OK)]
            if found: return found[0]
        except Exception: pass
        return None

    def _install_node_via_mise():
        mise = shutil.which("mise")
        if not mise: return None
        try:
            print("[WS] Installing node via mise...", flush=True)
            subprocess.run([mise, "install", "node@20"], check=True, timeout=120)
            return _find_node()
        except Exception as e:
            print(f"[WS] mise install failed: {e}", flush=True)
            return None

    node_bin = _find_node()
    if not node_bin:
        print("[WS] node not in PATH — trying mise install...", flush=True)
        node_bin = _install_node_via_mise()
    print(f"[WS] node_bin = {node_bin}", flush=True)
    if not node_bin:
        print("[WS] ERROR: node not found — cannot start WS service", flush=True)
        sys.exit(1)

    # npm install if baileys missing
    pkg_dir = os.path.dirname(os.path.abspath(__file__))
    baileys_dir = os.path.join(pkg_dir, "node_modules", "@whiskeysockets", "baileys")
    if not os.path.isdir(baileys_dir):
        print("[WS] baileys not found — running npm install...", flush=True)
        npm_bin = shutil.which("npm") or os.path.join(os.path.dirname(node_bin), "npm")
        if not npm_bin or not os.path.isfile(npm_bin):
            npm_bin = node_bin.replace("/node", "/npm")
        subprocess.run([npm_bin, "install", "--legacy-peer-deps"], cwd=pkg_dir, check=True)
        print("[WS] npm install done", flush=True)
    else:
        print("[WS] node_modules OK", flush=True)
    ws_script = _write_ws_service()
    ws_proc = subprocess.Popen(
        [node_bin, ws_script],
        stdout=sys.stdout,
        stderr=sys.stderr,
    )
    atexit.register(ws_proc.terminate)
    print(f"[WS] Service started with '{node_bin}' (PID {ws_proc.pid})")

    app = ApplicationBuilder().token(TOKEN).build()

    app.add_handler(CommandHandler("start",     cmd_start))
    app.add_handler(CommandHandler("ban",       cmd_ban))
    app.add_handler(CommandHandler("unban",     cmd_unban))
    app.add_handler(CommandHandler("balance",   cmd_balance))
    app.add_handler(CommandHandler("broadcast", cmd_broadcast))

    app.add_handler(CallbackQueryHandler(cb_check_join,  pattern=r"^cj$"))
    app.add_handler(CallbackQueryHandler(cb_referral,    pattern=r"^(ref_proof|ref_board|ref_back)$"))
    app.add_handler(CallbackQueryHandler(cb_ref_admin,   pattern=r"^ra_(acc|rej):\d+$"))
    app.add_handler(CallbackQueryHandler(cb_pay_submit,  pattern=r"^pay_submit$"))
    app.add_handler(CallbackQueryHandler(cb_pay_admin,   pattern=r"^pa_(acc|rej):\d+$"))
    app.add_handler(CallbackQueryHandler(cb_deposit,     pattern=r"^(dep_inr|dep_usdt|dep_back)$"))
    app.add_handler(CallbackQueryHandler(cb_buy_select,  pattern=r"^bs:.+$"))
    app.add_handler(CallbackQueryHandler(cb_buy_confirm, pattern=r"^bc:.+$"))
    app.add_handler(CallbackQueryHandler(cb_buy_cancel,  pattern=r"^bx$"))
    app.add_handler(CallbackQueryHandler(cb_rm_select,   pattern=r"^rm:.+$"))
    app.add_handler(CallbackQueryHandler(cb_rm_confirm,  pattern=r"^rm_ok:.+$"))
    app.add_handler(CallbackQueryHandler(cb_rm_cancel,   pattern=r"^rm_no$"))
    app.add_handler(CallbackQueryHandler(cb_upi_remove,    pattern=r"^upi_rm_(yes|no)$"))
    app.add_handler(CallbackQueryHandler(cb_wsrm_select,   pattern=r"^wsrm:.+$"))
    app.add_handler(CallbackQueryHandler(cb_wsrm_confirm,  pattern=r"^wsrm_ok:.+$"))
    app.add_handler(CallbackQueryHandler(cb_wsrm_cancel,   pattern=r"^wsrm_no$"))
    app.add_handler(CallbackQueryHandler(cb_ws_buy_select, pattern=r"^wsbs:.+$"))
    app.add_handler(CallbackQueryHandler(cb_ws_buy_confirm,pattern=r"^wsbc:.+$"))
    app.add_handler(CallbackQueryHandler(cb_ws_buy_cancel, pattern=r"^wsbx$"))

    app.add_handler(MessageHandler(filters.PHOTO,                   handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    print("Bot is running...")
    app.run_polling()

if __name__ == "__main__":
    main()
