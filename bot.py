import os, json, time, html as _html, sqlite3, asyncio, logging
from urllib.parse import quote
import httpx
from aiohttp import web
from telegram import Update, InlineKeyboardButton as B, InlineKeyboardMarkup as M, ReplyKeyboardMarkup
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import (Application, CommandHandler, MessageHandler, CallbackQueryHandler,
                          ContextTypes, filters)

logging.basicConfig(level=logging.INFO)
TOKEN = os.environ["9295622848:AAF_xdr2yZhxCdObLDfU1wXBO6dkeFWfl5Y"]
ADMINS = {int(x) for x in os.environ.get("8061612320", "").split(",") if x.strip()}
DB = os.environ.get("DB_PATH", "bot.db")
PORT = int(os.environ.get("PORT", "8080"))
BASE_URL = os.environ.get("BASE_URL", "").rstrip("/")
PB_KEY = os.environ.get("POSTBACK_KEY", "change-this-secret")
BOT_NAME = os.environ.get("BOT_NAME", "TaskPay")
HTML = ParseMode.HTML
esc = _html.escape


def now():
    return int(time.time())


def money(x):
    return f"₹{float(x):.2f}"


def q(sql, args=(), one=False, w=False):
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    try:
        cur = con.execute(sql, args)
        if w:
            con.commit()
            return cur.lastrowid
        return cur.fetchone() if one else cur.fetchall()
    finally:
        con.close()


def execrc(sql, args=()):
    con = sqlite3.connect(DB)
    try:
        cur = con.execute(sql, args)
        con.commit()
        return cur.rowcount
    finally:
        con.close()


def init_db():
    q("""CREATE TABLE IF NOT EXISTS users(tg_id INTEGER PRIMARY KEY, username TEXT, name TEXT,
         balance REAL DEFAULT 0, upi TEXT, banned INTEGER DEFAULT 0, joined INTEGER, referred_by INTEGER)""", w=True)
    q("""CREATE TABLE IF NOT EXISTS tasks(id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT, descr TEXT,
         reward REAL, link TEXT, kind TEXT, target TEXT, vmethod TEXT, verify_url TEXT, keyword TEXT,
         hours INTEGER DEFAULT 0, expires INTEGER DEFAULT 0, active INTEGER DEFAULT 1, created INTEGER)""", w=True)
    q("""CREATE TABLE IF NOT EXISTS subs(id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, task_id INTEGER,
         proof TEXT, status TEXT, created INTEGER)""", w=True)
    q("""CREATE TABLE IF NOT EXISTS wd(id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, amount REAL,
         upi TEXT, status TEXT DEFAULT 'pending', note TEXT, created INTEGER)""", w=True)
    q("""CREATE TABLE IF NOT EXISTS gateways(id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, url TEXT,
         method TEXT, body TEXT, headers TEXT, keyword TEXT)""", w=True)
    q("""CREATE TABLE IF NOT EXISTS channels(id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT, ref TEXT, link TEXT)""", w=True)
    q("CREATE TABLE IF NOT EXISTS pb(task_id INTEGER, uid INTEGER, created INTEGER, PRIMARY KEY(task_id,uid))", w=True)
    q("CREATE TABLE IF NOT EXISTS settings(k TEXT PRIMARY KEY, v TEXT)", w=True)


def get(k, d=""):
    r = q("SELECT v FROM settings WHERE k=?", (k,), one=True)
    return r["v"] if r else d


def put(k, v):
    q("INSERT OR REPLACE INTO settings VALUES(?,?)", (k, str(v)), w=True)


def user(uid):
    return q("SELECT * FROM users WHERE tg_id=?", (uid,), one=True)


def add_bal(uid, amt):
    q("UPDATE users SET balance=balance+? WHERE tg_id=?", (amt, uid), w=True)


def deduct(uid, amt):
    return execrc("UPDATE users SET balance=balance-? WHERE tg_id=? AND balance>=?", (amt, uid, amt)) == 1


def is_admin(uid):
    return uid in ADMINS


def cid(x):
    x = str(x).strip()
    return int(x) if x.lstrip("-").isdigit() else (x if x.startswith("@") else "@" + x)


def mask(uid):
    s = str(uid)
    return f"{s[:3]}•••{s[-2:]}"


def fdate(ts):
    return time.strftime("%d %b %H:%M", time.localtime(ts))


KIND = {"manual": "🕵️ Manual review", "join": "⚡ Auto · Channel join", "bot": "⚡ Auto · Bot start"}
DURS = [("1h", 1), ("6h", 6), ("12h", 12), ("24h", 24), ("3 days", 72), ("7 days", 168), ("♾ No limit", 0)]
BACK = [B("⬅️ Admin Menu", callback_data="a:home")]
MENU = ReplyKeyboardMarkup([["💰 Balance", "📋 Tasks"], ["💸 Withdraw", "💳 Payout Method"],
                            ["👥 Referral", "📜 History"]], resize_keyboard=True)
MENU_TEXTS = {"💰 Balance", "📋 Tasks", "💸 Withdraw", "💳 Payout Method", "👥 Referral", "📜 History"}
ADM_PREFIX = ("tk_", "te:", "gw_", "ch_", "us_", "sx_", "bc")


def left(t):
    if not t["expires"]:
        return "No time limit"
    s = t["expires"] - now()
    if s <= 0:
        return "Expired"
    d, r = divmod(s, 86400)
    h, r = divmod(r, 3600)
    return (f"{d}d " if d else "") + (f"{h}h " if h else "") + f"{r // 60}m left"


def live(t):
    return bool(t and t["active"] and (not t["expires"] or t["expires"] > now()))


def done(uid, tid):
    return q("SELECT 1 FROM subs WHERE user_id=? AND task_id=? AND status!='rejected'", (uid, tid), one=True)


async def show(u, c, text, kb=None):
    cq = u.callback_query
    if cq:
        try:
            await cq.edit_message_text(text, parse_mode=HTML, reply_markup=kb, disable_web_page_preview=True)
            return
        except BadRequest as e:
            if "not modified" in str(e).lower():
                return
        except Exception:
            pass
        await cq.message.reply_text(text, parse_mode=HTML, reply_markup=kb, disable_web_page_preview=True)
    else:
        await u.effective_message.reply_text(text, parse_mode=HTML, reply_markup=kb, disable_web_page_preview=True)


async def notify_admins(c, text, kb=None):
    for a in ADMINS:
        try:
            await c.bot.send_message(a, text, parse_mode=HTML, reply_markup=kb)
        except Exception as e:
            logging.warning(e)


async def post_channel(c, text):
    ch = get("payout_channel")
    if ch:
        try:
            await c.bot.send_message(cid(ch), text, parse_mode=HTML)
        except Exception as e:
            logging.warning(e)


async def missing(c, uid):
    out = []
    if get("force_join", "on") != "on":
        return out
    for ch in q("SELECT * FROM channels"):
        try:
            m = await c.bot.get_chat_member(cid(ch["ref"]), uid)
            if m.status in ("left", "kicked"):
                out.append(ch)
        except Exception:
            pass
    return out


async def gate(u, c):
    uid = u.effective_user.id
    if is_admin(uid):
        return False
    ms = await missing(c, uid)
    if not ms:
        return False
    kb = [[B(f"📢 Join {ch['title']}", url=ch["link"])] for ch in ms] + [[B("✅ I've Joined", callback_data="chk")]]
    await show(u, c, "🔒 <b>Access Locked</b>\n━━━━━━━━━━━━\nPlease join the channels below to use the bot, then tap <b>I've Joined</b>.", M(kb))
    return True


async def verify_task(c, uid, t):
    if t["kind"] == "join":
        try:
            m = await c.bot.get_chat_member(cid(t["target"]), uid)
            return m.status in ("member", "administrator", "creator") or (m.status == "restricted" and getattr(m, "is_member", False))
        except Exception:
            return None
    if t["kind"] == "bot":
        if q("SELECT 1 FROM pb WHERE task_id=? AND uid=?", (t["id"], uid), one=True):
            return True
        if t["vmethod"] == "url" and t["verify_url"]:
            url = t["verify_url"].replace("{uid}", str(uid)).replace("{task}", str(t["id"]))
            try:
                async with httpx.AsyncClient(timeout=15) as h:
                    r = await h.get(url)
                return r.status_code < 400 and (t["keyword"] or "true").lower() in r.text.lower()
            except Exception:
                return False
    return False


async def credit_task(c, uid, t):
    add_bal(uid, t["reward"])
    try:
        await c.bot.send_message(uid, f"🎉 <b>Task Approved!</b>\n━━━━━━━━━━━━\n📋 {esc(t['title'])}\n💰 <b>{money(t['reward'])}</b> added to your wallet.", parse_mode=HTML)
    except Exception:
        pass
    await post_channel(c, f"✅ <b>Task Completed</b>\n👤 User <code>{mask(uid)}</code>\n📋 {esc(t['title'])}\n💰 Earned: <b>{money(t['reward'])}</b>")


async def run_gateway(gw, upi, amount, name, oid, uid):
    rep = {"{upi}": upi, "{amount}": f"{amount:.2f}", "{name}": name or "", "{order_id}": oid, "{uid}": str(uid)}
    url, body = gw["url"], gw["body"] or ""
    for k, v in rep.items():
        url = url.replace(k, quote(str(v), safe="@"))
        body = body.replace(k, json.dumps(str(v))[1:-1])
    try:
        headers = json.loads(gw["headers"]) if gw["headers"] else {}
    except Exception:
        headers = {}
    try:
        async with httpx.AsyncClient(timeout=30) as h:
            if gw["method"] == "POST":
                kw = {}
                if body:
                    try:
                        kw["json"] = json.loads(body)
                    except Exception:
                        kw["content"] = body
                r = await h.post(url, headers=headers, **kw)
            else:
                r = await h.get(url, headers=headers)
        ok = r.status_code < 400 and (gw["keyword"] or "success").lower() in r.text.lower()
        return ok, f"HTTP {r.status_code}: {r.text[:150]}"
    except Exception as e:
        return False, f"Error: {e}"


async def auto_pay(wid):
    w = q("SELECT * FROM wd WHERE id=?", (wid,), one=True)
    gw = q("SELECT * FROM gateways WHERE id=?", (int(get("gateway", "0") or 0),), one=True)
    if not gw:
        return False, "No active gateway"
    u = user(w["user_id"])
    ok, txt = await run_gateway(gw, w["upi"], w["amount"], u["name"], f"WD{wid}", w["user_id"])
    q("UPDATE wd SET note=? WHERE id=?", (txt[:200], wid), w=True)
    return ok, txt


async def finish_wd(c, wid, paid):
    st = "paid" if paid else "rejected"
    if execrc("UPDATE wd SET status=? WHERE id=? AND status='pending'", (st, wid)) != 1:
        return False
    w = q("SELECT * FROM wd WHERE id=?", (wid,), one=True)
    if paid:
        msg = f"✅ <b>Withdrawal Paid</b>\n━━━━━━━━━━━━\n💰 {money(w['amount'])}\n💳 {esc(w['upi'])}\n🧾 Ref: WD{wid}"
        await post_channel(c, f"💸 <b>Payout Successful</b>\n👤 User <code>{mask(w['user_id'])}</code>\n💰 Amount: <b>{money(w['amount'])}</b>\n🧾 Ref: WD{wid}\n✅ Status: Paid")
    else:
        add_bal(w["user_id"], w["amount"])
        msg = f"❌ <b>Withdrawal Rejected</b>\n━━━━━━━━━━━━\n💰 {money(w['amount'])} has been refunded to your wallet."
    try:
        await c.bot.send_message(w["user_id"], msg, parse_mode=HTML)
    except Exception:
        pass
    return True


# ---------------- USER SIDE ----------------
async def start(u: Update, c: ContextTypes.DEFAULT_TYPE):
    t = u.effective_user
    if not user(t.id):
        ref = None
        if c.args and c.args[0].startswith("ref_") and c.args[0][4:].isdigit():
            r = int(c.args[0][4:])
            if r != t.id and user(r):
                ref = r
        q("INSERT INTO users(tg_id,username,name,joined,referred_by) VALUES(?,?,?,?,?)",
          (t.id, t.username, t.full_name, now(), ref), w=True)
        bonus = float(get("ref_bonus", "0") or 0)
        if ref and bonus > 0:
            add_bal(ref, bonus)
            try:
                await c.bot.send_message(ref, f"🎁 New referral joined! <b>{money(bonus)}</b> added.", parse_mode=HTML)
            except Exception:
                pass
        total = q("SELECT COUNT(*) n FROM users", one=True)["n"]
        await notify_admins(c, f"🆕 <b>New User</b>\n━━━━━━━━━━━━\n👤 {esc(t.full_name)}\n🔗 @{esc(t.username or '-')}\n🆔 <code>{t.id}</code>\n👥 Referred by: <code>{ref or '-'}</code>\n📊 Total users: {total}")
    if user(t.id)["banned"]:
        return
    if await gate(u, c):
        return
    await u.effective_message.reply_text(
        f"✨ <b>Welcome to {esc(BOT_NAME)}</b> ✨\n━━━━━━━━━━━━━━\nHi <b>{esc(t.first_name)}</b>! Complete simple tasks, earn real money and withdraw straight to your UPI.\n\n"
        "💰 Instant payouts\n📋 Fresh tasks every day\n🔒 Secure &amp; transparent\n\n👇 Choose an option below",
        parse_mode=HTML, reply_markup=MENU)


async def balance(u, c):
    r = user(u.effective_user.id)
    n = q("SELECT COUNT(*) n FROM subs WHERE user_id=? AND status='approved'", (r["tg_id"],), one=True)["n"]
    wd = q("SELECT COALESCE(SUM(amount),0) s FROM wd WHERE user_id=? AND status='paid'", (r["tg_id"],), one=True)["s"]
    await show(u, c, f"👤 <b>My Wallet</b>\n━━━━━━━━━━━━━━\n🆔 Telegram ID: <code>{r['tg_id']}</code>\n💰 Balance: <b>{money(r['balance'])}</b>\n"
                     f"💳 UPI: <code>{esc(r['upi'] or 'Not linked')}</code>\n✅ Tasks done: {n}\n💸 Total withdrawn: {money(wd)}")


async def tasks_list(u, c):
    uid = u.effective_user.id
    rows = [t for t in q("SELECT * FROM tasks ORDER BY id DESC") if live(t)]
    if not rows:
        return await show(u, c, "📭 <b>No tasks available right now.</b>\nCheck back soon!")
    kb = [[B(("✔️ " if done(uid, t["id"]) else "🎯 ") + f"{t['title'][:28]} • {money(t['reward'])}", callback_data=f"t:{t['id']}")] for t in rows]
    await show(u, c, "📋 <b>Available Tasks</b>\n━━━━━━━━━━━━━━\nTap a task to see details.", M(kb))


async def payout_method(u, c):
    c.user_data["st"] = "upi"
    r = user(u.effective_user.id)
    await show(u, c, f"💳 <b>Payout Method</b>\n━━━━━━━━━━━━━━\nCurrent UPI: <code>{esc(r['upi'] or 'Not linked')}</code>\n\nSend your UPI ID (example: <code>name@bank</code>):")


async def withdraw(u, c):
    r = user(u.effective_user.id)
    if not r["upi"]:
        return await show(u, c, "⚠️ Link your UPI ID first using <b>💳 Payout Method</b>.")
    c.user_data["st"] = "wd_amount"
    await show(u, c, f"💸 <b>Withdraw</b>\n━━━━━━━━━━━━━━\n💰 Balance: <b>{money(r['balance'])}</b>\n💳 UPI: <code>{esc(r['upi'])}</code>\n📉 Minimum: {money(get('min_withdraw', '10'))}\n\nEnter the amount to withdraw:")


async def referral(u, c):
    uid = u.effective_user.id
    n = q("SELECT COUNT(*) n FROM users WHERE referred_by=?", (uid,), one=True)["n"]
    await show(u, c, f"👥 <b>Refer &amp; Earn</b>\n━━━━━━━━━━━━━━\n🎁 Bonus per referral: <b>{money(get('ref_bonus', '0') or 0)}</b>\n👤 Your referrals: {n}\n\n🔗 Your link:\n<code>https://t.me/{c.bot.username}?start=ref_{uid}</code>")


async def history(u, c):
    uid = u.effective_user.id
    ic = {"pending": "⏳", "approved": "✅", "rejected": "❌", "paid": "✅"}
    subs = q("SELECT s.status,s.created,t.title,t.reward FROM subs s JOIN tasks t ON t.id=s.task_id WHERE s.user_id=? ORDER BY s.id DESC LIMIT 8", (uid,))
    wds = q("SELECT * FROM wd WHERE user_id=? ORDER BY id DESC LIMIT 8", (uid,))
    txt = "📜 <b>History</b>\n━━━━━━━━━━━━━━\n<b>Tasks</b>\n"
    txt += "\n".join(f"{ic.get(s['status'], '•')} {esc(s['title'][:22])} • {money(s['reward'])} • {fdate(s['created'])}" for s in subs) or "—"
    txt += "\n\n<b>Withdrawals</b>\n"
    txt += "\n".join(f"{ic.get(w['status'], '•')} {money(w['amount'])} • {w['status']} • {fdate(w['created'])}" for w in wds) or "—"
    await show(u, c, txt)


MT = {"💰 Balance": balance, "📋 Tasks": tasks_list, "💳 Payout Method": payout_method,
      "💸 Withdraw": withdraw, "👥 Referral": referral, "📜 History": history}


async def send_review(c, chat, sid):
    s = q("SELECT * FROM subs WHERE id=?", (sid,), one=True)
    t = q("SELECT * FROM tasks WHERE id=?", (s["task_id"],), one=True)
    kb = M([[B("✅ Approve", callback_data=f"a:r:ok:{sid}"), B("❌ Reject", callback_data=f"a:r:no:{sid}")]])
    cap = f"📝 <b>Submission #{sid}</b>\n👤 <code>{s['user_id']}</code>\n📋 {esc(t['title'])} ({money(t['reward'])})"
    if s["proof"].startswith("photo:"):
        await c.bot.send_photo(chat, s["proof"][6:], caption=cap, parse_mode=HTML, reply_markup=kb)
    else:
        await c.bot.send_message(chat, cap + f"\n💬 {esc(s['proof'][5:])}", parse_mode=HTML, reply_markup=kb)


async def router(u: Update, c: ContextTypes.DEFAULT_TYPE):
    uid = u.effective_user.id
    m = u.effective_message
    if not user(uid):
        return await start(u, c)
    if user(uid)["banned"]:
        return
    text = (m.text or "").strip()
    st = c.user_data.get("st") or ""
    if is_admin(uid) and st.startswith(ADM_PREFIX):
        if text in MENU_TEXTS:
            c.user_data.pop("st", None)
        else:
            return await admin_text(u, c, st, text)
    if await gate(u, c):
        return
    if text in MT:
        c.user_data.pop("st", None)
        return await MT[text](u, c)

    if st == "upi":
        import re
        if not re.match(r"^[a-zA-Z0-9.\-_]{2,256}@[a-zA-Z]{2,64}$", text):
            return await m.reply_text("❌ Invalid UPI ID. Try again (example: name@bank):")
        q("UPDATE users SET upi=? WHERE tg_id=?", (text, uid), w=True)
        c.user_data.pop("st")
        await m.reply_text(f"✅ UPI linked: <code>{esc(text)}</code>", parse_mode=HTML)

    elif st == "wd_amount":
        try:
            amt = round(float(text), 2)
        except ValueError:
            return await m.reply_text("Enter a valid number:")
        r = user(uid)
        if amt < float(get("min_withdraw", "10")):
            return await m.reply_text(f"❌ Minimum withdrawal is {money(get('min_withdraw', '10'))}.")
        if not deduct(uid, amt):
            return await m.reply_text("❌ Insufficient balance.")
        c.user_data.pop("st")
        wid = q("INSERT INTO wd(user_id,amount,upi,created) VALUES(?,?,?,?)", (uid, amt, r["upi"], now()), w=True)
        ok, txt = False, ""
        if get("auto_payout", "off") == "on":
            ok, txt = await auto_pay(wid)
        if ok:
            await finish_wd(c, wid, True)
            return
        await m.reply_text(f"⏳ <b>Withdrawal Requested</b>\n━━━━━━━━━━━━\n💰 {money(amt)}\n💳 {esc(r['upi'])}\n🧾 Ref: WD{wid}\nStatus: Pending", parse_mode=HTML)
        await post_channel(c, f"⏳ <b>Withdrawal Requested</b>\n👤 User <code>{mask(uid)}</code>\n💰 {money(amt)}\n🧾 WD{wid}\nStatus: Pending")
        extra = f"\n⚠️ Auto payout failed: {esc(txt)}" if txt else ""
        await notify_admins(c, f"💸 <b>Withdraw #{wid}</b>\n👤 <code>{uid}</code>\n💰 {money(amt)}\n💳 <code>{esc(r['upi'])}</code>{extra}",
                            M([[B("✅ Paid", callback_data=f"a:w:ok:{wid}"), B("🤖 Auto Pay", callback_data=f"a:w:auto:{wid}")],
                               [B("❌ Reject", callback_data=f"a:w:no:{wid}")]]))

    elif st.startswith("proof:"):
        tid = int(st.split(":")[1])
        t = q("SELECT * FROM tasks WHERE id=?", (tid,), one=True)
        if not live(t):
            c.user_data.pop("st")
            return await m.reply_text("⌛ This task is no longer available.")
        if done(uid, tid):
            c.user_data.pop("st")
            return await m.reply_text("You already submitted this task.")
        if m.photo:
            proof = "photo:" + m.photo[-1].file_id
        elif text:
            proof = "text:" + text
        else:
            return await m.reply_text("Send a screenshot or text as proof.")
        sid = q("INSERT INTO subs(user_id,task_id,proof,status,created) VALUES(?,?,?,?,?)", (uid, tid, proof, "pending", now()), w=True)
        c.user_data.pop("st")
        await m.reply_text("📤 <b>Proof submitted!</b>\nYour task is under review.", parse_mode=HTML)
        for a in ADMINS:
            try:
                await send_review(c, a, sid)
            except Exception as e:
                logging.warning(e)


async def callbacks(u: Update, c: ContextTypes.DEFAULT_TYPE):
    cq = u.callback_query
    await cq.answer()
    d, uid = cq.data, cq.from_user.id
    if d.startswith("a:"):
        if is_admin(uid):
            await admin_cb(u, c, d.split(":"))
        return
    ru = user(uid)
    if not ru or ru["banned"]:
        return
    if d == "chk":
        if await gate(u, c):
            return
        await cq.message.reply_text("✅ <b>Access granted!</b> Welcome aboard.", parse_mode=HTML, reply_markup=MENU)
        return
    if await gate(u, c):
        return
    if d == "tl":
        return await tasks_list(u, c)
    kind, _, val = d.partition(":")
    t = q("SELECT * FROM tasks WHERE id=?", (int(val),), one=True) if val.isdigit() else None
    if not t:
        return
    if kind == "t":
        kb = []
        if t["link"]:
            kb.append([B("🔗 Open Task Link", url=t["link"])])
        if live(t) and not done(uid, t["id"]):
            kb.append([B("✅ I've Completed", callback_data=f"td:{t['id']}")])
        kb.append([B("⬅️ Back", callback_data="tl")])
        st = "✔️ Completed" if done(uid, t["id"]) else ("🟢 Open" if live(t) else "⌛ Closed")
        await show(u, c, f"🎯 <b>{esc(t['title'])}</b>\n━━━━━━━━━━━━━━\n{esc(t['descr'] or '')}\n\n💰 Reward: <b>{money(t['reward'])}</b>\n🧩 {KIND[t['kind']]}\n⏱ {left(t)}\n📌 {st}", M(kb))
    elif kind == "td":
        if not live(t):
            return await cq.message.reply_text("⌛ This task is no longer available.")
        if done(uid, t["id"]):
            return await cq.message.reply_text("✔️ You already completed this task.")
        if t["kind"] == "manual":
            c.user_data["st"] = f"proof:{t['id']}"
            return await cq.message.reply_text("📸 Send your <b>proof</b> (screenshot or text):", parse_mode=HTML)
        res = await verify_task(c, uid, t)
        if res:
            q("INSERT INTO subs(user_id,task_id,proof,status,created) VALUES(?,?,?,?,?)", (uid, t["id"], "auto", "approved", now()), w=True)
            await credit_task(c, uid, t)
        elif res is None:
            await cq.message.reply_text("⚠️ Could not verify right now. Please try again later.")
        else:
            hint = f"Join {t['target']} first." if t["kind"] == "join" else f"Start {t['target']} using the task link first."
            await cq.message.reply_text(f"❌ <b>Not verified yet.</b>\n{esc(hint)}", parse_mode=HTML,
                                        reply_markup=M([[B("🔄 Verify Again", callback_data=f"td:{t['id']}")]]))


# ---------------- ADMIN ----------------
async def admin_cmd(u, c):
    if is_admin(u.effective_user.id):
        c.user_data.pop("st", None)
        await admin_home(u, c)


async def admin_home(u, c):
    kb = [[B("📋 Tasks", callback_data="a:t:list"), B("📝 Reviews", callback_data="a:r:list")],
          [B("💸 Withdrawals", callback_data="a:w:list"), B("👥 Users", callback_data="a:u:home")],
          [B("💳 Gateways", callback_data="a:g:list"), B("📢 Channels", callback_data="a:c:list")],
          [B("⚙️ Settings", callback_data="a:s:home"), B("📣 Broadcast", callback_data="a:b:new")],
          [B("📊 Stats", callback_data="a:st")]]
    await show(u, c, "🛠 <b>Admin Panel</b>\n━━━━━━━━━━━━━━\nChoose a section:", M(kb))


def dur_kb(prefix):
    rows, row = [], []
    for lbl, h in DURS:
        row.append(B(lbl, callback_data=f"{prefix}:{h}"))
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return rows


async def task_view(u, c, tid):
    t = q("SELECT * FROM tasks WHERE id=?", (tid,), one=True)
    if not t:
        return await show(u, c, "Task not found.", M([BACK]))
    n = q("SELECT COUNT(*) n FROM subs WHERE task_id=? AND status='approved'", (tid,), one=True)["n"]
    txt = (f"📋 <b>Task #{tid}</b>\n━━━━━━━━━━━━━━\n<b>{esc(t['title'])}</b>\n{esc(t['descr'] or '-')}\n\n"
           f"💰 Reward: <b>{money(t['reward'])}</b>\n🔗 Link: {esc(t['link'] or '-')}\n🧩 Type: {KIND[t['kind']]}\n"
           f"🎯 Target: {esc(t['target'] or '-')}\n⏱ {left(t)}\n📌 {'Active' if t['active'] else 'Paused'}\n✅ Completed: {n}")
    if t["kind"] == "bot":
        if t["vmethod"] == "pb":
            txt += f"\n\n🔔 <b>Postback URL</b> (call it from your bot when the user starts it):\n<code>{esc(BASE_URL or 'https://YOUR-APP.onrender.com')}/postback?task={tid}&amp;uid={{user_id}}&amp;key={esc(PB_KEY)}</code>"
        else:
            txt += f"\n\n🔎 Verify URL: <code>{esc(t['verify_url'])}</code>\n🔑 Keyword: <code>{esc(t['keyword'])}</code>"
    kb = [[B("▶️ Resume" if not t["active"] else "⏸ Pause", callback_data=f"a:t:tog:{tid}"), B("✏️ Edit", callback_data=f"a:t:edm:{tid}")],
          [B("🗑 Delete", callback_data=f"a:t:del:{tid}")], [B("⬅️ Tasks", callback_data="a:t:list")]]
    await show(u, c, txt, M(kb))


async def adm_tasks(u, c, p):
    act = p[2] if len(p) > 2 else "list"
    tmp = c.user_data.setdefault("tmp", {})
    if act == "list":
        rows = q("SELECT * FROM tasks ORDER BY id DESC LIMIT 30")
        kb = []
        for t in rows:
            ic = "🟢" if live(t) else ("⏸" if not t["active"] else "⌛")
            kb.append([B(f"{ic} #{t['id']} {t['title'][:24]} • {money(t['reward'])}", callback_data=f"a:t:v:{t['id']}")])
        kb += [[B("➕ New Task", callback_data="a:t:new")], BACK]
        await show(u, c, "📋 <b>Tasks</b>\n🟢 live · ⏸ paused · ⌛ expired", M(kb))
    elif act == "new":
        c.user_data["tmp"] = {}
        c.user_data["st"] = "tk_title"
        await show(u, c, "➕ <b>New Task</b>\n\nStep 1: Send the <b>task name</b>:", M([BACK]))
    elif act == "v":
        await task_view(u, c, int(p[3]))
    elif act == "tog":
        q("UPDATE tasks SET active=1-active WHERE id=?", (int(p[3]),), w=True)
        await task_view(u, c, int(p[3]))
    elif act == "del":
        await show(u, c, f"⚠️ Delete task #{p[3]}? This cannot be undone.", M([[B("🗑 Yes, delete", callback_data=f"a:t:delok:{p[3]}"), B("Cancel", callback_data=f"a:t:v:{p[3]}")]]))
    elif act == "delok":
        q("DELETE FROM tasks WHERE id=?", (int(p[3]),), w=True)
        await adm_tasks(u, c, ["a", "t", "list"])
    elif act == "edm":
        tid = p[3]
        kb = [[B("Title", callback_data=f"a:t:ed:{tid}:title"), B("Description", callback_data=f"a:t:ed:{tid}:descr")],
              [B("Reward", callback_data=f"a:t:ed:{tid}:reward"), B("Link", callback_data=f"a:t:ed:{tid}:link")],
              [B("Target", callback_data=f"a:t:ed:{tid}:target"), B("Verify URL", callback_data=f"a:t:ed:{tid}:verify_url")],
              [B("⏱ Duration", callback_data=f"a:t:edur:{tid}")], [B("⬅️ Back", callback_data=f"a:t:v:{tid}")]]
        await show(u, c, f"✏️ <b>Edit Task #{tid}</b>\nWhat do you want to change?", M(kb))
    elif act == "ed":
        c.user_data["st"] = f"te:{p[3]}:{p[4]}"
        await show(u, c, f"Send the new value for <b>{p[4]}</b> (send <code>-</code> to clear):", M([[B("Cancel", callback_data=f"a:t:v:{p[3]}")]]))
    elif act == "edur":
        await show(u, c, "⏱ Choose new duration (starts from now):", M(dur_kb(f"a:t:sd:{p[3]}") + [[B("⬅️ Back", callback_data=f"a:t:edm:{p[3]}")]]))
    elif act == "sd":
        h = int(p[4])
        q("UPDATE tasks SET hours=?, expires=?, active=1 WHERE id=?", (h, now() + h * 3600 if h else 0, int(p[3])), w=True)
        await task_view(u, c, int(p[3]))
    elif act == "type":
        tmp["kind"] = p[3]
        if p[3] == "manual":
            await show(u, c, "⏱ <b>Duration</b>\nHow long should this task stay live?", M(dur_kb("a:t:dur")))
        elif p[3] == "join":
            c.user_data["st"] = "tk_target"
            await show(u, c, "📢 Send the <b>channel/group</b> users must join (<code>@username</code> or numeric ID).\n⚠️ Add the bot as admin there so it can verify.")
        else:
            c.user_data["st"] = "tk_target"
            await show(u, c, "🤖 Send the <b>partner bot username</b> (example: <code>@MyOtherBot</code>):")
    elif act == "vm":
        tmp["vmethod"] = p[3]
        if p[3] == "pb":
            await show(u, c, "⏱ <b>Duration</b>\nHow long should this task stay live?", M(dur_kb("a:t:dur")))
        else:
            c.user_data["st"] = "tk_vurl"
            await show(u, c, "🔎 Send the <b>verify URL</b>. Use <code>{uid}</code> for the user ID.\nExample: <code>https://mybot.com/check?uid={uid}</code>")
    elif act == "dur":
        h = int(p[3])
        tid = q("""INSERT INTO tasks(title,descr,reward,link,kind,target,vmethod,verify_url,keyword,hours,expires,active,created)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,1,?)""",
                (tmp.get("title"), tmp.get("descr", ""), tmp.get("reward"), tmp.get("link", ""), tmp.get("kind"),
                 tmp.get("target", ""), tmp.get("vmethod", ""), tmp.get("verify_url", ""), tmp.get("keyword", ""),
                 h, now() + h * 3600 if h else 0, now()), w=True)
        c.user_data.pop("tmp", None)
        c.user_data.pop("st", None)
        await task_view(u, c, tid)


async def adm_gateways(u, c, p):
    act = p[2] if len(p) > 2 else "list"
    tmp = c.user_data.setdefault("tmp", {})
    if act == "list":
        active = get("gateway", "0")
        rows = q("SELECT * FROM gateways")
        kb = [[B(("✅ " if str(g["id"]) == active else "") + g["name"], callback_data=f"a:g:v:{g['id']}")] for g in rows]
        kb += [[B("➕ Add Gateway", callback_data="a:g:new")], BACK]
        await show(u, c, "💳 <b>UPI Payout Gateways</b>\n━━━━━━━━━━━━━━\nThe ✅ gateway is used for auto payouts.", M(kb))
    elif act == "new":
        c.user_data["tmp"] = {}
        c.user_data["st"] = "gw_name"
        await show(u, c, "➕ <b>Add Gateway</b>\n\nSend a <b>name</b> (e.g. MyPayAPI):", M([BACK]))
    elif act == "v":
        g = q("SELECT * FROM gateways WHERE id=?", (int(p[3]),), one=True)
        if not g:
            return await adm_gateways(u, c, ["a", "g", "list"])
        txt = (f"💳 <b>{esc(g['name'])}</b>\n━━━━━━━━━━━━━━\nMethod: {g['method']}\nURL: <code>{esc(g['url'])}</code>\n"
               f"Body: <code>{esc(g['body'] or '-')}</code>\nHeaders: <code>{esc(g['headers'] or '-')}</code>\nSuccess keyword: <code>{esc(g['keyword'])}</code>\n"
               f"Active: {'Yes' if str(g['id']) == get('gateway', '0') else 'No'}")
        await show(u, c, txt, M([[B("✅ Set Active", callback_data=f"a:g:act:{g['id']}"), B("🗑 Delete", callback_data=f"a:g:del:{g['id']}")], [B("⬅️ Back", callback_data="a:g:list")]]))
    elif act == "act":
        put("gateway", p[3])
        await adm_gateways(u, c, ["a", "g", "list"])
    elif act == "del":
        q("DELETE FROM gateways WHERE id=?", (int(p[3]),), w=True)
        if get("gateway") == p[3]:
            put("gateway", "0")
        await adm_gateways(u, c, ["a", "g", "list"])
    elif act == "meth":
        tmp["method"] = p[3]
        if p[3] == "POST":
            c.user_data["st"] = "gw_body"
            await show(u, c, "Send the <b>JSON body</b> template (or <code>-</code> for none).\nPlaceholders: <code>{upi} {amount} {name} {order_id} {uid}</code>")
        else:
            tmp["body"] = ""
            c.user_data["st"] = "gw_headers"
            await show(u, c, "Send <b>headers as JSON</b> (example: <code>{\"Authorization\":\"Bearer KEY\"}</code>) or <code>-</code> for none:")


async def adm_channels(u, c, p):
    act = p[2] if len(p) > 2 else "list"
    if act == "list":
        rows = q("SELECT * FROM channels")
        kb = [[B(f"🗑 {r['title']}", callback_data=f"a:c:del:{r['id']}")] for r in rows]
        kb += [[B("➕ Add Channel", callback_data="a:c:new")], [B(f"🔒 Force join: {get('force_join', 'on').upper()}", callback_data="a:c:fj")], BACK]
        await show(u, c, "📢 <b>Force-Join Channels</b>\n━━━━━━━━━━━━━━\nUsers must join these before using the bot.\nTap a channel to remove it.", M(kb))
    elif act == "new":
        c.user_data["tmp"] = {}
        c.user_data["st"] = "ch_ref"
        await show(u, c, "📢 Send the channel <code>@username</code> or numeric ID (<code>-100…</code>).\n⚠️ The bot must be admin there.", M([BACK]))
    elif act == "del":
        q("DELETE FROM channels WHERE id=?", (int(p[3]),), w=True)
        await adm_channels(u, c, ["a", "c", "list"])
    elif act == "fj":
        put("force_join", "off" if get("force_join", "on") == "on" else "on")
        await adm_channels(u, c, ["a", "c", "list"])


async def user_card(u, c, uid):
    r = user(uid)
    if not r:
        return await show(u, c, "User not found.", M([[B("⬅️ Back", callback_data="a:u:home")]]))
    n = q("SELECT COUNT(*) n FROM subs WHERE user_id=? AND status='approved'", (uid,), one=True)["n"]
    txt = (f"👤 <b>{esc(r['name'] or '-')}</b>\n━━━━━━━━━━━━━━\n🆔 <code>{uid}</code>\n🔗 @{esc(r['username'] or '-')}\n💰 Balance: <b>{money(r['balance'])}</b>\n"
           f"💳 UPI: <code>{esc(r['upi'] or '-')}</code>\n✅ Tasks done: {n}\n📅 Joined: {fdate(r['joined'])}\n🚫 Banned: {'Yes' if r['banned'] else 'No'}")
    kb = [[B("➕ Add Balance", callback_data=f"a:u:add:{uid}"), B("➖ Subtract", callback_data=f"a:u:sub:{uid}")],
          [B("✅ Unban" if r["banned"] else "🚫 Ban", callback_data=f"a:u:ban:{uid}")], [B("⬅️ Back", callback_data="a:u:home")]]
    await show(u, c, txt, M(kb))


async def adm_users(u, c, p):
    act = p[2] if len(p) > 2 else "home"
    if act == "home":
        await show(u, c, "👥 <b>Users</b>", M([[B("🔍 Find User", callback_data="a:u:find"), B("🆕 Recent", callback_data="a:u:recent")], BACK]))
    elif act == "find":
        c.user_data["st"] = "us_find"
        await show(u, c, "Send the user's <b>Telegram ID</b> or <b>@username</b>:", M([BACK]))
    elif act == "recent":
        rows = q("SELECT * FROM users ORDER BY joined DESC LIMIT 15")
        txt = "🆕 <b>Recent Users</b>\n━━━━━━━━━━━━━━\n" + "\n".join(f"<code>{r['tg_id']}</code> @{esc(r['username'] or '-')} • {money(r['balance'])}" for r in rows)
        await show(u, c, txt, M([[B("⬅️ Back", callback_data="a:u:home")]]))
    elif act == "v":
        await user_card(u, c, int(p[3]))
    elif act in ("add", "sub"):
        c.user_data["st"] = f"us_{act}:{p[3]}"
        await show(u, c, f"Send the amount to <b>{'add to' if act == 'add' else 'subtract from'}</b> user <code>{p[3]}</code>:", M([[B("Cancel", callback_data=f"a:u:v:{p[3]}")]]))
    elif act == "ban":
        r = user(int(p[3]))
        q("UPDATE users SET banned=? WHERE tg_id=?", (0 if r["banned"] else 1, int(p[3])), w=True)
        await user_card(u, c, int(p[3]))


async def wd_view(u, c, wid):
    w = q("SELECT * FROM wd WHERE id=?", (wid,), one=True)
    if not w:
        return await adm_wd(u, c, ["a", "w", "list"])
    txt = (f"💸 <b>Withdrawal #{wid}</b>\n━━━━━━━━━━━━━━\n👤 <code>{w['user_id']}</code>\n💰 {money(w['amount'])}\n💳 <code>{esc(w['upi'])}</code>\n"
           f"📌 Status: {w['status']}\n🕒 {fdate(w['created'])}\n📝 Note: {esc(w['note'] or '-')}")
    kb = []
    if w["status"] == "pending":
        kb = [[B("✅ Mark Paid", callback_data=f"a:w:ok:{wid}"), B("🤖 Auto Pay", callback_data=f"a:w:auto:{wid}")], [B("❌ Reject & Refund", callback_data=f"a:w:no:{wid}")]]
    kb.append([B("⬅️ Back", callback_data="a:w:list")])
    await show(u, c, txt, M(kb))


async def adm_wd(u, c, p):
    act = p[2] if len(p) > 2 else "list"
    if act == "list":
        rows = q("SELECT * FROM wd WHERE status='pending' ORDER BY id LIMIT 20")
        kb = [[B(f"#{w['id']} • {money(w['amount'])} • {w['user_id']}", callback_data=f"a:w:v:{w['id']}")] for w in rows] + [BACK]
        await show(u, c, f"💸 <b>Pending Withdrawals</b> ({len(rows)})", M(kb))
    elif act == "v":
        await wd_view(u, c, int(p[3]))
    elif act == "ok":
        ok = await finish_wd(c, int(p[3]), True)
        await show(u, c, f"{'✅ Marked paid' if ok else '⚠️ Already processed'}: #{p[3]}", M([[B("⬅️ Withdrawals", callback_data="a:w:list")]]))
    elif act == "no":
        ok = await finish_wd(c, int(p[3]), False)
        await show(u, c, f"{'❌ Rejected & refunded' if ok else '⚠️ Already processed'}: #{p[3]}", M([[B("⬅️ Withdrawals", callback_data="a:w:list")]]))
    elif act == "auto":
        w = q("SELECT * FROM wd WHERE id=?", (int(p[3]),), one=True)
        if w["status"] != "pending":
            return await show(u, c, "⚠️ Already processed.", M([[B("⬅️ Withdrawals", callback_data="a:w:list")]]))
        ok, txt = await auto_pay(w["id"])
        if ok:
            await finish_wd(c, w["id"], True)
        await show(u, c, f"{'✅ Auto payout success' if ok else '❌ Auto payout failed'}\n<code>{esc(txt)}</code>", M([[B("⬅️ Withdrawals", callback_data="a:w:list")]]))


async def adm_reviews(u, c, p):
    act = p[2] if len(p) > 2 else "list"
    if act == "list":
        rows = q("SELECT id FROM subs WHERE status='pending' ORDER BY id LIMIT 5")
        total = q("SELECT COUNT(*) n FROM subs WHERE status='pending'", one=True)["n"]
        await show(u, c, f"📝 <b>Pending reviews:</b> {total}\nShowing the oldest {len(rows)} below.", M([BACK]))
        for r in rows:
            await send_review(c, u.effective_user.id, r["id"])
    else:
        sid = int(p[3])
        s = q("SELECT * FROM subs WHERE id=?", (sid,), one=True)
        try:
            await u.callback_query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        if not s or s["status"] != "pending":
            return await u.callback_query.message.reply_text("⚠️ Already processed.")
        t = q("SELECT * FROM tasks WHERE id=?", (s["task_id"],), one=True)
        if act == "ok":
            q("UPDATE subs SET status='approved' WHERE id=?", (sid,), w=True)
            await credit_task(c, s["user_id"], t)
            await u.callback_query.message.reply_text(f"✅ Approved #{sid}")
        else:
            q("UPDATE subs SET status='rejected' WHERE id=?", (sid,), w=True)
            try:
                await c.bot.send_message(s["user_id"], f"❌ <b>Task Rejected</b>\n📋 {esc(t['title'])}\nYour proof was not accepted.", parse_mode=HTML)
            except Exception:
                pass
            await u.callback_query.message.reply_text(f"❌ Rejected #{sid}")


async def settings_view(u, c):
    kb = [[B(f"🤖 Auto payout: {get('auto_payout', 'off').upper()}", callback_data="a:s:auto")],
          [B("💵 Min withdraw", callback_data="a:s:min"), B("📣 Payout channel", callback_data="a:s:pc")],
          [B("🎁 Referral bonus", callback_data="a:s:ref"), B(f"🔒 Force join: {get('force_join', 'on').upper()}", callback_data="a:s:fj")], BACK]
    gw = q("SELECT name FROM gateways WHERE id=?", (int(get("gateway", "0") or 0),), one=True)
    await show(u, c, f"⚙️ <b>Settings</b>\n━━━━━━━━━━━━━━\n🤖 Auto payout: <b>{get('auto_payout', 'off')}</b>\n💳 Active gateway: <b>{esc(gw['name']) if gw else 'None'}</b>\n"
                     f"💵 Min withdraw: <b>{money(get('min_withdraw', '10'))}</b>\n📣 Payout channel: <code>{esc(get('payout_channel') or 'Not set (status stays in bot)')}</code>\n"
                     f"🎁 Referral bonus: <b>{money(get('ref_bonus', '0') or 0)}</b>", M(kb))


async def adm_settings(u, c, p):
    act = p[2] if len(p) > 2 else "home"
    if act == "home":
        await settings_view(u, c)
    elif act == "auto":
        put("auto_payout", "off" if get("auto_payout", "off") == "on" else "on")
        await settings_view(u, c)
    elif act == "fj":
        put("force_join", "off" if get("force_join", "on") == "on" else "on")
        await settings_view(u, c)
    else:
        prompts = {"min": ("sx_min", "Send the new <b>minimum withdraw</b> amount:"),
                   "pc": ("sx_pc", "Send the <b>payout channel</b> (<code>@username</code> or ID, bot must be admin). Send <code>-</code> to disable:"),
                   "ref": ("sx_ref", "Send the <b>referral bonus</b> amount (0 to disable):")}
        c.user_data["st"] = prompts[act][0]
        await show(u, c, prompts[act][1], M([[B("Cancel", callback_data="a:s:home")]]))


async def admin_cb(u, c, p):
    sec = p[1]
    act = p[2] if len(p) > 2 else ""
    if act not in ("type", "dur", "vm", "meth"):
        c.user_data.pop("st", None)
    if sec == "home":
        return await admin_home(u, c)
    if sec == "st":
        n = q("SELECT COUNT(*) n, COALESCE(SUM(balance),0) b FROM users", one=True)
        day = q("SELECT COUNT(*) n FROM users WHERE joined>?", (now() - 86400,), one=True)["n"]
        pw = q("SELECT COUNT(*) n, COALESCE(SUM(amount),0) s FROM wd WHERE status='pending'", one=True)
        pd = q("SELECT COALESCE(SUM(amount),0) s FROM wd WHERE status='paid'", one=True)["s"]
        tk = q("SELECT COUNT(*) n FROM tasks WHERE active=1", one=True)["n"]
        pr = q("SELECT COUNT(*) n FROM subs WHERE status='pending'", one=True)["n"]
        return await show(u, c, f"📊 <b>Stats</b>\n━━━━━━━━━━━━━━\n👥 Users: {n['n']} (+{day} today)\n💰 Total balances: {money(n['b'])}\n📋 Active tasks: {tk}\n"
                                f"📝 Pending reviews: {pr}\n⏳ Pending withdrawals: {pw['n']} ({money(pw['s'])})\n✅ Total paid: {money(pd)}", M([BACK]))
    if sec == "b":
        c.user_data["st"] = "bc"
        return await show(u, c, "📣 Send the message to <b>broadcast to all users</b>:", M([BACK]))
    handler = {"t": adm_tasks, "g": adm_gateways, "c": adm_channels, "u": adm_users, "w": adm_wd, "r": adm_reviews, "s": adm_settings}.get(sec)
    if handler:
        await handler(u, c, p)


async def admin_text(u, c, st, text):
    m = u.effective_message
    if not text:
        return
    tmp = c.user_data.setdefault("tmp", {})

    async def say(t, kb=None):
        await m.reply_text(t, parse_mode=HTML, reply_markup=kb, disable_web_page_preview=True)

    def num():
        try:
            v = float(text)
            return v if v >= 0 else None
        except ValueError:
            return None

    if st == "tk_title":
        tmp["title"] = text[:80]
        c.user_data["st"] = "tk_reward"
        await say("Step 2: Send the <b>reward amount</b> (e.g. 5):")
    elif st == "tk_reward":
        v = num()
        if v is None:
            return await say("❌ Send a valid number:")
        tmp["reward"] = v
        c.user_data["st"] = "tk_descr"
        await say("Step 3: Send the <b>description</b> (or <code>-</code> to skip):")
    elif st == "tk_descr":
        tmp["descr"] = "" if text == "-" else text
        c.user_data["st"] = "tk_link"
        await say("Step 4: Send the <b>task link</b> (or <code>-</code> to skip):")
    elif st == "tk_link":
        tmp["link"] = "" if text == "-" else text
        c.user_data.pop("st")
        await say("Step 5: Choose the <b>verification type</b>:", M([[B("🕵️ Manual (proof review)", callback_data="a:t:type:manual")],
                                                                    [B("⚡ Auto: Channel Join", callback_data="a:t:type:join")],
                                                                    [B("⚡ Auto: Bot Start", callback_data="a:t:type:bot")]]))
    elif st == "tk_target":
        if tmp.get("kind") == "join":
            try:
                ch = await c.bot.get_chat(cid(text))
                tmp["target"] = str(ch.id) if not ch.username else "@" + ch.username
            except Exception:
                return await say("❌ Cannot access that chat. Add the bot as admin and send it again:")
            c.user_data.pop("st")
            await say("⏱ <b>Duration</b>\nHow long should this task stay live?", M(dur_kb("a:t:dur")))
        else:
            tmp["target"] = "@" + text.replace("https://t.me/", "").lstrip("@").split("?")[0]
            c.user_data.pop("st")
            await say("🔔 How should the partner bot be verified?", M([[B("🔔 Postback (your bot calls us)", callback_data="a:t:vm:pb")],
                                                                      [B("🔎 Verify URL (we call your API)", callback_data="a:t:vm:url")]]))
    elif st == "tk_vurl":
        tmp["verify_url"] = text
        c.user_data["st"] = "tk_vkey"
        await say("Send the <b>success keyword</b> your API returns (e.g. <code>true</code>):")
    elif st == "tk_vkey":
        tmp["keyword"] = text
        c.user_data.pop("st")
        await say("⏱ <b>Duration</b>\nHow long should this task stay live?", M(dur_kb("a:t:dur")))
    elif st.startswith("te:"):
        _, tid, f = st.split(":")
        if f not in ("title", "descr", "reward", "link", "target", "verify_url"):
            return
        val = "" if text == "-" else text
        if f == "reward":
            v = num()
            if v is None:
                return await say("❌ Send a valid number:")
            val = v
        q(f"UPDATE tasks SET {f}=? WHERE id=?", (val, int(tid)), w=True)
        c.user_data.pop("st")
        await say(f"✅ Updated.", M([[B("📋 View Task", callback_data=f"a:t:v:{tid}")]]))
    elif st == "gw_name":
        tmp["name"] = text[:40]
        c.user_data["st"] = "gw_url"
        await say("Send the payout <b>API URL</b>.\nPlaceholders: <code>{upi} {amount} {name} {order_id} {uid}</code>\nExample:\n<code>https://api.example.com/pay?key=KEY&amp;upi={upi}&amp;amount={amount}&amp;order={order_id}</code>")
    elif st == "gw_url":
        tmp["url"] = text
        c.user_data.pop("st")
        await say("Choose the request <b>method</b>:", M([[B("GET", callback_data="a:g:meth:GET"), B("POST", callback_data="a:g:meth:POST")]]))
    elif st == "gw_body":
        tmp["body"] = "" if text == "-" else text
        c.user_data["st"] = "gw_headers"
        await say("Send <b>headers as JSON</b> (example: <code>{\"Authorization\":\"Bearer KEY\"}</code>) or <code>-</code> for none:")
    elif st == "gw_headers":
        if text != "-":
            try:
                json.loads(text)
            except Exception:
                return await say("❌ Invalid JSON. Send again or <code>-</code>:")
        tmp["headers"] = "" if text == "-" else text
        c.user_data["st"] = "gw_key"
        await say("Send the <b>success keyword</b> found in a successful response (e.g. <code>success</code>):")
    elif st == "gw_key":
        gid = q("INSERT INTO gateways(name,url,method,body,headers,keyword) VALUES(?,?,?,?,?,?)",
                (tmp["name"], tmp["url"], tmp.get("method", "GET"), tmp.get("body", ""), tmp.get("headers", ""), text), w=True)
        if not get("gateway") or get("gateway") == "0":
            put("gateway", gid)
        c.user_data.pop("st")
        c.user_data.pop("tmp", None)
        await say("✅ Gateway added.", M([[B("💳 Gateways", callback_data="a:g:list")]]))
    elif st == "ch_ref":
        try:
            ch = await c.bot.get_chat(cid(text))
        except Exception:
            return await say("❌ Cannot access that chat. Add the bot as admin and send again:")
        tmp["title"], tmp["ref"] = ch.title or text, (str(ch.id) if not ch.username else "@" + ch.username)
        if ch.username:
            q("INSERT INTO channels(title,ref,link) VALUES(?,?,?)", (tmp["title"], tmp["ref"], f"https://t.me/{ch.username}"), w=True)
            c.user_data.pop("st")
            await say("✅ Channel added.", M([[B("📢 Channels", callback_data="a:c:list")]]))
        else:
            c.user_data["st"] = "ch_link"
            await say("🔐 Private chat. Send its <b>invite link</b> (format <code>https://t.me/+xxxxxxxx</code>):")
    elif st == "ch_link":
        if not text.startswith("https://t.me/"):
            return await say("❌ Send a valid invite link starting with https://t.me/")
        q("INSERT INTO channels(title,ref,link) VALUES(?,?,?)", (tmp["title"], tmp["ref"], text), w=True)
        c.user_data.pop("st")
        await say("✅ Channel added.", M([[B("📢 Channels", callback_data="a:c:list")]]))
    elif st == "us_find":
        r = user(int(text)) if text.lstrip("-").isdigit() else q("SELECT * FROM users WHERE lower(username)=?", (text.lstrip("@").lower(),), one=True)
        c.user_data.pop("st")
        if not r:
            return await say("User not found.", M([[B("⬅️ Back", callback_data="a:u:home")]]))
        await user_card(u, c, r["tg_id"])
    elif st.startswith(("us_add:", "us_sub:")):
        v = num()
        if v is None or v <= 0:
            return await say("❌ Send a valid amount:")
        act, uid = st.split(":")
        uid = int(uid)
        c.user_data.pop("st")
        if act == "us_add":
            add_bal(uid, v)
            msg = f"💰 <b>{money(v)}</b> was added to your wallet."
        elif deduct(uid, v):
            msg = f"➖ <b>{money(v)}</b> was deducted from your wallet."
        else:
            return await say("❌ User has insufficient balance.", M([[B("⬅️ Back", callback_data=f"a:u:v:{uid}")]]))
        try:
            await c.bot.send_message(uid, msg, parse_mode=HTML)
        except Exception:
            pass
        await user_card(u, c, uid)
    elif st == "sx_min":
        v = num()
        if v is None:
            return await say("❌ Send a valid number:")
        put("min_withdraw", v)
        c.user_data.pop("st")
        await settings_view(u, c)
    elif st == "sx_ref":
        v = num()
        if v is None:
            return await say("❌ Send a valid number:")
        put("ref_bonus", v)
        c.user_data.pop("st")
        await settings_view(u, c)
    elif st == "sx_pc":
        if text == "-":
            put("payout_channel", "")
        else:
            try:
                await c.bot.get_chat(cid(text))
            except Exception:
                return await say("❌ Cannot access that chat. Add the bot as admin and send again:")
            put("payout_channel", text)
        c.user_data.pop("st")
        await settings_view(u, c)
    elif st == "bc":
        c.user_data.pop("st")
        sent = 0
        for r in q("SELECT tg_id FROM users WHERE banned=0"):
            try:
                await c.bot.send_message(r["tg_id"], text)
                sent += 1
            except Exception:
                pass
            await asyncio.sleep(0.05)
        await say(f"📣 Sent to <b>{sent}</b> users.", M([BACK]))


# ---------------- WEB SERVER (health + postback) ----------------
async def health(request):
    return web.Response(text="OK")


async def postback(request):
    p = dict(request.query)
    if request.method == "POST":
        try:
            p.update(await request.post())
        except Exception:
            pass
    if p.get("key") != PB_KEY:
        return web.Response(text="forbidden", status=403)
    try:
        tid, uid = int(p["task"]), int(p["uid"])
    except Exception:
        return web.Response(text="bad request", status=400)
    q("INSERT OR IGNORE INTO pb(task_id,uid,created) VALUES(?,?,?)", (tid, uid, now()), w=True)
    return web.Response(text="ok")


async def main():
    init_db()
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("admin", admin_cmd))
    app.add_handler(CallbackQueryHandler(callbacks))
    app.add_handler(MessageHandler((filters.TEXT & ~filters.COMMAND) | filters.PHOTO, router))
    web_app = web.Application()
    web_app.add_routes([web.get("/", health), web.get("/postback", postback), web.post("/postback", postback)])
    await app.initialize()
    await app.start()
    await app.updater.start_polling(drop_pending_updates=True)
    runner = web.AppRunner(web_app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
