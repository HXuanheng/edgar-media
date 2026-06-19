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
import hashlib
import html
import io
import json
import os
import random
import re
import sys
import threading
import time
import urllib.parse
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
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
# SEC/price request pacing is now enforced by the shared RateLimiter (see SEC_RATE
# / PRICE_RATE below) so the parallel fetch loops stay under SEC's 10 req/s.

# --- market price (free, no key; keyed provider optional) -----------------
# Last quote + day change + a 5-day sparkline per firm, fetched at build time
# (Yahoo/Stooq send no CORS headers, so a static page can't fetch them live).
# Tried in order; a keyed provider is skipped unless its env var is set (same
# rule as the AI chain below). Default path is keyless Yahoo -> Stooq. Setting
# TWELVEDATA_API_KEY (free, 800 req/day, no card -> twelvedata.com) promotes a
# contractually-supported API to primary with no code change -- the long-run
# escape hatch if a keyless endpoint ever dies for good. Reliability also comes
# from carry-forward (load_prev_prices): if every source is down for a ticker we
# reuse last run's price (shown as stale via its as_of), never a blank.
YAHOO_URL = ("https://query1.finance.yahoo.com/v8/finance/chart/"
             "{sym}?interval=1d&range=3mo")    # 3mo covers the 90-day filing window
STOOQ_URL = "https://stooq.com/q/d/l/?s={sym}.us&i=d"          # daily OHLCV CSV
TWELVEDATA_URL = ("https://api.twelvedata.com/time_series?symbol={sym}"
                  "&interval=1day&outputsize=70&apikey={key}")  # quote+spark+history
HISTORY_DAYS = 70      # trading days kept for the per-firm price chart (~3 months)
PRICE_PROVIDERS = [
    {"name": "twelvedata", "kind": "twelvedata", "key_env": "TWELVEDATA_API_KEY"},
    {"name": "yahoo", "kind": "yahoo"},
    {"name": "stooq", "kind": "stooq"},
]

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

# --- AI persona-agents (DB-backed, optional, free) ------------------------
# Fictional characters (value investor, momentum degen, short-seller, quant, macro)
# that post grounded "takes" into the Supabase comments table. They run AFTER the
# summaries above and consume only LEFTOVER free quota, so summaries (higher priority)
# are never starved: a small per-run cap bounds total agent LLM calls, and the same
# 429 -> exhausted-provider short-circuit applies. Enabled only when the service-role
# key is present (GitHub Actions secret); absent -> agents are skipped, build unaffected.
# Agent identities + personality dials + system prompts live in the profiles table
# (seeded by scripts/seed_agents.py) and are read back here at runtime.
SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
AGENTS_ENABLED = bool(SUPABASE_URL and SUPABASE_SERVICE_KEY)
MAX_AGENT_CALLS_PER_RUN = int(os.environ.get("MAX_AGENT_CALLS_PER_RUN", "12"))
# Tier 2 — agents also react to market moves, not just filings. Thresholds (tunable
# via env) for what counts as worth a "trend reaction" take on a firm with no fresh
# filing this run. Reaction posts are capped to one per agent/firm/day.
AGENT_PRICE_MOVE_PCT = float(os.environ.get("AGENT_PRICE_MOVE_PCT", "5"))   # |day move| %
AGENT_HYPE_SURGE_PCT = float(os.environ.get("AGENT_HYPE_SURGE_PCT", "50"))  # mention jump %
AGENT_HYPE_MIN_MENTIONS = int(os.environ.get("AGENT_HYPE_MIN_MENTIONS", "30"))
AGENT_HYPE_TOP_RANK = int(os.environ.get("AGENT_HYPE_TOP_RANK", "3"))       # rank <= this
# Tier 3 — agents converse like real users: reply to humans, remember their own takes,
# and post probabilistically so they trickle in rather than all firing at once.
MAX_AGENT_HUMAN_REPLIES = int(os.environ.get("MAX_AGENT_HUMAN_REPLIES", "3"))  # per run
AGENT_HUMAN_REPLY_HOURS = int(os.environ.get("AGENT_HUMAN_REPLY_HOURS", "8"))  # how fresh
AGENT_POST_PROB = float(os.environ.get("AGENT_POST_PROB", "0.7"))             # per-target dice


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


# --- concurrency ----------------------------------------------------------
# A small thread pool overlaps the network latency of the per-firm SEC/price
# fetches (those loops were sequential with fixed sleeps). A shared rate limiter
# keeps us under SEC's 10 req/s guidance ACROSS threads — same request count as
# before, just paced tightly instead of loosely, so it's both faster and no
# heavier on SEC. Pure stdlib (concurrent.futures + threading), no deps.
FETCH_WORKERS = 4
SEC_RATE = 9            # *.sec.gov requests/sec ceiling (SEC's documented limit is 10)
PRICE_RATE = 6         # price-provider requests/sec ceiling (Yahoo/Stooq, separate hosts)


class RateLimiter:
    """Thread-safe global rate cap: at most `rate` acquire()s per second across all
    threads. Each caller reserves the next time-slot under a brief lock, then sleeps
    OUTSIDE the lock, so threads pace evenly instead of bursting."""

    def __init__(self, rate):
        self._interval = 1.0 / rate
        self._lock = threading.Lock()
        self._next = 0.0

    def acquire(self):
        with self._lock:
            scheduled = max(time.monotonic(), self._next)
            self._next = scheduled + self._interval
        delay = scheduled - time.monotonic()
        if delay > 0:
            time.sleep(delay)


SEC_LIMITER = RateLimiter(SEC_RATE)
PRICE_LIMITER = RateLimiter(PRICE_RATE)


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


def _gemini_call(model, prompt, key, temperature=0.2, max_tokens=120):
    """One Gemini generateContent call -> (text|None, 'ok'|'quota'|'fail').
    temperature/max_tokens default to the summary settings; the persona-agent path
    passes per-character values."""
    url = GEMINI_URL.format(model=model, key=key)
    body = json.dumps({
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "maxOutputTokens": max_tokens,
            "temperature": temperature,
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


def _openai_call(url, model, prompt, key, temperature=0.2, max_tokens=120):
    """One OpenAI-compatible chat call (Groq, OpenRouter, ...) -> (text|None, status).
    temperature/max_tokens default to the summary settings; the persona-agent path
    passes per-character values."""
    body = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": temperature,
    }).encode("utf-8")
    try:
        req = Request(url, data=body, headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {key}",
            # Groq is behind Cloudflare, which 403s urllib's default "Python-urllib"
            # UA (error 1010). A real UA is required or the Groq agents never post.
            "User-Agent": UA,
        })
        with urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except HTTPError as e:
        if e.code != 429:                          # 429 is normal quota; surface the rest
            detail = ""
            try:
                detail = e.read()[:300].decode("utf-8", "replace")
            except Exception:
                pass
            print(f"    ! LLM {model} -> HTTP {e.code} {detail}", file=sys.stderr)
        return None, ("quota" if e.code == 429 else "fail")
    except (URLError, TimeoutError, ValueError) as e:
        print(f"    ! LLM {model} -> {type(e).__name__}: {e}", file=sys.stderr)
        return None, "fail"
    try:
        txt = (data["choices"][0]["message"]["content"] or "").strip()
    except (KeyError, IndexError, TypeError):
        print(f"    ! LLM {model} -> unexpected response shape", file=sys.stderr)
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


# ── AI persona-agents: DB-backed grounded takes ──────────────────────────
# These run AFTER attach_summaries on leftover free quota (summaries-first), and
# write into the Supabase comments table via the service-role key (bypasses RLS).
# Each agent is bound to ONE provider for transparency — we never silently swap a
# character's model — so if its provider is exhausted/failing the agent just skips.

def _svc_request(method, path, body=None, prefer=None):
    """Supabase REST call with the service-role key. Returns parsed JSON or None.
    Never raises (an agent failure must not kill the build)."""
    url = f"{SUPABASE_URL}/rest/v1/{path}"
    headers = {
        "apikey": SUPABASE_SERVICE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
        "Content-Type": "application/json",
    }
    if prefer:
        headers["Prefer"] = prefer
    data = json.dumps(body).encode("utf-8") if body is not None else None
    try:
        req = Request(url, data=data, headers=headers, method=method)
        with urlopen(req, timeout=30) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else None
    except HTTPError as e:
        detail = e.read()[:200].decode("utf-8", "replace") if hasattr(e, "read") else ""
        print(f"    ! supabase {method} {path.split('?')[0]} -> {e.code} {detail}",
              file=sys.stderr)
        return None
    except (URLError, TimeoutError, ValueError) as e:
        print(f"    ! supabase {method} {path.split('?')[0]} -> {e}", file=sys.stderr)
        return None


def fetch_agent_roster():
    """The seeded agents (profiles where is_agent), in stable id order."""
    rows = _svc_request(
        "GET", "profiles?select=id,display_name,agent_meta&is_agent=eq.true&order=id")
    return rows or []


def agent_has_take(author_id, cik, accession):
    """True if this agent already commented on this (firm, filing) — the dedup key,
    so a firm staying in trending with no new filing never gets a reposted take."""
    rows = _svc_request(
        "GET", f"comments?select=id&author_id=eq.{author_id}"
               f"&cik=eq.{cik}&accession=eq.{accession}&limit=1")
    return bool(rows)


def _headline_take_target(it):
    """For a firm's FRESH headline filing, resolve (acc, form, date, ai_summary).
    The headline dict (it['filing']) is a copy without ai_summary, so we pull the
    summary from the windowed it['filings'] row with the matching accession. Returns
    None unless there's a fresh headline that already has a grounded summary."""
    f = it.get("filing")
    if not (f and f.get("fresh") and f.get("accession")):
        return None
    acc = f["accession"]
    for row in it.get("filings", []):
        if row.get("accession") == acc and row.get("ai_summary"):
            return acc, f.get("form", "?"), f.get("date", "?"), row["ai_summary"]
    return None


# --- social / news buzz (free, keyless, best-effort) ----------------------
# Beyond Reddit (ApeWisdom): real news headlines (Google News RSS) + Twitter-style
# retail chatter & sentiment (Stocktwits), so agents react to the whole online
# conversation, not just one subreddit. Both are unofficial keyless endpoints, so
# every fetch is best-effort — any failure returns empty and the run continues on the
# sources that did answer (never aborts; no carry-forward — stale buzz isn't worth reusing).
GOOGLE_NEWS_RSS = "https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en"
STOCKTWITS_URL = "https://api.stocktwits.com/api/2/streams/symbol/{sym}.json"


def fetch_news(query, limit=4):
    """Recent news headlines for a firm via Google News RSS. Query by the company
    NAME (not the ticker) for relevant hits. Returns up to `limit` cleaned headline
    strings (newest first); [] on any failure."""
    if not query:
        return []
    url = GOOGLE_NEWS_RSS.format(q=urllib.parse.quote_plus(f"{query} stock"))
    xml = get_text(url, retries=2, pause=0.6)
    if not xml:
        return []
    try:                                  # bytes (not str) so the XML decl is accepted
        root = ET.fromstring(xml.encode("utf-8"))
    except ET.ParseError:
        return []
    out = []
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        if title:
            out.append(html.unescape(title))
        if len(out) >= limit:
            break
    return out


def fetch_stocktwits(ticker):
    """Twitter-style retail chatter for a ticker via Stocktwits' public stream:
    message count plus bull/bear sentiment tally and a couple of sample posts. Returns
    None on any failure (best-effort — this endpoint may throttle datacenter IPs)."""
    if not ticker:
        return None
    data = get_json(STOCKTWITS_URL.format(sym=ticker.upper()), retries=2, pause=0.6)
    msgs = (data or {}).get("messages") or []
    if not msgs:
        return None
    bull = bear = 0
    samples = []
    for m in msgs:
        basic = ((m.get("entities") or {}).get("sentiment") or {}).get("basic")
        if basic == "Bullish":
            bull += 1
        elif basic == "Bearish":
            bear += 1
        if len(samples) < 2:
            body = html.unescape(" ".join((m.get("body") or "").split()))
            if body:
                samples.append(body[:120])
    return {"count": len(msgs), "bullish": bull, "bearish": bear, "samples": samples}


def _filing_date_for(it, acc):
    """The filing DATE for an accession, so a take about it can anchor the price move to
    the filing day (not the latest session). Looks up it['filings']; falls back to the
    headline it['filing']. None when unknown."""
    if not acc:
        return None
    for f in (it.get("filings") or []):
        if f.get("accession") == acc:
            return f.get("date")
    fil = it.get("filing") or {}
    return fil.get("date") if fil.get("accession") == acc else None


def _filing_move_line(it, filing_date):
    """The price move ANCHORED TO A FILING instead of to the latest session: the close on
    the filing day (vs the prior session) plus the move since. Computed from the daily
    history so a take about a filing cites that filing's own market reaction, not whatever
    today's change happens to be. '' when there's no usable history for that date."""
    pr = it.get("price") or {}
    hist = pr.get("history") or []
    if not filing_date or len(hist) < 2:
        return ""
    # history is [date, close], oldest -> newest. The filing day is the first session on
    # or after the filing date (a filing on a weekend/holiday lands on the next session).
    idx = next((i for i, row in enumerate(hist)
                if isinstance(row, (list, tuple)) and len(row) == 2 and row[0] >= filing_date), None)
    if not idx:                        # None (older than history) or 0 (no prior close)
        return ""
    fdate, fclose = hist[idx]
    pclose = hist[idx - 1][1]
    last_date, last_close = hist[-1]
    if not all(isinstance(x, (int, float)) for x in (fclose, pclose, last_close)) \
            or pclose == 0 or fclose == 0:
        return ""
    day = (fclose - pclose) / pclose * 100
    line = (f"- Filing-day move ({fdate}): closed {fclose:.2f}, {day:+.1f}% vs the prior "
            f"session — use THIS for the filing's market reaction, NOT the latest-day "
            f"change above.")
    if last_date != fdate:
        since = (last_close - fclose) / fclose * 100
        line += f" The stock is {since:+.1f}% since the filing."
    return line


def _market_context(it, filing_date=None):
    """A compact 'what's happening right now' block — live price action plus the online
    buzz the pipeline has on the item (Reddit attention via ApeWisdom, recent news
    headlines, and Stocktwits chatter/sentiment). Lets agents react like real users (to
    a spike, a hype surge, or a fresh headline), not just to the filing text. Returns ''
    when there's nothing to show so the prompt stays clean."""
    lines = []
    pr = it.get("price")
    if pr and isinstance(pr.get("last"), (int, float)):
        chg = pr.get("change_pct")
        # Date-stamp the day change so it can never be mistaken for the move on the day of
        # a filing the take is about (which may be days earlier — see _filing_move_line).
        as_of = pr.get("as_of")
        when = f"on {as_of}" if as_of else "on the latest session"
        chg_s = f"{chg:+.1f}% {when}" if isinstance(chg, (int, float)) else when
        trend = ""
        spark = pr.get("spark") or []
        if len(spark) >= 2 and isinstance(spark[0], (int, float)):
            trend = " · 5-day trend up" if spark[-1] >= spark[0] else " · 5-day trend down"
        stale = " (price may be stale)" if pr.get("stale") else ""
        lines.append(f"- Price: {pr.get('currency','USD')} {pr['last']:.2f}, {chg_s}{trend}{stale}.")
        fmove = _filing_move_line(it, filing_date)   # filing-anchored move, when applicable
        if fmove:
            lines.append(fmove)
    buzz = []
    if it.get("rank") is not None:
        buzz.append(f"#{it['rank']} trending")
    if it.get("mentions") is not None:
        m = f"{it['mentions']} mentions"
        mcp = it.get("mention_change_pct")
        if isinstance(mcp, (int, float)):
            m += f" ({mcp:+.0f}% vs 24h ago)"
        buzz.append(m)
    if it.get("upvotes"):
        buzz.append(f"{it['upvotes']} upvotes")
    if buzz:
        lines.append("- Online buzz: " + ", ".join(buzz) + ".")
    soc = it.get("social")
    if soc and soc.get("count"):
        b, br = soc.get("bullish", 0), soc.get("bearish", 0)
        lean = "leaning bullish" if b > br else "leaning bearish" if br > b else "mixed"
        lines.append(f"- Stocktwits: {soc['count']} recent posts, {lean} ({b} bull / {br} bear).")
    news = it.get("news") or []
    if news:
        lines.append("- Recent headlines:\n" + "\n".join(f"  · {h}" for h in news[:4]))
    if not lines:
        return ""
    return "MARKET & BUZZ RIGHT NOW:\n" + "\n".join(lines) + "\n\n"


# Per-run cache of the recent discussion for a firm, so we fetch each thread once
# regardless of how many agents react to it. Cleared at the top of each agent run.
_THREAD_CACHE = {}


def thread_context(cik, limit=6):
    """The last few visible comments on this firm (humans + other agents), so an agent's
    take/reply actually responds to the conversation instead of talking into a void.
    Cached per run; returns '' when the thread is empty or unavailable."""
    if cik in _THREAD_CACHE:
        return _THREAD_CACHE[cik]
    rows = _svc_request(
        "GET", f"comments?select=body,is_deleted,mod_state,"
               f"author:author_id(display_name,is_agent)"
               f"&cik=eq.{cik}&order=created_at.desc&limit={limit}")
    lines = []
    for r in reversed(rows or []):                 # oldest -> newest for readability
        if r.get("is_deleted") or r.get("mod_state") == "hidden":
            continue
        a = r.get("author") or {}
        who = a.get("display_name") or "someone"
        tag = " [AI]" if a.get("is_agent") else ""
        body = " ".join((r.get("body") or "").split())
        if len(body) > 160:
            body = body[:160] + "…"
        if body:
            lines.append(f"- {who}{tag}: {body}")
    out = ("RECENT DISCUSSION ON THIS FIRM (oldest first):\n" + "\n".join(lines) + "\n\n"
           if lines else "")
    _THREAD_CACHE[cik] = out
    return out


_MEMORY_CACHE = {}     # author_id -> memory block, per run


def agent_memory(author_id, limit=3):
    """An agent's own most-recent takes, so it stays consistent and can reference its
    earlier calls ('like I said on $X...') instead of having amnesia each run. Cached
    per run; returns '' when the agent has no history yet."""
    if author_id in _MEMORY_CACHE:
        return _MEMORY_CACHE[author_id]
    rows = _svc_request(
        "GET", f"comments?select=body&author_id=eq.{author_id}"
               f"&order=created_at.desc&limit={limit}")
    bodies = [" ".join((r.get("body") or "").split())[:120] for r in (rows or []) if r.get("body")]
    out = ("YOUR RECENT TAKES (stay consistent with these; reference them if relevant, "
           "but don't repeat them verbatim):\n" + "\n".join(f"- {b}" for b in bodies) + "\n\n"
           if bodies else "")
    _MEMORY_CACHE[author_id] = out
    return out


def _voice_line(meta):
    """Per-persona tone instruction. High-creativity characters (e.g. the momentum
    degen) talk like real retail traders — casual, opinionated, speculative; the rest
    stay measured and analytical. The 'never fabricate specific numbers' guardrail is
    enforced separately in each prompt, so loosening tone never licenses made-up data."""
    if (meta.get("dials") or {}).get("creativity", 30) >= 60:
        return ("VOICE: talk like a casual retail trader posting online — punchy, "
                "informal, slang and an emoji or two are fine, strong opinions, and you "
                "may speculate about where the stock goes (hunches and vibes welcome).")
    return ("VOICE: stay measured and analytical in character — precise over flashy, "
            "but still sound like a real person, not a report.")


# Length shapes for a take, weighted by the agent's verbosity dial: terse agents
# mostly fire one-liners, wordier ones get room to breathe. random keeps the length
# varied run-to-run so takes don't all come out the same size.
_LENGTH_SHAPES = [
    "ONE punchy line — under 15 words",
    "1-2 quick sentences — under 35 words",
    "a few sentences — up to ~60 words",
]


def _length_target(meta):
    """Pick a length shape for this take. The verbosity dial (0..100) shifts the odds
    toward shorter (low) or longer (high); random gives natural run-to-run variation."""
    v = (meta.get("dials") or {}).get("verbosity", 50)
    if v <= 35:
        weights = (0.6, 0.3, 0.1)
    elif v >= 60:
        weights = (0.2, 0.4, 0.4)
    else:
        weights = (0.35, 0.4, 0.25)
    return random.choices(_LENGTH_SHAPES, weights=weights, k=1)[0]


def _format_guidance(meta):
    """Shared closing instructions for every agent take: variable length, a real-person
    social-media register, name-not-ticker, and the no-fabrication / no-boilerplate
    guardrails. Reused across all four prompt builders so the human tone stays uniform."""
    return (
        f"Length: {_length_target(meta)}. Don't pad to fill space.\n"
        "Write like a real person posting on social media: get straight to the point, "
        "casual, fragments and lowercase are fine. No throat-clearing, no press-release "
        "or analyst-report tone, no bullet lists. Do NOT open by naming the company or "
        "its ticker (everyone in the thread already knows which firm it is) — start with "
        "your actual point or reaction; weave the name in later only if you need it. "
        f"{_voice_line(meta)} "
        "Never invent specific figures you weren't given above; opinions, predictions "
        "and hot takes are fair game. If what you're looking at turns out to be a "
        "non-event, you usually don't need to say so out loud — a real person rarely "
        "posts just to point out that nothing happened; they talk about whatever they "
        "actually find interesting instead. No preamble, no greeting, no sign-off, no "
        "disclaimer (the site adds one)."
    )


def _reaction_reason(it):
    """Why a no-filing firm is worth reacting to right now (big price move or hype
    surge), as a short human phrase — or None if nothing notable. Price move wins over
    hype so the lead reason matches the strongest signal."""
    pr = it.get("price")
    if pr and isinstance(pr.get("change_pct"), (int, float)) and abs(pr["change_pct"]) >= AGENT_PRICE_MOVE_PCT:
        return f"the stock {'jumped' if pr['change_pct'] > 0 else 'dropped'} {pr['change_pct']:+.1f}% today"
    mcp = it.get("mention_change_pct")
    if (isinstance(mcp, (int, float)) and mcp >= AGENT_HYPE_SURGE_PCT
            and it.get("mentions", 0) >= AGENT_HYPE_MIN_MENTIONS):
        return f"social mentions just surged {mcp:+.0f}%"
    if it.get("rank") is not None and it["rank"] <= AGENT_HYPE_TOP_RANK:
        return f"it's blowing up at #{it['rank']} on the trending list"
    news = it.get("news") or []
    if news:
        return f'it\'s in the news: "{news[0][:80]}"'
    return None


def agent_reacted_today(author_id, cik):
    """True if this agent already posted a non-filing 'reaction' take on this firm today
    (accession is null) — caps trend reactions at one per agent/firm/day so a stock that
    stays hot all day doesn't get spammed."""
    since = datetime.now(timezone.utc).strftime("%Y-%m-%dT00:00:00")
    rows = _svc_request(
        "GET", f"comments?select=id&author_id=eq.{author_id}&cik=eq.{cik}"
               f"&accession=is.null&created_at=gte.{since}&limit=1")
    return bool(rows)


def _reaction_prompt(meta, it, reason, memory=""):
    """A take on a firm with NO fresh filing — pure reaction to the price move / hype."""
    name = it.get("display_name") or it.get("name") or it.get("ticker")
    return (
        f"{meta['system_prompt']}\n\n"
        f"You're scrolling the trending stocks and {name} catches your "
        f"eye because {reason}. There's no new SEC filing — you're reacting to the move "
        f"and the chatter.\n\n"
        f"{_market_context(it)}"
        f"{thread_context(it['cik'])}"
        f"{memory}"
        "Post your take in your own distinct voice reacting to what's happening.\n"
        f"{_format_guidance(meta)}"
    )


def _persona_prompt(meta, it, form, date, summary, memory=""):
    name = it.get("display_name") or it.get("name") or it.get("ticker")
    return (
        f"{meta['system_prompt']}\n\n"
        f"A fresh SEC filing for {name} just put it on your radar. "
        f"Filing: {form} on {date}.\n"
        f"OFFICIAL ONE-LINE SUMMARY OF THE FILING:\n{summary}\n\n"
        f"{_market_context(it, date)}"
        f"{thread_context(it['cik'])}"
        f"{memory}"
        "Post like a real person scrolling the feed: the filing is what caught your eye, "
        "not an assignment you have to review. It's already linked above your comment — "
        "do NOT recap or summarize it. If it genuinely moves your view, give YOUR "
        "angle/opinion on it (cite a specific figure only if it's essential to your "
        "point). If it's a routine non-event, don't dwell on it or announce that it's "
        "unimportant — talk about whatever you actually care about on this firm instead: "
        "the financials, the live price, the headlines, the online buzz, or your broader "
        "thesis. Don't invent numbers; if a fact you'd want isn't in the summary, say you "
        "don't see it rather than making it up. If nothing here is worth posting at all "
        "(routine filing AND no real angle in the price/news/buzz either), reply with just "
        "the single word SKIP and nothing else. Stay fully in character.\n"
        f"{_format_guidance(meta)}"
    )


def _debate_prompt(meta, root_name, root_body, it, form, summary, filing_date=None):
    name = it.get("display_name") or it.get("name") or it.get("ticker")
    if summary:                                   # filing-driven root take
        filing = f", {form} filing" if form else ""
        head = (f"Another AI analyst, {root_name}, just posted this take on {name}"
                f"{filing}:\n\"{root_body}\"\n\n"
                f"OFFICIAL ONE-LINE SUMMARY OF THE FILING:\n{summary}\n\n")
    else:                                         # reaction root take (no filing)
        head = (f"Another AI analyst, {root_name}, just posted this take on "
                f"{name}:\n\"{root_body}\"\n\n")
    return (
        f"{meta['system_prompt']}\n\n"
        f"{head}"
        f"{_market_context(it, filing_date if summary else None)}"
        f"Reply to {root_name} in your own distinct voice — agree, push back, or add "
        "your angle. Don't repeat what they already said and don't recap the filing "
        "(it's linked); add something new. Ground factual claims in the summary or the "
        f"price/buzz above; if a fact isn't there, say so rather than inventing it. Stay "
        f"in character.\n"
        f"{_format_guidance(meta)}"
    )


def call_agent_model(meta, prompt, exhausted):
    """One LLM call through the agent's assigned provider, at its creativity
    temperature. Mutates `exhausted` on a 429. Returns the take text or None."""
    pkey = meta.get("provider_key")
    p = next((x for x in AI_PROVIDERS if x["name"] == pkey), None)
    if not p or p["name"] in exhausted:
        return None
    key = os.environ.get(p["key_env"])
    if not key:
        return None
    temp = max(0.0, min(2.0, (meta.get("dials") or {}).get("creativity", 30) / 50.0))
    if p["kind"] == "gemini":
        txt, status = _gemini_call(p["model"], prompt, key, temperature=temp, max_tokens=200)
    else:
        txt, status = _openai_call(p["url"], p["model"], prompt, key,
                                   temperature=temp, max_tokens=200)
    if status == "quota":
        exhausted.add(p["name"])
        print(f"    . agent provider {p['name']} hit 429 (quota) -> skipping",
              file=sys.stderr)
        return None
    return txt


def _filing_ref(form, date, acc):
    """An inline @-mention token the frontend renders as a clickable SEC filing link:
    renderBody() turns @[label](accession) into an sec.gov <a>. Built deterministically
    from the known accession (same `form · date` label the human @-picker uses) — never
    asked of the model, which would mangle the long accession id and break the link."""
    label = f"{form or 'Filing'} · {date or ''}".strip(" ·")
    return f"@[{label}]({acc})"


def post_agent_comment(author_id, cik, accession, body, parent_id=None):
    """Insert an agent comment (service-role; bypasses RLS). Returns the new id."""
    row = _svc_request("POST", "comments", {
        "cik": cik, "accession": accession, "author_id": author_id,
        "parent_id": parent_id, "body": body,
    }, prefer="return=representation")
    if isinstance(row, list) and row:
        # Invalidate this firm's cached discussion so the NEXT agent in the same run
        # sees this just-posted take (and doesn't duplicate it).
        _THREAD_CACHE.pop(cik, None)
        return row[0].get("id")
    return None


def agent_upvote(author_id, comment_id):
    """Register an agent's 'like' on an existing comment (service-role). The votes PK is
    (comment_id, user_id), so resolution=ignore-duplicates makes a repeat a harmless
    no-op. Returns True on success."""
    if not comment_id:
        return False
    _svc_request("POST", "votes", {"comment_id": comment_id, "user_id": author_id},
                 prefer="resolution=ignore-duplicates,return=minimal")
    return True


def recent_takes(cik, limit=6):
    """Fresh (uncached) list of the visible comments on a firm as
    {id, author, is_agent, body} — used to show an agent what's already been said this
    cycle (with stable indices) so it can like/extend instead of echoing. Newest last."""
    rows = _svc_request(
        "GET", f"comments?select=id,body,is_deleted,mod_state,parent_id,"
               f"author:author_id(display_name,is_agent)"
               f"&cik=eq.{cik}&parent_id=is.null&order=created_at.desc&limit={limit}")
    out = []
    for r in reversed(rows or []):                 # oldest -> newest
        if r.get("is_deleted") or r.get("mod_state") == "hidden":
            continue
        body = " ".join((r.get("body") or "").split())
        if not body:
            continue
        a = r.get("author") or {}
        out.append({"id": r["id"], "author": a.get("display_name") or "someone",
                    "is_agent": bool(a.get("is_agent")), "body": body})
    return out


def _join_prompt(meta, it, takes, summary="", memory="", filing_date=None):
    """Used when other takes already exist on this firm: the agent JOINS the thread like a
    real user — likes one it agrees with (optionally adding ONE new point), or posts a
    genuinely different take — instead of echoing what's already there. `takes` is the few
    most-recent root takes (see recent_takes); the agent need not read the whole thread.
    `summary` is the filing's one-line summary (for filing threads) so the joining agent
    stays ON TOPIC — without it, agents free-style a take unrelated to the document."""
    name = it.get("display_name") or it.get("name") or it.get("ticker")
    listing = "\n".join(
        f"[{i + 1}] {t['author']}{' [AI]' if t['is_agent'] else ''}: {t['body'][:200]}"
        for i, t in enumerate(takes))
    filing_block = (f"WHAT THE FILING SAYS (it's already linked above — react to THIS, "
                    f"don't recap it):\n{summary}\n\n" if summary else "")
    return (
        f"{meta['system_prompt']}\n\n"
        f"You're reading the recent discussion on {name}. People already posted:\n"
        f"{listing}\n\n"
        f"{filing_block}"
        f"{_market_context(it, filing_date if summary else None)}"
        f"{memory}"
        "Act like a real user joining the thread. Reply with EXACTLY ONE of these on the "
        "first line:\n"
        "  LIKE n            — you basically agree with take n and have nothing to add\n"
        "  LIKE n | <point>  — you agree with take n but add ONE genuinely new point\n"
        "  NEW | <take>      — you have a DIFFERENT angle nobody above has voiced\n"
        "Rules: stay ON the topic of the filing/discussion above — don't drift to an "
        "unrelated thesis; don't repeat a point already made; don't summarize the filing "
        "(it's linked); only choose NEW if your angle is genuinely different. The text "
        "after the | is your actual comment — write it in your own voice per the style "
        "below.\n"
        f"{_format_guidance(meta)}"
    )


def _is_skip(text):
    """True if an agent declined to post — it replied with just SKIP (a routine filing
    with no angle worth a take). Tight match so a real take that merely contains the word
    'skip' is never swallowed: the whole reply, minus surrounding punctuation/brackets,
    must be exactly SKIP."""
    s = (text or "").strip().strip("[](){}.!\"' ").upper()
    return s == "SKIP"


def _parse_join(text, n):
    """Parse a _join_prompt reply -> ('like', idx0, add_text) | ('new', None, take_text).
    Lenient: a malformed or out-of-range reply falls back to a fresh NEW take with the
    whole text, so we never crash and never post an empty comment."""
    s = (text or "").strip()
    m = re.match(r"^\s*LIKE\s+(\d+)\s*(?:\|\s*(.*))?$", s, re.IGNORECASE | re.DOTALL)
    if m and 1 <= int(m.group(1)) <= n:
        return ("like", int(m.group(1)) - 1, (m.group(2) or "").strip())
    m = re.match(r"^\s*NEW\s*\|\s*(.*)$", s, re.IGNORECASE | re.DOTALL)
    if m and m.group(1).strip():
        return ("new", None, m.group(1).strip())
    return ("new", None, s)


def maybe_post_debate(roster, posted_roots, exhausted, budget):
    """One scripted reply per run: a DIFFERENT persona replies to a take posted this
    run (one-level threading), to spark a debate. For each take (hottest first) we try
    candidate responders in a contrarian-first order, skipping any that authored the
    take, are out of quota, or already weighed in on this filing (dedup). The first
    viable responder posts; returns # replies posted (0 or 1)."""
    if not posted_roots or budget <= 0:
        return 0
    by_slug = {(a.get("agent_meta") or {}).get("slug"): a for a in roster}
    # Contrarian voices first — they make the liveliest rebuttal — then the rest.
    order = ["red_flag_rhea", "sigma", "prudence_vale", "atlas", "diamondhandz_dex"]
    ranked = [by_slug[s] for s in order if s in by_slug]
    ranked += [a for a in roster if a not in ranked]
    for root_agent, it, parent_id, root_body, form, summary, acc in posted_roots:
        cik = it["cik"]
        for responder in ranked:
            if responder["id"] == root_agent["id"]:
                continue
            rmeta = responder.get("agent_meta") or {}
            pkey = rmeta.get("provider_key")
            if not pkey or pkey in exhausted:
                continue
            # Dedup: skip if this responder already weighed in on this filing (acc set)
            # or already reacted to this firm today (reaction root, acc is null).
            if (agent_has_take(responder["id"], cik, acc) if acc
                    else agent_reacted_today(responder["id"], cik)):
                continue
            prompt = _debate_prompt(rmeta, root_agent.get("display_name", "another analyst"),
                                    root_body, it, form, summary,
                                    filing_date=_filing_date_for(it, acc))
            text = call_agent_model(rmeta, prompt, exhausted)
            if not text:
                continue
            body = text.strip()[:4000]
            if not body:
                continue
            if post_agent_comment(responder["id"], cik, acc, body, parent_id=parent_id):
                print(f"    @ {rmeta.get('slug','?'):16s} (reply to "
                      f"{(root_agent.get('agent_meta') or {}).get('slug','?')}) "
                      f"{it['ticker']:6s} -> {body[:46]}")
                time.sleep(AI_PAUSE)
                return 1
    return 0


def _human_reply_prompt(meta, it, human_name, human_body, memory=""):
    """An agent replying directly to a human's comment on a firm."""
    name = it.get("display_name") or it.get("name") or it.get("ticker")
    return (
        f"{meta['system_prompt']}\n\n"
        f"A user named {human_name} commented on {name}:\n"
        f"\"{human_body}\"\n\n"
        f"{_market_context(it)}"
        f"{thread_context(it['cik'])}"
        f"{memory}"
        f"Reply directly to {human_name} in your own distinct voice — engage with what "
        "they actually said: agree, push back, or add your angle.\n"
        f"{_format_guidance(meta)}"
    )


def fetch_recent_human_comments(ciks, hours, limit=25):
    """Recent, visible, human-authored comments on firms we're covering this run — the
    candidates an agent might jump in on. Newest first."""
    if not ciks:
        return []
    since = (datetime.now(timezone.utc).timestamp() - hours * 3600)
    since_iso = datetime.fromtimestamp(since, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    rows = _svc_request(
        "GET", f"comments?select=id,cik,accession,body,parent_id,is_deleted,mod_state,"
               f"author:author_id(display_name,is_agent)"
               f"&created_at=gte.{since_iso}&order=created_at.desc&limit={limit}")
    cikset = set(ciks)
    out = []
    for r in rows or []:
        a = r.get("author") or {}
        if a.get("is_agent") or r.get("is_deleted") or r.get("mod_state") == "hidden":
            continue
        if r.get("cik") in cikset and (r.get("body") or "").strip():
            out.append(r)
    return out


def agent_already_replied(comment_id):
    """True if any agent has already replied to this comment — so multiple agents don't
    dogpile a single human comment in one run."""
    rows = _svc_request(
        "GET", f"comments?select=author:author_id(is_agent)&parent_id=eq.{comment_id}")
    return any((r.get("author") or {}).get("is_agent") for r in (rows or []))


def maybe_reply_to_humans(roster, items_by_cik, exhausted, budget):
    """Agents engage with real users: pick recent human comments on firms we cover and
    have one fitting agent reply to each (root comments first). Capped per run, shares
    the LLM budget, and skips any human comment an agent already answered. The DB depth
    cap rejects replies that would nest too deep (handled gracefully). Returns # posted."""
    if budget <= 0 or not items_by_cik:
        return 0
    cands = fetch_recent_human_comments(list(items_by_cik), AGENT_HUMAN_REPLY_HOURS)
    if not cands:
        return 0
    cands.sort(key=lambda r: r.get("parent_id") is not None)   # roots before replies
    order = list(roster)
    random.shuffle(order)
    made = 0
    for hc in cands:
        if made >= MAX_AGENT_HUMAN_REPLIES or budget <= 0:
            break
        it = items_by_cik.get(hc["cik"])
        if not it or agent_already_replied(hc["id"]):
            continue
        human_name = (hc.get("author") or {}).get("display_name") or "a user"
        human_body = " ".join((hc.get("body") or "").split())[:500]
        for responder in order:
            rmeta = responder.get("agent_meta") or {}
            pkey = rmeta.get("provider_key")
            if not pkey or pkey in exhausted:
                continue
            prompt = _human_reply_prompt(rmeta, it, human_name, human_body,
                                         agent_memory(responder["id"]))
            text = call_agent_model(rmeta, prompt, exhausted)
            if not text:
                continue        # transient/empty — let another agent try this comment
            body = text.strip()[:4000]
            if not body:
                continue
            if post_agent_comment(responder["id"], hc["cik"],
                                   hc.get("accession"), body, parent_id=hc["id"]):
                print(f"    > {rmeta.get('slug','?'):16s} (reply to {human_name}) "
                      f"{it['ticker']:6s} -> {body[:46]}")
                made += 1
                budget -= 1
                time.sleep(AI_PAUSE)
            break        # one agent reply per human comment (rejected -> don't waste more calls)
    return made


def generate_agent_takes(items, now):
    """Post persona takes to Supabase. Two kinds of root take:
      - FILING takes: grounded in a fresh, summarized headline filing (dedup by
        (agent, cik, accession) so a lingering filing is never reposted).
      - REACTION takes: on a firm with no fresh filing but a big price move / hype
        surge (accession null; dedup to one per agent/firm/day).
    Quota-disciplined so summaries (run first) are never starved: a fresh exhausted
    set, a shared MAX_AGENT_CALLS_PER_RUN budget, and per-agent coverage scaled by
    the agent's diligence dial. Filings take precedence; reactions fill leftover."""
    _THREAD_CACHE.clear()        # fresh discussion snapshot each run
    _MEMORY_CACHE.clear()        # fresh per-agent memory each run
    roster = fetch_agent_roster()
    if not roster:
        print("agents: no agent profiles seeded (run scripts/seed_agents.py)")
        return

    verified = [it for it in items
                if it.get("name_match") == "verified" and it.get("cik")]
    items_by_cik = {it["cik"]: it for it in verified}

    # Filing-driven targets (grounded in the fresh filing summary).
    filing_targets = {}   # cik -> (it, acc, form, date, summary)
    for it in verified:
        tgt = _headline_take_target(it)
        if tgt:
            filing_targets[it["cik"]] = (it, *tgt)

    # Reaction targets: notable move/hype on a firm WITHOUT a fresh filing this run.
    reaction_targets = []   # (it, reason)
    for it in verified:
        if it["cik"] in filing_targets:
            continue          # a filing take already covers this firm
        reason = _reaction_reason(it)
        if reason:
            reaction_targets.append((it, reason))

    # Unified target list: (kind, it, acc, form, ctx) — ctx is the filing summary or
    # the reaction reason. Hottest (lowest Reddit rank) first.
    targets = [("filing", it, acc, form, summary)
               for (it, acc, form, date, summary) in filing_targets.values()]
    targets += [("reaction", it, None, "", reason) for (it, reason) in reaction_targets]
    targets.sort(key=lambda t: t[1]["rank"] if t[1].get("rank") is not None else 999)
    if not targets:
        print("agents: no eligible firms (no fresh filing, price move, or hype surge)")
        return

    exhausted = set()
    budget = MAX_AGENT_CALLS_PER_RUN
    per_agent_base = max(1, MAX_AGENT_CALLS_PER_RUN // max(1, len(roster)))
    made = 0
    posted_roots = []   # (agent, it, comment_id, body, form, ctx, acc)

    roster_order = list(roster)
    random.shuffle(roster_order)        # vary who gets first pick so takes don't clump
    for agent in roster_order:
        meta = agent.get("agent_meta") or {}
        pkey = meta.get("provider_key")
        if not pkey or pkey in exhausted:
            continue
        mem = agent_memory(agent["id"])
        diligence = (meta.get("dials") or {}).get("diligence", 50)
        quota_n = max(0, round(diligence / 100 * (per_agent_base + 1)))
        for kind, it, acc, form, ctx in targets:
            if budget <= 0 or quota_n <= 0:
                break
            # Probabilistic skip so agents trickle in (don't blanket every firm at once).
            if random.random() > AGENT_POST_PROB:
                continue
            cik = it["cik"]
            # Same-agent dedup: never repeat yourself on the same filing / same firm/day.
            if kind == "filing":
                if agent_has_take(agent["id"], cik, acc):
                    continue
            elif agent_reacted_today(agent["id"], cik):
                continue

            # If others already weighed in on this firm, JOIN the thread (like / add / new)
            # like a real user instead of echoing them; otherwise post the first take.
            # Only the few most-recent roots are shown (recent_takes) — no need to read all.
            existing = [t for t in recent_takes(cik)
                        if t["author"] != agent.get("display_name")]
            if existing:
                # Pass the filing summary so a joining agent reacts to the actual document
                # (a board change, say), not a free-styled unrelated thesis.
                prompt = _join_prompt(meta, it, existing,
                                      ctx if kind == "filing" else "", mem,
                                      filing_date=it["filing"].get("date") if kind == "filing" else None)
            elif kind == "filing":
                prompt = _persona_prompt(meta, it, form, it["filing"].get("date", "?"), ctx, mem)
            else:
                prompt = _reaction_prompt(meta, it, ctx, mem)

            text = call_agent_model(meta, prompt, exhausted)
            if pkey in exhausted:
                break            # provider out of quota -> stop this agent
            if not text or not text.strip():
                continue
            budget -= 1          # a model call was spent (whether it likes, posts, or skips)
            quota_n -= 1
            if _is_skip(text):   # agent looked, judged it a non-event, declined to post
                print(f"    . {meta.get('slug','?'):16s} {it['ticker']:6s} "
                      f"{(form or 'trend'):6s} -> skip")
                continue

            # Joining: like an existing take (optionally + a reply), or fall through to a
            # genuinely new root take.
            if existing:
                action, idx, extra = _parse_join(text, len(existing))
                if action == "like":
                    liked = existing[idx]
                    agent_upvote(agent["id"], liked["id"])
                    made += 1
                    if extra:    # agree, and add ONE new point as a reply
                        reply = extra[:4000]
                        post_agent_comment(agent["id"], cik,
                                           acc if kind == "filing" else None, reply,
                                           parent_id=liked["id"])
                        print(f"    + {meta.get('slug','?'):16s} {it['ticker']:6s} "
                              f"like+reply -> {reply[:50]}")
                    else:
                        print(f"    ^ {meta.get('slug','?'):16s} {it['ticker']:6s} "
                              f"liked {liked['author']}")
                    time.sleep(AI_PAUSE)
                    continue
                body = extra[:4000]          # action == "new"
            else:
                body = text.strip()[:4000]
            if not body:
                continue

            # Filing takes lead with a clickable @-reference to the document they react
            # to (built from the known accession, not the model). posted_roots keeps the
            # clean body so a debate quote isn't cluttered with the token.
            post_body = body
            if kind == "filing" and acc:
                ref = _filing_ref(form, it["filing"].get("date", "?"), acc)
                post_body = f"{ref} {body}"[:4000]
            cid = post_agent_comment(agent["id"], cik, acc, post_body)
            if cid:
                posted_roots.append((agent, it, cid, body, form, ctx if kind == "filing" else "", acc))
                made += 1
                print(f"    @ {meta.get('slug','?'):16s} {it['ticker']:6s} "
                      f"{(form or 'trend'):6s} -> {body[:60]}")
                time.sleep(AI_PAUSE)

    n_debate = maybe_post_debate(roster, posted_roots, exhausted, budget)
    budget -= n_debate
    n_human = maybe_reply_to_humans(roster, items_by_cik, exhausted, budget)
    made += n_debate + n_human
    print(f"agents: {made} new takes posted "
          f"({n_debate} agent debate, {n_human} human replies; "
          f"{', '.join(sorted(exhausted)) or 'no provider exhaustion'})")


def strip_doc_urls(items):
    """Drop doc_url from windowed rows before writing (kept only for fetching)."""
    for it in items:
        for row in it.get("filings", []):
            row.pop("doc_url", None)


# --- market price ---------------------------------------------------------
def _price_yahoo(ticker):
    """Yahoo v8 chart (free, no key). near-real-time price + 5-day closes."""
    data = get_json(YAHOO_URL.format(sym=ticker.upper()))
    try:
        res = data["chart"]["result"][0]
        meta = res["meta"]
        last = meta.get("regularMarketPrice")
        if last is None:
            return None
        raw = res["indicators"]["quote"][0].get("close", [])
        stamps = res.get("timestamp", []) or []
        # Dated daily closes for the per-firm chart. Yahoo's timestamp[] aligns
        # 1:1 with the raw close[] (nulls = market holidays) -> zip then drop nulls.
        history = []
        for t, c in zip(stamps, raw):
            if c is None:
                continue
            d = datetime.fromtimestamp(t, timezone.utc).date().isoformat()
            history.append([d, round(c, 2)])
        history = history[-HISTORY_DAYS:]
        # Day change = last vs the PRIOR session's close. Yahoo's meta previousClose/
        # chartPreviousClose are the close *before the chart range starts* (~3 months
        # ago with range=3mo), so take yesterday's close from the series itself.
        prev = (history[-2][1] if len(history) >= 2
                else meta.get("previousClose") or meta.get("chartPreviousClose"))
        if not prev:
            return None
        closes = [c for c in raw if c is not None]
        spark = [round(c, 2) for c in closes][-5:]
        if not spark or spark[-1] != round(last, 2):   # ensure latest point shows
            spark = (spark + [round(last, 2)])[-5:]
        ts = meta.get("regularMarketTime")
        as_of = (datetime.fromtimestamp(ts, timezone.utc).date().isoformat()
                 if ts else None)
        return {
            "last": round(last, 2),
            "prev_close": round(prev, 2),
            "change_pct": round((last - prev) / prev * 100, 2),
            "currency": meta.get("currency") or "USD",
            "spark": spark,
            "history": history,
            "source": "yahoo",
            "as_of": as_of,
            # Transient identity (stripped before the price object is stored):
            # Yahoo is a free third source for security type + name, used by
            # reconcile_identity to classify ETFs and adjudicate name matches.
            "_quote_type": meta.get("instrumentType"),
            "_yahoo_name": meta.get("longName") or meta.get("shortName"),
        }
    except (KeyError, IndexError, TypeError, ZeroDivisionError):
        return None


def _price_stooq(ticker):
    """Stooq daily CSV (free, no key). EOD close; stable fallback for Yahoo."""
    csv = get_text(STOOQ_URL.format(sym=ticker.lower()))
    if not csv:
        return None
    rows = [r for r in csv.strip().splitlines() if r]
    if len(rows) < 2 or not rows[0].lower().startswith("date"):
        return None                                    # "No data" page etc.
    data = []
    for r in rows[1:]:                                  # Date,Open,High,Low,Close,Vol
        parts = r.split(",")
        if len(parts) < 5:
            continue
        try:
            data.append([parts[0], float(parts[4])])
        except ValueError:
            continue
    if not data:
        return None
    history = [[d, round(c, 2)] for d, c in data[-HISTORY_DAYS:]]
    closes = [c for _, c in data]
    last_date = data[-1][0]
    last = closes[-1]
    prev = closes[-2] if len(closes) >= 2 else last
    return {
        "last": round(last, 2),
        "prev_close": round(prev, 2),
        "change_pct": round((last - prev) / prev * 100, 2) if prev else 0.0,
        "currency": "USD",
        "spark": [round(c, 2) for c in closes[-5:]],
        "history": history,
        "source": "stooq",
        "as_of": last_date,
    }


def _price_twelvedata(ticker, key):
    """Twelve Data time_series (sanctioned API, free key). One call yields the
    latest close, the prior close, and the 5-day sparkline (newest first)."""
    data = get_json(TWELVEDATA_URL.format(sym=ticker.upper(), key=key))
    try:
        if not data or data.get("status") == "error":
            return None
        vals = data.get("values") or []
        closes = [float(v["close"]) for v in vals if v.get("close")]
        if not closes:
            return None
        last = closes[0]
        prev = closes[1] if len(closes) > 1 else last
        # values are newest-first -> reverse to oldest->newest for the chart.
        hist_pairs = [[(v.get("datetime") or "")[:10], round(float(v["close"]), 2)]
                      for v in vals if v.get("close") and v.get("datetime")]
        history = list(reversed(hist_pairs))[-HISTORY_DAYS:]
        return {
            "last": round(last, 2),
            "prev_close": round(prev, 2),
            "change_pct": round((last - prev) / prev * 100, 2) if prev else 0.0,
            "currency": (data.get("meta") or {}).get("currency") or "USD",
            "spark": [round(c, 2) for c in reversed(closes[:5])],  # oldest->newest
            "history": history,
            "source": "twelvedata",
            "as_of": (vals[0].get("datetime") or "")[:10] or None,
        }
    except (KeyError, IndexError, ValueError, TypeError, ZeroDivisionError):
        return None


def fetch_price(ticker, prev):
    """Price object for a ticker, walking PRICE_PROVIDERS in order. Keyed
    providers are skipped unless their env var is set (like active_providers).
    If every live source fails, reuse last run's price (carry-forward) so an
    outage degrades to a stale quote, never a blank. Never raises."""
    for p in PRICE_PROVIDERS:
        env = p.get("key_env")
        key = os.environ.get(env) if env else None
        if env and not key:
            continue                                   # keyed provider, no key
        PRICE_LIMITER.acquire()                        # global <=PRICE_RATE/s across threads
        try:
            kind = p["kind"]
            if kind == "yahoo":
                got = _price_yahoo(ticker)
            elif kind == "stooq":
                got = _price_stooq(ticker)
            elif kind == "twelvedata":
                got = _price_twelvedata(ticker, key)
            else:
                got = None
        except Exception as e:                         # a quote must never kill the run
            print(f"  ! price {p['name']} {ticker}: {e}", file=sys.stderr)
            got = None
        if got:
            return got
    return prev.get(ticker)                             # carry-forward (or None)


def load_prev_prices():
    """ticker -> last run's price object, for carry-forward when every live
    source is down. Reads the committed trending.json; missing/corrupt -> {}.
    Keyed by every class so dual-class issuers carry forward either way."""
    try:
        with open(OUT_PATH, encoding="utf-8") as f:
            prev = json.load(f)
    except (FileNotFoundError, ValueError):
        return {}
    out = {}
    for it in prev.get("items", []):
        pr = it.get("price")
        if not pr:
            continue
        for t in (it.get("tickers") or [it.get("ticker")]):
            if t:
                out[t] = pr
    return out


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
    SEC_LIMITER.acquire()
    data = get_json(SUBMISSIONS_URL.format(cik=cik))
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


# --- identity reconciliation (Yahoo as a free third source) ---------------
# Yahoo's chart response (fetched with the price) carries the security type and
# its real name. We use it to (1) classify ETFs/funds and (2) break ties when
# the ApeWisdom name and the SEC name disagree -- the two-source matcher can't
# tell which side is wrong, a third source can.
FUND_TYPES = {"ETF", "MUTUALFUND", "INDEX"}
FUND_NAME_HINTS = ("etf", "etn", "proshares", "direxion", "ishares", "vanguard",
                   "spdr", "invesco qqq", "global x", "roundhill")


def looks_like_fund(item):
    """ETF/fund? Trust Yahoo's instrumentType when present; else fall back to
    strong issuer keywords in the (ApeWisdom) name."""
    qt = (item.get("quote_type") or "").upper()
    if qt:
        return qt in FUND_TYPES        # Yahoo knows -> authoritative
    name = (item.get("name") or "").lower()
    return any(h in name for h in FUND_NAME_HINTS)


def reconcile_identity(items):
    """Tag funds and resolve 'mismatch' name matches using Yahoo's name as a
    tiebreaker (set transiently as _yahoo_name during the price fetch)."""
    for it in items:
        it["is_fund"] = looks_like_fund(it)
        yn = it.pop("_yahoo_name", None)          # transient; never persisted
        if it.get("name_match") != "mismatch":
            continue
        if yn and it.get("sec_name") and name_similarity(yn, it["sec_name"]):
            it["name_match"] = "verified"          # SEC corroborated by Yahoo
        elif yn and name_similarity(yn, it.get("name") or ""):
            it["name_match"] = "wrong_cik"         # SEC CIK is the odd one out
            it["filing"] = None                    # don't show an unrelated filing
            it["filings"] = []


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


# --- XBRL financials (SEC companyfacts) -----------------------------------
# A compact, curated subset of each firm's audited annual figures, extracted at
# build time from SEC's free XBRL `companyfacts` API and written one small file
# per firm to data/financials/CIK{cik}.json. The static client lazy-loads only
# the firm it's showing (the raw companyfacts JSON is 1-15 MB; the extract is
# ~10 KB). This is the "data behind the filing/buzz" for the company page.
COMPANYFACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
FIN_DIR = os.path.join(os.path.dirname(__file__), "..", "data", "financials")
FIN_YEARS = 5          # most-recent fiscal years kept per line item
FIN_SCHEMA = 3         # bump when the concept map / extraction logic changes -> forces
                       # a one-time refresh of every firm's file even if its accession
                       # is unchanged (otherwise the accession-gate would skip the fix)
FIN_ENABLED = os.environ.get("FIN_ENABLED", "1") == "1"   # 0 -> skip the big fetches locally

# Curated line items. Each row = (label, kind, unit, [tag chain]). Tags are
# tried in order; the first one carrying annual data wins (filers tag the same
# concept differently). kind selects the annual frame (above); unit is the
# companyfacts units bucket ("USD", or "USD/shares" for per-share figures).
FIN_STATEMENTS = [
    ("income", [
        ("Revenue", "duration", "USD",
         ["Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax",
          "RevenueFromContractWithCustomerIncludingAssessedTax",
          "RegulatedAndUnregulatedOperatingRevenue",   # utilities (e.g. DTE)
          "SalesRevenueNet"]),
        ("Cost of revenue", "duration", "USD",
         ["CostOfRevenue", "CostOfGoodsAndServicesSold"]),
        ("Gross profit", "duration", "USD", ["GrossProfit"]),
        ("Operating income", "duration", "USD", ["OperatingIncomeLoss"]),
        ("R&D expense", "duration", "USD", ["ResearchAndDevelopmentExpense"]),
        ("Net income", "duration", "USD", ["NetIncomeLoss", "ProfitLoss"]),
        ("Diluted EPS", "duration", "USD/shares", ["EarningsPerShareDiluted"]),
    ]),
    ("balance", [
        ("Total assets", "instant", "USD", ["Assets"]),
        ("Current assets", "instant", "USD", ["AssetsCurrent"]),
        ("Total liabilities", "instant", "USD", ["Liabilities"]),
        ("Current liabilities", "instant", "USD", ["LiabilitiesCurrent"]),
        # Prefer TOTAL equity (incl. noncontrolling interest) so Assets = Liabilities
        # + Equity ties for firms with minority interests (Tesla, etc.); fall back to
        # parent-only where the total isn't separately tagged (most firms: identical).
        ("Stockholders' equity", "instant", "USD",
         ["StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
          "StockholdersEquity"]),
        ("Cash & equivalents", "instant", "USD",
         ["CashAndCashEquivalentsAtCarryingValue"]),
    ]),
    ("cashflow", [
        ("Operating cash flow", "duration", "USD",
         ["NetCashProvidedByUsedInOperatingActivities"]),
        ("Investing cash flow", "duration", "USD",
         ["NetCashProvidedByUsedInInvestingActivities"]),
        ("Financing cash flow", "duration", "USD",
         ["NetCashProvidedByUsedInFinancingActivities"]),
        ("Capital expenditure", "duration", "USD",
         ["PaymentsToAcquirePropertyPlantAndEquipment"]),
    ]),
]

# Canonical line order per statement, so a derived row lands in its proper place.
CANON_ORDER = {key: [lbl for (lbl, _k, _u, _t) in rows] for key, rows in FIN_STATEMENTS}


def _derive_income(rows):
    """Robustness against a single missing tag: fill gaps in the income statement
    via exact accounting identities, never overwriting a reported value. Creates a
    line if it's wholly absent but derivable.
        Gross profit     = Revenue - Cost of revenue
        Cost of revenue  = Revenue - Gross profit
    Both are definitional, so a filer that reports any two of the three yields the
    third for free."""
    by = {r["label"]: r for r in rows}

    def get(label):
        return by[label]["values"] if label in by else None

    def ensure(label):
        if label not in by:
            by[label] = {"label": label, "unit": "USD", "values": {}}
            rows.append(by[label])
        return by[label]["values"]

    rev, cost = get("Revenue"), get("Cost of revenue")
    if rev is not None and cost is not None:
        gp = ensure("Gross profit")
        for y in set(rev) & set(cost):
            gp.setdefault(y, rev[y] - cost[y])
    rev, gp = get("Revenue"), get("Gross profit")
    if rev is not None and gp is not None:
        cost = ensure("Cost of revenue")
        for y in set(rev) & set(gp):
            cost.setdefault(y, rev[y] - gp[y])


def _is_annual(start, end):
    """True if [start, end] spans roughly a fiscal year (~330-400 days) -> used to
    keep full-year durations and drop quarterly / YTD-partial facts in the fallback."""
    try:
        d0 = datetime.strptime(start, "%Y-%m-%d")
        d1 = datetime.strptime(end, "%Y-%m-%d")
    except (ValueError, TypeError):
        return False
    return 330 <= (d1 - d0).days <= 400


def annual_values(facts_list, kind):
    """One concept's fact list -> {fiscal_year:int -> value} for annual figures.

    Keyed by the period-END year, which IS the fiscal-year label for essentially
    every US filer (calendar-year filers and off-cycle ones alike: Apple FY2024
    ends Sep-2024, Microsoft FY2024 ends Jun-2024, Marvell FY2025 ends Feb-2025 ->
    all labelled by the end year). We deliberately ignore the XBRL `frame` (CYxxxx):
    it is calendar-based and mislabels non-December filers, which produced shifted
    and DUPLICATED years (a Marvell close of 2025-02-01 landing as both 2024 via the
    frame and 2025 via the end date). One consistent key removes that whole class
    of bug.

    Annual = a fact whose fiscal period is the full year (fp == 'FY'); for durations
    we also require a ~365-day span to drop the rare FY-tagged transition stub. A
    10-K's prior-year comparatives are also fp=='FY', so they correctly populate
    their own end-years. Facts are oldest-first, so a later restatement overwrites
    an earlier value; a 10-K value is never overridden by a non-10-K one (e.g. an
    8-K earnings release furnishing preliminary numbers)."""
    out, have_10k = {}, set()
    for f in facts_list:
        val, end, fp = f.get("val"), f.get("end"), f.get("fp")
        if val is None or not end or fp != "FY":
            continue
        if kind == "duration" and not _is_annual(f.get("start"), end):
            continue
        try:
            year = int(end[:4])
        except (ValueError, TypeError):
            continue
        is_10k = str(f.get("form", "")).startswith("10-K")
        if year in have_10k and not is_10k:
            continue                                   # keep the audited 10-K value
        out[year] = val
        if is_10k:
            have_10k.add(year)
    return out


def extract_financials(facts, cik, ticker, now):
    """companyfacts payload -> compact financials dict, or None if no usable
    us-gaap annual data (foreign ifrs-full filers, funds, empty)."""
    gaap = (facts or {}).get("facts", {}).get("us-gaap")
    if not gaap:
        return None
    statements, years = {}, set()
    for key, rows in FIN_STATEMENTS:
        out_rows = []
        for label, kind, unit, tags in rows:
            # Merge the whole tag chain by year rather than taking the first tag
            # with any data: filers switch tags across eras (e.g. MSFT used
            # `Revenues` pre-2018, `RevenueFromContractWithCustomerExcludingAssessedTax`
            # after), so one tag often holds only stale years. Union covers all
            # years; the primary (first-listed) tag wins on the rare overlap.
            values = {}
            for tag in tags:
                units = gaap.get(tag, {}).get("units", {}).get(unit)
                if not units:
                    continue
                for yr, val in annual_values(units, kind).items():
                    values.setdefault(yr, val)
            if values:
                out_rows.append({"label": label, "unit": unit, "values": values})
        if key == "income":
            _derive_income(out_rows)               # fill gaps via accounting identities
        # Keep the canonical line order (derivation may have appended a created row).
        order = CANON_ORDER[key]
        out_rows.sort(key=lambda r: order.index(r["label"]) if r["label"] in order else 99)
        for r in out_rows:
            years.update(r["values"])
        if out_rows:
            statements[key] = out_rows
    if not statements:
        return None
    fiscal_years = sorted(years, reverse=True)[:FIN_YEARS]
    keep = set(fiscal_years)
    # Trim each row to the kept years (stringify year keys for JSON); drop rows and
    # statements left empty (a concept only reported outside the recent window).
    for skey in list(statements):
        trimmed = []
        for row in statements[skey]:
            row["values"] = {str(y): v for y, v in row["values"].items() if y in keep}
            if row["values"]:
                trimmed.append(row)
        if trimmed:
            statements[skey] = trimmed
        else:
            del statements[skey]
    if not statements:
        return None
    return {
        "cik": cik,
        "ticker": ticker,
        "currency": "USD",
        "schema": FIN_SCHEMA,
        "fiscal_years": fiscal_years,
        "updated": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": "SEC EDGAR XBRL companyfacts",
        "statements": statements,
    }


def _fin_path(cik):
    return os.path.join(FIN_DIR, f"CIK{cik}.json")


def load_existing_fin(cik):
    try:
        with open(_fin_path(cik), encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return None


def _fin_hash(fin):
    """Hash of the meaningful content only (excludes `updated`) so a timestamp-only
    diff never triggers a rewrite/commit. Includes based_on_accession so a fresh
    10-Q that didn't move the curated numbers still records the new trigger (and is
    skipped on the next run)."""
    payload = {"schema": fin.get("schema"),
               "fiscal_years": fin.get("fiscal_years"),
               "statements": fin.get("statements"),
               "based_on_accession": fin.get("based_on_accession")}
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


# Reports that update fundamentals -> a new one is our cue to refetch companyfacts.
REPORT_FORMS = ("10-K", "10-Q", "10-K/A", "10-Q/A")


def latest_report_accession(it):
    """Accession of the firm's most recent 10-K/10-Q (it['filings'] is newest-first),
    or None if none is in the window. Comes free from the submissions we already
    fetched for the trending list — no extra request."""
    for f in it.get("filings") or []:
        if f.get("form") in REPORT_FORMS and f.get("accession"):
            return f["accession"]
    return None


def build_financials(items, now):
    """Write a compact data/financials/CIK{cik}.json per verified, non-fund firm.

    Light by default: companyfacts is 0.3-1 MB gzipped per firm, but fundamentals
    only move on a new 10-K/10-Q. So we SKIP the fetch entirely unless the firm's
    latest 10-K/10-Q accession changed since we last built its file — the accession
    comes free from the submissions already pulled for the trending list. Most hours
    that means zero companyfacts downloads; a small burst only around earnings.
    Foreign/funds/no-facts firms simply get no file (404 client-side)."""
    if not FIN_ENABLED:
        print("financials: disabled (FIN_ENABLED=0)")
        return
    os.makedirs(FIN_DIR, exist_ok=True)
    written = unchanged = skipped = nofacts = 0
    seen = set()
    jobs = []                                          # (cik, ticker, latest_acc, prev)
    for it in items:
        cik = it.get("cik")
        if not cik or cik in seen:
            continue
        if it.get("is_fund") or it.get("name_match") != "verified":
            continue
        seen.add(cik)
        prev = load_existing_fin(cik)
        latest_acc = latest_report_accession(it)
        # Already have a CURRENT-schema file and no new report since -> no possible
        # change, no fetch. (latest_acc None = no 10-K/10-Q in window = nothing new.)
        # A schema bump forces a refetch so extractor fixes reach existing files.
        if (prev is not None and prev.get("schema") == FIN_SCHEMA
                and (latest_acc is None
                     or prev.get("based_on_accession") == latest_acc)):
            skipped += 1
            continue
        jobs.append((cik, it.get("ticker"), latest_acc, prev))

    def fetch_one(job):
        cik, ticker, latest_acc, prev = job
        SEC_LIMITER.acquire()
        facts = get_json(COMPANYFACTS_URL.format(cik=cik))
        try:
            fin = extract_financials(facts, cik, ticker, now) if facts else None
        except Exception as e:                         # bad XBRL must never kill the run
            print(f"  ! financials {ticker}: {e}", file=sys.stderr)
            fin = None
        return cik, latest_acc, prev, fin

    # Fetch (the slow, network-bound part) in parallel; write + count single-threaded.
    with ThreadPoolExecutor(max_workers=FETCH_WORKERS) as pool:
        for cik, latest_acc, prev, fin in pool.map(fetch_one, jobs):
            if not fin:
                nofacts += 1
                continue
            fin["based_on_accession"] = latest_acc
            if prev and _fin_hash(prev) == _fin_hash(fin):
                unchanged += 1
                continue
            with open(_fin_path(cik), "w", encoding="utf-8") as f:
                json.dump(fin, f, indent=2, ensure_ascii=False)
            written += 1
    print(f"financials: {written} written, {unchanged} unchanged, "
          f"{skipped} skipped (no new report), {nofacts} no us-gaap")


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

    def build_item(row):
        """Build one firm's item dict (incl. its submissions fetch). Pure per-row:
        reads the shared read-only cik_map and writes only its own dict, so it's
        safe to run across threads. Returns (item, log_line) so the caller can print
        progress in input order instead of interleaved."""
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
            log = (f"  {ticker:6s} cik={cik} {name_match:8s} "
                   f"filing={filing['form'] if filing else '-'} "
                   f"(+{extra} in {WINDOW_DAYS}d)")
        else:
            log = f"  {ticker:6s} (no SEC match - ETF/crypto/foreign)"

        return {
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
        }, log

    # Fetch submissions concurrently (rate-limited under SEC's 10 req/s); pool.map
    # preserves input order, so items + log lines stay in ApeWisdom rank order.
    items = []
    with ThreadPoolExecutor(max_workers=FETCH_WORKERS) as pool:
        for item, log in pool.map(build_item, results):
            print(log)
            items.append(item)

    # Collapse dual-class tickers (GOOG/GOOGL) into one card per issuer (CIK).
    items = merge_same_cik(items)

    # Market price: last quote + day change + 5-day sparkline, fetched per
    # displayed card (after the merge so a dual-class issuer is fetched once, by
    # its primary class). Free + keyless (Yahoo -> Stooq); carry-forward reuses
    # last run's price when every source is down, so an outage shows as stale,
    # never blank.
    print("fetching market prices (Yahoo -> Stooq, carry-forward on outage)...")
    prev_prices = load_prev_prices()

    def price_item(it):
        """Fetch + attach one firm's price (writes only its own dict). Returns True
        if the quote was carried forward from last run (every live source down)."""
        pr = fetch_price(it["ticker"], prev_prices)
        if not pr:
            return False
        carried_now = pr is prev_prices.get(it["ticker"])
        if pr.get("_quote_type"):                  # Yahoo identity -> onto the item
            it["quote_type"] = pr["_quote_type"]
        if pr.get("_yahoo_name"):
            it["_yahoo_name"] = pr["_yahoo_name"]   # transient (reconcile pops it)
        pr = {k: v for k, v in pr.items() if not k.startswith("_")}  # strip + copy
        if carried_now:
            pr["stale"] = True                     # sources down -> last known
        it["price"] = pr
        return carried_now

    with ThreadPoolExecutor(max_workers=FETCH_WORKERS) as pool:
        carried = sum(1 for c in pool.map(price_item, items) if c)
    priced = sum(1 for x in items if x.get("price"))
    print(f"prices: {priced}/{len(items)} priced, "
          f"{carried} carried forward (live sources unavailable)")

    # Reconcile identity with Yahoo (type + name): tag ETFs and resolve any
    # name mismatches the ApeWisdom-vs-SEC comparison couldn't adjudicate.
    reconcile_identity(items)
    funds = sum(1 for x in items if x.get("is_fund"))
    print(f"identity: {funds} funds tagged, "
          f"{sum(1 for x in items if x['name_match'] == 'wrong_cik')} wrong-cik, "
          f"{sum(1 for x in items if x['name_match'] == 'mismatch')} still unverified")

    # Social/news buzz beyond Reddit: real headlines (Google News) + Stocktwits
    # chatter/sentiment, attached per displayed card. Best-effort and keyless — runs
    # after identity reconcile so news is queried by the final company name; a card
    # simply carries no news/social field when its source(s) didn't answer.
    print("fetching news + social buzz (Google News RSS + Stocktwits, best-effort)...")

    def buzz_item(it):
        q = it.get("display_name") or it.get("name") or it.get("ticker")
        news = fetch_news(q)
        if news:
            it["news"] = news
        social = fetch_stocktwits(it.get("ticker"))
        if social:
            it["social"] = social
        return bool(news), bool(social)

    with ThreadPoolExecutor(max_workers=FETCH_WORKERS) as pool:
        bz = list(pool.map(buzz_item, items))
    print(f"buzz: {sum(1 for n, _ in bz if n)}/{len(items)} with news, "
          f"{sum(1 for _, s in bz if s)}/{len(items)} with stocktwits")

    # XBRL financials: one compact data/financials/CIK*.json per verified firm,
    # lazy-loaded by the company page's Financials tab. Runs after identity is
    # final (funds tagged, dual-class merged -> one fetch per CIK).
    build_financials(items, now)

    # AI prose layer: one-sentence summary per recent filing row, free via the
    # provider chain, cached by accession. Rows without one show the item label.
    made = attach_summaries(items, summaries)

    # AI persona-agents: DB-backed grounded takes, AFTER summaries so they only use
    # leftover free quota and never starve the (higher-priority) summary feature.
    # Runs BEFORE strip_doc_urls only matters if agents re-fetched filing text; they
    # ground purely on the cached ai_summary, so order is flexible. Wrapped so an
    # agent/Supabase failure can never abort the trending build.
    if AGENTS_ENABLED:
        try:
            generate_agent_takes(items, now)
        except Exception as e:                  # noqa: BLE001 - never kill the build
            print(f"agents: aborted ({e})", file=sys.stderr)
    else:
        print("agents: disabled (set SUPABASE_URL + SUPABASE_SERVICE_ROLE_KEY to enable)")

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
            "news": "Google News RSS (~hourly, best-effort)",
            "social": "Stocktwits messages + sentiment (~hourly, best-effort)",
            "filings": "SEC EDGAR submissions API (real-time)",
            "price": "Yahoo Finance / Stooq (~hourly, carry-forward on outage)",
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
