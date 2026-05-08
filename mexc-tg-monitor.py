#!/usr/bin/env python3
"""MEXC-Binance Spread Monitor v8 — Codex execution plan P0"""

import asyncio, json, time, os, logging, hmac, hashlib, random, sqlite3
from datetime import datetime, date
from urllib.parse import urlencode
from curl_cffi import requests as curl_requests
from pathlib import Path

# Сохраняем прокси ДО очистки (MEXC WS требует прокси, без него 403)
_WS_PROXY = os.environ.get("ALL_PROXY", "") or os.environ.get("all_proxy", "")

for k in ['HTTP_PROXY','HTTPS_PROXY','http_proxy','https_proxy']:
    os.environ.pop(k, None)

try:
    import websockets
except ImportError:
    import subprocess; subprocess.check_call(["pip","install","websockets"]); import websockets

# ========== CONFIG (env-first, no hardcoded secrets) ==========

BOT_VERSION = "v8-p0"
DRY_RUN = os.getenv("MEXC_DRY_RUN", "1") == "1"
AUTO_TRADE_ENABLED = os.getenv("MEXC_AUTO_TRADE", "0") == "1"
SIGNAL_ONLY = os.getenv("MEXC_SIGNAL_ONLY", "0") == "1"
MEXC_WS_ENABLED = os.getenv("MEXC_WS_ENABLED", "1") == "1"
MEXC_REST_FALLBACK_ENABLED = os.getenv("MEXC_REST_FALLBACK_ENABLED", "1") == "1"
STATE_DIR = os.getenv("MEXC_STATE_DIR", "/root/obsidian/tldr/проекты/mexc/data")
HEARTBEAT_PATH = os.getenv("MEXC_HEARTBEAT_PATH", "/run/mexc-monitor/heartbeat.json")

TG_BOT_TOKEN = os.getenv("MEXC_TG_BOT_TOKEN", "")
TG_CHAT_ID = os.getenv("MEXC_TG_CHAT_ID", "")
TG_ENABLED = bool(TG_BOT_TOKEN and TG_CHAT_ID)

ENTRY_SPREAD = float(os.getenv("MEXC_ENTRY_SPREAD", "0.12"))
TG_THRESHOLD = float(os.getenv("MEXC_TG_THRESHOLD", "0.12"))
ALERT_CD = int(os.getenv("MEXC_ALERT_CD", "60"))
EXIT_TIMEOUT = int(os.getenv("MEXC_EXIT_TIMEOUT", "30"))
EXIT_SPREAD = float(os.getenv("MEXC_EXIT_SPREAD", "0.02"))

TRADE_AMOUNT_BASE = float(os.getenv("MEXC_TRADE_AMOUNT", "7"))
TRADE_AMOUNT_JITTER = float(os.getenv("MEXC_TRADE_AMOUNT_JITTER", "1.0"))
TRADE_LEVERAGE = int(os.getenv("MEXC_LEVERAGE", "20"))
MAX_MEXC_SPREAD = float(os.getenv("MEXC_MAX_MEXC_SPREAD", "0.05"))
MIN_LIQUIDITY = float(os.getenv("MEXC_MIN_LIQUIDITY", "1000"))

MAX_CONCURRENT_TRADES = int(os.getenv("MEXC_MAX_CONCURRENT", "1"))
MAX_TRADES_PER_DAY = int(os.getenv("MEXC_MAX_TRADES_PER_DAY", "20"))
DAILY_LOSS_LIMIT = float(os.getenv("MEXC_DAILY_LOSS_LIMIT", "3.0"))
MIN_DELAY_OPEN = float(os.getenv("MEXC_MIN_DELAY", "0.5"))
MAX_DELAY_OPEN = float(os.getenv("MEXC_MAX_DELAY", "2.0"))
SERIES_PAUSE_AFTER = int(os.getenv("MEXC_SERIES_PAUSE_AFTER", "8"))
SERIES_PAUSE_SEC = int(os.getenv("MEXC_SERIES_PAUSE_SEC", "120"))

# Fee model
MEXC_MAKER_FEE_PCT = float(os.getenv("MEXC_MAKER_FEE_PCT", "0.0"))
MEXC_TAKER_FEE_PCT = float(os.getenv("MEXC_TAKER_FEE_PCT", "0.01"))
ENTRY_FEE_MODE = os.getenv("MEXC_ENTRY_FEE_MODE", "maker")
EXIT_FEE_MODE = os.getenv("MEXC_EXIT_FEE_MODE", "maker")
SLIPPAGE_BUFFER_PCT = float(os.getenv("MEXC_SLIPPAGE_BUFFER_PCT", "0.01"))
MIN_NET_EDGE_PCT = float(os.getenv("MEXC_MIN_NET_EDGE_PCT", "0.04"))

# Limit order config
LIMIT_POST_ONLY_TYPE = int(os.getenv("MEXC_LIMIT_POST_ONLY_TYPE", "5"))
ENTRY_LIMIT_OFFSET_BPS = float(os.getenv("MEXC_ENTRY_LIMIT_OFFSET_BPS", "0.5"))
ORDER_FILL_TIMEOUT_SEC = float(os.getenv("MEXC_ORDER_FILL_TIMEOUT_SEC", "2.0"))
MEXC_WS_STALE_SEC = float(os.getenv("MEXC_WS_STALE_SEC", "5"))

TRADING_PAUSED_REASON = ""
CLOSE_UNKNOWN_ON_STARTUP = os.getenv("MEXC_CLOSE_UNKNOWN_ON_STARTUP", "0") == "1"

PAIRS = {"zecusdt":"ZECUSDT","xlmusdt":"XLMUSDT","solusdt":"SOLUSDT",
         "bchusdt":"BCHUSDT","btcusdt":"BTCUSDT","ethusdt":"ETHUSDT",
         "xrpusdt":"XRPUSDT","ltcusdt":"LTCUSDT"}

BINANCE_WS = "wss://stream.binance.com:9443/ws"
SIGNAL_FILE = "/tmp/mexc-tg-signals.txt"

# --- API keys (file-only, never hardcoded) ---
API_KEY = ""
API_SECRET = ""
_cfg_path = "/root/.mexc-api/config.json"
try:
    with open(_cfg_path) as f:
        cfg = json.load(f)
        API_KEY = os.getenv("MEXC_API_KEY", cfg.get("api_key", ""))
        API_SECRET = os.getenv("MEXC_API_SECRET", cfg.get("api_secret", ""))
except:
    API_KEY = os.getenv("MEXC_API_KEY", "")
    API_SECRET = os.getenv("MEXC_API_SECRET", "")

AUTO_MODE = bool(API_KEY and API_SECRET and AUTO_TRADE_ENABLED and not DRY_RUN and not SIGNAL_ONLY)

# ========== Symbol utilities (P0.2) ==========

def to_mexc_spot_symbol(symbol: str) -> str:
    return symbol.upper().replace("_", "")

def to_mexc_contract_symbol(symbol: str) -> str:
    spot = to_mexc_spot_symbol(symbol)
    if not spot.endswith("USDT"):
        raise ValueError(f"unsupported quote: {symbol}")
    return f"{spot[:-4]}_{spot[-4:]}"

def sym_key(symbol: str) -> str:
    return to_mexc_spot_symbol(symbol).lower()

# ========== curl_cffi wrapper (P0.2) ==========

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

def parse_json_response(resp, context: str):
    ct = resp.headers.get("Content-Type", "")
    if "json" not in ct.lower():
        log.warning("%s non-json status=%s ct=%s body=%r",
                    context, resp.status_code, ct, resp.text[:200])
        return None
    try:
        return resp.json()
    except Exception as e:
        log.warning("%s invalid-json status=%s err=%s body=%r",
                    context, resp.status_code, e, resp.text[:200])
        return None

# ========== SQLite (P0.3) ==========

_DB = None

def get_db() -> sqlite3.Connection:
    global _DB
    if _DB is not None:
        return _DB
    os.makedirs(STATE_DIR, exist_ok=True)
    db_path = os.path.join(STATE_DIR, "mexc-trades.sqlite3")
    _DB = sqlite3.connect(db_path, check_same_thread=False)
    _DB.row_factory = sqlite3.Row
    _DB.execute("PRAGMA journal_mode=WAL;")
    _DB.execute("PRAGMA synchronous=NORMAL;")
    _init_schema()
    return _DB

def _init_schema():
    sqls = [
        """CREATE TABLE IF NOT EXISTS signals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL, symbol TEXT NOT NULL, direction TEXT NOT NULL,
            binance_bid REAL NOT NULL, binance_ask REAL NOT NULL,
            mexc_bid REAL NOT NULL, mexc_ask REAL NOT NULL,
            raw_edge_pct REAL NOT NULL, net_edge_pct REAL,
            liquidity_usd REAL, mexc_spread_pct REAL,
            decision TEXT NOT NULL, reject_reason TEXT, meta_json TEXT
        )""",
        """CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT, signal_id INTEGER,
            ts TEXT NOT NULL, symbol TEXT NOT NULL, direction TEXT NOT NULL,
            order_id TEXT, external_oid TEXT, side INTEGER, order_type INTEGER,
            price REAL, qty REAL, status TEXT NOT NULL, raw_json TEXT,
            FOREIGN KEY(signal_id) REFERENCES signals(id)
        )""",
        """CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT, signal_id INTEGER,
            opened_at TEXT NOT NULL, closed_at TEXT,
            symbol TEXT NOT NULL, direction TEXT NOT NULL,
            qty REAL NOT NULL, leverage REAL NOT NULL,
            margin_usd REAL NOT NULL, notional_usd REAL NOT NULL,
            entry_order_id TEXT, exit_order_id TEXT,
            entry_price REAL, exit_price REAL,
            binance_entry_ref REAL, binance_exit_ref REAL,
            raw_edge_entry_pct REAL, net_edge_entry_pct REAL,
            raw_edge_exit_pct REAL,
            expected_fee_usd REAL, realized_fee_usd REAL,
            expected_pnl_usd REAL, realized_pnl_usd REAL,
            status TEXT NOT NULL, close_reason TEXT, meta_json TEXT,
            FOREIGN KEY(signal_id) REFERENCES signals(id)
        )""",
        """CREATE TABLE IF NOT EXISTS volume_ledger (
            id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL,
            symbol TEXT NOT NULL, event TEXT NOT NULL,
            notional_usd REAL NOT NULL, trade_id INTEGER,
            meta_json TEXT, FOREIGN KEY(trade_id) REFERENCES trades(id)
        )""",
        """CREATE INDEX IF NOT EXISTS idx_signals_ts ON signals(ts)""",
        """CREATE INDEX IF NOT EXISTS idx_signals_symbol ON signals(symbol)""",
        """CREATE INDEX IF NOT EXISTS idx_trades_status ON trades(status)""",
        """CREATE INDEX IF NOT EXISTS idx_volume_ts ON volume_ledger(ts)""",
    ]
    for sql in sqls:
        try:
            _DB.execute(sql)
        except Exception as e:
            log.warning(f"DB schema: {e}")
    _DB.commit()

def db_exec(sql: str, params=()) -> sqlite3.Cursor:
    try:
        c = get_db().execute(sql, params)
        get_db().commit()
        return c
    except Exception as e:
        log.error(f"DB error: {e}")
        return None

def db_insert_signal(ts, symbol, direction, bp, mp, raw_edge_pct, net_edge_pct,
                      liquidity_usd, mexc_spread_pct, decision, reject_reason=""):
    db_exec(
        "INSERT INTO signals(ts,symbol,direction,binance_bid,binance_ask,"
        "mexc_bid,mexc_ask,raw_edge_pct,net_edge_pct,liquidity_usd,"
        "mexc_spread_pct,decision,reject_reason) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (ts, symbol, direction, float(bp["bid"]), float(bp["ask"]),
         float(mp["bid"]), float(mp["ask"]), raw_edge_pct, net_edge_pct,
         liquidity_usd, mexc_spread_pct, decision, reject_reason)
    )
    return db_exec("SELECT last_insert_rowid()").fetchone()[0]

def db_insert_order(signal_id, symbol, direction, order_id, external_oid,
                     side, order_type, price, qty, status, raw_json=""):
    db_exec(
        "INSERT INTO orders(signal_id,ts,symbol,direction,order_id,"
        "external_oid,side,order_type,price,qty,status,raw_json) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (signal_id, datetime.utcnow().isoformat(), symbol, direction,
         order_id, external_oid, side, order_type, price, qty, status,
         json.dumps(raw_json) if raw_json else "")
    )

def db_insert_trade(signal_id, symbol, direction, qty, leverage, margin_usd, notional_usd,
                    entry_price, binance_entry_ref, raw_edge_pct, net_edge_pct,
                    status="open", entry_order_id=""):
    db_exec(
        "INSERT INTO trades(signal_id,opened_at,symbol,direction,qty,leverage,"
        "margin_usd,notional_usd,entry_price,binance_entry_ref,"
        "raw_edge_entry_pct,net_edge_entry_pct,entry_order_id,status) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (signal_id, datetime.utcnow().isoformat(), symbol, direction,
         qty, leverage, margin_usd, notional_usd, entry_price, binance_entry_ref,
         raw_edge_pct, net_edge_pct, entry_order_id, status)
    )

# ========== Fee model (P0.4) ==========

def fee_pct(mode: str) -> float:
    return MEXC_MAKER_FEE_PCT if mode == "maker" else MEXC_TAKER_FEE_PCT

def compute_net_edge_pct(raw_edge_pct: float) -> float:
    return raw_edge_pct - fee_pct(ENTRY_FEE_MODE) - fee_pct(EXIT_FEE_MODE) - SLIPPAGE_BUFFER_PCT

# ========== Cross-edge pricing (P0.4) ==========

def compute_cross_edges(bp: dict, mp: dict) -> dict:
    b_bid, b_ask = float(bp["bid"]), float(bp["ask"])
    m_bid, m_ask = float(mp["bid"]), float(mp["ask"])
    return {
        "LONG": {
            "raw_edge_pct": (b_bid - m_ask) / m_ask * 100 if m_ask else 0,
            "binance_ref": b_bid,
            "mexc_entry_cross": m_ask,
            "mexc_passive_price": m_bid,
        },
        "SHORT": {
            "raw_edge_pct": (m_bid - b_ask) / b_ask * 100 if b_ask else 0,
            "binance_ref": b_ask,
            "mexc_entry_cross": m_bid,
            "mexc_passive_price": m_ask,
        },
    }

def quantize_to_step(value: float, step: float = 0.0, fallback_digits: int = 8) -> float:
    if step and step > 0:
        return round(round(value / step) * step, 12)
    return float(f"{value:.{fallback_digits}f}")

# ========== MEXC API (V1 signature) ==========

def _param_string(method, params, body):
    if method == "GET":
        return urlencode(sorted(params.items())) if params else ""
    return json.dumps(body) if body else ""

def _mexc_sig(method, params, body, ts):
    raw = API_KEY + ts + _param_string(method, params, body)
    return hmac.new(API_SECRET.encode(), raw.encode(), hashlib.sha256).hexdigest()

def _mexc_headers(ts, sig):
    return {"ApiKey": API_KEY, "Request-Time": ts, "Signature": sig,
            "Content-Type": "application/json"}

async def mexc_api(method, path, params=None, body=None):
    if not AUTO_MODE:
        return None
    ts = str(int(time.time() * 1000))
    sig = _mexc_sig(method, params or {}, body or {}, ts)
    base = "https://contract.mexc.com" if path.startswith("/api/v1/private") else "https://api.mexc.com"
    url = f"{base}{path}"
    try:
        r = await asyncio.to_thread(_curl, method, url, headers=_mexc_headers(ts, sig),
                                    json=body if method != "GET" else None,
                                    params=params if method == "GET" else None)
        return parse_json_response(r, f"API ({path})")
    except Exception as e:
        log.error(f"⚠️ API ({path}): {e}")
        return None

async def mexc_set_leverage(sym_key_val):
    msym = PAIRS[sym_key_val]
    msym_v1 = to_mexc_contract_symbol(msym)
    r = await mexc_api("POST", "/api/v1/private/position/change_leverage",
        body={"symbol": msym_v1, "openType": 1, "positionType": 1, "leverage": TRADE_LEVERAGE})
    if r and r.get("success"):
        log.info(f"⚙️ {msym} плечо {TRADE_LEVERAGE}x")
        return True
    log.warning(f"⚠️ {msym} leverage: {r}")
    return False

async def mexc_place_market_order(sym_key_val, direction, qty):
    """Emergency market order — only for close, not entry."""
    msym = PAIRS[sym_key_val]
    msym_v1 = to_mexc_contract_symbol(msym)
    order_side = 1 if direction == "LONG" else 3
    body = {
        "symbol": msym_v1, "price": 0, "vol": qty,
        "side": order_side, "type": 1, "openType": 1,
        "positionId": 0, "externalOid": f"hermes_mkt_{int(time.time())}"
    }
    r = await mexc_api("POST", "/api/v1/private/order/create", body=body)
    if r and r.get("success"):
        data = r.get("data", {})
        log.info(f"📈 {msym} {direction} MARKET #{data.get('orderId','?')} vol={qty}")
        return data
    log.warning(f"⚠️ {msym} market order: {r}")
    return None

async def mexc_place_limit_entry(sym_key_val, direction, qty, passive_price):
    """Post-only limit entry — primary entry path."""
    msym = PAIRS[sym_key_val]
    msym_v1 = to_mexc_contract_symbol(msym)
    if direction == "LONG":
        side = 1
        price = passive_price * (1 - ENTRY_LIMIT_OFFSET_BPS / 10000)
    else:
        side = 3
        price = passive_price * (1 + ENTRY_LIMIT_OFFSET_BPS / 10000)
    body = {
        "symbol": msym_v1, "price": quantize_to_step(price, 0.01),
        "vol": qty, "side": side, "type": LIMIT_POST_ONLY_TYPE, "openType": 1,
        "positionId": 0, "externalOid": f"hermes_lmt_{int(time.time())}"
    }
    r = await mexc_api("POST", "/api/v1/private/order/create", body=body)
    if r is None:
        log.warning(f"⚠️ {msym} limit entry: API error (no response)")
        return None, "api_error"
    if r.get("success"):
        data = r.get("data", {})
        oid = data.get("orderId", "?")
        log.info(f"📋 {msym} {direction} LIMIT #{oid} @{price} vol={qty}")
        return data, None
    err_msg = str(r.get("code", ""))
    log.warning(f"⚠️ {msym} limit entry rejected: {r}")
    return None, err_msg

async def mexc_cancel_order(sym_key_val, order_id):
    msym = PAIRS[sym_key_val]
    msym_v1 = to_mexc_contract_symbol(msym)
    r = await mexc_api("POST", "/api/v1/private/order/cancel",
        body={"symbol": msym_v1, "orderId": order_id})
    if r and r.get("success"):
        log.info(f"❌ {msym} ордер #{order_id} отменён")
        return True
    log.warning(f"⚠️ {msym} cancel #{order_id}: {r}")
    return False

async def mexc_order_status(sym_key_val, order_id):
    msym = PAIRS[sym_key_val]
    r = await mexc_api("GET", f"/api/v1/private/order/get/{order_id}")
    if r and r.get("success"):
        return r.get("data")
    log.warning(f"⚠️ order status {order_id}: {r}")
    return None

async def mexc_close_position(sym_key_val):
    msym = PAIRS[sym_key_val]
    r = await mexc_api("POST", "/api/v1/private/position/close_all", body={})
    if r and r.get("success"):
        log.info(f"📉 {msym} закрыто")
        return True
    log.warning(f"⚠️ {msym} close: {r}")
    return False

async def mexc_get_open_positions():
    r = await mexc_api("GET", "/api/v1/private/position/open_positions")
    if r and r.get("success"):
        return r.get("data", [])
    return None

async def mexc_get_qty(sym_key_val):
    jitter = random.uniform(-TRADE_AMOUNT_JITTER, TRADE_AMOUNT_JITTER)
    amount = max(TRADE_AMOUNT_BASE + jitter, 5)
    msym = PAIRS[sym_key_val]
    msym_v1 = to_mexc_contract_symbol(msym)
    try:
        r = await asyncio.to_thread(_curl, "GET",
            f"https://contract.mexc.com/api/v1/contract/detail?symbol={msym_v1}")
        if r.status_code != 200:
            log.warning(f"⚠️ contract/detail {msym_v1}: {r.status_code}")
            return 1, amount
        d = parse_json_response(r, f"contract/detail {msym_v1}")
        if d and d.get("success") and d.get("data"):
            cs = float(d["data"]["contractSize"])
            price = mexc_prices.get(sym_key_val, {}).get("ask", 0) or mexc_prices.get(sym_key_val, {}).get("bid", 0)
            if price and cs and price > 0:
                qty = int((amount * TRADE_LEVERAGE) / (price * cs))
                qty = max(qty, 1)
                log.info(f"  {msym} qty={qty} (${amount}×{TRADE_LEVERAGE}x)")
                return qty, amount
    except Exception as e:
        log.warning(f"⚠️ contract/detail {msym_v1}: {e}")
    return 1, amount

# ========== Startup reconciliation (P0.6) ==========

async def reconcile_positions():
    global TRADING_PAUSED_REASON
    log.info("🔍 Startup position reconciliation...")
    if not AUTO_MODE:
        log.info("  Skip: AUTO_MODE=False")
        return
    try:
        opens = await mexc_get_open_positions()
    except Exception as e:
        log.warning(f"  position check unavailable: {e}")
        return
    if not opens:
        log.info("  ✅ No open positions on MEXC")
        return
    if not isinstance(opens, list) or len(opens) == 0:
        log.info("  ✅ No open positions on MEXC")
        return

    # There are open positions — check DB
    symbols_open = [o.get("symbol", "") for o in opens if isinstance(o, dict)]
    log.warning(f"  ⚠️ Open positions on MEXC: {symbols_open}")
    db = get_db()
    cur = db.execute("SELECT id, symbol, direction FROM trades WHERE status='open'")
    db_open = {row["symbol"]: dict(row) for row in cur.fetchall()}

    for sym in symbols_open:
        sym_clean = sym.replace("_", "").lower()
        if sym_clean in db_open:
            log.info(f"  ✅ {sym} matched DB trade #{db_open[sym_clean]['id']}")
        else:
            log.warning(f"  ❓ {sym} has no matching DB trade")
            TRADING_PAUSED_REASON = f"unknown position on startup: {sym}"
            if CLOSE_UNKNOWN_ON_STARTUP:
                log.warning(f"  🛑 Closing unknown position {sym}...")
                await mexc_close_position(sym_clean)
                TRADING_PAUSED_REASON = ""

    if TRADING_PAUSED_REASON:
        log.warning(f"  🚫 TRADING PAUSED: {TRADING_PAUSED_REASON}")
        await send_tg(f"🚫 *MEXC Monitor:* {TRADING_PAUSED_REASON}\n"
                       "Новые сделки не открываются до ручного подтверждения.")

# ========== State ==========

binance_prices = {}
mexc_prices = {}
last_alert_ts = {}
active_trades = {}
exit_alert_ts = {}
_contract_cache = {}

_trade_count_today = 0
_today_date = date.today()
_daily_pnl = 0.0
_series_count = 0
_last_trade_time = 0
_consecutive_losses = 0

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s', datefmt='%H:%M:%S')
log = logging.getLogger('monitor')

# ========== Risk Control ==========

def _check_daily():
    global _trade_count_today, _today_date, _daily_pnl, _series_count
    if date.today() != _today_date:
        _trade_count_today = 0
        _today_date = date.today()
        _daily_pnl = 0.0
        _series_count = 0

def risk_check():
    _check_daily()
    now = time.time()
    if TRADING_PAUSED_REASON:
        return False, f"paused: {TRADING_PAUSED_REASON}"
    if _trade_count_today >= MAX_TRADES_PER_DAY:
        return False, f"лимит {MAX_TRADES_PER_DAY}/день"
    if _daily_pnl <= -DAILY_LOSS_LIMIT:
        return False, f"убыток ${_daily_pnl:.2f}"
    if len(active_trades) >= MAX_CONCURRENT_TRADES:
        return False, f"уже {len(active_trades)}"
    if _series_count >= SERIES_PAUSE_AFTER:
        if now - _last_trade_time < SERIES_PAUSE_SEC:
            return False, f"пауза {int(SERIES_PAUSE_SEC-(now-_last_trade_time))}s"
    return True, "ok"

def record_trade(pnl_usd):
    global _trade_count_today, _daily_pnl, _series_count, _last_trade_time, _consecutive_losses
    _check_daily()
    _trade_count_today += 1
    _last_trade_time = time.time()
    if pnl_usd >= 0:
        _series_count += 1
        _consecutive_losses = 0
    else:
        _series_count = 0
        _consecutive_losses += 1
    _daily_pnl += pnl_usd

# ========== Formatting ==========

def mexc_link(sym):
    return f"https://futures.mexc.com/exchange/{to_mexc_contract_symbol(sym)}"

def fmt_entry(sym, sp, net_sp, dr, bm, mm, auto=False, amount=7):
    emoji = "🟢" if dr == "SHORT" else "🔵"
    pos_size = amount * TRADE_LEVERAGE
    side_ru = "SELL (Short)" if dr == "SHORT" else "BUY (Long)"
    pnl_pct = abs(net_sp) * TRADE_LEVERAGE
    mode = "🤖 Авто" if auto else "🛠 Ручной"
    status = "Ордер выставлен!" if auto else "Жди сигнала"
    return (f"{emoji} *{sym}* — спред *{abs(sp):.2f}%* ({mode})\n"
            f"Net: *{abs(net_sp):.2f}%* (после fees+slip)\n"
            f"Направление: {dr}\n"
            f"Binance: `${bm:.4f}`\n"
            f"MEXC:    `${mm:.4f}`\n\n"
            f"📊 *Вход:* ${amount:.0f} × {TRADE_LEVERAGE}x = ${pos_size:.0f}\n"
            f"💰 *Расчёт:* ~{pnl_pct:.1f}% к депозиту\n"
            f"🛒 Действие: *{side_ru}* на MEXC\n"
            f"[👉 Открыть {sym} на MEXC]({mexc_link(sym)})\n\n"
            f"*{status}*")

def fmt_exit(sym, entry_sp, entry_dr, entry_bm, entry_mm, cur_bm, cur_mm, auto=False):
    pnl_pct = abs(entry_sp) * TRADE_LEVERAGE
    pnl_usd = TRADE_AMOUNT_BASE * pnl_pct / 100
    mode = "🤖 Закрыто" if auto else "🟢 Закрывай вручную"
    return (f"🟢 *ВЫХОД {sym}*\n"
            f"Спред схлопнулся! {mode}\n\n"
            f"Вход: ${entry_mm:.4f}\n"
            f"Binance: `${cur_bm:.4f}`\n"
            f"MEXC:    `${cur_mm:.4f}`\n\n"
            f"💵 *Расчёт:* ~${pnl_usd:.2f} ({pnl_pct:.1f}% к депозиту)\n"
            f"[👉 Закрыть {sym} на MEXC]({mexc_link(sym)})")

async def send_tg(text):
    if not TG_ENABLED:
        return
    try:
        from aiohttp import ClientSession
        async with ClientSession() as s:
            async with s.post(f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage", json={
                "chat_id": TG_CHAT_ID, "text": text,
                "parse_mode": "Markdown", "disable_web_page_preview": True
            }, timeout=10) as r:
                if r.status != 200:
                    log.error(f"TG: {(await r.text())[:200]}")
    except ImportError:
        import requests as sync_req
        try:
            sync_req.post(f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage", json={
                "chat_id": TG_CHAT_ID, "text": text,
                "parse_mode": "Markdown", "disable_web_page_preview": True
            }, timeout=10)
        except Exception as e:
            log.error(f"TG send: {e}")
    except Exception as e:
        log.error(f"TG send: {e}")

# ========== Liquidity filter ==========

async def check_liquidity(sym_key_val, direction):
    try:
        msym = PAIRS[sym_key_val]
        r = await asyncio.to_thread(_curl, "GET",
            f"https://api.mexc.com/api/v3/depth?symbol={msym}&limit=20")
        if r.status_code != 200:
            return False, f"depth {r.status_code}", 0, 0
        data = parse_json_response(r, f"depth {msym}")
        if not data:
            return False, "empty depth", 0, 0
        bids = [(float(p), float(q)) for p, q in data.get('bids', [])]
        asks = [(float(p), float(q)) for p, q in data.get('asks', [])]
        if not bids or not asks:
            return False, "empty book", 0, 0
        mxc_sp = (asks[0][0] - bids[0][0]) / asks[0][0] * 100
        if mxc_sp > MAX_MEXC_SPREAD:
            return False, f"spread {mxc_sp:.3f}%", 0, mxc_sp
        cum_liq = sum(q * p for p, q in asks) if direction == "LONG" else sum(q * p for p, q in bids)
        if cum_liq < MIN_LIQUIDITY:
            return False, f"liq ${cum_liq:.0f}", cum_liq, mxc_sp
        return True, f"✅ liq=${cum_liq:.0f} spread={mxc_sp:.3f}%", cum_liq, mxc_sp
    except Exception as e:
        return False, str(e), 0, 0

# ========== Streams ==========

async def binance_stream():
    streams = [f"{p}@bookTicker" for p in PAIRS]
    url = f"{BINANCE_WS}/{'/'.join(streams)}"
    while True:
        try:
            async with websockets.connect(url, ping_interval=20) as ws:
                log.info(f"✅ Binance WS ({len(streams)} pairs)")
                async for msg in ws:
                    d = json.loads(msg)
                    s = d["s"].lower()
                    if s in PAIRS:
                        binance_prices[s] = {"bid": float(d["b"]), "ask": float(d["a"])}
        except Exception as e:
            log.error(f"⚠️ WS: {e}")
            await asyncio.sleep(3)

async def mexc_ws_stream():
    """MEXC WebSocket — primary market data feed."""
    log.info("🔌 MEXC WS connecting...")
    while True:
        try:
            # MEXC WS требует прокси (NO_PROXY=* блокирует системный прокси)
            if _WS_PROXY:
                os.environ["HTTP_PROXY"] = _WS_PROXY
                os.environ["HTTPS_PROXY"] = _WS_PROXY
                os.environ["ALL_PROXY"] = _WS_PROXY
            os.environ.pop("NO_PROXY", None)
            os.environ.pop("no_proxy", None)
            async with websockets.connect("wss://contract.mexc.com/edge", ping_interval=20) as ws:
                log.info("✅ MEXC WS connected")
                # Subscribe to all active pairs
                for key in PAIRS:
                    msym_v1 = to_mexc_contract_symbol(PAIRS[key])
                    sub = {"method": "sub.ticker", "param": {"symbol": msym_v1}}
                    await ws.send(json.dumps(sub))
                log.info(f"  Subscribed to {len(PAIRS)} pairs")
                async for msg in ws:
                    d = json.loads(msg)
                    # Skip subscription confirmation
                    if "channel" in d and d.get("channel") == "rs.sub.ticker":
                        continue
                    sym_data = d.get("data", {})
                    symbol_raw = d.get("symbol", "")
                    if symbol_raw and sym_data.get("bid1") is not None and sym_data.get("ask1") is not None:
                        key = symbol_raw.replace("_", "").lower()
                        if key in PAIRS:
                            mexc_prices[key] = {
                                "bid": float(sym_data["bid1"]),
                                "ask": float(sym_data["ask1"]),
                                "source": "ws",
                                "ts": time.time(),
                            }
        except Exception as e:
            log.error(f"⚠️ MEXC WS: {e}")
            await asyncio.sleep(3)

async def mexc_rest_fallback():
    """REST fallback — only for stale or unsubscribed symbols."""
    if not MEXC_REST_FALLBACK_ENABLED:
        return
    while True:
        try:
            stale_keys = [k for k in PAIRS
                          if k not in mexc_prices
                          or mexc_prices[k].get("source") == "rest"
                          or (time.time() - mexc_prices[k].get("ts", 0) > MEXC_WS_STALE_SEC)]
            if not stale_keys:
                await asyncio.sleep(5)
                continue
            tasks = []
            for key in stale_keys:
                msym = PAIRS[key]
                tasks.append(asyncio.to_thread(_curl, "GET",
                    f"https://api.mexc.com/api/v3/ticker/bookTicker?symbol={msym}"))
            responses = await asyncio.gather(*tasks, return_exceptions=True)
            for key, resp in zip(stale_keys, responses):
                if isinstance(resp, Exception):
                    continue
                if resp.status_code == 200:
                    d = parse_json_response(resp, f"bookTicker {key}")
                    if d:
                        mexc_prices[key] = {
                            "bid": float(d["bidPrice"]),
                            "ask": float(d["askPrice"]),
                            "source": "rest",
                            "ts": time.time(),
                        }
        except Exception as e:
            log.error(f"⚠️ MEXC fallback: {e}")
        await asyncio.sleep(2)

# ========== Heartbeat (P0.7) ==========

async def health_reporter():
    while True:
        try:
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
        except Exception as e:
            log.warning(f"heartbeat: {e}")
        await asyncio.sleep(5)

# ========== Core signal engine ==========

async def check_signals():
    while True:
        await asyncio.sleep(0.3)
        now = time.time()

        # --- Exit ---
        expired = []
        for sym, trade in active_trades.items():
            bp = binance_prices.get(sym)
            mp = mexc_prices.get(sym)
            if not bp or not mp: continue
            bm = (bp["bid"] + bp["ask"]) / 2
            mm = (mp["bid"] + mp["ask"]) / 2
            if bm == 0: continue
            cur_sp = (mm - bm) / bm * 100
            cur_absp = abs(cur_sp)

            if cur_absp < EXIT_SPREAD:
                if sym not in exit_alert_ts or now - exit_alert_ts[sym] >= 30:
                    exit_alert_ts[sym] = now
                    if AUTO_MODE and trade.get('order_placed'):
                        await mexc_close_position(sym)
                        db_exec("UPDATE trades SET closed_at=?,status='closed',close_reason=?,exit_price=?,binance_exit_ref=? "
                                "WHERE status='open' AND symbol=? ORDER BY id DESC LIMIT 1",
                                (datetime.utcnow().isoformat(), "spread_closed", mm, bm, sym.upper()))
                    pnl_usd = trade['entry_sp'] * TRADE_LEVERAGE / 100 * TRADE_AMOUNT_BASE
                    record_trade(pnl_usd)
                    _check_daily()
                    exit_msg = fmt_exit(sym.upper(), trade['entry_sp'],
                        trade['direction'], trade['entry_bm'], trade['entry_mm'],
                        bm, mm, auto=AUTO_MODE)
                    exit_msg += f"\n📊 День: {_trade_count_today}/{MAX_TRADES_PER_DAY} | ${_daily_pnl:+.2f}"
                    log.info(f"🚪 {sym.upper()} — выход")
                    print(f"\n{exit_msg}\n", flush=True)
                    await send_tg(exit_msg)
                expired.append(sym)
            elif now - trade['entry_time'] > EXIT_TIMEOUT:
                log.info(f"⌛ {sym.upper()} — таймаут")
                if AUTO_MODE and trade.get('order_placed'):
                    await mexc_close_position(sym)
                    db_exec("UPDATE trades SET closed_at=?,status='closed',close_reason=? "
                            "WHERE status='open' AND symbol=? ORDER BY id DESC LIMIT 1",
                            (datetime.utcnow().isoformat(), "timeout", sym.upper()))
                expired.append(sym)
        for sym in expired:
            active_trades.pop(sym, None)

        # --- Entry ---
        for sym in PAIRS:
            bp = binance_prices.get(sym)
            mp = mexc_prices.get(sym)
            if not bp or not mp: continue
            bm = (bp["bid"] + bp["ask"]) / 2
            mm = (mp["bid"] + mp["ask"]) / 2
            if bm == 0: continue
            if sym in active_trades: continue

            # Cross-edge pricing (P0.4)
            edges = compute_cross_edges(bp, mp)
            direction = "LONG" if edges["LONG"]["raw_edge_pct"] > edges["SHORT"]["raw_edge_pct"] else "SHORT"
            edge = edges[direction]
            raw_sp = edge["raw_edge_pct"]
            absp = abs(raw_sp)
            net_edge = compute_net_edge_pct(raw_sp)

            if absp >= ENTRY_SPREAD and net_edge >= MIN_NET_EDGE_PCT:
                last = last_alert_ts.get(sym, 0)
                if now - last >= ALERT_CD:
                    last_alert_ts[sym] = now

                    valid, reason, cum_liq, mxc_sp = await check_liquidity(sym, direction)
                    if not valid:
                        log.info(f"⏭ {sym.upper()} {absp:.2f}% {direction} — {reason}")
                        db_insert_signal(datetime.utcnow().isoformat(), sym.upper(), direction,
                                         bp, mp, absp, net_edge, cum_liq, mxc_sp,
                                         "rejected", reason)
                        continue

                    rc_ok, rc_reason = risk_check()
                    if not rc_ok:
                        log.info(f"⏭ {sym.upper()} {absp:.2f}% {direction} — RC: {rc_reason}")
                        db_insert_signal(datetime.utcnow().isoformat(), sym.upper(), direction,
                                         bp, mp, absp, net_edge, cum_liq, mxc_sp,
                                         "rejected", f"RC: {rc_reason}")
                        continue

                    jitter = random.uniform(-TRADE_AMOUNT_JITTER, TRADE_AMOUNT_JITTER)
                    amount = max(round(TRADE_AMOUNT_BASE + jitter, 1), 5.0)

                    # Record accepted signal
                    sig_id = db_insert_signal(datetime.utcnow().isoformat(), sym.upper(), direction,
                                               bp, mp, absp, net_edge, cum_liq, mxc_sp,
                                               "accepted", "")

                    order_placed = False
                    if AUTO_MODE and absp >= TG_THRESHOLD:
                        delay = random.uniform(MIN_DELAY_OPEN, MAX_DELAY_OPEN)
                        log.info(f"⏳ {sym.upper()} задержка {delay:.1f}s")
                        await asyncio.sleep(delay)

                        # Limit entry (P0.5)
                        await mexc_set_leverage(sym)
                        qty, amt = await mexc_get_qty(sym)
                        passive_price = edge["mexc_passive_price"]
                        order_result, err = await mexc_place_limit_entry(sym, direction, qty, passive_price)

                        if order_result:
                            oid = order_result.get("orderId", "?")
                            ext_oid = order_result.get("externalOid", "")
                            db_insert_order(sig_id, sym.upper(), direction, oid, ext_oid,
                                            1 if direction == "LONG" else 3, LIMIT_POST_ONLY_TYPE,
                                            edge["mexc_passive_price"], qty, "placed", order_result)

                            # Poll for fill (P0.5)
                            filled = False
                            poll_end = time.time() + ORDER_FILL_TIMEOUT_SEC
                            while time.time() < poll_end:
                                await asyncio.sleep(0.3)
                                status = await mexc_order_status(sym, oid)
                                if status is None:
                                    log.warning(f"  {sym} fill check: API error")
                                    break
                                if status.get("status") == 2:  # filled
                                    fill_price = float(status.get("price", edge["mexc_passive_price"]))
                                    log.info(f"  ✅ {sym} LIMIT filled @{fill_price}")
                                    db_insert_order(sig_id, sym.upper(), direction, oid, ext_oid,
                                                    1 if direction == "LONG" else 3, LIMIT_POST_ONLY_TYPE,
                                                    fill_price, qty, "filled", status)
                                    order_placed = True
                                    filled = True
                                    break
                                elif status.get("status") in (3, 4):  # cancelled / partial
                                    log.warning(f"  {sym} LIMIT status={status.get('status')}")
                                    break
                                elif status.get("status") == 1:  # pending
                                    pass
                                else:
                                    log.warning(f"  {sym} LIMIT unknown status: {status}")
                                    break

                            if not filled:
                                log.info(f"  {sym} LIMIT not filled in {ORDER_FILL_TIMEOUT_SEC}s — cancelling")
                                await mexc_cancel_order(sym, oid)
                                db_insert_order(sig_id, sym.upper(), direction, oid, ext_oid,
                                                1 if direction == "LONG" else 3, LIMIT_POST_ONLY_TYPE,
                                                edge["mexc_passive_price"], qty, "timeout_cancel", "")
                        else:
                            log.warning(f"  {sym} limit entry rejected ({err}) — SIGNAL_ONLY for this event")
                            db_insert_order(sig_id, sym.upper(), direction, "", "",
                                            1 if direction == "LONG" else 3, LIMIT_POST_ONLY_TYPE,
                                            edge["mexc_passive_price"], qty, f"rejected_{err}", {})

                    # Record trade in DB
                    if order_placed:
                        margin = amount
                        notional = amount * TRADE_LEVERAGE
                        db_insert_trade(sig_id, sym.upper(), direction, qty, TRADE_LEVERAGE,
                                        margin, notional, edge["mexc_passive_price"],
                                        edge["binance_ref"], raw_sp, net_edge,
                                        "open", "")
                        db_exec("INSERT INTO volume_ledger(ts,symbol,event,notional_usd) VALUES(?,?,?,?)",
                                (datetime.utcnow().isoformat(), sym.upper(), "entry", notional))

                    msg = fmt_entry(sym.upper(), raw_sp, net_edge, direction, bm, mm,
                                    auto=order_placed, amount=amount)
                    mode_str = 'AUTO' if order_placed else 'SIGNAL'
                    with open(SIGNAL_FILE, "a") as f:
                        f.write(json.dumps({"t": now, "sym": sym.upper(), "msg": msg}) + "\n")
                    with open("/tmp/mexc-spread-alerts.log", "a") as f:
                        f.write(f"{datetime.now().isoformat()} | {sym.upper()} | {absp:.3f}% | {direction} | {mode_str} | ${amount:.0f} | {reason}\n")
                    log.info(f"{'🤖' if order_placed else '🚨'} {sym.upper()} {absp:.2f}% {direction} ${amount:.0f}")
                    print(f"\n{msg}\n", flush=True)

                    if absp >= TG_THRESHOLD:
                        await send_tg(msg)
                        if order_placed:
                            active_trades[sym] = {
                                'entry_time': now, 'entry_sp': raw_sp,
                                'direction': direction, 'entry_bm': bm, 'entry_mm': mm,
                                'order_placed': order_placed, 'amount': amount
                            }

# ========== Main ==========

async def main():
    _check_daily()
    log.info(f"🚀 MEXC-Binance Monitor {BOT_VERSION}")
    log.info(f"Пары: {', '.join(PAIRS)} | Вход ≥{ENTRY_SPREAD}% | TG ≥{TG_THRESHOLD}%")
    log.info(f"Фильтры: спред ≤{MAX_MEXC_SPREAD}% | ликв. ≥${MIN_LIQUIDITY}")
    log.info(f"Параметры: ${TRADE_AMOUNT_BASE}±${TRADE_AMOUNT_JITTER} × {TRADE_LEVERAGE}x")
    log.info(f"RC: {MAX_CONCURRENT_TRADES} одн. | {MAX_TRADES_PER_DAY}/день | loss ${DAILY_LOSS_LIMIT}")
    log.info(f"Fees: maker={MEXC_MAKER_FEE_PCT}% taker={MEXC_TAKER_FEE_PCT}% slippage={SLIPPAGE_BUFFER_PCT}%")
    log.info(f"Anti-ban: curl_cffi | задержка {MIN_DELAY_OPEN}-{MAX_DELAY_OPEN}s")
    log.info(f"Gates: dry_run={DRY_RUN} auto_trade={AUTO_TRADE_ENABLED} signal_only={SIGNAL_ONLY}")
    log.info(f"MEXC data: WS={'✅' if MEXC_WS_ENABLED else '❌'} REST_fallback={'✅' if MEXC_REST_FALLBACK_ENABLED else '❌'}")
    log.info(f"DB: {STATE_DIR}/mexc-trades.sqlite3")
    log.info(f"TG: {'✅' if TG_ENABLED else '❌'} | API keys: {'✅' if API_KEY else '❌'}")
    if AUTO_MODE:
        log.info("🔥 РЕЖИМ: АВТО-ТРЕЙДИНГ")
    elif DRY_RUN:
        log.info("🧪 РЕЖИМ: DRY RUN (нет сделок)")
    else:
        log.info("📡 РЕЖИМ: СИГНАЛЫ (TG)")

    # Init DB
    get_db()
    log.info("✅ SQLite initialized")

    # Reconcile (P0.6)
    await reconcile_positions()

    for f in [SIGNAL_FILE, "/tmp/mexc-spread-alerts.log"]:
        open(f, "w").close()

    await asyncio.gather(
        binance_stream(),
        mexc_ws_stream(),
        mexc_rest_fallback(),
        check_signals(),
        health_reporter(),
    )

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("🛑")
