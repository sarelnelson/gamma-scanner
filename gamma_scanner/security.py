"""
Security Module — IP Allowlist + Token Authentication

Features:
- IP allowlist: only approved IPs can access the server
- Open Gate mode: when enabled, successful login adds the IP to allowlist
- Token verification on all API endpoints
- Complex password (generated once, stored in config)

Files (on Docker volume for persistence):
- /app/data/security/allowlist.json — approved IPs
- /app/data/security/gate_open — exists when gate is open
- /app/data/security/tokens.json — active session tokens

Usage:
- First deploy: gate starts OPEN so you can log in and get greenlisted
- After logging in: close the gate from the dashboard
- To add new devices: open gate, log in from new device, close gate
"""

import os
import json
import hashlib
import secrets
import time
from datetime import datetime
from fastapi import Request, HTTPException
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

# Security data directory
try:
    from config import DATA_DIR
except:
    DATA_DIR = os.environ.get("GAMMA_DATA_DIR", "/app/data")

SECURITY_DIR = os.path.join(DATA_DIR, "security")
ALLOWLIST_FILE = os.path.join(SECURITY_DIR, "allowlist.json")
GATE_FILE = os.path.join(SECURITY_DIR, "gate_open")
TOKENS_FILE = os.path.join(SECURITY_DIR, "tokens.json")

# Password — from env var or users.json
def _get_password():
    env_pw = os.environ.get("GAMMA_PASSWORD")
    if env_pw:
        return env_pw
    try:
        users_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "users.json")
        with open(users_path) as f:
            return json.load(f).get("password", "")
    except:
        return "XWOOTdH7KqwxaD-NFFrb7w"

PASSWORD = _get_password()

# Token expiry: 30 days
TOKEN_EXPIRY_SECONDS = 30 * 24 * 60 * 60


def ensure_security_dir():
    os.makedirs(SECURITY_DIR, exist_ok=True)
    # First run: create allowlist and open gate
    if not os.path.exists(ALLOWLIST_FILE):
        with open(ALLOWLIST_FILE, "w") as f:
            json.dump({"ips": {}, "created": datetime.utcnow().isoformat()}, f, indent=2)
        open_gate()


# === ALLOWLIST ===

def load_allowlist() -> dict:
    ensure_security_dir()
    try:
        with open(ALLOWLIST_FILE) as f:
            return json.load(f)
    except:
        return {"ips": {}}


def save_allowlist(data: dict):
    os.makedirs(SECURITY_DIR, exist_ok=True)
    with open(ALLOWLIST_FILE, "w") as f:
        json.dump(data, f, indent=2)


def is_ip_allowed(ip: str) -> bool:
    """Check if IP is on the allowlist."""
    data = load_allowlist()
    return ip in data.get("ips", {})


def add_ip(ip: str, note: str = ""):
    """Add an IP to the allowlist."""
    data = load_allowlist()
    data["ips"][ip] = {
        "added": datetime.utcnow().isoformat(),
        "note": note,
    }
    save_allowlist(data)


def remove_ip(ip: str):
    """Remove an IP from the allowlist."""
    data = load_allowlist()
    data["ips"].pop(ip, None)
    save_allowlist(data)


def get_all_ips() -> dict:
    """Get all allowed IPs."""
    return load_allowlist().get("ips", {})


# === GATE ===

def is_gate_open() -> bool:
    ensure_security_dir()
    return os.path.exists(GATE_FILE)


def open_gate():
    ensure_security_dir()
    with open(GATE_FILE, "w") as f:
        f.write(datetime.utcnow().isoformat())


def close_gate():
    ensure_security_dir()
    if os.path.exists(GATE_FILE):
        os.remove(GATE_FILE)


# === TOKENS ===

def load_tokens() -> dict:
    ensure_security_dir()
    try:
        with open(TOKENS_FILE) as f:
            return json.load(f)
    except:
        return {}


def save_tokens(tokens: dict):
    ensure_security_dir()
    with open(TOKENS_FILE, "w") as f:
        json.dump(tokens, f)


def create_token(ip: str) -> str:
    """Create a new session token."""
    token = secrets.token_urlsafe(32)
    tokens = load_tokens()
    tokens[token] = {
        "created": time.time(),
        "ip": ip,
    }
    # Clean expired tokens
    tokens = {k: v for k, v in tokens.items() if time.time() - v["created"] < TOKEN_EXPIRY_SECONDS}
    save_tokens(tokens)
    return token


def verify_token(token: str) -> bool:
    """Check if a token is valid and not expired."""
    if not token:
        return False
    tokens = load_tokens()
    entry = tokens.get(token)
    if not entry:
        return False
    if time.time() - entry["created"] > TOKEN_EXPIRY_SECONDS:
        return False
    return True


def verify_password(password: str) -> bool:
    """Check password."""
    return password == PASSWORD


# === MIDDLEWARE ===

class IPAllowlistMiddleware(BaseHTTPMiddleware):
    """
    Middleware that blocks requests from non-allowlisted IPs.
    Exceptions:
    - If gate is open: allow access to login page and login endpoint
    - Health check endpoint (for Docker healthcheck)
    """
    
    async def dispatch(self, request: Request, call_next):
        # Get client IP (handle proxies)
        ip = request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
        if not ip:
            ip = request.headers.get("X-Real-IP", "")
        if not ip:
            ip = request.client.host if request.client else "unknown"
        
        # Store IP on request for later use
        request.state.client_ip = ip
        
        # Always allow health check (Docker needs this)
        if request.url.path == "/api/health":
            return await call_next(request)
        
        # Check if IP is allowed
        if is_ip_allowed(ip):
            return await call_next(request)
        
        # If gate is open, allow login page and login endpoint
        if is_gate_open():
            allowed_paths = ["/", "/api/auth/login", "/static/", "/favicon.ico"]
            if any(request.url.path.startswith(p) for p in allowed_paths) or request.url.path == "/":
                return await call_next(request)
        
        # Blocked
        return JSONResponse(
            status_code=403,
            content={"error": "Access denied. IP not authorized."}
        )


class TokenAuthMiddleware(BaseHTTPMiddleware):
    """
    Middleware that verifies session token on API endpoints.
    Exceptions: login, health check, static files.
    """
    
    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        
        # Skip auth for these paths
        skip_paths = ["/", "/api/health", "/api/auth/login", "/static/", "/favicon.ico"]
        if any(path.startswith(p) for p in skip_paths) or path == "/":
            return await call_next(request)
        
        # Check for token in header or query param
        token = request.headers.get("Authorization", "").replace("Bearer ", "")
        if not token:
            token = request.query_params.get("token", "")
        
        if not verify_token(token):
            return JSONResponse(
                status_code=401,
                content={"error": "Invalid or expired session. Please log in again."}
            )
        
        return await call_next(request)
