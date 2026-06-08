# EDGAR Media — Trending Disclosures

What Reddit is paying attention to, joined with the **official SEC filing** behind it.

A live dashboard of trending tickers (Reddit mention volume) cross-referenced with
each company's latest **material SEC EDGAR filing**. Tickers that are heating up
*and* filed something material in the last week get a 🔥 **fresh** flag — the case
where retail attention and real disclosure line up.

**Live:** https://hxuanheng.github.io/edgar-media/

## Why this exists

Retail investors hear "$XYZ is mooning" on Reddit but rarely read the actual 8-K.
This bridges the noisy attention signal to the authoritative primary source.

Plenty of dashboards rank stocks by Reddit chatter. The differentiator here is the
**filing layer**: surfacing the official disclosure tied to the attention spike.

## How it works

```
ApeWisdom API (Reddit mentions, ~hourly)   ->  WHAT people are watching
        x
SEC EDGAR submissions API (real-time)      ->  the OFFICIAL filing behind it
        =
data/trending.json  ->  static dashboard
```

- `scripts/build_data.py` — pure-stdlib pipeline. Pulls the top ~30 trending
  tickers, maps each to its SEC CIK, fetches the latest **material** filing
  (8-K, 10-K, 10-Q, S-1, DEF 14A, 13D/G, …), flags filings ≤ 7 days old.
  Each ticker→CIK match is **validated against SEC's official company name**;
  conflicts are flagged `unverified` and never surfaced as a confident filing.
- `.github/workflows/update.yml` — runs hourly, commits `data/trending.json`.
- `index.html` / `styles.css` / `app.js` — static dashboard, reads the JSON.

No backend. No build step. GitHub Pages + a scheduled Action.

## Run locally

```bash
python scripts/build_data.py     # writes data/trending.json
python -m http.server            # then open http://localhost:8000
```

## Notes & limits

- **No live download counts exist.** SEC publishes EDGAR access logs only with a
  ~6–12 month lag, so true "most-downloaded filing" cannot be live. Reddit mentions
  are used as a *live attention proxy* instead.
- Reddit attention is **retail- and meme-skewed** — many filers get zero mentions.
  This is a narrow, noisy slice of attention, not the whole market.
- **Not investment advice.**

## Deploy

1. Push to GitHub as `edgar-media`.
2. Settings → Pages → deploy from `main` branch, root.
3. Settings → Actions → enable workflows; the hourly cron then refreshes data.
4. Add a link from your main site to `/edgar-media/`.

Built by [Xuanheng Huang](https://hxuanheng.github.io/).
