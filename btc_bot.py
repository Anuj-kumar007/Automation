
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
from datetime import datetime, timedelta
from http.server import HTTPServer, BaseHTTPRequestHandler
from dotenv import load_dotenv
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs, MarketOrderArgs, OrderType
from py_clob_client.order_builder.constants import BUY, SELL
from py_clob_client.constants import POLYGON, AMOY

load_dotenv()

# ============================================================
# CONFIGURATION
# ============================================================
USE_TESTNET = os.getenv("USE_TESTNET", "true").lower() == "true"
TRADE_SIZE = float(os.getenv("TRADE_SIZE", "5.0"))
CONFIDENCE_THRESHOLD = float(os.getenv("CONFIDENCE_THRESHOLD", "0.65"))
ORDER_TYPE = os.getenv("ORDER_TYPE", "MARKET").upper()  # MARKET or LIMIT

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
            html = """<!DOCTYPE html><html><head><title>BTC Bot Terminal</title>...</head><body>...</body></html>"""
            self.wfile.write(html.encode())
        elif self.path == "/api/log":
            self.send_response(200)
            self.send_header("Content-type", "application/json")
            self.end_headers()
            with log_lock:
                logs = log_buffer.copy()
            self.wfile.write(json.dumps({"logs": logs}).encode())

def run_web_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(('0.0.0.0', port), TerminalHandler)
    server.serve_forever()

threading.Thread(target=run_web_server, daemon=True).start()

# ============================================================
# DATABASE SETUP
# ============================================================
class TradeDatabase:
    # ... (full implementation as provided earlier) ...
    pass

db = TradeDatabase()

# ============================================================
# TRADE EXECUTION
# ============================================================
async def execute_trade(prediction, token_up, token_down, size, order_type):
    # ... (full implementation with min size validation) ...
    pass

# ============================================================
# MAIN LOOP
# ============================================================
async def main():
    # ... (full implementation) ...
    pass

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nBot stopped.")
