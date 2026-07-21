"""
Gamma Scanner — Path & Config resolver.
All paths are relative to THIS file's directory.
Works in local dev, Docker, or any deployment.
"""
import os

# Base directory = wherever this file lives
SCANNER_DIR = os.path.dirname(os.path.abspath(__file__))

# Data directory: use env var, or Docker volume if it exists, or fall back to scanner dir
if os.environ.get("GAMMA_DATA_DIR"):
    DATA_DIR = os.environ["GAMMA_DATA_DIR"]
elif os.path.isdir("/app/data"):
    DATA_DIR = "/app/data"
else:
    DATA_DIR = SCANNER_DIR

# File paths
TRADES_FILE = os.path.join(DATA_DIR, "trades_loose.json")
TRADES_STRICT_FILE = os.path.join(DATA_DIR, "trades_strict.json")
PICKS_FILE = os.path.join(DATA_DIR, "picks_loose.json")
PICKS_STRICT_FILE = os.path.join(DATA_DIR, "picks_strict.json")
SCAN_LOG = os.path.join(DATA_DIR, "scan.log")
MONITOR_LOG = os.path.join(DATA_DIR, "monitor.log")
BROKER_LOG = os.path.join(DATA_DIR, "broker.log")
PID_FILE = os.path.join(SCANNER_DIR, ".monitor.pid")
ACCOUNT_FILE = os.path.join(DATA_DIR, "account.json")

# API Keys (from environment)
ALPACA_API_KEY = os.environ.get("ALPACA_API_KEY", "PKOMKRLONHFRTJIPY3OTSRQYDP")
ALPACA_SECRET_KEY = os.environ.get("ALPACA_SECRET_KEY", "85eucWnKfY5DmBxCiWP3uTefYMbLdwn7D7fjTSpbNGx4")

# Notifications
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")
NTFY_SERVER = os.environ.get("NTFY_SERVER", "https://ntfy.sh")
