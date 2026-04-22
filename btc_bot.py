#!/usr/bin/env python3
"""
BTC 5-Minute Polymarket Trading Bot – FULLY CORRECTED
"""

import os, sys, threading, json, asyncio, aiohttp, time, sqlite3, math, hashlib, hmac, base64
from datetime import datetime, timedelta, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler
from dotenv import load_dotenv

from py_clob_client.client import ClobClient
from py_clob_client.constants import POLYGON, AMOY
from py_clob_client.clob_types import OrderArgs, MarketOrderArgs, OrderType
from py_clob_client.order_builder.constants import BUY

load_dotenv()

USE_TESTNET = os.getenv("USE_TESTNET", "true").lower() == "true"
TRADE_SIZE = float(os.getenv("TRADE_SIZE", "5.0"))
CONFIDENCE_THRESHOLD = float(os.getenv("CONFIDENCE_THRESHOLD", "0.50"))
ORDER_TYPE = os.getenv("ORDER_TYPE", "MARKET").upper()

HOST = "https://clob-staging.polymarket.com" if USE_TESTNET else "https://clob.polymarket.com"
CHAIN_ID = AMOY if USE_TESTNET else POLYGON

# -------------------------------------------------------------------
# LOG CAPTURE & WEB TERMINAL
# -------------------------------------------------------------------
log_buffer = []
log_lock = threading.Lock()

class TeeWriter:
    def __init__(self, o): self.original = o
    def write(self, text):
        if text.strip():
            ts = datetime.now().strftime("%H:%M:%S")
            with log_lock:
                log_buffer.append(f"[{ts}] {text.rstrip()}")
                if len(log_buffer) > 200: log_buffer.pop(0)
        self.original.write(text); self.original.flush()
    def flush(self): self.original.flush()

sys.stdout = TeeWriter(sys.stdout)

class TerminalHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/":
            self.send_response(200); self.send_header("Content-type", "text/html"); self.end_headers()
            self.wfile.write("""<!DOCTYPE html><html><head><title>BTC Bot</title><meta charset="UTF-8"><style>
                *{margin:0;padding:0;box-sizing:border-box} body{background:#0C0C0C;font-family:Consolas,monospace;height:100vh;display:flex;flex-direction:column;overflow:hidden}
                .bar{background:#2D2D2D;padding:8px 12px;display:flex;align-items:center;gap:12px;border-bottom:1px solid #3C3C3C}
                .win{display:flex;gap:8px} .c{width:12px;height:12px;border-radius:50%} .r{background:#FF5F56} .y{background:#FFBD2E} .g{background:#27C93F}
                .title{color:#E5E5E5;font-size:13px;flex-grow:1;text-align:center} .log-cont{flex:1;overflow-y:auto;padding:16px 20px;background:#0C0C0C}
                .log{font-family:monospace;font-size:14px;line-height:1.5;white-space:pre-wrap;color:#D4D4D4;padding-left:8px;margin-bottom:2px}
                .e{color:#F48771} .s{color:#6A9955} .w{color:#DCDCAA} .cursor{display:inline-block;width:8px;height:16px;background:#D4D4D4;animation:blink 1s step-end infinite}
                @keyframes blink{0%,100%{opacity:1}50%{opacity:0}} .footer{background:#1E1E1E;padding:6px 12px;font-size:11px;color:#858585;border-top:1px solid #3C3C3C}
                ::-webkit-scrollbar{width:8px} ::-webkit-scrollbar-track{background:#1E1E1E} ::-webkit-scrollbar-thumb{background:#424242;border-radius:4px}
            </style></head><body><div class="bar"><div class="win"><div class="c r"></div><div class="c y"></div><div class="c g"></div></div><div class="title">BTC Bot – Live</div><div style="width:48px"></div></div><div class="log-cont" id="logContainer"><div id="logContent">Loading...</div></div><div class="footer">Live output | Auto‑scroll | Trades automatic</div><script>
                function escapeHtml(t){return t.replace(/[&<>]/g,m=>m==='&'?'&amp;':m==='<'?'&lt;':'&gt;')}
                function fetchLog(){fetch('/api/log').then(r=>r.json()).then(d=>{
                    const c=document.getElementById('logContent'),s=document.getElementById('logContainer'),l=d.logs||[];
                    let h=''; l.forEach(line=>{let cls='log'; if(line.includes('❌')||line.includes('Error')) cls+=' e'; else if(line.includes('✅')||line.includes('🎯')) cls+=' s'; else if(line.includes('⚠️')) cls+=' w'; h+=`<div class="${cls}">${escapeHtml(line)}</div>`});
                    h+=`<div class="log">█<span class="cursor"></span></div>`;
                    if(c.innerHTML!==h){const wasBottom=s.scrollHeight-s.scrollTop<=s.clientHeight+50; c.innerHTML=h; if(wasBottom) s.scrollTop=s.scrollHeight;}
                });}
                setInterval(fetchLog,2000); fetchLog();
            </script></body></html>""".encode())
        elif self.path == "/api/log":
            self.send_response(200); self.send_header("Content-type", "application/json"); self.end_headers()
            with log_lock: logs = log_buffer.copy()
            self.wfile.write(json.dumps({"logs": logs}).encode())
        else: self.send_response(404); self.end_headers()

def run_web_server():
    HTTPServer(('0.0.0.0', int(os.environ.get("PORT", 10000))), TerminalHandler).serve_forever()
threading.Thread(target=run_web_server, daemon=True).start()
print(f"🌐 Web terminal on port {os.environ.get('PORT', 10000)}", flush=True)

# -------------------------------------------------------------------
# DATABASE
# -------------------------------------------------------------------
class TradeDatabase:
    def __init__(self, db_path="btc_bot_trades.db"):
        self.db_path = db_path; self._tl = threading.local(); self._init()
    def _get_conn(self):
        if not hasattr(self._tl, 'conn'):
            self._tl.conn = sqlite3.connect(self.db_path, check_same_thread=False)
            self._tl.conn.execute("PRAGMA journal_mode=WAL"); self._tl.conn.row_factory = sqlite3.Row
        return self._tl.conn
    def _init(self):
        c = self._get_conn().cursor()
        c.execute("CREATE TABLE IF NOT EXISTS trades (id INTEGER PRIMARY KEY, window_start_ts INTEGER, window_start_time TEXT, prediction TEXT, confidence REAL, token_id TEXT, order_type TEXT, size REAL, limit_price REAL, fill_price REAL, order_id TEXT, order_status TEXT DEFAULT 'pending', cost_usdc REAL, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
        c.execute("CREATE TABLE IF NOT EXISTS settlements (id INTEGER PRIMARY KEY, trade_id INTEGER, window_end_ts INTEGER, window_end_time TEXT, price_to_beat REAL, settlement_price REAL, actual_outcome TEXT, prediction_correct BOOLEAN, payout_usdc REAL, pnl_usdc REAL, settled_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, FOREIGN KEY(trade_id) REFERENCES trades(id))")
        c.execute("CREATE TABLE IF NOT EXISTS predictions (id INTEGER PRIMARY KEY, window_start_ts INTEGER, window_start_time TEXT, price_to_beat REAL, current_btc REAL, change_pct REAL, prediction TEXT, confidence REAL, up_odds REAL, down_odds REAL, trade_executed BOOLEAN DEFAULT FALSE, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
        c.execute("CREATE TABLE IF NOT EXISTS performance_summary (id INTEGER PRIMARY KEY, date TEXT UNIQUE, total_trades INTEGER, winning_trades INTEGER, losing_trades INTEGER, total_pnl_usdc REAL, win_rate REAL, avg_pnl_per_trade REAL, total_volume_usdc REAL, updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_trades_window ON trades(window_start_ts)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_settlements_trade ON settlements(trade_id)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_predictions_window ON predictions(window_start_ts)")
        self._get_conn().commit()
    def log_trade(self, *args):
        c = self._get_conn().cursor()
        c.execute("INSERT INTO trades (window_start_ts,window_start_time,prediction,confidence,token_id,order_type,size,limit_price,fill_price,order_id,order_status,cost_usdc) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", args[:12])
        self._get_conn().commit(); return c.lastrowid
    def update_trade_status(self, tid, status, fill_price=None):
        c = self._get_conn().cursor()
        if fill_price: c.execute("UPDATE trades SET order_status=?, fill_price=? WHERE id=?", (status, fill_price, tid))
        else: c.execute("UPDATE trades SET order_status=? WHERE id=?", (status, tid))
        self._get_conn().commit()
    def log_settlement(self, *args):
        c = self._get_conn().cursor()
        c.execute("INSERT INTO settlements (trade_id,window_end_ts,window_end_time,price_to_beat,settlement_price,actual_outcome,prediction_correct,payout_usdc,pnl_usdc) VALUES (?,?,?,?,?,?,?,?,?)", args[:9])
        self._get_conn().commit()
    def log_prediction(self, *args):
        c = self._get_conn().cursor()
        c.execute("INSERT INTO predictions (window_start_ts,window_start_time,price_to_beat,current_btc,change_pct,prediction,confidence,up_odds,down_odds,trade_executed) VALUES (?,?,?,?,?,?,?,?,?,?)", args[:10])
        self._get_conn().commit()
    def update_performance_summary(self):
        c = self._get_conn().cursor()
        today = datetime.now().strftime("%Y-%m-%d")
        c.execute("SELECT COUNT(*) as total_trades, SUM(CASE WHEN s.pnl_usdc>0 THEN 1 ELSE 0 END) as winning_trades, SUM(CASE WHEN s.pnl_usdc<=0 THEN 1 ELSE 0 END) as losing_trades, COALESCE(SUM(s.pnl_usdc),0) as total_pnl, COALESCE(AVG(s.pnl_usdc),0) as avg_pnl, COALESCE(SUM(t.cost_usdc),0) as total_volume FROM settlements s JOIN trades t ON s.trade_id=t.id WHERE DATE(s.settled_at)=?", (today,))
        row = c.fetchone()
        if row and row['total_trades']>0:
            win_rate = (row['winning_trades']/row['total_trades'])*100
            c.execute("INSERT INTO performance_summary (date,total_trades,winning_trades,losing_trades,total_pnl_usdc,win_rate,avg_pnl_per_trade,total_volume_usdc) VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(date) DO UPDATE SET total_trades=excluded.total_trades, winning_trades=excluded.winning_trades, losing_trades=excluded.losing_trades, total_pnl_usdc=excluded.total_pnl_usdc, win_rate=excluded.win_rate, avg_pnl_per_trade=excluded.avg_pnl_per_trade, total_volume_usdc=excluded.total_volume_usdc, updated_at=CURRENT_TIMESTAMP", (today, row['total_trades'], row['winning_trades'], row['losing_trades'], row['total_pnl'], win_rate, row['avg_pnl'], row['total_volume']))
        self._get_conn().commit()
    def get_last_trades(self, limit=5):
        c = self._get_conn().cursor()
        c.execute("""
            SELECT t.window_start_time, t.prediction, t.size, t.fill_price, t.cost_usdc,
                   s.actual_outcome, s.pnl_usdc
            FROM trades t
            LEFT JOIN settlements s ON t.id = s.trade_id
            WHERE t.order_status = 'filled'
            ORDER BY t.id DESC
            LIMIT ?
        """, (limit,))
        return c.fetchall()
db = TradeDatabase()

# ============================================================
# ACCURACY TRACKER (Original prediction logic untouched)
# ============================================================
class AccuracyTracker:
    def __init__(self):
        self.total = 0
        self.correct = 0
        self.history = []

    def add_result(self, window_start_ts, prediction, actual):
        if prediction not in ("UP", "DOWN"):
            return
        self.total += 1
        is_correct = (prediction == actual)
        if is_correct:
            self.correct += 1
        self.history.append((window_start_ts, prediction, actual, is_correct))
        return is_correct

    def accuracy(self):
        return (self.correct / self.total * 100) if self.total else 0.0

    def summary(self):
        print("\n" + "=" * 60, flush=True)
        print("📊 PREDICTION ACCURACY", flush=True)
        print("=" * 60, flush=True)
        print(f"   Total predictions: {self.total}", flush=True)
        print(f"   ✅ Correct: {self.correct}", flush=True)
        print(f"   ❌ Wrong: {self.total - self.correct}", flush=True)
        print(f"   📈 Accuracy: {self.accuracy():.1f}%", flush=True)
        if self.history:
            print("\n📋 Last 5 predictions:", flush=True)
            for ws, pred, act, correct in self.history[-5:]:
                status = "✅" if correct else "❌"
                time_str = datetime.fromtimestamp(ws).strftime('%H:%M:%S')
                print(f"   {status} {time_str} | Pred: {pred} | Actual: {act}", flush=True)
        print("=" * 60, flush=True)

def print_last_trades(limit=5):
    """Print a table of the last N trades from the database."""
    rows = db.get_last_trades(limit)
    if not rows:
        return
    print("\n" + "=" * 75, flush=True)
    print("📋 LAST 5 TRADES", flush=True)
    print("=" * 75, flush=True)
    print(f"{'Time':<10} {'Pred':<5} {'Size':<6} {'Entry':<8} {'Cost':<7} {'Outcome':<8} {'P&L':<8}", flush=True)
    print("-" * 75, flush=True)
    for row in reversed(rows):
        time_str = row['window_start_time'][11:16] if row['window_start_time'] else "N/A"
        pred = row['prediction']
        size = f"{row['size']:.1f}"
        entry = f"${row['fill_price']:.4f}" if row['fill_price'] else "N/A"
        cost = f"${row['cost_usdc']:.2f}" if row['cost_usdc'] else "N/A"
        outcome = row['actual_outcome'] if row['actual_outcome'] else "PENDING"
        pnl = f"${row['pnl_usdc']:.2f}" if row['pnl_usdc'] is not None else "---"
        print(f"{time_str:<10} {pred:<5} {size:<6} {entry:<8} {cost:<7} {outcome:<8} {pnl:<8}", flush=True)
    print("=" * 75, flush=True)

# -------------------------------------------------------------------
# BALANCE FETCH (Direct API Call)
# -------------------------------------------------------------------
def fetch_balance_direct():
    try:
        api_key = os.getenv("POLY_BUILDER_API_KEY")
        secret = os.getenv("POLY_BUILDER_SECRET")
        passphrase = os.getenv("POLY_BUILDER_PASSPHRASE")
        if not all([api_key, secret, passphrase]):
            return None

        timestamp = str(int(time.time()))
        method = "GET"
        path = "/balance-allowance"
        body = ""
        message = timestamp + method + path + body
        signature = base64.b64encode(hmac.new(secret.encode(), message.encode(), hashlib.sha256).digest()).decode()

        headers = {
            "POLY-API-KEY": api_key,
            "POLY-PASSPHRASE": passphrase,
            "POLY-TIMESTAMP": timestamp,
            "POLY-SIGNATURE": signature,
        }

        import requests
        resp = requests.get(HOST + path, headers=headers, timeout=10)
        if resp.status_code == 200:
            return float(resp.json().get("balance", 0))
    except Exception as e:
        print(f"⚠️ Balance fetch error: {e}", flush=True)
    return None

# -------------------------------------------------------------------
# CLOB CLIENT
# -------------------------------------------------------------------
def init_clob_client():
    creds = {"key": os.getenv("POLY_BUILDER_API_KEY"), "secret": os.getenv("POLY_BUILDER_SECRET"), "passphrase": os.getenv("POLY_BUILDER_PASSPHRASE")}
    client = ClobClient(HOST, key=os.getenv("POLY_PRIVATE_KEY"), chain_id=CHAIN_ID, signature_type=1, funder=os.getenv("POLY_FUNDER_ADDRESS"), creds=creds)
    try: client.set_api_creds(client.create_or_derive_api_creds())
    except: pass
    return client
clob_client = init_clob_client()
print("✅ CLOB Client initialized", flush=True)

balance = fetch_balance_direct()
if balance is not None:
    print(f"💰 USDC Balance: ${balance:.2f}", flush=True)
else:
    print("⚠️ Could not fetch balance", flush=True)

# -------------------------------------------------------------------
# TIMEZONE (Original logic preserved)
# -------------------------------------------------------------------
try:
    from zoneinfo import ZoneInfo
    ET = ZoneInfo("America/New_York")
except ImportError:
    try:
        import pytz
        ET = pytz.timezone("US/Eastern")
    except ImportError:
        ET = None

def get_et_time():
    return datetime.now(ET) if ET else datetime.now(timezone.utc)

# -------------------------------------------------------------------
# DATA FETCHING (Original Binance sources untouched)
# -------------------------------------------------------------------
async def get_btc_price_at_timestamp(timestamp_sec: int):
    minute_start = timestamp_sec - (timestamp_sec % 60)
    url = f"https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval=1m&startTime={minute_start*1000}&limit=1"
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(url, timeout=10) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if data and len(data) > 0:
                        return float(data[0][1])
        except Exception as e:
            print(f"Error fetching historical price: {e}", flush=True)
    return None

async def get_btc_price_now():
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get("https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT", timeout=5) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return float(data["price"])
        except Exception:
            pass
    return None

async def get_odds(token_id: str):
    if not token_id:
        return None
    url = f"{HOST}/book?token_id={token_id}"
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(url, timeout=5) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    asks = data.get("asks", [])
                    if asks:
                        return float(asks[0]["price"])
        except Exception:
            pass
    return None

async def get_token_ids(slug: str):
    url = f"https://gamma-api.polymarket.com/markets/slug/{slug}"
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(url, timeout=10) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    token_ids_raw = data.get("clobTokenIds")
                    if isinstance(token_ids_raw, str):
                        token_ids = json.loads(token_ids_raw)
                    else:
                        token_ids = token_ids_raw
                    if token_ids and len(token_ids) >= 2:
                        return token_ids[0], token_ids[1]
        except Exception:
            pass
    return None, None

# -------------------------------------------------------------------
# TRADE EXECUTION (with 404 handling)
# -------------------------------------------------------------------
async def execute_trade(prediction, token_up, token_down, size, order_type="MARKET"):
    if prediction not in ("UP", "DOWN"): return None
    token_to_buy = token_up if prediction == "UP" else token_down
    if not token_to_buy:
        print("❌ No token ID", flush=True); return None

    current_odds = await get_odds(token_to_buy)
    est_price = current_odds if current_odds else 0.55

    MIN_LIMIT = 5.0; MIN_MARKET_NOTIONAL = 1.0
    if order_type == "LIMIT":
        if size < MIN_LIMIT: size = MIN_LIMIT
    else:
        if size * est_price < MIN_MARKET_NOTIONAL:
            size = math.ceil(MIN_MARKET_NOTIONAL / est_price)
            print(f"⚠️ Adjusting size to {size} shares", flush=True)

    try:
        loop = asyncio.get_running_loop()
        tick_size_str = await loop.run_in_executor(None, clob_client.get_tick_size, token_to_buy)
        tick_size = float(tick_size_str)
    except:
        tick_size = 0.01

    limit_price = round(current_odds / tick_size) * tick_size if current_odds else 0.52
    limit_price = round(limit_price, len(str(tick_size).split('.')[-1]))

    print(f"\n🚀 {order_type} ORDER – {prediction}", flush=True)
    print(f"   Size: {size} shares | Price: ${limit_price:.4f} | Cost: ~${size*est_price:.2f}", flush=True)

    ws_ts = int(get_et_time().timestamp())
    ws_time = get_et_time().strftime("%Y-%m-%d %H:%M:%S ET")
    trade_db_id = db.log_trade(ws_ts, ws_time, prediction, 0.0, token_to_buy, order_type, size, limit_price, None, None, 'submitted', size*est_price)

    try:
        if order_type == "LIMIT":
            args = OrderArgs(price=limit_price, size=size, side=BUY, token_id=token_to_buy)
            resp = await loop.run_in_executor(None, clob_client.create_and_post_order, args)
        else:
            args = MarketOrderArgs(token_id=token_to_buy, amount=size, side=BUY, order_type=1)
            resp = await loop.run_in_executor(None, clob_client.create_market_order, args)

        if resp and resp.get("success"):
            oid = resp.get("orderID")
            fill = resp.get("avgPrice", limit_price)
            print(f"✅ Order FILLED! ID: {oid}", flush=True)
            db.update_trade_status(trade_db_id, "filled", float(fill) if fill else None)
            return {"db_id": trade_db_id, "order_id": oid, "token_id": token_to_buy, "size": size, "fill_price": float(fill) if fill else limit_price, "prediction": prediction, "timestamp": time.time()}
        else:
            err_msg = resp.get('errorMsg', 'Unknown error')
            if "404" in str(err_msg) and "market not found" in str(err_msg):
                print(f"⚠️ Market not found on testnet – skipping trade.", flush=True)
            else:
                print(f"❌ Order FAILED: {err_msg}", flush=True)
            db.update_trade_status(trade_db_id, "failed")
            return None
    except Exception as e:
        err_str = str(e)
        if "404" in err_str and "market not found" in err_str:
            print(f"⚠️ Market not found on testnet – skipping trade.", flush=True)
        else:
            print(f"❌ ERROR: {e}", flush=True)
        db.update_trade_status(trade_db_id, "failed")
        return None

# -------------------------------------------------------------------
# MAIN LOOP (Original prediction logic untouched)
# -------------------------------------------------------------------
async def main():
    print("=" * 60, flush=True)
    print("Polymarket Prediction Bot – with Live Terminal", flush=True)
    print("=" * 60, flush=True)
    print(f"Mode: {'TESTNET' if USE_TESTNET else 'MAINNET'}", flush=True)
    print(f"Trade size: {TRADE_SIZE} shares | Confidence threshold: {CONFIDENCE_THRESHOLD:.0%}", flush=True)
    print("Press Ctrl+C to stop\n", flush=True)

    tracker = AccuracyTracker()
    last_window_start = None
    price_to_beat = None
    token_up = token_down = None
    last_prediction = None
    active_position = None

    while True:
        try:
            now_et = get_et_time()
            minute = now_et.minute
            window_minute = (minute // 5) * 5
            window_start_et = now_et.replace(minute=window_minute, second=0, microsecond=0)
            window_end_et = window_start_et + timedelta(minutes=5)
            window_start_ts = int(window_start_et.timestamp())

            if window_start_ts != last_window_start:
                if last_window_start is not None and last_prediction is not None:
                    end_price = await get_btc_price_at_timestamp(last_window_start + 300)
                    if end_price and price_to_beat:
                        actual = "UP" if end_price >= price_to_beat else "DOWN"
                        tracker.add_result(last_window_start, last_prediction, actual)
                        
                        if active_position:
                            correct = (active_position["prediction"] == actual)
                            sz = active_position["size"]
                            fill = active_position["fill_price"]
                            payout = sz if correct else 0.0
                            cost = sz * fill
                            pnl = payout - cost
                            db.log_settlement(active_position["db_id"], last_window_start+300,
                                              datetime.fromtimestamp(last_window_start+300).strftime("%Y-%m-%d %H:%M:%S ET"),
                                              price_to_beat, end_price, actual, correct, payout, pnl)
                            db.update_performance_summary()
                            outcome = "✅ WON" if correct else "❌ LOST"
                            print(f"\n🏁 Position settled: {outcome} | P&L: ${pnl:.2f} USDC", flush=True)
                            active_position = None
                        
                        tracker.summary()
                        print_last_trades(5)
                    else:
                        print(f"⚠️ Could not get end price for window {last_window_start}", flush=True)

                print(f"\n📌 New window: {window_start_et.strftime('%H:%M:%S ET')} → {window_end_et.strftime('%H:%M:%S ET')}", flush=True)
                price_to_beat = await get_btc_price_at_timestamp(window_start_ts)
                if price_to_beat:
                    print(f"   ✅ Price to beat: ${price_to_beat:,.2f}", flush=True)
                    last_window_start = window_start_ts
                    slug = f"btc-updown-5m-{window_start_ts}"
                    token_up, token_down = await get_token_ids(slug)
                    last_prediction = None
                else:
                    print("   ❌ Could not fetch price to beat. Retrying in 10s...", flush=True)
                    await asyncio.sleep(10)
                    continue

            now_ts = int(get_et_time().timestamp())
            seconds_into = now_ts - window_start_ts
            if seconds_into < 120:
                await asyncio.sleep(120 - seconds_into)
                continue

            print("\n📈 Collecting live BTC prices (30s)...", flush=True)
            prices = []
            for i in range(6):
                p = await get_btc_price_now()
                if p:
                    prices.append(p)
                    print(f"   [{i+1}/6] BTC: ${p:,.2f} (vs beat: {p - price_to_beat:+.2f})", flush=True)
                await asyncio.sleep(5)
            if len(prices) < 2:
                print("⚠️ Not enough price data. Skipping.", flush=True)
                continue

            current = prices[-1]
            change_pct = (current - price_to_beat) / price_to_beat * 100
            if change_pct > 0.02:
                pred = "UP"
                conf = min(0.85, 0.5 + change_pct/20)
            elif change_pct < -0.02:
                pred = "DOWN"
                conf = min(0.85, 0.5 - change_pct/20)
            else:
                pred = "NEUTRAL"
                conf = 0.5

            if pred in ("UP", "DOWN"):
                last_prediction = pred

            up_odds = await get_odds(token_up) if token_up else None
            down_odds = await get_odds(token_down) if token_down else None
            odd = up_odds if pred == "UP" else down_odds

            print("\n" + "=" * 50, flush=True)
            print(f"🎯 PREDICTION: {pred}", flush=True)
            print(f"   Confidence: {conf:.1%}", flush=True)
            print(f"   Current BTC: ${current:,.2f}", flush=True)
            print(f"   Price to beat: ${price_to_beat:,.2f}", flush=True)
            print(f"   Difference: {current - price_to_beat:+.2f} ({change_pct:+.2f}%)", flush=True)
            if pred != "NEUTRAL" and odd:
                print(f"   Live odds for {pred}: {odd:.4f}", flush=True)
            print("=" * 50, flush=True)

            db.log_prediction(window_start_ts, window_start_et.strftime("%Y-%m-%d %H:%M:%S ET"),
                              price_to_beat, current, change_pct, pred, conf, up_odds, down_odds, False)

            if pred in ("UP", "DOWN") and conf >= CONFIDENCE_THRESHOLD and not active_position:
                if not token_up or not token_down:
                    print("⚠️ Token IDs missing – skipping trade.", flush=True)
                else:
                    position = await execute_trade(pred, token_up, token_down, TRADE_SIZE, ORDER_TYPE)
                    if position:
                        active_position = position
                        db.log_prediction(window_start_ts, window_start_et.strftime("%Y-%m-%d %H:%M:%S ET"),
                                          price_to_beat, current, change_pct, pred, conf, up_odds, down_odds, True)
            elif pred in ("UP", "DOWN") and conf < CONFIDENCE_THRESHOLD:
                print(f"🔍 Confidence {conf:.1%} below threshold. No trade.", flush=True)
            else:
                if pred == "NEUTRAL": print("⚪ Neutral prediction, no trade.", flush=True)

            remaining = window_end_et.timestamp() - get_et_time().timestamp()
            await asyncio.sleep(max(0, remaining + 2))

        except Exception as e:
            print(f"Error: {e}", flush=True)
            await asyncio.sleep(5)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n👋 Bot stopped.", flush=True)
