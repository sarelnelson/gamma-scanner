"""
auth_gate.py — cookie-based device whitelist with an admin-controlled gate.

Access model (enforced by middleware in server.py, only when LOGIN_GATE_ENABLED=true):
  - A device holds an opaque random cookie token, tracked server-side (revocable).
  - Gate OPEN  : entering DASHBOARD_PASSWORD enrolls the device (issues a cookie).
  - Gate LOCKED: no enrollment; only devices with a valid cookie get in.
  - ADMIN_MASTER_PASSWORD always works (even locked, even with no cookie) and is
    what opens/locks the gate — the anti-lockout key.
  - Sliding expiry: every accepted visit refreshes the token's last_seen; tokens
    unused for INACTIVITY_DAYS expire.
  - Opening the gate auto-relocks after OPEN_HOURS.

Secrets (passwords) come from env vars, never from tracked files.
State is persisted under DATA_DIR so it survives restarts.
"""
import os
import json
import time
import secrets as _secrets

try:
    from config import DATA_DIR
except Exception:
    DATA_DIR = os.environ.get("GAMMA_DATA_DIR") or ("/app/data" if os.path.isdir("/app/data") else os.path.dirname(os.path.abspath(__file__)))

WHITELIST_FILE = os.path.join(DATA_DIR, "whitelist.json")   # {token: {created, last_seen, label}}
GATE_FILE = os.path.join(DATA_DIR, "gate_state.json")        # {open_until: epoch_seconds}

INACTIVITY_DAYS = 90          # sliding cookie lifetime
OPEN_HOURS = 3.0              # auto-relock window after opening
COOKIE_NAME = "gamma_wl"      # whitelist cookie
COOKIE_MAX_AGE = INACTIVITY_DAYS * 24 * 60 * 60


# ---- small json helpers (atomic writes) ----
def _load(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def _save(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


# ---- passwords (env only) ----
def dashboard_password():
    return os.environ.get("DASHBOARD_PASSWORD", "")


def master_password():
    return os.environ.get("ADMIN_MASTER_PASSWORD", "")


def check_dashboard_password(pw):
    expected = dashboard_password()
    return bool(expected) and _secrets.compare_digest(str(pw), expected)


def check_master_password(pw):
    expected = master_password()
    return bool(expected) and _secrets.compare_digest(str(pw), expected)


# ---- gate state ----
def gate_is_open():
    st = _load(GATE_FILE, {})
    return time.time() < float(st.get("open_until", 0) or 0)


def open_gate(hours=OPEN_HOURS):
    _save(GATE_FILE, {"open_until": time.time() + hours * 3600})
    return gate_status()


def lock_gate():
    _save(GATE_FILE, {"open_until": 0})
    return gate_status()


def gate_status():
    st = _load(GATE_FILE, {})
    open_until = float(st.get("open_until", 0) or 0)
    remaining = max(0, int(open_until - time.time()))
    return {"open": remaining > 0, "seconds_remaining": remaining}


# ---- whitelist tokens ----
def _prune(wl):
    cutoff = time.time() - INACTIVITY_DAYS * 24 * 60 * 60
    return {t: m for t, m in wl.items() if float(m.get("last_seen", 0) or 0) >= cutoff}


def issue_token(label=""):
    wl = _prune(_load(WHITELIST_FILE, {}))
    token = _secrets.token_urlsafe(32)
    now = time.time()
    wl[token] = {"created": now, "last_seen": now, "label": label}
    _save(WHITELIST_FILE, wl)
    return token


def verify_token(token):
    """Return True if the token is valid; refresh its last_seen (sliding expiry)."""
    if not token:
        return False
    wl = _prune(_load(WHITELIST_FILE, {}))
    if token not in wl:
        # token may have been pruned for inactivity; persist the prune and reject
        _save(WHITELIST_FILE, wl)
        return False
    wl[token]["last_seen"] = time.time()
    _save(WHITELIST_FILE, wl)
    return True


def revoke_token(token):
    wl = _load(WHITELIST_FILE, {})
    if token in wl:
        del wl[token]
        _save(WHITELIST_FILE, wl)
        return True
    return False


def list_tokens():
    return _prune(_load(WHITELIST_FILE, {}))


def gate_enabled():
    return os.environ.get("LOGIN_GATE_ENABLED", "").lower() == "true"
