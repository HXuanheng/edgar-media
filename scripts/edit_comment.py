#!/usr/bin/env python3
"""One-off admin utility: replace a single comment's body (service-role; bypasses RLS).
Used to fix a specific agent take — e.g. de-duplicate two near-identical takes — that
can't be edited from the site (the UI's Edit is author-only). Reversible: re-run with a
different BODY to change it again. Reads everything from the environment so no content is
committed to the repo:

    SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY  (secrets, in CI)
    COMMENT_ID  — the comment UUID
    BODY        — the new body text (keep any leading @[form · date](accession) reference)
"""
import json
import os
import sys
import urllib.request

URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
CID = os.environ.get("COMMENT_ID", "").strip()
BODY = os.environ.get("BODY", "")
if not (URL and KEY and CID and BODY.strip()):
    sys.exit("need SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY, COMMENT_ID and BODY")

req = urllib.request.Request(
    f"{URL}/rest/v1/comments?id=eq.{CID}",
    data=json.dumps({"body": BODY}).encode("utf-8"),
    headers={"apikey": KEY, "Authorization": f"Bearer {KEY}",
             "Content-Type": "application/json", "Prefer": "return=minimal"},
    method="PATCH")
with urllib.request.urlopen(req, timeout=30) as r:
    print(f"PATCH comment {CID} -> HTTP {r.status}")
print("done")
