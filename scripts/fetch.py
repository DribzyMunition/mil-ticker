#!/usr/bin/env python3
"""
MIL-Ticker data builder
- Oil: WTI/Brent via yfinance (prefers daily closes; falls back to live price)
- Steel: TradingEconomics (if TE_KEY) OR Yahoo HRC=F fallback, else manual
- Metals: Copper (HG=F) & Aluminum (ALI=F) via yfinance (fallback safe)
- DoD contracts: official RSS (best-effort parse)
- % fallback: if no "previous close", compare to price stored in prior public/data.json
- Output: public/data.json
"""
import json, time, os, re, pathlib

DATA_PATH = pathlib.Path("public") / "data.json"

# ---------- helpers ----------
def r2(x):
    try: return float(f"{float(x):.2f}")
    except: return None

def pct(curr, prev):
    try:
        if prev in (None, 0): return 0.0
        return r2(100.0 * (float(curr) - float(prev)) / float(prev))
    except:
        return 0.0

def load_previous_prices():
    """Return {commodity_name: last_saved_price} from existing public/data.json, if any."""
    try:
        if DATA_PATH.exists():
            data = json.loads(DATA_PATH.read_text())
            prev = {}
            for c in data.get("commodities", []):
                name = c.get("name")
                price = c.get("price")
                if name is not None and isinstance(price, (int, float)):
                    prev[name] = float(price)
            return prev
    except Exception as e:
        print(f"[PREV] couldn't read prior data.json: {e}")
    return {}

# ---------- Yahoo helpers ----------
def yahoo_last_two_closes(symbol):
    """Return (last_close, prev_close) using daily candles or (None, None)."""
    try:
        import yfinance as yf
        t = yf.Ticker(symbol)
        hist = t.history(period="10d", interval="1d", auto_adjust=False)
        if hist is None or hist.empty:
            print(f"[YF] {symbol} daily history empty")
            return None, None
        closes = [float(x) for x in hist["Close"].dropna().tolist()]
        if len(closes) < 2:
            print(f"[YF] {symbol} not enough closes")
            return None, None
        return r2(closes[-1]), r2(closes[-2])
    except Exception as e:
        print(f"[YF] {symbol} closes error: {e}")
        return None, None

def yahoo_live_price_and_prev(symbol):
    """Return (live_price, prev_close) via fast_info/info or (None, None)."""
    try:
        import yfinance as yf
        t = yf.Ticker(symbol)
        price = prev = None
        fast = getattr(t, "fast_info", None)
        if fast:
            price = fast.get("last_price") or fast.get("regular_market_price")
            prev  = fast.get("previous_close") or fast.get("previousClose") or prev
        if price is None or prev is None:
            info = t.info  # slower
            price = price or info.get("regularMarketPrice")
            prev  = prev  or info.get("regularMarketPreviousClose")
        return r2(price), r2(prev)
    except Exception as e:
        print(f"[YF] {symbol} info error: {e}")
        return None, None

# ---------- oil via yfinance (with fallback to prior saved price) ----------
def fetch_oil(prev_prices):
    out = []
    for sym, name in [("CL=F", "WTI"), ("BZ=F", "Brent")]:
        price, prev = yahoo_last_two_closes(sym)   # preferred: day-on-day
        if price is None or prev is None:
            # fallback to live price vs prev close
            lp, pv = yahoo_live_price_and_prev(sym)
            price = price or lp
            prev  = prev  or pv

        # last resort: compare to prior saved price so % isn't always 0
        if price is not None and prev is None:
            prev = prev_prices.get(name)

        # if still nothing, placeholder
        if price is None:
            price = 83.12 if name == "WTI" else 86.47
        if prev is None:
            prev = price  # will yield 0.0%, acceptable on first-ever run

        out.append({"name": name, "price": r2(price), "pct": pct(price, prev)})
    return out

# ---------- steel via TradingEconomics OR Yahoo OR manual ----------
def fetch_hrc(prev_prices):
    key = os.getenv("TE_KEY")
    if key:
        try:
            import requests
            url = f"https://api.tradingeconomics.com/markets/commodities?c={key}&f=json"
            r = requests.get(url, timeout=20)
            r.raise_for_status()
            for row in r.json():
                name = (row.get("Name") or "").lower()
                if "hrc" in name and "steel" in name:
                    last = row.get("Last")
                    chg  = row.get("DailyPercentualChange") or 0
                    if last is not None:
                        return {"name": "HRC Steel", "price": r2(last), "pct": r2(chg)}
        except Exception as e:
            print(f"[TE] HRC fetch error: {e}")

    # Yahoo fallback (US Midwest HRC futures)
    last, prev = yahoo_last_two_closes("HRC=F")
    if last is None or prev is None:
        lp, pv = yahoo_live_price_and_prev("HRC=F")
        last = last or lp
        prev = prev or pv
    if last is not None and prev is None:
        prev = prev_prices.get("HRC Steel")
    if last is not None and prev is not None:
        return {"name": "HRC Steel", "price": r2(last), "pct": pct(last, prev)}

    # Manual fallback
    return {"name": "HRC Steel", "price": 830.00, "pct": 0.9}

# ---------- generic metal via Yahoo with fallback to prior price ----------
def fetch_metal(symbol, name, fallback_price, fallback_pct, prev_prices):
    last, prev = yahoo_last_two_closes(symbol)
    if last is None or prev is None:
        lp, pv = yahoo_live_price_and_prev(symbol)
        last = last or lp
        prev = prev or pv
    if last is not None and prev is None:
        prev = prev_prices.get(name)
    if last is not None and prev is not None:
        return {"name": name, "price": r2(last), "pct": pct(last, prev)}
    return {"name": name, "price": fallback_price, "pct": fallback_pct}

# ---------- DoD Contracts via RSS ----------
def fetch_dod_contracts(limit=6):
    try:
        import feedparser
    except Exception as e:
        print(f"[RSS] feedparser import error: {e}")
        return []
    feed_url = "https://www.defense.gov/DesktopModules/ArticleCS/RSS.ashx?ContentType=400&Site=727&max=10"
    d = feedparser.parse(feed_url)
    contracts = []
    pattern = re.compile(
        r'([A-Z][A-Za-z0-9&\.\- ]+(?:, [A-Z][A-Za-z\.\- ]+)*?)\s*(?:, [A-Z]{2})?\s*'
        r'(?:has been awarded|was awarded)\s*\$([\d,]+(?:\.\d+)?)\s*(billion|million)?',
        re.IGNORECASE
    )
    for entry in d.entries[:4]:
        text = entry.get("summary", "") or entry.get("description", "") or ""
        text = re.sub(r"<[^>]+>", " ", text)
        for m in pattern.finditer(text):
            entity = m.group(1).strip()
            amt = float(m.group(2).replace(",", ""))
            scale = (m.group(3) or "").lower()
            if scale == "billion": value = int(amt * 1_000_000_000)
            elif scale == "million": value = int(amt * 1_000_000)
            else: value = int(amt)
            contracts.append({"entity": entity, "value_usd": value, "note": "DoD daily awards"})
            if len(contracts) >= limit:
                return contracts
    return contracts

def main():
    prev_prices = load_previous_prices()

    # Commodities (live where possible; fallback to prior saved price for %)
    commodities = []
    commodities.extend(fetch_oil(prev_prices))
    commodities.append(fetch_hrc(prev_prices))
    commodities.append(fetch_metal("HG=F",  "Copper",   4.12, -1.8, prev_prices))
    commodities.append(fetch_metal("ALI=F", "Aluminum", 2421,  0.7, prev_prices))

    # Contracts: live RSS + placeholders
    try:
        live = fetch_dod_contracts(limit=6)
    except Exception as e:
        print(f"[RSS] fetch error: {e}")
        live = []
    contracts = (live or []) + [
        {"entity": "Lockheed Martin", "value_usd": 540_000_000, "note": "JASSM production lot (placeholder)"},
        {"entity": "Raytheon",        "value_usd": 220_000_000, "note": "Patriot spares IDIQ (placeholder)"},
    ]

    conflicts = [
        {"name": "Black Sea",    "note": "Drone strike uptick"},
        {"name": "Red Sea",      "note": "Shipping insurance premia rising"},
        {"name": "Taiwan Strait","note": "Increased ADIZ incursions"},
        {"name": "Sahel",        "note": "Cross-border operations reported"},
    ]

    apparel = [
        {"brand": "Arc'teryx (LEAF)", "note": "Technical shells, load-bearing apparel"},
        {"brand": "The North Face",   "note": "Extreme cold-weather lines; expedition wear"},
        {"brand": "Crye Precision",   "note": "Combat uniforms & plate carriers"},
        {"brand": "5.11 Tactical",    "note": "Duty apparel & gear"}
    ]

    payload = {
        "commodities": commodities,
        "contracts": contracts,
        "conflicts": conflicts,
        "apparel": apparel,
        "generated_at": int(time.time())
    }

    DATA_PATH.parent.mkdir(parents=True, exist_ok=True)
    DATA_PATH.write_text(json.dumps(payload, indent=2))
    print(f"Wrote {DATA_PATH.resolve()}")

if __name__ == "__main__":
    main()
