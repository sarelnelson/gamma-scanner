"""
User Manager — handles per-user data isolation.
Each user has their own:
  - trades.json (positions)
  - account.json (balance, deposits, withdrawals)
  - .paused file
  - .crash_warned file
"""
import os, json
from config import DATA_DIR, SCANNER_DIR

USERS_FILE = os.path.join(SCANNER_DIR, "users.json")


def load_users_config():
    try:
        with open(USERS_FILE) as f:
            return json.load(f)
    except:
        return {"password": "gamma2026", "users": {"sarel": {"name": "Sarel"}}}


def get_active_users():
    """Get list of user IDs that have Alpaca keys configured."""
    config = load_users_config()
    users = []
    for uid, u in config.get("users", {}).items():
        # User is active if they have alpaca keys OR are the default user
        if u.get("alpaca_key") or uid == "sarel":
            users.append(uid)
    return users


def get_user_dir(user_id):
    """Get/create user's data directory."""
    d = os.path.join(DATA_DIR, f"user_{user_id}")
    os.makedirs(d, exist_ok=True)
    return d


def get_user_trades_file(user_id):
    return os.path.join(get_user_dir(user_id), "trades.json")


def get_user_account_file(user_id):
    return os.path.join(get_user_dir(user_id), "account.json")


def get_user_pause_file(user_id):
    return os.path.join(get_user_dir(user_id), ".paused")


def get_user_crash_file(user_id):
    return os.path.join(get_user_dir(user_id), ".crash_warned")


def is_user_paused(user_id):
    return os.path.exists(get_user_pause_file(user_id))


def load_user_trades(user_id):
    path = get_user_trades_file(user_id)
    try:
        with open(path) as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except:
        return []


def save_user_trades(user_id, trades):
    path = get_user_trades_file(user_id)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(trades, f, indent=2)
    os.replace(tmp, path)


def load_user_account(user_id):
    path = get_user_account_file(user_id)
    try:
        with open(path) as f:
            return json.load(f)
    except:
        # Init from users.json
        config = load_users_config()
        bal = config.get("users", {}).get(user_id, {}).get("starting_balance", 0)
        account = {"starting_balance": bal, "transactions": []}
        save_user_account(user_id, account)
        return account


def save_user_account(user_id, account):
    path = get_user_account_file(user_id)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(account, f, indent=2)
    os.replace(tmp, path)


def get_user_balance(user_id):
    """Current balance = starting + deposits - withdrawals + realized P&L."""
    account = load_user_account(user_id)
    base = account.get("starting_balance", 0)
    deposits = sum(t["amount"] for t in account.get("transactions", []) if t["type"] == "deposit")
    withdrawals = sum(t["amount"] for t in account.get("transactions", []) if t["type"] == "withdrawal")
    
    # Realized P&L from trades
    trades = load_user_trades(user_id)
    realized = sum(t.get("pnl", 0) for t in trades if t.get("status") in ("closed", "expired"))
    
    return base + deposits - withdrawals + realized


def get_user_deployed(user_id):
    """Capital currently in open positions."""
    trades = load_user_trades(user_id)
    open_t = [t for t in trades if t.get("status") == "open"]
    return sum(t.get("cost_per_contract", 0) for t in open_t)


def get_user_alpaca_keys(user_id):
    """Get Alpaca API keys for a user."""
    config = load_users_config()
    user = config.get("users", {}).get(user_id, {})
    return user.get("alpaca_key", ""), user.get("alpaca_secret", "")
