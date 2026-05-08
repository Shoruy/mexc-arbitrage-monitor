# MEXC-Binance Spread Monitor v8/v9: Codex Execution Plan

**Date:** 2026-05-08  
**Author:** Codex  
**Target repo:** `/root/obsidian/tldr/проекты/mexc`  
**Live script:** `/root/bin/mexc-tg-monitor.py`  
**Source script:** `/root/obsidian/tldr/проекты/mexc/mexc-tg-monitor.py`  
**Current state:** v7 single-file bot, Binance WebSocket, MEXC REST via `curl_cffi`, auto-trade enabled when `/root/.mexc-api/config.json` has working futures API keys.

## Executive Summary

The current bot can find obvious Binance-MEXC lag, but it is not yet a profitable production trading system. The biggest problems are not syntax or API migration anymore. The biggest problems are execution quality, fee reality, fill probability, and MEXC account-risk behavior.

The plan below is intentionally more conservative than the existing draft:

- Do not scale to 30-50 pairs until signal quality is logged in SQLite and websocket/fallback behavior is stable.
- Do not lower `ENTRY_SPREAD` from `0.12%` to `0.06%` until bid/ask cross-edge, fee model, and limit-fill statistics prove that the edge survives execution.
- Do not assume MEXC WebSocket or post-only order type from memory. Probe them and record the verified shapes in `mexc-api-reference.md`.
- Do not rely on Telegram as a trading loop. Telegram is notification only; the bot must make and record decisions locally within milliseconds.
- Do not optimize for "more trades" before proving that closed trades have positive expected value after fees, missed fills, stale books, and cancellations.

The profit path is:

1. Measure executable cross-edge, not midpoint spread.
2. Enter MEXC passively when Binance has already moved and MEXC has not.
3. Cancel quickly if the maker entry does not fill.
4. Exit by rule before lag collapses against the position.
5. Keep notional and behavior below account-review triggers.
6. Stop trading when the -PNL account is no longer economically useful.

## Brutal Reality Check

### What is realistic

- A small VPS Python bot can monitor 8-50 symbols and catch slow MEXC repricing events.
- `curl_cffi` with browser impersonation is a practical workaround for current Cloudflare issues.
- SQLite is enough for this strategy at current scale.
- `systemd` plus heartbeat is enough to keep the process alive.
- A 0.06-0.15% raw cross-edge can be profitable only if maker fees are truly zero and fills happen fast.

### What is not realistic

- Market orders on alt futures are unlikely to be profitable at `0.06-0.12%` edge after spread, taker fees, slippage, and rejection risk.
- 4-5 profitable API trades per minute is not realistic without triggering account-risk systems or paying with terrible fill quality.
- Telegram alerts are too slow for the actual entry decision.
- A generic "30-50 pairs" expansion is dangerous if it includes illiquid, mismatched, non-zero-fee, or contract-size oddities.
- Anti-PK randomization does not make a bot invisible. It only reduces the most obvious repeated patterns.
- Running from a blocked or mismatched jurisdiction can invalidate the entire setup regardless of code quality.

## Current System Assessment

### Working

- Binance `bookTicker` WebSocket updates for 8 symbols.
- MEXC public REST book ticker via `curl_cffi`.
- MEXC futures private REST migrated to `https://contract.mexc.com`.
- MEXC futures symbol conversion from `SOLUSDT` to `SOL_USDT`.
- Basic content-type guard for Cloudflare HTML responses.
- Telegram alert path.
- Basic risk limits: one active trade, max trades per day, daily loss limit.

### Dangerous or incomplete

- Entry decision uses midpoint spread, not executable bid/ask prices.
- Current PnL estimate is not a realized PnL estimate and can be directionally wrong.
- Entry function says "limit" in project docs, but v7 code uses `type: 1`, `price: 0`, which is effectively market-style execution unless MEXC treats it otherwise.
- No order fill verification before adding active trade state.
- No cancel path for unfilled limit entries.
- No durable trade log.
- No systemd service in the project plan has been actually created by this task.
- Hardcoded Telegram fallback token exists in the v7 dump and must be removed from code and git history if committed.
- No startup reconciliation of open positions. Restart during an open position can lose state.
- No fee verification per symbol/account.
- No account PnL monitor.

## Target Architecture

```text
                         ┌─────────────────────────────────────────┐
                         │ systemd: mexc-monitor.service            │
                         │ Restart=always, RuntimeDirectory, env    │
                         └───────────────────┬─────────────────────┘
                                             │
                                             v
┌──────────────────────────────────────────────────────────────────────────┐
│ /root/bin/mexc-tg-monitor.py                                             │
│ source of truth copied from repo after validation                         │
├──────────────────────────────────────────────────────────────────────────┤
│ ConfigLoader                                                              │
│ - env first, /root/.mexc-api/config.json second                           │
│ - no hardcoded Telegram/API secrets                                        │
│ - dry-run and signal-only gates                                            │
├──────────────────────────────────────────────────────────────────────────┤
│ Market Data                                                               │
│ ┌──────────────────────┐  ┌──────────────────────┐  ┌──────────────────┐ │
│ │ BinanceWsClient       │  │ MexcWsClient          │  │ MexcRestFallback │ │
│ │ wss://stream...       │  │ wss://contract...     │  │ curl_cffi only   │ │
│ │ @bookTicker           │  │ verified channel only │  │ stale symbols    │ │
│ └──────────┬───────────┘  └──────────┬───────────┘  └────────┬─────────┘ │
│            └──────────────┬──────────┴───────────────────────┘           │
│                           v                                              │
│                    PriceBook                                             │
│ - bid, ask, source, exchange_ts, local_ts, staleness                       │
├──────────────────────────────────────────────────────────────────────────┤
│ SignalEngine                                                              │
│ - bid/ask cross-edge only                                                 │
│ - fee-adjusted net edge                                                   │
│ - liquidity/depth validation                                              │
│ - BTC/ETH correlation confirmation where useful                           │
├──────────────────────────────────────────────────────────────────────────┤
│ RiskManager                                                               │
│ - max concurrent, max daily trades, loss limit                             │
│ - notional caps, pair cooldowns, session pauses                            │
│ - account PnL stop/throttle                                                │
│ - startup open-position reconciliation                                     │
├──────────────────────────────────────────────────────────────────────────┤
│ ExecutionEngine                                                           │
│ - set leverage once per symbol/openType                                    │
│ - post-only/passive limit entry                                            │
│ - fill timeout and cancel                                                  │
│ - close position with explicit close logic                                 │
│ - no automatic market fallback for entries                                 │
├──────────────────────────────────────────────────────────────────────────┤
│ TradeStore: SQLite                                                        │
│ /root/obsidian/tldr/проекты/mexc/data/mexc-trades.sqlite3                 │
│ - signals, orders, trades, fills, volume ledger, pair stats                │
├──────────────────────────────────────────────────────────────────────────┤
│ HealthReporter                                                            │
│ /run/mexc-monitor/heartbeat.json                                           │
│ Telegram: notification only                                                │
└──────────────────────────────────────────────────────────────────────────┘
```

## P0: Stop Losing Money From Bad State

P0 must be implemented before any threshold reduction or pair expansion. Estimated total: **7-11 hours**.

### P0.1 Remove secrets from code and add runtime gates

**Estimate:** 30-45 min  
**Files:** `mexc-tg-monitor.py`, optional `.env.example` if added later  
**Why it matters for profit:** Prevents accidental live trading during tests and avoids credential leaks that can destroy the account.

Exact code changes:

- Add:

```python
BOT_VERSION = "v8-p0"
DRY_RUN = os.getenv("MEXC_DRY_RUN", "1") == "1"
AUTO_TRADE_ENABLED = os.getenv("MEXC_AUTO_TRADE", "0") == "1"
SIGNAL_ONLY = os.getenv("MEXC_SIGNAL_ONLY", "0") == "1"
MEXC_WS_ENABLED = os.getenv("MEXC_WS_ENABLED", "1") == "1"
MEXC_REST_FALLBACK_ENABLED = os.getenv("MEXC_REST_FALLBACK_ENABLED", "1") == "1"
STATE_DIR = os.getenv("MEXC_STATE_DIR", "/root/obsidian/tldr/проекты/mexc/data")
HEARTBEAT_PATH = os.getenv("MEXC_HEARTBEAT_PATH", "/run/mexc-monitor/heartbeat.json")
```

- Replace Telegram defaults with env-only:

```python
TG_BOT_TOKEN = os.getenv("MEXC_TG_BOT_TOKEN", "")
TG_CHAT_ID = os.getenv("MEXC_TG_CHAT_ID", "")
TG_ENABLED = bool(TG_BOT_TOKEN and TG_CHAT_ID)
```

- Replace:

```python
AUTO_MODE = bool(API_KEY and API_SECRET)
```

with:

```python
AUTO_MODE = bool(API_KEY and API_SECRET and AUTO_TRADE_ENABLED and not DRY_RUN and not SIGNAL_ONLY)
```

- Log only booleans:

```text
api_key_set=True api_secret_set=True tg_enabled=True auto_mode=False dry_run=True
```

Validation:

```bash
python3 -m py_compile /root/obsidian/tldr/проекты/mexc/mexc-tg-monitor.py
rg -n "875402|AAGg|1371329042|api_secret|api_key" /root/obsidian/tldr/проекты/mexc/mexc-tg-monitor.py
MEXC_DRY_RUN=1 MEXC_AUTO_TRADE=1 python3 /root/obsidian/tldr/проекты/mexc/mexc-tg-monitor.py
```

Pass criteria:

- No hardcoded Telegram token/chat ID.
- API config keys may be referenced by name, but secret values are never printed.
- `AUTO_MODE=False` when `MEXC_DRY_RUN=1`.

### P0.2 Normalize symbols and REST transport

**Estimate:** 45-60 min  
**Files:** `mexc-tg-monitor.py`  
**Why it matters for profit:** Prevents silent wrong-symbol requests and preserves the Cloudflare workaround.

Add helpers:

```python
def to_mexc_spot_symbol(symbol: str) -> str:
    return symbol.upper().replace("_", "")

def to_mexc_contract_symbol(symbol: str) -> str:
    spot = to_mexc_spot_symbol(symbol)
    if not spot.endswith("USDT"):
        raise ValueError(f"unsupported quote symbol: {symbol}")
    return f"{spot[:-4]}_{spot[-4:]}"

def sym_key(symbol: str) -> str:
    return to_mexc_spot_symbol(symbol).lower()
```

Replace every manual `msym[:-4] + "_" + msym[-4:]` with `to_mexc_contract_symbol(msym)`.

Use one `curl_cffi` wrapper for MEXC REST:

```python
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

Add JSON guard:

```python
def parse_json_response(resp, context: str):
    ct = resp.headers.get("Content-Type", "")
    if "json" not in ct.lower():
        log.warning("%s non-json status=%s content_type=%s body=%r",
                    context, resp.status_code, ct, resp.text[:200])
        return None
    try:
        return resp.json()
    except Exception as e:
        log.warning("%s invalid-json status=%s err=%s body=%r",
                    context, resp.status_code, e, resp.text[:200])
        return None
```

Validation:

```bash
python3 -m py_compile /root/obsidian/tldr/проекты/mexc/mexc-tg-monitor.py
rg -n ":-4|\\+ \"_\"|aiohttp.ClientSession\\(\\).*mexc|requests\\.get\\(.*mexc" /root/obsidian/tldr/проекты/mexc/mexc-tg-monitor.py
```

Pass criteria:

- Symbol conversion is centralized.
- MEXC REST calls use `_curl`.
- Non-JSON responses do not crash the process.

### P0.3 Add SQLite before changing trading logic

**Estimate:** 1.5-2 hours  
**Files:** `mexc-tg-monitor.py`; runtime DB under `data/mexc-trades.sqlite3`  
**Why it matters for profit:** Without durable rejected-signal and fill data, the strategy cannot be optimized.

Schema:

```sql
CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    symbol TEXT NOT NULL,
    direction TEXT NOT NULL,
    binance_bid REAL NOT NULL,
    binance_ask REAL NOT NULL,
    mexc_bid REAL NOT NULL,
    mexc_ask REAL NOT NULL,
    raw_edge_pct REAL NOT NULL,
    net_edge_pct REAL,
    liquidity_usd REAL,
    mexc_spread_pct REAL,
    decision TEXT NOT NULL,
    reject_reason TEXT,
    meta_json TEXT
);

CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_id INTEGER,
    ts TEXT NOT NULL,
    symbol TEXT NOT NULL,
    direction TEXT NOT NULL,
    order_id TEXT,
    external_oid TEXT,
    side INTEGER,
    order_type INTEGER,
    price REAL,
    qty REAL,
    status TEXT NOT NULL,
    raw_json TEXT,
    FOREIGN KEY(signal_id) REFERENCES signals(id)
);

CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_id INTEGER,
    opened_at TEXT NOT NULL,
    closed_at TEXT,
    symbol TEXT NOT NULL,
    direction TEXT NOT NULL,
    qty REAL NOT NULL,
    leverage REAL NOT NULL,
    margin_usd REAL NOT NULL,
    notional_usd REAL NOT NULL,
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
    meta_json TEXT,
    FOREIGN KEY(signal_id) REFERENCES signals(id)
);

CREATE TABLE IF NOT EXISTS volume_ledger (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    symbol TEXT NOT NULL,
    event TEXT NOT NULL,
    notional_usd REAL NOT NULL,
    trade_id INTEGER,
    meta_json TEXT,
    FOREIGN KEY(trade_id) REFERENCES trades(id)
);

CREATE INDEX IF NOT EXISTS idx_signals_ts ON signals(ts);
CREATE INDEX IF NOT EXISTS idx_signals_symbol ON signals(symbol);
CREATE INDEX IF NOT EXISTS idx_trades_status ON trades(status);
CREATE INDEX IF NOT EXISTS idx_volume_ts ON volume_ledger(ts);
```

Implementation notes:

- Use simple synchronous `sqlite3` writes at first.
- Set `PRAGMA journal_mode=WAL;`.
- Wrap DB writes in short functions and catch/log exceptions so DB failure does not leave an unmanaged open position.
- Store raw exchange JSON in `orders.raw_json`, but never store request headers or API secrets.

Validation:

```bash
MEXC_DRY_RUN=1 python3 /root/obsidian/tldr/проекты/mexc/mexc-tg-monitor.py
sqlite3 /root/obsidian/tldr/проекты/mexc/data/mexc-trades.sqlite3 '.tables'
sqlite3 /root/obsidian/tldr/проекты/mexc/data/mexc-trades.sqlite3 '.schema signals'
```

Pass criteria:

- DB initializes on startup.
- Every signal threshold crossing becomes a `signals` row with `accepted`, `rejected`, `dry_run`, or `signal_only`.

### P0.4 Replace midpoint signals with executable cross-edge

**Estimate:** 1-1.5 hours  
**Files:** `mexc-tg-monitor.py`  
**Why it matters for profit:** Midpoint edge is fake edge. Only cross-edge has any chance of paying after execution.

Add:

```python
def compute_cross_edges(bp: dict, mp: dict) -> dict:
    b_bid, b_ask = float(bp["bid"]), float(bp["ask"])
    m_bid, m_ask = float(mp["bid"]), float(mp["ask"])
    return {
        "LONG": {
            "raw_edge_pct": (b_bid - m_ask) / m_ask * 100,
            "binance_ref": b_bid,
            "mexc_entry_cross": m_ask,
            "mexc_passive_price": m_bid,
        },
        "SHORT": {
            "raw_edge_pct": (m_bid - b_ask) / b_ask * 100,
            "binance_ref": b_ask,
            "mexc_entry_cross": m_bid,
            "mexc_passive_price": m_ask,
        },
    }
```

Interpretation:

- `LONG`: Binance bid is already above MEXC ask. Buy/long MEXC if MEXC is lagging below Binance.
- `SHORT`: MEXC bid is above Binance ask. Sell/short MEXC if MEXC is lagging above where Binance implies it should be.

Add fee model:

```python
MEXC_MAKER_FEE_PCT = float(os.getenv("MEXC_MAKER_FEE_PCT", "0.0"))
MEXC_TAKER_FEE_PCT = float(os.getenv("MEXC_TAKER_FEE_PCT", "0.01"))
ENTRY_FEE_MODE = os.getenv("MEXC_ENTRY_FEE_MODE", "maker")
EXIT_FEE_MODE = os.getenv("MEXC_EXIT_FEE_MODE", "maker")
SLIPPAGE_BUFFER_PCT = float(os.getenv("MEXC_SLIPPAGE_BUFFER_PCT", "0.01"))
MIN_NET_EDGE_PCT = float(os.getenv("MEXC_MIN_NET_EDGE_PCT", "0.04"))

def fee_pct(mode: str) -> float:
    return MEXC_MAKER_FEE_PCT if mode == "maker" else MEXC_TAKER_FEE_PCT

def compute_net_edge_pct(raw_edge_pct: float) -> float:
    return raw_edge_pct - fee_pct(ENTRY_FEE_MODE) - fee_pct(EXIT_FEE_MODE) - SLIPPAGE_BUFFER_PCT
```

Entry rule:

```python
edges = compute_cross_edges(bp, mp)
direction, edge = max(edges.items(), key=lambda kv: kv[1]["raw_edge_pct"])
net_edge_pct = compute_net_edge_pct(edge["raw_edge_pct"])
if edge["raw_edge_pct"] < ENTRY_SPREAD:
    continue
if net_edge_pct < MIN_NET_EDGE_PCT:
    record rejected signal
    continue
```

Keep `ENTRY_SPREAD=0.12` until P1 metrics say otherwise.

Validation:

```bash
python3 - <<'PY'
def compute_cross_edges(bp, mp):
    b_bid, b_ask = bp["bid"], bp["ask"]
    m_bid, m_ask = mp["bid"], mp["ask"]
    return {
        "LONG": {"raw_edge_pct": (b_bid - m_ask) / m_ask * 100},
        "SHORT": {"raw_edge_pct": (m_bid - b_ask) / b_ask * 100},
    }
bp = {"bid": 101.0, "ask": 101.1}
mp = {"bid": 100.0, "ask": 100.1}
assert compute_cross_edges(bp, mp)["LONG"]["raw_edge_pct"] > 0
assert compute_cross_edges(bp, mp)["SHORT"]["raw_edge_pct"] < 0
bp = {"bid": 99.9, "ask": 100.0}
mp = {"bid": 101.0, "ask": 101.1}
assert compute_cross_edges(bp, mp)["SHORT"]["raw_edge_pct"] > 0
print("ok")
PY
```

Pass criteria:

- Logs and Telegram show raw edge and net edge.
- No entry uses midpoint spread.

### P0.5 Implement passive limit entry with fill timeout

**Estimate:** 2-3 hours  
**Files:** `mexc-tg-monitor.py`, `гайд/mexc-api-reference.md` after verification  
**Why it matters for profit:** The strategy depends on maker/passive entry. Market entry likely destroys edge.

First run endpoint probes with current API keys in dry mode or smallest possible live test. Verify:

- post-only or maker-only order type;
- cancel endpoint;
- order detail endpoint;
- response shape for order status and filled quantity.

Candidate endpoints:

```text
POST https://contract.mexc.com/api/v1/private/order/create
POST https://contract.mexc.com/api/v1/private/order/cancel
GET  https://contract.mexc.com/api/v1/private/order/get/{orderId}
GET  https://contract.mexc.com/api/v1/private/order/list/open_orders/{symbol}
```

Candidate order body:

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

Implementation:

- Rename existing `mexc_place_order` to `mexc_place_market_order`.
- Do not call market entry from normal signal path.
- Add:

```python
LIMIT_POST_ONLY_TYPE = int(os.getenv("MEXC_LIMIT_POST_ONLY_TYPE", "5"))
ENTRY_LIMIT_OFFSET_BPS = float(os.getenv("MEXC_ENTRY_LIMIT_OFFSET_BPS", "0.5"))
ORDER_FILL_TIMEOUT_SEC = float(os.getenv("MEXC_ORDER_FILL_TIMEOUT_SEC", "2.0"))
```

- Add `mexc_get_contract_meta(sym_key)` cache from:

```text
GET https://contract.mexc.com/api/v1/contract/detail?symbol=SOL_USDT
```

Required metadata:

```python
{
    "contractSize": float(...),
    "priceUnit": float(...),
    "volUnit": float(...),
    "minVol": float(...),
    "makerFeeRate": optional,
    "takerFeeRate": optional,
}
```

- Add:

```python
def quantize_to_step(value: float, step: float | None, fallback_digits: int = 8) -> float:
    if step and step > 0:
        return round(round(value / step) * step, 12)
    return float(f"{value:.{fallback_digits}f}")
```

- Entry price:

```python
if direction == "LONG":
    side = 1
    price = passive_price * (1 - ENTRY_LIMIT_OFFSET_BPS / 10000)
else:
    side = 3
    price = passive_price * (1 + ENTRY_LIMIT_OFFSET_BPS / 10000)
```

Important: `passive_price` comes from `compute_cross_edges()[direction]["mexc_passive_price"]`. For `LONG`, that is MEXC bid. For `SHORT`, that is MEXC ask. This avoids crossing the spread.

- After order create:
  - insert `orders` row;
  - poll order detail until filled, rejected, canceled, or timeout;
  - cancel on timeout;
  - add `active_trades` only after confirmed fill or confirmed position.

Fail-closed rule:

```text
If post-only type is rejected or fill status cannot be determined, bot switches that event to SIGNAL_ONLY. It must not fall back to market entry.
```

Validation:

```bash
python3 -m py_compile /root/obsidian/tldr/проекты/mexc/mexc-tg-monitor.py
rg -n "mexc_place_market_order|mexc_place_limit_entry|LIMIT_POST_ONLY_TYPE|type.*1" /root/obsidian/tldr/проекты/mexc/mexc-tg-monitor.py
```

Pass criteria:

- Normal entry path calls `mexc_place_limit_entry`.
- Market helper remains only for explicit emergency close paths, not entry.
- Unfilled entries are canceled and recorded as `timeout_cancel`.

### P0.6 Startup position reconciliation and close safety

**Estimate:** 1-1.5 hours  
**Files:** `mexc-tg-monitor.py`  
**Why it matters for profit:** A reboot during an open position can turn a scalp into an unmanaged directional bet.

Probe/implement:

```text
GET https://contract.mexc.com/api/v1/private/position/open_positions
```

Startup rules:

- If no open MEXC positions: normal start.
- If open positions exist and DB has matching `status='open'`: restore `active_trades`.
- If open positions exist and DB has no matching open trade:
  - set `TRADING_PAUSED_REASON="unknown open position on startup"`;
  - send Telegram;
  - do not open new trades;
  - optionally close only if `MEXC_CLOSE_UNKNOWN_ON_STARTUP=1`.

Close logic:

- Keep `close_all` only as a last resort.
- Prefer symbol-specific close if endpoint supports it.
- Record exit order and close reason in SQLite.

Validation:

```bash
MEXC_DRY_RUN=1 python3 /root/obsidian/tldr/проекты/mexc/mexc-tg-monitor.py
sqlite3 /root/obsidian/tldr/проекты/mexc/data/mexc-trades.sqlite3 \
  "select status,count(*) from trades group by status;"
```

Pass criteria:

- Startup logs explicitly say whether open position reconciliation succeeded, was unavailable, or paused trading.

### P0.7 Heartbeat and systemd deployment

**Estimate:** 1-1.5 hours  
**Files:** `mexc-tg-monitor.py`, `/etc/systemd/system/mexc-monitor.service`, `/root/bin/mexc-monitor-healthcheck.sh`  
**Why it matters for profit:** The bot must recover from process crashes and VPS reboot, but not silently trade while unhealthy.

Add heartbeat:

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
            "trading_paused_reason": TRADING_PAUSED_REASON,
        }
        tmp = HEARTBEAT_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(payload, f)
        os.replace(tmp, HEARTBEAT_PATH)
        await asyncio.sleep(5)
```

Systemd unit:

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
Environment=MEXC_AUTO_TRADE=0
Environment=MEXC_DRY_RUN=1
Environment=MEXC_SIGNAL_ONLY=0
Environment=MEXC_WS_ENABLED=1
Environment=MEXC_REST_FALLBACK_ENABLED=1
Environment=MEXC_ENTRY_SPREAD=0.12
Environment=MEXC_MIN_NET_EDGE_PCT=0.08
Environment=MEXC_HEARTBEAT_PATH=/run/mexc-monitor/heartbeat.json
RuntimeDirectory=mexc-monitor
RuntimeDirectoryMode=0755
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

Important: default service should start in dry-run. Flip `MEXC_DRY_RUN=0` and `MEXC_AUTO_TRADE=1` only after live order probes pass.

Health-check script:

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

Validation:

```bash
python3 -m py_compile /root/obsidian/tldr/проекты/mexc/mexc-tg-monitor.py
cp /root/obsidian/tldr/проекты/mexc/mexc-tg-monitor.py /root/bin/mexc-tg-monitor.py
chmod +x /root/bin/mexc-tg-monitor.py
systemctl daemon-reload
systemctl enable mexc-monitor.service
systemctl restart mexc-monitor.service
sleep 10
systemctl status mexc-monitor.service --no-pager
cat /run/mexc-monitor/heartbeat.json
/root/bin/mexc-monitor-healthcheck.sh
journalctl -u mexc-monitor.service -n 120 --no-pager
```

Pass criteria:

- Service active.
- Heartbeat updates every 5 seconds.
- Default service mode is dry-run until explicitly changed.

## P1: Improve Edge Capture

P1 starts only after P0 logs at least one full dry-run session and no startup/runtime crashes. Estimated total: **8-14 hours**.

### P1.1 Verify and integrate MEXC WebSocket

**Estimate:** 2-3 hours  
**Files:** `mexc-tg-monitor.py`, `гайд/mexc-api-reference.md`  
**Why it matters for profit:** REST polling at 1 second can miss most of a 1-2 second lag window.

Probe first:

```bash
python3 - <<'PY'
import asyncio, json, websockets

async def try_sub(payload):
    print("TRY", payload)
    async with websockets.connect("wss://contract.mexc.com/edge", ping_interval=20) as ws:
        await ws.send(json.dumps(payload))
        for _ in range(8):
            print(await asyncio.wait_for(ws.recv(), timeout=10))

async def main():
    candidates = [
        {"method": "sub.ticker", "param": {"symbol": "SOL_USDT"}},
        {"method": "sub.depth", "param": {"symbol": "SOL_USDT"}},
        {"method": "sub.depth.full", "param": {"symbol": "SOL_USDT", "limit": 5}},
    ]
    for c in candidates:
        try:
            await try_sub(c)
        except Exception as e:
            print("ERR", e)

asyncio.run(main())
PY
```

Implement only the verified channel. Do not carry three speculative parsers forever; keep defensive parsing, but document the actual observed message shape.

Client requirements:

- endpoint: `wss://contract.mexc.com/edge`;
- per-symbol subscription for active MEXC futures symbols;
- ping/pong handling;
- reconnect with exponential backoff capped at 30 sec;
- update `mexc_prices[sym] = {"bid": ..., "ask": ..., "source": "ws", "ts": time.time()}`;
- if WebSocket does not provide bid/ask, use depth top level.

Fallback:

- Rename `mexc_poller` to `mexc_rest_fallback_poller`.
- Poll only stale symbols:

```python
MEXC_WS_STALE_SEC = float(os.getenv("MEXC_WS_STALE_SEC", "5"))

def mexc_price_is_stale(sym: str) -> bool:
    p = mexc_prices.get(sym)
    return not p or time.time() - p.get("ts", 0) > MEXC_WS_STALE_SEC
```

Validation:

```bash
MEXC_DRY_RUN=1 MEXC_WS_ENABLED=1 MEXC_REST_FALLBACK_ENABLED=1 \
  python3 /root/obsidian/tldr/проекты/mexc/mexc-tg-monitor.py
```

Pass criteria:

- MEXC WS produces bid/ask for bootstrap pairs within 10 seconds, or logs a clear fallback reason.
- REST fallback rate is visible in logs or heartbeat.
- No crash on one rejected subscription.

### P1.2 Dynamic pair universe, but only with hard filters

**Estimate:** 2.5-4 hours  
**Files:** `mexc-tg-monitor.py`  
**Why it matters for profit:** More pairs increase opportunities, but bad pairs create fake edge and account risk.

Keep bootstrap:

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

Add:

```python
TARGET_PAIR_MIN = int(os.getenv("MEXC_TARGET_PAIR_MIN", "12"))
TARGET_PAIR_MAX = int(os.getenv("MEXC_TARGET_PAIR_MAX", "30"))
PAIR_REFRESH_SEC = int(os.getenv("MEXC_PAIR_REFRESH_SEC", "3600"))
MIN_BINANCE_24H_QUOTE_VOLUME = float(os.getenv("MEXC_MIN_BINANCE_24H_QUOTE_VOLUME", "1000000"))
MIN_MEXC_DEPTH_USD = float(os.getenv("MEXC_MIN_MEXC_DEPTH_USD", "1500"))
MAX_MEXC_INTERNAL_SPREAD_PCT = float(os.getenv("MEXC_MAX_MEXC_INTERNAL_SPREAD_PCT", "0.04"))
MANUAL_SYMBOL_BLOCKLIST = set(os.getenv("MEXC_SYMBOL_BLOCKLIST", "").upper().split(",")) - {""}
```

Use endpoints:

```text
GET https://api.binance.com/api/v3/exchangeInfo
GET https://api.binance.com/api/v3/ticker/24hr
GET https://contract.mexc.com/api/v1/contract/detail
GET https://api.mexc.com/api/v3/depth?symbol=SOLUSDT&limit=20
```

Filter:

- Binance status `TRADING`;
- quote asset `USDT`;
- MEXC contract status tradable/open if field exists;
- Binance 24h quote volume above threshold;
- MEXC spot/public depth above threshold;
- MEXC internal spread below threshold;
- contract size and price unit parse cleanly;
- symbol not in blocklist;
- exclude obvious leveraged tokens or non-equivalent symbols.

Scoring:

```python
score = (
    min(binance_quote_volume / 1_000_000, 20)
    + min(mexc_depth_usd / 1_000, 20)
    - (mexc_internal_spread_pct * 100)
    + min(recent_profitable_signal_count, 10)
)
```

Conservative rollout:

- Week 1: target max 12-20 pairs.
- Increase to 30 only if WS reconnects <5/hour and rejected-signal quality is understood.
- 50 pairs is P2, not P1, unless metrics prove stability.

Validation:

```bash
MEXC_DRY_RUN=1 MEXC_TARGET_PAIR_MAX=12 python3 /root/obsidian/tldr/проекты/mexc/mexc-tg-monitor.py
sqlite3 /root/obsidian/tldr/проекты/mexc/data/mexc-trades.sqlite3 \
  "select symbol, decision, reject_reason, count(*) from signals group by 1,2,3 order by 4 desc limit 20;"
```

Pass criteria:

- Pair selector logs selected symbols and top reject reasons.
- WebSocket clients reconnect/resubscribe when pair generation changes.

### P1.3 Liquidity and order-book hole filter

**Estimate:** 1.5-2 hours  
**Files:** `mexc-tg-monitor.py`  
**Why it matters for profit:** The guide explicitly says holes in MEXC book kill the trade.

Replace current simple depth sum with side-specific executable liquidity:

```python
def analyze_depth(levels: list[tuple[float, float]], target_notional_usd: float) -> dict:
    cum = 0.0
    first_price = levels[0][0]
    worst_price = first_price
    gaps = []
    prev = first_price
    for price, qty in levels:
        gap_pct = abs(price - prev) / prev * 100 if prev else 0
        if gap_pct > 0.03:
            gaps.append(gap_pct)
        cum += price * qty
        worst_price = price
        if cum >= target_notional_usd:
            break
        prev = price
    impact_pct = abs(worst_price - first_price) / first_price * 100 if first_price else 999
    return {"depth_usd": cum, "impact_pct": impact_pct, "max_gap_pct": max(gaps or [0])}
```

Rules:

- For `LONG`, inspect asks if crossing, but also inspect bid queue depth for passive fill.
- For `SHORT`, inspect bids if crossing, but also inspect ask queue depth for passive fill.
- Reject if:
  - internal MEXC spread too wide;
  - target depth not available;
  - impact exceeds `0.03-0.05%`;
  - max gap exceeds `0.03%`.

Validation:

```bash
MEXC_DRY_RUN=1 python3 /root/obsidian/tldr/проекты/mexc/mexc-tg-monitor.py
sqlite3 /root/obsidian/tldr/проекты/mexc/data/mexc-trades.sqlite3 \
  "select reject_reason,count(*) from signals where decision='rejected' group by 1 order by 2 desc;"
```

Pass criteria:

- Liquidity reject reasons distinguish low depth, wide spread, and book gaps.

### P1.4 Dynamic sizing based on edge, liquidity, and loss streak

**Estimate:** 1-1.5 hours  
**Files:** `mexc-tg-monitor.py`  
**Why it matters for profit:** Fixed size ignores depth and account state. Too large triggers slippage/account risk; too small may not matter.

Add:

```python
ACCOUNT_EQUITY_USD = float(os.getenv("MEXC_ACCOUNT_EQUITY_USD", "30"))
MIN_TRADE_MARGIN_USD = float(os.getenv("MEXC_MIN_TRADE_MARGIN_USD", "5"))
MAX_TRADE_MARGIN_USD = float(os.getenv("MEXC_MAX_TRADE_MARGIN_USD", "12"))
BASE_RISK_FRACTION = float(os.getenv("MEXC_BASE_RISK_FRACTION", "0.25"))
MAX_DEPTH_USAGE_PCT = float(os.getenv("MEXC_MAX_DEPTH_USAGE_PCT", "3"))
LOSS_SIZE_REDUCTION_FACTOR = float(os.getenv("MEXC_LOSS_SIZE_REDUCTION_FACTOR", "0.5"))
```

Function:

```python
def compute_trade_margin_usd(sym: str, net_edge_pct: float, depth_usd: float) -> float:
    base = ACCOUNT_EQUITY_USD * BASE_RISK_FRACTION
    depth_cap_margin = (depth_usd * MAX_DEPTH_USAGE_PCT / 100) / TRADE_LEVERAGE
    edge_factor = min(max(net_edge_pct / 0.08, 0.5), 1.5)
    loss_factor = LOSS_SIZE_REDUCTION_FACTOR if _consecutive_losses >= 2 else 1.0
    jitter = random.uniform(0.85, 1.15)
    amount = base * edge_factor * loss_factor * jitter
    amount = min(amount, depth_cap_margin, MAX_TRADE_MARGIN_USD)
    amount = max(amount, MIN_TRADE_MARGIN_USD)
    return round(amount, 2)
```

Qty calculation:

```python
qty = int((margin_usd * TRADE_LEVERAGE) / (price * contract_size))
qty = max(qty, int(min_vol))
qty = quantize_to_step(qty, vol_unit, fallback_digits=0)
```

Validation:

```bash
MEXC_DRY_RUN=1 MEXC_ACCOUNT_EQUITY_USD=30 python3 /root/obsidian/tldr/проекты/mexc/mexc-tg-monitor.py
```

Pass criteria:

- Logs show margin, leverage, notional, contract size, qty, and depth cap for each accepted signal.

### P1.5 Lower threshold only by experiment

**Estimate:** 1 hour config/testing after P1.1-P1.4  
**Files:** systemd env, `mexc-tg-monitor.py` only if defaults change  
**Why it matters for profit:** Lower threshold increases signal count but can flip expectancy negative.

Do not jump directly to live `0.06%`. Run this sequence:

1. Dry-run 24h at `ENTRY_SPREAD=0.12`, collect cross-edge signals and fill simulations.
2. Dry-run 24h at `ENTRY_SPREAD=0.09`.
3. Dry-run 24h at `ENTRY_SPREAD=0.06`.
4. Live minimum size only if `net_edge_pct` and fill/cancel stats justify it.

Success gate for lowering:

```sql
-- enough candidates
select count(*) from signals
where raw_edge_pct >= 0.06 and net_edge_pct >= 0.04;

-- reject quality
select reject_reason, count(*) from signals
where raw_edge_pct >= 0.06 group by reject_reason;
```

Live threshold should be:

```ini
Environment=MEXC_ENTRY_SPREAD=0.09
Environment=MEXC_MIN_NET_EDGE_PCT=0.05
```

Only move to:

```ini
Environment=MEXC_ENTRY_SPREAD=0.06
Environment=MEXC_MIN_NET_EDGE_PCT=0.04
```

after at least 50 accepted live attempts show positive expected value.

## P2: Account Survival and Profit Extraction

P2 is where the bot becomes account-aware. Estimated total: **6-10 hours**.

### P2.1 Account PnL and equity monitor

**Estimate:** 2-3 hours  
**Files:** `mexc-tg-monitor.py`, `гайд/mexc-api-reference.md`  
**Why it matters for profit:** The strategy is tied to -PNL account economics. Continuing after the account approaches positive PnL increases risk.

Probe:

```text
GET https://contract.mexc.com/api/v1/private/account/assets
GET https://contract.mexc.com/api/v1/private/account/asset/USDT
GET https://contract.mexc.com/api/v1/private/position/open_positions
GET https://contract.mexc.com/api/v1/private/position/history_positions
```

Add:

```python
NEGATIVE_PNL_TARGET_USD = float(os.getenv("MEXC_NEGATIVE_PNL_TARGET_USD", "-200"))
STOP_IF_ACCOUNT_PNL_ABOVE = float(os.getenv("MEXC_STOP_IF_ACCOUNT_PNL_ABOVE", "0"))
THROTTLE_IF_ACCOUNT_PNL_ABOVE = float(os.getenv("MEXC_THROTTLE_IF_ACCOUNT_PNL_ABOVE", "-50"))
ACCOUNT_MONITOR_SEC = int(os.getenv("MEXC_ACCOUNT_MONITOR_SEC", "60"))
account_state = {"pnl_usd": None, "equity_usd": None, "updated_at": 0, "raw_json": None}
```

Rules:

- If account PnL is unknown, keep conservative limits.
- If account PnL >= `STOP_IF_ACCOUNT_PNL_ABOVE`, pause new entries.
- If account PnL >= `THROTTLE_IF_ACCOUNT_PNL_ABOVE`, reduce max daily trades and require higher net edge.
- If equity is below minimum margin, pause.

Validation:

```bash
MEXC_DRY_RUN=1 python3 /root/obsidian/tldr/проекты/mexc/mexc-tg-monitor.py
```

Pass criteria:

- Account endpoint success or explicit unavailable state.
- Bot never crashes on unexpected account JSON.

### P2.2 Notional and behavior caps

**Estimate:** 1.5-2 hours  
**Files:** `mexc-tg-monitor.py`  
**Why it matters for profit:** Account review risk is partly volume and pattern based.

Add:

```python
MAX_DAILY_NOTIONAL_USD = float(os.getenv("MEXC_MAX_DAILY_NOTIONAL_USD", "5000"))
MAX_TOTAL_NOTIONAL_USD = float(os.getenv("MEXC_MAX_TOTAL_NOTIONAL_USD", "200000"))
VOLUME_THROTTLE_AT_PCT = float(os.getenv("MEXC_VOLUME_THROTTLE_AT_PCT", "80"))
PAIR_REPEAT_COOLDOWN_SEC = float(os.getenv("MEXC_PAIR_REPEAT_COOLDOWN_SEC", "300"))
```

Insert volume rows on every filled entry and exit:

```python
notional_usd = margin_usd * TRADE_LEVERAGE
```

Risk rules:

- reject if daily notional cap reached;
- reject if total notional cap reached;
- if above 80% daily cap, increase `MIN_NET_EDGE_PCT` by 50%;
- reject repeated same-pair entries inside cooldown.

Validation:

```bash
sqlite3 /root/obsidian/tldr/проекты/mexc/data/mexc-trades.sqlite3 \
  "select date(ts), sum(notional_usd) from volume_ledger group by 1;"
```

Pass criteria:

- Risk logs include notional utilization.

### P2.3 Human-like pacing without pretending it solves detection

**Estimate:** 1-1.5 hours  
**Files:** `mexc-tg-monitor.py`  
**Why it matters for profit:** Reduces obvious repetitive patterns, but should not be treated as protection from MEXC risk control.

Add:

```python
HUMAN_DELAY_MIN_SEC = float(os.getenv("MEXC_HUMAN_DELAY_MIN_SEC", "0.3"))
HUMAN_DELAY_MAX_SEC = float(os.getenv("MEXC_HUMAN_DELAY_MAX_SEC", "2.5"))
RANDOM_SKIP_PROBABILITY = float(os.getenv("MEXC_RANDOM_SKIP_PROBABILITY", "0.05"))
SESSION_TRADE_MIN = int(os.getenv("MEXC_SESSION_TRADE_MIN", "3"))
SESSION_TRADE_MAX = int(os.getenv("MEXC_SESSION_TRADE_MAX", "8"))
SESSION_PAUSE_MIN_SEC = int(os.getenv("MEXC_SESSION_PAUSE_MIN_SEC", "180"))
SESSION_PAUSE_MAX_SEC = int(os.getenv("MEXC_SESSION_PAUSE_MAX_SEC", "900"))
```

Rules:

- Random skip only after signal is recorded, so skipped opportunities can be analyzed.
- Delay must happen before order placement, but revalidate edge after delay. If edge disappeared, cancel signal before order.
- Session pauses only apply to new entries, never exits.

Validation:

```bash
MEXC_DRY_RUN=1 MEXC_RANDOM_SKIP_PROBABILITY=0.5 python3 /root/obsidian/tldr/проекты/mexc/mexc-tg-monitor.py
```

Pass criteria:

- Logs show `random_skip`, `session_pause`, and `edge_gone_after_delay` as distinct reasons.

### P2.4 Fee verification and symbol disable

**Estimate:** 1-2 hours  
**Files:** `mexc-tg-monitor.py`  
**Why it matters for profit:** If maker fee is not zero, most small-edge alt flips become negative expectancy.

During contract metadata fetch inspect:

```text
makerFeeRate
takerFeeRate
makerFee
takerFee
feeRate
```

Rules:

- If maker fee field exists and exceeds configured `MEXC_MAKER_FEE_PCT`, disable that symbol.
- If fee fields are unavailable, use env fee model and log once per startup.
- If any live fill reports realized fee above expected, update SQLite and send Telegram warning.

Validation:

```bash
MEXC_MAKER_FEE_PCT=0.02 MEXC_DRY_RUN=1 python3 /root/obsidian/tldr/проекты/mexc/mexc-tg-monitor.py
```

Pass criteria:

- Net edge changes when fee env changes.
- Known non-zero-fee pairs do not auto-trade.

## P3: Optional Refactor and Dashboard

P3 is not required for profit. Estimated total: **8-16 hours**.

Only do this after the single-file v8 has stable logs and metrics.

Suggested split:

```text
mexc_bot/
  __init__.py
  config.py
  symbols.py
  rest_mexc.py
  ws_binance.py
  ws_mexc.py
  signals.py
  risk.py
  execution.py
  store.py
  health.py
  main.py
tests/
  test_symbols.py
  test_edges.py
  test_risk.py
```

Minimal dashboard:

- read-only SQLite report;
- current pairs, data staleness, active positions;
- signal reject reasons;
- daily notional and PnL;
- no trading controls in first version.

## API Endpoints To Use or Verify

### Binance

```text
GET wss://stream.binance.com:9443/ws/<symbol>@bookTicker[/...]
GET https://api.binance.com/api/v3/exchangeInfo
GET https://api.binance.com/api/v3/ticker/24hr
GET https://api.binance.com/api/v3/ticker/24hr?symbol=SOLUSDT
```

### MEXC public spot-style REST

```text
GET https://api.mexc.com/api/v3/ticker/bookTicker?symbol=SOLUSDT
GET https://api.mexc.com/api/v3/depth?symbol=SOLUSDT&limit=20
```

Use only through `curl_cffi` wrapper with browser headers.

### MEXC futures REST

```text
GET  https://contract.mexc.com/api/v1/contract/detail
GET  https://contract.mexc.com/api/v1/contract/detail?symbol=SOL_USDT
POST https://contract.mexc.com/api/v1/private/position/change_leverage
POST https://contract.mexc.com/api/v1/private/order/create
POST https://contract.mexc.com/api/v1/private/order/cancel
GET  https://contract.mexc.com/api/v1/private/order/get/{orderId}
GET  https://contract.mexc.com/api/v1/private/order/list/open_orders/{symbol}
POST https://contract.mexc.com/api/v1/private/position/close_all
GET  https://contract.mexc.com/api/v1/private/position/open_positions
GET  https://contract.mexc.com/api/v1/private/account/assets
GET  https://contract.mexc.com/api/v1/private/account/asset/USDT
```

Status: private endpoints beyond the currently working order/create, leverage, and close_all must be probed and documented before relying on them.

### MEXC futures WebSocket

```text
wss://contract.mexc.com/edge
```

Candidate subscriptions to verify:

```json
{"method":"sub.ticker","param":{"symbol":"SOL_USDT"}}
{"method":"sub.depth","param":{"symbol":"SOL_USDT"}}
{"method":"sub.depth.full","param":{"symbol":"SOL_USDT","limit":5}}
```

The plan requires updating `гайд/mexc-api-reference.md` with the exact verified subscription and message shape.

## File-by-File Diff Plan

### `/root/obsidian/tldr/проекты/mexc/mexc-tg-monitor.py`

P0 changes:

- Add runtime gates: `DRY_RUN`, `AUTO_TRADE_ENABLED`, `SIGNAL_ONLY`, WebSocket/fallback flags.
- Remove hardcoded Telegram token and chat ID defaults.
- Add symbol helpers.
- Centralize MEXC `curl_cffi` transport and JSON guard.
- Add SQLite schema and store helpers.
- Replace midpoint spread with bid/ask cross-edge.
- Add fee model.
- Rename current market order helper and remove it from normal entry path.
- Add contract metadata cache.
- Add passive limit entry, order detail, cancel, fill timeout.
- Add startup open-position reconciliation.
- Add heartbeat writer.

P1 changes:

- Add verified MEXC WebSocket client.
- Convert REST poller to stale-symbol fallback.
- Add pair discovery and scoring.
- Add pair generation and WS resubscribe.
- Add depth/hole filter.
- Add dynamic sizing.

P2 changes:

- Add account monitor.
- Add volume ledger risk rules.
- Add session pacing and pair cooldown.
- Add maker-fee verification and pair disable.

### `/root/bin/mexc-tg-monitor.py`

Deployment copy only.

Deployment command:

```bash
cp /root/obsidian/tldr/проекты/mexc/mexc-tg-monitor.py /root/bin/mexc-tg-monitor.py
chmod +x /root/bin/mexc-tg-monitor.py
cmp -s /root/obsidian/tldr/проекты/mexc/mexc-tg-monitor.py /root/bin/mexc-tg-monitor.py
echo $?
```

Expected output: `0`.

### `/etc/systemd/system/mexc-monitor.service`

Create in P0.7. Start in dry-run by default. Change to live only after endpoint probes and minimum-size order tests.

### `/root/bin/mexc-monitor-healthcheck.sh`

Create in P0.7. It restarts the service on missing/stale heartbeat.

### `/etc/systemd/system/mexc-monitor-healthcheck.service`

Optional:

```ini
[Unit]
Description=MEXC Monitor heartbeat health-check

[Service]
Type=oneshot
ExecStart=/root/bin/mexc-monitor-healthcheck.sh
```

### `/etc/systemd/system/mexc-monitor-healthcheck.timer`

Optional:

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

### `/root/obsidian/tldr/проекты/mexc/data/mexc-trades.sqlite3`

Created automatically by `db_init()`. Use WAL mode. Contains `signals`, `orders`, `trades`, and `volume_ledger`.

### `/root/obsidian/tldr/проекты/mexc/README.md`

Update after implementation:

- Current architecture.
- Systemd operations.
- Dry-run/live flags.
- SQLite inspection commands.
- Recovery procedure for unknown open position.

### `/root/obsidian/tldr/проекты/mexc/гайд/mexc-api-reference.md`

Update after probes:

- Verified MEXC WS channel.
- Verified post-only order type.
- Verified cancel/order-detail endpoints.
- Verified account/position endpoints.
- Exact response snippets with secrets removed.

### `/root/.mexc-api/config.json`

Do not edit except rotating keys. Never commit, print, or copy contents.

## Risk Register

| Risk | Probability | Impact | Priority | Mitigation | Stop Condition |
|---|---:|---:|---|---|---|
| Midpoint spread creates fake signals | High | High | P0 | Use bid/ask cross-edge only | Any normal entry still references midpoint spread |
| Market/taker entry destroys edge | High | High | P0 | Passive limit only; no market fallback | Post-only cannot be verified |
| Post-only type `5` is wrong | Medium | High | P0 | Probe smallest order; configurable order type | MEXC rejects type or fills as taker |
| Fill timeout too short/long | High | Medium | P1 | Record fill rate and tune from data | Fill rate <30% after 50 valid signals |
| MEXC WS channel shape differs | Medium | Medium | P1 | Probe first; REST fallback | No bid/ask from WS after 30 min |
| REST blocked by Cloudflare | Medium | High | P0/P1 | `curl_cffi`, browser headers, content-type guard | >50% REST failures for 10 min |
| SQLite blocks event loop | Low | Medium | P1 | Small writes; move to `asyncio.to_thread` if lag | Signal loop lag >1 sec |
| Restart loses open position | Medium | High | P0 | Startup reconciliation and pause on unknown position | Unknown open position detected |
| Fee is not zero | Medium | High | P2 | Fee env, metadata, realized fee reconciliation | Known maker fee > allowed fee |
| Pair universe includes non-equivalent contract | Medium | High | P1 | contract metadata, blocklist, price sanity checks | Price ratio abnormal or contract size unknown |
| Account PnL reaches risky zone | High | High | P2 | PnL monitor and pause/throttle | PnL >= configured stop |
| Account review / futures restriction | High | High | P2 | notional caps, session pacing, pair cooldown | opening restricted, warning, or freeze |
| Telegram fails | Medium | Low | P0 | Log locally; Telegram not critical path | None, unless user relies on manual mode |
| API key leakage | Low | Critical | P0 | env-only secrets, log redaction, git scan | Secret appears in repo/journal |
| Over-expansion to 50 pairs destabilizes WS | Medium | Medium | P1 | staged pair caps 12 -> 20 -> 30 -> 50 | reconnects >5/hour |
| PnL estimate diverges from reality | Medium | High | P1/P2 | store expected vs realized; reconcile fills/fees | divergence >30% over 20 trades |
| Jurisdiction/geo restriction blocks trading | Medium | Critical | P2 | detect API errors, stop trading, do not retry spam | private endpoint says opening restricted |

## Success Metrics

### Operational

| Metric | P0 Target | P1 Target | P2 Target | Source |
|---|---:|---:|---:|---|
| Service uptime | >99% daily | >99% daily | >99% daily | `systemctl`, heartbeat |
| Restart recovery | <10 sec | <10 sec | <10 sec | `journalctl` |
| Heartbeat age | <10 sec | <10 sec | <10 sec | `/run/mexc-monitor/heartbeat.json` |
| Binance price freshness | <2 sec | <1 sec | <1 sec | heartbeat/logs |
| MEXC WS price freshness | fallback allowed | <2 sec | <1 sec | heartbeat/logs |
| REST fallback share | visible | <20% updates | <10% updates | internal counters |
| Unknown open position handling | pause | restore/manage | restore/manage | startup logs |

### Strategy

| Metric | Current v7 | First Target | Mature Target | Source |
|---|---:|---:|---:|---|
| Active pairs | 8 | 12-20 | 30-50 only if stable | heartbeat |
| Entry edge type | midpoint | cross-edge | cross-edge | logs/code |
| Entry threshold | 0.12% | 0.12%/0.09% | 0.06% only after proof | env |
| Minimum net edge | none | >=0.08% initially | >=0.04-0.06% | SQLite |
| Limit fill rate | none | >30% | >50% | orders table |
| Timeout cancel rate | none | tracked | falling or justified | orders table |
| Signals/day | unknown | measured | 15-30 qualified | signals table |
| Live trades/day | max 20 | 3-10 | 5-15 if profitable | trades table |
| Expected vs realized PnL gap | unknown | measured | <30% | trades table |
| 7-day realized PnL | unknown | non-negative | positive after fees | SQLite |

### Account Safety

| Metric | Target | Source |
|---|---:|---|
| Max concurrent trades | 1 | risk logs |
| Daily notional | below `MEXC_MAX_DAILY_NOTIONAL_USD` | `volume_ledger` |
| Total notional | below `MEXC_MAX_TOTAL_NOTIONAL_USD` | `volume_ledger` |
| Same-pair cooldown | respected | `trades` timestamps |
| Trades after account PnL stop | 0 | account monitor + SQLite |
| Trades on known non-zero maker fee pairs | 0 | pair disable + SQLite |

## Execution Order

1. P0.1 runtime gates and secret cleanup.
2. P0.2 symbol helpers and REST wrapper.
3. P0.3 SQLite.
4. P0.4 cross-edge and fee model.
5. P0.5 passive limit entry, fill timeout, cancel.
6. P0.6 startup position reconciliation.
7. P0.7 heartbeat and systemd dry-run.
8. Run 12-24h dry-run and inspect SQLite.
9. P1.1 MEXC WebSocket integration.
10. P1.2 pair selector with cap 12-20.
11. P1.3 depth/hole filter.
12. P1.4 dynamic sizing.
13. P1.5 threshold experiment.
14. P2.1 account PnL monitor.
15. P2.2 notional caps.
16. P2.3 pacing.
17. P2.4 fee verification.

## Live Trading Gate

Before `MEXC_AUTO_TRADE=1` and `MEXC_DRY_RUN=0`, all must be true:

```bash
python3 -m py_compile /root/obsidian/tldr/проекты/mexc/mexc-tg-monitor.py
cmp -s /root/obsidian/tldr/проекты/mexc/mexc-tg-monitor.py /root/bin/mexc-tg-monitor.py
/root/bin/mexc-monitor-healthcheck.sh
sqlite3 /root/obsidian/tldr/проекты/mexc/data/mexc-trades.sqlite3 '.tables'
journalctl -u mexc-monitor.service -n 200 --no-pager | rg -i "error|traceback|secret|token|api_key" || true
```

Manual review must confirm:

- post-only order type verified;
- cancel endpoint verified;
- fill status endpoint verified;
- close path verified;
- no unknown open positions;
- Telegram secrets not in source;
- service runs in dry-run without crashes;
- SQLite records accepted and rejected signals;
- no live order path is reachable when `MEXC_DRY_RUN=1`;
- no market entry fallback exists.

## First Live Rollout

Use smallest practical size and conservative config:

```ini
Environment=MEXC_AUTO_TRADE=1
Environment=MEXC_DRY_RUN=0
Environment=MEXC_TARGET_PAIR_MAX=8
Environment=MEXC_ENTRY_SPREAD=0.12
Environment=MEXC_MIN_NET_EDGE_PCT=0.08
Environment=MEXC_MAX_TRADES_PER_DAY=3
Environment=MEXC_MAX_DAILY_NOTIONAL_USD=500
Environment=MEXC_MAX_TRADE_MARGIN_USD=7
```

First live session stop conditions:

- any order fills as taker unexpectedly;
- cancel endpoint fails;
- close endpoint fails;
- realized fee is above expected fee;
- first 3 trades have negative realized expectancy;
- account endpoint reports restriction;
- heartbeat stale or service restarts during active position.

## Final Verification Checklist

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
sqlite3 /root/obsidian/tldr/проекты/mexc/data/mexc-trades.sqlite3 \
  "select decision, reject_reason, count(*) from signals group by 1,2 order by 3 desc limit 20;"
```

Ready means:

- compile passes;
- deployed copy matches source;
- service is active;
- heartbeat age is under 20 seconds;
- Binance and MEXC data sources are active or fallback is explicit;
- SQLite initializes;
- dry-run records signals and rejections;
- no secret values appear in logs;
- live mode remains disabled until explicit rollout.

