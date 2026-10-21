"""ربات تلگرام ElevenLabs — نقطهٔ شروع برنامه."""
import asyncio
import base64
import hashlib
import html
import io
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

def is_admin(uid: int) -> bool:
    return uid in config.ADMIN_IDS


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
    if config.ALLOWED_USER_IDS and user.id not in config.ALLOWED_USER_IDS and not is_admin(user.id):
        if update.callback_query:
            await update.callback_query.answer("⛔ دسترسی ندارید.", show_alert=True)
        elif update.effective_chat.type == ChatType.PRIVATE:
            await update.effective_message.reply_text(f"⛔ دسترسی ندارید.\nآیدی شما: <code>{user.id}</code>",
                                                      parse_mode=HTML)
        return False
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
    spent = db.spend(uid, cost, unlimited=is_admin(uid))
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

    await run_job(update, context, kind if kind == "file" else "tts", len(text), "🎙 در حال ساخت صدا…", job)


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

    await run_job(update, context, "dialogue", total, "🎭 در حال ساخت دیالوگ…", job)


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

    await run_job(update, context, "stt", config.COSTS["stt"], "📝 در حال تبدیل به متن…", job)


async def do_changer(update: Update, context, m: dict):
    u = db.get_user(update.effective_user.id)
    voice_id, voice_name = user_voice(u)

    async def job():
        data = await download(context, m)
        audio = await eleven.voice_changer(data, m["filename"], voice_id)
        await deliver_audio(update, context, audio, "changer", f"تغییر صدا به {voice_name}",
                            f"🔄 تغییر صدا ← 🎤 {esc(voice_name)}")

    await run_job(update, context, "changer", config.COSTS["changer"], "🔄 در حال تغییر صدا…", job)


async def do_isolate(update: Update, context, m: dict):
    async def job():
        data = await download(context, m)
        audio = await eleven.isolate(data, m["filename"])
        await deliver_audio(update, context, audio, "isolate", "صدای بدون نویز", "🧹 نویز حذف شد",
                            force_audio=True, reencode=True)

    await run_job(update, context, "isolate", config.COSTS["isolate"], "🧹 در حال حذف نویز…", job)


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

    await run_job(update, context, "dub", config.COSTS["dub"], "🎬 در حال آپلود برای دوبله…", job)


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

    await run_job(update, context, "clone", config.COSTS["clone"], "🧬 در حال ساخت صدا…", job)


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

    await run_job(update, context, "design", config.COSTS["design"], "🎨 در حال طراحی صدا…", job)


async def do_sfx(update: Update, context, prompt: str):
    dur = context.user_data.get("sfx_dur")

    async def job():
        audio = await eleven.sound_effect(prompt, dur)
        await deliver_audio(update, context, audio, "sfx", prompt, f"💥 {esc(prompt[:200])}", force_audio=True)

    await run_job(update, context, "sfx", config.COSTS["sfx"], "💥 در حال ساخت افکت…", job)


async def do_music(update: Update, context, prompt: str):
    sec = context.user_data.get("music_len")
    cost = config.COSTS["music"] * max(1, round((sec or 60) / 60))

    async def job():
        audio = await eleven.music(prompt, sec * 1000 if sec else None)
        await deliver_audio(update, context, audio, "music", prompt, f"🎵 {esc(prompt[:200])}",
                            force_audio=True, reencode=True)

    await run_job(update, context, "music", cost, "🎵 در حال ساخت موسیقی… (ممکنه یکی دو دقیقه طول بکشه)", job)


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
    if is_admin(update.effective_user.id) and v.get("category") != "premade":
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
                                  f"<i>هزینه: {config.COSTS['music']:,} اعتبار به ازای هر دقیقه</i>", ui.cancel_kb(*rows))
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
        unlimited = "\n♾ <b>شما ادمین هستید؛ محدودیتی ندارید.</b>" if is_admin(uid) else ""
        costs = "\n".join(f"• {ui.FEATURES[k][0]}: {v:,}" for k, v in config.COSTS.items()
                          if k in ui.FEATURES)
        return await show(update, (
            f"💳 <b>اعتبار من</b>{unlimited}\n\n🎁 رایگان امروز: <b>{free:,}</b> از {config.DAILY_FREE_CREDITS:,}\n"
            f"💰 موجودی خریداری‌شده: <b>{bal:,}</b>\n\n📌 <b>هزینه‌ها</b>\n• متن به صدا: هر کاراکتر = ۱\n{costs}\n\n"
            "⭐ خرید اعتبار با Telegram Stars:"), ui.credit_kb())
    if key == "help":
        return await show(update, ui.HELP.replace("{bot}", context.bot.username or "bot"), ui.home_kb())
    if key == "admin" and is_admin(uid):
        return await show(update, "🛠 <b>پنل مدیریت</b>", ui.admin_kb())


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
        if action == "d" and is_admin(uid):
            return await show(update, "🗑 مطمئنی این صدا از حساب ElevenLabs حذف بشه؟",
                              M([[btn("✅ بله، حذف کن", f"v:dd:{arg}"), btn("❌ نه", f"v:i:{arg}")]]))
        if action == "dd" and is_admin(uid):
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
        credits, stars = config.STAR_PACKAGES[int(data[4:])]
        return await context.bot.send_invoice(
            chat_id=q.message.chat_id, title=f"{credits:,} اعتبار",
            description=f"خرید {credits:,} اعتبار برای ربات صوتی ElevenLabs",
            payload=f"credits:{credits}:{stars}", currency="XTR",
            prices=[LabeledPrice(f"{credits:,} اعتبار", stars)])

    # --- ادمین ---
    if data.startswith("a:") and is_admin(uid):
        action = data[2:]
        if action == "stats":
            s = db.stats()
            return await show(update, (
                "📊 <b>آمار</b>\n\n"
                f"👥 کل کاربران: <b>{s['users']:,}</b>\n🟢 فعال ۲۴ ساعت: <b>{s['active_24h']:,}</b>\n"
                f"🆕 جدید ۲۴ ساعت: <b>{s['new_24h']:,}</b>\n🚫 مسدود: <b>{s['blocked']:,}</b>\n\n"
                f"🔤 مصرف امروز: <b>{s['used_today']:,}</b>\n🔤 مصرف کل: <b>{s['used_total']:,}</b>\n"
                f"🎧 تعداد ساخته‌ها: <b>{s['generations']:,}</b>\n⭐ ستاره‌های دریافتی: <b>{s['stars']:,}</b>\n"
                f"⏳ کارهای در صف: <b>{_waiting}</b>"), ui.admin_kb())
        if action == "acct":
            try:
                sub = await eleven.subscription()
            except eleven.ElevenError as e:
                return await show(update, f"❌ {esc(str(e))}", ui.admin_kb())
            used, limit = sub.get("character_count", 0), sub.get("character_limit", 0)
            reset = sub.get("next_character_count_reset_unix")
            reset_s = time.strftime("%Y-%m-%d", time.gmtime(reset)) if reset else "-"
            pct = f" ({used * 100 // limit}%)" if limit else ""
            return await show(update, (
                "💼 <b>حساب ElevenLabs</b>\n\n"
                f"📦 پلن: <b>{esc(str(sub.get('tier', '-')))}</b>\n"
                f"🔤 مصرف: <b>{used:,}</b> از {limit:,}{pct}\n"
                f"💰 باقی‌مانده: <b>{max(0, limit - used):,}</b>\n"
                f"🔄 تمدید: {reset_s}\n"
                f"🎤 اسلات صدا: {sub.get('voice_slots_used', '-')} از {sub.get('voice_limit', '-')}"), ui.admin_kb())
        if action in ("bc", "block", "credit"):
            ud["state"] = f"a_{action}"
            prompts = {"bc": "📢 پیامی که می‌خوای برای همه ارسال بشه رو بفرست (متن، عکس، ویس…):",
                       "block": "🚫 آیدی عددی کاربر رو بفرست (مسدود ↔ آزاد):",
                       "credit": "➕ بفرست: <code>آیدی مقدار</code>\nمثال: <code>123456789 5000</code>"}
            return await show(update, prompts[action], ui.cancel_kb())


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
            return await msg.reply_text("📢 ارسال همگانی شروع شد…", reply_markup=ui.admin_kb())
        if state == "a_block" and text.strip().lstrip("-").isdigit():
            target = int(text.strip())
            u = db.get_user(target)
            if not u:
                return await msg.reply_text("⚠️ کاربر پیدا نشد.", reply_markup=ui.admin_kb())
            db.set_field(target, "blocked", 0 if u["blocked"] else 1)
            return await msg.reply_text(f"{'✅ آزاد' if u['blocked'] else '🚫 مسدود'} شد: {target}",
                                        reply_markup=ui.admin_kb())
        if state == "a_credit":
            try:
                target, amount = (int(x) for x in text.split())
                if not db.get_user(target):
                    raise ValueError
                db.add_balance(target, amount)
                return await msg.reply_text(f"✅ {amount:,} اعتبار به {target} اضافه شد.", reply_markup=ui.admin_kb())
            except ValueError:
                return await msg.reply_text("⚠️ فرمت اشتباه یا کاربر ناموجود.", reply_markup=ui.admin_kb())
        return await msg.reply_text("⚠️ ورودی نامعتبر.", reply_markup=ui.admin_kb())

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
    if u["blocked"] or (config.ALLOWED_USER_IDS and uid not in config.ALLOWED_USER_IDS and not is_admin(uid)):
        return await q.answer([], cache_time=5, is_personal=True)
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
                spent = db.spend(uid, len(text), unlimited=is_admin(uid))
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
    valid = any(q.invoice_payload == f"credits:{c}:{s}" and q.total_amount == s for c, s in config.STAR_PACKAGES)
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
