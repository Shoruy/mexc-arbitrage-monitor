#!/usr/bin/env python3
"""MEXC-Binance Spread Monitor + Virtual Trade Simulation"""
import asyncio, json, time, os, logging
from datetime import datetime

for k in ['HTTP_PROXY','HTTPS_PROXY','http_proxy','https_proxy','ALL_PROXY','all_proxy']:
    os.environ.pop(k, None)

try:
    import websockets
except ImportError:
    import subprocess; subprocess.check_call(["pip","install","websockets"]); import websockets

# ─── Config ────────────────────────────────────────────────────────────────
PAIRS = {
    "btcusdt":"BTCUSDT","ethusdt":"ETHUSDT","xlmusdt":"XLMUSDT",
    "bchusdt":"BCHUSDT","zecusdt":"ZECUSDT","solusdt":"SOLUSDT",
    "xrpusdt":"XRPUSDT","ltcusdt":"LTCUSDT",
}
ENTRY_SPREAD = 0.04   # Вход при спреде >= 0.04%
EXIT_SPREAD  = 0.01   # Выход при спреде <= 0.01%
ALERT_CD     = 30      # Cooldown алертов (сек)
LEVERAGE     = 100     # Плечо для расчёта PnL
POSITION_USD = 10      # Размер позиции в $
MAX_HOLD     = 30      # Макс удержание позиции (сек) — стоп по времени
STOP_LOSS    = 0.20    # Стоп-лосс по модулю спреда (%) — принудительный выход
MIN_TRADE_SEC = 2      # Минимальная длительность сделки (сек) — фильтр флип-флопа
MAX_LEVERAGE = {        # Макс плечо на MEXC по каждой паре
    "btcusdt": 100, "ethusdt": 100, "xlmusdt": 50,
    "bchusdt": 50, "zecusdt": 50, "solusdt": 50,
    "xrpusdt": 50, "ltcusdt": 50,
}

BINANCE_WS = "wss://stream.binance.com:9443/ws"

# ─── State ─────────────────────────────────────────────────────────────────
binance_prices = {}
mexc_prices = {}
last_alert_ts = {}
spread_signals = []    # Для алертов
trades = []            # Завершённые виртуальные сделки
open_positions = {}    # {symbol: {entry_time, entry_spread, direction, binance_entry, mexc_entry}}
last_close = {}        # {symbol: timestamp} — анти-флипфлоп

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s', datefmt='%H:%M:%S')
log = logging.getLogger('spread-monitor')

# ─── Binance WebSocket ─────────────────────────────────────────────────────
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
                        binance_prices[s] = {"bid": float(d["b"]), "ask": float(d["a"]), "time": time.time()}
                        await process(s)
        except Exception as e:
            log.error(f"⚠️ Binance WS: {e}")
            await asyncio.sleep(3)

# ─── MEXC REST ─────────────────────────────────────────────────────────────
async def mexc_poller():
    import aiohttp
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                for key, mex_sym in PAIRS.items():
                    r = await session.get(f"https://api.mexc.com/api/v3/ticker/bookTicker?symbol={mex_sym}", timeout=5)
                    if r.status == 200:
                        d = await r.json()
                        mexc_prices[key] = {"bid": float(d["bidPrice"]), "ask": float(d["askPrice"]), "time": time.time()}
                        await process(key)
            except Exception as e:
                log.error(f"⚠️ MEXC: {e}")
            await asyncio.sleep(2)

# ─── Core Logic: spread + simulation ───────────────────────────────────────
async def process(symbol: str):
    bp = binance_prices.get(symbol)
    mp = mexc_prices.get(symbol)
    if not bp or not mp: return

    bm = (bp["bid"] + bp["ask"]) / 2
    mm = (mp["bid"] + mp["ask"]) / 2
    if bm == 0: return

    spread = (mm - bm) / bm * 100
    direction = "SHORT" if mm > bm else "LONG"
    abs_spread = abs(spread)
    now = time.time()

    # ── Signal / Alert ──
    if abs_spread >= ENTRY_SPREAD:
        last = last_alert_ts.get(symbol, 0)
        if now - last >= ALERT_CD:
            last_alert_ts[symbol] = now
            spread_signals.append((now, symbol, spread, direction))
            sig = f"🚨 СИГНАЛ {symbol.upper()} | спред {abs_spread:.3f}% | {direction} | B:{bm:.4f} M:{mm:.4f}"
            log.info(sig)
            with open("/tmp/mexc-spread-alerts.log", "a") as f:
                f.write(f"{datetime.now().isoformat()} | {symbol.upper()} | {abs_spread:.3f}% | {direction} | B:{bm:.4f} M:{mm:.4f}\n")
            print(f"\n{sig}\n", flush=True)

    # ── Simulation: ENTER ──
    pos = open_positions.get(symbol)
    recently_closed = last_close.get(symbol, 0)
    if pos is None and abs_spread >= ENTRY_SPREAD and (now - recently_closed) > MIN_TRADE_SEC:
        # Используем корректное плечо для пары
        lev = MAX_LEVERAGE.get(symbol, LEVERAGE)
        open_positions[symbol] = {
            "entry_time": now,
            "entry_spread": spread,
            "direction": direction,
            "bm_entry": bm,
            "mm_entry": mm,
            "leverage": lev,
        }
        print(f"📌 {symbol.upper()} → ПОЗИЦИЯ OPEN {direction} @ спред {spread:.3f}% ({lev}x) (B:{bm:.4f} M:{mm:.4f})", flush=True)

    # ── Simulation: EXIT ──
    elif pos is not None:
        should_exit = False
        exit_reason = ""
        hold_time = now - pos["entry_time"]

        # Стоп по времени
        if hold_time >= MAX_HOLD:
            should_exit = True
            exit_reason = f"max_hold ({hold_time:.0f}с)"
        # Спред схлопнулся
        elif abs_spread <= EXIT_SPREAD:
            should_exit = True
            exit_reason = f"спред схлопнулся ({abs_spread:.3f}%)"
        # Направление сменилось
        elif (pos["direction"] == "SHORT" and mm < bm) or (pos["direction"] == "LONG" and mm > bm):
            should_exit = True
            exit_reason = f"направление сменилось (спред {spread:.3f}%)"
        # Стоп-лосс: спред расширился дальше порога
        elif abs_spread >= STOP_LOSS:
            # Проверяем что движение НЕ в нашу пользу
            if (pos["direction"] == "SHORT" and spread > pos["entry_spread"]) or \
               (pos["direction"] == "LONG" and spread < pos["entry_spread"]):
                should_exit = True
                exit_reason = f"стоп-лосс (спред {abs_spread:.3f}%)"

        if should_exit:
            lev = pos.get("leverage", LEVERAGE)
            # PnL: правильная формула со знаком
            if pos["direction"] == "SHORT":
                spread_moved = pos["entry_spread"] - spread
            else:  # LONG
                spread_moved = spread - pos["entry_spread"]

            pnl_pct = spread_moved * lev
            pnl_usd = pnl_pct / 100 * POSITION_USD

            trade = {
                "symbol": symbol.upper(),
                "direction": pos["direction"],
                "entry_time": datetime.fromtimestamp(pos["entry_time"]).strftime('%H:%M:%S'),
                "exit_time": datetime.fromtimestamp(now).strftime('%H:%M:%S'),
                "hold_sec": round(hold_time, 1),
                "entry_spread": round(pos["entry_spread"], 3),
                "exit_spread": round(spread, 3),
                "spread_moved": round(spread_moved, 3),
                "pnl_pct": round(pnl_pct, 2),
                "pnl_usd": round(pnl_usd, 2),
            }
            trades.append(trade)
            last_close[symbol] = now  # анти-флипфлоп
            del open_positions[symbol]

            emoji = "🟢" if pnl_usd > 0 else "🔴"
            msg = (f"{emoji} {symbol.upper()} → СДЕЛКА ЗАКРЫТА | "
                   f"{pos['direction']} | {exit_reason} | "
                   f"держали {hold_time:.0f}с | "
                   f"PnL: {pnl_pct:+.2f}% (${pnl_usd:+.2f})")
            log.info(msg)
            print(f"\n{msg}\n", flush=True)

            # Запись в лог сделок
            with open("/tmp/mexc-simulation-trades.log", "a") as f:
                f.write(f"{datetime.now().isoformat()} | {trade['symbol']} | {trade['direction']} | "
                        f"вход:{trade['entry_time']} выход:{trade['exit_time']} "
                        f"спред:{trade['entry_spread']}%→{trade['exit_spread']}% | "
                        f"PnL:{trade['pnl_pct']:+.2f}% ${trade['pnl_usd']:+.2f} | {exit_reason}\n")

# ─── Reporter ──────────────────────────────────────────────────────────────
async def reporter():
    while True:
        await asyncio.sleep(15)
        now = time.time()
        lines = [f"\n{'='*55}", f"{datetime.now().strftime('%H:%M:%S')} — 📊 ДЭШБОРД"]

        for sym in PAIRS:
            bp = binance_prices.get(sym)
            mp = mexc_prices.get(sym)
            pos = open_positions.get(sym)
            
            if bp and mp:
                bm = (bp["bid"]+bp["ask"])/2
                mm = (mp["bid"]+mp["ask"])/2
                sp = (mm-bm)/bm*100
                pos_mark = " ⚡В ПОЗИЦИИ" if pos else ""
                sig_mark = " ⚠️" if abs(sp) >= ENTRY_SPREAD else ""
                lines.append(f"{sym.upper():8s} B:{bm:.4f} M:{mm:.4f} спред:{sp:+.3f}%{sig_mark}{pos_mark}")
            else:
                lines.append(f"{sym.upper():8s} B[{'✓' if bp else '✗'}] M[{'✓' if mp else '✗'}]")

        # Сводка по сделкам
        last_hour = [t for t in trades if t["entry_time"] > datetime.fromtimestamp(now-3600).strftime('%H:%M:%S')]
        total_trades = len(trades)
        if total_trades:
            wins = sum(1 for t in trades if t["pnl_usd"] > 0)
            total_pnl = sum(t["pnl_usd"] for t in trades)
            lines.append(f"│ Сделок: {total_trades} (wins:{wins} loss:{total_trades-wins}) "
                         f"PnL: ${total_pnl:+.2f} | В позиции: {len(open_positions)}")

        lines.append(f"{'='*55}\n")
        print("\n".join(lines), flush=True)

# ─── Main ──────────────────────────────────────────────────────────────────
async def main():
    log.info("🚀 MEXC-Binance + SIMULATION")
    log.info(f"Пары: {', '.join(PAIRS)} | Вход: ≥{ENTRY_SPREAD}% Выход: ≤{EXIT_SPREAD}% "
             f"Плечо:{LEVERAGE}x Позиция:${POSITION_USD}")
    
    # Очищаем логи при старте
    for f in ["/tmp/mexc-spread-alerts.log", "/tmp/mexc-simulation-trades.log"]:
        open(f, "w").close()

    await asyncio.gather(binance_stream(), mexc_poller(), reporter())

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("🛑 Остановлен")
        print(f"\nИтого сделок: {len(trades)}")
        wins = sum(1 for t in trades if t["pnl_usd"] > 0)
        total_pnl = sum(t["pnl_usd"] for t in trades)
        print(f"Прибыльных: {wins} | Убыточных: {len(trades)-wins}")
        print(f"Общий PnL: ${total_pnl:+.2f}")
        if trades:
            print(f"\nПоследние 5 сделок:")
            for t in trades[-5:]:
                print(f"  {t['symbol']} {t['direction']} | "
                      f"спред {t['entry_spread']}%→{t['exit_spread']}% | "
                      f"PnL:{t['pnl_pct']:+.2f}% ${t['pnl_usd']:+.2f}")
        print(f"\nЛоги: /tmp/mexc-simulation-trades.log")
        print(f"Алерты: /tmp/mexc-spread-alerts.log")
