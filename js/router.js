// Hash router. `#/` (or empty) shows the home list; `#/company/<TICKER>` (or
// `#/company/cik/<CIK>`) shows a dedicated per-company discussion page.
// Hash routing because GitHub Pages has no server rewrites — deep links never 404.

import { ready, findByTicker, findByCik } from "./store.js";
import { esc, momentum, filingsListHtml } from "./util.js";
import { renderFullThread } from "./comments.js";

function parseHash() {
    const h = (location.hash || "").replace(/^#/, "");
    const m = h.match(/^\/company\/(?:cik\/(\d+)|([^/]+))\/?$/i);
    if (!m) return { view: "home" };
    return m[1]
        ? { view: "company", cik: m[1] }
        : { view: "company", ticker: decodeURIComponent(m[2]) };
}

function show(homeEl, companyEl, onHome) {
    if (homeEl) homeEl.hidden = !onHome;
    if (companyEl) companyEl.hidden = onHome;
}

function companyHeaderHtml(it) {
    const coName = it.display_name || it.name;
    const tickers = (it.tickers && it.tickers.length) ? it.tickers : [it.ticker];
    const ticker = it.cik
        ? tickers.map((t) =>
            `<a class="ticker" href="https://www.sec.gov/edgar/browse/?CIK=${esc(it.cik)}" target="_blank" rel="noopener">$${esc(t)}</a>`
          ).join(`<span class="ticker-sep">·</span>`)
        : `<span class="ticker">$${esc(it.ticker)}</span>`;
    return `<div class="company-head">
        <a class="back-link" href="#/">← All trending</a>
        <div class="tick-row">${ticker}<span class="coname">${esc(coName)}</span></div>
        <div class="attention">${momentum(it)}</div>
    </div>`;
}

function renderCompany(companyEl, it) {
    if (!it) {
        companyEl.innerHTML = `<div class="company-head"><a class="back-link" href="#/">← All trending</a>
            <p class="cmt-empty">Company not found in the current trending set.</p></div>`;
        return;
    }
    companyEl.innerHTML = `${companyHeaderHtml(it)}
        <article class="card company-card">${filingsListHtml(it)}</article>
        <section class="company-comments"><h2 class="company-comments-title">Discussion</h2>
            <div class="card-comments" data-cik="${esc(it.cik || "")}"></div>
        </section>`;
    const mount = companyEl.querySelector(".card-comments");
    if (it.cik) renderFullThread(it.cik, mount, it);
    else mount.innerHTML = `<p class="cmt-empty">No SEC company record to attach a discussion to.</p>`;
    window.scrollTo(0, 0);
}

let homeEl, companyEl;

async function route() {
    await ready;
    const r = parseHash();
    if (r.view === "home") {
        show(homeEl, companyEl, true);
        if (companyEl) companyEl.innerHTML = "";
        return;
    }
    const it = r.cik ? findByCik(r.cik) : findByTicker(r.ticker);
    show(homeEl, companyEl, false);
    renderCompany(companyEl, it);
}

export function init() {
    homeEl = document.getElementById("home-view");
    companyEl = document.getElementById("company-view");
    window.addEventListener("hashchange", route);
    route();
}
