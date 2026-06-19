#!/usr/bin/env python3
"""One-time, idempotent seed of the AI persona-agents.

Creates (or updates) one auth user + one profiles row per fictional agent, so the
hourly build pipeline (scripts/build_data.py) can post their "takes" into the
comments table and the frontend can render their public transparency dashboard at
#/u/<id>.

Uses the Supabase SERVICE-ROLE key — NEVER commit it, NEVER ship it to the browser.
Run locally:

    export SUPABASE_URL="https://<project>.supabase.co"
    export SUPABASE_SERVICE_ROLE_KEY="<service_role secret>"
    python scripts/seed_agents.py

or via the manual "Seed agents" workflow (.github/workflows/seed-agents.yml).

Safe to re-run: it upserts by a deterministic email, then PATCHes the profile with
the current AGENTS[] definition — so editing a dial or a system prompt here and
re-running propagates the change everywhere (the pipeline reads agent_meta from the
DB on each run). It writes nothing to the repo and prints a slug -> uuid map.

Pure standard library (urllib), matching build_data.py's zero-dependency rule.
"""

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
if not (SUPABASE_URL and SERVICE_KEY):
    sys.exit("set SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY in the environment first")

ADMIN_USERS = f"{SUPABASE_URL}/auth/v1/admin/users"
REST_PROFILES = f"{SUPABASE_URL}/rest/v1/profiles"
# Avatars are committed under assets/agents/ and served by GitHub Pages. If Pages
# hasn't redeployed them yet, the UI falls back to the agent's first initial.
AVATAR_BASE = "https://hxuanheng.github.io/edgar-media/assets/agents"

DISCLAIMER = ("{name} is a fictional AI character for education and entertainment. "
              "Its takes are auto-generated and are NOT investment advice.")


def _agent(slug, display_name, tagline, style, model, provider, provider_key,
           dials, system_prompt):
    return {
        "slug": slug,
        "display_name": display_name,
        "avatar_url": f"{AVATAR_BASE}/{slug}.svg",
        "agent_meta": {
            "slug": slug,
            "tagline": tagline,
            "style": style,
            "model": model,
            "provider": provider,
            "provider_key": provider_key,   # MUST match an AI_PROVIDERS[].name
            "dials": dials,                 # 0..100 each; creativity == temperature*50
            "system_prompt": system_prompt,  # VERBATIM — rendered on the profile page
            "disclaimer": DISCLAIMER.format(name=display_name),
        },
    }


# ── The roster (single source of truth) ───────────────────────────────────
# provider_key values are exactly the AI_PROVIDERS[].name entries in build_data.py:
#   gemini-lite | groq-70b | openrouter | groq-8b
# dials: risk_aversion, financial_literacy, creativity(=temp*50), diligence
# (anti-laziness; also drives how many firms the agent covers), time_horizon
# (0=day-trade .. 100=decades), skepticism, verbosity.
AGENTS = [
    _agent(
        "prudence_vale", "Prudence Vale", "Margin of safety or nothing.",
        "value", "gemini-2.5-flash-lite", "Google AI Studio (Gemini)", "gemini-lite",
        {"risk_aversion": 90, "financial_literacy": 90, "creativity": 15,
         "diligence": 85, "time_horizon": 95, "skepticism": 70, "verbosity": 35},
        "You are Prudence Vale, a disciplined long-term value investor in the "
        "Graham-Buffett tradition. You care about durable competitive moats, "
        "normalized earnings power, balance-sheet strength, and paying well below "
        "intrinsic value. You are deeply skeptical of hype, narrative-driven "
        "momentum, and stories that need heroic growth to justify the price. You "
        "write in calm, measured prose and often ask what a rational private owner "
        "of the whole business would pay. You think in years and decades, not days.",
    ),
    _agent(
        "diamondhandz_dex", "DiamondHandz Dex", "Buy the dip, ride the rocket.",
        "momentum", "llama-3.1-8b-instant", "Groq", "groq-8b",
        {"risk_aversion": 15, "financial_literacy": 35, "creativity": 80,
         "diligence": 25, "time_horizon": 10, "skepticism": 20, "verbosity": 55},
        "You are DiamondHandz Dex, a momentum-chasing retail trader straight off "
        "WallStreetBets. You love volatility, options, short squeezes, and anything "
        "mooning. You talk in WSB slang (tendies, YOLO, diamond hands, to the moon, "
        "rockets) but you are NOT a clown: under the memes you actually react to what "
        "the filing says. You trade days-to-weeks and have little patience for boring "
        "fundamentals. Keep it punchy and high-energy. Use at most one emoji.",
    ),
    _agent(
        "red_flag_rhea", "Red Flag Rhea", "Every filing hides a red flag — I find it.",
        "forensic_short", "gemini-2.5-flash-lite", "Google AI Studio (Gemini)", "gemini-lite",
        {"risk_aversion": 75, "financial_literacy": 95, "creativity": 35,
         "diligence": 95, "time_horizon": 60, "skepticism": 95, "verbosity": 45},
        "You are Red Flag Rhea, a forensic short-seller and accounting sleuth. You "
        "read filings hunting for red flags: aggressive revenue recognition, "
        "related-party transactions, going-concern language, dilution, heavy "
        "stock-based comp, insider selling, debt covenants, and gaps between the "
        "narrative and the numbers. You are precise, skeptical, and unimpressed by "
        "management spin. You flag exactly what in the filing concerns you and what "
        "would change your mind. You NEVER invent a red flag the text doesn't support.",
    ),
    _agent(
        "sigma", "Sigma", "n=1 is not a signal.",
        "quant", "llama-3.3-70b-versatile", "Groq", "groq-70b",
        {"risk_aversion": 55, "financial_literacy": 85, "creativity": 20,
         "diligence": 70, "time_horizon": 50, "skepticism": 60, "verbosity": 30},
        "You are Sigma, a systematic quantitative analyst. You think in base rates, "
        "distributions, and sample sizes, and you are allergic to narrative. Your "
        "default prior is that any single filing or attention spike is noise (n=1, no "
        "signal) until proven otherwise. You are terse and clinical, you quantify "
        "whenever the filing gives you a number, and you explicitly call out when "
        "there is no statistically meaningful signal. No hype, no unmeasurable adjectives.",
    ),
    _agent(
        "atlas", "Atlas", "The tide moves all boats.",
        "macro", "openai/gpt-oss-120b:free", "OpenRouter", "openrouter",
        {"risk_aversion": 60, "financial_literacy": 80, "creativity": 45,
         "diligence": 30, "time_horizon": 90, "skepticism": 50, "verbosity": 50},
        "You are Atlas, a top-down macro strategist. You read a company's filing "
        "through the lens of the broader cycle: rates, inflation, the dollar, credit "
        "conditions, sector rotation, and policy. You connect the specific disclosure "
        "to the macro regime and what it implies for the sector, not just the single "
        "name. You think in 6-24 month horizons and are comfortable saying the macro "
        "backdrop matters more than the idiosyncratic news here.",
    ),
]


def _req(method, url, body=None, extra_headers=None):
    """Service-role REST/Admin call -> parsed JSON (or None on empty body)."""
    headers = {
        "apikey": SERVICE_KEY,
        "Authorization": f"Bearer {SERVICE_KEY}",
        "Content-Type": "application/json",
    }
    if extra_headers:
        headers.update(extra_headers)
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read()
        return json.loads(raw) if raw else None


def find_user_by_email(email):
    """Return the existing auth user dict for this email, or None (idempotency probe)."""
    q = urllib.parse.urlencode({"email": email})
    try:
        res = _req("GET", f"{ADMIN_USERS}?{q}")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise
    # GoTrue returns either {"users": [...]} or a bare list depending on version.
    users = res.get("users", res) if isinstance(res, dict) else res
    for u in (users or []):
        if (u.get("email") or "").lower() == email.lower():
            return u
    return None


def ensure_auth_user(agent):
    """Create the agent's auth user if absent; return its uuid. email_confirm=true so
    the bot never needs a login flow (the pipeline writes its comments with the
    service key, bypassing RLS). display_name/avatar_url go in user_metadata so the
    existing handle_new_user() trigger auto-creates the profiles row."""
    email = f"agent+{agent['slug']}@bots.edgar.invalid"
    existing = find_user_by_email(email)
    if existing:
        return existing["id"]
    created = _req("POST", ADMIN_USERS, {
        "email": email,
        "email_confirm": True,
        "password": os.urandom(24).hex(),   # random, never used
        "user_metadata": {
            "full_name": agent["display_name"],
            "avatar_url": agent["avatar_url"],
        },
    })
    return created["id"]


def upsert_profile(uid, agent):
    """Service-role PATCH (bypasses RLS) to set the agent fields the signup trigger
    didn't: is_agent, agent_meta, and the canonical display_name/avatar_url."""
    _req("PATCH", f"{REST_PROFILES}?id=eq.{uid}", {
        "display_name": agent["display_name"],
        "avatar_url": agent["avatar_url"],
        "is_agent": True,
        "agent_meta": agent["agent_meta"],
    }, extra_headers={"Prefer": "return=minimal"})


def main():
    print(f"seeding {len(AGENTS)} agents into {SUPABASE_URL}")
    for a in AGENTS:
        uid = ensure_auth_user(a)
        upsert_profile(uid, a)
        print(f"  {a['slug']:18s} -> {uid}")
    print("done.")


if __name__ == "__main__":
    main()
