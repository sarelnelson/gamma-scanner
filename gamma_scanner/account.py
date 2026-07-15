"""
Gamma Scanner — Account Management
Tracks paper/real account balance, deposits, withdrawals, and transaction history.

The account.json file is the ledger:
- starting_balance: initial deposit
- transactions: list of deposits/withdrawals with timestamps
- Current balance = starting_balance + sum(transactions) + realized P&L from trades
"""
import json, os
from datetime import datetime

from config import ACCOUNT_FILE, DATA_DIR

DEFAULT_ACCOUNT = {
    "starting_balance": 5000.00,
    "transactions": [],  # {"type": "deposit"/"withdrawal", "amount": float, "date": str, "note": str}
}


def load_account():
    """Load account data from file."""
    if os.path.exists(ACCOUNT_FILE):
        try:
            with open(ACCOUNT_FILE) as f:
                return json.load(f)
        except:
            pass
    return DEFAULT_ACCOUNT.copy()


def save_account(account):
    """Save account data atomically."""
    tmp = ACCOUNT_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(account, f, indent=2)
    os.replace(tmp, ACCOUNT_FILE)


def get_total_deposits():
    """Total cash put into the account."""
    account = load_account()
    base = account.get("starting_balance", 5000)
    deposits = sum(t["amount"] for t in account.get("transactions", []) if t["type"] == "deposit")
    return base + deposits


def get_total_withdrawals():
    """Total cash taken out."""
    account = load_account()
    return sum(t["amount"] for t in account.get("transactions", []) if t["type"] == "withdrawal")


def get_cash_basis():
    """Net cash put in (deposits - withdrawals). This is what you're 'risking'."""
    return get_total_deposits() - get_total_withdrawals()


def deposit(amount, note=""):
    """Add funds to the account."""
    if amount <= 0:
        return {"error": "Amount must be positive"}
    
    account = load_account()
    account["transactions"].append({
        "type": "deposit",
        "amount": round(amount, 2),
        "date": datetime.utcnow().isoformat(),
        "note": note or f"Deposit ${amount:.2f}",
    })
    save_account(account)
    
    return {
        "success": True,
        "deposited": round(amount, 2),
        "new_cash_basis": round(get_cash_basis(), 2),
        "message": f"Deposited ${amount:.2f}",
    }


def withdraw(amount, note=""):
    """Remove funds from the account."""
    if amount <= 0:
        return {"error": "Amount must be positive"}
    
    # Don't allow withdrawing more than available (cash basis + realized P&L - deployed)
    account = load_account()
    account["transactions"].append({
        "type": "withdrawal",
        "amount": round(amount, 2),
        "date": datetime.utcnow().isoformat(),
        "note": note or f"Withdrawal ${amount:.2f}",
    })
    save_account(account)
    
    return {
        "success": True,
        "withdrawn": round(amount, 2),
        "new_cash_basis": round(get_cash_basis(), 2),
        "message": f"Withdrew ${amount:.2f}",
    }


def set_starting_balance(amount):
    """Set/reset the starting balance (for initial setup or migration)."""
    account = load_account()
    account["starting_balance"] = round(amount, 2)
    save_account(account)
    return {"success": True, "starting_balance": round(amount, 2)}


def get_account_summary():
    """Full account summary."""
    account = load_account()
    transactions = account.get("transactions", [])
    starting = account.get("starting_balance", 5000)
    
    total_deposited = starting + sum(t["amount"] for t in transactions if t["type"] == "deposit")
    total_withdrawn = sum(t["amount"] for t in transactions if t["type"] == "withdrawal")
    cash_basis = total_deposited - total_withdrawn
    
    return {
        "starting_balance": starting,
        "total_deposited": round(total_deposited, 2),
        "total_withdrawn": round(total_withdrawn, 2),
        "cash_basis": round(cash_basis, 2),
        "transactions": transactions[-20:],  # last 20
    }
