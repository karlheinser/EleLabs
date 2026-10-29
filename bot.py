"""ربات تلگرام ElevenLabs — نقطهٔ شروع برنامه."""
import asyncio
import base64
import hashlib
import html
import io
import json
import math
import logging
import time
import traceback
import uuid

from telegram import (InlineKeyboardMarkup as M, InlineQueryResultArticle, InlineQueryResultCachedAudio,
                      InlineQueryResultCachedVoice, InputFile, InputTextMessageContent, LabeledPrice, Update)
from telegram.constants import ChatAction, ChatType, ParseMode
from telegram.error import BadRequest, Forbidden
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler, ContextTypes,
                          InlineQueryHandler, MessageHandler, PreCheckoutQueryHandler, filters)

import config
import db
import eleven
import media
import ui
from ui import HOME, btn

logging.basicConfig(format="%(asctime)s | %(levelname)s | %(name)s | %(message)s", level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("bot")

MAX_TG_DOWNLOAD = 20 * 1024 * 1024
HTML = ParseMode.HTML
esc = html.escape

# ============================ کمکی‌ها ============================

def is_owner(uid: int) -> bool:
    """مدیر اصلی (ADMIN_IDS در فایل .env) — دسترسی کامل + تعیین مدیرها"""
    return uid in config.ADMIN_IDS


def admin_perms(uid: int) -> set | None:
    """None = مدیر نیست"""
    if is_owner(uid):
        return set(ui.ADMIN_PERMS)
    return db.admin_perms_of(uid)


def is_admin(uid: int) -> bool:
    return is_owner(uid) or db.admin_perms_of(uid) is not None


def can(uid: int, perm: str) -> bool:
    return is_owner(uid) or perm in (db.admin_perms_of(uid) or set())


async def notify_user(context, u, text: str) -> bool:
    """پیام اطلاع‌رسانی به کاربر؛ True اگر ارسال شد."""
    if not u or not u["last_seen"]:
        return False
    try:
        await context.bot.send_message(u["id"], text, parse_mode=HTML)
        return True
    except Exception:
        return False


def sent_note(ok: bool) -> str:
    return ("\n\n📨 <i>به کاربر اطلاع داده شد.</i>" if ok else
            "\n\n⚠️ <i>اعلان ارسال نشد (کاربر ربات رو استارت نکرده یا بلاکش کرده).</i>")


def is_white(u) -> bool:
    return bool(u and (u["whitelisted"] or u["id"] in config.ALLOWED_USER_IDS))


def is_unlimited(uid: int) -> bool:
    return is_admin(uid) or is_white(db.get_user(uid))


def bot_open() -> bool:
    return db.get_setting("bot_open", "0") == "1"


def closed_message() -> str:
    return db.get_setting("closed_msg") or ui.DEFAULT_CLOSED_MSG


# ---------------- هزینه‌ها و بسته‌ها (قابل تغییر از پنل) ----------------
def cost(key: str):
    v = db.get_setting(f"cost:{key}")
    if v is not None:
        try:
            return float(v) if key == "tts" else int(v)
        except ValueError:
            pass
    return config.TTS_RATE if key == "tts" else config.COSTS[key]


def text_cost(chars: int) -> int:
    return math.ceil(chars * cost("tts"))


def packages() -> list[dict]:
    raw = db.get_setting("packages")
    if raw:
        try:
            return json.loads(raw)
        except ValueError:
            pass
    return [{"credits": c, "stars": s, "title": ""} for c, s in config.STAR_PACKAGES]


def save_packages(pkgs: list[dict]):
    db.set_setting("packages", json.dumps(pkgs, ensure_ascii=False))


def stars_enabled() -> bool:
    return db.get_setting("stars_enabled", "1") == "1"


def costs_menu():
    rows = [[btn(f"{label}: {ui.num(cost(k))}", f"c:e:{k}")] for k, label in ui.COST_LABELS.items()]
    rows.append([btn("♻️ بازگشت به پیش‌فرض", "c:reset"), btn("🔙 پنل مدیریت", "m:admin")])
    text = ("💰 <b>هزینه‌ها</b>\n\nروی هر مورد بزن تا مقدارش رو عوض کنی.\n"
            "<i>۰ = رایگان. برای متن به صدا عدد اعشاری هم مجازه (مثلاً 0.5).</i>")
    return text, M(rows)


def packages_menu():
    pk = packages()
    on = stars_enabled()
    rows = [[btn(f"📦 {ui.package_label(p)}", f"p:v:{i}")] for i, p in enumerate(pk)]
    rows.append([btn("➕ افزودن بسته", "p:add"),
                 btn("🟢 فروش فعال ← غیرفعال کن" if on else "🔴 فروش غیرفعال ← فعال کن", "p:toggle")])
    rows.append([btn("♻️ بسته‌های پیش‌فرض", "p:reset"), btn("🔙 پنل مدیریت", "m:admin")])
    text = (f"⭐ <b>بسته‌های خرید با Telegram Stars</b>\n\nوضعیت فروش: {'🟢 فعال' if on else '🔴 غیرفعال'}\n"
            f"تعداد بسته‌ها: <b>{len(pk)}</b>\n\nروی هر بسته بزن تا ویرایشش کنی.")
    return text, M(rows)


def package_menu(i: int):
    p = packages()[i]
    text = (f"📦 <b>بسته {i + 1}</b>\n\n🏷 عنوان: <b>{esc(p.get('title') or '—')}</b>\n"
            f"💰 اعتبار: <b>{p['credits']:,}</b>\n⭐ قیمت: <b>{p['stars']:,}</b> ستاره")
    kb = M([
        [btn("✏️ تعداد اعتبار", f"p:ec:{i}"), btn("✏️ قیمت (ستاره)", f"p:es:{i}")],
        [btn("🏷 عنوان", f"p:et:{i}"), btn("⬆️ انتقال به بالا", f"p:up:{i}")],
        [btn("🗑 حذف بسته", f"p:del:{i}"), btn("🔙 بسته‌ها", "p:list")],
    ])
    return text, kb


def akb(uid: int):
    return ui.admin_kb(admin_perms(uid) or set(), is_owner(uid), bot_open(), db.daily_free_default())


def admin_panel_text() -> str:
    s = db.stats()
    status = "🟢 <b>باز</b> — همه می‌تونن استفاده کنن" if bot_open() else "🔴 <b>بسته</b> — فقط ادمین و لیست سفید"
    custom = "اختصاصی" if db.get_setting("closed_msg") else "پیش‌فرض"
    return ("🛠 <b>پنل مدیریت</b>\n\n"
            f"وضعیت ربات: {status}\n"
            f"✉️ پیام حالت بسته: {custom}\n"
            f"⭐ لیست سفید: <b>{s['whitelisted']}</b> نفر\n"
            f"🎁 اعتبار روزانهٔ کاربران عادی: <b>{db.daily_free_default():,}</b>\n"
            f"👥 کل کاربران: <b>{s['users']:,}</b>")


def user_card_text(u) -> str:
    free, bal = db.remaining(u["id"])
    if is_owner(u["id"]):
        role = "👑 مدیر اصلی"
    elif u["admin_perms"] is not None:
        role = "🛠 مدیر"
    elif u["blocked"]:
        role = "🚫 مسدود"
    elif is_white(u):
        role = "⭐ لیست سفید (بدون محدودیت)"
    else:
        role = "👤 کاربر عادی"
    limit = db.daily_limit_for(u)
    limit_s = f"{limit:,} (اختصاصی)" if u["daily_limit"] is not None else f"{limit:,} (پیش‌فرض)"
    name = esc(u["first_name"] or "—")
    uname = f"@{esc(u['username'])}" if u["username"] else "—"
    started = "" if u["last_seen"] else "\n<i>⚠️ این کاربر هنوز ربات رو استارت نکرده.</i>"
    return (f"👤 <b>{name}</b>  {uname}\n🆔 <code>{u['id']}</code>\n\n"
            f"وضعیت: {role}\n🎁 سهمیهٔ روزانه: {limit_s}\n"
            f"📉 مصرف امروز: {db.used_today(u['id']):,}\n💰 موجودی خریداری/هدیه: <b>{bal:,}</b>{started}")


async def show_user_card(update: Update, u, note: str = ""):
    viewer = update.effective_user.id
    await show(update, user_card_text(u) + note, ui.user_card_kb(u, admin_perms(viewer) or set(), is_owner(viewer)))


def admin_perm_text(u) -> str:
    perms = db.admin_perms_of(u["id"])
    name = esc(u["first_name"] or "—")
    if perms is None:
        status = "👤 مدیر نیست"
    else:
        status = f"🛠 مدیر — {len(perms)} از {len(ui.ADMIN_PERMS)} بخش"
    return (f"👑 <b>تنظیم مدیر</b>\n👤 {name}  🆔 <code>{u['id']}</code>\n\nوضعیت: {status}\n\n"
            "روی هر بخش بزن تا دسترسی این مدیر به اون بخش از پنل روشن ✅ یا خاموش ⬜ بشه.\n"
            "<i>مدیرها بدون محدودیت اعتبار از ربات استفاده می‌کنن. تعیین مدیر فقط دست مدیر اصلیه.</i>")


A_PERM = {"stats": "stats", "acct": "acct", "toggle": "access", "cmsg": "access", "cmsg_del": "access",
          "cmsg_edit": "access", "wl": "users", "wladd": "users", "user": ("users", "credit"), "daily": "daily",
          "bc": "bc", "api": "api", "apiset": "api", "apitest": "api", "apienv": "api",
          "admins": "owner", "admadd": "owner"}


def allowed(uid: int, need) -> bool:
    if is_owner(uid):
        return True
    if need is None or need == "owner":
        return False
    return any(can(uid, n) for n in (need if isinstance(need, tuple) else (need,)))


NO_ACCESS = "🔒 به این بخش از پنل دسترسی نداری."


async def show_admin_perm(update: Update, u, note: str = ""):
    await show(update, admin_perm_text(u) + note, ui.admin_perm_kb(u["id"], db.admin_perms_of(u["id"])))


def admins_menu():
    rows = []
    for oid in sorted(config.ADMIN_IDS):
        o = db.get_user(oid)
        rows.append([btn(f"👑 {((o['first_name'] if o else None) or 'مدیر اصلی')[:22]} · {oid}", f"u:v:{oid}")])
    for a in db.sub_admins():
        if a["id"] in config.ADMIN_IDS:
            continue
        n = len(db.admin_perms_of(a["id"]) or ())
        rows.append([btn(f"🛠 {(a['first_name'] or 'بدون نام')[:22]} · {n} بخش", f"g:v:{a['id']}")])
    rows.append([btn("➕ افزودن مدیر", "a:admadd")])
    rows.append([btn("🔙 پنل مدیریت", "m:admin")])
    return ("👑 <b>مدیران ربات</b>\n\n👑 = مدیر اصلی (از فایل .env، دسترسی کامل)\n🛠 = مدیر با دسترسی محدود\n\n"
            "روی هر مدیر بزن تا دسترسی‌هاش رو تنظیم کنی."), M(rows)


def api_menu():
    src = {"panel": "پنل مدیریت ✏️", "env": "فایل .env", "none": "❌ ثبت نشده"}[eleven.key_source()]
    rows = [[btn("✏️ تغییر کلید", "a:apiset"), btn("🧪 تست کلید فعلی", "a:apitest")]]
    if eleven.key_source() == "panel" and config.ELEVENLABS_API_KEY:
        rows.append([btn("↩️ برگشت به کلید .env", "a:apienv")])
    rows.append([btn("🔙 پنل مدیریت", "m:admin")])
    return (f"🔑 <b>کلید API ElevenLabs</b>\n\nکلید فعلی: <code>{esc(eleven.mask(eleven.current_key()))}</code>\n"
            f"منبع: {src}\n\n<i>وقتی پلن یا کاراکترهای حساب تموم شد، کلید حساب جدید رو اینجا ثبت کن؛ "
            "بلافاصله و بدون ری‌استارت اعمال می‌شه.</i>"), M(rows)


def sub_text(sub: dict) -> str:
    used, limit = sub.get("character_count", 0), sub.get("character_limit", 0)
    reset = sub.get("next_character_count_reset_unix")
    reset_s = time.strftime("%Y-%m-%d", time.gmtime(reset)) if reset else "-"
    pct = f" ({used * 100 // limit}%)" if limit else ""
    return (f"📦 پلن: <b>{esc(str(sub.get('tier', '-')))}</b>\n"
            f"🔤 مصرف: <b>{used:,}</b> از {limit:,}{pct}\n"
            f"💰 باقی‌مانده: <b>{max(0, limit - used):,}</b>\n"
            f"🔄 تمدید: {reset_s}\n"
            f"🎤 اسلات صدا: {sub.get('voice_slots_used', '-')} از {sub.get('voice_limit', '-')}")


async def send_closed(update: Update):
    uid = update.effective_user.id
    text = f"{closed_message()}\n\n🆔 آیدی شما: <code>{uid}</code>"
    if update.callback_query:
        try:
            await update.callback_query.answer()
        except Exception:
            pass
    try:
        await update.effective_message.reply_text(text, parse_mode=HTML)
    except BadRequest:
        await update.effective_message.reply_text(text)


def feature_allowed(uid: int, feature: str) -> bool:
    return is_admin(uid) or feature not in config.ADMIN_ONLY_FEATURES


async def notify_admins(context, text: str):
    for aid in config.ADMIN_IDS:
        try:
            await context.bot.send_message(aid, text[:4000], parse_mode=HTML)
        except Exception:
            pass


async def show(update: Update, text: str, kb=None):
    """اگر از دکمه آمده، همان پیام را ویرایش می‌کند؛ وگرنه پیام جدید می‌فرستد."""
    q = update.callback_query
    if q and q.message and q.message.text:
        try:
            return await q.edit_message_text(text, reply_markup=kb, parse_mode=HTML,
                                             disable_web_page_preview=True)
        except BadRequest as e:
            if "not modified" in str(e).lower():
                return
    return await update.effective_message.reply_text(text, reply_markup=kb, parse_mode=HTML,
                                                     disable_web_page_preview=True)


async def channels_missing(context, uid: int) -> list[str]:
    if not config.REQUIRED_CHANNELS or is_admin(uid):
        return []
    cache = context.user_data.get("join_ok_until", 0)
    if cache > time.time():
        return []
    missing = []
    for ch in config.REQUIRED_CHANNELS:
        try:
            m = await context.bot.get_chat_member(ch, uid)
            if m.status in ("left", "kicked"):
                missing.append(ch)
        except Exception:
            log.warning("cannot check membership for %s (is bot admin there?)", ch)
    if not missing:
        context.user_data["join_ok_until"] = time.time() + 600
    return missing


async def guard(update: Update, context) -> bool:
    """ثبت کاربر + بررسی مسدودی، لیست مجاز و عضویت اجباری."""
    user = update.effective_user
    if not user:
        return False
    u = db.upsert_user(user)
    if u["blocked"]:
        if update.callback_query:
            await update.callback_query.answer("⛔ شما مسدود شده‌اید.", show_alert=True)
        return False
    privileged = is_admin(user.id) or is_white(u)
    if not privileged and not bot_open():
        await send_closed(update)
        return False
    if privileged:
        return True
    missing = await channels_missing(context, user.id)
    if missing:
        rows = [[ui.B(f"📢 عضویت در {ch}", url=f"https://t.me/{ch.lstrip('@')}")] for ch in missing]
        rows.append([btn("✅ عضو شدم", "joined")])
        text = "🔒 برای استفاده از ربات، اول عضو کانال زیر شو:"
        if update.callback_query:
            await update.callback_query.answer()
        await update.effective_message.reply_text(text, reply_markup=M(rows))
        return False
    return True


def get_media(msg) -> dict | None:
    """فایل صوتی/تصویری/سند پیام را برمی‌گرداند."""
    if not msg:
        return None
    for kind in ("voice", "audio", "video", "video_note", "document"):
        obj = getattr(msg, kind, None)
        if obj:
            default_ext = {"voice": "ogg", "audio": "mp3", "video": "mp4", "video_note": "mp4"}.get(kind, "bin")
            name = getattr(obj, "file_name", None) or f"{kind}.{default_ext}"
            return {"file_id": obj.file_id, "filename": name, "size": obj.file_size or 0, "kind": kind,
                    "mime": getattr(obj, "mime_type", "") or ""}
    return None


def is_text_doc(m: dict) -> bool:
    return m and m["kind"] == "document" and m["filename"].lower().endswith((".txt", ".pdf", ".docx", ".md"))


async def download(context, m: dict) -> bytes:
    if m["size"] > MAX_TG_DOWNLOAD:
        raise ValueError("حجم فایل بیشتر از ۲۰ مگابایته (محدودیت تلگرام برای ربات‌ها).")
    f = await context.bot.get_file(m["file_id"])
    return bytes(await f.download_as_bytearray())


def user_voice(u) -> tuple[str, str]:
    return u["voice_id"] or config.DEFAULT_VOICE_ID, u["voice_name"] or "پیش‌فرض"


# ============================ صف و اعتبار ============================
_sem = asyncio.Semaphore(config.MAX_CONCURRENT_JOBS)
_waiting = 0


async def run_job(update: Update, context, feature: str, cost: int, status_text: str, func):
    """اعتبار کم می‌کند، در صف می‌ماند، کار را اجرا و در صورت خطا اعتبار را برمی‌گرداند."""
    global _waiting
    uid = update.effective_user.id
    msg = update.effective_message
    if not feature_allowed(uid, feature):
        await msg.reply_text("🔒 این بخش فقط برای ادمین فعاله.")
        return None
    spent = db.spend(uid, cost, unlimited=is_unlimited(uid))
    if spent is None:
        free, bal = db.remaining(uid)
        await msg.reply_text(
            f"⚠️ اعتبار کافی نیست.\nلازم: <b>{cost:,}</b> | رایگان امروز: <b>{free:,}</b> | موجودی: <b>{bal:,}</b>",
            parse_mode=HTML, reply_markup=M([[btn("💳 خرید اعتبار", "m:credit")], [HOME]]))
        return None

    queued = _sem.locked()
    status = None
    if queued:
        _waiting += 1
        status = await msg.reply_text(f"⏳ در صف هستی… ({_waiting} کار در انتظار)")
    try:
        await _sem.acquire()
    finally:
        if queued:
            _waiting -= 1
    try:
        if status:
            await status.edit_text(status_text)
        else:
            status = await msg.reply_text(status_text)
        await context.bot.send_chat_action(update.effective_chat.id, ChatAction.UPLOAD_VOICE)
        return await func()
    except eleven.ElevenError as e:
        db.refund(uid, spent)
        await msg.reply_text(f"❌ خطای ElevenLabs:\n<code>{esc(str(e))}</code>", parse_mode=HTML,
                             reply_markup=ui.home_kb())
        err = f"{e.status} {e}".lower()
        if any(k in err for k in ("quota", "invalid_api_key", "invalid api key", "payment", "subscription")) \
                and context.bot_data.get("key_alert", 0) < time.time():
            context.bot_data["key_alert"] = time.time() + 3600  # حداکثر ساعتی یک بار
            await notify_admins(context, f"🔑 <b>مشکل حساب/کلید ElevenLabs</b>\n<code>{esc(str(e)[:300])}</code>\n\n"
                                         "اگه کاراکترهای حساب تموم شده، از «🛠 پنل مدیریت ← 🔑 کلید API» کلید جدید ثبت کن.")
    except ValueError as e:
        db.refund(uid, spent)
        await msg.reply_text(f"⚠️ {e}", reply_markup=ui.home_kb())
    except Exception as e:
        db.refund(uid, spent)
        log.exception("job %s failed", feature)
        await msg.reply_text("❌ یه خطای داخلی پیش اومد. دوباره امتحان کن.", reply_markup=ui.home_kb())
        await notify_admins(context, f"⚠️ <b>خطا در {feature}</b>\nکاربر: <code>{uid}</code>\n"
                                     f"<pre>{esc(''.join(traceback.format_exception(e))[-2500:])}</pre>")
    finally:
        _sem.release()
        if status:
            try:
                await status.delete()
            except Exception:
                pass
    return None


async def deliver_audio(update: Update, context, data: bytes, kind: str, title: str,
                        caption: str = "", force_audio: bool = False, reencode: bool = False):
    """صدا را بر اساس تنظیم کاربر به‌صورت ویس یا MP3 می‌فرستد و در تاریخچه ثبت می‌کند."""
    uid = update.effective_user.id
    u = db.get_user(uid)
    msg = update.effective_message
    kb = M([[btn("⚙️ تنظیمات", "m:settings"), HOME]])
    if u["output"] == "voice" and not force_audio:
        ogg = await media.to_ogg_voice(data)
        sent = await msg.reply_voice(voice=ogg, caption=caption or None, reply_markup=kb, parse_mode=HTML)
        db.add_history(uid, kind, title, sent.voice.file_id, "voice")
    else:
        if reencode:
            data = await media.to_mp3(data)
        sent = await msg.reply_audio(audio=InputFile(io.BytesIO(data), filename=f"{kind}.mp3"),
                                     title=title[:60], performer="ElevenLabs",
                                     caption=caption or None, reply_markup=kb, parse_mode=HTML)
        db.add_history(uid, kind, title, sent.audio.file_id, "audio")
    return sent


# ============================ قابلیت‌ها ============================

async def do_tts(update: Update, context, text: str, kind: str = "tts"):
    uid = update.effective_user.id
    u = db.get_user(uid)
    text = text.strip()
    if not text:
        return
    limit = config.MAX_FILE_CHARS if kind == "file" else config.MAX_TEXT_CHARS
    if len(text) > limit:
        return await update.effective_message.reply_text(f"⚠️ متن طولانیه (حداکثر {limit:,} کاراکتر).")
    voice_id, voice_name = user_voice(u)
    model = u["model_id"] or config.DEFAULT_MODEL_ID

    async def job():
        audio = await eleven.tts_long(text, voice_id, model, eleven.voice_settings(u), u["lang"])
        cap = f"🗣 {esc(voice_name)} • {ui.MODEL_SHORT.get(model, model)} • {len(text):,} کاراکتر"
        await deliver_audio(update, context, audio, kind, text, cap)

    await run_job(update, context, kind if kind == "file" else "tts", text_cost(len(text)), "🎙 در حال ساخت صدا…", job)


def parse_dialogue(text: str) -> list[tuple[str, str]]:
    lines = []
    for raw in text.splitlines():
        if ":" not in raw:
            if lines and raw.strip():
                lines[-1] = (lines[-1][0], lines[-1][1] + " " + raw.strip())
            continue
        name, line = raw.split(":", 1)
        if name.strip() and line.strip():
            lines.append((name.strip(), line.strip()))
    return lines


async def do_dialogue(update: Update, context, text: str):
    lines = parse_dialogue(text)
    if len(lines) < 2:
        return await update.effective_message.reply_text(
            "⚠️ حداقل دو خط با فرمت <code>اسم: متن</code> بفرست.", parse_mode=HTML, reply_markup=ui.cancel_kb())
    total = sum(len(t) for _, t in lines)
    if total > 2000:
        return await update.effective_message.reply_text("⚠️ مجموع متن دیالوگ حداکثر ۲۰۰۰ کاراکتره.")
    voices = await get_voices_cached(context)
    speakers = list(dict.fromkeys(n for n, _ in lines))
    if len(speakers) > 10:
        return await update.effective_message.reply_text("⚠️ حداکثر ۱۰ گوینده مجازه.")
    mapping, used = {}, set()
    for s in speakers:  # تطبیق اسم گوینده با اسم صداها
        for v in voices:
            if v["voice_id"] not in used and (s.lower() in v["name"].lower() or v["name"].lower().startswith(s.lower())):
                mapping[s] = v
                used.add(v["voice_id"])
                break
    pool = [v for v in voices if v["voice_id"] not in used]
    for s in speakers:
        if s not in mapping and pool:
            mapping[s] = pool.pop(0)
    inputs = [{"text": t, "voice_id": mapping[n]["voice_id"]} for n, t in lines]
    cast = "\n".join(f"• {esc(s)} ← 🎤 {esc(mapping[s]['name'])}" for s in speakers)

    async def job():
        audio = await eleven.dialogue(inputs, config.DIALOGUE_MODEL_ID)
        await deliver_audio(update, context, audio, "dialogue", f"دیالوگ {len(speakers)} نفره",
                            f"🎭 <b>دیالوگ</b>\n{cast}")

    await run_job(update, context, "dialogue", text_cost(total), "🎭 در حال ساخت دیالوگ…", job)


async def do_stt(update: Update, context, m: dict):
    u = db.get_user(update.effective_user.id)

    async def job():
        data = await download(context, m)
        res = await eleven.stt(data, m["filename"], u["lang"])
        text = (res.get("text") or "").strip() or "(متنی تشخیص داده نشد)"
        lang = res.get("language_code", "")
        msg = update.effective_message
        if len(text) > 3800:
            await msg.reply_document(InputFile(io.BytesIO(text.encode()), filename="transcript.txt"),
                                     caption=f"📝 متن کامل ({lang})", reply_markup=ui.home_kb())
        else:
            await msg.reply_text(f"📝 <b>متن</b> <i>({esc(lang)})</i>\n\n{esc(text)}", parse_mode=HTML,
                                 reply_markup=M([[btn("🗣 خواندن همین متن", "stt:tts")], [HOME]]))
        context.user_data["last_transcript"] = text

    await run_job(update, context, "stt", cost("stt"), "📝 در حال تبدیل به متن…", job)


async def do_changer(update: Update, context, m: dict):
    u = db.get_user(update.effective_user.id)
    voice_id, voice_name = user_voice(u)

    async def job():
        data = await download(context, m)
        audio = await eleven.voice_changer(data, m["filename"], voice_id)
        await deliver_audio(update, context, audio, "changer", f"تغییر صدا به {voice_name}",
                            f"🔄 تغییر صدا ← 🎤 {esc(voice_name)}")

    await run_job(update, context, "changer", cost("changer"), "🔄 در حال تغییر صدا…", job)


async def do_isolate(update: Update, context, m: dict):
    async def job():
        data = await download(context, m)
        audio = await eleven.isolate(data, m["filename"])
        await deliver_audio(update, context, audio, "isolate", "صدای بدون نویز", "🧹 نویز حذف شد",
                            force_audio=True, reencode=True)

    await run_job(update, context, "isolate", cost("isolate"), "🧹 در حال حذف نویز…", job)


async def do_dub(update: Update, context, m: dict, lang: str):
    uid = update.effective_user.id
    chat_id = update.effective_chat.id
    is_video = m["kind"] in ("video", "video_note") or m["mime"].startswith("video/")
    lang_name = dict(ui.DUB_LANGS).get(lang, lang)

    async def job():
        data = await download(context, m)
        dub_id = await eleven.dub_create(data, m["filename"], lang)
        await update.effective_message.reply_text(
            f"🎬 دوبله به {lang_name} شروع شد.\n⏳ ممکنه چند دقیقه طول بکشه؛ آماده که شد خودم می‌فرستم.",
            reply_markup=ui.home_kb())
        context.application.create_task(poll_dub(context, chat_id, uid, dub_id, lang, is_video, lang_name))

    await run_job(update, context, "dub", cost("dub"), "🎬 در حال آپلود برای دوبله…", job)


async def poll_dub(context, chat_id, uid, dub_id, lang, is_video, lang_name):
    deadline = time.time() + 60 * 60
    try:
        while time.time() < deadline:
            await asyncio.sleep(15)
            st = await eleven.dub_status(dub_id)
            if st.get("status") == "failed":
                raise eleven.ElevenError(st.get("error") or "dubbing failed")
            if st.get("status") == "dubbed":
                data, ctype = await eleven.dub_download(dub_id, lang)
                if len(data) > 50 * 1024 * 1024:
                    raise ValueError("فایل دوبله‌شده بیشتر از ۵۰ مگابایته و تلگرام اجازهٔ ارسالش رو نمی‌ده.")
                cap = f"🎬 دوبله به {lang_name}"
                kb = ui.home_kb()
                if is_video or "video" in ctype:
                    sent = await context.bot.send_video(chat_id, InputFile(io.BytesIO(data), filename="dubbed.mp4"),
                                                        caption=cap, reply_markup=kb, supports_streaming=True)
                    db.add_history(uid, "dub", cap, sent.video.file_id, "video")
                else:
                    sent = await context.bot.send_audio(chat_id, InputFile(io.BytesIO(data), filename="dubbed.mp3"),
                                                        caption=cap, title=cap, reply_markup=kb)
                    db.add_history(uid, "dub", cap, sent.audio.file_id, "audio")
                return
        raise ValueError("دوبله بیشتر از یک ساعت طول کشید.")
    except Exception as e:
        log.exception("dub poll failed")
        await context.bot.send_message(chat_id, f"❌ دوبله ناموفق بود:\n<code>{esc(str(e))[:500]}</code>",
                                       parse_mode=HTML, reply_markup=ui.home_kb())


async def do_clone(update: Update, context):
    data = context.user_data.get("clone") or {}
    samples = data.get("files", [])
    if not samples:
        return await update.effective_message.reply_text("⚠️ هنوز نمونه‌ای نفرستادی.")
    name = data.get("name", "My Voice")

    async def job():
        voice_id = await eleven.clone_voice(name, samples)
        db.set_field(update.effective_user.id, "voice_id", voice_id)
        db.set_field(update.effective_user.id, "voice_name", name)
        invalidate_voices(context)
        context.user_data.pop("clone", None)
        context.user_data["state"] = None
        await update.effective_message.reply_text(
            f"🧬 صدای <b>{esc(name)}</b> ساخته و انتخاب شد ✅\nحالا یه متن بفرست تا امتحانش کنی.",
            parse_mode=HTML, reply_markup=ui.home_kb())

    await run_job(update, context, "clone", cost("clone"), "🧬 در حال ساخت صدا…", job)


async def do_design(update: Update, context, description: str):
    if len(description) < 20:
        return await update.effective_message.reply_text("⚠️ توضیح رو کامل‌تر بنویس (حداقل ۲۰ کاراکتر).")

    async def job():
        previews = await eleven.design_voice(description)
        if not previews:
            raise eleven.ElevenError("پیش‌نمایشی ساخته نشد")
        context.user_data["design"] = {"desc": description, "ids": [p["generated_voice_id"] for p in previews]}
        for i, p in enumerate(previews):
            audio = base64.b64decode(p["audio_base_64"])
            ogg = await media.to_ogg_voice(audio)
            await update.effective_message.reply_voice(
                ogg, caption=f"🎨 پیش‌نمایش {i + 1}",
                reply_markup=M([[btn(f"💾 ذخیره و انتخاب صدای {i + 1}", f"dz:{i}")]]))
        await update.effective_message.reply_text("👆 هر کدوم رو دوست داشتی ذخیره کن.", reply_markup=ui.home_kb())

    await run_job(update, context, "design", cost("design"), "🎨 در حال طراحی صدا…", job)


async def do_sfx(update: Update, context, prompt: str):
    dur = context.user_data.get("sfx_dur")

    async def job():
        audio = await eleven.sound_effect(prompt, dur)
        await deliver_audio(update, context, audio, "sfx", prompt, f"💥 {esc(prompt[:200])}", force_audio=True)

    await run_job(update, context, "sfx", cost("sfx"), "💥 در حال ساخت افکت…", job)


async def do_music(update: Update, context, prompt: str):
    sec = context.user_data.get("music_len")
    price = cost("music") * max(1, round((sec or 60) / 60))

    async def job():
        audio = await eleven.music(prompt, sec * 1000 if sec else None)
        await deliver_audio(update, context, audio, "music", prompt, f"🎵 {esc(prompt[:200])}",
                            force_audio=True, reencode=True)

    await run_job(update, context, "music", price, "🎵 در حال ساخت موسیقی… (ممکنه یکی دو دقیقه طول بکشه)", job)


async def do_file(update: Update, context, m: dict):
    if not is_text_doc(m):
        return await update.effective_message.reply_text("⚠️ فقط فایل TXT، PDF، DOCX یا MD.")
    try:
        data = await download(context, m)
        text = await asyncio.to_thread(media.extract_text, data, m["filename"])
    except Exception as e:
        return await update.effective_message.reply_text(f"⚠️ خواندن فایل ممکن نشد: {e}")
    if not text:
        return await update.effective_message.reply_text("⚠️ متنی در فایل پیدا نشد (شاید PDF اسکن‌شده است).")
    await do_tts(update, context, text, kind="file")


# ============================ صداها ============================

async def get_voices_cached(context) -> list[dict]:
    cache = context.bot_data.get("voices")
    if cache and cache[0] > time.time():
        return cache[1]
    voices = await eleven.my_voices()
    context.bot_data["voices"] = (time.time() + 300, voices)
    return voices


def invalidate_voices(context):
    context.bot_data.pop("voices", None)


async def show_voices(update: Update, context, page: int = 0):
    try:
        voices = await get_voices_cached(context)
    except eleven.ElevenError as e:
        return await show(update, f"❌ {esc(str(e))}", ui.home_kb())
    u = db.get_user(update.effective_user.id)
    cur, _ = user_voice(u)
    per = 8
    pages = max(1, (len(voices) + per - 1) // per)
    page = max(0, min(page, pages - 1))
    rows = []
    for v in voices[page * per:(page + 1) * per]:
        icon = "✅" if v["voice_id"] == cur else ui.CATEGORY_ICON.get(v.get("category"), "🎤")
        rows.append([btn(f"{icon} {v['name'][:40]}", f"v:i:{v['voice_id']}")])
    nav = []
    if page > 0:
        nav.append(btn("◀️ قبلی", f"v:p:{page - 1}"))
    nav.append(btn(f"📄 {page + 1}/{pages}", "noop"))
    if page < pages - 1:
        nav.append(btn("بعدی ▶️", f"v:p:{page + 1}"))
    rows.append(nav)
    rows.append([btn("📚 کتابخانه صدا", "m:library"), HOME])
    await show(update, "🎤 <b>صداهای من</b>\n⭐ آماده | 🧬 شبیه‌سازی | 🎨 طراحی | 💎 حرفه‌ای\n\nیکی رو انتخاب کن:", M(rows))


async def show_voice_detail(update: Update, context, voice_id: str):
    voices = await get_voices_cached(context)
    v = next((x for x in voices if x["voice_id"] == voice_id), None)
    if not v:
        return await show(update, "⚠️ این صدا پیدا نشد.", ui.home_kb())
    labels = " • ".join(str(x) for x in (v.get("labels") or {}).values() if x)
    text = (f"🎤 <b>{esc(v['name'])}</b>\n"
            f"📂 نوع: {esc(v.get('category') or '-')}\n"
            + (f"🏷 {esc(labels)}\n" if labels else "")
            + (f"\n{esc((v.get('description') or '')[:300])}" if v.get("description") else ""))
    rows = [[btn("✅ انتخاب این صدا", f"v:s:{voice_id}"), btn("▶️ پیش‌نمایش", f"v:pv:{voice_id}")]]
    if can(update.effective_user.id, "voices") and v.get("category") != "premade":
        rows.append([btn("🗑 حذف از حساب", f"v:d:{voice_id}")])
    rows.append([btn("🔙 بازگشت", "v:p:0"), HOME])
    await show(update, text, M(rows))


async def show_library(update: Update, context, voices: list[dict], title: str):
    context.user_data["lib"] = voices
    if not voices:
        return await show(update, "🔍 چیزی پیدا نشد.", M([[btn("🔍 جستجوی دوباره", "lib:search")], [HOME]]))
    rows = []
    for i, v in enumerate(voices):
        meta = " • ".join(x for x in (v.get("gender"), v.get("language"), v.get("accent")) if x)
        rows.append([btn(f"🔊 {v['name'][:30]} ({meta})"[:60], f"lib:i:{i}")])
    rows.append([btn("🔍 جستجوی دیگر", "lib:search"), HOME])
    await show(update, title, M(rows))


# ============================ منوها ============================

async def open_feature(update: Update, context, key: str):
    uid = update.effective_user.id
    ud = context.user_data
    ud["state"] = None
    if key in ("tts", "dialogue", "stt", "changer", "isolate", "dub", "clone", "design",
               "library", "sfx", "music", "file") and not feature_allowed(uid, key):
        return await show(update, "🔒 این بخش فقط برای ادمین فعاله.", ui.home_kb())
    u = db.get_user(uid)
    _, vname = user_voice(u)

    if key == "main":
        return await show(update, ui.welcome(esc(update.effective_user.first_name or "")), ui.main_menu(is_admin(uid)))
    if key in ("tts", "stt", "changer", "isolate", "file"):
        ud["state"] = key
        extra = f"\n\n🎤 صدای فعلی: <b>{esc(vname)}</b>" if key in ("tts", "changer", "file") else ""
        title, desc = ui.FEATURES[key]
        return await show(update, f"<b>{title}</b>\n\n{desc}{extra}",
                          ui.cancel_kb([btn("🎤 تغییر صدا", "m:voices"), btn("⚙️ تنظیمات", "m:settings")]))
    if key == "dialogue":
        ud["state"] = "dialogue"
        return await show(update, (
            "🎭 <b>دیالوگ چندنفره</b>\n\nهر خط رو این شکلی بنویس:\n"
            "<code>علی: سلام، چطوری؟\nسارا: [laughs] خوبم، تو چطوری؟</code>\n\n"
            "اگه اسم گوینده با اسم یکی از صداهات یکی باشه همون صدا استفاده می‌شه، وگرنه خودکار صدای متفاوت می‌گیره.\n"
            "حداکثر ۱۰ گوینده و ۲۰۰۰ کاراکتر."), ui.cancel_kb())
    if key == "dub":
        rows = ui.rows2([btn(n, f"dub:{c}") for c, n in ui.DUB_LANGS])
        return await show(update, "🎬 <b>دوبله</b>\n\nبه چه زبانی دوبله بشه؟", ui.cancel_kb(*rows))
    if key == "clone":
        ud["state"] = "clone_name"
        ud["clone"] = {"files": []}
        return await show(update, (
            "🧬 <b>شبیه‌سازی صدا</b>\n\n۱️⃣ اول یه <b>اسم</b> برای صدای جدید بفرست.\n"
            "۲️⃣ بعد ۱ تا ۲۵ نمونه ویس/فایل صوتی تمیز (در مجموع ۱ تا ۲ دقیقه) بفرست.\n\n"
            "⚠️ فقط صدای خودت یا کسی که اجازه داده رو شبیه‌سازی کن."), ui.cancel_kb())
    if key == "design":
        ud["state"] = "design"
        return await show(update, (
            "🎨 <b>طراحی صدا</b>\n\nصدای دلخواهت رو توصیف کن (انگلیسی نتیجهٔ بهتری می‌ده). مثال:\n"
            "<code>A warm, calm middle-aged Persian man with a deep voice, speaking slowly like a narrator</code>\n\n"
            "۳ پیش‌نمایش می‌سازم، هر کدوم رو خواستی ذخیره کن."), ui.cancel_kb())
    if key == "library":
        return await show(update, "📚 <b>کتابخانهٔ صدای ElevenLabs</b>\n\nهزاران صدای عمومی؛ می‌تونی به صداهات اضافه کنی.",
                          M([[btn("🇮🇷 صداهای فارسی", "lib:fa"), btn("🔥 محبوب‌ها", "lib:top")],
                             [btn("🔍 جستجو", "lib:search")], [HOME]]))
    if key == "voices":
        return await show_voices(update, context)
    if key == "sfx":
        rows = [[btn("🎲 خودکار", "sfx:0"), btn("3 ثانیه", "sfx:3"), btn("5 ثانیه", "sfx:5")],
                [btn("10 ثانیه", "sfx:10"), btn("20 ثانیه", "sfx:20")]]
        return await show(update, "💥 <b>افکت صوتی</b>\n\nمدت افکت رو انتخاب کن:", ui.cancel_kb(*rows))
    if key == "music":
        rows = [[btn("🎲 خودکار", "mu:0"), btn("30 ثانیه", "mu:30"), btn("1 دقیقه", "mu:60")],
                [btn("2 دقیقه", "mu:120"), btn("3 دقیقه", "mu:180")]]
        return await show(update, f"🎵 <b>ساخت موسیقی</b>\n\nطول آهنگ رو انتخاب کن:\n"
                                  f"<i>هزینه: {cost('music'):,} اعتبار به ازای هر دقیقه</i>", ui.cancel_kb(*rows))
    if key == "history":
        items = db.get_history(uid)
        if not items:
            return await show(update, "🕘 تاریخچه‌ات خالیه.", ui.home_kb())
        icons = {"tts": "🗣", "file": "📄", "dialogue": "🎭", "changer": "🔄", "isolate": "🧹", "dub": "🎬",
                 "sfx": "💥", "music": "🎵", "inline": "⚡"}
        rows = [[btn(f"{icons.get(h['kind'], '🎧')} {h['title'][:45]}", f"h:{h['id']}")] for h in items]
        rows.append([HOME])
        return await show(update, "🕘 <b>۱۰ مورد آخر</b>\nروی هر کدوم بزنی دوباره ارسال می‌شه (رایگان):", M(rows))
    if key == "settings":
        return await show(update, ui.settings_text(u), ui.settings_kb(u))
    if key == "credit":
        free, bal = db.remaining(uid)
        unlimited = ("\n♾ <b>شما ادمین هستید؛ محدودیتی ندارید.</b>" if is_admin(uid) else
                     "\n⭐ <b>شما در لیست سفید هستید؛ محدودیتی ندارید.</b>" if is_white(u) else "")
        costs = "\n".join(f"• {label}: {ui.num(cost(k))}" for k, label in ui.COST_LABELS.items())
        shop = ("⭐ <b>خرید اعتبار با Telegram Stars:</b>" if stars_enabled() and packages()
                else "<i>🔒 خرید اعتبار در حال حاضر غیرفعال است.</i>")
        return await show(update, (
            f"💳 <b>اعتبار من</b>{unlimited}\n\n🎁 رایگان امروز: <b>{free:,}</b> از {db.daily_limit_for(u):,}\n"
            f"💰 موجودی خریداری‌شده: <b>{bal:,}</b>\n\n📌 <b>هزینه‌ها (اعتبار)</b>\n{costs}\n\n{shop}"),
            ui.credit_kb(packages(), stars_enabled()))
    if key == "help":
        return await show(update, ui.HELP.replace("{bot}", context.bot.username or "bot"), ui.home_kb())
    if key == "admin" and is_admin(uid):
        return await show(update, admin_panel_text(), akb(uid))


# ============================ هندلرها ============================

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update, context):
        return
    context.user_data["state"] = None
    await update.message.reply_text(ui.welcome(esc(update.effective_user.first_name or "")),
                                    reply_markup=ui.main_menu(is_admin(update.effective_user.id)), parse_mode=HTML)


SETTING_CYCLES = {
    "stability": [0.0, 0.5, 1.0],
    "similarity": [0.25, 0.5, 0.75, 1.0],
    "style": [0.0, 0.25, 0.5, 0.75],
    "speed": [0.7, 0.85, 1.0, 1.1, 1.2],
    "lang": list(ui.LANGS.keys()),
    "output": ["voice", "mp3"],
}


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not await guard(update, context):
        return
    data = q.data or ""
    uid = q.from_user.id
    ud = context.user_data
    try:
        await q.answer()
    except Exception:
        pass

    if data == "noop":
        return
    if data == "joined":
        return await open_feature(update, context, "main")
    if data.startswith("m:"):
        return await open_feature(update, context, data[2:])

    # --- تنظیمات ---
    if data.startswith("s:"):
        u = db.get_user(uid)
        if data == "s:models":
            rows = [[btn(("✅ " if u["model_id"] == mid else "") + label, f"s:m:{mid}")] for mid, label in ui.MODELS]
            rows.append([btn("🔙 بازگشت", "m:settings")])
            return await show(update, "🧠 <b>انتخاب مدل</b>\n\n⚠️ برای فارسی فقط <b>v3</b> مناسبه.", M(rows))
        if data.startswith("s:m:"):
            db.set_field(uid, "model_id", data[4:])
        elif data.startswith("s:c:"):
            field = data[4:]
            opts = SETTING_CYCLES[field]
            cur = u[field]
            idx = opts.index(cur) if cur in opts else -1
            db.set_field(uid, field, opts[(idx + 1) % len(opts)])
        elif data == "s:reset":
            for f, v in (("stability", 0.5), ("similarity", 0.75), ("style", 0.0), ("speed", 1.0),
                         ("lang", "auto"), ("output", "voice"), ("model_id", config.DEFAULT_MODEL_ID)):
                db.set_field(uid, f, v)
        u = db.get_user(uid)
        return await show(update, ui.settings_text(u), ui.settings_kb(u))

    # --- صداها ---
    if data.startswith("v:"):
        _, action, arg = data.split(":", 2)
        if action == "p":
            return await show_voices(update, context, int(arg))
        if action == "i":
            return await show_voice_detail(update, context, arg)
        if action == "s":
            voices = await get_voices_cached(context)
            v = next((x for x in voices if x["voice_id"] == arg), {"name": arg})
            db.set_field(uid, "voice_id", arg)
            db.set_field(uid, "voice_name", v["name"])
            return await show_voices(update, context)
        if action == "pv":
            voices = await get_voices_cached(context)
            v = next((x for x in voices if x["voice_id"] == arg), None)
            if v and v.get("preview_url"):
                try:
                    return await q.message.reply_audio(v["preview_url"], title=v["name"], performer="Preview")
                except Exception:
                    return await q.message.reply_text(f"▶️ {v['preview_url']}")
            return await q.message.reply_text("پیش‌نمایشی برای این صدا نیست.")
        if action == "d" and can(uid, "voices"):
            return await show(update, "🗑 مطمئنی این صدا از حساب ElevenLabs حذف بشه؟",
                              M([[btn("✅ بله، حذف کن", f"v:dd:{arg}"), btn("❌ نه", f"v:i:{arg}")]]))
        if action == "dd" and can(uid, "voices"):
            try:
                await eleven.delete_voice(arg)
                invalidate_voices(context)
            except eleven.ElevenError as e:
                return await show(update, f"❌ {esc(str(e))}", ui.home_kb())
            return await show_voices(update, context)
        return

    # --- کتابخانه ---
    if data.startswith("lib:"):
        parts = data.split(":")
        try:
            if parts[1] == "fa":
                return await show_library(update, context, await eleven.shared_voices(language="fa"),
                                          "🇮🇷 <b>صداهای فارسی</b>")
            if parts[1] == "top":
                return await show_library(update, context, await eleven.shared_voices(), "🔥 <b>صداهای محبوب</b>")
            if parts[1] == "search":
                ud["state"] = "lib_search"
                return await show(update, "🔍 کلمهٔ جستجو رو بفرست (مثلاً <code>narrator</code> یا <code>deep male</code>):",
                                  ui.cancel_kb())
            lib = ud.get("lib") or []
            i = int(parts[2])
            if i >= len(lib):
                return await show(update, "⚠️ نتایج منقضی شده، دوباره جستجو کن.", ui.home_kb())
            v = lib[i]
            if parts[1] == "i":
                meta = "\n".join(f"• {k}: {esc(str(v.get(k)))}" for k in
                                 ("gender", "age", "accent", "language", "use_case", "descriptive") if v.get(k))
                return await show(update, f"🔊 <b>{esc(v['name'])}</b>\n{meta}\n\n{esc((v.get('description') or '')[:400])}",
                                  M([[btn("➕ افزودن و انتخاب", f"lib:add:{i}"), btn("▶️ پیش‌نمایش", f"lib:p:{i}")],
                                     [btn("🔙 نتایج", "lib:back:0"), HOME]]))
            if parts[1] == "back":
                return await show_library(update, context, lib, "📚 <b>نتایج</b>")
            if parts[1] == "p":
                if v.get("preview_url"):
                    return await q.message.reply_audio(v["preview_url"], title=v["name"], performer="Preview")
                return
            if parts[1] == "add":
                if not feature_allowed(uid, "library"):
                    return await show(update, "🔒 این بخش فقط برای ادمین فعاله.", ui.home_kb())
                new_id = await eleven.add_shared_voice(v["public_owner_id"], v["voice_id"], v["name"])
                invalidate_voices(context)
                db.set_field(uid, "voice_id", new_id)
                db.set_field(uid, "voice_name", v["name"])
                return await show(update, f"✅ صدای <b>{esc(v['name'])}</b> اضافه و انتخاب شد.\nحالا یه متن بفرست 🎙",
                                  ui.home_kb())
        except eleven.ElevenError as e:
            return await show(update, f"❌ {esc(str(e))}", ui.home_kb())
        return

    # --- انتخاب‌های چندمرحله‌ای ---
    if data.startswith("dub:"):
        lang = data[4:]
        pending = ud.pop("pending", None)
        if pending:
            return await do_dub(update, context, pending, lang)
        ud["state"], ud["dub_lang"] = "dub", lang
        return await show(update, f"🎬 حالا ویدیو یا فایل صوتی رو بفرست (حداکثر ۲۰ مگابایت).\n"
                                  f"زبان مقصد: {dict(ui.DUB_LANGS)[lang]}", ui.cancel_kb())
    if data.startswith("sfx:"):
        n = int(data[4:])
        ud["state"], ud["sfx_dur"] = "sfx", (n or None)
        return await show(update, "💥 افکت رو توصیف کن (انگلیسی بهتره). مثال:\n<code>heavy rain on a tin roof with distant thunder</code>",
                          ui.cancel_kb())
    if data.startswith("mu:"):
        n = int(data[3:])
        ud["state"], ud["music_len"] = "music", (n or None)
        return await show(update, "🎵 آهنگ رو توصیف کن. مثال:\n<code>calm persian santoor and piano, cinematic, instrumental</code>",
                          ui.cancel_kb())
    if data == "cl:done":
        return await do_clone(update, context)
    if data.startswith("dz:"):
        d = ud.get("design")
        i = int(data[3:])
        if not d or i >= len(d["ids"]):
            return await q.message.reply_text("⚠️ پیش‌نمایش‌ها منقضی شده‌اند.")
        name = f"Designed {d['desc'][:25]}"
        try:
            vid = await eleven.save_designed_voice(name, d["desc"], d["ids"][i])
        except eleven.ElevenError as e:
            return await q.message.reply_text(f"❌ {esc(str(e))}", parse_mode=HTML)
        invalidate_voices(context)
        db.set_field(uid, "voice_id", vid)
        db.set_field(uid, "voice_name", name)
        ud["state"] = None
        return await q.message.reply_text(f"✅ صدای {i + 1} ذخیره و انتخاب شد. حالا متن بفرست 🎙", reply_markup=ui.home_kb())
    if data == "stt:tts":
        text = ud.get("last_transcript")
        if text:
            return await do_tts(update, context, text)
        return
    if data.startswith("pf:"):  # فایل ارسالی بدون انتخاب بخش
        m = ud.get("pending")
        if not m:
            return await q.message.reply_text("⚠️ فایل منقضی شده، دوباره بفرست.")
        action = data[3:]
        if action == "dub":
            rows = ui.rows2([btn(n, f"dub:{c}") for c, n in ui.DUB_LANGS])
            return await show(update, "🎬 به چه زبانی دوبله بشه؟", ui.cancel_kb(*rows))
        ud.pop("pending", None)
        return await {"stt": do_stt, "changer": do_changer, "isolate": do_isolate, "file": do_file}[action](update, context, m)
    if data.startswith("h:"):
        h = db.get_history_item(uid, int(data[2:]))
        if not h:
            return
        send = {"voice": q.message.reply_voice, "audio": q.message.reply_audio,
                "video": q.message.reply_video}.get(h["file_type"], q.message.reply_document)
        return await send(h["file_id"], caption=h["title"][:200])

    # --- خرید ---
    if data.startswith("buy:"):
        pk, i = packages(), int(data[4:])
        if not stars_enabled() or i >= len(pk):
            return await show(update, "⚠️ این بسته دیگه موجود نیست.", ui.home_kb())
        credits, stars = pk[i]["credits"], pk[i]["stars"]
        return await context.bot.send_invoice(
            chat_id=q.message.chat_id, title=(pk[i].get("title") or f"{credits:,} اعتبار")[:32],
            description=f"خرید {credits:,} اعتبار برای ربات صوتی ElevenLabs",
            payload=f"credits:{credits}:{stars}", currency="XTR",
            prices=[LabeledPrice(f"{credits:,} اعتبار", stars)])

    # --- ادمین ---
    if data.startswith("a:") and is_admin(uid):
        action = data[2:]
        if not allowed(uid, A_PERM.get(action)):
            return await show(update, NO_ACCESS, akb(uid))
        if action == "api":
            return await show(update, *api_menu())
        if action == "apitest":
            try:
                sub = await eleven.subscription()
            except eleven.ElevenError as e:
                return await show(update, f"❌ کلید فعلی کار نمی‌کنه:\n<code>{esc(str(e))}</code>", api_menu()[1])
            return await show(update, "✅ <b>کلید فعلی سالمه</b>\n\n" + sub_text(sub), api_menu()[1])
        if action == "apienv":
            db.del_setting("api_key")
            invalidate_voices(context)
            text, kb = api_menu()
            return await show(update, "↩️ کلید فایل .env دوباره فعال شد.\n\n" + text, kb)
        if action == "admins":
            return await show(update, *admins_menu())
        if action == "stats":
            s = db.stats()
            return await show(update, (
                "📊 <b>آمار</b>\n\n"
                f"👥 کل کاربران: <b>{s['users']:,}</b>\n🟢 فعال ۲۴ ساعت: <b>{s['active_24h']:,}</b>\n"
                f"🆕 جدید ۲۴ ساعت: <b>{s['new_24h']:,}</b>\n🚫 مسدود: <b>{s['blocked']:,}</b>\n\n"
                f"🔤 مصرف امروز: <b>{s['used_today']:,}</b>\n🔤 مصرف کل: <b>{s['used_total']:,}</b>\n"
                f"🎧 تعداد ساخته‌ها: <b>{s['generations']:,}</b>\n⭐ ستاره‌های دریافتی: <b>{s['stars']:,}</b>\n"
                f"⏳ کارهای در صف: <b>{_waiting}</b>"), akb(uid))
        if action == "acct":
            try:
                sub = await eleven.subscription()
            except eleven.ElevenError as e:
                return await show(update, f"❌ {esc(str(e))}", akb(uid))
            return await show(update, "💼 <b>حساب ElevenLabs</b>\n\n" + sub_text(sub), akb(uid))
        if action == "toggle":
            db.set_setting("bot_open", "0" if bot_open() else "1")
            return await show(update, admin_panel_text(), akb(uid))
        if action == "cmsg":
            label = "اختصاصی ✏️" if db.get_setting("closed_msg") else "پیش‌فرض"
            return await show(update, f"✉️ <b>پیام حالت بسته</b> ({label})\n"
                                      f"این پیام وقتی ربات بسته است برای کاربران عادی ارسال می‌شه:\n\n"
                                      f"────────────\n{closed_message()}\n────────────",
                              M([[btn("✏️ ویرایش پیام", "a:cmsg_edit"), btn("🗑 حذف پیام", "a:cmsg_del")],
                                 [btn("🔙 پنل مدیریت", "m:admin")]]))
        if action == "cmsg_del":
            db.del_setting("closed_msg")
            return await show(update, "🗑 پیام اختصاصی حذف شد و از این به بعد پیام پیش‌فرض نمایش داده می‌شه:\n\n"
                                      f"{ui.DEFAULT_CLOSED_MSG}", M([[btn("🔙 پنل مدیریت", "m:admin")]]))
        if action == "wl":
            users = db.whitelisted_users()
            rows = [[btn(f"⭐ {(w['first_name'] or 'بدون نام')[:25]} · {w['id']}", f"u:v:{w['id']}")] for w in users[:40]]
            rows.append([btn("➕ افزودن به لیست سفید", "a:wladd")])
            rows.append([btn("🔙 پنل مدیریت", "m:admin")])
            text = (f"⭐ <b>لیست سفید</b> ({len(users)} نفر)\n\nاین کاربران حتی وقتی ربات بسته است، بدون محدودیت اعتبار "
                    "از ربات استفاده می‌کنن.\nروی هر نفر بزنی، مدیریتش می‌کنی.")
            return await show(update, text, M(rows))
        prompts = {
            "cmsg_edit": "✏️ متن جدید پیام حالت بسته رو بفرست.\n<i>می‌تونی از بولد، ایتالیک، لینک و ایموجی استفاده کنی.</i>",
            "wladd": "➕ آیدی عددی یا @یوزرنیم کاربر رو بفرست:\n<i>(با آیدی عددی، حتی کاربری که هنوز ربات رو استارت نکرده هم اضافه می‌شه)</i>",
            "user": "👤 آیدی عددی یا @یوزرنیم کاربر رو بفرست:",
            "daily": f"🎁 اعتبار رایگان روزانهٔ کاربران عادی رو بفرست (عدد).\nمقدار فعلی: <b>{db.daily_free_default():,}</b>\n"
                     "<i>۰ یعنی بدون اعتبار رایگان (فقط با خرید یا هدیهٔ ادمین).</i>",
            "bc": "📢 پیامی که می‌خوای برای همه ارسال بشه رو بفرست (متن، عکس، ویس…):",
            "apiset": "🔑 کلید API جدید ElevenLabs رو بفرست.\n<i>قبل از ذخیره تست می‌شه و پیامت برای امنیت پاک می‌شه.</i>",
            "admadd": "👑 آیدی عددی یا @یوزرنیم کسی که می‌خوای مدیر بشه رو بفرست:",
        }
        if action in prompts:
            ud["state"] = f"a_{action}"
            back = {"apiset": "a:api", "admadd": "a:admins"}.get(action, "m:admin")
            return await show(update, prompts[action], M([[btn("❌ لغو", back)]]))

    # --- هزینه‌ها ---
    if data == "a:costs" and can(uid, "costs"):
        return await show(update, *costs_menu())
    if data.startswith("c:") and can(uid, "costs"):
        if data == "c:reset":
            for k in ui.COST_LABELS:
                db.del_setting(f"cost:{k}")
            return await show(update, *costs_menu())
        key = data.split(":", 2)[2]
        if key in ui.COST_LABELS:
            ud["state"], ud["target"] = "a_cost", key
            return await show(update, f"✏️ <b>{ui.COST_LABELS[key]}</b>\nمقدار فعلی: <b>{ui.num(cost(key))}</b>\n\nمقدار جدید رو بفرست:",
                              M([[btn("❌ لغو", "a:costs")]]))

    # --- بسته‌های استارز ---
    if data.startswith("p:") and can(uid, "packages"):
        parts = data.split(":")
        action = parts[1]
        pk = packages()
        if action == "list":
            return await show(update, *packages_menu())
        if action == "toggle":
            db.set_setting("stars_enabled", "0" if stars_enabled() else "1")
            return await show(update, *packages_menu())
        if action == "reset":
            db.del_setting("packages")
            return await show(update, *packages_menu())
        if action == "add":
            if len(pk) >= 8:
                return await show(update, "⚠️ حداکثر ۸ بسته مجازه.", M([[btn("🔙 بسته‌ها", "p:list")]]))
            ud["state"] = "a_padd"
            return await show(update, ("➕ <b>بستهٔ جدید</b>\n\nبه این شکل بفرست:\n<code>اعتبار ستاره عنوان</code>\n\n"
                                       "مثال: <code>10000 80 🥈 نقره‌ای</code>\n<i>عنوان اختیاریه.</i>"),
                              M([[btn("❌ لغو", "p:list")]]))
        i = int(parts[2])
        if i >= len(pk):
            return await show(update, *packages_menu())
        if action == "v":
            return await show(update, *package_menu(i))
        if action == "up":
            if i > 0:
                pk[i - 1], pk[i] = pk[i], pk[i - 1]
                save_packages(pk)
            return await show(update, *packages_menu())
        if action == "del":
            return await show(update, f"🗑 بستهٔ «{esc(ui.package_label(pk[i]))}» حذف بشه؟",
                              M([[btn("✅ بله، حذف کن", f"p:dd:{i}"), btn("❌ نه", f"p:v:{i}")]]))
        if action == "dd":
            pk.pop(i)
            save_packages(pk)
            return await show(update, *packages_menu())
        if action in ("ec", "es", "et"):
            ud["state"], ud["target"] = f"a_p{action}", i
            prompts = {"ec": f"✏️ تعداد اعتبار جدید رو بفرست (فعلی: {pk[i]['credits']:,}):",
                       "es": f"✏️ قیمت جدید به ستاره رو بفرست (فعلی: {pk[i]['stars']:,}):",
                       "et": "🏷 عنوان جدید رو بفرست (حداکثر ۲۰ کاراکتر).\nبرای حذف عنوان، علامت <code>-</code> بفرست."}
            return await show(update, prompts[action], M([[btn("❌ لغو", f"p:v:{i}")]]))

    # --- مدیریت یک کاربر ---
    if data.startswith("u:") and is_admin(uid):
        _, action, target_s = data.split(":", 2)
        target = int(target_s)
        t = db.get_user(target)
        if not t:
            return await show(update, "⚠️ کاربر پیدا نشد.", akb(uid))
        need = {"v": ("users", "credit"), "wl": "users", "bl": "users"}.get(action, "credit")
        if not allowed(uid, need):
            return await show(update, NO_ACCESS, akb(uid))
        if action != "v" and is_admin(target) and not is_owner(uid):
            return await show_user_card(update, t, "\n\n⚠️ <i>تغییر مدیرها فقط دست مدیر اصلیه.</i>")
        note = ""
        if action == "wl":
            db.set_field(target, "whitelisted", 0 if t["whitelisted"] else 1)
            if t["whitelisted"]:
                msg_ = "ℹ️ شما از لیست سفید ربات حذف شدید."
                msg_ += ("\nاز این به بعد با سهمیهٔ عادی از ربات استفاده می‌کنید." if bot_open() else
                         "\nربات در حال حاضر برای کاربران عادی بسته است.")
            else:
                msg_ = "⭐ شما به لیست سفید اضافه شدید و حالا بدون محدودیت از ربات استفاده می‌کنید."
            note = sent_note(await notify_user(context, t, msg_))
        elif action == "bl":
            if is_owner(target):
                return await show_user_card(update, t, "\n\n⚠️ <i>مدیر اصلی رو نمی‌شه مسدود کرد.</i>")
            db.set_field(target, "blocked", 0 if t["blocked"] else 1)
            msg_ = ("✅ مسدودیت شما برداشته شد و دوباره می‌تونید از ربات استفاده کنید." if t["blocked"] else
                    "🚫 دسترسی شما به ربات توسط مدیر مسدود شد.")
            note = sent_note(await notify_user(context, t, msg_))
        elif action == "dlr":
            db.set_field(target, "daily_limit", None)
        elif action in ("add", "dl"):
            ud["state"], ud["target"] = f"a_u{action}", target
            prompts = {"add": f"➕ مقدار اعتبار رو بفرست (موجودی فعلی: {t['balance']:,}).\n"
                              "<i>عدد منفی = کسر اعتبار (مثلاً -500)</i>",
                       "dl": f"🎁 سهمیهٔ روزانهٔ اختصاصی این کاربر رو بفرست (پیش‌فرض همه: {db.daily_free_default():,}):"}
            return await show(update, prompts[action], M([[btn("❌ لغو", f"u:v:{target}")]]))
        return await show_user_card(update, db.get_user(target), note)

    if data.startswith("g:") and not is_owner(uid):
        return await show(update, NO_ACCESS, akb(uid) if is_admin(uid) else ui.home_kb())

    # --- تنظیم مدیر (فقط مدیر اصلی) ---
    if data.startswith("g:") and is_owner(uid):
        parts = data.split(":")
        action, target = parts[1], int(parts[2])
        t = db.get_user(target) or db.ensure_user(target)
        if is_owner(target):
            return await show(update, "👑 این کاربر <b>مدیر اصلیه</b> (از فایل .env) و همیشه دسترسی کامل داره.",
                              M([[btn("👤 کارت کاربر", f"u:v:{target}"), btn("👑 مدیران", "a:admins")]]))
        before = db.admin_perms_of(target)
        perms = set(before or ())
        if action == "mk":
            pass
        elif action == "t" and len(parts) > 3 and parts[3] in ui.ADMIN_PERMS:
            perms ^= {parts[3]}
        elif action == "all":
            perms = set(ui.ADMIN_PERMS)
        elif action == "none":
            perms = set()
        if action == "rm":
            db.set_field(target, "admin_perms", None)
            ok = await notify_user(context, t, "ℹ️ دسترسی مدیریت شما در ربات برداشته شد.")
            return await show_admin_perm(update, db.get_user(target), sent_note(ok))
        note = ""
        if action != "v":
            db.set_field(target, "admin_perms", ",".join(p for p in ui.ADMIN_PERMS if p in perms))
            if before is None:
                ok = await notify_user(context, t, "👑 <b>شما به‌عنوان مدیر ربات انتخاب شدید!</b>\n"
                                                   "با /start وارد منو بشید و روی «🛠 پنل مدیریت» بزنید.")
                note = sent_note(ok)
        return await show_admin_perm(update, db.get_user(target), note)


async def on_private_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await guard(update, context):
        return
    msg = update.message
    uid = update.effective_user.id
    ud = context.user_data
    state = ud.get("state")
    text = msg.text or ""
    m = get_media(msg)

    # --- حالت‌های ادمین ---
    if state and state.startswith("a_") and is_admin(uid):
        ud["state"] = None
        if state == "a_bc":
            context.application.create_task(broadcast(context, msg.chat_id, msg.message_id, uid))
            return await msg.reply_text("📢 ارسال همگانی شروع شد…", reply_markup=akb(uid))
        if state == "a_cmsg_edit":
            if not text:
                return await msg.reply_text("⚠️ فقط متن قبول می‌شه.", reply_markup=akb(uid))
            db.set_setting("closed_msg", msg.text_html)
            return await msg.reply_text(f"✅ پیام حالت بسته ذخیره شد:\n\n{msg.text_html}", parse_mode=HTML,
                                        reply_markup=akb(uid))
        if state in ("a_wladd", "a_user"):
            t = db.find_user(text)
            if not t and text.strip().isdigit() and state == "a_wladd":
                t = db.ensure_user(int(text.strip()))
            if not t:
                return await msg.reply_text("⚠️ کاربر پیدا نشد. (با @یوزرنیم فقط کاربرانی که ربات رو استارت کرده‌اند پیدا می‌شن؛ "
                                            "آیدی عددی رو امتحان کن.)", reply_markup=akb(uid))
            note = ""
            if state == "a_wladd" and not t["whitelisted"]:
                db.set_field(t["id"], "whitelisted", 1)
                note = sent_note(await notify_user(
                    context, t, "⭐ شما به لیست سفید اضافه شدید و حالا بدون محدودیت از ربات استفاده می‌کنید."))
            return await show_user_card(update, db.get_user(t["id"]), note)
        if state == "a_admadd":
            t = db.find_user(text)
            if not t and text.strip().isdigit():
                t = db.ensure_user(int(text.strip()))
            if not t:
                return await msg.reply_text("⚠️ کاربر پیدا نشد. آیدی عددی رو امتحان کن.",
                                            reply_markup=M([[btn("👑 مدیران", "a:admins")]]))
            return await show_admin_perm(update, t)
        if state == "a_apiset":
            key = text.strip()
            try:
                await msg.delete()
            except Exception:
                pass
            back = M([[btn("🔑 کلید API", "a:api"), btn("🔙 پنل مدیریت", "m:admin")]])
            if len(key) < 20 or " " in key:
                return await msg.chat.send_message("⚠️ این شبیه کلید API نیست. دوباره از «✏️ تغییر کلید» امتحان کن.",
                                                   reply_markup=back)
            wait_msg = await msg.chat.send_message("⏳ در حال تست کلید…")
            status_, info = await eleven.check_key(key)
            if status_ == "invalid":
                return await wait_msg.edit_text(f"❌ <b>کلید نامعتبره</b> و ذخیره نشد. کلید قبلی فعال مونده.\n"
                                                f"<code>{esc(info)}</code>", parse_mode=HTML, reply_markup=back)
            db.set_setting("api_key", key)
            invalidate_voices(context)
            head = f"✅ <b>کلید جدید ثبت و فعال شد</b>\n🔑 <code>{esc(eleven.mask(key))}</code>\n\n"
            body = (sub_text(info) if status_ == "ok" else
                    f"⚠️ کلید ذخیره شد ولی اطلاعات حساب خونده نشد:\n<code>{esc(info)}</code>\n"
                    "<i>(اگه کلید محدود ساختی، دسترسی‌های لازم رو بهش بده.)</i>")
            return await wait_msg.edit_text(head + body, parse_mode=HTML, reply_markup=back)
        if state == "a_padd":
            bits = text.split(maxsplit=2)
            try:
                credits, stars = int(bits[0].replace(",", "")), int(bits[1].replace(",", ""))
                assert credits > 0 and 1 <= stars <= 10000
            except Exception:
                return await msg.reply_text("⚠️ فرمت درست: «اعتبار ستاره عنوان» — مثل 10000 80 نقره‌ای",
                                            reply_markup=M([[btn("⭐ بسته‌ها", "p:list")]]))
            pk = packages()
            pk.append({"credits": credits, "stars": stars, "title": (bits[2] if len(bits) > 2 else "")[:20]})
            save_packages(pk)
            return await show(update, *packages_menu())
        if state == "a_pet":
            i, pk = ud.pop("target", -1), packages()
            if 0 <= i < len(pk):
                pk[i]["title"] = "" if text.strip() == "-" else text.strip()[:20]
                save_packages(pk)
                return await show(update, *package_menu(i))
            return await show(update, *packages_menu())
        if state == "a_cost":
            key = ud.pop("target", None)
            try:
                value = float(text.strip().replace(",", "")) if key == "tts" else int(text.strip().replace(",", ""))
                assert value >= 0
            except Exception:
                return await msg.reply_text("⚠️ یه عدد مثبت (یا ۰) بفرست.", reply_markup=M([[btn("💰 هزینه‌ها", "a:costs")]]))
            if key in ui.COST_LABELS:
                db.set_setting(f"cost:{key}", value)
            return await show(update, *costs_menu())
        if state in ("a_pec", "a_pes"):
            i, pk = ud.pop("target", -1), packages()
            n_s = text.strip().replace(",", "")
            if not n_s.isdigit() or int(n_s) <= 0 or (state == "a_pes" and int(n_s) > 10000):
                return await msg.reply_text("⚠️ یه عدد مثبت بفرست (قیمت حداکثر ۱۰٬۰۰۰ ستاره).",
                                            reply_markup=M([[btn("⭐ بسته‌ها", "p:list")]]))
            if 0 <= i < len(pk):
                pk[i]["credits" if state == "a_pec" else "stars"] = int(n_s)
                save_packages(pk)
                return await show(update, *package_menu(i))
            return await show(update, *packages_menu())
        number = text.strip().replace(",", "")
        if not number.lstrip("-").isdigit():
            return await msg.reply_text("⚠️ باید عدد بفرستی.", reply_markup=akb(uid))
        value = int(number)
        if state == "a_daily":
            if value < 0:
                return await msg.reply_text("⚠️ عدد منفی مجاز نیست.", reply_markup=akb(uid))
            db.set_setting("daily_free", value)
            return await msg.reply_text(f"✅ اعتبار روزانهٔ کاربران عادی: {value:,}", reply_markup=akb(uid))
        target = ud.pop("target", None)
        if target and db.get_user(target):
            if state == "a_uadd":
                if value == 0:
                    return await show_user_card(update, db.get_user(target))
                delta = db.add_balance(target, value)
                t = db.get_user(target)
                if delta > 0:
                    txt = f"🎁 <b>{delta:,}</b> اعتبار به حساب شما اضافه شد."
                elif delta < 0:
                    txt = f"➖ <b>{-delta:,}</b> اعتبار از حساب شما کسر شد."
                else:
                    return await show_user_card(update, t, "\n\nℹ️ <i>موجودی کاربر صفر بود؛ چیزی کسر نشد.</i>")
                ok = await notify_user(context, t, f"{txt}\n💰 موجودی فعلی شما: <b>{t['balance']:,}</b>")
                return await show_user_card(update, t, sent_note(ok))
            elif state == "a_udl" and value >= 0:
                db.set_field(target, "daily_limit", value)
            else:
                return await msg.reply_text("⚠️ عدد منفی مجاز نیست.", reply_markup=akb(uid))
            return await show_user_card(update, db.get_user(target))
        return await msg.reply_text("⚠️ ورودی نامعتبر.", reply_markup=akb(uid))

    # --- حالت‌های متنی ---
    if text:
        if state == "dialogue":
            return await do_dialogue(update, context, text)
        if state == "design":
            return await do_design(update, context, text)
        if state == "sfx":
            return await do_sfx(update, context, text)
        if state == "music":
            return await do_music(update, context, text)
        if state == "lib_search":
            ud["state"] = None
            try:
                voices = await eleven.shared_voices(search=text)
            except eleven.ElevenError as e:
                return await msg.reply_text(f"❌ {e}")
            return await show_library(update, context, voices, f"🔍 نتایج «{esc(text[:30])}»")
        if state == "clone_name":
            ud["clone"] = {"name": text[:50], "files": []}
            ud["state"] = "clone_samples"
            return await msg.reply_text(f"✅ اسم: <b>{esc(text[:50])}</b>\nحالا نمونه‌های صوتی رو بفرست 🎙",
                                        parse_mode=HTML, reply_markup=ui.cancel_kb())
        if state in ("stt", "changer", "isolate", "dub", "clone_samples", "file"):
            return await msg.reply_text("📎 اینجا باید فایل یا ویس بفرستی.", reply_markup=ui.cancel_kb())
        return await do_tts(update, context, text)  # حالت پیش‌فرض: متن به صدا

    if not m:
        return
    # --- حالت‌های فایلی ---
    if state == "stt":
        return await do_stt(update, context, m)
    if state == "changer":
        return await do_changer(update, context, m)
    if state == "isolate":
        return await do_isolate(update, context, m)
    if state == "file":
        return await do_file(update, context, m)
    if state == "dub":
        return await do_dub(update, context, m, ud.get("dub_lang", "en"))
    if state == "clone_samples":
        clone = ud.setdefault("clone", {"name": "My Voice", "files": []})
        if len(clone["files"]) >= 25:
            return await msg.reply_text("⚠️ حداکثر ۲۵ نمونه.")
        try:
            data = await download(context, m)
        except ValueError as e:
            return await msg.reply_text(f"⚠️ {e}")
        clone["files"].append((m["filename"], data))
        n = len(clone["files"])
        return await msg.reply_text(f"✅ نمونهٔ {n} دریافت شد. بازم بفرست یا بساز:",
                                    reply_markup=M([[btn(f"🧬 ساخت صدا ({n} نمونه)", "cl:done")], [ui.CANCEL]]))

    # --- فایل بدون انتخاب بخش: پیشنهاد گزینه‌ها ---
    ud["pending"] = m
    if is_text_doc(m):
        rows = [[btn("📄 خواندن فایل با صدا", "pf:file")]]
    else:
        rows = [[btn("📝 تبدیل به متن", "pf:stt"), btn("🔄 تغییر صدا", "pf:changer")],
                [btn("🧹 حذف نویز", "pf:isolate"), btn("🎬 دوبله", "pf:dub")]]
    rows.append([HOME])
    await msg.reply_text("📎 با این فایل چی کار کنم؟", reply_markup=M(rows))


async def on_group_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg or not msg.text:
        return
    me = context.bot.username or ""
    mention = f"@{me}".lower()
    replied_to_bot = bool(msg.reply_to_message and msg.reply_to_message.from_user
                          and msg.reply_to_message.from_user.id == context.bot.id)
    if mention not in msg.text.lower() and not replied_to_bot:
        return
    if not await guard(update, context):
        return
    text = msg.text
    idx = text.lower().find(mention)
    if idx >= 0:
        text = (text[:idx] + text[idx + len(mention):]).strip()
    if not text and msg.reply_to_message and msg.reply_to_message.text:
        text = msg.reply_to_message.text  # منشن روی ریپلای = خواندن پیام ریپلای‌شده
    if text:
        await do_tts(update, context, text)


async def on_inline(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.inline_query
    uid = q.from_user.id
    u = db.upsert_user(q.from_user)
    if u["blocked"]:
        return await q.answer([], cache_time=5, is_personal=True)
    if not (is_admin(uid) or is_white(u) or bot_open()):
        return await q.answer([InlineQueryResultArticle(
            id="closed", title="🔒 ربات در حال حاضر بسته است",
            input_message_content=InputTextMessageContent("🔒 ربات در حال حاضر بسته است."))],
            cache_time=5, is_personal=True)
    text = q.query.strip()
    results = []

    def article(title, desc):
        return InlineQueryResultArticle(id=str(uuid.uuid4()), title=title, description=desc,
                                        input_message_content=InputTextMessageContent(f"🎙 @{context.bot.username}"))

    if len(text) >= 2 and text[-1] in ".!?؟":
        if not config.STORAGE_CHAT_ID:
            results.append(article("⚠️ حالت Inline تنظیم نشده", "ادمین باید STORAGE_CHAT_ID را تنظیم کند."))
        elif len(text) > 500:
            results.append(article("⚠️ متن طولانیه", "حداکثر ۵۰۰ کاراکتر در حالت Inline"))
        else:
            cache = context.user_data.setdefault("inline_cache", {})
            fid = cache.get(text)
            if not fid:
                spent = db.spend(uid, text_cost(len(text)), unlimited=is_unlimited(uid))
                if spent is None:
                    results.append(article("⚠️ اعتبار کافی نیست", "از داخل ربات اعتبار بخر."))
                else:
                    try:
                        voice_id, _ = user_voice(u)
                        audio = await eleven.tts(text, voice_id, u["model_id"] or config.DEFAULT_MODEL_ID,
                                                 eleven.voice_settings(u), u["lang"])
                        ogg = await media.to_ogg_voice(audio)
                        stored = await context.bot.send_voice(config.STORAGE_CHAT_ID, ogg, caption=f"inline • {uid}")
                        fid = stored.voice.file_id
                        if len(cache) > 50:
                            cache.clear()
                        cache[text] = fid
                        db.add_history(uid, "inline", text, fid, "voice")
                    except Exception as e:
                        db.refund(uid, spent)
                        log.warning("inline failed: %s", e)
                        results.append(article("❌ خطا در ساخت صدا", str(e)[:100]))
            if fid:
                results.append(InlineQueryResultCachedVoice(id=str(uuid.uuid4()), voice_file_id=fid,
                                                            title=f"🔊 {text[:60]}"))
    else:
        results.append(article("✍️ متن رو بنویس و با . یا ! یا ؟ تموم کن", "یا یکی از صداهای قبلی رو بفرست 👇"))
        for h in db.get_history(uid, 15):
            if h["file_type"] == "voice":
                results.append(InlineQueryResultCachedVoice(id=f"h{h['id']}", voice_file_id=h["file_id"],
                                                            title=f"🕘 {h['title'][:60]}"))
            elif h["file_type"] == "audio":
                results.append(InlineQueryResultCachedAudio(id=f"h{h['id']}", audio_file_id=h["file_id"]))
    try:
        await q.answer(results, cache_time=0, is_personal=True)
    except BadRequest as e:
        log.info("inline answer failed: %s", e)


async def broadcast(context, from_chat: int, message_id: int, admin_id: int):
    ok = fail = 0
    for target in db.all_user_ids():
        try:
            await context.bot.copy_message(target, from_chat, message_id)
            ok += 1
        except Forbidden:
            fail += 1
        except Exception:
            fail += 1
        await asyncio.sleep(0.05)  # رعایت محدودیت تلگرام (~۲۰ پیام در ثانیه)
    await context.bot.send_message(admin_id, f"📢 ارسال همگانی تمام شد.\n✅ موفق: {ok}\n❌ ناموفق: {fail}")


async def on_precheckout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.pre_checkout_query
    valid = stars_enabled() and any(q.invoice_payload == f"credits:{p['credits']}:{p['stars']}"
                                    and q.total_amount == p["stars"] for p in packages())
    await q.answer(ok=valid, error_message=None if valid else "بسته نامعتبر است.")


async def on_paid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    p = update.message.successful_payment
    _, credits, stars = p.invoice_payload.split(":")
    db.add_payment(update.effective_user.id, int(stars), int(credits), p.telegram_payment_charge_id)
    await update.message.reply_text(f"🎉 پرداخت موفق! <b>{int(credits):,}</b> اعتبار به حسابت اضافه شد.",
                                    parse_mode=HTML, reply_markup=ui.home_kb())
    await notify_admins(context, f"⭐ پرداخت جدید: {stars} ستاره از <code>{update.effective_user.id}</code>")


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    log.error("Unhandled error", exc_info=context.error)
    tb = "".join(traceback.format_exception(context.error))[-3000:]
    await notify_admins(context, f"⚠️ <b>خطای پیش‌بینی‌نشده</b>\n<pre>{esc(tb)}</pre>")


# ============================ اجرا ============================

async def post_init(app: Application):
    from telegram import BotCommand
    await app.bot.set_my_commands([BotCommand("start", "🏠 منوی اصلی")])
    log.info("Bot @%s ready", app.bot.username)
    if not eleven.current_key():
        log.warning("No ElevenLabs API key! Set ELEVENLABS_API_KEY or use admin panel → 🔑 کلید API")


async def post_shutdown(app: Application):
    await eleven.close()


def main():
    app = (Application.builder().token(config.TELEGRAM_BOT_TOKEN).concurrent_updates(True)
           .post_init(post_init).post_shutdown(post_shutdown).build())
    private = filters.ChatType.PRIVATE
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(InlineQueryHandler(on_inline))
    app.add_handler(PreCheckoutQueryHandler(on_precheckout))
    app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, on_paid))
    app.add_handler(MessageHandler(private & ~filters.COMMAND, on_private_message))
    app.add_handler(MessageHandler(filters.ChatType.GROUPS & filters.TEXT, on_group_message))
    app.add_error_handler(on_error)

    use_webhook = config.USE_WEBHOOK == "true" or (config.USE_WEBHOOK == "auto" and config.WEBHOOK_DOMAIN)
    if use_webhook:
        secret = config.WEBHOOK_SECRET or hashlib.sha256(config.TELEGRAM_BOT_TOKEN.encode()).hexdigest()[:32]
        log.info("Starting in WEBHOOK mode on port %s", config.PORT)
        app.run_webhook(listen="0.0.0.0", port=config.PORT, url_path="tg",
                        webhook_url=f"https://{config.WEBHOOK_DOMAIN}/tg", secret_token=secret,
                        allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)
    else:
        log.info("Starting in POLLING mode")
        app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
