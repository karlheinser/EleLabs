"""متن‌ها و کیبوردهای شیشه‌ای (Inline Keyboard)."""
from telegram import InlineKeyboardButton as B, InlineKeyboardMarkup as M

import config

FEATURES = {
    # کلید: (عنوان دکمه، توضیح کوتاه)
    "tts": ("🗣 متن به صدا", "متن بفرست تا با صدای انتخابی‌ات خوانده شود."),
    "dialogue": ("🎭 دیالوگ چندنفره", ""),
    "stt": ("📝 ویس به متن", "ویس، فایل صوتی یا ویدیو بفرست تا متنش را تحویل بگیری."),
    "changer": ("🔄 تغییر صدا", "ویس یا فایل صوتی بفرست تا با صدای انتخابی‌ات بازسازی شود."),
    "isolate": ("🧹 حذف نویز", "ویس، فایل صوتی یا ویدیو بفرست تا نویز و صدای پس‌زمینه حذف شود."),
    "dub": ("🎬 دوبله", ""),
    "clone": ("🧬 شبیه‌سازی صدا", ""),
    "design": ("🎨 طراحی صدا", ""),
    "library": ("📚 کتابخانه صدا", ""),
    "voices": ("🎤 صداهای من", ""),
    "sfx": ("💥 افکت صوتی", ""),
    "music": ("🎵 ساخت موسیقی", ""),
    "file": ("📄 فایل به صدا", "فایل TXT، PDF یا DOCX بفرست تا کامل خوانده شود."),
    "history": ("🕘 تاریخچه", ""),
    "settings": ("⚙️ تنظیمات", ""),
    "credit": ("💳 اعتبار و خرید", ""),
    "help": ("ℹ️ راهنما", ""),
}

MODELS = [
    ("eleven_v3", "🌟 Eleven v3 — بهترین کیفیت، فارسی ✅"),
    ("eleven_multilingual_v2", "🌍 Multilingual v2 — ۲۹ زبان"),
    ("eleven_flash_v2_5", "⚡ Flash v2.5 — خیلی سریع"),
]
MODEL_SHORT = {"eleven_v3": "v3", "eleven_multilingual_v2": "Multilingual v2", "eleven_flash_v2_5": "Flash v2.5"}

LANGS = {"auto": "🌐 خودکار", "fa": "🇮🇷 فارسی", "en": "🇬🇧 انگلیسی", "ar": "🇸🇦 عربی",
         "tr": "🇹🇷 ترکی", "de": "🇩🇪 آلمانی", "fr": "🇫🇷 فرانسوی"}

DUB_LANGS = [("fa", "🇮🇷 فارسی"), ("en", "🇬🇧 انگلیسی"), ("ar", "🇸🇦 عربی"), ("tr", "🇹🇷 ترکی"),
             ("de", "🇩🇪 آلمانی"), ("fr", "🇫🇷 فرانسوی"), ("es", "🇪🇸 اسپانیایی"), ("it", "🇮🇹 ایتالیایی"),
             ("ru", "🇷🇺 روسی"), ("hi", "🇮🇳 هندی"), ("zh", "🇨🇳 چینی"), ("ja", "🇯🇵 ژاپنی")]

CATEGORY_ICON = {"cloned": "🧬", "generated": "🎨", "professional": "💎", "premade": "⭐"}


def btn(text, data):
    return B(text, callback_data=data)


def rows2(buttons):
    return [buttons[i:i + 2] for i in range(0, len(buttons), 2)]


HOME = btn("🏠 منوی اصلی", "m:main")
CANCEL = btn("❌ لغو", "m:main")


def main_menu(is_admin: bool) -> M:
    keys = ["tts", "dialogue", "stt", "changer", "isolate", "dub", "clone", "design",
            "library", "voices", "sfx", "music", "file", "history", "settings", "credit"]
    rows = rows2([btn(FEATURES[k][0], f"m:{k}") for k in keys])
    last = [btn(FEATURES["help"][0], "m:help")]
    if is_admin:
        last.append(btn("🛠 پنل مدیریت", "m:admin"))
    rows.append(last)
    return M(rows)


def home_kb():
    return M([[HOME]])


def cancel_kb(*extra_rows):
    return M([*extra_rows, [CANCEL]])


def welcome(name: str) -> str:
    return (
        f"سلام <b>{name}</b> 👋\n\n"
        "به ربات صوتی <b>ElevenLabs</b> خوش اومدی 🎙\n"
        "از دکمه‌های زیر یکی رو انتخاب کن.\n\n"
        "💡 <i>میان‌بر: همین الان هر متنی بفرستی به صدا تبدیل می‌شه و هر ویسی بفرستی، گزینه‌هاش رو نشونت می‌دم.</i>"
    )


HELP = (
    "ℹ️ <b>راهنما</b>\n\n"
    "🗣 <b>متن به صدا:</b> متن بفرست. متن‌های طولانی خودکار تکه‌تکه و به هم وصل می‌شن.\n"
    "   در مدل v3 می‌تونی تگ احساسی بذاری، مثل <code>[laughs]</code>، <code>[whispers]</code>، <code>[sad]</code>.\n"
    "🎭 <b>دیالوگ:</b> هر خط به شکل <code>اسم: متن</code>.\n"
    "📝 <b>ویس به متن:</b> ویس، صوت یا ویدیو بفرست.\n"
    "🔄 <b>تغییر صدا:</b> حرفت رو با صدای دیگه تحویل بگیر.\n"
    "🧹 <b>حذف نویز:</b> صدای پس‌زمینه رو پاک کن.\n"
    "🎬 <b>دوبله:</b> ویدیو/صوت رو به زبان دیگه دوبله کن.\n"
    "🧬 <b>شبیه‌سازی:</b> با چند نمونه از صدای خودت، صدای اختصاصی بساز.\n"
    "🎨 <b>طراحی صدا:</b> صدای جدید رو با توضیح متنی بساز.\n"
    "📚 <b>کتابخانه:</b> هزاران صدای عمومی، از جمله فارسی.\n"
    "💥 <b>افکت</b> و 🎵 <b>موسیقی:</b> با توضیح متنی بساز.\n"
    "📄 <b>فایل به صدا:</b> TXT / PDF / DOCX.\n\n"
    "⚡ <b>حالت Inline:</b> توی هر چتی بنویس <code>@{bot} متن.</code> (با نقطه، ! یا ؟ تموم کن).\n"
    "👥 <b>گروه:</b> ربات رو منشن کن یا روی پیامش ریپلای بزن.\n\n"
    "⚠️ حداکثر حجم فایل ورودی ۲۰ مگابایته (محدودیت تلگرام)."
)


def settings_text(u) -> str:
    speed_note = " <i>(در v3 اعمال نمی‌شه)</i>" if u["model_id"] == "eleven_v3" else ""
    return (
        "⚙️ <b>تنظیمات</b>\n\n"
        f"🎤 صدا: <b>{u['voice_name'] or 'پیش‌فرض'}</b>\n"
        f"🧠 مدل: <b>{MODEL_SHORT.get(u['model_id'], u['model_id'])}</b>\n"
        f"⚖️ ثبات: <b>{u['stability']}</b>\n"
        f"🎯 شباهت: <b>{u['similarity']}</b>\n"
        f"🎭 استایل: <b>{u['style']}</b>\n"
        f"⏩ سرعت: <b>{u['speed']}</b>{speed_note}\n"
        f"🌐 زبان: <b>{LANGS.get(u['lang'], u['lang'])}</b>\n"
        f"📤 خروجی: <b>{'🎙 ویس تلگرام' if u['output'] == 'voice' else '🎧 فایل MP3'}</b>\n\n"
        "<i>با زدن هر دکمه، مقدارش عوض می‌شه.</i>"
    )


def settings_kb(u) -> M:
    return M([
        [btn("🎤 انتخاب صدا", "m:voices"), btn("🧠 مدل", "s:models")],
        [btn(f"⚖️ ثبات {u['stability']}", "s:c:stability"), btn(f"🎯 شباهت {u['similarity']}", "s:c:similarity")],
        [btn(f"🎭 استایل {u['style']}", "s:c:style"), btn(f"⏩ سرعت {u['speed']}", "s:c:speed")],
        [btn(LANGS.get(u["lang"], u["lang"]), "s:c:lang"),
         btn("🎙 ویس" if u["output"] == "voice" else "🎧 MP3", "s:c:output")],
        [btn("♻️ بازنشانی", "s:reset"), HOME],
    ])


def credit_kb() -> M:
    rows = [[btn(f"⭐ {stars} ستاره ← {credits:,} اعتبار", f"buy:{i}")]
            for i, (credits, stars) in enumerate(config.STAR_PACKAGES)]
    rows.append([HOME])
    return M(rows)


def admin_kb() -> M:
    return M([
        [btn("📊 آمار", "a:stats"), btn("💼 حساب ElevenLabs", "a:acct")],
        [btn("📢 پیام همگانی", "a:bc"), btn("🚫 مسدود / آزاد", "a:block")],
        [btn("➕ افزودن اعتبار", "a:credit"), HOME],
    ])
