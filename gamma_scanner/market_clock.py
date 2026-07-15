"""Single source of truth for market hours — uses Alpaca clock API."""
import requests

ALPACA_KEY = "PKOMKRLONHFRTJIPY3OTSRQYDP"
ALPACA_SECRET = "85eucWnKfY5DmBxCiWP3uTefYMbLdwn7D7fjTSpbNGx4"

def is_market_open():
    """Check Alpaca clock. No fallback — if we can't verify, don't trade."""
    try:
        resp = requests.get(
            "https://paper-api.alpaca.markets/v2/clock",
            headers={"APCA-API-KEY-ID": ALPACA_KEY, "APCA-API-SECRET-KEY": ALPACA_SECRET},
            timeout=5
        )
        if resp.status_code == 200:
            return resp.json().get("is_open", False)
    except:
        pass
    return False  # if we can't confirm, assume closed
