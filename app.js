// EDGAR Media — render trending.json into the dashboard.

import { esc, timeAgo, momentum, filingsListHtml } from "./js/util.js";
import { isConfigured } from "./js/config.js";
import { setItems } from "./js/store.js";
import * as auth from "./js/auth.js";
import * as router from "./js/router.js";
import { renderInlinePanel } from "./js/comments.js";

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

// --- card ---
function tickerId(it) { return (it.ticker || "").replace(/[^a-z0-9]/gi, ""); }

// Per-card comments footer: a CSS-checkbox expander (same pattern as .more-toggle)
// whose .card-comments panel is lazy-loaded on first open. Only shown for companies
// with a CIK, and only when Supabase is configured (otherwise the live site is
// byte-for-byte unchanged).
function commentsFooter(it) {
    if (!isConfigured || !it.cik) return "";
    const id = `cmt-${tickerId(it)}`;
    return `<input type="checkbox" id="${id}" class="cmt-toggle" hidden>
        <div class="card-comments-bar">
            <label class="cmt-toggle-label" for="${id}">💬 Comments<span class="cmt-count"></span></label>
        </div>
        <div class="card-comments" data-cik="${esc(it.cik)}"></div>`;
}

function cardHtml(it, i) {
    const verified = it.name_match === "verified";
    const hot = verified && it.filing && it.filing.fresh;
    const coName = it.display_name || it.name;
    // Ticker(s) -> the company's EDGAR filing page (no page exists for CIK-less
    // tickers like ETFs, so those render as plain non-link text). Dual-class
    // issuers (GOOG/GOOGL) are merged into one card carrying every ticker.
    const tickers = (it.tickers && it.tickers.length) ? it.tickers : [it.ticker];
    const ticker = it.cik
        ? tickers.map((t) =>
            `<a class="ticker" href="https://www.sec.gov/edgar/browse/?CIK=${esc(it.cik)}" target="_blank" rel="noopener">$${esc(t)}</a>`
          ).join(`<span class="ticker-sep">·</span>`)
        : `<span class="ticker">$${esc(it.ticker)}</span>`;
    return `<article class="card ${hot ? "hot" : ""}">
        <div class="card-main">
            <div class="rank">${i + 1}</div>
            <div class="tick">
                <div class="tick-row">
                    ${ticker}
                    <span class="coname">${esc(coName)}</span>
                </div>
                <div class="attention">${momentum(it)}</div>
            </div>
        </div>
        ${filingsListHtml(it)}
        ${commentsFooter(it)}
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
        list = list.filter((it) => {
            const tks = (it.tickers && it.tickers.length) ? it.tickers : [it.ticker];
            return tks.some((t) => t.toLowerCase().includes(q)) ||
                (it.display_name || it.name || "").toLowerCase().includes(q);
        });
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

// Lazy-load a card's inline comments the first time its panel is opened. One
// delegated listener on #list survives the innerHTML re-renders in applyView.
function initCommentPanels() {
    const list = $("#list");
    if (!list) return;
    list.addEventListener("change", (e) => {
        const cb = e.target.closest(".cmt-toggle");
        if (!cb || !cb.checked) return;
        const card = cb.closest(".card");
        const panel = card?.querySelector(".card-comments");
        if (!panel || panel.dataset.loaded) return;
        panel.dataset.loaded = "1";
        const cik = panel.dataset.cik;
        const company = allItems.find((it) => String(it.cik) === String(cik));
        const countEl = card.querySelector(".cmt-count");
        renderInlinePanel(cik, panel, company, (n) => {
            if (countEl) countEl.textContent = n ? ` (${n})` : "";
        });
    });
}

// --- load ---
auth.init();   // header sign-in/out (no-op until Supabase is configured)
initCommentPanels();

fetch("./data/trending.json", { cache: "no-store" })
    .then((r) => { if (!r.ok) throw new Error(r.status); return r.json(); })
    .then((data) => {
        $("#updated").textContent =
            `Updated ${timeAgo(data.updated_utc)} · filings from SEC EDGAR`;
        allItems = data.items || [];
        setItems(allItems);            // share with the per-company page
        const savedCount = parseInt(localStorage.getItem("count"), 10) || DEFAULT_COUNT;
        if (countSel) countSel.value = String(savedCount);
        if (sortSel) sortSel.value = localStorage.getItem("sort") || "default";
        applyView();
        router.init();                 // resolve any #/company/… deep link
    })
    .catch((e) => {
        $("#updated").textContent = "";
        $("#list").innerHTML =
            `<div class="loading">Couldn't load data (${esc(e.message)}). ` +
            `The hourly updater may not have run yet.</div>`;
        // Still resolve the store + start the router so a #/company/… deep link
        // shows "not found" instead of hanging forever on `await ready`.
        setItems([]);
        router.init();
    });
