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

# --- AI summary providers (all optional, all free) ------------------------
# One-sentence prose summary of FRESH headline filings. We try a chain of free
# providers in order; when one is out of its daily quota (HTTP 429) we fall
# through to the next, so their free allowances STACK. Cached by accession in
# data/summaries.json so each filing is summarized once ever. No keys set -> AI
# summaries skipped (item-code labels still show). Keys come from env vars
# (GitHub Actions secrets); a provider with no key is silently skipped.
#   GEMINI_API_KEY      -> Google AI Studio (free, no card)   aistudio.google.com
#   GROQ_API_KEY        -> Groq Console      (free, no card)   console.groq.com
#   OPENROUTER_API_KEY  -> OpenRouter        (free, no card)   openrouter.ai
# Tried top to bottom: good quality first, huge-quota overflow last. Free RPD
# figures are approximate and per-model; check each console for current limits.
# Groq's free tier is ~6000 tokens/min, so a big filing (a 10-K at 50k chars ~=
# 12k tokens) is rejected outright. "max_chars" restricts a provider to filings
# whose text fits: Groq is used only for short ones (most 8-Ks); longer filings
# skip it and fall through to providers with room.
GROQ_MAX_CHARS = 18000   # ~4.5k tokens, inside Groq's 6000 TPM free limit
AI_PROVIDERS = [
    {"name": "gemini-lite", "kind": "gemini", "key_env": "GEMINI_API_KEY",
     "model": "gemini-2.5-flash-lite"},                                # ~1000/day
    {"name": "groq-70b", "kind": "openai", "key_env": "GROQ_API_KEY",
     "url": "https://api.groq.com/openai/v1/chat/completions",
     "model": "llama-3.3-70b-versatile", "max_chars": GROQ_MAX_CHARS}, # ~1000/day, short only
    {"name": "openrouter", "kind": "openai", "key_env": "OPENROUTER_API_KEY",
     "url": "https://openrouter.ai/api/v1/chat/completions",
     "model": "openai/gpt-oss-120b:free"},                            # ~50/day (no card)
    {"name": "groq-8b", "kind": "openai", "key_env": "GROQ_API_KEY",
     "url": "https://api.groq.com/openai/v1/chat/completions",
     "model": "llama-3.1-8b-instant", "max_chars": GROQ_MAX_CHARS},    # ~14400/day overflow, short only
]
GEMINI_URL = ("https://generativelanguage.googleapis.com/v1beta/"
              "models/{model}:generateContent?key={key}")
AI_PAUSE = 4.0         # pause after each successful call; keeps us under RPM caps
GEMINI_MAX_CHARS = 50000  # filing text fed to the model (covers an 8-K fully)
AI_MAX_DAYS = int(os.environ.get("AI_MAX_DAYS", "10"))       # filings this recent
MAX_AI_PER_RUN = int(os.environ.get("MAX_AI_PER_RUN", "20"))  # cap new per run


def active_providers():
    """Providers whose API key env var is set, in chain order."""
    return [p for p in AI_PROVIDERS if os.environ.get(p["key_env"])]

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


def decode_filing(raw, content_type=""):
    """Decode filing bytes to text. SEC filings are usually UTF-8 but many are
    Windows-1252 (¥ £ € — and smart quotes), whose bytes are invalid UTF-8 and
    were turning into U+FFFD. Try the declared charset, then strict UTF-8, then
    cp1252; only mojibake-replace as a last resort."""
    charset = None
    if "charset=" in content_type.lower():
        charset = content_type.lower().split("charset=")[-1].split(";")[0].strip()
    for enc in (charset, "utf-8", "cp1252"):
        if not enc:
            continue
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", errors="replace")


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
                return decode_filing(raw, resp.headers.get("Content-Type", ""))
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


def _summary_prompt(text, form):
    return (f"Summarize this SEC {form} filing for a retail investor in ONE "
            f"factual sentence (max 25 words), no preamble:\n\n{text}")


def _gemini_call(model, prompt, key):
    """One Gemini generateContent call -> (text|None, 'ok'|'quota'|'fail')."""
    url = GEMINI_URL.format(model=model, key=key)
    body = json.dumps({
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "maxOutputTokens": 120,
            "temperature": 0.2,
            # 2.5 Flash(-Lite) is a thinking model; thinking tokens eat the
            # output budget, so disable thinking or the answer comes back empty.
            "thinkingConfig": {"thinkingBudget": 0},
        },
    }).encode("utf-8")
    try:
        req = Request(url, data=body,
                      headers={"Content-Type": "application/json"})
        with urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except HTTPError as e:
        return None, ("quota" if e.code == 429 else "fail")
    except (URLError, TimeoutError, ValueError):
        return None, "fail"
    cands = data.get("candidates")
    if not cands:                               # safety block / no output
        return None, "fail"
    parts = cands[0].get("content", {}).get("parts", [])
    txt = "".join(p.get("text", "") for p in parts).strip()
    return (txt or None), ("ok" if txt else "fail")


def _openai_call(url, model, prompt, key):
    """One OpenAI-compatible chat call (Groq, OpenRouter, ...) -> (text|None, status)."""
    body = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 120,
        "temperature": 0.2,
    }).encode("utf-8")
    try:
        req = Request(url, data=body, headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {key}",
        })
        with urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except HTTPError as e:
        return None, ("quota" if e.code == 429 else "fail")
    except (URLError, TimeoutError, ValueError):
        return None, "fail"
    try:
        txt = (data["choices"][0]["message"]["content"] or "").strip()
    except (KeyError, IndexError, TypeError):
        return None, "fail"
    return (txt or None), ("ok" if txt else "fail")


def ai_summarize(text, form, providers, exhausted):
    """One-sentence summary via the first provider with quota left. Mutates
    `exhausted` (set of provider names) so a 429'd provider is skipped for the
    rest of the run. Returns (summary|None, provider_name|None)."""
    if not text:
        return None, None
    prompt = _summary_prompt(text, form)
    for p in providers:
        if p["name"] in exhausted:
            continue
        key = os.environ.get(p["key_env"])
        if not key:
            continue
        if p.get("max_chars") and len(text) > p["max_chars"]:
            continue                            # filing too big for this provider's TPM
        if p["kind"] == "gemini":
            txt, status = _gemini_call(p["model"], prompt, key)
        else:
            txt, status = _openai_call(p["url"], p["model"], prompt, key)
        if status == "ok":
            return txt, p["name"]
        if status == "quota":                   # out of daily quota -> drop it
            exhausted.add(p["name"])
            print(f"    . {p['name']} hit 429 (quota) -> falling through",
                  file=sys.stderr)
        # 'fail' (transient/other) -> just try the next provider for this filing
    return None, None


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


def attach_summaries(items, cache):
    """Summarize each recent filing row (within AI_MAX_DAYS) and attach
    ai_summary to it. Cached by accession so a filing is summarized once ever;
    new ones capped at MAX_AI_PER_RUN per run, the rest drain over later runs.
    Verified-match firms only. Uses the free provider chain (active_providers);
    no keys -> only already-cached rows get a summary (labels still show on the
    rest). Stops early once every provider is out of quota. Returns # made.
    """
    providers = active_providers()
    exhausted = set()
    made = 0
    for it in items:
        if it.get("name_match") != "verified":
            continue                            # skip unverified-match firms
        for row in it.get("filings", []):
            acc = row.get("accession")
            if not acc:
                continue
            if (acc not in cache and providers       # not summarized yet
                    and len(exhausted) < len(providers)  # not all out of quota
                    and made < MAX_AI_PER_RUN):
                days = row.get("days_ago")
                if days is not None and days <= AI_MAX_DAYS:
                    text = strip_html(get_text(row.get("doc_url")))
                    summary, prov = ai_summarize(text, row.get("form", ""),
                                                 providers, exhausted)
                    if summary:
                        cache[acc] = {"summary": summary, "model": prov}
                        made += 1
                        print(f"    ~ {prov:11s} {it['ticker']:6s} {row['form']:6s}"
                              f" -> {summary[:60]}")
                        time.sleep(AI_PAUSE)
            if acc in cache:                    # attach (existing or just-made)
                row["ai_summary"] = cache[acc]["summary"]
    return made


def strip_doc_urls(items):
    """Drop doc_url from windowed rows before writing (kept only for fetching)."""
    for it in items:
        for row in it.get("filings", []):
            row.pop("doc_url", None)


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
        # Keep the full dict (incl. doc_url) so each row can be summarized;
        # doc_url is stripped from the output later to stay lean.
        windowed.append(dict(f))
        if len(windowed) >= MAX_FILINGS:
            break
    return headline, windowed


def merge_same_cik(items):
    """Collapse verified entries that share a CIK into one card. Dual-class
    tickers (GOOG/GOOGL, and the like) are the same issuer with identical SEC
    filings, so showing two cards is redundant. We sum their Reddit attention,
    keep every ticker (most-mentioned first), and recompute the 24h change.
    Non-verified or CIK-less rows (ETFs, mismatches) are left untouched.
    Output order follows each issuer's first appearance.
    """
    out = []
    by_cik = {}
    for it in items:
        cik = it.get("cik")
        if not cik or it.get("name_match") != "verified":
            out.append(it)
            continue
        base = by_cik.get(cik)
        if base is None:
            it["_tickers"] = [(it["ticker"], it.get("mentions", 0))]
            by_cik[cik] = it
            out.append(it)
            continue
        base["mentions"] += it.get("mentions", 0)
        base["mentions_24h_ago"] += it.get("mentions_24h_ago", 0)
        base["upvotes"] = base.get("upvotes", 0) + it.get("upvotes", 0)
        if it.get("rank") is not None:
            base["rank"] = (it["rank"] if base.get("rank") is None
                            else min(base["rank"], it["rank"]))
        base["_tickers"].append((it["ticker"], it.get("mentions", 0)))
    for it in by_cik.values():
        m0 = it["mentions_24h_ago"]
        it["mention_change_pct"] = (round((it["mentions"] - m0) / m0 * 100, 1)
                                    if m0 else None)
        tickers = [t for t, _ in sorted(it.pop("_tickers"),
                                        key=lambda x: x[1], reverse=True)]
        it["tickers"] = tickers          # all classes, most-mentioned first
        it["ticker"] = tickers[0]        # primary = most-mentioned class
    return out


def main():
    # Summaries can contain non-cp1252 chars (¥, €, narrow no-break space, smart
    # quotes); printing them to a Windows cp1252 console otherwise crashes the
    # run. GitHub Actions is already UTF-8. Make stdout/stderr never fail on print.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    now = datetime.now(timezone.utc)
    today = now.date()
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

    # Collapse dual-class tickers (GOOG/GOOGL) into one card per issuer (CIK).
    items = merge_same_cik(items)

    # AI prose layer: one-sentence summary per recent filing row, free via the
    # provider chain, cached by accession. Rows without one show the item label.
    made = attach_summaries(items, summaries)
    strip_doc_urls(items)
    provs = active_providers()
    if provs:
        save_summaries(summaries)
        names = ", ".join(p["key_env"].split("_")[0].lower()
                          for p in {p["key_env"]: p for p in provs}.values())
        print(f"AI: {made} new summaries ({names}), {len(summaries)} cached total")
    else:
        print("AI: no provider keys set -> summaries skipped (labels only)")

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
