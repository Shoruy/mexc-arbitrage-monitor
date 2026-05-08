# MEXC API Reference для арбитраж-ботов

## Домены

| Назначение | Домен |
|---|---|
| Spot REST (v3) | `api.mexc.com` |
| Futures REST (v1) | `contract.mexc.com` |

**Важно:** v1 futures endpoints на `api.mexc.com` больше не работают (403/1001).
Все futures запросы — только на `contract.mexc.com`.

## Формат символов

| Биржа | Формат | Пример |
|---|---|---|
| Binance | `SOLUSDT` | `symbol=SOLUSDT` |
| MEXC spot | `SOLUSDT` | `symbol=SOLUSDT` |
| MEXC futures | `SOL_USDT` | `symbol=SOL_USDT` |

**Конвертация в коде:**
```python
msym_v1 = msym[:-4] + "_" + msym[-4:]  # SOLUSDT → SOL_USDT
```

## Эндпоинты

### Публичные (без ключа)

| Эндпоинт | Метод | Описание |
|---|---|---|
| `contract.mexc.com/api/v1/contract/detail?symbol=SOL_USDT` | GET | Контрактная спецификация (contractSize) |
| `api.mexc.com/api/v3/ticker/bookTicker?symbol=SOLUSDT` | GET | Лучшие bid/ask |
| `api.mexc.com/api/v3/depth?symbol=SOLUSDT&limit=20` | GET | Стакан |

### Приватные (с подписью V1)

| Эндпоинт | Метод | Описание |
|---|---|---|
| `contract.mexc.com/api/v1/private/position/change_leverage` | POST | Сменить плечо |
| `contract.mexc.com/api/v1/private/order/create` | POST | Создать ордер |
| `contract.mexc.com/api/v1/private/position/close_all` | POST | Закрыть все позиции |

## Авторизация (V1 signature)

```
param_string = urlencode(sorted(params))    # GET
param_string = json.dumps(body)              # POST
raw = API_KEY + timestamp + param_string
sig = HMAC-SHA256(raw, API_SECRET)
Headers: ApiKey, Request-Time, Signature
```

## Анти-бан (Cloudflare)

**Проблема:** aiohttp/httpx по умолчанию шлют `User-Agent: Python-aiohttp/X.Y` → Cloudflare блокирует (403 text/html).

**Фикс — browser-заголовки на все запросы:**
```python
_BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 ...",
    "Accept": "application/json",
    "Accept-Language": "en-US,en;q=0.9,ru;q=0.8",
    "Referer": "https://www.mexc.com/",
    "Origin": "https://www.mexc.com",
}
```

**Дополнительно:** проверять Content-Type ответа — если не `application/json`, значит бан. Не крашиться, логировать.

## SDK/Библиотеки

- `python-mexc-futures` (официальный MEXC SDK) — есть в PyPI, но под капотом те же запросы
- В нашем боте используется чистый aiohttp + самописная подпись — меньше зависимостей

## История

- **2025**: api.mexc.com/api/v1/ — работало без ключа, User-Agent не важен
- **2026-05**: MEXC перевёл futures API на contract.mexc.com, Cloudflare включил блокировку aiohttp-запросов
