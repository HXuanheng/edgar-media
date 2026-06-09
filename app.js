// EDGAR Media — render trending.json into the dashboard.

const $ = (s) => document.querySelector(s);

// --- dark mode (persisted) ---
const toggle = $("#theme-toggle");
function applyTheme(dark) {
    document.body.classList.toggle("dark-mode", dark);
    toggle.textContent = dark ? "☀️" : "🌙";
}
applyTheme(localStorage.getItem("theme") === "dark");
toggle.addEventListener("click", () => {
    const dark = !document.body.classList.contains("dark-mode");
    localStorage.setItem("theme", dark ? "dark" : "light");
    applyTheme(dark);
});

// --- helpers ---
function timeAgo(iso) {
    const then = new Date(iso).getTime();
    if (Number.isNaN(then)) return "";
    const mins = Math.round((Date.now() - then) / 60000);
    if (mins < 1) return "just now";
    if (mins < 60) return `${mins} min ago`;
    const hrs = Math.round(mins / 60);
    if (hrs < 24) return `${hrs} h ago`;
    return `${Math.round(hrs / 24)} d ago`;
}

function daysLabel(d) {
    if (d === 0) return "today";
    if (d === 1) return "yesterday";
    return `${d} days ago`;
}

function esc(s) {
    return String(s).replace(/[&<>"]/g, (c) =>
        ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}

function momentum(it) {
    const { mention_change_pct: c, mentions } = it;
    if (c === null || c === undefined)
        return `${mentions} mentions`;
    const arrow = c >= 0 ? "▲" : "▼";
    const cls = c >= 0 ? "chg-up" : "chg-down";
    return `${mentions} mentions <span class="${cls}">${arrow} ${Math.abs(c)}%</span> <span style="opacity:.7">vs 24h</span>`;
}

function filingHtml(f, nameMatch) {
    if (!f) return `<div class="filing-none">no recent material filing</div>`;
    const meta = `${f.days_ago != null ? daysLabel(f.days_ago) : f.date}`;
    // Card badge shows only freshness + date; the form is the link, and the
    // item-code label now lives in the per-row summary list, not here.
    if (nameMatch === "mismatch") {
        // Unverified match: stay cautious, show the form code only.
        return `<div class="filing-stale">
            <span class="warn">⚠ unverified ticker/name match</span><br>
            <span class="filing-form">${esc(f.form)}</span>
            <span class="filing-meta">· ${esc(meta)}</span><br>
            <a class="filing-meta" href="${esc(f.index_url)}" target="_blank" rel="noopener">view on SEC ↗</a>
        </div>`;
    }
    if (f.fresh) {
        return `<div class="filing-fresh">
            <span class="badge">🔥 ${esc(meta)}</span><br>
            <a href="${esc(f.index_url)}" target="_blank" rel="noopener">${esc(f.form)} ↗</a>
        </div>`;
    }
    return `<div class="filing-stale">
        <span class="filing-form">${esc(f.form)}</span>
        <span class="filing-meta">· ${esc(meta)}</span><br>
        <a class="filing-meta" href="${esc(f.index_url)}" target="_blank" rel="noopener">view on SEC ↗</a>
    </div>`;
}

function filingRow(f) {
    const when = f.days_ago != null ? daysLabel(f.days_ago) : f.date;
    const lbl = f.summary && f.summary !== f.form ? ` · ${esc(f.summary)}` : "";
    const formDate = `<span class="flform">${esc(f.form)}</span>
        <span class="fl-date">${esc(f.date)}${lbl}</span>`;
    // when + link cluster — pushed to the right edge (.flwhen has margin-left:auto)
    const tail = `<span class="flwhen">${esc(when)}</span>
        <a class="fl-sec" href="${esc(f.index_url)}" target="_blank" rel="noopener">View on SEC ↗</a>`;
    // Summarized row stays a one-liner with a "Summary" tag (next to the
    // description it annotates) as the expand cue; clicking the header (a <label>
    // tied to a hidden checkbox) reveals it inline. The SEC link is an interactive
    // descendant, so it navigates without toggling the row. Pure CSS, no JS.
    if (f.ai_summary) {
        const id = `flx-${(f.accession || "").replace(/[^a-z0-9]/gi, "")}`;
        return `<li class="flrow flrow-ai">
            <input type="checkbox" id="${id}" class="fl-expand" hidden>
            <label class="fl-head" for="${id}">${formDate}<span class="fl-tag">Summary</span>${tail}</label>
            <p class="fl-summary"><em class="fl-label">Summary:</em> ${esc(f.ai_summary)}</p>
        </li>`;
    }
    return `<li class="flrow flrow-plain"><div class="fl-head">${formDate}${tail}</div></li>`;
}

function filingsListHtml(it) {
    // Nothing to expand -> the headline line already conveys filing status.
    const fs = it.cik ? (it.filings || []) : [];
    if (!fs.length) return "";
    const rows = fs.map(filingRow).join("");
    return `<details class="card-details">
        <summary>${fs.length} recent filing${fs.length > 1 ? "s" : ""} · last 90 days</summary>
        <ul class="filing-list">${rows}</ul>
    </details>`;
}

function cardHtml(it, i) {
    const verified = it.name_match === "verified";
    const hot = verified && it.filing && it.filing.fresh;
    const coName = it.display_name || it.name;
    const redditUrl = `https://www.reddit.com/r/wallstreetbets/search/?q=%24${encodeURIComponent(it.ticker)}`;
    return `<article class="card ${hot ? "hot" : ""}">
        <div class="card-main">
            <div class="rank">${i + 1}</div>
            <div class="tick">
                <div class="tick-row">
                    <a class="ticker" href="${redditUrl}" target="_blank" rel="noopener">$${esc(it.ticker)}</a>
                    <span class="coname">${esc(coName)}</span>
                </div>
                <div class="attention">${momentum(it)}</div>
            </div>
            <div class="filing">${filingHtml(it.filing, it.name_match)}</div>
        </div>
        ${filingsListHtml(it)}
    </article>`;
}

// --- controls: search + sort + count -> one render pipeline ---
const DEFAULT_COUNT = 10;
let allItems = [];

const searchInput = $("#search");
const sortSel = $("#sort-by");
const countSel = $("#show-count");

const filingDays = (it) => (it.filing ? it.filing.days_ago : null) ?? Infinity;

function sortItems(list, key) {
    if (key === "mentions")
        return list.sort((a, b) => (b.mentions || 0) - (a.mentions || 0));
    if (key === "momentum")
        return list.sort((a, b) =>
            (b.mention_change_pct ?? -Infinity) - (a.mention_change_pct ?? -Infinity));
    if (key === "filing")
        return list.sort((a, b) => filingDays(a) - filingDays(b));
    return list;   // "default" -> keep backend (hot-first) order
}

// filter (search) -> sort -> slice (count) -> render
function applyView() {
    if (!allItems.length) {
        $("#list").innerHTML = `<div class="loading">No data yet.</div>`;
        return;
    }
    const q = (searchInput?.value || "").trim().toLowerCase();
    const sortKey = sortSel?.value || "default";
    const n = parseInt(countSel?.value, 10) || DEFAULT_COUNT;

    let list = sortItems(allItems.slice(), sortKey);
    if (q) {
        list = list.filter((it) =>
            it.ticker.toLowerCase().includes(q) ||
            (it.display_name || it.name || "").toLowerCase().includes(q));
    }
    const view = q ? list : list.slice(0, n);   // count applies only when not searching

    if (q && view.length === 0) {
        $("#list").innerHTML =
            `<div class="empty-search">“${esc(searchInput.value.trim())}” is not trending right now.</div>`;
        return;
    }
    $("#list").innerHTML = view.map(cardHtml).join("");
}

if (searchInput) searchInput.addEventListener("input", applyView);
if (sortSel) sortSel.addEventListener("change", () => {
    localStorage.setItem("sort", sortSel.value);
    applyView();
});
if (countSel) countSel.addEventListener("change", () => {
    localStorage.setItem("count", parseInt(countSel.value, 10) || DEFAULT_COUNT);
    applyView();
});

// --- load ---
fetch("./data/trending.json", { cache: "no-store" })
    .then((r) => { if (!r.ok) throw new Error(r.status); return r.json(); })
    .then((data) => {
        $("#updated").textContent =
            `Updated ${timeAgo(data.updated_utc)} · filings from SEC EDGAR`;
        allItems = data.items || [];
        const savedCount = parseInt(localStorage.getItem("count"), 10) || DEFAULT_COUNT;
        if (countSel) countSel.value = String(savedCount);
        if (sortSel) sortSel.value = localStorage.getItem("sort") || "default";
        applyView();
    })
    .catch((e) => {
        $("#updated").textContent = "";
        $("#list").innerHTML =
            `<div class="loading">Couldn't load data (${esc(e.message)}). ` +
            `The hourly updater may not have run yet.</div>`;
    });
