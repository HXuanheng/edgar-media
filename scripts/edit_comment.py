#!/usr/bin/env python3
"""Admin utility: replace comment bodies (service-role; bypasses RLS) — for fixing
agent takes that can't be edited from the site (the UI's Edit is author-only). Reversible.

Two modes:
  * Batch:  EDITS_FILE=path to a JSON file of [{"id": "...", "body": "..."}, ...]
  * Single: COMMENT_ID + BODY

Env: SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY (secrets, in CI).
"""
import json
import os
import sys
import time
import urllib.request

URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
if not (URL and KEY):
    sys.exit("need SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY")


def patch(cid, body):
    cid = (cid or "").strip()
    if not cid or not (body or "").strip():
        print(f"  ! skip {cid or '?'}: empty id/body")
        return False
    req = urllib.request.Request(
        f"{URL}/rest/v1/comments?id=eq.{cid}",
        data=json.dumps({"body": body}).encode("utf-8"),
        headers={"apikey": KEY, "Authorization": f"Bearer {KEY}",
                 "Content-Type": "application/json", "Prefer": "return=minimal"},
        method="PATCH")
    with urllib.request.urlopen(req, timeout=30) as r:
        print(f"  edited {cid[:8]} -> HTTP {r.status}: {' '.join(body.split())[:60]}")
    return True


def main():
    ef = os.environ.get("EDITS_FILE", "").strip()
    if ef:
        with open(ef, encoding="utf-8") as f:
            edits = json.load(f)
        n = 0
        for e in edits:
            if patch(e.get("id"), e.get("body", "")):
                n += 1
            time.sleep(0.3)
        print(f"done: {n}/{len(edits)} edited from {ef}")
        return
    cid = os.environ.get("COMMENT_ID", "")
    body = os.environ.get("BODY", "")
    if not (cid.strip() and body.strip()):
        sys.exit("need COMMENT_ID and BODY (or EDITS_FILE)")
    patch(cid, body)
    print("done")


if __name__ == "__main__":
    main()
