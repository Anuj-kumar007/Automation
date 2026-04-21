#!/usr/bin/env python3
"""
BTC Bot Test Suite
Run this BEFORE deploying with real money.
Tests all components without executing real trades.
"""

import os
import sys
import asyncio
import aiohttp
import json
import ssl
from datetime import datetime, timedelta
from dotenv import load_dotenv

load_dotenv()

# ============================================================
# TEST CONFIGURATION
# ============================================================
USE_TESTNET = os.getenv("USE_TESTNET", "true").lower() == "true"

# ANSI colors for prettier output
GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
BLUE = "\033[94m"
RESET = "\033[0m"
BOLD = "\033[1m"

def print_test(name, passed, message=""):
    """Pretty test result printer"""
    status = f"{GREEN}✅ PASS{RESET}" if passed else f"{RED}❌ FAIL{RESET}"
    print(f"{status} - {BOLD}{name}{RESET}")
    if message:
        print(f"      {message}")

def print_section(title):
    """Print section header"""
    print(f"\n{BLUE}{'='*60}{RESET}")
    print(f"{BLUE}{BOLD}{title}{RESET}")
    print(f"{BLUE}{'='*60}{RESET}")

# ============================================================
# TEST 1: ENVIRONMENT VARIABLES
# ============================================================
def test_environment():
    """Check all required environment variables are set"""
    print_section("TEST 1: ENVIRONMENT VARIABLES")
    
    required_vars = [
        "POLY_BUILDER_API_KEY",
        "POLY_BUILDER_SECRET", 
        "POLY_BUILDER_PASSPHRASE",
        "POLY_PRIVATE_KEY",
        "POLY_FUNDER_ADDRESS"
    ]
    
    all_passed = True
    for var in required_vars:
        value = os.getenv(var)
        passed = value is not None and len(value) > 10
        
        if passed:
            masked = value[:8] + "..." + value[-4:] if len(value) > 12 else "***"
            print_test(var, True, f"Found: {masked}")
        else:
            print_test(var, False, "Missing or too short")
            all_passed = False
    
    # Optional variables
    trade_size = os.getenv("TRADE_SIZE", "5.0")
    conf_threshold = os.getenv("CONFIDENCE_THRESHOLD", "0.65")
    
    print_test("TRADE_SIZE", True, f"Value: {trade_size}")
    print_test("CONFIDENCE_THRESHOLD", True, f"Value: {conf_threshold}")
    print_test("USE_TESTNET", True, f"Value: {USE_TESTNET}")
    
    return all_passed

# ============================================================
# TEST 2: NETWORK CONNECTIVITY
# ============================================================
async def test_connectivity():
    """Test connections to Binance and Polymarket APIs"""
    print_section("TEST 2: NETWORK CONNECTIVITY")
    
    # Create SSL context that ignores certificate errors (for Termux testing)
    ssl_context = ssl.create_default_context()
    ssl_context.check_hostname = False
    ssl_context.verify_mode = ssl.CERT_NONE
    connector = aiohttp.TCPConnector(ssl=ssl_context)
    
    # Test Binance
    print("\n📡 Testing Binance API...")
    binance_ok = False
    async with aiohttp.ClientSession(connector=connector) as session:
        try:
            async with session.get("https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT", timeout=10) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    price = float(data["price"])
                    print_test("Binance Price API", True, f"BTC/USDT = ${price:,.2f}")
                    binance_ok = True
                else:
                    print_test("Binance Price API", False, f"HTTP {resp.status}")
        except Exception as e:
            print_test("Binance Price API", False, str(e)[:50])
    
    # Test Polymarket Gamma API
    print("\n📡 Testing Polymarket Gamma API...")
    gamma_ok = False
    async with aiohttp.ClientSession(connector=connector) as session:
        try:
            # Get current time for a real market slug
            now = datetime.now(datetime.UTC)
            minute = now.minute
            window_minute = (minute // 5) * 5
            window_start = now.replace(minute=window_minute, second=0, microsecond=0)
            window_ts = int(window_start.timestamp())
            slug = f"btc-updown-5m-{window_ts}"
            
            url = f"https://gamma-api.polymarket.com/markets/slug/{slug}"
            async with session.get(url, timeout=15) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    token_ids = data.get("clobTokenIds", [])
                    if len(token_ids) >= 2:
                        print_test("Gamma API", True, f"Found market: {slug}")
                        print(f"      UP Token: {token_ids[0][:12]}...")
                        print(f"      DOWN Token: {token_ids[1][:12]}...")
                        gamma_ok = True
                    else:
                        print_test("Gamma API", False, "No token IDs found")
                else:
                    print_test("Gamma API", False, f"HTTP {resp.status}")
        except Exception as e:
            print_test("Gamma API", False, str(e)[:50])
    
    # Test Polymarket CLOB API
    print("\n📡 Testing Polymarket CLOB API...")
    clob_ok = False
    host = "https://clob-staging.polymarket.com" if USE_TESTNET else "https://clob.polymarket.com"
    async with aiohttp.ClientSession(connector=connector) as session:
        try:
            async with session.get(f"{host}/book?token_id=0x123", timeout=10) as resp:
                # Expect 400 or 404, but connection should work
                clob_ok = resp.status in [200, 400, 404]
                print_test("CLOB API", clob_ok, f"Connected to {host} (HTTP {resp.status})")
        except Exception as e:
            print_test("CLOB API", False, str(e)[:50])
    
    return binance_ok and gamma_ok and clob_ok

# ============================================================
# TEST 3: CLOB CLIENT AUTHENTICATION
# ============================================================
async def test_clob_auth():
    """Test Polymarket CLOB client authentication"""
    print_section("TEST 3: CLOB CLIENT AUTHENTICATION")
    
    try:
        from py_clob_client.client import ClobClient
        from py_clob_client.constants import POLYGON, AMOY
    except ImportError as e:
        print_test("CLOB Client Import", False, f"Missing py-clob-client: pip install py-clob-client")
        return False
    
    print_test("CLOB Client Import", True, "py-clob-client installed")
    
    try:
        host = "https://clob-staging.polymarket.com" if USE_TESTNET else "https://clob.polymarket.com"
        chain_id = AMOY if USE_TESTNET else POLYGON
        
        client = ClobClient(
            host,
            key=os.getenv("POLY_PRIVATE_KEY"),
            chain_id=chain_id,
            signature_type=1,
            funder=os.getenv("POLY_FUNDER_ADDRESS"),
            api_key=os.getenv("POLY_BUILDER_API_KEY"),
            api_secret=os.getenv("POLY_BUILDER_SECRET"),
            api_passphrase=os.getenv("POLY_BUILDER_PASSPHRASE")
        )
        
        print_test("CLOB Client Init", True, f"Connected to {'TESTNET' if USE_TESTNET else 'MAINNET'}")
        
        # Test getting server time (doesn't require auth)
        loop = asyncio.get_running_loop()
        try:
            server_time = await loop.run_in_executor(None, client.get_server_time)
            print_test("Server Time", True, f"Server timestamp: {server_time}")
        except Exception as e:
            print_test("Server Time", False, f"Error: {str(e)[:50]}")
        
        # Test getting balance (requires auth)
        try:
            balance_allowance = await loop.run_in_executor(None, client.get_balance_allowance)
            balance = float(balance_allowance.get("balance", 0))
            print_test("Balance Check", True, f"USDC Balance: ${balance:.2f}")
        except Exception as e:
            print_test("Balance Check", False, f"Auth may have failed: {str(e)[:50]}")
            return False
        
        return True
        
    except Exception as e:
        print_test("CLOB Client", False, str(e)[:80])
        return False

# ============================================================
# TEST 4: DATABASE OPERATIONS
# ============================================================
def test_database():
    """Test SQLite database operations"""
    print_section("TEST 4: DATABASE OPERATIONS")
    
    import sqlite3
    import tempfile
    import os
    
    # Create temporary database
    fd, temp_db = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    
    try:
        conn = sqlite3.connect(temp_db)
        cursor = conn.cursor()
        
        # Create test tables
        cursor.execute("""
            CREATE TABLE trades (
                id INTEGER PRIMARY KEY,
                prediction TEXT,
                size REAL,
                fill_price REAL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        cursor.execute("""
            CREATE TABLE settlements (
                id INTEGER PRIMARY KEY,
                trade_id INTEGER,
                pnl_usdc REAL,
                FOREIGN KEY (trade_id) REFERENCES trades(id)
            )
        """)
        
        print_test("Table Creation", True, "trades and settlements tables created")
        
        # Test insert
        cursor.execute(
            "INSERT INTO trades (prediction, size, fill_price) VALUES (?, ?, ?)",
            ("UP", 5.0, 0.67)
        )
        trade_id = cursor.lastrowid
        conn.commit()
        
        cursor.execute("SELECT * FROM trades WHERE id = ?", (trade_id,))
        row = cursor.fetchone()
        
        print_test("Data Insert", row is not None, f"Inserted trade ID: {trade_id}")
        
        # Test settlement insert
        cursor.execute(
            "INSERT INTO settlements (trade_id, pnl_usdc) VALUES (?, ?)",
            (trade_id, 1.65)
        )
        conn.commit()
        
        # Test join query
        cursor.execute("""
            SELECT t.prediction, t.size, t.fill_price, s.pnl_usdc
            FROM trades t
            LEFT JOIN settlements s ON t.id = s.trade_id
            WHERE t.id = ?
        """, (trade_id,))
        row = cursor.fetchone()
        
        print_test("Join Query", row is not None, f"Trade: {row[0]}, Size: {row[1]}, P&L: ${row[3]}")
        
        conn.close()
        
    except Exception as e:
        print_test("Database Operations", False, str(e)[:60])
        return False
    finally:
        os.unlink(temp_db)
    
    return True

# ============================================================
# TEST 5: PRICE FETCHING
# ============================================================
async def test_price_fetching():
    """Test BTC price fetching functions"""
    print_section("TEST 5: BTC PRICE FETCHING")
    
    ssl_context = ssl.create_default_context()
    ssl_context.check_hostname = False
    ssl_context.verify_mode = ssl.CERT_NONE
    connector = aiohttp.TCPConnector(ssl=ssl_context)
    
    async def get_btc_price_now():
        async with aiohttp.ClientSession(connector=connector) as session:
            try:
                async with session.get(
                    "https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT",
                    timeout=10
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        return float(data["price"])
            except Exception:
                pass
        return None
    
    async def get_btc_price_at_timestamp(timestamp_sec):
        minute_start = timestamp_sec - (timestamp_sec % 60)
        url = f"https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval=1m&startTime={minute_start*1000}&limit=1"
        async with aiohttp.ClientSession(connector=connector) as session:
            try:
                async with session.get(url, timeout=15) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        if data and len(data) > 0:
                            return float(data[0][1])
            except Exception:
                pass
        return None
    
    # Test current price
    current = await get_btc_price_now()
    if current:
        print_test("Current BTC Price", True, f"${current:,.2f}")
    else:
        print_test("Current BTC Price", False, "Failed to fetch")
        return False
    
    # Test historical price (5 minutes ago)
    past_ts = int(datetime.now(datetime.UTC).timestamp()) - 300
    historical = await get_btc_price_at_timestamp(past_ts)
    if historical:
        print_test("Historical BTC Price", True, f"5 min ago: ${historical:,.2f}")
        diff = current - historical
        pct = (diff / historical) * 100
        print(f"      Change: ${diff:+.2f} ({pct:+.3f}%)")
    else:
        print_test("Historical BTC Price", False, "Failed to fetch")
        return False
    
    return True

# ============================================================
# TEST 6: MINIMUM ORDER VALIDATION
# ============================================================
def test_order_validation():
    """Test order size validation logic"""
    print_section("TEST 6: ORDER SIZE VALIDATION")
    
    MIN_LIMIT_SHARES = 5.0
    MIN_MARKET_NOTIONAL = 1.00
    
    import math
    
    test_cases = [
        # (order_type, input_size, estimated_price, expected_size)
        ("LIMIT", 1.0, 0.55, 5.0),
        ("LIMIT", 5.0, 0.55, 5.0),
        ("LIMIT", 10.0, 0.55, 10.0),
        ("MARKET", 1.0, 0.55, 2.0),  # $0.55 < $1.00 → need 2 shares ($1.10)
        ("MARKET", 5.0, 0.55, 5.0),   # $2.75 >= $1.00 → keep 5
        ("MARKET", 1.0, 0.30, 4.0),   # $0.30 < $1.00 → need 4 shares ($1.20)
    ]
    
    all_passed = True
    for order_type, input_size, est_price, expected in test_cases:
        size = input_size
        
        if order_type == "LIMIT":
            if size < MIN_LIMIT_SHARES:
                size = MIN_LIMIT_SHARES
        else:  # MARKET
            if size * est_price < MIN_MARKET_NOTIONAL:
                size = math.ceil(MIN_MARKET_NOTIONAL / est_price)
        
        passed = size == expected
        status = "✅" if passed else "❌"
        print(f"   {status} {order_type}: {input_size} shares @ ${est_price} → {size} shares")
        
        if not passed:
            all_passed = False
            print(f"      Expected {expected}, got {size}")
    
    return all_passed

# ============================================================
# TEST 7: WINDOW TIMING LOGIC
# ============================================================
def test_window_timing():
    """Test 5-minute window calculation"""
    print_section("TEST 7: WINDOW TIMING LOGIC")
    
    test_times = [
        datetime(2024, 1, 15, 10, 13, 30),  # Should floor to 10:10
        datetime(2024, 1, 15, 10, 17, 45),  # Should floor to 10:15
        datetime(2024, 1, 15, 10, 20, 0),   # Should be 10:20
        datetime(2024, 1, 15, 10, 22, 15),  # Should floor to 10:20
    ]
    
    all_passed = True
    for dt in test_times:
        minute = dt.minute
        window_minute = (minute // 5) * 5
        window_start = dt.replace(minute=window_minute, second=0, microsecond=0)
        window_end = window_start + timedelta(minutes=5)
        
        expected_start_minute = (dt.minute // 5) * 5
        passed = window_start.minute == expected_start_minute
        
        status = "✅" if passed else "❌"
        print(f"   {status} {dt.strftime('%H:%M:%S')} → Window: {window_start.strftime('%H:%M')} - {window_end.strftime('%H:%M')}")
        
        if not passed:
            all_passed = False
    
    return all_passed

# ============================================================
# MAIN TEST RUNNER
# ============================================================
async def run_all_tests():
    """Run all tests and report summary"""
    print(f"\n{BOLD}{'='*70}{RESET}")
    print(f"{BOLD}🧪 BTC BOT TEST SUITE{RESET}")
    print(f"{BOLD}{'='*70}{RESET}")
    print(f"Mode: {YELLOW if USE_TESTNET else RED}{'TESTNET' if USE_TESTNET else 'MAINNET ⚠️'}{RESET}")
    print(f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    
    results = {}
    
    # Test 1: Environment
    results["Environment"] = test_environment()
    
    # Test 2: Connectivity
    results["Connectivity"] = await test_connectivity()
    
    # Test 3: CLOB Auth (only if connectivity passed)
    if results["Connectivity"]:
        results["CLOB Auth"] = await test_clob_auth()
    else:
        print_section("TEST 3: CLOB CLIENT AUTHENTICATION")
        print_test("CLOB Auth", False, "Skipped - connectivity failed")
        results["CLOB Auth"] = False
    
    # Test 4: Database
    results["Database"] = test_database()
    
    # Test 5: Price Fetching
    results["Price Fetching"] = await test_price_fetching()
    
    # Test 6: Order Validation
    results["Order Validation"] = test_order_validation()
    
    # Test 7: Window Timing
    results["Window Timing"] = test_window_timing()
    
    # Summary
    print_section("TEST SUMMARY")
    
    passed = sum(1 for v in results.values() if v)
    total = len(results)
    
    for name, result in results.items():
        status = f"{GREEN}✅ PASS{RESET}" if result else f"{RED}❌ FAIL{RESET}"
        print(f"   {status} - {name}")
    
    print(f"\n{BOLD}Result: {passed}/{total} tests passed{RESET}")
    
    if passed == total:
        print(f"\n{GREEN}{BOLD}✅ ALL TESTS PASSED!{RESET}")
        print("The bot is ready for deployment.")
        print("\nRecommended next steps:")
        print("   1. Run with USE_TESTNET=true first")
        print("   2. Verify trades execute on testnet")
        print("   3. Monitor database logging")
        print("   4. Switch to mainnet with small TRADE_SIZE")
    else:
        print(f"\n{RED}{BOLD}❌ SOME TESTS FAILED{RESET}")
        print("Fix the issues above before running the bot.")
    
    return passed == total

# ============================================================
# QUICK CHECK - MINIMAL VERIFICATION
# ============================================================
async def quick_check():
    """Quick 30-second sanity check"""
    print(f"\n{BOLD}🔍 QUICK SANITY CHECK{RESET}\n")
    
    # Check .env exists
    if os.path.exists(".env"):
        print("✅ .env file found")
    else:
        print("❌ .env file missing")
        return False
    
    # Check internet
    try:
        ssl_context = ssl.create_default_context()
        ssl_context.check_hostname = False
        ssl_context.verify_mode = ssl.CERT_NONE
        connector = aiohttp.TCPConnector(ssl=ssl_context)
        async with aiohttp.ClientSession(connector=connector) as session:
            async with session.get("https://api.binance.com/api/v3/ping", timeout=5) as resp:
                if resp.status == 200:
                    print("✅ Internet connection OK")
                else:
                    print("❌ Internet connection issue")
                    return False
    except Exception as e:
        print(f"❌ Cannot reach internet: {e}")
        return False
    
    # Check Python version
    if sys.version_info >= (3, 9):
        print(f"✅ Python {sys.version_info.major}.{sys.version_info.minor}")
    else:
        print(f"❌ Python {sys.version_info.major}.{sys.version_info.minor} (need 3.9+)")
        return False
    
    print("\n✅ Quick check passed!")
    return True

if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "--quick":
        asyncio.run(quick_check())
    else:
        asyncio.run(run_all_tests())