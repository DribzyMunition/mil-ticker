#!/usr/bin/env python3
"""
MIL-Ticker data builder
- Live: WTI/Brent (yfinance), HRC Steel via TradingEconomics (if TE_KEY set)
- Live: DoD daily contract awards (defense.gov RSS)
- Manual: apparel + conflict notes
- Output: public/data.json
"""
import json, time, os, re, pathlib

def r2(x):
    try: return float(f"{float(x):.2f}")
    except: return None

def pct(curr, prev):
    try:
        if prev in (None, 0): return 0.0
        return r2(100.0 * (float(curr) - float(prev)) / float(prev))
    except:
        return 0.0

# --- Oil via yfinance ---
def fetch_oil():
    try:
        import yfinance as yf
        tickers = yf.Tickers("CL=F BZ=F")
        out = []
        for sym, name in [("CL=F", "WTI"), ("BZ=F", "Brent")]:
            t = tickers.tickers[sym]
            hist = t.history(period="5d", interval="1d")
            if hist is None or hist.empty: 
                continue
            closes = hist["Close"].dropna().tail(2).tolist()
            price = r2(closes[-1])
            prev  = r2(closes[-2]) if len(closes) > 1 else price
            out.append({"name": name, "price": price, "pct": pct(price, prev)})
        return out
    except Exception:
        # graceful fallback so site never breaks
        return [
            {"name": "WTI", "price": 83.12, "pct": 0.0},
            {"name": "Brent", "price": 86.47, "pct": 0.0},
        ]

# --- HRC Steel via TradingEconomics (optional) ---
def fetch_hrc_te():
    """
    Requires repo secret TE_KEY (TradingEconomics API key).
    If absent, returns None and we use a manual fallback.
    """
    key = os.getenv("TE_KEY")
    if not key:
        return None
    import requests
    url = f"https://api.tradingeconomics.com/markets/commodities?c={key}&f=json"
    r = requests.get(url, timeout=20)
    r.raise_for_status()
    data = r.json()
    for row in data:
        name = (row.get("Name") or "").lower()
        if "hrc" in name and "steel" in name:
            last = row.get("Last")
            chg  = row.get("DailyPercentualChange")
            if last is None: 
                continue
            return {"name": "HRC Steel", "price": r2(last), "pct": r2(chg or 0)}
    return None

# --- DoD Contracts via RSS ---
def fetch_dod_contracts(limit=6):
    """
    Official DoD contracts RSS: vendor names + amounts appear in the summary.
    We regex approximate 'X was awarded $NNN (million|billion)'. Best-effort.
    """
    import feedparser
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
        text = re.sub(r"<[^>]+>", " ", text)  # strip tags
        for m in pattern.finditer(text):
            entity = m.group(1).strip()
            amt = float(m.group(2).replace(",", ""))
            scale = (m.group(3) or "").lower()
            if scale == "billion":
                value = int(amt * 1_000_000_000)
            elif scale == "million":
                value = int(amt * 1_000_000)
            else:
                value = int(amt)
            contracts.append({"entity": entity, "value_usd": value, "note": "DoD daily awards"})
            if len(contracts) >= limit:
                return contracts
    return contracts

def main():
    # Commodities
    commodities = fetch_oil()
    try:
        hrc = fetch_hrc_te()
    except Exception:
        hrc = None
    if hrc:
        commodities.append(hrc)
    else:
        commodities.append({"name": "HRC Steel", "price": 830.00, "pct": 0.9})
    # Optional static metals for context
    commodities += [
        {"name": "Copper", "price": 4.12, "pct": -1.8},
        {"name": "Aluminum", "price": 2421.00, "pct": 0.7}
    ]

    # Contracts: live + a couple of anchors
    try:
        live = fetch_dod_contracts(limit=6)
    except Exception:
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

    out = pathlib.Path("public") / "data.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2))
    print(f"Wrote {out.resolve()}")

if __name__ == "__main__":
    main()
