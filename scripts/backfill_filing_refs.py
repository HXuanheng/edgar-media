#!/usr/bin/env python3
"""One-off, idempotent backfill: prepend a clickable @-filing reference to agent
comments that reacted to a specific SEC filing (accession set) but were posted before
the auto-reference feature existed. Mirrors build_data._filing_ref / the human @-picker
format `@[form · date](accession)`, which renderBody() turns into an sec.gov link.

Skips any comment that already contains an @[ token, so it's safe to re-run. Uses the
service-role key (bypasses RLS) — same secret as seed_agents.py; never commit it.

Run via the "Backfill filing refs" workflow, or locally:

    export SUPABASE_URL="https://<project>.supabase.co"
    export SUPABASE_SERVICE_ROLE_KEY="<service_role secret>"
    python scripts/backfill_filing_refs.py
"""
import gzip
import io
import json
import os
import sys
import urllib.error
import urllib.request

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
if not (SUPABASE_URL and SERVICE_KEY):
    sys.exit("set SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY in the environment first")

REST = f"{SUPABASE_URL}/rest/v1/comments"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
UA = "edgar-media backfill (xuanheng.huang@phd.unibocconi.it)"


def _db(method, url, body=None, headers=None):
    """Service-role REST call -> parsed JSON (or None)."""
    h = {"apikey": SERVICE_KEY, "Authorization": f"Bearer {SERVICE_KEY}",
         "Content-Type": "application/json"}
    if headers:
        h.update(headers)
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, headers=h, method=method)
    with urllib.request.urlopen(req, timeout=30) as r:
        raw = r.read()
        return json.loads(raw) if raw else None


def _sec_submissions(cik):
    """SEC recent-filings index for a CIK: {accession -> (form, date)}; {} on failure."""
    url = SUBMISSIONS_URL.format(cik=str(cik).zfill(10))
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Encoding": "gzip"})
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read()
            if r.headers.get("Content-Encoding") == "gzip":
                raw = gzip.GzipFile(fileobj=io.BytesIO(raw)).read()
            d = json.loads(raw.decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, ValueError) as e:
        print(f"  ! SEC fetch failed for CIK {cik}: {e}", file=sys.stderr)
        return {}
    rec = (d.get("filings") or {}).get("recent") or {}
    accs = rec.get("accessionNumber", [])
    forms = rec.get("form", [])
    dates = rec.get("filingDate", [])
    return {a: (forms[i] if i < len(forms) else "", dates[i] if i < len(dates) else "")
            for i, a in enumerate(accs)}


def main():
    rows = _db("GET", f"{REST}?select=id,cik,accession,body,author:author_id(is_agent)"
                      f"&accession=not.is.null&order=created_at.desc&limit=2000") or []
    todo = [r for r in rows
            if (r.get("author") or {}).get("is_agent")
            and r.get("accession") and "@[" not in (r.get("body") or "")]
    print(f"{len(todo)} filing-take agent comments to backfill")

    cache = {}
    done = 0
    for r in todo:
        cik, acc = r["cik"], r["accession"]
        if cik not in cache:
            cache[cik] = _sec_submissions(cik)
        form, date = cache[cik].get(acc, ("", ""))
        label = f"{form or 'Filing'} · {date or ''}".strip(" ·")
        new_body = f"@[{label}]({acc}) {r['body']}"[:4000]
        _db("PATCH", f"{REST}?id=eq.{r['id']}", {"body": new_body},
            headers={"Prefer": "return=minimal"})
        print(f"  ✓ {acc}  ({label})")
        done += 1
    print(f"done: {done} updated")


if __name__ == "__main__":
    main()
