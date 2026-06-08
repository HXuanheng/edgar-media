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
import html
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
FRESH_DAYS = 7         # a filing this recent gets the "fresh" highlight
WINDOW_DAYS = 90       # per-firm list shows material filings this recent
HEADLINE_MAX_DAYS = 365  # ignore ancient filings as the card headline (ETF junk)
MAX_FILINGS = 25       # cap per firm (bounds JSON size)
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


# --- name matching --------------------------------------------------------
# Tokens dropped before comparing names: legal forms, fund/ETF boilerplate,
# and generic noise. Stripping these focuses the match on distinctive words
# (e.g. "Invesco QQQ ETF" vs "Invesco QQQ Trust, Series 1" -> both "invesco qqq").
NAME_NOISE = {
    "the", "inc", "incorporated", "corp", "corporation", "co", "company",
    "ltd", "limited", "plc", "llc", "lp", "llp", "sa", "ag", "nv",
    "holdings", "holding", "group", "class", "cl",
    "etf", "trust", "fund", "index", "series", "shares",
}


def clean_name(s):
    """Lowercase, strip punctuation, drop noise/numeric tokens -> token list."""
    if not s:
        return []
    low = "".join(c if c.isalnum() or c.isspace() else " " for c in s.lower())
    return [t for t in low.split()
            if t and not t.isdigit() and t not in NAME_NOISE]


def name_similarity(a, b):
    """True if two company names plausibly refer to the same entity.

    Conservative: only a clear conflict is treated as a mismatch.
    """
    ta, tb = clean_name(a), clean_name(b)
    if not ta or not tb:
        return False
    sa, sb = set(ta), set(tb)
    if sa <= sb or sb <= sa:          # one name's tokens contain the other's
        return True
    if len(sa & sb) / len(sa | sb) >= 0.5:      # majority token overlap
        return True
    # Rebrand / contained name (e.g. 'strategy' inside 'microstrategy').
    # Require a long token AND a *proper* substring (x != y) so a single
    # shared token doesn't trigger it ('american' must NOT match 'american'
    # alone) and short fragments don't ('spac' must NOT match 'space').
    for x in sa:
        if len(x) >= 6 and any(x != y and (x in y or y in x) for y in sb):
            return True
    return False


def prettify(s):
    """Title-case all-caps SEC names ('APPLE INC.' -> 'Apple Inc.')."""
    if s and s.isupper():
        return s.title()
    return s


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
def load_ticker_map():
    """SEC ticker -> {"cik": 10-digit CIK, "sec_name": official title}.

    Single company_tickers.json request; the official name comes free with it.
    """
    data = get_json(TICKERS_URL)
    if not data:
        return {}
    out = {}
    for row in data.values():
        out[row["ticker"].upper()] = {
            "cik": str(row["cik_str"]).zfill(10),
            "sec_name": row.get("title", ""),
        }
    return out


def build_filing(recent, i, cik_int, today):
    """Construct one filing dict from index i of a filings.recent block."""
    forms = recent["form"]
    date = recent["filingDate"][i]
    acc = recent["accessionNumber"][i]
    acc_nodash = acc.replace("-", "")
    doc = recent["primaryDocument"][i]
    try:
        days_ago = (today - datetime.strptime(date, "%Y-%m-%d").date()).days
    except ValueError:
        days_ago = None
    base = f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{acc_nodash}"
    return {
        "form": forms[i],
        "date": date,
        "days_ago": days_ago,
        "fresh": days_ago is not None and days_ago <= FRESH_DAYS,
        "desc": recent.get("primaryDocDescription", [""] * len(forms))[i] or forms[i],
        "doc_url": f"{base}/{doc}" if doc else f"{base}/",
        "index_url": f"{base}/{acc}-index.htm",
    }


def recent_filings(cik, today):
    """Fetch a CIK's submissions once and return (headline, windowed_list).

    headline = most recent material filing of any date (drives the card's top
               line + fresh/hot), or None.
    windowed_list = material filings within WINDOW_DAYS, newest-first, capped
               at MAX_FILINGS (the click-to-expand list; may be empty).

    filings.recent is newest-first, so we stop as soon as we pass the window.
    Note: only filings.recent is read. For a hyper-active filer whose recent[]
    is saturated, older material filings can spill into filings.files shards
    and won't appear here. Rare and acceptable for a 90-day window.
    """
    data = get_json(SUBMISSIONS_URL.format(cik=cik))
    time.sleep(SEC_PAUSE)
    if not data:
        return None, []
    recent = data.get("filings", {}).get("recent", {})
    forms = recent.get("form", [])
    cik_int = int(cik)
    headline = None
    windowed = []
    for i, form in enumerate(forms):
        if form not in MATERIAL_FORMS:
            continue
        f = build_filing(recent, i, cik_int, today)
        if (headline is None and f["days_ago"] is not None
                and f["days_ago"] <= HEADLINE_MAX_DAYS):
            headline = f                       # newest recent material = headline
        if f["days_ago"] is None or f["days_ago"] > WINDOW_DAYS:
            break                              # nothing later is within window
        # Trim doc_url from list entries (they link via index_url) to stay lean.
        windowed.append({k: v for k, v in f.items() if k != "doc_url"})
        if len(windowed) >= MAX_FILINGS:
            break
    return headline, windowed


def main():
    now = datetime.now(timezone.utc)
    today = now.date()

    print("fetching ApeWisdom (Reddit attention)...")
    ape = get_json(APEWISDOM_URL)
    if not ape or "results" not in ape:
        print("ABORT: no ApeWisdom data", file=sys.stderr)
        sys.exit(1)
    results = ape["results"][:TOP_N]

    print("loading SEC ticker map...")
    cik_map = load_ticker_map()

    items = []
    for row in results:
        ticker = row["ticker"].upper()
        # ApeWisdom returns HTML-encoded names ("S&amp;P"); decode so the
        # front-end escapes exactly once.
        ape_name = html.unescape(row.get("name") or ticker)
        m, m0 = row.get("mentions", 0), row.get("mentions_24h_ago", 0)
        change = round((m - m0) / m0 * 100, 1) if m0 else None

        entry = cik_map.get(ticker)
        cik = entry["cik"] if entry else None
        sec_name = entry["sec_name"] if entry else None
        if not cik:
            name_match = "no_cik"
        elif not ape_name or ape_name.strip().upper() == ticker:
            # ApeWisdom gave no real name (just the ticker) -> nothing to
            # contradict SEC's authoritative ticker map; trust it.
            name_match = "verified"
        elif name_similarity(ape_name, sec_name):
            name_match = "verified"
        else:
            name_match = "mismatch"
        display_name = prettify(sec_name) if sec_name else ape_name

        filing, filings = recent_filings(cik, today) if cik else (None, [])
        if cik:
            extra = max(len(filings) - 1, 0)
            print(f"  {ticker:6s} cik={cik} {name_match:8s} "
                  f"filing={filing['form'] if filing else '-'} "
                  f"(+{extra} in {WINDOW_DAYS}d)")
        else:
            print(f"  {ticker:6s} (no SEC match - ETF/crypto/foreign)")

        items.append({
            "rank": row.get("rank"),
            "ticker": ticker,
            "name": ape_name,
            "sec_name": sec_name,
            "display_name": display_name,
            "name_match": name_match,
            "mentions": m,
            "mentions_24h_ago": m0,
            "mention_change_pct": change,
            "upvotes": row.get("upvotes", 0),
            "cik": cik,
            "filing": filing,
            "filings": filings,
        })

    # Hot = VERIFIED name match AND a fresh material filing. A mismatched
    # match is never surfaced as confident.
    def is_hot(x):
        return (x["name_match"] == "verified"
                and x["filing"] and x["filing"]["fresh"])

    items.sort(key=lambda x: (
        not is_hot(x),                               # hot items first
        x["rank"] if x["rank"] is not None else 999,
    ))

    payload = {
        "updated_utc": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": {
            "attention": "ApeWisdom (Reddit mentions, ~hourly)",
            "filings": "SEC EDGAR submissions API (real-time)",
        },
        "fresh_days": FRESH_DAYS,
        "window_days": WINDOW_DAYS,
        "count": len(items),
        "items": items,
    }

    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
    hot = sum(1 for x in items if is_hot(x))
    mism = sum(1 for x in items if x["name_match"] == "mismatch")
    print(f"\nwrote {OUT_PATH}: {len(items)} tickers, "
          f"{hot} hot (verified + fresh), {mism} name-mismatch")


if __name__ == "__main__":
    main()
