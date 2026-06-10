// Pure rendering helpers shared by the home list (app.js) and the per-company
// page (router.js). No DOM side effects, no data fetching — safe to import anywhere.
// esc/timeAgo/daysLabel/momentum/filingRow/filingsListHtml were lifted verbatim
// from the original app.js so existing output is byte-for-byte unchanged.

export function timeAgo(iso) {
    const then = new Date(iso).getTime();
    if (Number.isNaN(then)) return "";
    const mins = Math.round((Date.now() - then) / 60000);
    if (mins < 1) return "just now";
    if (mins < 60) return `${mins} min ago`;
    const hrs = Math.round(mins / 60);
    if (hrs < 24) return `${hrs} h ago`;
    return `${Math.round(hrs / 24)} d ago`;
}

export function daysLabel(d) {
    if (d === 0) return "today";
    if (d === 1) return "yesterday";
    return `${d} days ago`;
}

export function esc(s) {
    return String(s).replace(/[&<>"']/g, (c) =>
        ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

export function momentum(it) {
    const { mention_change_pct: c, mentions } = it;
    // The count comes from ApeWisdom -> link to its per-ticker page so anyone can
    // verify the mentions (24h breakdown, users, WSB split). New tab.
    const url = `https://apewisdom.io/stocks/${encodeURIComponent(it.ticker)}/`;
    const link = `<a class="mentions-link" href="${url}" target="_blank" rel="noopener">${mentions} mentions</a>`;
    if (c === null || c === undefined)
        return link;
    const arrow = c >= 0 ? "▲" : "▼";
    const cls = c >= 0 ? "chg-up" : "chg-down";
    return `${link} <span class="${cls}">${arrow} ${Math.abs(c)}%</span> <span style="opacity:.7">vs 24h</span>`;
}

export function filingRow(f, prefix, ns = "") {
    const when = f.days_ago != null ? daysLabel(f.days_ago) : f.date;
    const fire = f.fresh ? "🔥 " : "";   // very recent (<=7 days) -> flame on the age
    const lbl = f.summary && f.summary !== f.form ? ` · ${esc(f.summary)}` : "";
    // Form code IS the link to the SEC filing (the ↗ cues the external jump); it's
    // an interactive descendant, so on summary rows clicking it navigates without
    // toggling the row open.
    const formDate = `<a class="flform" href="${esc(f.index_url)}" target="_blank" rel="noopener">${esc(f.form)} ↗</a>
        <span class="fl-date">${esc(f.date)}${lbl}</span>`;
    // age, pushed to the right edge (.flwhen has margin-left:auto)
    const tail = `<span class="flwhen">${fire}${esc(when)}</span>`;
    // Summarized row stays a one-liner with a "Summary" tag (next to the
    // description it annotates) as the expand cue; clicking the header (a <label>
    // tied to a hidden checkbox) reveals it inline. The SEC link is an interactive
    // descendant, so it navigates without toggling the row. Pure CSS, no JS.
    if (f.ai_summary) {
        // Namespace by ticker: tickers sharing a CIK (e.g. GOOG/GOOGL) repeat
        // the same accessions, so accession alone yields duplicate DOM ids and
        // a <label for> would toggle the first card's checkbox, not this one.
        const id = `flx-${ns}${(prefix || "").replace(/[^a-z0-9]/gi, "")}-${(f.accession || "").replace(/[^a-z0-9]/gi, "")}`;
        return `<li class="flrow flrow-ai">
            <input type="checkbox" id="${id}" class="fl-expand" hidden>
            <label class="fl-head" for="${id}">${formDate}<span class="fl-tag">Summary</span>${tail}</label>
            <p class="fl-summary"><em class="fl-label">Summary:</em> ${esc(f.ai_summary)}</p>
        </li>`;
    }
    return `<li class="flrow flrow-plain"><div class="fl-head">${formDate}${tail}</div></li>`;
}

// Tiny inline trend line for the 5-day closes. Normalized min->max so the shape
// always fills the box; colored by overall direction. <2 points -> nothing.
export function sparklineSvg(spark, up) {
    if (!Array.isArray(spark) || spark.length < 2) return "";
    const w = 72, h = 22, pad = 3;
    const min = Math.min(...spark), max = Math.max(...spark);
    const span = max - min || 1;
    const n = spark.length;
    const xy = (v, i) => [
        pad + (i * (w - 2 * pad)) / (n - 1),
        pad + (h - 2 * pad) * (1 - (v - min) / span),         // y is flipped
    ];
    const pts = spark.map((v, i) => xy(v, i).map((z) => z.toFixed(1)).join(",")).join(" ");
    const [ex, ey] = xy(spark[n - 1], n - 1);                 // last point -> anchor dot
    // No preserveAspectRatio override: viewBox matches width/height so the
    // 1.75px stroke isn't squashed horizontally.
    return `<svg class="spark ${up ? "spark-up" : "spark-down"}" viewBox="0 0 ${w} ${h}"`
        + ` width="${w}" height="${h}" aria-hidden="true">`
        + `<polyline points="${pts}" fill="none" stroke-width="1.75"`
        + ` stroke-linejoin="round" stroke-linecap="round"/>`
        + `<circle cx="${ex.toFixed(1)}" cy="${ey.toFixed(1)}" r="1.9"/></svg>`;
}

// Market-price chip: last price + day change % (reusing the .chg-up/.chg-down
// colors from momentum) + sparkline. No price (foreign/crypto/outage) -> "".
// A carried-forward (stale) price is labelled with its as_of date so it is never
// shown as if live. Built at build time in scripts/build_data.py.
export function priceHtml(it) {
    const p = it.price;
    if (!p || p.last == null) return "";
    const c = p.change_pct;
    const up = (c ?? 0) >= 0;
    const last = Number(p.last).toLocaleString(undefined, {
        minimumFractionDigits: 2, maximumFractionDigits: 2,
    });
    // Currency rides inside .px-last (muted) so it hugs the number; change %,
    // sparkline and the stale note are separate flex items. No leading spaces:
    // a stray text node becomes an anonymous flex item and doubles the gap.
    const cur = p.currency ? `<span class="px-cur">${esc(p.currency)}</span>` : "";
    const chg = (c === null || c === undefined) ? "" :
        `<span class="${up ? "chg-up" : "chg-down"}">${up ? "▲" : "▼"} ${Math.abs(c)}%</span>`;
    const stale = p.stale && p.as_of
        ? `<span class="px-stale">· as of ${esc(p.as_of)}</span>` : "";
    return `<div class="price"><span class="px-last">${esc(last)}${cur}</span>`
        + `${chg}${sparklineSvg(p.spark, up)}${stale}</div>`;
}

export function filingsListHtml(it, ns = "") {
    const head = it.filing;
    const all = it.cik ? (it.filings || []) : [];
    const warn = it.name_match === "mismatch"
        ? `<p class="filing-warn"><span class="warn">⚠ unverified ticker/name match</span></p>`
        : it.name_match === "wrong_cik"
        ? `<p class="filing-warn"><span class="warn">⚠ couldn't confirm an SEC company for this ticker</span></p>`
        : "";
    // Match the headline within the 90-day window (by accession, or date+form if a
    // headline ever lacks one).
    const matchHead = head
        ? (head.accession
            ? (f) => f.accession === head.accession
            : (f) => f.date === head.date && f.form === head.form)
        : () => false;
    // Lead = the latest filing, shown directly as the first row (no menu). Prefer
    // the in-window copy because only it carries the ai_summary (-> Summary button);
    // fall back to the headline if it's older than the window, then to the newest
    // in-window filing. No filing at all -> a muted note, no menu.
    const lead = all.find(matchHead) || head || all[0] || null;
    if (!lead) {
        // wrong_cik: the warn already explains it. Funds: the ETF badge already
        // says it -> leave the filing area empty rather than adding a note.
        const none = it.name_match === "wrong_cik" ? ""
            : it.is_fund ? ""
            : "no recent material filing";
        const noneHtml = none ? `<p class="filing-none">${esc(none)}</p>` : "";
        if (!warn && !noneHtml) return "";   // nothing to show (e.g. an ETF) -> render nothing
        return `<div class="card-filings">${warn}${noneHtml}</div>`;
    }
    // Show the first leadN filings directly; the rest go behind the toggle. The
    // landing page (ns "co") leads with 3, the home cards with 1.
    const leadN = ns === "co" ? 3 : 1;
    const rest = all.filter((f) => f !== lead);          // newest-first, minus lead
    const ordered = [lead, ...rest];
    const shown = ordered.slice(0, leadN);
    const hidden = ordered.slice(leadN);
    const leadRows = `<ul class="filing-list filing-lead">${shown.map((f) => filingRow(f, it.ticker, ns)).join("")}</ul>`;
    // No remainder -> a "Latest filings" label, no toggle.
    if (!hidden.length) {
        return `<div class="card-filings">${warn}
            <div class="filing-head"><span class="filing-head-label">Latest filings</span></div>
            ${leadRows}</div>`;
    }
    const id = `more-${ns}${(it.ticker || "").replace(/[^a-z0-9]/gi, "")}`;
    const n = hidden.length;
    const rows = hidden.map((f) => filingRow(f, it.ticker, ns)).join("");
    // Checkbox first, then the header (with both toggle labels) and the rows -> the
    // control lives in the fixed header at the top, so collapsing never needs a scroll.
    // The whole header row is the toggle (one <label>), not just the "show more"
    // text -> bigger click target. more-open/close are spans shown via the checkbox.
    return `<div class="card-filings">${warn}
        <input type="checkbox" id="${id}" class="more-toggle" hidden>
        <label class="filing-head" for="${id}">
            <span class="filing-head-label">Latest filings</span>
            <span class="more-open">show ${n} more</span>
            <span class="more-close">show less</span>
        </label>
        ${leadRows}
        <ul class="filing-list more-rows">${rows}</ul>
    </div>`;
}
