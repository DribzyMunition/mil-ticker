#!/usr/bin/env python3
"""
MIL-Ticker data builder
- Oil: WTI/Brent via yfinance
- Steel: TradingEconomics (if TE_KEY set) OR Yahoo Finance futures (HRC=F) fallback
- Metals: Copper (HG=F), Aluminum (ALI=F) via yfinance (with safe fallbacks)
- DoD contracts: official RSS (best-effort parse)
- Tier 2 lists: manual
- Output: public/data.json
"""
import json, time, os, re, pathlib

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

def yahoo_last_two(symbol):
    """Return (last, prev) closes for a Yahoo ticker or (None, None)."""
    try:
        import yfinance as yf
        t = yf.Ticker(symbol)
        hist = t.history(period="5d", interval="1d")
        if hist is None or hist.empty:
            return None, None
        closes = hist["Close"].dropna().tail(2).tolist()
        if not closes: return None, None
        last = r2(closes[-1])
        prev = r2(closes[-2]) if len(closes) > 1 else last
        return last, prev
    except Exception:
        return None, None

# ---------- oil via yfinance ----------
def fetch_oil():
    out = []
    for sym, name in [("CL=F", "WTI"), ("BZ=F", "Brent")]:
        last, prev = yahoo_last_two(sym)
        if last is not None:
            out.append({"name": name, "price": last, "pct": pct(last, prev)})
    # Safety net if Yahoo was unavailable
    if not out:
        out = [
            {"name": "WTI", "price": 83.12, "pct": 0.0},
            {"name": "Brent", "price": 86.47, "pct": 0.0},
        ]
    return out

# ---------- steel via TradingEconomics OR Yahoo ----------
def fetch_hrc():
    # 1) Try TradingEconomics if TE_KEY present
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
        except Exception:
            pass
    # 2) Try Yahoo Finance futures (US Midwest HRC)
    last, prev = yahoo_last_two("HRC=F")
    if last is not None:
        return {"name": "HRC Steel", "price": last, "pct": pct(last, prev)}
    # 3) Fallback manual
    return {"name": "HRC Steel", "price": 830.00, "pct": 0.9}

# ---------- metals via Yahoo with fallback ----------
def fetch_metal(symbol, name, fallback_price, fallback_pct):
    last, prev = yahoo_last_two(symbol)
    if last is not None:
        return {"name": name, "price": last, "pct": pct(last, prev)}
    return {"name": name, "price": fallback_price, "pct": fallback_pct}

# ---------- DoD Contracts via RSS ----------
def fetch_dod_contracts(limit=6):
    """
    Official DoD contracts RSS: best-effort vendor + $ scrape from summary.
    If the pattern isn't present, you still get whatever entries matched.
    """
    try:
        import feedparser
    except Exception:
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
        text = re.sub(r"<[^>]+>", " ", text)  # strip tags
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
    # Commodities (live where possible)
    commodities = []
    commodities.extend(fetch_oil())
    commodities.append(fetch_hrc())
    # Copper (HG=F), Aluminum (ALI=F) with safe fallbacks
    commodities.append(fetch_metal("HG=F",  "Copper",   4.12,  -1.8))
    commodities.append(fetch_metal("ALI=F", "Aluminum", 2421,   0.7))

    # Contracts: live RSS + a couple of anchors
    live = []
    try:
        live = fetch_dod_contracts(limit=6)
    except Exception:
        live = []
    contracts = (live or []) + [
        {"entity": "Lockheed Martin", "value_usd": 540_000_000, "note": "JASSM production lot (placeholder)"},
        {"entity": "Raytheon",        "value_usd": 220_000_000, "note": "Patriot spares IDIQ (placeholder)"},
    ]

    # Tier 1: conflicts (manual notes for now)
    conflicts = [
        {"name": "Black Sea",    "note": "Drone strike uptick"},
        {"name": "Red Sea",      "note": "Shipping insurance premia rising"},
        {"name": "Taiwan Strait","note": "Increased ADIZ incursions"},
        {"name": "Sahel",        "note": "Cross-border operations reported"},
    ]

    # Tier 2: apparel
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
