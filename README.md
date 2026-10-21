# 🎙 ربات تلگرام ElevenLabs

ربات کامل صوتی با منوی شیشه‌ای: متن به صدا، دیالوگ، ویس به متن، تغییر صدا، حذف نویز، دوبله،
شبیه‌سازی و طراحی صدا، کتابخانه صدا، افکت، موسیقی، فایل به صدا، تاریخچه، تنظیمات، اعتبار و خرید با Stars،
پنل ادمین، عضویت اجباری، حالت Inline، گروه، Webhook و صف پردازش.

## ساختار
| فایل | کار |
|---|---|
| `bot.py` | هندلرها و منطق اصلی |
| `ui.py` | متن‌ها و دکمه‌های شیشه‌ای |
| `eleven.py` | اتصال به API سایت ElevenLabs |
| `db.py` | دیتابیس SQLite (کاربر، سهمیه، تاریخچه، پرداخت) |
| `media.py` | تبدیل صدا با ffmpeg و خواندن PDF/DOCX |
| `config.py` | خواندن متغیرهای محیطی |

## راه‌اندازی روی Railway
1. پروژه را در یک مخزن **خصوصی** GitHub قرار بده.
2. Railway → New Project → Deploy from GitHub repo.
3. سرویس → **Variables**: حداقل `TELEGRAM_BOT_TOKEN`، `ELEVENLABS_API_KEY`، `ADMIN_IDS` (بقیه در `.env.example`).
4. سرویس → راست‌کلیک → **Attach Volume** با Mount path برابر `/data` (تا دیتابیس با هر Deploy پاک نشود).
5. (اختیاری، برای Webhook) Settings → Networking → **Generate Domain**. ربات خودکار به Webhook می‌رود.
6. Deploy. در لاگ باید `Bot @... ready` را ببینی.

## تنظیمات BotFather
- `/setinline` → فعال‌سازی حالت Inline (و `STORAGE_CHAT_ID` را بگذار: یک کانال خصوصی که ربات در آن ادمین است).
- `/setprivacy` → **Disable** تا ربات در گروه منشن‌ها را ببیند (یا ربات را ادمین گروه کن).
- پرداخت با Telegram Stars نیازی به تنظیم اضافه ندارد.

## اجرای محلی
```bash
pip install -r requirements.txt   # ffmpeg هم باید نصب باشد
cp .env.example .env              # مقادیر را پر کن و export کن
DB_PATH=./bot.db USE_WEBHOOK=false python bot.py
```
