import os, hashlib, hmac, base64, time, requests
from dotenv import load_dotenv
load_dotenv()

USE_TESTNET = os.getenv("USE_TESTNET", "false").lower() == "true"
HOST = "https://clob-staging.polymarket.com" if USE_TESTNET else "https://clob.polymarket.com"

api_key = os.getenv("POLY_BUILDER_API_KEY")
secret = os.getenv("POLY_BUILDER_SECRET")
passphrase = os.getenv("POLY_BUILDER_PASSPHRASE")

print(f"Host: {HOST}")
print(f"API Key: {api_key[:10] if api_key else 'MISSING'}...")

if not all([api_key, secret, passphrase]):
    print("❌ Missing credentials in .env")
    exit(1)

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

try:
    resp = requests.get(HOST + path, headers=headers, timeout=10)
    print(f"Status: {resp.status_code}")
    print(f"Response: {resp.text[:200]}")
except Exception as e:
    print(f"Error: {e}")
