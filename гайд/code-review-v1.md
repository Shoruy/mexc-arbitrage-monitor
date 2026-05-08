# Code Review v1: MEXC-Binance Spread Monitor v8-p0

Date: 2026-05-08  
Reviewed files:
- `/root/obsidian/tldr/проекты/mexc/mexc-tg-monitor.py`
- `/root/obsidian/tldr/проекты/mexc/гайд/plan-codex-v1.md`
- `/root/obsidian/tldr/проекты/mexc/гайд/mexc-api-reference.md`
- `/etc/systemd/system/mexc-monitor.service`

External reference checked:
- Official MEXC contract API docs: https://mexcdevelop.github.io/apidocs/contract_v1_en/

## Verdict

Not ready for live auto-trading. It is acceptable for production dry-run/signal collection with caveats, but the collected data will be incomplete until stale-book checks, futures-depth liquidity, and dry-run signal classification are fixed.

Main blocker: the P0.5 implementation is probably using the wrong MEXC order type and definitely checks the wrong order-status field. The official docs list `type=2` as Post Only Maker and `type=5` as market order. They also return order state as `state`, not `status`, for `GET /api/v1/private/order/get/{order_id}`. Current code defaults `MEXC_LIMIT_POST_ONLY_TYPE=5` and polls `status.get("status")`, so a live run can place market orders while believing they are post-only, then fail to recognize fills.

## Findings

### Critical: `type=5` is documented as market, not post-only

Lines: `mexc-tg-monitor.py:65`, `mexc-tg-monitor.py:362-367`, `mexc-tg-monitor.py:856-858`  
Plan lines: `plan-codex-v1.md:486-491`, `plan-codex-v1.md:502-514`, `plan-codex-v1.md:1426`

The code sets:

```python
LIMIT_POST_ONLY_TYPE = int(os.getenv("MEXC_LIMIT_POST_ONLY_TYPE", "5"))
```

and sends this as the entry order type. The official MEXC contract docs for the maintained `order/submit` endpoint define order type as:

- `1`: price limited order
- `2`: Post Only Maker
- `3`: transact or cancel instantly
- `4`: transact completely or cancel completely
- `5`: market orders
- `6`: convert market price to current price

This contradicts the current implementation and the local docs/plan assumption. Even if `/api/v1/private/order/create` is a web/private endpoint with a slightly different shape, this must be proven before live mode. Right now the default is unsafe because it may create market entries.

Recommendation:
- Set default `MEXC_LIMIT_POST_ONLY_TYPE=2`, or better, refuse live auto mode unless a probe has written a verified order-type mapping.
- Add a startup guard: if `AUTO_MODE` and `LIMIT_POST_ONLY_TYPE == 5`, abort unless `MEXC_ALLOW_UNVERIFIED_ORDER_TYPE=1`.
- Update `гайд/mexc-api-reference.md` with the actual verified `/order/create` type mapping.

### Critical: Fill polling checks `status`, but MEXC order detail uses `state`

Lines: `mexc-tg-monitor.py:391-397`, `mexc-tg-monitor.py:860-885`

The official order detail response uses:

```json
{
  "state": 3,
  "dealAvgPrice": 1208.35,
  "dealVol": 1
}
```

where state is:

- `1`: uninformed
- `2`: uncompleted
- `3`: completed
- `4`: cancelled
- `5`: invalid

The code checks:

```python
if status.get("status") == 2:
```

This means the bot will not detect filled orders if MEXC returns the documented shape. It will treat the filled order as unknown, attempt cancel, fail or no-op, and never add `active_trades`. That creates an unmanaged live position.

Recommendation:
- Normalize order detail into internal fields: `state`, `dealVol`, `dealAvgPrice`, `remainingVol`.
- Treat `state == 3` or `dealVol >= vol` as filled.
- Treat `state == 2` with `dealVol > 0` as partial fill and immediately reconcile/close or manage the partial position. Do not simply cancel and continue.
- Store both `price` and `dealAvgPrice`; `price` is the limit price, not necessarily the fill price.

### Critical: Partial fills are unmanaged

Lines: `mexc-tg-monitor.py:878-892`

The comment says `status in (3, 4)` is “cancelled / partial”, but partial fills are not handled. If an order partially fills before timeout, the code cancels the remainder and then records `timeout_cancel` without opening an `active_trades` entry or closing the partial position.

Recommendation:
- After every cancel, re-fetch order detail and/or open positions.
- If `dealVol > 0`, create a trade for the filled volume or immediately close it with explicit close logic.
- Add a DB status such as `partial_cancelled_position_open` so the bot cannot hide this state.

### Critical: `close_all` is not symbol-specific and can close unrelated positions

Lines: `mexc-tg-monitor.py:399-406`, `mexc-tg-monitor.py:767-789`, `mexc-tg-monitor.py:472-475`

`mexc_close_position(sym_key_val)` ignores `sym_key_val` and calls:

```python
POST /api/v1/private/position/close_all
body={}
```

This can close every open MEXC position in the account, not just the bot-managed symbol. It is especially dangerous during startup reconciliation when `MEXC_CLOSE_UNKNOWN_ON_STARTUP=1`.

Recommendation:
- Do not use `close_all` for normal exit.
- Implement explicit close orders by symbol, side, position ID, and volume:
  - close long: side `4`
  - close short: side `2`
- Only keep `close_all` as a manually enabled emergency kill switch with a very explicit env var.

### High: Startup reconciliation does not restore known open DB trades

Lines: `mexc-tg-monitor.py:440-480`  
Plan lines: `plan-codex-v1.md:607-615`

The plan required restoring `active_trades` when MEXC open positions match DB open trades. Current code logs a match but does not repopulate `active_trades`. After a restart with a known open position, the bot will not run exit logic for that position and can open new positions if risk limits allow.

There is also a symbol-case mismatch:

```python
db_open = {row["symbol"]: dict(row) for row in cur.fetchall()}
sym_clean = sym.replace("_", "").lower()
if sym_clean in db_open:
```

DB stores `SOLUSDT`; `sym_clean` is `solusdt`, so matches will fail unless DB symbols are normalized.

Recommendation:
- Key DB open trades by `row["symbol"].replace("_", "").lower()`.
- Restore `active_trades` with enough fields for exit logic: `entry_time`, `entry_sp`, `direction`, `entry_bm`, `entry_mm`, `order_placed`, `amount`.
- If required fields are missing, pause trading instead of just logging.

### High: MEXC WebSocket enable flag is ignored

Lines: `mexc-tg-monitor.py:27`, `mexc-tg-monitor.py:961-967`

`MEXC_WS_ENABLED` is logged but not used. `mexc_ws_stream()` is always started. If the operator sets `MEXC_WS_ENABLED=0`, the bot still mutates proxy env vars and connects to MEXC WS.

Recommendation:
- In `main()`, include `mexc_ws_stream()` only when `MEXC_WS_ENABLED`.
- If disabled, rely on REST fallback only.

### High: WebSocket proxy handling mutates process-global environment

Lines: `mexc-tg-monitor.py:10-14`, `mexc-tg-monitor.py:648-654`

The code removes HTTP/HTTPS proxy env vars at import time, then later restores `HTTP_PROXY`, `HTTPS_PROXY`, and `ALL_PROXY` globally before MEXC WS connects. This affects all concurrent tasks, including Binance WS, Telegram, and `curl_cffi` REST. It also permanently removes `NO_PROXY`.

This fixes the immediate `NO_PROXY=*` conflict, but it is not isolated or safe.

Recommendation:
- Prefer passing proxy settings to the websocket client directly if the installed `websockets` version supports it.
- If env mutation is unavoidable, do it before starting tasks and treat it as process configuration, not inside a reconnect loop.
- Log only whether a proxy is present, never its URL, because proxy URLs can contain credentials.
- Preserve/restore previous env values around the connection attempt if other tasks need different proxy behavior.

### High: Binance combined stream URL is likely wrong

Lines: `mexc-tg-monitor.py:77`, `mexc-tg-monitor.py:627-638`

The code builds:

```python
wss://stream.binance.com:9443/ws/zecusdt@bookTicker/xlmusdt@bookTicker/...
```

Binance combined streams usually use `/stream?streams=...`, while `/ws/<streamName>` is for a single raw stream. If this happens to connect, message shape may differ; combined streams return `{"stream": "...", "data": {...}}`, not top-level `s`, `b`, `a`.

Recommendation:
- Either connect one stream per symbol or use:
  - `wss://stream.binance.com:9443/stream?streams=zecusdt@bookTicker/...`
- Parse both raw and combined message shapes.
- Add `ts` to `binance_prices` for stale checks.

### High: Signal engine does not reject stale market data

Lines: `mexc-tg-monitor.py:673-678`, `mexc-tg-monitor.py:752-812`

MEXC prices have `ts`, but Binance prices do not. Entry and exit logic do not check freshness. A stale Binance or MEXC quote can create false edge and false exits.

Recommendation:
- Store `ts` for Binance updates.
- Before signal/exit, require both exchanges to be fresh, e.g. `< 2s` for WS and `< MEXC_WS_STALE_SEC` for fallback.
- Record stale rejections in `signals` once above threshold would otherwise trigger.

### High: Liquidity filter uses MEXC spot depth for futures execution

Lines: `mexc-tg-monitor.py:601-621`

The bot trades MEXC futures but checks:

```python
https://api.mexc.com/api/v3/depth?symbol={msym}&limit=20
```

That is spot depth, not futures depth. It can approve trades where futures book liquidity is thin or reject trades where spot is thin but futures is fine.

Recommendation:
- Use `https://contract.mexc.com/api/v1/contract/depth/{symbol}` for futures.
- Convert contract volumes using `contractSize`.
- Add hole checks near best bid/ask before P1.5 threshold changes.

### Medium: Quantity/price quantization is too hardcoded

Lines: `mexc-tg-monitor.py:287-290`, `mexc-tg-monitor.py:352-365`, `mexc-tg-monitor.py:414-436`

Price is always quantized with `0.01`. This is wrong for many contracts. `mexc_get_qty()` reads `contractSize` but ignores `priceUnit`, `volUnit`, `minVol`, and `apiAllowed`.

Recommendation:
- Implement the planned contract metadata cache.
- Quantize price to `priceUnit`, volume to `volUnit`, enforce `minVol`, and skip if `apiAllowed` is false.

### Medium: DB schema matches the plan, but data population is incomplete

Lines: `mexc-tg-monitor.py:161-211`, `mexc-tg-monitor.py:222-258`, `mexc-tg-monitor.py:837-908`

The schema exists and WAL is enabled. Runtime schema matches the plan. Problems are in usage:

- `entry_order_id` is inserted as `""` instead of the actual order ID at `mexc-tg-monitor.py:903-906`.
- `orders.price` records `edge["mexc_passive_price"]`, not the actual quantized order price from the request.
- The filled row uses `status.get("price")`, which is limit price, not average fill.
- No fill table exists, so partial fills and fees cannot be analyzed cleanly.
- `db_insert_signal()` calls `db_exec()` twice; if insert fails, `.fetchone()` on `None` can crash.
- Every accepted dry-run/signal-only event is stored as `accepted`, not `dry_run` or `signal_only`, despite the plan pass criteria.

Recommendation:
- Add `fills` table before live mode.
- Return IDs from insert helpers safely.
- Store actual submitted price, filled average price, deal volume, fees, and raw order detail.
- Differentiate `accepted`, `dry_run`, `signal_only`, `order_placed`, `order_filled`, `order_timeout_cancelled`.

### Medium: Exit PnL and exit condition are still midpoint-based

Lines: `mexc-tg-monitor.py:758-772`, `mexc-tg-monitor.py:563-573`

Entry uses cross-edge, but exit uses midpoint spread collapse. PnL uses `entry_sp * leverage * amount` and does not use actual entry/exit price, direction, fees, or funding. This is acceptable for rough Telegram text, but not for production trade accounting.

Recommendation:
- For LONG, compare executable close/sell price on MEXC bid against entry fill.
- For SHORT, compare executable close/buy price on MEXC ask against entry fill.
- Store realized close data from exchange order/fill detail.

### Medium: Leverage setup handles only long side

Lines: `mexc-tg-monitor.py:323-332`

`mexc_set_leverage()` always sends `positionType: 1`. For SHORT entries, MEXC may require `positionType: 2` if no short position exists.

Recommendation:
- Pass direction into `mexc_set_leverage()`.
- Use `positionType=1` for LONG and `2` for SHORT.

### Medium: Market helper side mapping is wrong for close if used

Lines: `mexc-tg-monitor.py:334-350`

The helper is named “Emergency market order — only for close,” but uses side `1` for LONG and `3` for SHORT, which are open-long/open-short sides, not close sides. It is not currently used by normal exit, but it is dangerous if later wired in.

Recommendation:
- Rename to `mexc_place_market_entry()` if kept.
- Implement separate close helper with side `4` for closing long and `2` for closing short.

### Medium: `/root/bin/mexc-tg-monitor.py` is the systemd target, not the reviewed source file

Service line: `/etc/systemd/system/mexc-monitor.service:10`  
Repo source: `mexc-tg-monitor.py`

The service runs `/root/bin/mexc-tg-monitor.py`, while this review read `/root/obsidian/tldr/проекты/mexc/mexc-tg-monitor.py`. If those diverge, systemd may not be running the reviewed code.

Recommendation:
- Compare checksums on every deploy.
- Consider making `/root/bin/mexc-tg-monitor.py` a symlink to the repo file or add an explicit deploy script.

### Low: Import-time `pip install websockets` is not production-safe

Lines: `mexc-tg-monitor.py:16-19`

Installing dependencies at runtime can hang startup, change package versions unexpectedly, and make restarts depend on network/PyPI availability.

Recommendation:
- Move dependency installation to deployment.
- Fail fast with a clear error if `websockets` is missing.

### Low: pyc file is modified in the repo

Observed `git status --short`:

```text
 M __pycache__/mexc-tg-monitor.cpython-311.pyc
```

Recommendation:
- Add `__pycache__/` and `*.pyc` to `.gitignore` if not already ignored.
- Do not commit generated bytecode.

## P0.5 Limit Order Flow Review

Current flow:

1. Calculates passive price from bid for LONG and ask for SHORT. This part is directionally correct.
2. Applies an additional passive offset. This reduces crossing risk but lowers fill probability.
3. Sends `/api/v1/private/order/create` with `type=5`.
4. Polls `/api/v1/private/order/get/{order_id}` for up to 2 seconds.
5. Cancels on timeout.
6. Adds `active_trades` only after perceived full fill.

Conceptually this is the right shape. Implementation is not correct enough for live:

- Default `type=5` is unsafe unless verified for `/order/create`.
- Status parsing is likely wrong (`status` vs `state`).
- Partial fills are not managed.
- Fill price uses wrong field.
- Cancel is not followed by a final reconciliation.
- `entry_order_id` is not stored in `trades`.

Minimum live-safe version:

- Verified post-only type, with a startup assertion.
- Order response parser supports both `orderId`-only and full object responses.
- Poll parser supports documented `state/dealVol/dealAvgPrice`.
- Partial fill path either manages the open partial position or immediately closes it.
- Final cancel path rechecks order detail and open positions before returning to signal mode.

## WebSocket Proxy Handling Review

The current fix likely solves the immediate `NO_PROXY=*` problem, but it is broader than needed. It mutates global process env in a reconnect loop and can affect other network clients.

Safe enough for dry-run if logs show stable Binance/MEXC/TG connectivity. Not safe enough as a long-term production pattern.

Recommended next step: isolate proxy configuration to the MEXC WS connection or set it once in systemd. Also make `MEXC_WS_ENABLED=0` actually disable the MEXC WS task.

## SQLite Schema Review

Schema status: acceptable P0 baseline. Tables and indexes match the plan.

Missing for production analysis:

- `fills` table
- order state transitions or unique order row with updates
- actual average fill price and filled volume
- realized fees
- exchange timestamps
- contract metadata snapshot
- source/freshness fields for Binance/MEXC quotes

The schema is not the immediate blocker. Incorrect population and missing fill semantics are the blocker.

## P1.2-P1.5 Recommendation

Do not start P1.5 threshold reduction yet.

Recommended order:

1. Fix P0.5 live-safety issues first: order type, state parser, partial fills, symbol-specific close, final reconciliation.
2. Start P1.3 before P1.2: move liquidity to futures depth and add hole/staleness checks. This improves current symbols without increasing surface area.
3. Then P1.2 dynamic pair universe, capped at 12-20 symbols, with hard filters for `apiAllowed`, contract status, spread, depth, funding, and Binance/MEXC symbol match.
4. Then P1.4 dynamic sizing using futures depth and loss streak.
5. Only then P1.5 threshold experiments, and only as an A/B dry-run config with DB-backed fill/edge statistics.

## Production Dry-Run Rating

Dry-run readiness: 6.5/10.

Good:
- Secrets are out of source.
- Gates default to dry-run.
- SQLite initializes with WAL.
- Cross-edge entry calculation is in place.
- MEXC WS ticker integration matches the documented endpoint/channel shape.
- systemd runs in dry-run with restart and heartbeat.

Still missing for trustworthy dry-run:
- Binance/MEXC staleness checks.
- Futures depth instead of spot depth.
- Correct dry-run/signal-only decision labels.
- Capture source timestamps and data source in SQLite.
- Verify `/root/bin` matches repo source.

Live auto-trading readiness: 2/10.

Blockers:
- Unverified and likely wrong post-only order type.
- Wrong fill-state parser.
- No partial-fill handling.
- Non-symbol-specific `close_all`.
- Startup does not restore active trades.
- No realized PnL/fill/fee accounting.

## Immediate Fix Checklist

- Change/verify post-only type mapping before live mode.
- Normalize MEXC order detail: `state`, `dealVol`, `dealAvgPrice`, `makerFee`, `takerFee`.
- Add partial-fill handling.
- Replace `close_all` normal exits with symbol/position-specific close orders.
- Restore `active_trades` during reconciliation or pause when restore data is insufficient.
- Use futures depth for liquidity checks.
- Add stale quote checks for both exchanges.
- Make `MEXC_WS_ENABLED` functional.
- Compare `/root/bin/mexc-tg-monitor.py` with repo source used in review.
