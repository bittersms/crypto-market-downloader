# Local Binance-Compatible API

سرور API کاملاً سازگار با پروتکل Binance بر روی `127.0.0.1` که کندل‌های ذخیره‌شده در پوشه خروجی را سرو می‌کند. این سرور به همراه قابلیت Auto-Update کار می‌کند: هر n دقیقه کندل‌های جدید دریافت و با فایل ادغام می‌شوند و API بلافاصله داده‌های جدید را ارائه می‌دهد.

---

## ۱. روشن کردن سرور

### روش اول — از داخل برنامه

۱. به تب **Auto-Update** بروید.
۲. بخش **Local API Server** — پورت پیش‌فرض `8900` است (قابل تغییر).
۳. دکمه **▶ Start API Server** را بزنید.

> نکته: اگر **Start Auto Update** را بزنید، سرور API به‌طور خودکار هم روشن می‌شود.

### روش دوم — مستقل (بدون نیاز به GUI)

```bash
cd C:/Users/sms/AppData/Local/hermes/crypto_market_downloader
python api_server.py --port 8900
```

خروجی:
```
Serving 4 symbols on http://127.0.0.1:8900
Symbols: BTCUSD, ETHUSD, TAOUSD, ZECUSD
Endpoints: /api/v3/ping /api/v3/time /api/v3/exchangeInfo /api/v3/klines
```

برای متوقف کردن: `Ctrl+C`

---

## ۲. آدرس پایه

```
http://127.0.0.1:8900
```

تمام درخواست‌ها فقط از **localhost** قابل دسترس هستند (bind روی `127.0.0.1`).

---

## ۳. Endpointها

### `GET /api/v3/ping`

بررسی سالم بودن سرور. مثل بایننس، `{}` برمی‌گرداند.

```bash
curl http://127.0.0.1:8900/api/v3/ping
```

```json
{}
```

---

### `GET /api/v3/time`

زمان سرور به میلی‌ثانیه (Unix epoch).

```bash
curl http://127.0.0.1:8900/api/v3/time
```

```json
{"serverTime": 1789826400000}
```

---

### `GET /api/v3/exchangeInfo`

فهرست ارزهایی که داده آن‌ها به‌صورت محلی موجود است.

```bash
curl http://127.0.0.1:8900/api/v3/exchangeInfo
```

```json
{
  "timezone": "UTC",
  "serverTime": 1789826400000,
  "rateLimits": [],
  "exchangeFilters": [],
  "symbols": [
    {
      "symbol": "BTCUSDT",
      "status": "TRADING",
      "baseAsset": "BTC",
      "quoteAsset": "USDT",
      "baseAssetPrecision": 8,
      "quotePrecision": 8,
      "filters": [],
      "permissions": ["SPOT"]
    },
    ...
  ]
}
```

---

### `GET /api/v3/klines`

کندل‌های ذخیره‌شده. دقیقاً با همان شکل آرایه بایننس (۱۲ فیلد).

#### پارامترها

| پارامتر | اجباری | توضیح |
|---|---|---|
| `symbol` | بله | نام جفت ارز — `BTCUSDT` |
| `interval` | خیر | فقط `1m` پشتیبانی می‌شود (داده محلی ۱ دقیقه است) |
| `limit` | خیر | تعداد کندل. حداکثر `1000`. پیش‌فرض `1000` |
| `startTime` | خیر | از این زمان به بعد (میلی‌ثانیه) |
| `endTime` | خیر | تا این زمان (میلی‌ثانیه) |

#### مثال

```bash
curl "http://127.0.0.1:8900/api/v3/klines?symbol=BTCUSDT&interval=1m&limit=2"
```

```json
[
  [
    1789826340000,      // 0  openTime
    "81320.00000000",   // 1  open
    "81330.01000000",   // 2  high
    "81289.47000000",   // 3  low
    "81306.86000000",   // 4  close
    "94.24043000",      // 5  volume
    1789826399999,      // 6  closeTime
    "0",                // 7  quoteAssetVolume
    0,                  // 8  numberOfTrades
    "0",                // 9  takerBuyBaseVolume
    "0",                // 10 takerBuyQuoteVolume
    "0"                 // 11 ignore
  ],
  [
    1789826400000,
    "81306.86000000",
    "81306.86000000",
    "81292.77000000",
    "81292.78000000",
    "5.55328000",
    1789826459999,
    "0",
    0,
    "0",
    "0",
    "0"
  ]
]
```

> **رفتار limit:** مثل بایننس، وقتی بدون `startTime` درخواست بدهید، **آخرین** N کندل برگردانده می‌شود.

---

### `GET /api/v3/ticker/price`

آخرین قیمت ثبت‌شده (از آخرین کندل فایل).

```bash
curl "http://127.0.0.1:8900/api/v3/ticker/price?symbol=BTCUSDT"
```

```json
{"symbol": "BTCUSDT", "price": "81292.78000000", "time": 1789813800000}
```

---

### `GET /api/v3/ticker/bookTicker`

آخرین قیمت به‌صورت bid/ask (هر دو برابر قیمت آخرین کندل).

```bash
curl "http://127.0.0.1:8900/api/v3/ticker/bookTicker?symbol=ETHUSDT"
```

```json
{
  "symbol": "ETHUSDT",
  "bidPrice": "2645.03000000",
  "bidQty": "0",
  "askPrice": "2645.03000000",
  "askQty": "0"
}
```

---

## ۴. مدیریت خطا

خطاها دقیقاً با شکل بایننس برگردانده می‌شوند:

| HTTP | کد | معنی |
|---|---|---|
| 400 | `-1121` | نماد شناخته نیست (داده محلی ندارد) |
| 400 | `-1103` | پارامتر `symbol` ارسال نشده |
| 400 | `-1` | endpoint نامعتبر |

```bash
curl -s -w "\nHTTP %{http_code}\n" "http://127.0.0.1:8900/api/v3/klines?symbol=DOGEUSDT&interval=1m"
```

```json
{"code": -1121, "msg": "Invalid symbol: DOGEUSDT"}
HTTP 400
```

---

## ۵. نمونه کد کلاینت

### پایتون (requests)

```python
import requests

BASE = "http://127.0.0.1:8900"

# سالم بودن سرور
requests.get(f"{BASE}/api/v3/ping").raise_for_status()

# فهرست ارزها
symbols = requests.get(f"{BASE}/api/v3/exchangeInfo").json()["symbols"]
print([s["symbol"] for s in symbols])
# ['BTCUSDT', 'ETHUSDT', 'TAOUSDT', 'ZECUSDT']

# آخرین ۵۰۰ کندل
resp = requests.get(f"{BASE}/api/v3/klines",
                    params={"symbol": "BTCUSDT", "interval": "1m", "limit": 500})
candles = resp.json()
for k in candles[-3:]:
    print(k[0], k[1], k[4], k[5])   # openTime, open, close, volume
```

### پایتون (pandas)

```python
import pandas as pd

df = pd.DataFrame(
    requests.get(f"{BASE}/api/v3/klines",
                 params={"symbol": "BTCUSDT", "interval": "1m", "limit": 1000}).json(),
    columns=["openTime","open","high","low","close","volume","closeTime",
             "qav","trades","takerBase","takerQuote","ignore"])
df["time"] = pd.to_datetime(df["openTime"], unit="ms")
print(df[["time","open","high","low","close","volume"]].tail())
```

### Node.js

```javascript
const BASE = "http://127.0.0.1:8900";
const res = await fetch(`${BASE}/api/v3/klines?symbol=BTCUSDT&interval=1m&limit=100`);
const klines = await res.json();
klines.forEach(k => console.log(k[0], k[1], k[4]));
```

### MetaTrader (MQL)

```mql5
string url = "http://127.0.0.1:8900/api/v3/klines?symbol=BTCUSDT&interval=1m&limit=100";
char post[]; char result[]; string headers = "";
int code = WebRequest("GET", url, NULL, NULL, 5000, post, 0, result, headers);
```

---

## ۶. نگاشت نام ارزها

فایل‌های محلی به فرمت MetaTrader نام‌گذاری شده‌اند (`BTCUSD_1M_BINANCE.csv`)،
اما API نام بایننس می‌پذیرد:

| درخواست کلاینت | فایل محلی |
|---|---|
| `BTCUSDT` | `BTCUSD_1M_BINANCE.csv` |
| `ETHUSDT` | `ETHUSD_1M_BINANCE.csv` |

قانون: پسوند `USDT` حذف می‌شود، چون اسم فایل از قبل `USD` را دارد.

---

## ۷. بهینه‌سازی و عملکرد

فایل‌های ۱ دقیقه می‌توانند بیش از **۹۰۰,۰۰۰ کندل** (~۷۰ مگابایت) باشند. برای همین:

- کنار هر فایل یک فایل `.idx` کوچک ساخته می‌شود (شامل first/last/count).
- `exchangeInfo` و `ticker/price` **فوری** پاسخ می‌دهند — نیازی به خواندن کل فایل نیست.
- `klines` فایل را فقط زمانی تجزیه می‌کند که واقعاً کندل درخواست شده باشد.

| عملیات | زمان |
|---|---|
| `ping` / `time` | ~۵۰ ms |
| `exchangeInfo` | ~۱۴۰ ms |
| `ticker/price` | ~۵ ms |
| `klines` (اولین بار برای هر ارز) | ~۴ ثانیه |
| `klines` (دفعات بعد — کش شده) | ~۴۰ ms |

> توصیه: موقع راه‌اندازی کلاینت، یک درخواست `klines` با `limit=1` برای هر ارز بفرستید تا تجزیه اولیه در پس‌زمینه انجام شود.

---

## ۸. قابلیت Auto-Update

وقتی Auto-Update روشن است:

۱. هر **n دقیقه** (قابل تنظیم در تب Auto-Update) لیست انتخاب‌شده بررسی می‌شود.
۲. فقط کندل‌های **جدید** (از آخرین کندل فایل تا حالا) دانلود می‌شوند — نه کل بازه.
۳. با فایل موجود ادغام (merge) می‌شوند.
۴. API در درخواست بعدی داده‌های جدید را نشان می‌دهد.

**تنظیمات:**

| فیلد | توضیح |
|---|---|
| **List** | لیست علاقه‌مندی (مثلاً `base`) |
| **Exchange** | صرافی (مثلاً `binance`) |
| **Interval (min)** | هر چند دقیقه یک‌بار (حداقل ۱) |
| **API Port** | پورت سرور (پیش‌فرض `8900`) |

تنظیمات در دیتابیس ذخیره می‌شوند و در اجرای بعدی برنامه بازگردانده می‌شوند.

---

## ۹. عیب‌یابی

| مشکل | دلیل | راه‌حل |
|---|---|---|
| `curl: (7) Failed to connect` | سرور روشن نیست | دکمه Start API Server یا `python api_server.py` |
| `HTTP 000` بعد از چند ثانیه | اولین تجزیه فایل طول می‌کشد | دوباره درخواست بدهید — دفعات بعد کش شده است |
| `HTTP 400 code -1121` | ارز داده محلی ندارد | آن را در یک لیست قرار دهید و یک‌بار دانلود کنید |
| `Address already in use` | پورت اشغال است | پورت دیگری استفاده کنید |
| داده‌ها قدیمی به‌نظر می‌رسند | Auto-Update روشن نیست | تب Auto-Update → Start Auto Update |

**بررسی سریع:**

```bash
# سرور زنده است؟
curl -s -w "\nHTTP %{http_code}\n" http://127.0.0.1:8900/api/v3/ping

# چه ارزهایی دارد؟
curl -s http://127.0.0.1:8900/api/v3/exchangeInfo | python -m json.tool

# آخرین کندل چه زمانی است؟
curl -s "http://127.0.0.1:8900/api/v3/klines?symbol=BTCUSDT&interval=1m&limit=1"
```

---

## ۱۰. خلاصه endpointها

| Endpoint | روش | توضیح |
|---|---|---|
| `/api/v3/ping` | GET | بررسی سلامت |
| `/api/v3/time` | GET | زمان سرور |
| `/api/v3/exchangeInfo` | GET | فهرست ارزها |
| `/api/v3/klines` | GET | کندل‌های ۱ دقیقه |
| `/api/v3/ticker/price` | GET | آخرین قیمت |
| `/api/v3/ticker/bookTicker` | GET | bid/ask آخرین قیمت |
