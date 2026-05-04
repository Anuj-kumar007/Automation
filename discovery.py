#!/usr/bin/env python3
"""
Multi‑Limit Bot – 4 limits (0.35 … 0.20) + live ticker + BTC updater + health endpoint
Render‑ready with fallback slug and threaded dashboard.
"""
import os, sys, time, json, asyncio, threading, socketserver, requests
from datetime import datetime, timezone, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
import aiohttp

# -------------------------------------------------------------------
# CONFIG – only 4 limits now
# -------------------------------------------------------------------
SIM_STARTING_BALANCE = 100.0
SHARES_PER_SIDE = 5
LIMIT_PRICES = [0.35, 0.30, 0.25, 0.20]

WEB_PORT = int(os.environ.get("PORT", 10003))
SCAN_INTERVAL = 0.2
PUBLIC_HEADERS = {"User-Agent": "Mozilla/5.0"}

# Health tracking
loop_status = {
    "running": False,
    "last_window": None,
    "last_error": None,
    "uptime_start": time.time()
}

# -------------------------------------------------------------------
# Market data
# -------------------------------------------------------------------
def get_bid_ask_single(token_id):
    try:
        resp = requests.get(f"https://clob.polymarket.com/book?token_id={token_id}",
                            headers=PUBLIC_HEADERS, timeout=5)
        if resp.status_code == 200:
            data = resp.json()
            bids = data.get("bids", [])
            asks = data.get("asks", [])
            return (float(bids[0]["price"]) if bids else None,
                    float(asks[0]["price"]) if asks else None)
    except:
        pass
    return None, None

def get_bid_ask(up_token, down_token):
    payload = [
        {"token_id": up_token, "side": "BUY"}, {"token_id": up_token, "side": "SELL"},
        {"token_id": down_token, "side": "BUY"}, {"token_id": down_token, "side": "SELL"}
    ]
    up_bid = up_ask = down_bid = down_ask = None
    try:
        resp = requests.post("https://clob.polymarket.com/prices", json=payload,
                             headers=PUBLIC_HEADERS, timeout=5)
        if resp.status_code == 200:
            data = resp.json()
            up_bid = float(data[up_token]["BUY"]) if data.get(up_token, {}).get("BUY") else None
            up_ask = float(data[up_token]["SELL"]) if data.get(up_token, {}).get("SELL") else None
            down_bid = float(data[down_token]["BUY"]) if data.get(down_token, {}).get("BUY") else None
            down_ask = float(data[down_token]["SELL"]) if data.get(down_token, {}).get("SELL") else None
    except Exception as e:
        print(f"⚠️ get_bid_ask error: {e}")
    if up_bid is None or up_ask is None:
        ub, ua = get_bid_ask_single(up_token)
        up_bid = up_bid or ub
        up_ask = up_ask or ua
    if down_bid is None or down_ask is None:
        db, da = get_bid_ask_single(down_token)
        down_bid = down_bid or db
        down_ask = down_ask or da
    return {"UP": {"bid": up_bid, "ask": up_ask}, "DOWN": {"bid": down_bid, "ask": down_ask}}

async def get_current_slug():
    now = datetime.now(timezone.utc)
    wmin = (now.minute // 5) * 5
    ws = now.replace(minute=wmin, second=0, microsecond=0)
    return f"btc-updown-5m-{int(ws.timestamp())}"

async def get_token_ids(slug):
    url = f"https://gamma-api.polymarket.com/markets/slug/{slug}"
    async with aiohttp.ClientSession(headers=PUBLIC_HEADERS) as sess:
        try:
            async with sess.get(url, timeout=10) as resp:
                print(f"🔎 Token lookup {url} -> {resp.status}")
                if resp.status == 200:
                    data = await resp.json()
                    raw = data.get("clobTokenIds")
                    if isinstance(raw, str):
                        raw = json.loads(raw)
                    if raw and len(raw) >= 2:
                        return raw[0], raw[1]
        except Exception as e:
            print(f"❌ Token ID error: {e}")
    return None, None

class MarketData:
    def __init__(self):
        self.up_bid = self.up_ask = None
        self.down_bid = self.down_ask = None
        self.btc_price = None
        self.beat_price = None
        self.lock = threading.Lock()

    def update_order_book(self, prices):
        with self.lock:
            if prices:
                self.up_bid = prices["UP"]["bid"]
                self.up_ask = prices["UP"]["ask"]
                self.down_bid = prices["DOWN"]["bid"]
                self.down_ask = prices["DOWN"]["ask"]

    def update_btc(self, btc_price):
        with self.lock:
            self.btc_price = btc_price

    def update_beat(self, beat_price):
        with self.lock:
            self.beat_price = beat_price

    def snapshot(self):
        with self.lock:
            return {
                "up_bid": self.up_bid, "up_ask": self.up_ask,
                "down_bid": self.down_bid, "down_ask": self.down_ask,
                "btc_price": self.btc_price, "beat_price": self.beat_price
            }

market_data = MarketData()

class SimAccount:
    def __init__(self, name, initial_balance):
        self.name = name
        self.balance = initial_balance
        self.trades = []
        self.stats = {
            "name": name,
            "windows_processed": 0,
            "trades_attempted": 0,
            "filled_sides": 0,
            "both_filled": 0,
            "wins": 0,
            "losses": 0,
            "total_pnl": 0.0,
            "balance": initial_balance,
        }
        self.lock = threading.Lock()

    def add_trade(self, window_start, details):
        with self.lock:
            trade = {"time": window_start.strftime("%H:%M:%S"), **details}
            self.trades.insert(0, trade)
            if len(self.trades) > 5:
                self.trades.pop()

    def update(self, pnl, sides_filled, win):
        with self.lock:
            self.stats["windows_processed"] += 1
            if sides_filled > 0:
                self.stats["trades_attempted"] += 1
            self.stats["filled_sides"] += sides_filled
            if sides_filled == 2:
                self.stats["both_filled"] += 1
            if sides_filled > 0:
                if win:
                    self.stats["wins"] += 1
                else:
                    self.stats["losses"] += 1
            self.stats["total_pnl"] += pnl
            self.balance += pnl
            self.stats["balance"] = self.balance

accounts = {f"${limit:.2f}": SimAccount(f"${limit:.2f}", SIM_STARTING_BALANCE) for limit in LIMIT_PRICES}

async def btc_price_updater():
    async with aiohttp.ClientSession(headers=PUBLIC_HEADERS) as sess:
        while True:
            try:
                async with sess.get("https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT", timeout=3) as resp:
                    if resp.status == 200:
                        price = float((await resp.json())["price"])
                        market_data.update_btc(price)
            except Exception as e:
                print(f"⚠️ BTC price error: {e}")
            await asyncio.sleep(1)

async def market_monitor(up_token, down_token, beat_price, stop_event, fill_events):
    while not stop_event.is_set():
        try:
            loop = asyncio.get_running_loop()
            prices = await loop.run_in_executor(None, get_bid_ask, up_token, down_token)
            market_data.update_order_book(prices)
            if prices:
                ask_up = prices["UP"]["ask"]
                ask_down = prices["DOWN"]["ask"]
                for label, limit in zip(accounts.keys(), LIMIT_PRICES):
                    evts = fill_events[label]
                    if ask_up is not None and ask_up <= limit and not evts["up"].is_set():
                        print(f"🎯 {label} UP fill! ask_up={ask_up:.2f}")
                        evts["up"].set()
                    if ask_down is not None and ask_down <= limit and not evts["down"].is_set():
                        print(f"🎯 {label} DOWN fill! ask_down={ask_down:.2f}")
                        evts["down"].set()
        except Exception as e:
            print(f"⚠️ Monitor error: {e}")
        await asyncio.sleep(SCAN_INTERVAL)

# -------------------------------------------------------------------
# Threaded HTTP server
# -------------------------------------------------------------------
class ThreadedHTTPServer(socketserver.ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True

class DashboardHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/":
            self._serve_html()
        elif self.path == "/api/stats":
            self._serve_json({label: acc.stats for label, acc in accounts.items()})
        elif self.path == "/api/trades":
            self._serve_json({label: acc.trades for label, acc in accounts.items()})
        elif self.path == "/api/market":
            self._serve_json(market_data.snapshot())
        elif self.path == "/api/health":
            status = loop_status.copy()
            status["uptime"] = round(time.time() - status["uptime_start"], 1)
            self._serve_json(status)
        else:
            self.send_response(404)
            self.end_headers()

    def _serve_html(self):
        panels_html = ""
        for label in accounts.keys():
            clean_id = label.replace('$','').replace('.','')
            panels_html += f"""
            <div class="strategy-panel">
                <h2>{label} Limit</h2>
                <div class="stats-grid" id="stats-{clean_id}"></div>
                <h3>Recent Trades</h3>
                <table id="trades-{clean_id}">
                    <thead><tr><th>Time</th><th>UP</th><th>DOWN</th><th>Cost</th><th>Outcome</th><th>P&L</th></tr></thead>
                    <tbody></tbody>
                </table>
            </div>"""

        html = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, user-scalable=no">
    <title>Multi‑Limit Bot (BTC Live) – 4 Limits</title>
    <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{
            background: linear-gradient(135deg, #0a0f1e 0%, #141b2d 100%);
            color: #e0e6ed; font-family: 'Segoe UI', system-ui, sans-serif;
            min-height: 100vh; padding: 16px;
        }}
        .dashboard {{ max-width: 1600px; margin: 0 auto; }}
        h1 {{
            font-size: 2rem; font-weight: 600;
            background: linear-gradient(90deg, #60a5fa, #a78bfa);
            -webkit-background-clip: text; -webkit-text-fill-color: transparent;
            margin-bottom: 6px;
        }}
        .subtitle {{ color: #8b95a5; margin-bottom: 24px; font-size: 0.9rem; }}

        .live-market {{
            background: rgba(20, 27, 45, 0.6); backdrop-filter: blur(15px);
            border: 1px solid rgba(100, 130, 200, 0.2); border-radius: 16px;
            padding: 14px 12px; margin-bottom: 24px; display: flex;
            flex-wrap: wrap; gap: 14px; justify-content: space-around;
        }}
        .live-item {{ text-align: center; min-width: 60px; flex: 1 0 auto; }}
        .live-item .label {{ font-size: 0.65rem; text-transform: uppercase; color: #8b95a5; letter-spacing: 0.4px; }}
        .live-item .value {{
            font-size: 1.1rem; font-weight: 700;
            background: linear-gradient(135deg, #e0e7ff, #c4b5fd);
            -webkit-background-clip: text; -webkit-text-fill-color: transparent;
        }}

        .strategy-grid {{
            display: grid;
            grid-template-columns: repeat(auto-fill, minmax(300px, 1fr));
            gap: 20px;
            margin-bottom: 30px;
        }}
        .strategy-panel {{
            background: rgba(20, 27, 45, 0.7); backdrop-filter: blur(20px);
            border: 1px solid rgba(100, 130, 200, 0.15); border-radius: 16px;
            padding: 16px; box-shadow: 0 10px 30px rgba(0,0,0,0.4);
        }}
        .strategy-panel h2 {{
            font-size: 1.1rem; font-weight: 600;
            background: linear-gradient(90deg, #60a5fa, #818cf8);
            -webkit-background-clip: text; -webkit-text-fill-color: transparent;
            margin-bottom: 12px;
        }}
        .stats-grid {{ display: grid; grid-template-columns: repeat(2, 1fr); gap: 6px; margin-bottom: 15px; }}
        .stat-card {{
            background: rgba(30, 35, 55, 0.5); border: 1px solid rgba(255,255,255,0.05);
            border-radius: 8px; padding: 8px 6px; text-align: center; position: relative; overflow: hidden;
        }}
        .stat-card::before {{
            content: ''; position: absolute; top: -50%; left: -75%; width: 50%; height: 200%;
            background: linear-gradient(90deg, transparent 0%, rgba(255,255,255,0.06) 30%, rgba(255,255,255,0.12) 50%, rgba(255,255,255,0.06) 70%, transparent 100%);
            transform: rotate(25deg); animation: shimmer 3s infinite linear;
        }}
        @keyframes shimmer {{ 0% {{ left: -75%; }} 100% {{ left: 125%; }} }}
        .stat-value {{
            font-size: 1.1rem; font-weight: 700;
            background: linear-gradient(135deg, #e0e7ff, #c4b5fd);
            -webkit-background-clip: text; -webkit-text-fill-color: transparent; position: relative;
        }}
        .stat-label {{ color: #8b95a5; font-size: 0.6rem; text-transform: uppercase; margin-top: 2px; position: relative; }}

        h3 {{ color: #cbd5e1; margin-bottom: 8px; font-size: 0.85rem; }}
        table {{ width: 100%; border-collapse: collapse; font-size: 0.7rem; }}
        th {{ text-align: left; padding: 6px 3px; border-bottom: 2px solid rgba(255,255,255,0.1); color: #cbd5e1; font-size: 0.6rem; text-transform: uppercase; }}
        td {{ padding: 4px 3px; border-bottom: 1px solid rgba(255,255,255,0.05); }}
        .win {{ color: #4ade80; }} .loss {{ color: #f87171; }}

        .refresh-info {{ text-align: right; margin-top: 16px; color: #6b7280; font-size: 0.7rem; }}
        .refresh-info span {{ background: rgba(99, 102, 241, 0.2); padding: 3px 10px; border-radius: 20px; margin-left: 6px; color: #a5b4fc; }}

        @media (max-width: 700px) {{
            .strategy-grid {{ grid-template-columns: 1fr; }}
            .stats-grid {{ grid-template-columns: 1fr 1fr; }}
        }}
    </style>
</head>
<body>
    <div class="dashboard">
        <h1>🧬 Multi‑Limit Bot (BTC Live) – 4 Limits</h1>
        <div class="subtitle">Limits: $0.35, $0.30, $0.25, $0.20 · live ticker 0.2s · BTC 1s</div>

        <div class="live-market">
            <div class="live-item"><div class="label">UP Bid</div><div class="value" id="up-bid">-</div></div>
            <div class="live-item"><div class="label">UP Ask</div><div class="value" id="up-ask">-</div></div>
            <div class="live-item"><div class="label">DOWN Bid</div><div class="value" id="down-bid">-</div></div>
            <div class="live-item"><div class="label">DOWN Ask</div><div class="value" id="down-ask">-</div></div>
            <div class="live-item"><div class="label">BTC Price</div><div class="value" id="btc-price">-</div></div>
            <div class="live-item"><div class="label">Beat Price</div><div class="value" id="beat-price">-</div></div>
        </div>

        <div class="strategy-grid">
            {panels_html}
        </div>

        <div class="refresh-info">
            Stats refresh every 5s <span id="refreshCounter">next: 5s</span>
        </div>
    </div>

    <script>
        let countdown = 5;
        const counter = document.getElementById("refreshCounter");

        async function refreshLive() {{
            try {{
                const resp = await fetch('/api/market');
                const m = await resp.json();
                document.getElementById('up-bid').innerText = m.up_bid != null ? '$' + m.up_bid.toFixed(2) : '...';
                document.getElementById('up-ask').innerText = m.up_ask != null ? '$' + m.up_ask.toFixed(2) : '...';
                document.getElementById('down-bid').innerText = m.down_bid != null ? '$' + m.down_bid.toFixed(2) : '...';
                document.getElementById('down-ask').innerText = m.down_ask != null ? '$' + m.down_ask.toFixed(2) : '...';
                document.getElementById('btc-price').innerText = m.btc_price != null ? '$' + m.btc_price.toFixed(2) : '...';
                document.getElementById('beat-price').innerText = m.beat_price != null ? '$' + m.beat_price.toFixed(2) : '...';
            }} catch(e) {{}}
        }}
        setInterval(refreshLive, 200);
        refreshLive();

        function renderStats(label, data) {{
            const id = 'stats-' + label.replace('$','').replace('.','');
            const container = document.getElementById(id);
            if (!container) return;
            container.innerHTML = `
                <div class="stat-card"><div class="stat-value">${{data.windows_processed}}</div><div class="stat-label">Windows</div></div>
                <div class="stat-card"><div class="stat-value">${{data.trades_attempted}}</div><div class="stat-label">Trades</div></div>
                <div class="stat-card"><div class="stat-value">${{data.filled_sides}}</div><div class="stat-label">Sides</div></div>
                <div class="stat-card"><div class="stat-value">${{data.both_filled}}</div><div class="stat-label">Both</div></div>
                <div class="stat-card"><div class="stat-value">${{data.wins}}</div><div class="stat-label">Wins</div></div>
                <div class="stat-card"><div class="stat-value">${{data.losses}}</div><div class="stat-label">Losses</div></div>
                <div class="stat-card"><div class="stat-value">$${{data.balance.toFixed(2)}}</div><div class="stat-label">Balance</div></div>
                <div class="stat-card"><div class="stat-value">$${{data.total_pnl.toFixed(2)}}</div><div class="stat-label">P&L</div></div>
            `;
        }}

        function renderTrades(label, trades) {{
            const id = 'trades-' + label.replace('$','').replace('.','');
            const tbody = document.querySelector('#' + id + ' tbody');
            if (!tbody) return;
            tbody.innerHTML = trades.map(t => `
                <tr>
                    <td>${{t.time}}</td>
                    <td>${{t.up_filled || ''}}</td>
                    <td>${{t.down_filled || ''}}</td>
                    <td>${{t.cost || ''}}</td>
                    <td class="${{t.outcome === 'WIN' ? 'win' : 'loss'}}">${{t.outcome || ''}}</td>
                    <td class="${{t.outcome === 'WIN' ? 'win' : 'loss'}}">${{t.pnl || ''}}</td>
                </tr>
            `).join('');
        }}

        async function refresh() {{
            try {{
                const [statsRes, tradesRes] = await Promise.all([fetch('/api/stats'), fetch('/api/trades')]);
                const allStats = await statsRes.json();
                const allTrades = await tradesRes.json();
                for (const [label, stats] of Object.entries(allStats)) {{
                    renderStats(label, stats);
                }}
                for (const [label, trades] of Object.entries(allTrades)) {{
                    renderTrades(label, trades);
                }}
                countdown = 5;
                counter.innerText = 'next: 5s';
            }} catch(e) {{}}
        }}
        setInterval(() => {{ countdown--; counter.innerText = `next: ${{countdown}}s`; if (countdown <= 0) refresh(); }}, 1000);
        refresh();
    </script>
</body>
</html>"""
        self.send_response(200)
        self.send_header("Content-type", "text/html")
        self.end_headers()
        self.wfile.write(html.encode())

    def _serve_json(self, data):
        self.send_response(200)
        self.send_header("Content-type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())

def run_web_server():
    server = ThreadedHTTPServer(('0.0.0.0', WEB_PORT), DashboardHandler)
    print(f"🌐 Dashboard at http://localhost:{WEB_PORT}")
    server.serve_forever()

# -------------------------------------------------------------------
# MAIN LOOP
# -------------------------------------------------------------------
async def main():
    print("=" * 60)
    print("Multi‑Limit Bot – 4 limits, Render ready")
    asyncio.create_task(btc_price_updater())
    threading.Thread(target=run_web_server, daemon=True).start()

    loop_status["running"] = True

    while True:
        try:
            slug = await get_current_slug()
            print(f"🔍 Slug: {slug}")
            token_up, token_down = await get_token_ids(slug)

            # fallback to previous 5-min window if current not yet available
            if not token_up or not token_down:
                now = datetime.now(timezone.utc)
                wmin_prev = ((now.minute // 5) * 5) - 5
                ws_prev = now.replace(minute=wmin_prev % 60, second=0, microsecond=0)
                slug_prev = f"btc-updown-5m-{int(ws_prev.timestamp())}"
                print(f"⚠️ No tokens for {slug}, trying {slug_prev}")
                token_up, token_down = await get_token_ids(slug_prev)

            if not token_up or not token_down:
                loop_status["last_error"] = "no_token_ids"
                print("❌ No token IDs – retrying in 10s")
                await asyncio.sleep(10)
                continue

            now = datetime.now(timezone.utc)
            wmin = (now.minute // 5) * 5
            ws = now.replace(minute=wmin, second=0, microsecond=0)
            we = ws + timedelta(minutes=5)
            wts = int(ws.timestamp())
            if time.time() < wts:
                await asyncio.sleep(wts - time.time() + 0.5)

            loop_status["last_window"] = ws.strftime("%H:%M:%S UTC")
            print(f"\n📌 Window: {ws.strftime('%H:%M:%S')} → {we.strftime('%H:%M:%S')} UTC")

            # Beat price
            beat_price = None
            async with aiohttp.ClientSession(headers=PUBLIC_HEADERS) as sess:
                try:
                    async with sess.get(f"https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval=1m&startTime={(wts-60)*1000}&limit=1", timeout=5) as resp:
                        if resp.status == 200:
                            beat_price = float((await resp.json())[0][4])
                except: pass
            if not beat_price:
                async with aiohttp.ClientSession(headers=PUBLIC_HEADERS) as sess:
                    resp = await sess.get("https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT")
                    if resp.status == 200: beat_price = float((await resp.json())["price"])
            if not beat_price:
                print("No beat price, skipping")
                await asyncio.sleep(10)
                continue
            market_data.update_beat(beat_price)

            fill_events = {label: {"up": asyncio.Event(), "down": asyncio.Event()} for label in accounts}
            stop_event = asyncio.Event()
            monitor_task = asyncio.create_task(market_monitor(token_up, token_down, beat_price, stop_event, fill_events))

            while time.time() < we.timestamp() - 2:
                await asyncio.sleep(0.5)

            fill_status = {label: (evts["up"].is_set(), evts["down"].is_set()) for label, evts in fill_events.items()}
            stop_event.set()
            try:
                await asyncio.wait_for(monitor_task, timeout=2)
            except asyncio.TimeoutError:
                monitor_task.cancel()

            while time.time() < we.timestamp():
                await asyncio.sleep(1)

            # Settlement price
            settlement_price = None
            async with aiohttp.ClientSession(headers=PUBLIC_HEADERS) as sess:
                try:
                    async with sess.get(f"https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval=1m&startTime={wts*1000+300000}&limit=1", timeout=5) as resp:
                        if resp.status == 200:
                            settlement_price = float((await resp.json())[0][4])
                except: pass
            if not settlement_price:
                settlement_price = beat_price
            actual = "UP" if settlement_price >= beat_price else "DOWN"
            print(f"Settlement: {actual} (settle ${settlement_price:.2f})")

            for label, limit in zip(accounts.keys(), LIMIT_PRICES):
                filled_up, filled_down = fill_status[label]
                cost_up = SHARES_PER_SIDE * limit if filled_up else 0.0
                cost_down = SHARES_PER_SIDE * limit if filled_down else 0.0
                total_cost = cost_up + cost_down
                sides_filled = (1 if filled_up else 0) + (1 if filled_down else 0)
                payout_up = SHARES_PER_SIDE if (actual == "UP" and filled_up) else 0.0
                payout_down = SHARES_PER_SIDE if (actual == "DOWN" and filled_down) else 0.0
                pnl = (payout_up + payout_down) - total_cost
                win = pnl > 0
                accounts[label].update(pnl, sides_filled, win)
                accounts[label].add_trade(ws, {
                    "up_filled": "YES" if filled_up else "NO",
                    "down_filled": "YES" if filled_down else "NO",
                    "cost": f"${total_cost:.2f}",
                    "outcome": "WIN" if win else "LOSS",
                    "pnl": f"${pnl:.2f}"
                })
            loop_status["last_error"] = None
            await asyncio.sleep(2)

        except Exception as e:
            loop_status["last_error"] = str(e)[:200]
            print(f"❌ Window error: {e}")
            await asyncio.sleep(5)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nBot stopped.")