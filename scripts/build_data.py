#!/usr/bin/env python3
"""Build data/trending.json.

Joins two free, live signals:
  - Reddit attention  -> ApeWisdom API (mentions per ticker, with 24h momentum)
  - Official filings   -> SEC EDGAR submissions API (latest material disclosure)

The thesis: Reddit tells us WHAT people are paying attention to; SEC tells us
the OFFICIAL document behind it. The gold case is a trending ticker that ALSO
filed something material in the last few days ("fresh" flag).

Pure standard library -> zero pip installs, robust in CI.
SEC requires a descriptive User-Agent or it returns HTTP 403.
"""

import gzip
import io
import json
import os
import sys
import time
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

# --- config ---------------------------------------------------------------
UA = "edgar-media academic project (xuanheng.huang@phd.unibocconi.it)"
APEWISDOM_URL = "https://apewisdom.io/api/v1.0/filter/all-stocks/page/1"
TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"

TOP_N = 30          # how many trending tickers to keep
FRESH_DAYS = 7      # a filing this recent gets the "fresh" highlight
SEC_PAUSE = 0.15    # polite gap between SEC calls (limit is 10 req/sec)

# Forms that carry real disclosure content (skip Form 4 insider noise etc.)
MATERIAL_FORMS = {
    "8-K", "10-K", "10-Q", "10-K/A", "10-Q/A", "8-K/A",
    "S-1", "S-1/A", "S-4", "424B4", "424B5",
    "6-K", "20-F", "40-F", "F-1",
    "DEF 14A", "DEFA14A", "PRE 14A",
    "SC 13D", "SC 13G", "SC 13D/A", "SC 13G/A", "SC TO-T", "SC 14D9",
}

OUT_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "trending.json")


# --- http helpers ---------------------------------------------------------
def get_json(url, retries=3, pause=1.0):
    """GET a URL and parse JSON, with retries and gzip handling."""
    last = None
    for attempt in range(retries):
        try:
            req = Request(url, headers={
                "User-Agent": UA,
                "Accept-Encoding": "gzip",
                "Accept": "application/json",
            })
            with urlopen(req, timeout=30) as resp:
                raw = resp.read()
                if resp.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.GzipFile(fileobj=io.BytesIO(raw)).read()
                return json.loads(raw.decode("utf-8"))
        except (HTTPError, URLError, TimeoutError) as e:
            last = e
            time.sleep(pause * (attempt + 1))
    print(f"  ! failed {url}: {last}", file=sys.stderr)
    return None


# --- pipeline -------------------------------------------------------------
def load_ticker_to_cik():
    """SEC ticker -> zero-padded 10-digit CIK map."""
    data = get_json(TICKERS_URL)
    if not data:
        return {}
    out = {}
    for row in data.values():
        out[row["ticker"].upper()] = str(row["cik_str"]).zfill(10)
    return out


def latest_material_filing(cik, today):
    """Return the most recent material filing for a CIK, or None."""
    data = get_json(SUBMISSIONS_URL.format(cik=cik))
    time.sleep(SEC_PAUSE)
    if not data:
        return None
    recent = data.get("filings", {}).get("recent", {})
    forms = recent.get("form", [])
    for i, form in enumerate(forms):
        if form not in MATERIAL_FORMS:
            continue
        date = recent["filingDate"][i]
        acc = recent["accessionNumber"][i]
        acc_nodash = acc.replace("-", "")
        doc = recent["primaryDocument"][i]
        cik_int = int(cik)
        try:
            days_ago = (today - datetime.strptime(date, "%Y-%m-%d").date()).days
        except ValueError:
            days_ago = None
        base = f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{acc_nodash}"
        return {
            "form": form,
            "date": date,
            "days_ago": days_ago,
            "fresh": days_ago is not None and days_ago <= FRESH_DAYS,
            "desc": recent.get("primaryDocDescription", [""] * len(forms))[i] or form,
            "doc_url": f"{base}/{doc}" if doc else f"{base}/",
            "index_url": f"{base}/{acc}-index.htm",
        }
    return None


def main():
    now = datetime.now(timezone.utc)
    today = now.date()

    print("fetching ApeWisdom (Reddit attention)...")
    ape = get_json(APEWISDOM_URL)
    if not ape or "results" not in ape:
        print("ABORT: no ApeWisdom data", file=sys.stderr)
        sys.exit(1)
    results = ape["results"][:TOP_N]

    print("loading SEC ticker->CIK map...")
    cik_map = load_ticker_to_cik()

    items = []
    for row in results:
        ticker = row["ticker"].upper()
        m, m0 = row.get("mentions", 0), row.get("mentions_24h_ago", 0)
        change = round((m - m0) / m0 * 100, 1) if m0 else None
        cik = cik_map.get(ticker)
        filing = latest_material_filing(cik, today) if cik else None
        if cik:
            print(f"  {ticker:6s} cik={cik} "
                  f"filing={filing['form'] if filing else '-'}")
        else:
            print(f"  {ticker:6s} (no SEC match - ETF/crypto/foreign)")
        items.append({
            "rank": row.get("rank"),
            "ticker": ticker,
            "name": row.get("name", ticker),
            "mentions": m,
            "mentions_24h_ago": m0,
            "mention_change_pct": change,
            "upvotes": row.get("upvotes", 0),
            "cik": cik,
            "filing": filing,
        })

    # Hot first: trending AND a fresh material filing.
    items.sort(key=lambda x: (
        not (x["filing"] and x["filing"]["fresh"]),  # fresh-filing items first
        x["rank"] if x["rank"] is not None else 999,
    ))

    payload = {
        "updated_utc": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": {
            "attention": "ApeWisdom (Reddit mentions, ~hourly)",
            "filings": "SEC EDGAR submissions API (real-time)",
        },
        "fresh_days": FRESH_DAYS,
        "count": len(items),
        "items": items,
    }

    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
    hot = sum(1 for x in items if x["filing"] and x["filing"]["fresh"])
    print(f"\nwrote {OUT_PATH}: {len(items)} tickers, {hot} with a fresh filing")


if __name__ == "__main__":
    main()
