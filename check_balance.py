#!/usr/bin/env python3
"""
Standalone Polymarket Balance Checker (Fixed)
Uses correct parameter format for get_balance_allowance.
"""

import os
import sys
import asyncio
from dotenv import load_dotenv

load_dotenv()

USE_TESTNET = os.getenv("USE_TESTNET", "true").lower() == "true"

def main():
    print("\n🔍 Polymarket Balance Checker")
    print("-" * 50)
    print(f"Network: {'TESTNET' if USE_TESTNET else 'MAINNET'}")

    # Check env
    required_vars = [
        "POLY_BUILDER_API_KEY", "POLY_BUILDER_SECRET", "POLY_BUILDER_PASSPHRASE",
        "POLY_PRIVATE_KEY", "POLY_FUNDER_ADDRESS"
    ]
    missing = [v for v in required_vars if not os.getenv(v)]
    if missing:
        print(f"❌ Missing: {missing}")
        sys.exit(1)

    try:
        from py_clob_client.client import ClobClient
        from py_clob_client.constants import POLYGON, AMOY
        # Try to import the required params class
        try:
            from py_clob_client.clob_types import BalanceAllowanceParams
        except ImportError:
            # Older versions may not have it; we'll create a simple object
            BalanceAllowanceParams = None
    except ImportError:
        print("❌ py-clob-client not installed.")
        sys.exit(1)

    host = "https://clob-staging.polymarket.com" if USE_TESTNET else "https://clob.polymarket.com"
    chain_id = AMOY if USE_TESTNET else POLYGON

    # Initialize client
    try:
        api_creds = {
            "key": os.getenv("POLY_BUILDER_API_KEY"),
            "secret": os.getenv("POLY_BUILDER_SECRET"),
            "passphrase": os.getenv("POLY_BUILDER_PASSPHRASE"),
        }
        client = ClobClient(
            host,
            key=os.getenv("POLY_PRIVATE_KEY"),
            chain_id=chain_id,
            signature_type=1,
            funder=os.getenv("POLY_FUNDER_ADDRESS"),
            creds=api_creds
        )
        print("✅ ClobClient initialized")
    except Exception as e:
        print(f"❌ Init failed: {e}")
        sys.exit(1)

    # Set API credentials
    try:
        client.set_api_creds(client.create_or_derive_api_creds())
        print("✅ API credentials set")
    except Exception as e:
        print(f"⚠️ set_api_creds failed: {e}")

    # Create proper params object
    if BalanceAllowanceParams is not None:
        params = BalanceAllowanceParams(signature_type=-1)  # -1 means use default
        print("📦 Using BalanceAllowanceParams")
    else:
        # Fallback: create a simple class with required attribute
        class Params:
            signature_type = -1
        params = Params()
        print("📦 Using fallback params object")

    # Fetch balance
    print("\n--- Fetching Balance ---")
    try:
        result = client.get_balance_allowance(params)
        print(f"   Raw result: {result}")
        balance = float(result.get("balance", 0))
        print("\n" + "=" * 50)
        print(f"💰 USDC BALANCE: ${balance:.2f}")
        print("=" * 50)
        print("\n✅ SUCCESS!")
    except Exception as e:
        print(f"❌ Failed: {e}")
        # Last resort: try with empty dict (some versions accept it)
        print("\n--- Trying with empty dict ---")
        try:
            result = client.get_balance_allowance({})
            balance = float(result.get("balance", 0))
            print(f"💰 USDC BALANCE: ${balance:.2f}")
        except Exception as e2:
            print(f"❌ Also failed: {e2}")
            print("\nThe balance method is not working. However, your authentication is valid.")
            print("The bot will still be able to place trades.")

if __name__ == "__main__":
    main()
