# EDGAR Media — TODO / Roadmap

Running list so we don't repeat ourselves between sessions. Newest ideas at top.

---

## 🤖 AI persona-agents that comment on firms  (next big feature)

**Goal:** AI characters with distinct personalities + investment styles post takes
under each trending firm. They react to the *same* filing + attention spike
differently → debate. This is the site's strongest originality lever (Yahoo Finance
would never do this).

**Personas (distinct lenses, archetypes):**
- Value investor — moats, valuation, hype-skeptic
- WSB momentum degen — YOLO, options, "to the moon"
- Forensic short-seller — hunts red flags in the filing
- Quant — base rates, "n=1, no signal"
- (optional) Macro / sector top-down

**Implementation routes (static GitHub Pages + Supabase — site can't hold API keys):**
- **(a) Build-time — START HERE.** In `scripts/build_data.py`, per top-N firm call the
  Claude API once per persona, store takes in `data/trending.json` (or a new
  `data/agents.json`). Cheap, cacheable, fits the existing hourly pipeline.
- **(b) Runtime — later upgrade.** Serverless fn (Supabase Edge Function / Cloudflare
  Worker) holds the key, generates on demand, lets agents reply to human comments.

**UX:** render as special comments in the existing thread — bot badge + persona
avatar + a one-line style tag. Let agents reply to each other (scripted mini-debate).
Keep them visually distinct + collapsible so they don't drown real users.

**Model:** Claude Haiku 4.5 for cost at scale (e.g. 10 firms × 4 personas hourly = 40
calls); Sonnet/Opus for a few richer voices. (Check `/claude-api` skill for current
model ids + pricing before building.)

**Must-haves / guardrails:**
- Ground every take in the real filing text — reuse the existing `ai_summary`. Instruct
  agents to say "I don't see that in the filing" rather than invent numbers.
- Loud, persistent **"fictional characters — entertainment/education, NOT investment
  advice"** disclaimer. Legal + ethical.
- Only regenerate a firm's takes when the filing / mentions materially change (cost).

**Open decisions to make before coding:**
- [ ] Build-time (a) or runtime (b) first?
- [ ] Which personas, and how many per firm?
- [ ] How many firms get agent takes (all, or top-N)?

---

## ✨ Originality polish (cheap wins)

- [ ] **Synthesize the "why it's popping" in one line per card** — join the three data
  points we already have: `mentions +X% · filed <FORM> <when> · price ±Y%`
  (e.g. "$X mentions +1000%, filed 8-K yesterday, up 12%"). This causal synthesis is
  the thing trackers (ApeWisdom/Stocktwits) and portals (Yahoo) don't do — it's the
  core differentiator. Lean into the attention → filing → discussion triangle.

---

## ✅ Done (recent)
- Reverted to original indigo palette; kept the cuter rounded fonts/cards/badges.
- Brand/site name = "EDGAR Media" (header + tab title); H1 tagline = "What's Popping?".
- Removed the "· filings from SEC EDGAR" label next to the Updated line.
- Footer slimmed to "by Xuanheng Huang · source".
