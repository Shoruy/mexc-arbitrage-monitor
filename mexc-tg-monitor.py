#!/usr/bin/env python3
"""MEXC-Binance Spread Monitor v7 — curl_cffi (Cloudflare bypass)"""

import asyncio, json, time, os, logging, hmac, hashlib, random
from datetime import datetime, date
from urllib.parse import urlencode
from curl_cffi import requests as curl_requests

for k in ['HTTP_PROXY','HTTPS_PROXY','http_proxy','https_proxy','ALL_PROXY','all_proxy']:
    os.environ.pop(k, None)

try:
    import websockets
except ImportError:
    import subprocess; subprocess.check_call(["pip","install","websockets"]); import websockets

# --- Конфиг ---
PAIRS = {"zecusdt":"ZECUSDT","xlmusdt":"XLMUSDT","solusdt":"SOLUSDT",
         "bchusdt":"BCHUSDT","btcusdt":"BTCUSDT","ethusdt":"ETHUSDT",
         "xrpusdt":"XRPUSDT","ltcusdt":"LTCUSDT"}

ENTRY_SPREAD = 0.12
TG_THRESHOLD = 0.12
ALERT_CD = 60
EXIT_TIMEOUT = 30
EXIT_SPREAD = 0.02

TRADE_AMOUNT_BASE = 7
TRADE_AMOUNT_JITTER = 1.0
TRADE_LEVERAGE = 20
MAX_MEXC_SPREAD = 0.05
MIN_LIQUIDITY = 1000

# --- Risk Control ---
MAX_CONCURRENT_TRADES = 1
MAX_TRADES_PER_DAY = 20
DAILY_LOSS_LIMIT = 3.0
MIN_DELAY_OPEN = 0.5
MAX_DELAY_OPEN = 2.0
SERIES_PAUSE_AFTER = 8
SERIES_PAUSE_SEC = 120

TG_BOT_TOKEN = os.getenv("MEXC_TG_BOT_TOKEN", "8754024491:AAGgZ14zGGZFcLZ4cphcHZP0VnnLnRznaiE")
TG_CHAT_ID = os.getenv("MEXC_TG_CHAT_ID", "1371329042")
BINANCE_WS = "wss://stream.binance.com:9443/ws"
SIGNAL_FILE = "/tmp/mexc-tg-signals.txt"

# --- API ключи ---
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

AUTO_MODE = bool(API_KEY and API_SECRET)

# --- curl_cffi helper (имитируем curl, а не Python) ---
def _curl(method, url, **kwargs):
    """curl_cffi-запрос с impersonate chrome131"""
    kwargs.setdefault('impersonate', 'chrome131')
    kwargs.setdefault('timeout', 10)
    kwargs.setdefault('verify', True)
    # Force HTTP/2 if supported (curl_cffi handles)
    try:
        if method.upper() == 'GET':
            return curl_requests.get(url, **kwargs)
        else:
            return curl_requests.post(url, **kwargs)
    except Exception as e:
        raise

# --- Состояние ---
binance_prices = {}
mexc_prices = {}
last_alert_ts = {}
active_trades = {}
exit_alert_ts = {}

_trade_count_today = 0
_today_date = date.today()
_daily_pnl = 0.0
_series_count = 0
_last_trade_time = 0
_consecutive_losses = 0

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s', datefmt='%H:%M:%S')
log = logging.getLogger('monitor')

# ========== MEXC API (V1 signature) ==========

def _param_string(method, params, body):
    if method == "GET":
        if params:
            return urlencode(sorted(params.items()))
        return ""
    else:
        if body:
            return json.dumps(body)
        return ""

def _mexc_sig(method, params, body, ts):
    ps = _param_string(method, params, body)
    raw = API_KEY + ts + ps
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
    headers = _mexc_headers(ts, sig)
    try:
        r = await asyncio.to_thread(_curl, method, url, headers=headers,
                                    json=body if method != "GET" else None,
                                    params=params if method == "GET" else None)
        ct = r.headers.get("Content-Type", "")
        if "json" not in ct:
            log.error(f"⚠️ API ({path}): {r.status_code} {ct} — {r.text[:200]}")
            return None
        return r.json()
    except Exception as e:
        log.error(f"⚠️ API ({path}): {e}")
        return None

async def mexc_set_leverage(sym_key):
    msym = PAIRS[sym_key]
    # v1 futures: symbol with underscore
    msym_v1 = msym[:-4] + "_" + msym[-4:]
    r = await mexc_api("POST", "/api/v1/private/position/change_leverage",
        body={"symbol": msym_v1, "openType": 1, "positionType": 1, "leverage": TRADE_LEVERAGE})
    if r and r.get("success"):
        log.info(f"⚙️ {msym} плечо {TRADE_LEVERAGE}x")
        return True
    log.warning(f"⚠️ {msym} leverage: {r}")
    return False

async def mexc_place_order(sym_key, direction, qty):
    msym = PAIRS[sym_key]
    msym_v1 = msym[:-4] + "_" + msym[-4:]
    order_side = 1 if direction == "LONG" else 3
    body = {
        "symbol": msym_v1, "price": 0, "vol": qty,
        "side": order_side, "type": 1, "openType": 1,
        "positionId": 0, "externalOid": f"hermes_{int(time.time())}"
    }
    r = await mexc_api("POST", "/api/v1/private/order/create", body=body)
    if r and r.get("success"):
        data = r.get("data", {})
        log.info(f"📈 {msym} {direction} #{data.get('orderId','?')} vol={qty}")
        return data
    log.warning(f"⚠️ {msym} order: {r}")
    return None

async def mexc_close_position(sym_key):
    msym = PAIRS[sym_key]
    r = await mexc_api("POST", "/api/v1/private/position/close_all", body={})
    if r and r.get("success"):
        log.info(f"📉 {msym} закрыто")
        return True
    log.warning(f"⚠️ {msym} close: {r}")
    return False

async def mexc_get_qty(sym_key):
    """Объём со случайным jitter (±$1)"""
    jitter = random.uniform(-TRADE_AMOUNT_JITTER, TRADE_AMOUNT_JITTER)
    amount = max(TRADE_AMOUNT_BASE + jitter, 5)
    msym = PAIRS[sym_key]
    msym_v1 = msym[:-4] + "_" + msym[-4:]
    try:
        # curl_cffi bypasses Cloudflare
        r = await asyncio.to_thread(_curl, "GET",
            f"https://contract.mexc.com/api/v1/contract/detail?symbol={msym_v1}")
        if r.status_code != 200:
            log.warning(f"⚠️ contract/detail {msym_v1}: {r.status_code}")
            return 1
        d = r.json()
        if d.get("success") and d.get("data"):
            cs = float(d["data"]["contractSize"])
            price = mexc_prices.get(sym_key, {}).get("ask", 0) or mexc_prices.get(sym_key, {}).get("bid", 0)
            if price and cs and price > 0:
                qty = int((amount * TRADE_LEVERAGE) / (price * cs))
                qty = max(qty, 1)
                log.info(f"  {msym} qty={qty} (${amount}×{TRADE_LEVERAGE}x / ${price}×{cs})")
                return qty
    except Exception as e:
        log.warning(f"⚠️ contract/detail {msym_v1}: {e}")
    return 1

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

# ========== Вспомогательное ==========

def mexc_link(sym):
    usym = sym[:-4] + "_" + sym[-4:] if len(sym) > 4 and "_" not in sym else sym
    return f"https://futures.mexc.com/exchange/{usym}"

def fmt_entry(sym, sp, dr, bm, mm, auto=False, amount=7):
    emoji = "🟢" if dr == "SHORT" else "🔵"
    pos_size = amount * TRADE_LEVERAGE
    side_ru = "SELL (Short)" if dr == "SHORT" else "BUY (Long)"
    pnl_pct = abs(sp) * TRADE_LEVERAGE
    mode = "🤖 Авто" if auto else "🛠 Ручной"
    status = "Ордер выставлен!" if auto else "Жди сигнала на выход"
    return (f"{emoji} *{sym}* — спред *{abs(sp):.2f}%* ({mode})\n"
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
    mode = "🤖 Закрыто автоматом" if auto else "🟢 Закрывай вручную"
    return (f"🟢 *ВЫХОД {sym}*\n"
            f"Спред схлопнулся! {mode}\n\n"
            f"Вход: ${entry_mm:.4f}\n"
            f"Binance: `${cur_bm:.4f}`\n"
            f"MEXC:    `${cur_mm:.4f}`\n\n"
            f"💵 *Расчёт:* ~${pnl_usd:.2f} ({pnl_pct:.1f}% к депозиту)\n"
            f"[👉 Закрыть {sym} на MEXC]({mexc_link(sym)})")

async def send_tg(text):
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

# ========== Фильтр ==========

async def check_liquidity(sym_key, direction):
    try:
        msym = PAIRS[sym_key]
        r = await asyncio.to_thread(_curl, "GET",
            f"https://api.mexc.com/api/v3/depth?symbol={msym}&limit=20")
        if r.status_code != 200:
            return False, f"depth {r.status_code}"
        data = r.json()
        bids = [(float(p), float(q)) for p, q in data.get('bids', [])]
        asks = [(float(p), float(q)) for p, q in data.get('asks', [])]
        if not bids or not asks:
            return False, "empty book"
        mxc_sp = (asks[0][0] - bids[0][0]) / asks[0][0] * 100
        if mxc_sp > MAX_MEXC_SPREAD:
            return False, f"spread {mxc_sp:.3f}%"
        cum_liq = sum(q * p for p, q in asks) if direction == "LONG" else sum(q * p for p, q in bids)
        if cum_liq < MIN_LIQUIDITY:
            return False, f"liq ${cum_liq:.0f}"
        return True, f"✅ liq=${cum_liq:.0f} spread={mxc_sp:.3f}%"
    except Exception as e:
        return False, str(e)

# ========== Потоки ==========

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

async def mexc_poller():
    while True:
        try:
            tasks = []
            for key, msym in PAIRS.items():
                tasks.append(asyncio.to_thread(_curl, "GET",
                    f"https://api.mexc.com/api/v3/ticker/bookTicker?symbol={msym}"))
            responses = await asyncio.gather(*tasks, return_exceptions=True)
            for key, resp in zip(PAIRS.keys(), responses):
                if isinstance(resp, Exception):
                    continue
                if resp.status_code == 200:
                    d = resp.json()
                    mexc_prices[key] = {"bid": float(d["bidPrice"]), "ask": float(d["askPrice"])}
        except Exception as e:
            log.error(f"⚠️ MEXC: {e}")
        await asyncio.sleep(1)

# ========== Ядро ==========

async def check_signals():
    while True:
        await asyncio.sleep(0.3)
        now = time.time()

        # --- Выход ---
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
                expired.append(sym)
        for sym in expired:
            active_trades.pop(sym, None)

        # --- Вход ---
        for sym in PAIRS:
            bp = binance_prices.get(sym)
            mp = mexc_prices.get(sym)
            if not bp or not mp: continue
            bm = (bp["bid"] + bp["ask"]) / 2
            mm = (mp["bid"] + mp["ask"]) / 2
            if bm == 0: continue
            sp = (mm - bm) / bm * 100
            absp = abs(sp)
            if sym in active_trades: continue

            if absp >= ENTRY_SPREAD:
                last = last_alert_ts.get(sym, 0)
                if now - last >= ALERT_CD:
                    last_alert_ts[sym] = now
                    dr = "SHORT" if mm > bm else "LONG"

                    valid, reason = await check_liquidity(sym, dr)
                    if not valid:
                        log.info(f"⏭ {sym.upper()} {absp:.2f}% {dr} — {reason}")
                        continue

                    rc_ok, rc_reason = risk_check()
                    if not rc_ok:
                        log.info(f"⏭ {sym.upper()} {absp:.2f}% {dr} — RC: {rc_reason}")
                        continue

                    jitter = random.uniform(-TRADE_AMOUNT_JITTER, TRADE_AMOUNT_JITTER)
                    amount = max(round(TRADE_AMOUNT_BASE + jitter, 1), 5.0)

                    order_placed = False
                    if AUTO_MODE and absp >= TG_THRESHOLD:
                        delay = random.uniform(MIN_DELAY_OPEN, MAX_DELAY_OPEN)
                        log.info(f"⏳ {sym.upper()} задержка {delay:.1f}s")
                        await asyncio.sleep(delay)
                        await mexc_set_leverage(sym)
                        qty = await mexc_get_qty(sym)
                        order = await mexc_place_order(sym, dr, qty)
                        if order:
                            order_placed = True

                    msg = fmt_entry(sym.upper(), sp, dr, bm, mm, auto=order_placed, amount=amount)
                    mode_str = 'AUTO' if order_placed else 'SIGNAL'
                    with open(SIGNAL_FILE, "a") as f:
                        f.write(json.dumps({"t": now, "sym": sym.upper(), "msg": msg}) + "\n")
                    with open("/tmp/mexc-spread-alerts.log", "a") as f:
                        f.write(f"{datetime.now().isoformat()} | {sym.upper()} | {absp:.3f}% | {dr} | {mode_str} | ${amount:.0f} | {reason}\n")
                    log.info(f"{'🤖' if order_placed else '🚨'} {sym.upper()} {absp:.2f}% {dr} ${amount:.0f}")
                    print(f"\n{msg}\n", flush=True)

                    if absp >= TG_THRESHOLD:
                        await send_tg(msg)
                        active_trades[sym] = {
                            'entry_time': now, 'entry_sp': absp,
                            'direction': dr, 'entry_bm': bm, 'entry_mm': mm,
                            'order_placed': order_placed, 'amount': amount
                        }

async def main():
    _check_daily()
    log.info("🚀 MEXC-Binance Monitor v7 (curl_cffi)")
    log.info(f"Пары: {', '.join(PAIRS)} | Вход ≥{ENTRY_SPREAD}% | TG ≥{TG_THRESHOLD}%")
    log.info(f"Фильтры: спред ≤{MAX_MEXC_SPREAD}% | ликв. ≥${MIN_LIQUIDITY}")
    log.info(f"Параметры: ${TRADE_AMOUNT_BASE}±${TRADE_AMOUNT_JITTER} × {TRADE_LEVERAGE}x")
    log.info(f"RC: {MAX_CONCURRENT_TRADES} одн. | {MAX_TRADES_PER_DAY}/день | loss ${DAILY_LOSS_LIMIT}")
    log.info(f"Anti-ban: curl_cffi (Cloudflare bypass) | задержка {MIN_DELAY_OPEN}-{MAX_DELAY_OPEN}s")
    if AUTO_MODE:
        log.info("🔥 РЕЖИМ: АВТО-ТРЕЙДИНГ")
    else:
        log.info("📡 РЕЖИМ: СИГНАЛЫ (TG)")
    for f in [SIGNAL_FILE, "/tmp/mexc-spread-alerts.log"]:
        open(f, "w").close()
    await asyncio.gather(binance_stream(), mexc_poller(), check_signals())

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("🛑")
