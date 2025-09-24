#!/usr/bin/env python3
"""
MIL-Ticker data builder
- Oil: WTI/Brent via yfinance (daily close change, robust fallbacks)
- Steel: TradingEconomics (if TE_KEY) OR Yahoo HRC=F fallback, else manual
- Metals: Copper (HG=F) & Aluminum (ALI=F) via yfinance (fallback safe)
- DoD contracts: official RSS (best-effort parse)
- Tier 2 (apparel): manual list
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

# ---------- Yahoo helpers ----------
def yahoo_last_two_closes(symbol):
    """Return (last_close, prev_close) using daily candles or (None, None)."""
    try:
        import yfinance as yf
        t = yf.Ticker(symbol)
        # Pull up to 10 days to survive holidays/weekends
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
            info = t.info  # slower; ok in Actions
            price = price or info.get("regularMarketPrice")
            prev  = prev  or info.get("regularMarketPreviousClose")
        return r2(price), r2(prev)
    except Exception as e:
        print(f"[YF] {symbol} info error: {e}")
        return None, None

# ---------- oil via yfinance (prefer daily close change) ----------
def fetch_oil():
    out = []
    for sym, name in [("CL=F", "WTI"), ("BZ=F", "Brent")]:
        price, prev = yahoo_last_two_closes(sym)   # day-on-day close change
        if price is None or prev is None:
            # fallback to live price vs prev close (intraday)
            price, prev = yahoo_live_price_and_prev(sym)
        if price is not None and prev is not None:
            out.append({"name": name, "price": price, "pct": pct(price, prev)})
        else:
            print(f"[OIL] {sym} fell back to placeholder")
            # final safety net
            out.append({"name": name, "price": 0.0, "pct": 0.0})

    # Ensure both exist & nonzero price if we had to placeholder
    for x in out:
        if not isinstance(x["price"], (int, float)) or x["price"] == 0.0:
            if x["name"] == "WTI": x["price"] = 83.12
            if x["name"] == "Brent": x["price"] = 86.47
    return out

# ---------- steel via TradingEconomics OR Yahoo OR manual ----------
def fetch_hrc():
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
        last, prev = yahoo_live_price_and_prev("HRC=F")
    if last is not None and prev is not None:
        return {"name": "HRC Steel", "price": last, "pct": pct(last, prev)}

    # Manual fallback
    return {"name": "HRC Steel", "price": 830.00, "pct": 0.9}

# ---------- generic metal via Yahoo with fallback ----------
def fetch_metal(symbol, name, fallback_price, fallback_pct):
    last, prev = yahoo_last_two_closes(symbol)
    if last is None or prev is None:
        last, prev = yahoo_live_price_and_prev(symbol)
    if last is not None and prev is not None:
        return {"name": name, "price": last, "pct": pct(last, prev)}
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
    commodities.append(fetch_metal("HG=F",  "Copper",   4.12, -1.8))
    commodities.append(fetch_metal("ALI=F", "Aluminum", 2421,  0.7))

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

    out = pathlib.Path("public") / "data.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2))
    print(f"Wrote {out.resolve()}")

if __name__ == "__main__":
    main()
