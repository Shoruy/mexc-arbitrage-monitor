#!/usr/bin/env python3
"""MEXC-Binance Spread Monitor — Telegram алерты при расхождении цен"""

import asyncio
import json
import time
import os
import signal
import logging
from datetime import datetime
from decimal import Decimal

# Clear proxy env vars — WebSocket connection to exchanges needs direct access
for key in ['HTTP_PROXY', 'HTTPS_PROXY', 'http_proxy', 'https_proxy', 'ALL_PROXY', 'all_proxy']:
    os.environ.pop(key, None)

try:
    import websockets
except ImportError:
    import subprocess
    subprocess.check_call(["pip", "install", "websockets"])
    import websockets

# ─── Config ───────────────────────────────────────────────────────────────

# Пары для мониторинга (Binance символ → MEXC символ)
PAIRS = {
    "btcusdt": "BTCUSDT",
    "ethusdt": "ETHUSDT",
    "xlmusdt": "XLMUSDT",
    "bchusdt": "BCHUSDT",
    "zecusdt": "ZECUSDT",
    "solusdt": "SOLUSDT",
    "xrpusdt": "XRPUSDT",
    "ltcusdt": "LTCUSDT",
}

# Порог срабатывания в %
SPREAD_THRESHOLD = 0.05  # 0.05%

# Cooldown между алертами по одной паре (сек)
ALERT_COOLDOWN = 30

# Binance WebSocket
BINANCE_WS = "wss://stream.binance.com:9443/ws"

# ─── State ────────────────────────────────────────────────────────────────

binance_prices = {}    # {symbol: {"bid": float, "ask": float, "time": float}}
mexc_prices = {}       # {symbol: {"bid": float, "ask": float, "time": float}}
last_alert = {}        # {symbol: float} — timestamp last alert
spread_log = []        # [(ts, symbol, spread, direction)]

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%H:%M:%S'
)
log = logging.getLogger(__name__)

# ─── Binance WebSocket ───────────────────────────────────────────────────

async def binance_stream():
    """Подписка на bookTicker всех пар с Binance"""
    streams = [f"{pair}@bookTicker" for pair in PAIRS.keys()]
    url = f"{BINANCE_WS}/{'/'.join(streams)}"
    
    while True:
        try:
            async with websockets.connect(url, ping_interval=20) as ws:
                log.info(f"✅ Binance WS connected ({len(streams)} pairs)")
                async for msg in ws:
                    data = json.loads(msg)
                    symbol = data["s"].lower()
                    if symbol in PAIRS:
                        binance_prices[symbol] = {
                            "bid": float(data["b"]),
                            "ask": float(data["a"]),
                            "time": time.time(),
                        }
                        # Проверяем спред при каждом обновлении с Binance
                        await check_spread(symbol)
        except Exception as e:
            log.error(f"⚠️ Binance WS error: {e}")
            await asyncio.sleep(3)

# ─── MEXC REST API (fallback — WS blocked from this IP) ─────────────────

async def mexc_poller():
    """Poll MEXC REST API for book ticker prices every 2 seconds"""
    import aiohttp
    
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                for key, mex_symbol in PAIRS.items():
                    url = f"https://api.mexc.com/api/v3/ticker/bookTicker?symbol={mex_symbol}"
                    async with session.get(url, timeout=5) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            # Store with same key as binance_prices (lowercase)
                            mexc_prices[key] = {
                                "bid": float(data["bidPrice"]),
                                "ask": float(data["askPrice"]),
                                "time": time.time(),
                            }
                            await check_spread(key)
            except Exception as e:
                log.error(f"⚠️ MEXC poll error: {e}")
            
            await asyncio.sleep(2)

# ─── Spread Logic ─────────────────────────────────────────────────────────

async def check_spread(symbol: str):
    """Проверяет спред по символу, отправляет алерт если порог превышен"""
    bp = binance_prices.get(symbol)
    mp = mexc_prices.get(symbol)
    
    if not bp or not mp:
        return
    
    # Используем mid-price (среднее между bid и ask) для сравнения
    binance_mid = (bp["bid"] + bp["ask"]) / 2
    mexc_mid = (mp["bid"] + mp["ask"]) / 2
    
    if binance_mid == 0:
        return
    
    # Спред в %
    spread_pct = abs(mexc_mid - binance_mid) / binance_mid * 100
    
    # Направление: где цена выше
    if mexc_mid > binance_mid:
        direction = "MEXC > Binance"  # Шорт на MEXC
    else:
        direction = "Binance > MEXC"  # Лонг на MEXC
    
    # Логируем каждое событие (раз в 30 сек на пару — только изменения)
    now = time.time()
    
    # Проверяем порог и cooldown
    if spread_pct >= SPREAD_THRESHOLD:
        last = last_alert.get(symbol, 0)
        if now - last >= ALERT_COOLDOWN:
            last_alert[symbol] = now
            
            msg = (
                f"🚨 *СПРЕД {symbol.upper()}* 🚨\n"
                f"│ Binance: `${binance_mid:.4f}`\n"
                f"│ MEXC:    `${mexc_mid:.4f}`\n"
                f"│ Разница: *{spread_pct:.2f}%*\n"
                f"│ {direction}\n"
                f"│ Binance bid/ask: {bp['bid']:.4f}/{bp['ask']:.4f}\n"
                f"│ MEXC bid/ask:    {mp['bid']:.4f}/{mp['ask']:.4f}"
            )
            
            # Пишем в файл лога
            log_line = f"{datetime.now().isoformat()} | {symbol.upper()} | {spread_pct:.2f}% | {direction} | B:{binance_mid:.4f} M:{mexc_mid:.4f}"
            spread_log.append((time.time(), symbol, spread_pct, direction))
            
            with open("/tmp/mexc-spread-alerts.log", "a") as f:
                f.write(log_line + "\n")
            
            # Выводим в консоль
            log.info(f"SIGNAL: {symbol.upper()} spread={spread_pct:.2f}% {direction}")
            print(f"\n{msg}\n", flush=True)

# ─── Console Reporter ─────────────────────────────────────────────────────

async def reporter():
    """Выводит состояние каждые 30 секунд"""
    while True:
        await asyncio.sleep(30)
        lines = [f"\n{'='*50}", f"📊 Состояние ({datetime.now().strftime('%H:%M:%S')})"]
        
        for symbol in PAIRS:
            bp = binance_prices.get(symbol)
            mp = mexc_prices.get(symbol)
            
            if bp and mp:
                bm = (bp["bid"] + bp["ask"]) / 2
                mm = (mp["bid"] + mp["ask"]) / 2
                sp = abs(mm - bm) / bm * 100 if bm else 0
                marker = " ⚡" if sp >= SPREAD_THRESHOLD else ""
                lines.append(f"│ {symbol.upper():8s}  B:{bm:.4f}  M:{mm:.4f}  спред:{sp:.3f}%{marker}")
            else:
                b_ok = "✓" if bp else "✗"
                m_ok = "✓" if mp else "✗"
                lines.append(f"│ {symbol.upper():8s}  Binance[{b_ok}]  MEXC[{m_ok}]")
        
        lines.append(f"│ Всего алертов: {len([x for x in spread_log if x[0] > time.time() - 3600])} за час")
        lines.append(f"{'='*50}\n")
        print("\n".join(lines), flush=True)

# ─── Main ─────────────────────────────────────────────────────────────────

async def main():
    log.info("🚀 MEXC-Binance Spread Monitor — запуск")
    log.info(f"Пары: {', '.join(PAIRS.keys())}")
    log.info(f"Порог спреда: {SPREAD_THRESHOLD}%")
    log.info(f"Cooldown: {ALERT_COOLDOWN}s")
    
    # Запускаем всё параллельно
    await asyncio.gather(
        binance_stream(),
        mexc_poller(),
        reporter(),
    )

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("🛑 Монитор остановлен")
        print(f"\nВсего алертов за сессию: {len(spread_log)}")
        for ts, sym, sp, dr in spread_log[-10:]:
            print(f"  {datetime.fromtimestamp(ts).strftime('%H:%M:%S')} {sym} {sp:.2f}% {dr}")
