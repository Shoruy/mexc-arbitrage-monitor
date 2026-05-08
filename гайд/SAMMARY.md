# MEXC-Binance Spread Monitor — Самари проекта

## Что сделано

### 1. Анализ стратегии
Проанализировали гайд по флипам альтов между Binance и MEXC:
- **Суть:** Binance двигается быстрее MEXC → задержка до 2 секунд → возможность войти
- **Сигналы:** расхождение цен, разрыв с BTC/ETH, ликвидность
- **Аккаунты:** только с -PNL (минусовые), депозит до $30, не в стейблах
- **Монеты:** XLM, Pengu, Aster, ZEC, XMR, XPL, BCH, HYPE

### 2. Создан монитор спреда
Два Python-скрипта для автоматического отслеживания расхождений:

**mexc-spread-monitor.py** — сигнальный монитор
- Binance WebSocket (реальное время, 8 пар)
- MEXC REST API (polling 2 сек)
- Алерты при спреде > 0.05%

**mexc-spread-sim.py v2** — монитор + симуляция сделок
- Виртуальный вход/выход по спреду
- Расчёт PnL с корректной формулой
- Анти-флипфлоп (минимальная дистанция между сделками)
- Стоп-лосс (0.20%) и max_hold (30 сек)
- Плечо по каждой паре (100x BTC/ETH, 50x альты)

### 3. Code Review (19 багов найдено)
- **🔴 Critical (3):** PnL формула сломана при смене направления, flip-flop сделок, mid-price ≠ bid/ask
- **🟡 Major (10):** fees не учтены, порог для XLM/ZEC, нет стопа, нет Telegram, polling sequential, двойной триггер, 100x для всех пар...
- **🔵 Minor (6):** aiohttp без fallback, /tmp логи, trades без trimming...

**Исправлено (v2):** PnL формула, flip-flop guard, стоп-лосс, max_hold, плечо по паре

### 4. Codex аккаунты
Из 4 аккаунтов Codex — **ACC3 (Go план)** auth проходит, но лимит исчерпан до 11 мая. Остальные мертвы (refresh_token_reused).

## Источники (все сохранены в /гайд)
1. **Оригинальный текст гайда** от Shoryu — флипы альтов на MEXC с -PNL
2. **Teletype @cusazame** — гайд по флипам MEXC/DEX для новичков
3. **Teletype @xdmxdmxdd** — гайд по спредам MEXC/DEX
4. **Teletype @kabuzim** — арбитраж на MEXC $100/день
5. **Teletype @arbitrageindustries** — вынос MEXC фьючей (Jarvis)

## TODO (GEPs)
- [ ] Telegram-доставка сигналов
- [ ] MEXC WebSocket вместо REST
- [ ] Fee-моделирование
- [ ] Persistent logs (не /tmp)
- [ ] Health-check + авто-рестарт
- [ ] Web dashboard
