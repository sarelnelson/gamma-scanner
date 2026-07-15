"""
Alpaca Data Provider — replaces yfinance for stock price history.
Yahoo Finance blocks AWS IPs; Alpaca works everywhere with API keys.
"""
import os, requests, time
import pandas as pd
from datetime import datetime, timedelta

from config import ALPACA_API_KEY, ALPACA_SECRET_KEY

DATA_URL = "https://data.alpaca.markets/v2"
HEADERS = {
    "APCA-API-KEY-ID": ALPACA_API_KEY,
    "APCA-API-SECRET-KEY": ALPACA_SECRET_KEY,
}

# Rate limiting: Alpaca free tier = 200 req/min
_last_request = 0
_MIN_INTERVAL = 0.35  # ~170 req/min max


def _rate_limit():
    global _last_request
    now = time.time()
    elapsed = now - _last_request
    if elapsed < _MIN_INTERVAL:
        time.sleep(_MIN_INTERVAL - elapsed)
    _last_request = time.time()


def get_daily_bars(ticker: str, days: int = 90) -> pd.DataFrame:
    """
    Get daily OHLCV bars from Alpaca.
    Returns DataFrame with columns: Open, High, Low, Close, Volume (same as yfinance format).
    """
    _rate_limit()
    
    start = (datetime.now() - timedelta(days=days + 5)).strftime("%Y-%m-%d")
    
    try:
        resp = requests.get(
            f"{DATA_URL}/stocks/{ticker}/bars",
            headers=HEADERS,
            params={
                "timeframe": "1Day",
                "start": start,
                "limit": days,
                "adjustment": "split",
            },
            timeout=10,
        )
        
        if resp.status_code != 200:
            return pd.DataFrame()
        
        data = resp.json()
        bars = data.get("bars", [])
        
        if not bars:
            return pd.DataFrame()
        
        df = pd.DataFrame(bars)
        df = df.rename(columns={
            "o": "Open",
            "h": "High", 
            "l": "Low",
            "c": "Close",
            "v": "Volume",
            "t": "Date",
        })
        df["Date"] = pd.to_datetime(df["Date"])
        df = df.set_index("Date")
        df = df[["Open", "High", "Low", "Close", "Volume"]]
        
        return df
        
    except Exception as e:
        return pd.DataFrame()


def get_option_chain(ticker: str, expiration: str = None):
    """
    Get option chain from Alpaca.
    Returns calls and puts DataFrames similar to yfinance format.
    """
    _rate_limit()
    
    try:
        params = {
            "underlying_symbols": ticker,
            "status": "active",
            "limit": 100,
        }
        if expiration:
            params["expiration_date"] = expiration
            
        resp = requests.get(
            f"https://paper-api.alpaca.markets/v2/options/contracts",
            headers=HEADERS,
            params=params,
            timeout=10,
        )
        
        if resp.status_code != 200:
            return None
        
        contracts = resp.json().get("option_contracts", [])
        if not contracts:
            return None
        
        # Get quotes for these contracts
        calls = []
        puts = []
        
        for c in contracts:
            entry = {
                "strike": float(c["strike_price"]),
                "expiration": c["expiration_date"],
                "symbol": c["symbol"],
                "type": c["type"],
            }
            
            if c["type"] == "call":
                calls.append(entry)
            else:
                puts.append(entry)
        
        # Get latest quotes for all contracts (batch)
        symbols = [c["symbol"] for c in contracts[:20]]  # limit to avoid timeout
        quotes = _get_option_quotes_batch(symbols)
        
        # Merge quotes into contracts
        calls_df = _build_chain_df(calls, quotes)
        puts_df = _build_chain_df(puts, quotes)
        
        return {"calls": calls_df, "puts": puts_df}
        
    except:
        return None


def _get_option_quotes_batch(symbols):
    """Get latest quotes for multiple option symbols."""
    if not symbols:
        return {}
    
    _rate_limit()
    
    try:
        resp = requests.get(
            "https://data.alpaca.markets/v1beta1/options/quotes/latest",
            headers=HEADERS,
            params={"symbols": ",".join(symbols)},
            timeout=10,
        )
        if resp.status_code == 200:
            return resp.json().get("quotes", {})
    except:
        pass
    return {}


def _build_chain_df(contracts, quotes):
    """Build a DataFrame from contracts + quotes data."""
    rows = []
    for c in contracts:
        q = quotes.get(c["symbol"], {})
        bid = float(q.get("bp", 0))
        ask = float(q.get("ap", 0))
        rows.append({
            "strike": c["strike"],
            "expiration": c["expiration"],
            "symbol": c["symbol"],
            "bid": bid,
            "ask": ask,
            "lastPrice": (bid + ask) / 2 if bid > 0 and ask > 0 else 0,
            "openInterest": int(q.get("bs", 0)) + int(q.get("as", 0)),  # approximate from quote sizes
        })
    
    return pd.DataFrame(rows) if rows else pd.DataFrame()


def get_option_expirations(ticker: str):
    """Get available option expiration dates for a ticker."""
    _rate_limit()
    
    try:
        resp = requests.get(
            f"https://paper-api.alpaca.markets/v2/options/contracts",
            headers=HEADERS,
            params={
                "underlying_symbols": ticker,
                "status": "active",
                "limit": 100,
            },
            timeout=10,
        )
        
        if resp.status_code != 200:
            return []
        
        contracts = resp.json().get("option_contracts", [])
        expirations = sorted(set(c["expiration_date"] for c in contracts))
        return expirations
        
    except:
        return []
