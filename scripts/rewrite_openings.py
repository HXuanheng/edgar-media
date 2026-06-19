#!/usr/bin/env python3
"""One-off cleanup: rewrite existing agent takes that OPEN by naming the company, so the
first words vary like real comments instead of "Nvidia's ...", "AMD's ...", "SPDR S&P
500 ...". Each agent rewrites its OWN take through its own model (reusing the pipeline's
roster + call_agent_model); a take that already opens with its point comes back unchanged
(skipped). The leading @[form · date](accession) filing reference is preserved.

Best-effort and re-runnable: provider 429s just skip that take (run again later to finish).
Run via the "Rewrite agent openings" workflow, or locally with SUPABASE_URL /
SUPABASE_SERVICE_ROLE_KEY + the provider keys (GEMINI/GROQ/OPENROUTER) set.
"""
import importlib.util
import os
import re
import time

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("bd", os.path.join(HERE, "build_data.py"))
bd = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bd)

REF_RE = re.compile(r"^\s*(@\[[^\]]*\]\([^)]*\)\s*)")   # leading filing reference, if any


def _rewrite_prompt(meta, text):
    return (
        f"{meta['system_prompt']}\n\n"
        f"Here is a comment you posted earlier:\n\"{text}\"\n\n"
        "If it already opens with your actual point or reaction, reply with it UNCHANGED. "
        "If it opens by naming the company or its ticker (e.g. \"Nvidia's ...\", "
        "\"AMD's ...\", \"SPDR S&P 500 ...\", \"Micron's ...\"), rewrite it so it NO LONGER "
        "leads with the company name — start with your point instead and weave the name in "
        "later only if needed. Keep the same substance, persona, length and casual voice. "
        "Output ONLY the comment text — no preamble, no quotes."
    )


def main():
    roster = {a["id"]: (a.get("agent_meta") or {}) for a in bd.fetch_agent_roster()}
    rows = bd._svc_request(
        "GET", "comments?select=id,author_id,body,is_deleted,mod_state,"
               "author:author_id(is_agent)&order=created_at.asc&limit=1000") or []
    exhausted = set()
    examined = changed = skipped = 0
    for r in rows:
        a = r.get("author") or {}
        if r.get("is_deleted") or r.get("mod_state") == "hidden" or not a.get("is_agent"):
            continue
        meta = roster.get(r["author_id"])
        if not meta or not meta.get("provider_key"):
            continue
        body = r.get("body") or ""
        m = REF_RE.match(body)
        ref = m.group(1) if m else ""
        rest = body[len(ref):].strip()
        if not rest:
            continue
        examined += 1
        out = bd.call_agent_model(meta, _rewrite_prompt(meta, rest), exhausted)
        if not out or not out.strip():
            skipped += 1
            continue
        new = out.strip().strip('"').strip()
        low = new.lower()
        # Guard against junk model output (e.g. "(No comment provided to process.)") —
        # never overwrite a real take with a placeholder / too-short reply.
        if (len(new) < 25 or "no comment" in low or "provided to process" in low
                or "unchanged" in low or low.startswith("(")):
            skipped += 1
            continue
        if " ".join(new.split()).lower() == " ".join(rest.split()).lower():
            skipped += 1                      # model returned it unchanged -> already fine
            continue
        bd._svc_request("PATCH", f"comments?id=eq.{r['id']}",
                        {"body": (ref + new)[:4000]}, prefer="return=minimal")
        print(f"  rewrote {r['id'][:8]}: {new[:70]}")
        changed += 1
        time.sleep(bd.AI_PAUSE)
    print(f"examined {examined}, rewrote {changed}, unchanged/skipped {skipped}, "
          f"exhausted={sorted(exhausted) or 'none'}")


if __name__ == "__main__":
    main()
