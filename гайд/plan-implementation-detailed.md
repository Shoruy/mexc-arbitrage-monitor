# Detailed Implementation Plan: MEXC-Binance Spread Monitor v8

**Date:** 2026-05-08  
**Current live code:** `/root/bin/mexc-tg-monitor.py`  
**Project source copy:** `/root/obsidian/tldr/проекты/mexc/mexc-tg-monitor.py`  
**Current version:** v7, `curl_cffi` REST polling for MEXC, Binance WebSocket, Telegram alerts, auto-trading enabled when `/root/.mexc-api/config.json` contains `api_key` and `api_secret`.

## Goal

Move the current v7 monitor from a fragile single-file live script into a stable v8 bot that:

- survives VPS reboot and process crashes through `systemd`;
- uses MEXC WebSocket market data instead of 1s REST polling for faster spread detection;
- places post-only limit orders to target 0% maker fee instead of market/taker entries;
- expands from 8 static symbols to a filtered 30-50 symbol universe;
- evaluates executable bid/ask cross spread, fee-adjusted expected PnL, and liquidity before trading;
- stores trade and PnL history in SQLite;
- limits behavior that can trigger account review: repeated patterns, excessive volume, and trading after -PNL is exhausted.

## 1. Architecture Overview

### Current v7 data flow

```text
Binance WS bookTicker
        |
        v
binance_prices dict
        |
        |        MEXC REST /api/v3/ticker/bookTicker every 1s
        |                |
        |                v
        |          mexc_prices dict
        |                |
        +----------------+
        |
        v
check_signals loop every 0.3s
        |
        +--> MEXC REST /api/v3/depth liquidity check
        +--> risk_check
        +--> MEXC private futures REST order/create
        +--> Telegram
        +--> /tmp logs
```

### Target v8 data flow

```text
systemd mexc-monitor.service
        |
        v
/root/bin/mexc-tg-monitor.py
        |
        +--> BinanceWsClient
        |       endpoint: wss://stream.binance.com:9443/ws/<streams>
        |       stream: <lowercase_symbol>@bookTicker
        |       writes: PriceBook.binance[symbol]
        |
        +--> MexcWsClient
        |       endpoint: wss://contract.mexc.com/edge
        |       subscriptions: contract ticker or depth channel per MEXC futures symbol
        |       writes: PriceBook.mexc[symbol]
        |
        +--> MexcRestClient
        |       public fallback:
        |         GET https://api.mexc.com/api/v3/ticker/bookTicker?symbol=SOLUSDT
        |         GET https://api.mexc.com/api/v3/depth?symbol=SOLUSDT&limit=20
        |         GET https://contract.mexc.com/api/v1/contract/detail?symbol=SOL_USDT
        |       private:
        |         POST https://contract.mexc.com/api/v1/private/position/change_leverage
        |         POST https://contract.mexc.com/api/v1/private/order/create
        |         POST https://contract.mexc.com/api/v1/private/order/cancel
        |         POST https://contract.mexc.com/api/v1/private/position/close_all
        |       transport: curl_cffi with impersonate="chrome131"
        |
        +--> PairSelector
        |       builds active_symbols every 1h
        |       filters Binance overlap, liquidity, MEXC spread, contract metadata
        |
        +--> SignalEngine
        |       computes executable LONG/SHORT edge from bid/ask cross prices
        |       applies fee model, slippage buffer, entry threshold, cooldown
        |
        +--> RiskManager
        |       daily trade cap, loss cap, concurrency cap, account PnL cap, volume cap
        |
        +--> ExecutionEngine
        |       sets leverage
        |       places post-only limit entry
        |       monitors fill timeout
        |       places/executes exit
        |
        +--> TradeStore
        |       SQLite DB: /root/obsidian/tldr/проекты/mexc/data/mexc-trades.sqlite3
        |
        +--> HealthReporter
                writes /run/mexc-monitor/heartbeat.json every 5s
                systemd watchdog or health-check script restarts on stale heartbeat
```

### Code organization strategy

Keep the implementation in one Python file for Phase 1 to reduce deployment risk, but introduce clear sections/classes inside `mexc-tg-monitor.py`:

- config constants and env loading;
- symbol helpers;
- `MexcRestClient`;
- `BinanceWsClient`;
- `MexcWsClient`;
- `PairSelector`;
- `SignalEngine`;
- `RiskManager`;
- `TradeStore`;
- `ExecutionEngine`;
- `HealthReporter`;
- `main`.

After Phase 3, split into modules only if the file becomes hard to test. Do not split during Phase 1 unless tests or runtime readability become worse.

## Shared Implementation Rules

### Source/deploy rule

Edit the project source first:

```bash
/root/obsidian/tldr/проекты/mexc/mexc-tg-monitor.py
```

After validation, deploy by copying to:

```bash
/root/bin/mexc-tg-monitor.py
chmod +x /root/bin/mexc-tg-monitor.py
```

### Secret handling

Current config shape:

```json
{
  "api_key": "...",
  "api_secret": "...",
  "comment": "..."
}
```

Keep reading `/root/.mexc-api/config.json`, but never log key values. Prefer env vars when present:

```python
API_KEY = os.getenv("MEXC_API_KEY", cfg.get("api_key", ""))
API_SECRET = os.getenv("MEXC_API_SECRET", cfg.get("api_secret", ""))
```

Move Telegram token and chat ID out of source code during Phase 1:

```python
TG_BOT_TOKEN = os.getenv("MEXC_TG_BOT_TOKEN", "")
TG_CHAT_ID = os.getenv("MEXC_TG_CHAT_ID", "")
```

If either is missing, disable Telegram and log one warning.

### `curl_cffi` REST pattern

Keep all MEXC REST calls on `curl_cffi`, not `aiohttp`, `httpx`, `requests`, or `ccxt`, because MEXC/Cloudflare blocks Python-looking clients.

Use this helper pattern:

```python
from curl_cffi import requests as curl_requests

MEXC_HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9,ru;q=0.8",
    "Origin": "https://www.mexc.com",
    "Referer": "https://www.mexc.com/",
}

def _curl(method: str, url: str, **kwargs):
    kwargs.setdefault("impersonate", "chrome131")
    kwargs.setdefault("timeout", 10)
    kwargs.setdefault("verify", True)
    headers = dict(MEXC_HEADERS)
    headers.update(kwargs.pop("headers", {}) or {})
    kwargs["headers"] = headers
    if method.upper() == "GET":
        return curl_requests.get(url, **kwargs)
    return curl_requests.post(url, **kwargs)
```

Every REST response must check `Content-Type`. If it is not JSON, log `status_code`, content type, and first 200 chars, then return `None` without crashing.

### MEXC symbol formats

Use two helpers everywhere:

```python
def to_mexc_spot_symbol(symbol: str) -> str:
    return symbol.upper().replace("_", "")

def to_mexc_contract_symbol(symbol: str) -> str:
    s = to_mexc_spot_symbol(symbol)
    return s[:-4] + "_" + s[-4:]
```

Examples:

- Binance: `SOLUSDT`, lower stream key `solusdt`;
- MEXC spot/public v3: `SOLUSDT`;
- MEXC futures v1: `SOL_USDT`.

## 2. Phase 1 (P0): Stability, Limit Orders, MEXC WebSocket, Health Check

### Phase 1 success condition

- Bot starts automatically after reboot through `systemd`.
- Bot writes heartbeat every 5 seconds.
- Bot consumes Binance and MEXC real-time feeds.
- MEXC REST polling remains as fallback only.
- Entry orders are post-only limit orders or fail closed to signal-only mode.
- Existing 8-pair behavior remains available.
- A dry-run mode can validate signals without placing orders.

### 1.1 Add operational config and dry-run guard

File: `/root/obsidian/tldr/проекты/mexc/mexc-tg-monitor.py`

Exact changes:

1. Add constants near current config section:

```python
BOT_VERSION = "v8-p0"
DRY_RUN = os.getenv("MEXC_DRY_RUN", "0") == "1"
AUTO_TRADE_ENABLED = os.getenv("MEXC_AUTO_TRADE", "1") == "1"
MEXC_WS_ENABLED = os.getenv("MEXC_WS_ENABLED", "1") == "1"
MEXC_REST_FALLBACK_ENABLED = os.getenv("MEXC_REST_FALLBACK_ENABLED", "1") == "1"
HEARTBEAT_PATH = os.getenv("MEXC_HEARTBEAT_PATH", "/run/mexc-monitor/heartbeat.json")
STATE_DIR = os.getenv("MEXC_STATE_DIR", "/root/obsidian/tldr/проекты/mexc/data")
```

2. Change:

```python
AUTO_MODE = bool(API_KEY and API_SECRET)
```

to:

```python
AUTO_MODE = bool(API_KEY and API_SECRET and AUTO_TRADE_ENABLED and not DRY_RUN)
```

3. Add startup logs showing `BOT_VERSION`, `DRY_RUN`, `AUTO_TRADE_ENABLED`, `MEXC_WS_ENABLED`, and whether API keys are present without printing values.

Validation:

```bash
MEXC_DRY_RUN=1 python3 /root/obsidian/tldr/проекты/mexc/mexc-tg-monitor.py
```

Expected: startup logs show dry-run/signal mode and no private order calls are made.

### 1.2 Replace hardcoded Telegram fallback with env-only token

File: `/root/obsidian/tldr/проекты/mexc/mexc-tg-monitor.py`

Exact changes:

1. Replace current hardcoded defaults:

```python
TG_BOT_TOKEN = os.getenv("MEXC_TG_BOT_TOKEN", "...")
TG_CHAT_ID = os.getenv("MEXC_TG_CHAT_ID", "...")
```

with:

```python
TG_BOT_TOKEN = os.getenv("MEXC_TG_BOT_TOKEN", "")
TG_CHAT_ID = os.getenv("MEXC_TG_CHAT_ID", "")
TG_ENABLED = bool(TG_BOT_TOKEN and TG_CHAT_ID)
```

2. In `send_tg`, add first line:

```python
if not TG_ENABLED:
    return
```

Validation:

```bash
python3 -m py_compile /root/obsidian/tldr/проекты/mexc/mexc-tg-monitor.py
rg -n "875402|AAGg|1371329042" /root/obsidian/tldr/проекты/mexc/mexc-tg-monitor.py
```

Expected: compile passes; `rg` finds no hardcoded token/chat ID.

### 1.3 Create post-only limit order path

File: `/root/obsidian/tldr/проекты/mexc/mexc-tg-monitor.py`

Current function:

```python
async def mexc_place_order(sym_key, direction, qty):
    ...
    body = {
        "symbol": msym_v1, "price": 0, "vol": qty,
        "side": order_side, "type": 1, "openType": 1,
        "positionId": 0, "externalOid": f"hermes_{int(time.time())}"
    }
```

Exact changes:

1. Rename current market function to `mexc_place_market_order` and keep it only for emergency close if needed.

2. Add:

```python
LIMIT_POST_ONLY_TYPE = 5
ENTRY_LIMIT_OFFSET_BPS = 1.0
ORDER_FILL_TIMEOUT_SEC = 2.0
```

3. Add a price quantizer:

```python
def quantize_price(price: float, tick_size: float | None = None) -> float:
    if tick_size:
        return round(round(price / tick_size) * tick_size, 12)
    return float(f"{price:.8f}")
```

4. Add `contract_meta` cache loaded from `GET https://contract.mexc.com/api/v1/contract/detail?symbol=SOL_USDT`. Store at least:

```python
contract_meta[sym_key] = {
    "contractSize": float(...),
    "priceUnit": float(...) if present else None,
    "volUnit": float(...) if present else None,
    "minVol": float(...) if present else 1,
}
```

5. Implement:

```python
async def mexc_place_limit_entry(sym_key: str, direction: str, qty: int, reference_price: float):
    msym_v1 = to_mexc_contract_symbol(PAIRS[sym_key])
    meta = await mexc_get_contract_meta(sym_key)
    tick = meta.get("priceUnit")

    if direction == "LONG":
        side = 1  # open long
        price = reference_price * (1 - ENTRY_LIMIT_OFFSET_BPS / 10000)
    else:
        side = 3  # open short
        price = reference_price * (1 + ENTRY_LIMIT_OFFSET_BPS / 10000)

    body = {
        "symbol": msym_v1,
        "price": quantize_price(price, tick),
        "vol": qty,
        "side": side,
        "type": LIMIT_POST_ONLY_TYPE,
        "openType": 1,
        "positionId": 0,
        "externalOid": f"hermes_limit_{int(time.time() * 1000)}",
    }
    return await mexc_api("POST", "/api/v1/private/order/create", body=body)
```

6. In `check_signals`, replace `mexc_place_order(sym, dr, qty)` with `mexc_place_limit_entry(sym, dr, qty, reference_price)`, where:

- LONG uses current MEXC best bid or midpoint as passive bid reference;
- SHORT uses current MEXC best ask or midpoint as passive ask reference.

7. If MEXC rejects `type=5`, do not fall back to market entry. Send Telegram/log:

```text
LIMIT_REJECTED: switched to signal-only for this event
```

Validation:

```bash
MEXC_DRY_RUN=1 python3 -m py_compile /root/obsidian/tldr/проекты/mexc/mexc-tg-monitor.py
rg -n '"type": 1|LIMIT_POST_ONLY_TYPE|mexc_place_limit_entry' /root/obsidian/tldr/проекты/mexc/mexc-tg-monitor.py
```

Expected: market `type: 1` remains only in explicitly named market helper/close path; entry path uses `LIMIT_POST_ONLY_TYPE`.

### 1.4 Add order fill timeout and cancel

File: `/root/obsidian/tldr/проекты/mexc/mexc-tg-monitor.py`

Exact changes:

1. Add private cancel endpoint helper:

```python
async def mexc_cancel_order(sym_key: str, order_id: str):
    msym_v1 = to_mexc_contract_symbol(PAIRS[sym_key])
    body = {"symbol": msym_v1, "orderId": order_id}
    return await mexc_api("POST", "/api/v1/private/order/cancel", body=body)
```

2. Add order detail helper if MEXC response does not immediately provide filled status:

```python
async def mexc_get_order(order_id: str):
    return await mexc_api("GET", "/api/v1/private/order/get/" + str(order_id))
```

3. After placing a limit entry, wait up to `ORDER_FILL_TIMEOUT_SEC`; if not filled, cancel and do not add to `active_trades`.

Implementation pattern:

```python
order = await mexc_place_limit_entry(...)
order_id = str(order.get("data", {}).get("orderId", ""))
filled = await wait_for_order_fill(sym, order_id, ORDER_FILL_TIMEOUT_SEC)
if not filled:
    await mexc_cancel_order(sym, order_id)
    order_placed = False
```

4. If order status endpoint shape differs, log raw JSON for this helper only with secrets excluded.

Validation:

```bash
python3 -m py_compile /root/obsidian/tldr/проекты/mexc/mexc-tg-monitor.py
```

Manual dry-run validation: force a fake signal in test mode and verify no trade is inserted unless `filled=True`.

### 1.5 Add MEXC WebSocket client

File: `/root/obsidian/tldr/проекты/mexc/mexc-tg-monitor.py`

Exact changes:

1. Add endpoint constant:

```python
MEXC_CONTRACT_WS = "wss://contract.mexc.com/edge"
MEXC_WS_STALE_SEC = 5
```

2. Add `price_source_ts = {"binance": {}, "mexc": {}}`.

3. Implement `mexc_ws_stream()` using `websockets.connect`.

Initial subscription pattern to verify first:

```python
sub = {
    "method": "sub.ticker",
    "param": {"symbol": "SOL_USDT"}
}
```

If MEXC rejects `sub.ticker`, try documented contract channels in this order:

```python
{"method": "sub.depth", "param": {"symbol": "SOL_USDT"}}
{"method": "sub.depth.full", "param": {"symbol": "SOL_USDT", "limit": 5}}
```

Do this verification with a small standalone probe before editing production logic:

```bash
python3 - <<'PY'
import asyncio, json, websockets

async def main():
    async with websockets.connect("wss://contract.mexc.com/edge", ping_interval=20) as ws:
        await ws.send(json.dumps({"method": "sub.ticker", "param": {"symbol": "SOL_USDT"}}))
        for _ in range(5):
            print(await asyncio.wait_for(ws.recv(), timeout=10))

asyncio.run(main())
PY
```

4. Parse both likely ticker/depth response shapes defensively:

```python
def parse_mexc_ws_message(msg: dict):
    channel = msg.get("channel") or msg.get("method") or ""
    data = msg.get("data") or {}
    symbol = data.get("symbol") or msg.get("symbol")
    bid = data.get("bid1") or data.get("bidPrice") or data.get("bid")
    ask = data.get("ask1") or data.get("askPrice") or data.get("ask")
    ...
```

5. Update:

```python
mexc_prices[sym_key] = {"bid": float(bid), "ask": float(ask), "source": "ws", "ts": time.time()}
```

6. Add ping/pong handling:

```python
if "ping" in msg:
    await ws.send(json.dumps({"pong": msg["ping"]}))
```

7. Reconnect on exception with exponential backoff capped at 30 seconds.

Validation:

```bash
MEXC_DRY_RUN=1 MEXC_WS_ENABLED=1 python3 /root/obsidian/tldr/проекты/mexc/mexc-tg-monitor.py
```

Expected logs:

- `MEXC WS connected`;
- price updates for all 8 current pairs within 10 seconds;
- no crash if one subscription is rejected.

### 1.6 Keep REST poller as stale-data fallback

File: `/root/obsidian/tldr/проекты/mexc/mexc-tg-monitor.py`

Exact changes:

1. Rename `mexc_poller()` to `mexc_rest_fallback_poller()`.

2. Poll only symbols whose MEXC WS data is missing or stale:

```python
def mexc_price_is_stale(sym_key: str) -> bool:
    p = mexc_prices.get(sym_key)
    return not p or time.time() - p.get("ts", 0) > MEXC_WS_STALE_SEC
```

3. Use `MEXC_REST_FALLBACK_ENABLED` to disable fallback during WebSocket tests.

4. Keep endpoint:

```text
GET https://api.mexc.com/api/v3/ticker/bookTicker?symbol=SOLUSDT
```

5. Add `source="rest"` and `ts=time.time()` to fallback price objects.

Validation:

```bash
MEXC_DRY_RUN=1 MEXC_WS_ENABLED=0 MEXC_REST_FALLBACK_ENABLED=1 python3 /root/obsidian/tldr/проекты/mexc/mexc-tg-monitor.py
```

Expected: bot still receives MEXC prices through REST.

### 1.7 Add heartbeat writer

File: `/root/obsidian/tldr/проекты/mexc/mexc-tg-monitor.py`

Exact changes:

1. Add:

```python
async def health_reporter():
    while True:
        os.makedirs(os.path.dirname(HEARTBEAT_PATH), exist_ok=True)
        payload = {
            "ts": time.time(),
            "iso": datetime.utcnow().isoformat() + "Z",
            "version": BOT_VERSION,
            "pid": os.getpid(),
            "pairs": len(PAIRS),
            "binance_prices": len(binance_prices),
            "mexc_prices": len(mexc_prices),
            "active_trades": len(active_trades),
            "auto_mode": AUTO_MODE,
            "dry_run": DRY_RUN,
        }
        tmp = HEARTBEAT_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(payload, f)
        os.replace(tmp, HEARTBEAT_PATH)
        await asyncio.sleep(5)
```

2. Add `health_reporter()` to `asyncio.gather(...)` in `main`.

Validation:

```bash
MEXC_DRY_RUN=1 python3 /root/obsidian/tldr/проекты/mexc/mexc-tg-monitor.py &
sleep 7
cat /run/mexc-monitor/heartbeat.json
```

Expected: JSON exists and `ts` updates.

### 1.8 Add systemd service

File to create: `/etc/systemd/system/mexc-monitor.service`

Exact contents:

```ini
[Unit]
Description=MEXC-Binance Spread Monitor
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
WorkingDirectory=/root/obsidian/tldr/проекты/mexc
ExecStart=/usr/bin/python3 /root/bin/mexc-tg-monitor.py
Restart=always
RestartSec=5
StartLimitIntervalSec=300
StartLimitBurst=10
Environment=MEXC_AUTO_TRADE=1
Environment=MEXC_DRY_RUN=0
Environment=MEXC_WS_ENABLED=1
Environment=MEXC_REST_FALLBACK_ENABLED=1
Environment=MEXC_HEARTBEAT_PATH=/run/mexc-monitor/heartbeat.json
RuntimeDirectory=mexc-monitor
RuntimeDirectoryMode=0755
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

Validation:

```bash
python3 -m py_compile /root/obsidian/tldr/проекты/mexc/mexc-tg-monitor.py
cp /root/obsidian/tldr/проекты/mexc/mexc-tg-monitor.py /root/bin/mexc-tg-monitor.py
chmod +x /root/bin/mexc-tg-monitor.py
systemctl daemon-reload
systemctl enable mexc-monitor.service
systemctl restart mexc-monitor.service
systemctl status mexc-monitor.service --no-pager
journalctl -u mexc-monitor.service -n 80 --no-pager
```

Expected: service active, heartbeat file exists, no import/runtime errors.

### 1.9 Add external health-check script

File to create: `/root/bin/mexc-monitor-healthcheck.sh`

Exact contents:

```bash
#!/usr/bin/env bash
set -euo pipefail

HEARTBEAT="${MEXC_HEARTBEAT_PATH:-/run/mexc-monitor/heartbeat.json}"
MAX_AGE="${MEXC_HEARTBEAT_MAX_AGE:-20}"

if [[ ! -f "$HEARTBEAT" ]]; then
  echo "missing heartbeat: $HEARTBEAT"
  systemctl restart mexc-monitor.service
  exit 1
fi

now="$(date +%s)"
ts="$(python3 - <<PY
import json
with open("$HEARTBEAT") as f:
    print(int(float(json.load(f).get("ts", 0))))
PY
)"
age="$((now - ts))"

if (( age > MAX_AGE )); then
  echo "stale heartbeat age=${age}s > ${MAX_AGE}s"
  systemctl restart mexc-monitor.service
  exit 1
fi

echo "ok age=${age}s"
```

Optional timer files:

- `/etc/systemd/system/mexc-monitor-healthcheck.service`;
- `/etc/systemd/system/mexc-monitor-healthcheck.timer`.

Timer:

```ini
[Timer]
OnBootSec=1min
OnUnitActiveSec=30s
AccuracySec=5s
Unit=mexc-monitor-healthcheck.service
```

Validation:

```bash
chmod +x /root/bin/mexc-monitor-healthcheck.sh
/root/bin/mexc-monitor-healthcheck.sh
```

Expected: `ok age=<20s`.

## 3. Phase 2 (P1): 30-50 Pairs, Smart Pair Filter, Entry Threshold 0.06%

### Phase 2 success condition

- The bot can discover and maintain an active universe of 30-50 symbols.
- Only pairs listed on both Binance and MEXC with acceptable spread/liquidity are monitored.
- Entry threshold can be reduced from `0.12%` to `0.06%` only after Phase 1 limit-order and cross-pricing checks are active.
- The bot avoids subscribing to weak/noisy pairs.

### 2.1 Replace static `PAIRS` with base candidates plus dynamic active pairs

File: `/root/obsidian/tldr/проекты/mexc/mexc-tg-monitor.py`

Exact changes:

1. Keep current 8 pairs as the bootstrap list:

```python
BOOTSTRAP_PAIRS = {
    "zecusdt": "ZECUSDT",
    "xlmusdt": "XLMUSDT",
    "solusdt": "SOLUSDT",
    "bchusdt": "BCHUSDT",
    "btcusdt": "BTCUSDT",
    "ethusdt": "ETHUSDT",
    "xrpusdt": "XRPUSDT",
    "ltcusdt": "LTCUSDT",
}
PAIRS = dict(BOOTSTRAP_PAIRS)
```

2. Add:

```python
TARGET_PAIR_MIN = 30
TARGET_PAIR_MAX = 50
PAIR_REFRESH_SEC = 3600
MIN_24H_QUOTE_VOLUME = 500_000
PAIR_FILTER_MIN_LIQUIDITY = 1000
PAIR_FILTER_MAX_MEXC_SPREAD = 0.05
```

Validation:

```bash
python3 -m py_compile /root/obsidian/tldr/проекты/mexc/mexc-tg-monitor.py
```

### 2.2 Fetch Binance tradable USDT symbols

File: `/root/obsidian/tldr/проекты/mexc/mexc-tg-monitor.py`

Exact changes:

1. Add public Binance REST helper using `curl_cffi` or `aiohttp`. Since Binance is not the Cloudflare problem, `aiohttp` is acceptable, but for consistency use `_curl` with non-MEXC headers stripped if needed.

Endpoint:

```text
GET https://api.binance.com/api/v3/exchangeInfo
```

2. Parse symbols where:

```python
status == "TRADING"
quoteAsset == "USDT"
isSpotTradingAllowed == True
```

3. Store set like:

```python
binance_usdt_symbols = {"SOLUSDT", "XLMUSDT", ...}
```

Validation:

```bash
python3 - <<'PY'
# call the new helper or temporary equivalent and print len(symbols)
PY
```

Expected: more than 100 Binance USDT symbols.

### 2.3 Fetch MEXC available contracts

File: `/root/obsidian/tldr/проекты/mexc/mexc-tg-monitor.py`

Exact changes:

1. Add:

```python
async def mexc_get_all_contracts():
    r = await asyncio.to_thread(_curl, "GET", "https://contract.mexc.com/api/v1/contract/detail")
    ...
```

Endpoint:

```text
GET https://contract.mexc.com/api/v1/contract/detail
```

2. Parse each item’s contract symbol, status fields, `contractSize`, `priceUnit`, `volUnit`, `minVol`, and fee fields if present.

3. Convert `SOL_USDT` to `SOLUSDT` for intersection with Binance.

Validation:

```bash
python3 - <<'PY'
# call helper or temporary equivalent and print first 10 *_USDT contracts
PY
```

Expected: MEXC returns contract metadata through `contract.mexc.com`, not `api.mexc.com`.

### 2.4 Score and filter pair candidates

File: `/root/obsidian/tldr/проекты/mexc/mexc-tg-monitor.py`

Exact changes:

1. Add `PairStats` dictionary:

```python
pair_stats[sym_key] = {
    "mexc_spread_pct": ...,
    "depth_usd": ...,
    "binance_24h_quote_volume": ...,
    "signals_1h": ...,
    "reject_reason": "",
}
```

2. For each Binance/MEXC overlap:

- fetch Binance 24h ticker:

```text
GET https://api.binance.com/api/v3/ticker/24hr?symbol=SOLUSDT
```

- fetch MEXC depth:

```text
GET https://api.mexc.com/api/v3/depth?symbol=SOLUSDT&limit=20
```

- reject when:

```python
binance_quote_volume < MIN_24H_QUOTE_VOLUME
mexc_spread_pct > PAIR_FILTER_MAX_MEXC_SPREAD
depth_usd < PAIR_FILTER_MIN_LIQUIDITY
symbol in manual blocklist
```

3. Sort by:

```python
score = (depth_usd / 1000) + (binance_quote_volume / 1_000_000) + (signals_1h * 2) - (mexc_spread_pct * 100)
```

4. Select top `TARGET_PAIR_MAX`, but never fewer than bootstrap pairs.

Validation:

```bash
MEXC_DRY_RUN=1 python3 /root/obsidian/tldr/проекты/mexc/mexc-tg-monitor.py
```

Expected: startup log prints selected pair count and top 10 symbols with reject summary.

### 2.5 Resubscribe WebSockets when pair universe changes

File: `/root/obsidian/tldr/проекты/mexc/mexc-tg-monitor.py`

Exact changes:

1. Add `pair_generation = 0`.

2. Pair selector increments generation when `PAIRS` changes.

3. `binance_stream()` and `mexc_ws_stream()` watch generation; when it changes, reconnect with the new stream/subscription set.

4. Clear stale price entries for removed symbols:

```python
for removed in old_keys - new_keys:
    binance_prices.pop(removed, None)
    mexc_prices.pop(removed, None)
    last_alert_ts.pop(removed, None)
```

Validation:

Change `TARGET_PAIR_MAX=10` then `TARGET_PAIR_MAX=12` in dry run and verify reconnect logs mention new stream count.

### 2.6 Lower entry threshold to 0.06%

File: `/root/obsidian/tldr/проекты/mexc/mexc-tg-monitor.py`

Exact changes:

1. Do not lower threshold until Phase 3 bid/ask cross pricing is active. Add these constants now:

```python
ENTRY_SPREAD = float(os.getenv("MEXC_ENTRY_SPREAD", "0.06"))
TG_THRESHOLD = float(os.getenv("MEXC_TG_THRESHOLD", str(ENTRY_SPREAD)))
MIN_NET_EDGE_PCT = float(os.getenv("MEXC_MIN_NET_EDGE_PCT", "0.04"))
```

2. During Phase 2, run with env override if Phase 3 is not ready:

```ini
Environment=MEXC_ENTRY_SPREAD=0.12
```

3. After Phase 3, systemd can set:

```ini
Environment=MEXC_ENTRY_SPREAD=0.06
Environment=MEXC_MIN_NET_EDGE_PCT=0.04
```

Validation:

```bash
MEXC_ENTRY_SPREAD=0.06 MEXC_DRY_RUN=1 python3 /root/bin/mexc-tg-monitor.py
```

Expected: logs show `Вход ≥0.06%`.

## 4. Phase 3 (P1): Fee Modeling, Bid/Ask Cross Pricing, SQLite PnL, Dynamic Position Sizing

### Phase 3 success condition

- Signal decisions use executable cross prices, not midpoint spread.
- Each trade has expected fee-adjusted edge before entry.
- Each opened/closed trade is recorded in SQLite.
- Position size changes based on equity, risk limits, pair liquidity, and recent performance.

### 3.1 Replace midpoint spread with bid/ask cross spread

File: `/root/obsidian/tldr/проекты/mexc/mexc-tg-monitor.py`

Current logic:

```python
bm = (bp["bid"] + bp["ask"]) / 2
mm = (mp["bid"] + mp["ask"]) / 2
sp = (mm - bm) / bm * 100
dr = "SHORT" if mm > bm else "LONG"
```

Exact replacement:

```python
def compute_cross_edges(bp: dict, mp: dict) -> dict:
    b_bid, b_ask = bp["bid"], bp["ask"]
    m_bid, m_ask = mp["bid"], mp["ask"]

    # Binance higher than MEXC: buy/long MEXC expecting it to catch up.
    long_edge_pct = (b_bid - m_ask) / m_ask * 100

    # MEXC higher than Binance: sell/short MEXC expecting it to fall.
    short_edge_pct = (m_bid - b_ask) / b_ask * 100

    return {
        "LONG": {
            "edge_pct": long_edge_pct,
            "binance_ref": b_bid,
            "mexc_entry": m_ask,
            "mexc_passive_price": m_bid,
        },
        "SHORT": {
            "edge_pct": short_edge_pct,
            "binance_ref": b_ask,
            "mexc_entry": m_bid,
            "mexc_passive_price": m_ask,
        },
    }
```

Then choose:

```python
edges = compute_cross_edges(bp, mp)
direction, edge = max(edges.items(), key=lambda kv: kv[1]["edge_pct"])
if edge["edge_pct"] < ENTRY_SPREAD:
    continue
```

Validation:

Add unit-style assertions in a temporary `python3 - <<'PY'` block:

```python
bp = {"bid": 101, "ask": 101.1}
mp = {"bid": 100, "ask": 100.1}
assert compute_cross_edges(bp, mp)["LONG"]["edge_pct"] > 0
assert compute_cross_edges(bp, mp)["SHORT"]["edge_pct"] < 0
```

### 3.2 Add fee model

File: `/root/obsidian/tldr/проекты/mexc/mexc-tg-monitor.py`

Exact changes:

1. Add constants:

```python
MEXC_MAKER_FEE_PCT = float(os.getenv("MEXC_MAKER_FEE_PCT", "0.0"))
MEXC_TAKER_FEE_PCT = float(os.getenv("MEXC_TAKER_FEE_PCT", "0.01"))
ENTRY_FEE_MODE = os.getenv("MEXC_ENTRY_FEE_MODE", "maker")
EXIT_FEE_MODE = os.getenv("MEXC_EXIT_FEE_MODE", "maker")
SLIPPAGE_BUFFER_PCT = float(os.getenv("MEXC_SLIPPAGE_BUFFER_PCT", "0.01"))
```

2. Add:

```python
def fee_pct(mode: str) -> float:
    return MEXC_MAKER_FEE_PCT if mode == "maker" else MEXC_TAKER_FEE_PCT

def compute_net_edge_pct(raw_edge_pct: float) -> float:
    return raw_edge_pct - fee_pct(ENTRY_FEE_MODE) - fee_pct(EXIT_FEE_MODE) - SLIPPAGE_BUFFER_PCT
```

3. Require:

```python
net_edge_pct >= MIN_NET_EDGE_PCT
```

4. Log both raw and net edge:

```text
SOLUSDT raw=0.072% net=0.062% fees=0.000% slip=0.010%
```

Validation:

```bash
MEXC_MAKER_FEE_PCT=0 MEXC_TAKER_FEE_PCT=0.01 python3 -m py_compile /root/obsidian/tldr/проекты/mexc/mexc-tg-monitor.py
```

### 3.3 Add SQLite trade store

File: `/root/obsidian/tldr/проекты/mexc/mexc-tg-monitor.py`

File to create at runtime:

```text
/root/obsidian/tldr/проекты/mexc/data/mexc-trades.sqlite3
```

Exact changes:

1. Add:

```python
import sqlite3
```

2. Add schema initializer:

```sql
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    opened_at TEXT NOT NULL,
    closed_at TEXT,
    symbol TEXT NOT NULL,
    direction TEXT NOT NULL,
    qty REAL NOT NULL,
    leverage REAL NOT NULL,
    amount_usd REAL NOT NULL,
    entry_order_id TEXT,
    exit_order_id TEXT,
    entry_price REAL,
    exit_price REAL,
    binance_entry_ref REAL,
    binance_exit_ref REAL,
    raw_edge_entry_pct REAL,
    net_edge_entry_pct REAL,
    raw_edge_exit_pct REAL,
    expected_fee_usd REAL,
    realized_fee_usd REAL,
    expected_pnl_usd REAL,
    realized_pnl_usd REAL,
    status TEXT NOT NULL,
    close_reason TEXT,
    reject_reason TEXT,
    meta_json TEXT
);

CREATE INDEX IF NOT EXISTS idx_trades_opened_at ON trades(opened_at);
CREATE INDEX IF NOT EXISTS idx_trades_symbol ON trades(symbol);
CREATE INDEX IF NOT EXISTS idx_trades_status ON trades(status);
```

3. Add `TradeStore` functions:

```python
db_init()
db_insert_signal(...)
db_mark_opened(...)
db_mark_closed(...)
db_mark_rejected(...)
db_daily_stats(day)
```

4. Record every accepted signal, rejected trade, entry order, timeout cancel, and close.

5. Keep `/tmp/mexc-spread-alerts.log` for backward compatibility, but SQLite is the source of truth.

Validation:

```bash
MEXC_DRY_RUN=1 python3 /root/bin/mexc-tg-monitor.py
sqlite3 /root/obsidian/tldr/проекты/mexc/data/mexc-trades.sqlite3 '.schema trades'
```

Expected: schema exists.

### 3.4 Replace estimated PnL with recorded PnL calculation

File: `/root/obsidian/tldr/проекты/mexc/mexc-tg-monitor.py`

Current logic:

```python
pnl_usd = trade['entry_sp'] * TRADE_LEVERAGE / 100 * TRADE_AMOUNT_BASE
record_trade(pnl_usd)
```

Exact replacement:

```python
def estimate_pnl_usd(amount_usd: float, leverage: float, entry_edge_pct: float, exit_edge_pct: float, fee_pct_total: float) -> float:
    gross = amount_usd * leverage * ((entry_edge_pct - exit_edge_pct) / 100)
    fees = amount_usd * leverage * (fee_pct_total / 100)
    return gross - fees
```

On close:

- `exit_edge_pct` is current absolute cross edge;
- `fee_pct_total = fee_pct(ENTRY_FEE_MODE) + fee_pct(EXIT_FEE_MODE)`;
- store calculated PnL in SQLite;
- then call `record_trade(realized_pnl_usd)`.

Validation:

Manual assertion:

```python
assert estimate_pnl_usd(7, 20, 0.10, 0.02, 0.0) == 0.112
```

Use tolerance for float in actual check.

### 3.5 Add dynamic position sizing

File: `/root/obsidian/tldr/проекты/mexc/mexc-tg-monitor.py`

Exact changes:

1. Add constants:

```python
MIN_TRADE_AMOUNT_USD = float(os.getenv("MEXC_MIN_TRADE_AMOUNT_USD", "5"))
MAX_TRADE_AMOUNT_USD = float(os.getenv("MEXC_MAX_TRADE_AMOUNT_USD", "12"))
BASE_RISK_FRACTION = float(os.getenv("MEXC_BASE_RISK_FRACTION", "0.25"))
MAX_DEPTH_USAGE_PCT = float(os.getenv("MEXC_MAX_DEPTH_USAGE_PCT", "5"))
LOSS_SIZE_REDUCTION_FACTOR = float(os.getenv("MEXC_LOSS_SIZE_REDUCTION_FACTOR", "0.5"))
```

2. Add account equity helper if a MEXC private asset endpoint is confirmed. If not confirmed in Phase 3, use env fallback:

```python
ACCOUNT_EQUITY_USD = float(os.getenv("MEXC_ACCOUNT_EQUITY_USD", "30"))
```

3. Add:

```python
def compute_trade_amount_usd(sym_key: str, net_edge_pct: float, depth_usd: float) -> float:
    base = ACCOUNT_EQUITY_USD * BASE_RISK_FRACTION
    depth_cap = depth_usd * MAX_DEPTH_USAGE_PCT / 100 / TRADE_LEVERAGE
    edge_boost = min(max(net_edge_pct / 0.06, 0.75), 1.5)
    loss_factor = LOSS_SIZE_REDUCTION_FACTOR if _consecutive_losses >= 2 else 1.0
    jitter = random.uniform(0.85, 1.15)
    amount = base * edge_boost * loss_factor * jitter
    amount = min(amount, depth_cap, MAX_TRADE_AMOUNT_USD)
    amount = max(amount, MIN_TRADE_AMOUNT_USD)
    return round(amount, 2)
```

4. Update `mexc_get_qty(sym_key)` to accept `amount_usd`:

```python
async def mexc_get_qty(sym_key, amount_usd):
    qty = int((amount_usd * TRADE_LEVERAGE) / (price * contract_size))
```

Validation:

Dry-run logs show different trade amounts based on edge/depth and never below `$5` or above configured max.

## 5. Phase 4 (P2): Anti-PK Behavior Simulation, -PNL Monitoring, Volume Limits

### Phase 4 success condition

- Bot stops or slows before account PnL becomes positive.
- Bot tracks cumulative notional volume and daily volume.
- Bot randomizes timing/size/pair selection within risk limits.
- Bot can run signal-only if maker fee, account state, or volume limits become unsafe.

### 4.1 Add account PnL monitor

File: `/root/obsidian/tldr/проекты/mexc/mexc-tg-monitor.py`

Exact changes:

1. Confirm the exact MEXC private endpoint in a dry-run probe. Candidate futures endpoints to test with current V1 signature:

```text
GET https://contract.mexc.com/api/v1/private/account/assets
GET https://contract.mexc.com/api/v1/private/account/asset/USDT
GET https://contract.mexc.com/api/v1/private/position/open_positions
```

2. Add:

```python
NEGATIVE_PNL_TARGET_USD = float(os.getenv("MEXC_NEGATIVE_PNL_TARGET_USD", "-200"))
STOP_IF_ACCOUNT_PNL_ABOVE = float(os.getenv("MEXC_STOP_IF_ACCOUNT_PNL_ABOVE", "0"))
THROTTLE_IF_ACCOUNT_PNL_ABOVE = float(os.getenv("MEXC_THROTTLE_IF_ACCOUNT_PNL_ABOVE", "-50"))
account_state = {"pnl_usd": None, "equity_usd": None, "updated_at": 0}
```

3. Implement `account_monitor()` every 60 seconds:

- fetch account/position state;
- calculate or read realized/unrealized PnL;
- update `account_state`;
- if `pnl_usd >= STOP_IF_ACCOUNT_PNL_ABOVE`, set global `TRADING_PAUSED_REASON = "account pnl >= stop threshold"`;
- if `pnl_usd >= THROTTLE_IF_ACCOUNT_PNL_ABOVE`, reduce `MAX_TRADES_PER_DAY` effective limit by 50% and increase delays.

4. Add this reason to `risk_check()`.

Validation:

```bash
MEXC_DRY_RUN=1 python3 /root/bin/mexc-tg-monitor.py
```

Expected: logs show account endpoint success or explicit `account_monitor unavailable`; bot does not crash if endpoint shape differs.

### 4.2 Add volume limits

File: `/root/obsidian/tldr/проекты/mexc/mexc-tg-monitor.py`

Exact changes:

1. Add constants:

```python
MAX_DAILY_NOTIONAL_USD = float(os.getenv("MEXC_MAX_DAILY_NOTIONAL_USD", "5000"))
MAX_TOTAL_NOTIONAL_USD = float(os.getenv("MEXC_MAX_TOTAL_NOTIONAL_USD", "200000"))
VOLUME_THROTTLE_AT_PCT = float(os.getenv("MEXC_VOLUME_THROTTLE_AT_PCT", "80"))
```

2. Add SQLite table:

```sql
CREATE TABLE IF NOT EXISTS volume_ledger (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    notional_usd REAL NOT NULL,
    trade_id INTEGER,
    meta_json TEXT
);
```

3. On every filled entry and exit, insert `amount_usd * leverage` into `volume_ledger`.

4. Add `risk_check()` rules:

- reject new trades if daily notional >= `MAX_DAILY_NOTIONAL_USD`;
- reject new trades if total notional >= `MAX_TOTAL_NOTIONAL_USD`;
- if above `VOLUME_THROTTLE_AT_PCT`, increase minimum edge by 50% and double cooldown.

Validation:

```bash
sqlite3 /root/obsidian/tldr/проекты/mexc/data/mexc-trades.sqlite3 \
  'select coalesce(sum(notional_usd),0) from volume_ledger;'
```

Expected: sum updates after simulated/dry-run ledger insert tests.

### 4.3 Add anti-PK behavior simulation

File: `/root/obsidian/tldr/проекты/mexc/mexc-tg-monitor.py`

Exact changes:

1. Add constants:

```python
HUMAN_DELAY_MIN_SEC = float(os.getenv("MEXC_HUMAN_DELAY_MIN_SEC", "0.8"))
HUMAN_DELAY_MAX_SEC = float(os.getenv("MEXC_HUMAN_DELAY_MAX_SEC", "4.5"))
PAIR_REPEAT_COOLDOWN_SEC = float(os.getenv("MEXC_PAIR_REPEAT_COOLDOWN_SEC", "300"))
RANDOM_SKIP_PROBABILITY = float(os.getenv("MEXC_RANDOM_SKIP_PROBABILITY", "0.08"))
SESSION_TRADE_MIN = int(os.getenv("MEXC_SESSION_TRADE_MIN", "3"))
SESSION_TRADE_MAX = int(os.getenv("MEXC_SESSION_TRADE_MAX", "8"))
SESSION_PAUSE_MIN_SEC = int(os.getenv("MEXC_SESSION_PAUSE_MIN_SEC", "180"))
SESSION_PAUSE_MAX_SEC = int(os.getenv("MEXC_SESSION_PAUSE_MAX_SEC", "900"))
```

2. Add state:

```python
last_pair_trade_ts = {}
session_trade_limit = random.randint(SESSION_TRADE_MIN, SESSION_TRADE_MAX)
session_trade_count = 0
session_pause_until = 0
```

3. In `risk_check()`:

- reject if `time.time() < session_pause_until`;
- reject if same symbol traded within `PAIR_REPEAT_COOLDOWN_SEC`;
- reject randomly when `random.random() < RANDOM_SKIP_PROBABILITY`, but log `human_random_skip`.

4. Replace fixed open delay:

```python
delay = random.uniform(HUMAN_DELAY_MIN_SEC, HUMAN_DELAY_MAX_SEC)
```

5. After `session_trade_count >= session_trade_limit`, set:

```python
session_pause_until = time.time() + random.randint(SESSION_PAUSE_MIN_SEC, SESSION_PAUSE_MAX_SEC)
session_trade_limit = random.randint(SESSION_TRADE_MIN, SESSION_TRADE_MAX)
session_trade_count = 0
```

Validation:

Dry-run logs show varied delays, skips, and pauses.

### 4.4 Add maker-fee safety monitor

File: `/root/obsidian/tldr/проекты/mexc/mexc-tg-monitor.py`

Exact changes:

1. During contract metadata fetch, inspect fields likely named `makerFeeRate`, `takerFeeRate`, `makerFee`, `takerFee`, or similar.

2. If any active pair reports maker fee above `0`, set:

```python
PAIR_TRADE_DISABLED[sym_key] = "maker fee not zero"
```

3. If fee fields are unavailable, keep env fee model as authority but log:

```text
fee fields unavailable; using MEXC_MAKER_FEE_PCT=...
```

4. Do not auto-trade symbols where known maker fee exceeds configured `MEXC_MAKER_FEE_PCT`.

Validation:

Temporarily set `MEXC_MAKER_FEE_PCT=0.02` and confirm net edge and trade filters change.

## 6. File-by-File Change List

### `/root/obsidian/tldr/проекты/mexc/mexc-tg-monitor.py`

Primary implementation file.

Exact change list:

- Add `BOT_VERSION`, dry-run flags, WebSocket flags, heartbeat path, state directory constants.
- Remove hardcoded Telegram token/chat defaults; use env-only Telegram config.
- Add symbol helpers `to_mexc_spot_symbol`, `to_mexc_contract_symbol`.
- Extend `_curl` to merge browser headers into every MEXC REST request.
- Keep `_mexc_sig`, `_param_string`, `_mexc_headers`, but ensure private REST calls use `contract.mexc.com`.
- Add JSON content-type guard for every REST response.
- Add `contract_meta` cache and `mexc_get_contract_meta`.
- Replace entry market orders with `mexc_place_limit_entry`.
- Add `mexc_cancel_order`, `mexc_get_order`, and `wait_for_order_fill`.
- Rename market order helper to `mexc_place_market_order` and keep it out of normal entry flow.
- Add `MEXC_CONTRACT_WS`, `mexc_ws_stream`, WebSocket message parser, stale data timestamps, and reconnect backoff.
- Rename `mexc_poller` to `mexc_rest_fallback_poller` and poll stale symbols only.
- Add `health_reporter`.
- Replace midpoint spread with `compute_cross_edges`.
- Add fee model functions `fee_pct` and `compute_net_edge_pct`.
- Add SQLite initialization and trade/volume write helpers.
- Add dynamic position sizing with `compute_trade_amount_usd`.
- Add pair discovery/filtering helpers:
  - Binance exchangeInfo fetch;
  - MEXC contract detail fetch;
  - 24h volume/depth scoring;
  - WebSocket resubscribe generation.
- Add account monitor and volume ledger checks.
- Add anti-pattern randomization state and checks.
- Update `main()` gather list to include:
  - `binance_stream()`;
  - `mexc_ws_stream()` if enabled;
  - `mexc_rest_fallback_poller()` if enabled;
  - `pair_selector_loop()` after Phase 2;
  - `account_monitor()` after Phase 4;
  - `health_reporter()`;
  - `check_signals()`.

### `/root/bin/mexc-tg-monitor.py`

Deployment copy.

Exact change list:

- Do not edit directly except emergency hotfix.
- After source validation, copy project file here:

```bash
cp /root/obsidian/tldr/проекты/mexc/mexc-tg-monitor.py /root/bin/mexc-tg-monitor.py
chmod +x /root/bin/mexc-tg-monitor.py
```

- Verify deployed copy is identical:

```bash
cmp -s /root/obsidian/tldr/проекты/mexc/mexc-tg-monitor.py /root/bin/mexc-tg-monitor.py
echo $?
```

Expected: `0`.

### `/etc/systemd/system/mexc-monitor.service`

New service file.

Exact change list:

- Create service shown in Phase 1.8.
- Add runtime directory `/run/mexc-monitor`.
- Add environment flags for auto-trade, dry-run, MEXC WS, REST fallback, heartbeat.
- Use `/root/bin/mexc-tg-monitor.py` as deployed executable.
- Restart always with 5s delay.

Validation:

```bash
systemctl daemon-reload
systemctl enable mexc-monitor.service
systemctl restart mexc-monitor.service
systemctl status mexc-monitor.service --no-pager
```

### `/root/bin/mexc-monitor-healthcheck.sh`

New health-check script.

Exact change list:

- Create shell script shown in Phase 1.9.
- Restart service when heartbeat is missing or stale.
- Exit non-zero on missing/stale heartbeat.

Validation:

```bash
chmod +x /root/bin/mexc-monitor-healthcheck.sh
/root/bin/mexc-monitor-healthcheck.sh
```

### `/etc/systemd/system/mexc-monitor-healthcheck.service`

Optional new systemd oneshot.

Exact contents:

```ini
[Unit]
Description=MEXC Monitor heartbeat health-check

[Service]
Type=oneshot
ExecStart=/root/bin/mexc-monitor-healthcheck.sh
```

### `/etc/systemd/system/mexc-monitor-healthcheck.timer`

Optional new timer.

Exact contents:

```ini
[Unit]
Description=Run MEXC Monitor health-check every 30 seconds

[Timer]
OnBootSec=1min
OnUnitActiveSec=30s
AccuracySec=5s
Unit=mexc-monitor-healthcheck.service

[Install]
WantedBy=timers.target
```

Validation:

```bash
systemctl daemon-reload
systemctl enable --now mexc-monitor-healthcheck.timer
systemctl list-timers --all | rg mexc-monitor
```

### `/root/obsidian/tldr/проекты/mexc/data/mexc-trades.sqlite3`

Runtime SQLite DB.

Exact change list:

- Created automatically by `db_init()`.
- Tables:
  - `trades`;
  - `volume_ledger`;
  - optional `pair_stats` if runtime scoring should persist across restarts.

Validation:

```bash
sqlite3 /root/obsidian/tldr/проекты/mexc/data/mexc-trades.sqlite3 '.tables'
```

### `/root/.mexc-api/config.json`

Existing secret config.

Exact change list:

- No required structural change.
- Keep keys:
  - `api_key`;
  - `api_secret`;
  - `comment`.
- Do not commit, print, or copy this file into project docs.

Validation:

```bash
python3 - <<'PY'
import json
p="/root/.mexc-api/config.json"
with open(p) as f:
    d=json.load(f)
print("api_key_set", bool(d.get("api_key")))
print("api_secret_set", bool(d.get("api_secret")))
PY
```

### `/root/obsidian/tldr/проекты/mexc/README.md`

Documentation update after implementation.

Exact change list:

- Update current architecture from `MEXC REST polling every 1 sec` to `MEXC WebSocket with REST fallback`.
- Document systemd commands:

```bash
systemctl status mexc-monitor.service --no-pager
journalctl -u mexc-monitor.service -f
/root/bin/mexc-monitor-healthcheck.sh
```

- Document env flags:
  - `MEXC_DRY_RUN`;
  - `MEXC_AUTO_TRADE`;
  - `MEXC_ENTRY_SPREAD`;
  - `MEXC_MAKER_FEE_PCT`;
  - `MEXC_ACCOUNT_EQUITY_USD`;
  - `MEXC_MAX_DAILY_NOTIONAL_USD`.

### `/root/obsidian/tldr/проекты/mexc/гайд/mexc-api-reference.md`

Documentation update after endpoint verification.

Exact change list:

- Add verified MEXC WebSocket endpoint and subscription message.
- Add private order cancel and order detail endpoint if confirmed.
- Add post-only limit order body example:

```json
{
  "symbol": "SOL_USDT",
  "price": 100.12,
  "vol": 1,
  "side": 1,
  "type": 5,
  "openType": 1,
  "positionId": 0,
  "externalOid": "hermes_limit_..."
}
```

### `/root/obsidian/tldr/проекты/mexc/гайд/plan-improvement-v1.md`

No required change. Keep it as the high-level roadmap. This file is the execution-level plan.

## 7. Risk Register

| Risk | Probability | Impact | Phase | Mitigation | Stop condition |
|---|---:|---:|---|---|---|
| MEXC WebSocket channel names differ from assumptions | Medium | Medium | P0 | Run standalone WS probe first; keep REST fallback enabled | WS has no valid bid/ask after 30 min of probing |
| `type=5` is not the correct post-only type | Medium | High | P0 | Test with smallest dry/live order; never fall back to market entry automatically | Any reject that indicates order type unsupported |
| Limit order does not fill and opportunity disappears | High | Medium | P0 | 2s fill timeout, cancel stale orders, record rejected opportunity | Fill rate below 30% after 50 signals |
| Maker fee is not actually 0% for selected pair/account | Medium | High | P1/P2 | Fee metadata monitor; env fee override; require net edge after fees | Known maker fee > configured allowed fee |
| Midpoint removal reduces signal count | Medium | Low | P1 | Expand pair universe before lowering threshold; measure raw vs cross signals | Signals/day below current baseline for 3 days |
| 30-50 WebSocket subscriptions overload connection | Medium | Medium | P1 | Batch subscriptions; reconnect with backoff; reduce to top 30 if unstable | WS reconnects more than 10 times/hour |
| Binance and MEXC symbols are not equivalent contracts | Medium | High | P1 | Use contract metadata and manual blocklist; exclude leveraged/meme/illiquid anomalies | Unexpected price ratio or contract size mismatch |
| SQLite writes block event loop | Low | Medium | P1 | Keep writes small; use `asyncio.to_thread` if latency appears | Signal loop lag > 1s |
| Account PnL endpoint shape unknown | Medium | Medium | P2 | Implement as optional monitor; fail open to existing risk rules but log unavailable | Account endpoint returns auth errors repeatedly |
| Cloudflare blocks REST despite `curl_cffi` | Low | High | All | Keep `impersonate="chrome131"`, browser headers, content-type guards; update `curl_cffi` | >50% REST calls fail for 10 min |
| systemd restarts during active position | Low | High | P0 | On startup, query open positions before opening new trades; close/manage existing positions | Startup detects unknown open position |
| API keys leaked through logs | Low | Critical | All | Never print config values; redact request headers; remove hardcoded Telegram token | Any secret appears in repo or journal |
| Account review / PK behavior triggered | High | High | P2 | Session pauses, pair cooldown, random skip, volume caps, stop near positive PnL | Manual review warning, futures disabled, or PnL above stop |
| Daily volume grows too fast | Medium | High | P2 | SQLite volume ledger and daily/total notional caps | Daily notional >= configured cap |
| PnL estimates diverge from exchange reality | Medium | Medium | P1 | Store expected and realized values separately; reconcile from order fills when endpoint confirmed | Difference >30% over 20 trades |

## 8. Success Metrics

### Operational metrics

| Metric | Current v7 | Phase 1 target | Phase 2 target | Phase 3/4 target | Source |
|---|---:|---:|---:|---:|---|
| Process uptime | manual/unknown | >99% daily | >99% daily | >99% daily | `systemctl status`, heartbeat |
| Restart recovery | none | <10s | <10s | <10s | `journalctl` |
| Heartbeat freshness | none | <10s | <10s | <10s | `/run/mexc-monitor/heartbeat.json` |
| MEXC price latency | ~1s REST | WS updates <250ms after message receive | same | same | internal timestamps |
| REST fallback rate | 100% | <20% of price updates | <10% | <10% | logs/heartbeat |

### Trading metrics

| Metric | Current v7 | Target | Measurement |
|---|---:|---:|---|
| Active pair count | 8 | 30-50 | startup logs, heartbeat |
| Entry threshold | 0.12% | 0.06% after fee/cross model | config log |
| Signals/day | 2-5 expected | 15-30 | SQLite `trades` status/reject rows and `/tmp/mexc-spread-alerts.log` |
| Accepted trades/day | unknown | 5-15 | SQLite `trades.status='closed'` |
| Limit fill rate | none | >50% after tuning | filled entries / limit attempts |
| Average raw edge | 0.12%+ midpoint | 0.06-0.15% cross | SQLite `raw_edge_entry_pct` |
| Average net edge | not modeled | >0.04% | SQLite `net_edge_entry_pct` |
| Slippage buffer breaches | not modeled | <20% trades | expected vs realized PnL |
| Daily realized PnL | not tracked | positive after fees over 7-day window | SQLite |
| Daily loss | capped in memory | never below `-DAILY_LOSS_LIMIT` | SQLite + risk logs |

### Account safety metrics

| Metric | Target | Measurement |
|---|---:|---|
| Daily notional volume | below `MEXC_MAX_DAILY_NOTIONAL_USD` | `volume_ledger` |
| Total notional volume | below `MEXC_MAX_TOTAL_NOTIONAL_USD` | `volume_ledger` |
| Same-pair repeat interval | >`PAIR_REPEAT_COOLDOWN_SEC` | SQLite trade timestamps |
| Session size | 3-8 trades before pause by default | risk logs |
| Trading when account PnL >= 0 | 0 trades | account monitor + SQLite |
| Known non-zero maker-fee pairs traded | 0 | pair disable log + SQLite |

## Execution Order

1. Implement Phase 1.1-1.2 first to make runtime safe and remove hardcoded Telegram secrets.
2. Implement Phase 1.7-1.9 next so every later change is supervised by `systemd` and heartbeat.
3. Implement Phase 1.3-1.4 limit orders in dry-run, then test with minimal live size only after order type is confirmed.
4. Implement Phase 1.5-1.6 MEXC WS with REST fallback.
5. Implement Phase 3.1-3.2 cross pricing and fee model before lowering threshold.
6. Implement Phase 2 pair expansion and only then set `MEXC_ENTRY_SPREAD=0.06`.
7. Implement Phase 3.3-3.5 SQLite and dynamic sizing.
8. Implement Phase 4 account behavior controls.

## Final Verification Checklist

Run before considering v8 ready:

```bash
python3 -m py_compile /root/obsidian/tldr/проекты/mexc/mexc-tg-monitor.py
cp /root/obsidian/tldr/проекты/mexc/mexc-tg-monitor.py /root/bin/mexc-tg-monitor.py
chmod +x /root/bin/mexc-tg-monitor.py
cmp -s /root/obsidian/tldr/проекты/mexc/mexc-tg-monitor.py /root/bin/mexc-tg-monitor.py
systemctl daemon-reload
systemctl restart mexc-monitor.service
sleep 10
systemctl status mexc-monitor.service --no-pager
cat /run/mexc-monitor/heartbeat.json
/root/bin/mexc-monitor-healthcheck.sh
journalctl -u mexc-monitor.service -n 120 --no-pager
sqlite3 /root/obsidian/tldr/проекты/mexc/data/mexc-trades.sqlite3 '.tables'
```

Required pass criteria:

- compile succeeds;
- deployed copy matches source;
- service is active;
- heartbeat age is under 20 seconds;
- logs show Binance WS and MEXC WS/fallback active;
- no secret values appear in logs;
- SQLite DB initializes;
- dry-run mode produces signals/rejections without private order placement.
