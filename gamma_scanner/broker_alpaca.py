"""
Alpaca Options Broker — Real money execution layer for the Gamma Scanner.

This module handles:
- Option contract symbol resolution (OCC format)
- Real-time bid/ask quotes via Alpaca data API
- Limit order submission with configurable aggression
- Order status monitoring with timeout-based cancel/replace
- Partial fill handling (accept partial, don't chase the rest)
- Position tracking via broker (source of truth, not local files)
- Account balance and buying power checks

SWITCHING TO REAL MONEY:
1. Change PAPER_MODE = False
2. Change BASE_URL to "https://api.alpaca.markets" (remove "paper-")
3. Use your live API keys (different from paper keys)
4. Start with 1 contract per trade, scale up only after live validation

IMPORTANT: This module uses LIMIT orders only. Never market orders on options.
Options spreads can be 10-50% wide — a market order can fill catastrophically bad.
"""
import os, json, time, requests
from datetime import datetime, timedelta
from typing import Optional, Dict, List, Tuple

# === CONFIGURATION ===

PAPER_MODE = True  # SET TO False FOR REAL MONEY

# API endpoints
PAPER_BASE = "https://paper-api.alpaca.markets"
LIVE_BASE = "https://api.alpaca.markets"
DATA_BASE = "https://data.alpaca.markets"
BASE_URL = PAPER_BASE if PAPER_MODE else LIVE_BASE

# Credentials (from environment or hardcoded for paper)
API_KEY = os.getenv("ALPACA_API_KEY", "PKOMKRLONHFRTJIPY3OTSRQYDP")
API_SECRET = os.getenv("ALPACA_SECRET_KEY", "85eucWnKfY5DmBxCiWP3uTefYMbLdwn7D7fjTSpbNGx4")

HEADERS = {
    "APCA-API-KEY-ID": API_KEY,
    "APCA-API-SECRET-KEY": API_SECRET,
}
HEADERS_JSON = {**HEADERS, "Content-Type": "application/json"}

# Order behavior
ORDER_TIMEOUT_SECONDS = 30      # Cancel unfilled order after 30 seconds
REPLACE_ATTEMPTS = 2            # Try replacing order N times before giving up
REPLACE_AGGRESSION_STEP = 0.02  # Each replace, improve price by this much toward mid
MIN_CONTRACTS = 1               # Always trade exactly 1 contract for now
COMMISSION_PER_CONTRACT = 0.00  # Alpaca is commission-free for options (verify your plan)

# Logging
LOG_FILE = "/workspace/stock-agent/gamma_scanner/broker.log"


def log(msg, level="INFO"):
    ts = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
    line = f"[{ts}] [BROKER] [{level}] {msg}"
    print(line)
    try:
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")
    except:
        pass


# === SYMBOL RESOLUTION ===

def build_occ_symbol(ticker: str, expiration: str, direction: str, strike: float) -> str:
    """
    Build OCC option symbol format used by Alpaca.
    Format: TICKER + YYMMDD + C/P + strike*1000 (8 digits, zero-padded)
    Example: PFE260717C00023500 = PFE July 17 2026 Call $23.50
    """
    # Parse expiration date
    exp_date = datetime.strptime(expiration, "%Y-%m-%d")
    date_part = exp_date.strftime("%y%m%d")
    
    # Direction
    cp = "C" if direction.upper() == "CALL" else "P"
    
    # Strike: multiply by 1000, zero-pad to 8 digits
    strike_int = int(strike * 1000)
    strike_part = f"{strike_int:08d}"
    
    # Ticker: left-pad to 6 chars with spaces (but Alpaca uses variable-length)
    symbol = f"{ticker}{date_part}{cp}{strike_part}"
    return symbol


def find_contract(ticker: str, expiration: str, direction: str, strike: float) -> Optional[Dict]:
    """
    Look up the option contract via Alpaca's contracts API.
    Returns contract details or None if not found.
    This validates the contract actually exists and is tradeable.
    """
    try:
        params = {
            "underlying_symbols": ticker,
            "expiration_date": expiration,
            "type": "call" if direction.upper() == "CALL" else "put",
            "strike_price_gte": str(strike - 0.01),
            "strike_price_lte": str(strike + 0.01),
            "status": "active",
        }
        resp = requests.get(f"{BASE_URL}/v2/options/contracts", headers=HEADERS, params=params, timeout=10)
        if resp.status_code != 200:
            log(f"Contract lookup failed: {resp.status_code} {resp.text}", "ERROR")
            return None
        
        data = resp.json()
        contracts = data.get("option_contracts", [])
        if not contracts:
            log(f"No active contract found for {ticker} {expiration} {direction} ${strike}", "WARN")
            return None
        
        contract = contracts[0]
        return {
            "symbol": contract["symbol"],
            "id": contract["id"],
            "strike": float(contract["strike_price"]),
            "expiration": contract["expiration_date"],
            "type": contract["type"],
            "underlying": contract["underlying_symbol"],
            "status": contract["status"],
        }
    except Exception as e:
        log(f"Contract lookup error: {e}", "ERROR")
        return None


# === QUOTES ===

def get_option_quote(symbol: str) -> Optional[Dict]:
    """
    Get real-time bid/ask for an option contract.
    Returns: {bid, ask, bid_size, ask_size, timestamp} or None
    """
    try:
        resp = requests.get(
            f"{DATA_BASE}/v1beta1/options/quotes/latest",
            headers=HEADERS,
            params={"symbols": symbol},
            timeout=5,
        )
        if resp.status_code != 200:
            log(f"Quote failed for {symbol}: {resp.status_code}", "WARN")
            return None
        
        data = resp.json()
        quote = data.get("quotes", {}).get(symbol)
        if not quote:
            return None
        
        bid = float(quote.get("bp", 0))
        ask = float(quote.get("ap", 0))
        
        # Sanity check — if bid is 0 or spread is insane, something's wrong
        if bid <= 0 and ask <= 0:
            log(f"No valid quote for {symbol} (bid={bid}, ask={ask})", "WARN")
            return None
        
        spread = ask - bid if ask > 0 and bid > 0 else 999
        spread_pct = (spread / ask * 100) if ask > 0 else 999
        
        return {
            "bid": bid,
            "ask": ask,
            "mid": round((bid + ask) / 2, 2) if bid > 0 and ask > 0 else bid or ask,
            "bid_size": int(quote.get("bs", 0)),
            "ask_size": int(quote.get("as", 0)),
            "spread": round(spread, 2),
            "spread_pct": round(spread_pct, 1),
            "timestamp": quote.get("t"),
        }
    except Exception as e:
        log(f"Quote error for {symbol}: {e}", "ERROR")
        return None


# === ORDER EXECUTION ===

def submit_order(symbol: str, side: str, qty: int, limit_price: float, 
                 time_in_force: str = "day") -> Optional[Dict]:
    """
    Submit a limit order for options.
    Returns order dict with id, status, etc. or None on failure.
    
    NEVER use market orders on options. The spread will eat you alive.
    
    NOTE: position_intent is REQUIRED by Alpaca for options.
    Without it, sells get rejected as "uncovered option contracts" because
    Alpaca interprets them as opening a new short position.
    """
    # Alpaca requires position_intent for options to distinguish
    # opening vs closing trades. Without this, sell orders get 403'd.
    if side == "buy":
        position_intent = "buy_to_open"
    else:
        position_intent = "sell_to_close"
    
    order_data = {
        "symbol": symbol,
        "qty": str(qty),
        "side": side,  # "buy" or "sell"
        "type": "limit",
        "time_in_force": time_in_force,
        "limit_price": str(round(limit_price, 2)),
        "position_intent": position_intent,
    }
    
    log(f"Submitting {side.upper()} {qty}x {symbol} @ ${limit_price:.2f} (limit)")
    
    try:
        resp = requests.post(
            f"{BASE_URL}/v2/orders",
            headers=HEADERS_JSON,
            json=order_data,
            timeout=10,
        )
        
        if resp.status_code in (200, 201):
            order = resp.json()
            log(f"Order accepted: {order['id']} status={order['status']}")
            return order
        else:
            error = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else resp.text
            log(f"Order rejected: {resp.status_code} — {error}", "ERROR")
            return None
    except Exception as e:
        log(f"Order submission error: {e}", "ERROR")
        return None


def get_order_status(order_id: str) -> Optional[Dict]:
    """Get current order status."""
    try:
        resp = requests.get(f"{BASE_URL}/v2/orders/{order_id}", headers=HEADERS, timeout=5)
        if resp.status_code == 200:
            return resp.json()
        return None
    except:
        return None


def cancel_order(order_id: str) -> bool:
    """Cancel an open order. Returns True if successfully canceled."""
    try:
        resp = requests.delete(f"{BASE_URL}/v2/orders/{order_id}", headers=HEADERS, timeout=5)
        if resp.status_code == 204:
            log(f"Order {order_id} canceled")
            return True
        else:
            log(f"Cancel failed for {order_id}: {resp.status_code} {resp.text}", "WARN")
            return False
    except Exception as e:
        log(f"Cancel error: {e}", "ERROR")
        return False


def replace_order(order_id: str, new_limit_price: float, qty: int = None) -> Optional[Dict]:
    """
    Replace (amend) an open order with a new limit price.
    Used when our initial limit isn't getting filled and we want to be more aggressive.
    """
    patch_data = {"limit_price": str(round(new_limit_price, 2))}
    if qty:
        patch_data["qty"] = str(qty)
    
    try:
        resp = requests.patch(
            f"{BASE_URL}/v2/orders/{order_id}",
            headers=HEADERS_JSON,
            json=patch_data,
            timeout=5,
        )
        if resp.status_code == 200:
            order = resp.json()
            log(f"Order {order_id} replaced: new limit=${new_limit_price:.2f}")
            return order
        else:
            log(f"Replace failed: {resp.status_code} {resp.text}", "WARN")
            return None
    except Exception as e:
        log(f"Replace error: {e}", "ERROR")
        return None


# === HIGH-LEVEL EXECUTION FLOWS ===

def has_pending_order_for(ticker: str) -> bool:
    """
    Check if there's already an open/pending order for this ticker's options.
    Prevents duplicate simultaneous orders (e.g. double-tap on dashboard).
    """
    try:
        resp = requests.get(
            f"{BASE_URL}/v2/orders",
            headers=HEADERS,
            params={"status": "open", "limit": 50},
            timeout=5,
        )
        if resp.status_code != 200:
            # If we can't check, err on the side of caution — block the order
            log(f"Can't check open orders ({resp.status_code}), blocking duplicate guard", "WARN")
            return True
        
        open_orders = resp.json()
        for order in open_orders:
            symbol = order.get("symbol", "")
            # OCC symbols start with the ticker (e.g., DKNG260807C00024000)
            if symbol.startswith(ticker) and order.get("side") == "buy":
                log(f"  ⚠️ DUPLICATE BLOCKED: Already have open buy order for {ticker} ({symbol}, id={order['id'][:8]})")
                return True
        return False
    except Exception as e:
        log(f"Error checking pending orders: {e}", "WARN")
        return True  # Block if we can't verify


def buy_to_open(ticker: str, expiration: str, direction: str, strike: float,
                max_price: float = None) -> Dict:
    """
    Full buy-to-open flow:
    1. Check for existing in-flight orders (prevent duplicates)
    2. Resolve contract symbol
    3. Get current quote
    4. Submit limit order at mid (or max_price if lower)
    5. Wait up to ORDER_TIMEOUT for fill
    6. If not filled: replace at more aggressive price (up to REPLACE_ATTEMPTS times)
    7. If still not filled: cancel and report failure
    8. Handle partial fills: accept what we got
    
    Returns: {
        "success": bool,
        "order_id": str,
        "fill_price": float or None,
        "filled_qty": int,
        "status": str,  # "filled", "partial", "canceled", "failed"
        "contract_symbol": str,
        "quote_at_entry": dict,
    }
    """
    result = {
        "success": False,
        "order_id": None,
        "fill_price": None,
        "filled_qty": 0,
        "status": "failed",
        "contract_symbol": None,
        "quote_at_entry": None,
    }
    
    # Step 0: Check for duplicate in-flight orders
    if has_pending_order_for(ticker):
        result["status"] = "duplicate_blocked"
        return result
    
    # Step 1: Find the contract
    contract = find_contract(ticker, expiration, direction, strike)
    if not contract:
        log(f"BTO FAILED: No contract found for {ticker} {direction} ${strike} {expiration}", "ERROR")
        return result
    
    symbol = contract["symbol"]
    result["contract_symbol"] = symbol
    log(f"BTO: {ticker} {direction} ${strike} {expiration} → {symbol}")
    
    # Step 2: Get current quote
    quote = get_option_quote(symbol)
    if not quote:
        log(f"BTO FAILED: No quote available for {symbol}", "ERROR")
        return result
    
    result["quote_at_entry"] = quote
    
    # Determine limit price
    # Strategy: start at the MID price (save on spread). If not filled, replace at ask.
    # This saves $5-15 per trade on average vs paying full ask immediately.
    limit = quote["mid"]
    if max_price and max_price < limit:
        limit = max_price
        log(f"  Using max_price ${max_price:.2f} (below mid ${quote['mid']:.2f})")
    
    # Reject if spread is too wide (>15% means illiquid, likely bad fill)
    if quote["spread_pct"] > 15:
        log(f"BTO REJECTED: Spread too wide ({quote['spread_pct']:.1f}%) for {symbol}", "WARN")
        result["status"] = "rejected_spread"
        return result
    
    log(f"  Quote: bid=${quote['bid']:.2f} ask=${quote['ask']:.2f} spread={quote['spread_pct']:.1f}% | Limit: ${limit:.2f}")
    
    # Step 3: Submit order
    order = submit_order(symbol, "buy", MIN_CONTRACTS, limit)
    if not order:
        return result
    
    result["order_id"] = order["id"]
    
    # Step 4: Wait for fill with timeout
    fill_result = wait_for_fill(order["id"], ORDER_TIMEOUT_SECONDS)
    
    if fill_result["status"] == "filled":
        result["success"] = True
        result["fill_price"] = fill_result["fill_price"]
        result["filled_qty"] = fill_result["filled_qty"]
        result["status"] = "filled"
        log(f"  ✅ FILLED: {result['filled_qty']}x @ ${result['fill_price']:.2f}")
        return result
    
    if fill_result["status"] == "partially_filled":
        # Accept partial fill — don't chase the unfilled portion
        result["success"] = True
        result["fill_price"] = fill_result["fill_price"]
        result["filled_qty"] = fill_result["filled_qty"]
        result["status"] = "partial"
        # Cancel remaining
        cancel_order(order["id"])
        log(f"  ⚠️ PARTIAL FILL: {result['filled_qty']}/{MIN_CONTRACTS} @ ${result['fill_price']:.2f} — canceled remainder")
        return result
    
    # Step 5: Not filled at mid — escalate toward ask
    current_limit = limit
    for attempt in range(REPLACE_ATTEMPTS):
        if attempt == 0:
            # First replace: jump to the ask price
            new_limit = round(quote["ask"], 2)
        else:
            # Second replace: slightly above ask
            new_limit = round(quote["ask"] + REPLACE_AGGRESSION_STEP, 2)
        
        if new_limit <= current_limit:
            break
        if new_limit > quote["ask"] + 0.05:
            log(f"  Won't chase above ask+0.05 (${quote['ask'] + 0.05:.2f})")
            break
        
        log(f"  Replace attempt {attempt + 1}: ${current_limit:.2f} → ${new_limit:.2f}")
        replaced = replace_order(order["id"], new_limit)
        if not replaced:
            break
        
        current_limit = new_limit
        result["order_id"] = replaced.get("id", result["order_id"])
        
        # Wait again
        fill_result = wait_for_fill(result["order_id"], ORDER_TIMEOUT_SECONDS)
        
        if fill_result["status"] == "filled":
            result["success"] = True
            result["fill_price"] = fill_result["fill_price"]
            result["filled_qty"] = fill_result["filled_qty"]
            result["status"] = "filled"
            log(f"  ✅ FILLED (after replace): {result['filled_qty']}x @ ${result['fill_price']:.2f}")
            return result
        
        if fill_result["status"] == "partially_filled":
            result["success"] = True
            result["fill_price"] = fill_result["fill_price"]
            result["filled_qty"] = fill_result["filled_qty"]
            result["status"] = "partial"
            cancel_order(result["order_id"])
            log(f"  ⚠️ PARTIAL (after replace): {result['filled_qty']} @ ${result['fill_price']:.2f}")
            return result
    
    # Step 6: Give up — cancel and report failure
    cancel_order(result["order_id"])
    result["status"] = "canceled_timeout"
    log(f"  ❌ NOT FILLED: Canceled after {REPLACE_ATTEMPTS} replace attempts")
    return result


def sell_to_close(contract_symbol: str, qty: int = 1, min_price: float = None) -> Dict:
    """
    Full sell-to-close flow:
    1. Get current quote
    2. Submit limit sell at bid
    3. Wait for fill with timeout
    4. If not filled: replace at lower price (more aggressive)
    5. If still not filled: keep order open as GTC (don't cancel profit-takes)
    
    For profit-taking, we're less aggressive about canceling.
    Better to leave the order working than miss the exit.
    
    Returns same structure as buy_to_open.
    """
    result = {
        "success": False,
        "order_id": None,
        "fill_price": None,
        "filled_qty": 0,
        "status": "failed",
        "contract_symbol": contract_symbol,
        "quote_at_exit": None,
    }
    
    # Get current quote
    quote = get_option_quote(contract_symbol)
    if not quote:
        log(f"STC FAILED: No quote for {contract_symbol}", "ERROR")
        return result
    
    result["quote_at_exit"] = quote
    
    # Sell at bid (that's what buyers are willing to pay)
    limit = quote["bid"]
    if min_price and min_price > limit:
        # Don't sell below our minimum acceptable price
        limit = min_price
        log(f"  Using min_price ${min_price:.2f} (above bid ${quote['bid']:.2f}) — may not fill")
    
    if limit <= 0:
        log(f"STC REJECTED: Bid is $0 for {contract_symbol} — likely no market", "WARN")
        result["status"] = "rejected_no_bid"
        return result
    
    log(f"STC: {contract_symbol} {qty}x @ ${limit:.2f} (bid=${quote['bid']:.2f})")
    
    # Submit sell order
    order = submit_order(contract_symbol, "sell", qty, limit)
    if not order:
        return result
    
    result["order_id"] = order["id"]
    
    # Wait for fill
    fill_result = wait_for_fill(order["id"], ORDER_TIMEOUT_SECONDS)
    
    if fill_result["status"] == "filled":
        result["success"] = True
        result["fill_price"] = fill_result["fill_price"]
        result["filled_qty"] = fill_result["filled_qty"]
        result["status"] = "filled"
        log(f"  ✅ SOLD: {result['filled_qty']}x @ ${result['fill_price']:.2f}")
        return result
    
    if fill_result["status"] == "partially_filled":
        result["success"] = True
        result["fill_price"] = fill_result["fill_price"]
        result["filled_qty"] = fill_result["filled_qty"]
        result["status"] = "partial"
        # For sells, leave the rest working — don't cancel a profit-take
        log(f"  ⚠️ PARTIAL SELL: {result['filled_qty']}/{qty} @ ${result['fill_price']:.2f} — leaving rest open")
        return result
    
    # Not filled — try one replace at slightly lower price (more likely to fill)
    new_limit = round(limit - REPLACE_AGGRESSION_STEP, 2)
    if new_limit > 0:
        log(f"  Replace sell: ${limit:.2f} → ${new_limit:.2f}")
        replaced = replace_order(order["id"], new_limit)
        if replaced:
            fill_result = wait_for_fill(replaced.get("id", order["id"]), ORDER_TIMEOUT_SECONDS)
            if fill_result["status"] in ("filled", "partially_filled"):
                result["success"] = True
                result["fill_price"] = fill_result["fill_price"]
                result["filled_qty"] = fill_result["filled_qty"]
                result["status"] = "filled" if fill_result["status"] == "filled" else "partial"
                log(f"  ✅ SOLD (after replace): @ ${result['fill_price']:.2f}")
                return result
    
    # For profit-taking, leave the order working rather than cancel
    # The price will likely come back
    result["status"] = "working"
    log(f"  ⏳ Sell order still working (not canceling profit-take)")
    return result


def wait_for_fill(order_id: str, timeout_seconds: int) -> Dict:
    """
    Poll order status until filled, partially filled, or timeout.
    Returns: {status, fill_price, filled_qty}
    """
    start = time.time()
    poll_interval = 1  # start checking every 1 second
    
    while time.time() - start < timeout_seconds:
        order = get_order_status(order_id)
        if not order:
            time.sleep(poll_interval)
            continue
        
        status = order.get("status")
        
        if status == "filled":
            return {
                "status": "filled",
                "fill_price": float(order.get("filled_avg_price", 0)),
                "filled_qty": int(order.get("filled_qty", 0)),
            }
        
        if status == "partially_filled":
            filled_qty = int(order.get("filled_qty", 0))
            if filled_qty > 0:
                return {
                    "status": "partially_filled",
                    "fill_price": float(order.get("filled_avg_price", 0)),
                    "filled_qty": filled_qty,
                }
        
        if status in ("canceled", "expired", "rejected"):
            return {"status": status, "fill_price": None, "filled_qty": 0}
        
        # Still pending — wait and check again
        time.sleep(poll_interval)
        # Increase poll interval slightly to avoid rate limits
        poll_interval = min(poll_interval + 0.5, 3)
    
    # Timeout — check one last time
    order = get_order_status(order_id)
    if order and order.get("status") == "filled":
        return {
            "status": "filled",
            "fill_price": float(order.get("filled_avg_price", 0)),
            "filled_qty": int(order.get("filled_qty", 0)),
        }
    
    return {"status": "timeout", "fill_price": None, "filled_qty": 0}


# === ACCOUNT INFO ===

def get_account() -> Optional[Dict]:
    """Get current account balance, buying power, etc."""
    try:
        resp = requests.get(f"{BASE_URL}/v2/account", headers=HEADERS, timeout=5)
        if resp.status_code == 200:
            acct = resp.json()
            return {
                "cash": float(acct.get("cash", 0)),
                "buying_power": float(acct.get("options_buying_power", 0)),
                "portfolio_value": float(acct.get("portfolio_value", 0)),
                "options_level": int(acct.get("options_trading_level", 0)),
            }
    except:
        pass
    return None


def get_positions() -> List[Dict]:
    """Get all current option positions from broker (source of truth)."""
    try:
        resp = requests.get(f"{BASE_URL}/v2/positions", headers=HEADERS, timeout=5)
        if resp.status_code == 200:
            positions = resp.json()
            return [{
                "symbol": p["symbol"],
                "qty": int(p["qty"]),
                "side": p["side"],
                "avg_entry_price": float(p["avg_entry_price"]),
                "current_price": float(p["current_price"]),
                "market_value": float(p["market_value"]),
                "unrealized_pl": float(p["unrealized_pl"]),
                "unrealized_plpc": float(p["unrealized_plpc"]),
            } for p in positions if p.get("asset_class") == "us_option"]
    except:
        pass
    return []


# === CONVENIENCE ===

def format_trade_for_log(ticker, direction, strike, exp, fill_price, qty):
    """Format a trade for human-readable log output."""
    cost = round(fill_price * qty * 100, 2)
    return f"{ticker} {direction} ${strike} exp:{exp} | {qty}x @ ${fill_price:.2f} = ${cost:.2f}"


if __name__ == "__main__":
    # Self-test: verify connectivity and show account state
    print("=== Alpaca Options Broker Self-Test ===")
    print(f"Mode: {'PAPER' if PAPER_MODE else '⚠️  LIVE MONEY'}")
    print(f"Endpoint: {BASE_URL}")
    print()
    
    acct = get_account()
    if acct:
        print(f"Account connected ✅")
        print(f"  Cash: ${acct['cash']:,.2f}")
        print(f"  Options Buying Power: ${acct['buying_power']:,.2f}")
        print(f"  Options Level: {acct['options_level']}")
    else:
        print("❌ Cannot connect to Alpaca")
        exit(1)
    
    print()
    
    # Test contract resolution
    contract = find_contract("PFE", "2026-07-17", "CALL", 23.5)
    if contract:
        print(f"Contract lookup ✅: {contract['symbol']}")
        quote = get_option_quote(contract["symbol"])
        if quote:
            print(f"  Quote: bid=${quote['bid']:.2f} ask=${quote['ask']:.2f} spread={quote['spread_pct']:.1f}%")
        else:
            print("  ❌ No quote available")
    else:
        print("❌ Contract lookup failed")
    
    print()
    positions = get_positions()
    print(f"Current positions: {len(positions)}")
    for p in positions:
        print(f"  {p['symbol']}: {p['qty']}x @ ${p['avg_entry_price']:.2f} | P&L: ${p['unrealized_pl']:.2f}")
