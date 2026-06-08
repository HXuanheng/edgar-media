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
import re
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

# --- Gemini (optional, free) ----------------------------------------------
# One-sentence prose summary of FRESH headline filings, via Google's free
# Gemini API. Cached by accession in data/summaries.json so each filing is
# summarized once ever. No key -> skipped entirely (item-code labels still
# show). Key comes from the GEMINI_API_KEY env var (a GitHub Actions secret).
GEMINI_MODEL = "gemini-2.5-flash"   # must be a current free-tier Flash model
GEMINI_URL = ("https://generativelanguage.googleapis.com/v1beta/"
              "models/{model}:generateContent?key={key}")
GEMINI_PAUSE = 4.0     # ~15 req/min, well inside the free-tier RPM limit
GEMINI_MAX_CHARS = 50000  # filing text fed to the model (covers an 8-K fully)
AI_MAX_DAYS = 10       # only summarize headline filings this recent (bounds volume)
MAX_AI_PER_RUN = 20    # cap new summaries per run; backlog drains over hours

# Forms that carry real disclosure content (skip Form 4 insider noise etc.)
MATERIAL_FORMS = {
    "8-K", "10-K", "10-Q", "10-K/A", "10-Q/A", "8-K/A",
    "S-1", "S-1/A", "S-4", "424B4", "424B5",
    "6-K", "20-F", "40-F", "F-1",
    "DEF 14A", "DEFA14A", "PRE 14A",
    "SC 13D", "SC 13G", "SC 13D/A", "SC 13G/A", "SC TO-T", "SC 14D9",
}

# Form code -> plain-English label (the free, can't-be-wrong summary baseline).
FORM_LABELS = {
    "8-K": "Material event",
    "8-K/A": "Material event (amended)",
    "10-K": "Annual report",
    "10-K/A": "Annual report (amended)",
    "10-Q": "Quarterly report",
    "10-Q/A": "Quarterly report (amended)",
    "S-1": "IPO / new securities",
    "S-1/A": "IPO registration (amended)",
    "S-4": "M&A / exchange registration",
    "424B4": "IPO pricing prospectus",
    "424B5": "Shelf offering prospectus",
    "6-K": "Foreign issuer update",
    "20-F": "Annual report (foreign)",
    "40-F": "Annual report (foreign)",
    "F-1": "IPO registration (foreign)",
    "DEF 14A": "Proxy statement",
    "DEFA14A": "Proxy materials (additional)",
    "PRE 14A": "Preliminary proxy",
    "SC 13D": "Activist / large stake",
    "SC 13D/A": "Activist stake (amended)",
    "SC 13G": "Passive large stake",
    "SC 13G/A": "Passive stake (amended)",
    "SC TO-T": "Tender offer",
    "SC 14D9": "Tender offer response",
}

# 8-K item code -> what actually happened. These are SEC's own tags, shipped
# in filings.recent["items"], so the label is authoritative (never guessed).
# 9.01 (financial exhibits) is dropped -- it's attachments, not the event.
ITEM_LABELS = {
    "1.01": "Material agreement signed",
    "1.02": "Material agreement terminated",
    "1.03": "Bankruptcy",
    "2.01": "Acquisition / disposition completed",
    "2.02": "Earnings released",
    "2.03": "New debt obligation",
    "2.05": "Restructuring / costs",
    "3.01": "Delisting notice",
    "3.02": "Unregistered share sale",
    "4.01": "Auditor change",
    "4.02": "Financials no longer reliable",
    "5.01": "Change in control",
    "5.02": "Executive / board change",
    "5.03": "Bylaw / charter change",
    "5.07": "Shareholder vote results",
    "7.01": "Other material update",
    "8.01": "Other material update",
}


def filing_summary(form, items_raw):
    """Plain-English label for a filing: 8-K item codes if present, else form."""
    if form.startswith("8-K") and items_raw:
        seen = []
        for code in items_raw.split(","):
            label = ITEM_LABELS.get(code.strip())
            if label and label not in seen:
                seen.append(label)
        if seen:
            return "; ".join(seen)
        return FORM_LABELS.get("8-K", "8-K")
    return FORM_LABELS.get(form, form)


OUT_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "trending.json")
SUMMARIES_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "summaries.json")


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


def get_text(url, retries=3, pause=1.0):
    """GET a URL and return decoded text (for filing documents, which are HTML)."""
    last = None
    for attempt in range(retries):
        try:
            req = Request(url, headers={
                "User-Agent": UA,
                "Accept-Encoding": "gzip",
            })
            with urlopen(req, timeout=30) as resp:
                raw = resp.read()
                if resp.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.GzipFile(fileobj=io.BytesIO(raw)).read()
                return raw.decode("utf-8", errors="replace")
        except (HTTPError, URLError, TimeoutError) as e:
            last = e
            time.sleep(pause * (attempt + 1))
    print(f"  ! failed {url}: {last}", file=sys.stderr)
    return None


# --- AI summary (optional, free Gemini) -----------------------------------
def strip_html(s, limit=GEMINI_MAX_CHARS):
    """Crude HTML -> text for feeding a filing to the model. Truncated."""
    if not s:
        return ""
    s = re.sub(r"(?is)<(script|style).*?</\1>", " ", s)   # drop noisy blocks
    s = re.sub(r"(?s)<[^>]+>", " ", s)                    # strip tags
    s = html.unescape(s)
    s = re.sub(r"\s+", " ", s).strip()
    return s[:limit]


def gemini_summarize(text, form, key, retries=3, pause=1.0):
    """One-sentence summary of filing text via Gemini, or None on any failure."""
    if not text:
        return None
    url = GEMINI_URL.format(model=GEMINI_MODEL, key=key)
    prompt = (f"Summarize this SEC {form} filing for a retail investor in ONE "
              f"factual sentence (max 25 words), no preamble:\n\n{text}")
    body = json.dumps({
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "maxOutputTokens": 120,
            "temperature": 0.2,
            # 2.5 Flash is a thinking model; thinking tokens count against the
            # output budget. We want a quick one-liner, so disable thinking or
            # the budget gets eaten and the answer comes back truncated/empty.
            "thinkingConfig": {"thinkingBudget": 0},
        },
    }).encode("utf-8")
    last = None
    for attempt in range(retries):
        try:
            req = Request(url, data=body,
                          headers={"Content-Type": "application/json"})
            with urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            cands = data.get("candidates")
            if not cands:                       # empty -> safety block / no output
                return None
            parts = cands[0].get("content", {}).get("parts", [])
            txt = "".join(p.get("text", "") for p in parts).strip()
            return txt or None
        except HTTPError as e:
            last = e
            if e.code in (429, 503):            # rate-limited / loading -> back off
                time.sleep(pause * (attempt + 1) * 2)
                continue
            print(f"  ! gemini {e.code}: {e.reason}", file=sys.stderr)
            return None                         # bad key / bad request -> bail fast
        except (URLError, TimeoutError, ValueError) as e:
            last = e
            time.sleep(pause * (attempt + 1))
    print(f"  ! gemini failed: {last}", file=sys.stderr)
    return None


def load_summaries():
    """Load the accession -> summary cache (committed, persists across runs)."""
    try:
        with open(SUMMARIES_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return {}


def save_summaries(cache):
    os.makedirs(os.path.dirname(SUMMARIES_PATH), exist_ok=True)
    with open(SUMMARIES_PATH, "w", encoding="utf-8") as f:
        json.dump(cache, f, indent=2, ensure_ascii=False)


def summarize_fresh(items, cache, key):
    """Attach ai_summary to recent headline filings (cached by accession).

    Only the prominent headline filing per firm, filed within AI_MAX_DAYS, is
    summarized (<=1 call each), capped at MAX_AI_PER_RUN per run; the rest drain
    over later hourly runs via the cache. No key -> nothing happens (labels
    still show). Returns # new.
    """
    made = 0
    for it in items:
        f = it.get("filing")
        if not f:
            continue
        days = f.get("days_ago")
        if days is None or days > AI_MAX_DAYS:
            continue
        acc = f.get("accession")
        if not acc:
            continue
        if acc in cache:                        # already summarized -> reuse
            f["ai_summary"] = cache[acc]["summary"]
            continue
        if not key or made >= MAX_AI_PER_RUN:   # no key, or hit the per-run cap
            continue
        text = strip_html(get_text(f.get("doc_url")))
        summary = gemini_summarize(text, f.get("form", ""), key)
        if summary:
            f["ai_summary"] = summary
            cache[acc] = {"summary": summary, "model": GEMINI_MODEL}
            made += 1
            print(f"    ~ gemini {it['ticker']:6s} {f['form']:6s} -> {summary[:70]}")
            time.sleep(GEMINI_PAUSE)
    return made


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
    items_raw = recent.get("items", [""] * len(forms))[i]
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
        "summary": filing_summary(forms[i], items_raw),
        "accession": acc,                      # cache key for AI summaries
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
    gemini_key = os.environ.get("GEMINI_API_KEY")
    summaries = load_summaries()

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

    # AI prose layer: one-sentence summary on fresh headline filings, free via
    # Gemini, cached by accession. Falls back to item-code labels on any miss.
    made = summarize_fresh(items, summaries, gemini_key)
    if gemini_key:
        save_summaries(summaries)
        print(f"gemini: {made} new summaries, {len(summaries)} cached total")
    else:
        print("gemini: no GEMINI_API_KEY -> AI summaries skipped (labels only)")

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
