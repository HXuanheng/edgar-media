# EDGAR Media — TODO / Roadmap

Running list so we don't repeat ourselves between sessions. Newest ideas at top.

---

## 🤖 AI persona-agents that comment on firms — ✅ BUILT (2026-06-19, pending manual setup)

**Goal:** AI characters with distinct personalities + investment styles post takes
under each trending firm. They react to the *same* filing + attention spike
differently → debate. The site's strongest originality lever.

**Decisions made (2026-06-19):**
- **DB-backed & interactive** (not static JSON). The hourly pipeline writes takes into
  the Supabase `comments` table via a **service-role key** (GitHub Actions secret only,
  never in browser/repo). Agents have real `profiles` rows → humans reply & vote with the
  existing UI.
- **Free models only** (NOT the Claude subscription): each agent is bound to one provider
  in the existing `AI_PROVIDERS` chain (Gemini Flash-Lite / Groq 70B & 8B / OpenRouter).
- **5 agents:** Prudence Vale (value · gemini-lite), DiamondHandz Dex (momentum · groq-8b),
  Red Flag Rhea (forensic short · gemini-lite), Sigma (quant · groq-70b), Atlas (macro ·
  openrouter). Each has a public **transparency dashboard** at `#/u/<id>`: model+provider,
  personality dials (risk-aversion, financial-literacy, creativity=temp, diligence,
  horizon, skepticism, verbosity) + the verbatim system prompt + disclaimer.
- **Coverage is quota-driven**, runs AFTER summaries (higher priority) on leftover quota;
  each agent's `diligence` dial + `MAX_AGENT_CALLS_PER_RUN` (12) bound coverage. Lazy agents
  drain firms over later runs (like summaries do). Dedup by `(agent, cik, accession)` → no
  reposts. Grounded on the cached `ai_summary`; instructed to say "I don't see that in the
  filing" not invent. One scripted agent→agent debate reply per run.

**Where it lives:** `scripts/seed_agents.py` (roster = single source of truth) +
`generate_agent_takes()` in `scripts/build_data.py`; frontend in `js/comments.js`
(pinned "What the AI agents think" group + bot badges) and `js/profile.js` (dashboard);
loud disclaimer banner in the Discussion (`js/router.js`); styles in `styles.css`.

**⚠️ Manual setup required to go live (see SETUP / plan):**
- [ ] Add GitHub Actions secrets `SUPABASE_URL` + `SUPABASE_SERVICE_ROLE_KEY`
  (Supabase → Settings → API → `service_role`). Never paste the key into a file.
- [ ] Run the new `supabase/schema.sql` migration (adds `is_agent` + `agent_meta` to
  `profiles`, plus the dedup index) in the SQL editor.
- [ ] Run the seed once: `python scripts/seed_agents.py` (with the two env vars) or the
  manual **"Seed agents"** workflow. Confirm 5 `slug -> uuid` lines.
- [ ] Avatars are committed under `assets/agents/`; Pages serves them after the next deploy.

**Realism upgrades — ✅ BUILT (2026-06-19, Tiers 1–3, PRs #2–#4):**
- *Tier 1:* takes/replies now also see live **price + % move + 5-day trend**, **Reddit
  buzz** (rank/mentions/surge/upvotes), and the **recent comment thread** for the firm.
- *Tier 2:* agents also post **reaction takes** on a firm with no fresh filing when there's
  a big price move / hype surge (dedup 1 per agent/firm/day); **per-persona voice** (high
  `creativity` → casual WSB tone + speculation; others stay analytical).
- *Tier 3:* **nested threads** (DB depth cap 4, `enforce_max_depth`), agents **reply to
  human** comments (`maybe_reply_to_humans`), agent **memory** of own recent takes, and
  **staggered/probabilistic** posting (shuffled roster + per-target `AGENT_POST_PROB`).

**Future idea — tie the dice roll to the laziness (`diligence`) dial:** right now the
per-post probability is a FLAT `AGENT_POST_PROB` (0.7) for everyone — only *coverage*
(`quota_n`) scales with `diligence`. To make laziness feel real, make the post/reply
probability itself scale with `diligence` (lazy agent ~30% to post, diligent ~90%), and
weight `maybe_reply_to_humans` responder selection toward higher-`diligence` agents
(currently random order). Files: `generate_agent_takes` (the `random.random() >
AGENT_POST_PROB` gate) + `maybe_reply_to_humans` (the `random.shuffle(order)`) in
`scripts/build_data.py`.

**Other later upgrades:** richer/more personas; surface a one-line agent take on the home card.

---

## ✨ Originality polish (cheap wins)

- [ ] **Synthesize the "why it's popping" in one line per card** — join the three data
  points we already have: `mentions +X% · filed <FORM> <when> · price ±Y%`
  (e.g. "$X mentions +1000%, filed 8-K yesterday, up 12%"). This causal synthesis is
  the thing trackers (ApeWisdom/Stocktwits) and portals (Yahoo) don't do — it's the
  core differentiator. Lean into the attention → filing → discussion triangle.

---

## 🧭 UI / account / firm-page ideas (parked, evaluated)

### 1. Move header controls (About / sign-in / dark mode) to a floating control — ✅ BUILT (bottom-right FAB)
Intent: make the top-right header less invasive.
**Decision (2026-06-12): bottom-right FAB speed-dial.** A single round trigger (`☰`) pinned
to the bottom-right corner opens a small popover menu that expands upward: **About · sign-in ·
dark mode**. The header keeps only the brand. (`.fab`/`.fab-menu` in `index.html` + `styles.css`;
open/close + the self-labelling theme row live in `app.js`. Control IDs `#auth-slot`/`#theme-toggle`
unchanged so `auth.js` and the theme logic were untouched.)
- Behaviour: outside-click / Esc closes; picking About or sign-in closes; flipping the theme
  keeps the menu open so you see it land. Theme row self-labels (`🌙 Dark mode` / `☀️ Light mode`).
- ⚠️ A real bottom-right **PDF widget** is still planned (see §3 / the agent ideas) and would
  collide with this FAB. Decision deferred: when it ships, stack them vertically or move one.
- ⚠️ Accepted tradeoff: sign-in is one tap deeper, which can lower sign-ups. **Watch the
  sign-in rate; if it dips,** surface sign-in more (badge the trigger when logged-out, or make
  the trigger the user avatar when logged-in).

### 2. Account / profile page — ✅ BUILT (SQL migration run — verified live 2026-06-12)
Fields: photo, display name, first/last name, website, profession, investment style, background.
- New `#/account` route (`js/account.js`) + edit form; reached via the header avatar link.
- Profile photo: client-side downscale (≤512px) → upload to a public `avatars` Storage
  bucket → save the public URL to `profiles.avatar_url`. OAuth (Google/GitHub) sign-ins
  still auto-fill their photo. Non-essential fields are marked "(optional)".
- Extended `public.profiles` with the new columns + column-level UPDATE grant
  (`supabase/schema.sql`). **All profile fields are PUBLIC** (profiles is world-readable);
  the login **email stays private** (in auth.users, shown read-only, never stored here).
- [x] **DONE (verified live 2026-06-12): ran the `alter table ... add column` + `grant update(...)`
  lines and the avatar Storage bucket/policy block.** Columns exist + populated; `avatars` bucket
  live. Saving + photo upload work.
- [ ] Optional follow-up: if any field (e.g. background) should be private-to-you, move it
  to a separate owner-only table with stricter RLS.
- Investment style is stored as plain text from a dropdown — later it can flavor the
  persona-agents or filter the feed.

### 3. Firm landing-page menu: Overview / Financials — ✅ BUILT (2026-06-12)
Tabs on the per-company page: **Overview** (the existing chart + filings + Discussion,
unchanged) + **Financials** (income statement, balance sheet, cash flow).
- **Data source:** SEC EDGAR XBRL **`companyfacts`** API (us-gaap tags). Extracted at
  **build time** (`build_financials()` in `scripts/build_data.py`) into one compact
  `data/financials/CIK{cik}.json` per verified, non-fund firm (~5 KB each); the client
  (`js/financials.js`) lazy-fetches only the firm it's showing. Raw companyfacts is
  1–15 MB, so the build does the heavy fetch once/hour; visitors download ~5 KB.
- **Extraction:** a curated PRIMARY→FALLBACK tag map per line item, *merged across the
  whole chain by year* (filers switch tags across eras — e.g. MSFT `Revenues` pre-2018,
  `RevenueFromContractWithCustomerExcludingAssessedTax` after). Annual figures via the
  XBRL `frame` (`CYxxxx` / `CYxxxxQ4I`), with a 10-K/period-end-year fallback. Last 5 FY.
- **Cost control:** content-hash write-gate — a firm's file is rewritten only when its
  numbers change (~quarterly), so hourly git churn stays ~zero. CI commit step + the
  `update.yml` change/`git add` now include `data/financials` (porcelain check so a firm
  newly entering the top-30 is picked up).
- **UI:** delegated `.co-tab` handler in `router.js`; Overview stays mounted (toggled via
  `hidden`) so chart/comments wiring is never torn down. Three collapsible `.fin-table`s
  (Income open by default), `$B/$M` via new `fmtUSD`/`fmtNum` in `util.js`, EPS plain.
  Each statement wrapped in `.fin-stmt` so the shared `.more-toggle ~ .more-rows` CSS
  doesn't leak the open state across statements.
- Verified end-to-end (MSFT/AAPL values vs known 10-Ks, hash-gate no-churn, tab toggle,
  Overview intact, 404→empty state for firms with no us-gaap).
- [ ] Follow-ups: a few headline metrics on the Overview card too; gate refetch on the
  newest 10-K/10-Q accession to trim build time; add tags if a prominent firm shows gaps.

---

## 🛠 Deploy note
- **Cache-buster:** `index.html` has a version token `V` (currently `2026-06-12e`) used in an
  import map that versions the whole `./js/*.js` graph, plus the `styles.css?v=` link. **Bump
  `V` (both spots) on every deploy that changes JS/CSS** so visitors get new files on one normal
  refresh (no hard reload). Forgetting = users see stale JS for ~10 min (Pages cache).

## ✅ Done (recent)
- **AI persona-agents** (DB-backed): 5 fictional analysts post grounded, quota-budgeted
  takes into the Supabase comments thread + debate each other; public transparency
  dashboards at `#/u/<id>`. Built 2026-06-19 — pending manual Supabase setup (see top §).
- **Financials tab** on the per-company page (XBRL companyfacts → build-time per-firm
  `data/financials/CIK*.json` → lazy `js/financials.js`). See section 3 above.
- Cache-buster: import-map version token in `index.html` busts the full JS module graph + CSS.
- Public profile pages (`#/u/<id>`, `js/profile.js`) + comment authors are clickable to them.
  Read-only; email never shown. Account page links to "View public profile".
- Styled in-app confirm/prompt dialog (`js/dialog.js`) replaces the native browser popups for
  delete / admin-remove / report. Owner can self-grant admin via one SQL line (see below).
- Account/profile page (`#/account`) — edit form + extended `profiles` schema (SQL migration
  run + verified live 2026-06-12). See section 2 above.
- Reverted to original indigo palette; kept the cuter rounded fonts/cards/badges.
- Brand/site name = "EDGAR Media" (header + tab title); H1 tagline = "What's Popping?".
- Removed the "· filings from SEC EDGAR" label next to the Updated line.
- Footer slimmed to "by Xuanheng Huang · source".
