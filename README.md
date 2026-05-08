# MEXC-Binance Spread Monitor & Auto-Trader

## Файлы проекта

- `mexc-tg-monitor.py` — **Live-версия** (запущена на VPS). Мониторинг спреда Binance↔MEXC с авто-трейдингом и Telegram-алертами.
- `mexc-spread-monitor.py` — Старая версия (только сигналы, 0.05% порог, без авто).
- `mexc-spread-sim.py` — Монитор + симуляция сделок.
- `gaid-flipy-alty.md` — Гайд по флипам альтов (сводка).
- `гайд/` — Исходные материалы, NLM-анализ, Deep Research, телетайпы.

## Как работает (mexc-tg-monitor.py)

1. **Binance WebSocket** — bookTicker в реальном времени (8 пар).
2. **MEXC REST API** — polling каждые 1 сек (bookTicker v3).
3. При расхождении mid-price > порога (0.12%) — проверка ликвидности по стакану → если ОК → сигнал в Telegram.
4. В режиме AUTO (есть API-ключи) — ЛИМИТКА на MEXC.
5. Контроль рисков: лимит 20 сделок/день, стоп-лосс $3, пауза после 8 сделок.

## Конфиг (live)

| Параметр | Значение | 
|---|---|
| ENTRY_SPREAD | 0.12% |
| EXIT_SPREAD | 0.02% |
| LEVERAGE | 20x |
| Торговый объём | $7 ± $1 |
| TG алерты | ✅ |
| Авто-трейдинг | ✅ (если есть ключи) |

## Важные фиксы (2026-05-07)

### 🔴 MEXC API migration
MEXC перевёл фьючерсные API endpoints на новый домен:

| Было | Стало |
|---|---|
| `api.mexc.com/api/v1/contract/detail?symbol=SOLUSDT` | `contract.mexc.com/api/v1/contract/detail?symbol=SOL_USDT` |
| `api.mexc.com/api/v1/private/*` | `contract.mexc.com/api/v1/private/*` |
| Символы: `SOLUSDT` | Символы: `SOL_USDT` (через `_`) |
| User-Agent: не было (Python aiohttp) | User-Agent: `Mozilla/5.0 ... Chrome/131` |

### 🛡️ Anti-ban (Cloudflare)
- aiohttp по умолчанию шлёт `User-Agent: Python-aiohttp/X.Y` → **CF блокирует**
- Фикс: `_BROWSER_HEADERS` с User-Agent, Referer, Origin на все запросы
- V3 эндпоинты (`api/v3/ticker/bookTicker`, `api/v3/depth`) блокируются реже, но тоже добавлены заголовки

### ⚠️ Content-Type guard
- Вместо падения с `ContentTypeError` при 403 text/html → проверка Content-Type → логирование → graceful skip

## Питфоллы

- VPS с 38GB диска: одно из узких мест — 8GB занято swap-файлами
- При 100% диска → systemd degraded → сервисы не стартуют
- MEXC v1 API без ключа больше не отдаёт contract/detail (code 1001) — нужны ключи

## TODO

- [x] Telegram-доставка сигналов
- [x] MEXC WebSocket вместо REST (было, но REST стабильнее)
- [ ] MEXC WebSocket для скорости
- [ ] Fee-моделирование (taker 0.1% на альты)
- [ ] Health-check + auto-restart systemd
- [ ] Web UI / dashboard
