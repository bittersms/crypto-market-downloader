# Crypto Market Data Downloader

یک دانلودر ماژولار دیتای بازار کریپتو با رابط گرافیکی (Tkinter) که **بدون هیچ API Key** دیتای کندل می‌گیرد و فایل‌های CSV سازگار با فرمت MetaTrader تولید می‌کند. تمام ارتباطات از طریق پروکسی SOCKS5 انجام می‌شود.

---

## امکانات

- **۱۱ صرافی تأییدشده** — همه با تست واقعی (لیست ارزها + کندل) بررسی شده‌اند
- **بدون API Key** — هیچ توکن، رمز یا اعتبارنامه‌ای لازم نیست
- **خروجی سازگار با MT5** — فایل `SYMBOL_TF_EXCHANGE.csv` با فرمت `DATE<TAB>TIME<TAB>OPEN<TAB>HIGH<TAB>LOW<TAB>CLOSE<TAB>VOL`
- **دانلود تک‌منبعی** — هر سرویس دیتا را از صرافی انتخابی خودش می‌گیرد (failover روی کندل‌ها غیرفعال است)
- **merge افزایشی** — بازدانلود کل بازه انجام نمی‌شود؛ فقط کندل‌های جدید با فایل موجود ادغام می‌شوند
- **سرور API محلی** — یک سرور کاملاً سازگار با پروتکل Binance روی `127.0.0.1:8900` که فایل‌های ذخیره‌شده را سرو می‌کند (شامل `.idx` برای lazy-parse سریع)
- **به‌روزرسانی خودکار** — هر N دقیقه کندل‌های جدید دریافت و با فایل ادغام می‌شوند
- **پروکسی per-source** — SOCKS4/SOCKS5/HTTP، قابل تنظیم برای هر صرافی به‌صورت جداگانه
- **فهرست علاقه‌مندی‌ها** — ذخیره ارزها در لیست‌های نام‌دار

---

## صرافی‌های پشتیبانی‌شده

| صرافی | سابقه (۱ دقیقه) | نکته |
|---|---|---|
| binance | کامل | — |
| bybit | کامل | — |
| okx | کامل | — |
| kucoin | کامل | — |
| mexc | کامل | — |
| gate | کامل | — |
| bitfinex | کامل | — |
| bitstamp | کامل | — |
| xt | کامل | — |
| lbank | کامل | — |
| hyperliquid | 5m: کامل / **1m: ~۳.۵ روز** | API این صرافی سقف ~۵۲۰۰ کندل دارد (محدودیت سمت سرور) |

> محدودیت‌های بالا در تب **Data Sources** برنامه هم نمایش داده می‌شوند.

---

## اجرا

```bash
python launcher.py
```

### پیش‌نیازها

- Python 3.11+
- tkinter (در ویندوز پیش‌فرض نصب است)
- `pip install requests PySocks`

> پروکسی پیش‌فرض `socks5://127.0.0.1:10808` است و از تب **Settings** قابل تغییر است.

---

## ساختار پروژه

```
crypto_market_downloader/
├── launcher.py            # نقطه ورود
├── app.py                 # رابط گرافیکی اصلی (تب‌ها، دانلود، worker threadها)
├── gui.py                 # پالت رنگ تم دارک
├── widgets.py             # کامپوننت‌های UI تم‌دار
├── config.py              # مدیریت settings.json
├── database.py            # ذخیره‌سازی SQLite (sources، favorites، lists)
├── proxy_manager.py       # پیکربندی و مدیریت پروکسی
├── failover.py            # لایه request + retry (فقط برای لیست ارزها)
├── source_registry.py     # رجیستری صرافی‌ها + جدول قابلیت‌ها
├── data_fetcher.py        # کلاس‌های پایه انتزاعی منابع
├── mt5_exporter.py        # خروجی MT5 + merge افزایشی
├── api_server.py          # سرور API سازگار با Binance (پورت ۸۹۰۰)
└── sources/               # آداپتورهای صرافی‌ها
    ├── __init__.py
    ├── binance.py         # binance
    ├── mexc.py            # mexc
    ├── kucoin.py          # kucoin
    ├── gate.py            # gate
    ├── okx.py             # okx
    ├── coingecko.py       # coingecko (گارد ۴ ساعتی)
    ├── lbank.py           # lbank
    ├── hyperliquid.py     # hyperliquid (POST/JSON)
    ├── binance_like.py    # پایه مشترک Binance-like
    ├── binance_like_extras.py  # bybit / bingx / xt / digifinex
    └── native_exchanges.py     # kraken / bitfinex / bitstamp / coinbase / htx
```

---

## تب‌های برنامه

| تب | کاربرد |
|---|---|
| **Cryptocurrencies** | گرفتن فهرست برتر ارزها (top N) — منبع لیست از منوی کشویی **List Source** قابل تغییر است («Auto» = failover خودمان، یا یک صرافی خاص مانند CoinGecko) |
| **Favorite Lists** | ساختن و مدیریت لیست‌های علاقه‌مندی |
| **Data Downloader** | انتخاب صرافی/ارز/بازه و دانلود |
| **Data Sources** | فعال/غیرفعال کردن صرافی‌ها و پروکسی هر کدام + تست اتصال |
| **Auto Update** | به‌روزرسانی خودکار + روشن کردن سرور API |
| **Settings** | تنظیمات عمومی و پروکسی |

---

## سرور API

سرور محلی کاملاً سازگار با پروتکل Binance است:

```bash
python api_server.py --port 8900
```

**Endpoints:** `/api/v3/ping` · `/api/v3/exchangeInfo` · `/api/v3/klines` · `/api/v3/ticker/price` · `/api/v3/ticker/bookTicker`

سند کامل استفاده: [`API_DOC.md`](API_DOC.md)

---

## اضافه کردن صرافی جدید

۱. یک فایل در `sources/` بسازید (مثلاً `myexchange.py`)
۲. کلاس‌های `CryptoListSource` و `CandleSource` را پیاده‌سازی کنید
۳. در `source_registry.py` ثبت کنید:
   - در `LIST_SOURCES` و `CANDLE_SOURCES` (مسیر ماژول)
   - در `LIST_CLASS_NAMES` و `CANDLE_CLASS_NAMES` (نام کلاس)
   - در `HISTORY_LIMIT` (محدودیت سابقه صرافی برای نمایش در GUI)
۴. در دیتابیس (تب Data Sources) منبع را فعال کنید — GUI فقط منابع فعال را نمایش می‌دهد

> **نکته مهم:** نمایش در GUI از دیتابیس خوانده می‌شود، نه فقط از رجیستری. ثبت در رجیستری به‌تنهایی کافی نیست.

---

## فرمت فایل خروجی

```
DATE	TIME	OPEN	HIGH	LOW	CLOSE	VOL
2026.09.20	09:00	123456.780000	123500.000000	123400.000000	123480.000000	15.420000
```

- نام فایل: `SYMBOL_TF_EXCHANGE.csv` (مثلاً `BTC_1M_BINANCE.csv`)
- تاریخ `YYYY.MM.DD`، زمان `HH:MM`، قیمت با ۶ رقم اعشار
- هر صرافی در فایل جداگانه — چون سابقه هر venue متفاوت است

## License

MIT
