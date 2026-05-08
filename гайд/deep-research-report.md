# Deep Research Report: Binance-MEXC Arbitrage
**Источник:** Google Gemini Deep Research (CDP Chrome)
**Дата:** 2026-05-07
**Название:** Structural Inefficiencies and Risk Mitigation in Cross-Exchange Arbitrage

## 1. Geo-блокировка MEXC (Q2 2026)
**Tier 1 — Prohibited:** North Korea, Cuba, Sudan, Iran, China, Singapore, USA, UK, Hong Kong, Canada, Crimea/Donetsk/Luhansk → тотал бан
**Tier 2 — Innovation Zone Restricted:** Russia, Belarus, Armenia, Azerbaijan, Kazakhstan, Uzbekistan, Tajikistan, Moldova → можно закрывать позиции, НО нельзя открывать новые в high-volatility парах
**Tier 3 — App Limited:** India, Indonesia, South Korea, Japan → manual APK
**Tier 4 — Redirects:** Nigeria, Malaysia, Philippines → зеркала

## 2. MEXC Risk Control (РК) — триггеры
- **Lead-Lag Correlation:** ордера в течение 500-1500ms после скачка Binance → флаг
- **Order-to-Trade Ratio > 100:1** в минуту → rate-limiting
- **PNL stair-case growth:** мелкие стабильные профиты без просадок → системный арбитраж
- **Volume 5-10%** от минутного объёма пары → liquidity protection

## 3. Состояния аккаунта
- **A: Soft Throttling** — искусственная задержка 500ms на API (окно закрыто)
- **B: Restricted Opening** — только закрытие
- **C: Abnormal Funds Freeze** — видео KYC + Source of Wealth

## 4. Hidden API (u_id)
MEXC WebSocket требует **u_id** из cookies браузера:
```json
{
  "method": "SUBSCRIPTION",
  "params": ["spot@private.account.v3.api"],
  "uid": "123456789",
  "token": "SESSION_TOKEN_STRING"
}
```
Cookies: `mexc_session`, `u_id`, `cf_clearance`. Извлекаются через Puppeteer/Playwright.

## 5. OTC Рынок аккаунтов (-PNL)
| Тип | PNL | Цена | Надёжность |
|-----|-----|------|------------|
| Burner | 0 | $5-17 | Низкая |
| Washed | -$500 to -$2,000 | $40-100 | Средняя |
| Premium | -$10,000+ | $250-600 | Высокая |
| Aged | 1+ year old | $150-300 | Высокая |

## 6. Архитектура бота
- **Data Ingestion** (Rust/Go): Binance WebSocket → Local Order Book (<5ms)
- **MEXC Monitor** (Python/CCXT): Cloudflare bypass, Singapore/Tokyo сервер
- **Execution** (Python): spread formula S = (Pm - Pb)/Pb > (Fb + Fm + σ + γ)
- **ThrottledDebouncer:** 100ms debounce, 500ms max wait
- **Kill Switch** при Soft Throttling
