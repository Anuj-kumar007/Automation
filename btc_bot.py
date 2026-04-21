#!/usr/bin/env python3
"""
BTC 5-Minute Polymarket Trading Bot
Features:
- Automatic prediction based on BTC price movement
- Market/Limit order execution with size validation
- SQLite database logging for all trades and predictions
- Web terminal for live monitoring
- Testnet/Mainnet switch via .env
"""

import os
import sys
import threading
import json
import asyncio
import aiohttp
import time
import sqlite3
import math
from datetime import datetime, timedelta, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler
from dotenv import load_dotenv

# Polymarket CLOB imports
from py_clob_client.client import ClobClient
from py_clob_client.constants import POLYGON, AMOY
from py_clob_client.clob_types import OrderArgs, MarketOrderArgs, OrderType
from py_clob_client.order_builder.constants import BUY

# Load environment variables
load_dotenv()

# ============================================================
# CONFIGURATION
# ============================================================
USE_TESTNET = os.getenv("USE_TESTNET", "true").lower() == "true"
TRADE_SIZE = float(os.getenv("TRADE_SIZE", "5.0"))
CONFIDENCE_THRESHOLD = float(os.getenv("CONFIDENCE_THRESHOLD", "0.65"))
ORDER_TYPE = os.getenv("ORDER_TYPE", "MARKET").upper()

if USE_TESTNET:
    HOST = "https://clob-staging.polymarket.com"
    CHAIN_ID = AMOY
else:
    HOST = "https://clob.polymarket.com"
    CHAIN_ID = POLYGON

# ============================================================
# LOG CAPTURE & WEB TERMINAL
# ============================================================
log_buffer = []
log_lock = threading.Lock()

class TeeWriter:
    def __init__(self, original):
        self.original = original

    def write(self, text):
        if text.strip():
            timestamp = datetime.now().strftime("%H:%M:%S")
            with log_lock:
                log_buffer.append(f"[{timestamp}] {text.rstrip()}")
                while len(log_buffer) > 200:
                    log_buffer.pop(0)
        self.original.write(text)
        self.original.flush()

    def flush(self):
        self.original.flush()

sys.stdout = TeeWriter(sys.stdout)

class TerminalHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/":
            self.send_response(200)
            self.send_header("Content-type", "text/html")
            self.end_headers()
            html = """<!DOCTYPE html><html><head><title>BTC Bot Terminal</title><meta charset="UTF-8"><style>
                * { margin:0; padding:0; box-sizing:border-box; }
                body { background:#0C0C0C; font-family:'Consolas',monospace; height:100vh; display:flex; flex-direction:column; overflow:hidden; }
                .terminal-bar { background:#2D2D2D; padding:8px 12px; display:flex; align-items:center; gap:12px; border-bottom:1px solid #3C3C3C; }
                .window-controls { display:flex; gap:8px; } .control { width:12px; height:12px; border-radius:50%; }
                .red { background:#FF5F56; } .yellow { background:#FFBD2E; } .green { background:#27C93F; }
                .terminal-title { color:#E5E5E5; font-size:13px; flex-grow:1; text-align:center; }
                .log-container { flex:1; overflow-y:auto; padding:16px 20px; background:#0C0C0C; }
                .log-line { font-family:monospace; font-size:14px; line-height:1.5; white-space:pre-wrap; color:#D4D4D4; padding-left:8px; margin-bottom:2px; }
                .log-line.error { color:#F48771; } .log-line.success { color:#6A9955; } .log-line.warning { color:#DCDCAA; }
                .cursor { display:inline-block; width:8px; height:16px; background-color:#D4D4D4; animation:blink 1s step-end infinite; }
                @keyframes blink { 0%,100%{opacity:1} 50%{opacity:0} }
                .footer { background:#1E1E1E; padding:6px 12px; font-size:11px; color:#858585; border-top:1px solid #3C3C3C; }
                ::-webkit-scrollbar { width:8px; } ::-webkit-scrollbar-track { background:#1E1E1E; } ::-webkit-scrollbar-thumb { background:#424242; border-radius:4px; }
            </style></head><body>
            <div class="terminal-bar"><div class="window-controls"><div class="control red"></div><div class="control yellow"></div><div class="control green"></div></div>
            <div class="terminal-title">BTC Prediction Bot – Live Trading Terminal</div><div style="width:48px;"></div></div>
            <div class="log-container" id="logContainer"><div id="logContent">Loading terminal output...</div></div>
            <div class="footer">Live output | Auto‑scroll | Trades executed automatically</div>
            <script>
                function escapeHtml(t) { return t.replace(/[&<>]/g, m => m==='&'?'&amp;':m==='<'?'&lt;':'&gt;'); }
                function fetchLog() {
                    fetch('/api/log')
                        .then(res => res.json())
                        .then(data => {
                            const container = document.getElementById('logContent');
                            const scrollDiv = document.getElementById('logContainer');
                            const logs = data.logs || [];
                            let html = '';
                            logs.forEach(line => {
                                let cls = 'log-line';
                                if (line.includes('❌') || line.includes('Error')) cls += ' error';
                                else if (line.includes('✅') || line.includes('🎯') || line.includes('SUCCESS')) cls += ' success';
                                else if (line.includes('⚠️')) cls += ' warning';
                                html += `<div class="${cls}">${escapeHtml(line)}</div>`;
                            });
                            html += `<div class="log-line">█<span class="cursor"></span></div>`;
                            if (container.innerHTML !== html) {
                                const wasAtBottom = scrollDiv.scrollHeight - scrollDiv.scrollTop <= scrollDiv.clientHeight + 50;
                                container.innerHTML = html;
                                if (wasAtBottom) scrollDiv.scrollTop = scrollDiv.scrollHeight;
                            }
                        });
                }
                setInterval(fetchLog, 2000);
                fetchLog();
            </script></body></html>"""
            self.wfile.write(html.encode())
        elif self.path == "/api/log":
            self.send_response(200)
            self.send_header("Content-type", "application/json")
            self.end_headers()
            with log_lock:
                logs = log_buffer.copy()
            self.wfile.write(json.dumps({"logs": logs}).encode())
        else:
            self.send_response(404)
            self.end_headers()

def run_web_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(('0.0.0.0', port), TerminalHandler)
    server.serve_forever()

threading.Thread(target=run_web_server, daemon=True).start()
print(f"🌐 Web terminal running on port {os.environ.get('PORT', 10000)}", flush=True)

# ============================================================
# DATABASE SETUP
# ============================================================
DB_PATH = "btc_bot_trades.db"

class TradeDatabase:
    def __init__(self, db_path=DB_PATH):
        self.db_path = db_path
        self._thread_local = threading.local()
        self._init_database()

    def _get_connection(self):
        if not hasattr(self._thread_local, 'conn'):
            self._thread_local.conn = sqlite3.connect(self.db_path, check_same_thread=False)
            self._thread_local.conn.execute("PRAGMA journal_mode=WAL")
            self._thread_local.conn.row_factory = sqlite3.Row
        return self._thread_local.conn

    def _init_database(self):
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                window_start_ts INTEGER NOT NULL,
                window_start_time TEXT NOT NULL,
                prediction TEXT NOT NULL,
                confidence REAL NOT NULL,
                token_id TEXT NOT NULL,
                order_type TEXT NOT NULL,
                size REAL NOT NULL,
                limit_price REAL,
                fill_price REAL,
                order_id TEXT,
                order_status TEXT DEFAULT 'pending',
                cost_usdc REAL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS settlements (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                trade_id INTEGER,
                window_end_ts INTEGER NOT NULL,
                window_end_time TEXT NOT NULL,
                price_to_beat REAL NOT NULL,
                settlement_price REAL NOT NULL,
                actual_outcome TEXT NOT NULL,
                prediction_correct BOOLEAN NOT NULL,
                payout_usdc REAL NOT NULL,
                pnl_usdc REAL NOT NULL,
                settled_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (trade_id) REFERENCES trades(id)
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS predictions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                window_start_ts INTEGER NOT NULL,
                window_start_time TEXT NOT NULL,
                price_to_beat REAL NOT NULL,
                current_btc REAL NOT NULL,
                change_pct REAL NOT NULL,
                prediction TEXT NOT NULL,
                confidence REAL NOT NULL,
                up_odds REAL,
                down_odds REAL,
                trade_executed BOOLEAN DEFAULT FALSE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS performance_summary (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT NOT NULL UNIQUE,
                total_trades INTEGER DEFAULT 0,
                winning_trades INTEGER DEFAULT 0,
                losing_trades INTEGER DEFAULT 0,
                total_pnl_usdc REAL DEFAULT 0,
                win_rate REAL DEFAULT 0,
                avg_pnl_per_trade REAL DEFAULT 0,
                total_volume_usdc REAL DEFAULT 0,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_trades_window ON trades(window_start_ts)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_settlements_trade ON settlements(trade_id)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_predictions_window ON predictions(window_start_ts)")
        conn.commit()

    def log_trade(self, window_start_ts, window_start_time, prediction, confidence,
                  token_id, order_type, size, limit_price=None, fill_price=None,
                  order_id=None, cost_usdc=None):
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO trades
            (window_start_ts, window_start_time, prediction, confidence, token_id,
             order_type, size, limit_price, fill_price, order_id, order_status, cost_usdc)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (window_start_ts, window_start_time, prediction, confidence, token_id,
              order_type, size, limit_price, fill_price, order_id, 'submitted', cost_usdc))
        conn.commit()
        return cursor.lastrowid

    def update_trade_status(self, trade_id, order_status, fill_price=None):
        conn = self._get_connection()
        cursor = conn.cursor()
        if fill_price:
            cursor.execute("UPDATE trades SET order_status=?, fill_price=? WHERE id=?", (order_status, fill_price, trade_id))
        else:
            cursor.execute("UPDATE trades SET order_status=? WHERE id=?", (order_status, trade_id))
        conn.commit()

    def log_settlement(self, trade_id, window_end_ts, window_end_time, price_to_beat,
                       settlement_price, actual_outcome, prediction_correct,
                       payout_usdc, pnl_usdc):
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO settlements
            (trade_id, window_end_ts, window_end_time, price_to_beat,
             settlement_price, actual_outcome, prediction_correct, payout_usdc, pnl_usdc)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (trade_id, window_end_ts, window_end_time, price_to_beat,
              settlement_price, actual_outcome, prediction_correct, payout_usdc, pnl_usdc))
        conn.commit()

    def log_prediction(self, window_start_ts, window_start_time, price_to_beat,
                       current_btc, change_pct, prediction, confidence,
                       up_odds, down_odds, trade_executed):
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO predictions
            (window_start_ts, window_start_time, price_to_beat, current_btc,
             change_pct, prediction, confidence, up_odds, down_odds, trade_executed)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (window_start_ts, window_start_time, price_to_beat, current_btc,
              change_pct, prediction, confidence, up_odds, down_odds, trade_executed))
        conn.commit()

    def update_performance_summary(self):
        conn = self._get_connection()
        cursor = conn.cursor()
        today = datetime.now().strftime("%Y-%m-%d")
        cursor.execute("""
            SELECT
                COUNT(*) as total_trades,
                SUM(CASE WHEN s.pnl_usdc > 0 THEN 1 ELSE 0 END) as winning_trades,
                SUM(CASE WHEN s.pnl_usdc <= 0 THEN 1 ELSE 0 END) as losing_trades,
                COALESCE(SUM(s.pnl_usdc), 0) as total_pnl,
                COALESCE(AVG(s.pnl_usdc), 0) as avg_pnl,
                COALESCE(SUM(t.cost_usdc), 0) as total_volume
            FROM settlements s
            JOIN trades t ON s.trade_id = t.id
            WHERE DATE(s.settled_at) = ?
        """, (today,))
        row = cursor.fetchone()
        if row and row['total_trades'] > 0:
            win_rate = (row['winning_trades'] / row['total_trades']) * 100
            cursor.execute("""
                INSERT INTO performance_summary
                (date, total_trades, winning_trades, losing_trades, total_pnl_usdc,
                 win_rate, avg_pnl_per_trade, total_volume_usdc)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(date) DO UPDATE SET
                    total_trades = excluded.total_trades,
                    winning_trades = excluded.winning_trades,
                    losing_trades = excluded.losing_trades,
                    total_pnl_usdc = excluded.total_pnl_usdc,
                    win_rate = excluded.win_rate,
                    avg_pnl_per_trade = excluded.avg_pnl_per_trade,
                    total_volume_usdc = excluded.total_volume_usdc,
                    updated_at = CURRENT_TIMESTAMP
            """, (today, row['total_trades'], row['winning_trades'],
                  row['losing_trades'], row['total_pnl'], win_rate,
                  row['avg_pnl'], row['total_volume']))
        conn.commit()

db = TradeDatabase()

# ============================================================
# CLOB CLIENT INITIALIZATION
# ============================================================
def init_clob_client():
    """Initialize and return a configured ClobClient."""
    api_creds = {
        "key": os.getenv("POLY_BUILDER_API_KEY"),
        "secret": os.getenv("POLY_BUILDER_SECRET"),
        "passphrase": os.getenv("POLY_BUILDER_PASSPHRASE"),
    }
    client = ClobClient(
        HOST,
        key=os.getenv("POLY_PRIVATE_KEY"),
        chain_id=CHAIN_ID,
        signature_type=1,
        funder=os.getenv("POLY_FUNDER_ADDRESS"),
        creds=api_creds
    )
    # Explicitly set API credentials (required for authenticated calls)
    try:
        client.set_api_creds(client.create_or_derive_api_creds())
    except Exception:
        pass  # Some versions auto-set; ignore if it fails
    return client

clob_client = init_clob_client()
print("✅ CLOB Client initialized", flush=True)

# ============================================================
# TIMEZONE HANDLING
# ============================================================
try:
    from zoneinfo import ZoneInfo
    ET = ZoneInfo("America/New_York")
except ImportError:
    ET = None

def get_et_time():
    return datetime.now(ET) if ET else datetime.now(timezone.utc)

# ============================================================
# DATA FETCHING FUNCTIONS
# ============================================================
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
        except Exception:
            pass
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
                    token_ids = data.get("clobTokenIds", [])
                    if len(token_ids) >= 2:
                        return token_ids[0], token_ids[1]
        except Exception:
            pass
    return None, None

# ============================================================
# TRADE EXECUTION
# ============================================================
async def execute_trade(prediction, token_up, token_down, size, order_type="MARKET"):
    """Places an order with size validation."""
    if prediction not in ("UP", "DOWN"):
        return None

    token_to_buy = token_up if prediction == "UP" else token_down
    if not token_to_buy:
        print("❌ No token ID", flush=True)
        return None

    current_odds = await get_odds(token_to_buy)
    estimated_price = current_odds if current_odds else 0.55

    # Minimum size validation
    MIN_LIMIT_SHARES = 5.0
    MIN_MARKET_NOTIONAL = 1.00

    if order_type == "LIMIT":
        if size < MIN_LIMIT_SHARES:
            size = MIN_LIMIT_SHARES
    else:
        if size * estimated_price < MIN_MARKET_NOTIONAL:
            required_size = math.ceil(MIN_MARKET_NOTIONAL / estimated_price)
            print(f"⚠️ Adjusting size to {required_size} shares (min $1 notional)", flush=True)
            size = required_size

    estimated_cost = size * estimated_price

    # Fetch tick size
    try:
        loop = asyncio.get_running_loop()
        tick_size_str = await loop.run_in_executor(None, clob_client.get_tick_size, token_to_buy)
        tick_size = float(tick_size_str)
    except Exception:
        tick_size = 0.01

    if current_odds:
        limit_price = round(current_odds / tick_size) * tick_size
        limit_price = round(limit_price, len(str(tick_size).split('.')[-1]))
    else:
        limit_price = 0.52

    print(f"\n🚀 {order_type} ORDER – {prediction}", flush=True)
    print(f"   Size: {size} shares | Price: ${limit_price:.4f} | Cost: ~${estimated_cost:.2f}", flush=True)

    window_start_ts = int(get_et_time().timestamp())
    window_start_time = get_et_time().strftime("%Y-%m-%d %H:%M:%S ET")
    trade_db_id = db.log_trade(
        window_start_ts=window_start_ts,
        window_start_time=window_start_time,
        prediction=prediction,
        confidence=0.0,  # Will be updated in prediction log
        token_id=token_to_buy,
        order_type=order_type,
        size=size,
        limit_price=limit_price,
        cost_usdc=estimated_cost
    )

    try:
        if order_type == "LIMIT":
            order_args = OrderArgs(price=limit_price, size=size, side=BUY, token_id=token_to_buy)
            order_response = await loop.run_in_executor(None, clob_client.create_and_post_order, order_args)
        else:
            order_args = MarketOrderArgs(token_id=token_to_buy, amount=size, side=BUY, order_type=OrderType.MARKET)
            order_response = await loop.run_in_executor(None, clob_client.create_market_order, order_args)

        if order_response and order_response.get("success"):
            order_id = order_response.get("orderID")
            fill_price = order_response.get("avgPrice", limit_price)
            print(f"✅ Order FILLED! ID: {order_id}", flush=True)
            db.update_trade_status(trade_db_id, "filled", float(fill_price) if fill_price else None)
            return {
                "db_id": trade_db_id,
                "order_id": order_id,
                "token_id": token_to_buy,
                "size": size,
                "fill_price": float(fill_price) if fill_price else limit_price,
                "prediction": prediction,
                "timestamp": time.time()
            }
        else:
            error = order_response.get("errorMsg", "Unknown error")
            print(f"❌ Order FAILED: {error}", flush=True)
            db.update_trade_status(trade_db_id, "failed")
            return None
    except Exception as e:
        print(f"❌ ERROR: {e}", flush=True)
        db.update_trade_status(trade_db_id, "failed")
        return None

# ============================================================
# MAIN LOOP
# ============================================================
async def main():
    print("=" * 60, flush=True)
    print("BTC 5-Minute Prediction Bot – FULL AUTO TRADING", flush=True)
    print("=" * 60, flush=True)
    print(f"Mode: {'TESTNET' if USE_TESTNET else 'MAINNET'}", flush=True)
    print(f"Trade size: {TRADE_SIZE} shares | Confidence threshold: {CONFIDENCE_THRESHOLD:.0%}", flush=True)
    print("Press Ctrl+C to stop\n", flush=True)

    active_position = None
    last_window_start = None
    price_to_beat = None
    token_up = token_down = None
    last_prediction = None

    while True:
        try:
            now_et = get_et_time()
            minute = now_et.minute
            window_minute = (minute // 5) * 5
            window_start_et = now_et.replace(minute=window_minute, second=0, microsecond=0)
            window_end_et = window_start_et + timedelta(minutes=5)
            window_start_ts = int(window_start_et.timestamp())

            # New window detected
            if window_start_ts != last_window_start:
                # Settle previous position
                if last_window_start is not None and active_position is not None:
                    end_price = await get_btc_price_at_timestamp(last_window_start + 300)
                    if end_price and price_to_beat:
                        actual = "UP" if end_price >= price_to_beat else "DOWN"
                        is_correct = (active_position["prediction"] == actual)
                        size = active_position["size"]
                        fill_price = active_position["fill_price"]
                        payout = size if is_correct else 0.0
                        cost = size * fill_price
                        pnl = payout - cost
                        db.log_settlement(
                            trade_id=active_position["db_id"],
                            window_end_ts=last_window_start + 300,
                            window_end_time=datetime.fromtimestamp(last_window_start + 300).strftime("%Y-%m-%d %H:%M:%S ET"),
                            price_to_beat=price_to_beat,
                            settlement_price=end_price,
                            actual_outcome=actual,
                            prediction_correct=is_correct,
                            payout_usdc=payout,
                            pnl_usdc=pnl
                        )
                        db.update_performance_summary()
                        outcome = "✅ WON" if is_correct else "❌ LOST"
                        print(f"\n🏁 Position settled: {outcome} | P&L: ${pnl:.2f} USDC", flush=True)
                    active_position = None

                print(f"\n📌 New window: {window_start_et.strftime('%H:%M:%S ET')} → {window_end_et.strftime('%H:%M:%S ET')}", flush=True)
                price_to_beat = await get_btc_price_at_timestamp(window_start_ts)
                if price_to_beat:
                    print(f"   ✅ Price to beat: ${price_to_beat:,.2f}", flush=True)
                    last_window_start = window_start_ts
                    slug = f"btc-updown-5m-{window_start_ts}"
                    token_up, token_down = await get_token_ids(slug)
                    last_prediction = None
                else:
                    print("   ❌ Could not fetch price to beat. Retrying...", flush=True)
                    await asyncio.sleep(10)
                    continue

            # Wait for market open (first 2 minutes)
            now_ts = int(get_et_time().timestamp())
            seconds_into = now_ts - window_start_ts
            if seconds_into < 120:
                await asyncio.sleep(120 - seconds_into)
                continue

            # Collect live prices (30 seconds)
            print("\n📈 Collecting live BTC prices (30s)...", flush=True)
            prices = []
            for i in range(6):
                p = await get_btc_price_now()
                if p:
                    prices.append(p)
                    diff = p - price_to_beat if price_to_beat else 0
                    print(f"   [{i+1}/6] BTC: ${p:,.2f} (vs beat: {diff:+.2f})", flush=True)
                await asyncio.sleep(5)

            if len(prices) < 2:
                print("⚠️ Not enough price data. Skipping.", flush=True)
                continue

            current = prices[-1]
            change_pct = (current - price_to_beat) / price_to_beat * 100 if price_to_beat else 0

            # Prediction logic
            if change_pct > 0.02:
                pred = "UP"
                conf = min(0.85, 0.5 + change_pct / 20)
            elif change_pct < -0.02:
                pred = "DOWN"
                conf = min(0.85, 0.5 - change_pct / 20)
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

            # Log prediction
            db.log_prediction(
                window_start_ts=window_start_ts,
                window_start_time=window_start_et.strftime("%Y-%m-%d %H:%M:%S ET"),
                price_to_beat=price_to_beat,
                current_btc=current,
                change_pct=change_pct,
                prediction=pred,
                confidence=conf,
                up_odds=up_odds,
                down_odds=down_odds,
                trade_executed=False
            )

            # Execute trade if confident and not already in position
            if pred in ("UP", "DOWN") and conf >= CONFIDENCE_THRESHOLD and not active_position:
                position = await execute_trade(pred, token_up, token_down, TRADE_SIZE, ORDER_TYPE)
                if position:
                    active_position = position
                    # Update prediction record to reflect trade execution
                    db.log_prediction(
                        window_start_ts=window_start_ts,
                        window_start_time=window_start_et.strftime("%Y-%m-%d %H:%M:%S ET"),
                        price_to_beat=price_to_beat,
                        current_btc=current,
                        change_pct=change_pct,
                        prediction=pred,
                        confidence=conf,
                        up_odds=up_odds,
                        down_odds=down_odds,
                        trade_executed=True
                    )
            elif pred in ("UP", "DOWN") and conf < CONFIDENCE_THRESHOLD:
                print(f"🔍 Confidence {conf:.1%} below threshold. No trade.", flush=True)
            else:
                if pred == "NEUTRAL":
                    print("⚪ Neutral prediction, no trade.", flush=True)

            # Wait for next window
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