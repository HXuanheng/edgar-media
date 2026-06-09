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
    if (c === null || c === undefined)
        return `${mentions} mentions`;
    const arrow = c >= 0 ? "▲" : "▼";
    const cls = c >= 0 ? "chg-up" : "chg-down";
    return `${mentions} mentions <span class="${cls}">${arrow} ${Math.abs(c)}%</span> <span style="opacity:.7">vs 24h</span>`;
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

export function filingsListHtml(it, ns = "") {
    const head = it.filing;
    const all = it.cik ? (it.filings || []) : [];
    const warn = it.name_match === "mismatch"
        ? `<p class="filing-warn"><span class="warn">⚠ unverified ticker/name match</span></p>`
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
        return `<div class="card-filings">${warn}<p class="filing-none">no recent material filing</p></div>`;
    }
    // "More" rows = the rest of the 90-day window minus the lead (by reference).
    const rest = all.filter((f) => f !== lead);
    const leadRow = `<ul class="filing-list filing-lead">${filingRow(lead, it.ticker, ns)}</ul>`;
    // Single filing -> a "Latest filings" label, no toggle.
    if (!rest.length) {
        return `<div class="card-filings">${warn}
            <div class="filing-head"><span class="filing-head-label">Latest filings</span></div>
            ${leadRow}</div>`;
    }
    const id = `more-${ns}${(it.ticker || "").replace(/[^a-z0-9]/gi, "")}`;
    const n = rest.length;
    const rows = rest.map((f) => filingRow(f, it.ticker, ns)).join("");
    // Checkbox first, then the header (with both toggle labels) and the rows -> the
    // control lives in the fixed header at the top, so collapsing never needs a scroll.
    return `<div class="card-filings">${warn}
        <input type="checkbox" id="${id}" class="more-toggle" hidden>
        <div class="filing-head">
            <span class="filing-head-label">Latest filings</span>
            <label class="more-open" for="${id}">show ${n} more</label>
            <label class="more-close" for="${id}">show less</label>
        </div>
        ${leadRow}
        <ul class="filing-list more-rows">${rows}</ul>
    </div>`;
}
