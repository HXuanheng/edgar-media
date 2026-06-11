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

## 🧭 UI / account / firm-page ideas (parked, evaluated)

### 1. Move header controls (About / sign-in / dark mode) to a floating side control
Intent: make the top-right header less invasive.
**Verdict: do it selectively, not wholesale.**
- ⚠️ Bottom-right is already occupied by the floating PDF widget — avoid collision.
- Account + About in a floating blob hurts discoverability (users expect them top-right);
  likely lowers sign-in rather than reducing clutter. Floating elements also overlap
  content, esp. on mobile — can be *more* invasive.
- **Recommended:** theme toggle → small floating button is fine. Keep Account + About
  top-right but lighter — collapse into one avatar / ▾ menu. If the real issue is the
  sticky header eating vertical space, consider an **auto-hide-on-scroll header** instead.
- [ ] Decide: floating theme toggle only, vs. collapse-to-menu, vs. auto-hide header.

### 2. Account / profile page — ✅ BUILT (needs SQL migration run once)
Fields: photo, display name, first/last name, website, profession, investment style, background.
- New `#/account` route (`js/account.js`) + edit form; reached via the header avatar link.
- Profile photo: client-side downscale (≤512px) → upload to a public `avatars` Storage
  bucket → save the public URL to `profiles.avatar_url`. OAuth (Google/GitHub) sign-ins
  still auto-fill their photo. Non-essential fields are marked "(optional)".
- Extended `public.profiles` with the new columns + column-level UPDATE grant
  (`supabase/schema.sql`). **All profile fields are PUBLIC** (profiles is world-readable);
  the login **email stays private** (in auth.users, shown read-only, never stored here).
- [ ] **ACTION: run the new `alter table ... add column` + `grant update(...)` lines AND the
  avatar Storage bucket/policy block from `supabase/schema.sql` in the Supabase SQL editor**
  — until then, saving errors with "column does not exist" and photo upload has no bucket.
- [ ] Optional follow-up: if any field (e.g. background) should be private-to-you, move it
  to a separate owner-only table with stricter RLS.
- Investment style is stored as plain text from a dropdown — later it can flavor the
  persona-agents or filter the feed.

### 3. Firm landing-page menu: Overview / Financials
Tabs on the per-company page: Overview, then Financials = balance sheet, income
statement, cash-flow statement.
- **Data source:** SEC EDGAR XBRL **`companyfacts`** API (us-gaap tags) — free, official;
  we already call EDGAR in `scripts/build_data.py`.
- ⚠️ `companyfacts` JSON is large → fetch on-demand or top-N only, not all firms at build.
- ⚠️ XBRL tag-mapping varies across filers → start with a few headline metrics in
  Overview, expand later.
- Keep it tabbed + lazy-rendered ("not invasive"). This is the part most at risk of
  Yahoo-Finance-clone vibes — keep it framed as "the data behind the filing/buzz."
- [ ] Start with Overview headline metrics; add full statements incrementally.

---

## ✅ Done (recent)
- Account/profile page (`#/account`) — edit form + extended `profiles` schema (pending the
  one-time SQL migration in Supabase). See section 2 above.
- Reverted to original indigo palette; kept the cuter rounded fonts/cards/badges.
- Brand/site name = "EDGAR Media" (header + tab title); H1 tagline = "What's Popping?".
- Removed the "· filings from SEC EDGAR" label next to the Updated line.
- Footer slimmed to "by Xuanheng Huang · source".
