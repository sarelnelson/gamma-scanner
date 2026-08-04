"""
Security — IP Allowlist (Simple Implementation)

No middleware, no recursion, no complexity.
Just a JSON file with allowed IPs and helper functions.

Files stored at: /app/data/security/
- allowlist.json: {"ips": {"1.2.3.4": {"added": "...", "note": "..."}}}
- gate_status.json: {"open": true/false}
"""

import os
import json
from datetime import datetime

# Path setup
DATA_DIR = os.environ.get("GAMMA_DATA_DIR", "/app/data")
SECURITY_DIR = os.path.join(DATA_DIR, "security")
ALLOWLIST_FILE = os.path.join(SECURITY_DIR, "allowlist.json")
GATE_FILE = os.path.join(SECURITY_DIR, "gate_status.json")


def _init():
    """Create security dir and files if they don't exist."""
    os.makedirs(SECURITY_DIR, exist_ok=True)
    if not os.path.exists(ALLOWLIST_FILE):
        with open(ALLOWLIST_FILE, "w") as f:
            json.dump({"ips": {}}, f)
    if not os.path.exists(GATE_FILE):
        # Start with gate OPEN on first deploy
        with open(GATE_FILE, "w") as f:
            json.dump({"open": True}, f)


def get_client_ip(request) -> str:
    """Extract client IP from request, handling proxies."""
    ip = ""
    if hasattr(request, "headers"):
        ip = request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
        if not ip:
            ip = request.headers.get("X-Real-IP", "")
    if not ip and hasattr(request, "client") and request.client:
        ip = request.client.host
    return ip or "unknown"


def is_allowed(ip: str) -> bool:
    """Check if IP is on the allowlist."""
    try:
        with open(ALLOWLIST_FILE) as f:
            data = json.load(f)
        return ip in data.get("ips", {})
    except:
        return True  # If file is broken, allow (don't lock everyone out)


def add_ip(ip: str, note: str = ""):
    """Add IP to allowlist."""
    _init()
    with open(ALLOWLIST_FILE) as f:
        data = json.load(f)
    data["ips"][ip] = {"added": datetime.utcnow().isoformat(), "note": note}
    with open(ALLOWLIST_FILE, "w") as f:
        json.dump(data, f, indent=2)


def remove_ip(ip: str):
    """Remove IP from allowlist."""
    try:
        with open(ALLOWLIST_FILE) as f:
            data = json.load(f)
        data["ips"].pop(ip, None)
        with open(ALLOWLIST_FILE, "w") as f:
            json.dump(data, f, indent=2)
    except:
        pass


def get_all_ips() -> dict:
    """Get all allowed IPs."""
    try:
        with open(ALLOWLIST_FILE) as f:
            return json.load(f).get("ips", {})
    except:
        return {}


def is_gate_open() -> bool:
    """Check if gate is open (new logins get greenlisted)."""
    try:
        with open(GATE_FILE) as f:
            return json.load(f).get("open", False)
    except:
        return True  # Default open if file missing


def set_gate(is_open: bool):
    """Open or close the gate."""
    _init()
    with open(GATE_FILE, "w") as f:
        json.dump({"open": is_open, "changed": datetime.utcnow().isoformat()}, f)


# Initialize on import
_init()
