"""تنظیمات ربات — همه از متغیرهای محیطی (Railway → Variables) خوانده می‌شوند."""
import os


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except ValueError:
        return default


def _ids(name: str) -> set[int]:
    raw = os.getenv(name, "").replace(" ", "")
    return {int(x) for x in raw.split(",") if x.lstrip("-").isdigit()}


def _list(name: str) -> list[str]:
    return [x.strip() for x in os.getenv(name, "").split(",") if x.strip()]


# --- ضروری ---
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
ELEVENLABS_API_KEY = os.environ["ELEVENLABS_API_KEY"]

# --- دسترسی و مدیریت ---
ADMIN_IDS = _ids("ADMIN_IDS")                    # آیدی ادمین‌ها
ALLOWED_USER_IDS = _ids("ALLOWED_USER_IDS")      # خالی = همه مجازند
REQUIRED_CHANNELS = _list("REQUIRED_CHANNELS")   # مثل @mychannel
ADMIN_ONLY_FEATURES = set(_list("ADMIN_ONLY_FEATURES"))  # مثل clone,dub,music

# --- پیش‌فرض‌های ElevenLabs ---
DEFAULT_VOICE_ID = os.getenv("ELEVENLABS_VOICE_ID", "JBFqnCBsd6RMkjVDRZzb")
DEFAULT_MODEL_ID = os.getenv("ELEVENLABS_MODEL_ID", "eleven_v3")
DIALOGUE_MODEL_ID = os.getenv("DIALOGUE_MODEL_ID", "eleven_v3")
STT_MODEL_ID = os.getenv("STT_MODEL_ID", "scribe_v2")
STS_MODEL_ID = os.getenv("STS_MODEL_ID", "eleven_multilingual_sts_v2")
VOICE_DESIGN_MODEL_ID = os.getenv("VOICE_DESIGN_MODEL_ID", "eleven_ttv_v3")
MUSIC_MODEL_ID = os.getenv("MUSIC_MODEL_ID", "music_v1")
SFX_MODEL_ID = os.getenv("SFX_MODEL_ID", "eleven_text_to_sound_v2")

# --- سهمیه و اعتبار (واحد: کاراکتر/اعتبار ربات) ---
DAILY_FREE_CREDITS = _int("DAILY_FREE_CREDITS", 2000)
MAX_TEXT_CHARS = _int("MAX_TEXT_CHARS", 5000)
MAX_FILE_CHARS = _int("MAX_FILE_CHARS", 20000)
COSTS = {
    "stt": _int("COST_STT", 300),
    "changer": _int("COST_CHANGER", 500),
    "isolate": _int("COST_ISOLATE", 500),
    "dub": _int("COST_DUB", 3000),
    "clone": _int("COST_CLONE", 1000),
    "design": _int("COST_DESIGN", 500),
    "sfx": _int("COST_SFX", 200),
    "music": _int("COST_MUSIC", 2000),
}
# بسته‌های خرید با Telegram Stars → «اعتبار:ستاره»
STAR_PACKAGES = []
for item in _list("STAR_PACKAGES") or ["5000:50", "20000:150", "100000:500"]:
    credits, stars = item.split(":")
    STAR_PACKAGES.append((int(credits), int(stars)))

# --- زیرساخت ---
DB_PATH = os.getenv("DB_PATH", "/data/bot.db")
MAX_CONCURRENT_JOBS = _int("MAX_CONCURRENT_JOBS", 3)
STORAGE_CHAT_ID = _int("STORAGE_CHAT_ID", 0)     # برای حالت Inline (کانال خصوصی)
WEBHOOK_DOMAIN = os.getenv("WEBHOOK_DOMAIN") or os.getenv("RAILWAY_PUBLIC_DOMAIN", "")
USE_WEBHOOK = os.getenv("USE_WEBHOOK", "auto").lower()  # auto | true | false
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")
PORT = _int("PORT", 8080)
