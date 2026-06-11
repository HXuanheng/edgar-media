// Hash router. `#/` (or empty) shows the home list; `#/company/<TICKER>` (or
// `#/company/cik/<CIK>`) shows a dedicated per-company discussion page.
// Hash routing because GitHub Pages has no server rewrites — deep links never 404.

import { ready, findByTicker, findByCik } from "./store.js";
import { esc, momentum, priceHtml, filingsListHtml } from "./util.js";
import { priceChartHtml, wireChart } from "./chart.js";
import { renderFullThread } from "./comments.js";
import * as account from "./account.js";
import * as profile from "./profile.js";

function parseHash() {
    const h = (location.hash || "").replace(/^#/, "");
    if (/^\/account\/?$/i.test(h)) return { view: "account" };
    const u = h.match(/^\/u\/([0-9a-fA-F-]{8,})\/?$/);
    if (u) return { view: "profile", userId: u[1] };
    const m = h.match(/^\/company\/(?:cik\/(\d+)|([^/]+))\/?$/i);
    if (!m) return { view: "home" };
    return m[1]
        ? { view: "company", cik: m[1] }
        : { view: "company", ticker: decodeURIComponent(m[2]) };
}

function showView(view) {
    if (homeEl) homeEl.hidden = view !== "home";
    if (companyEl) companyEl.hidden = view !== "company";
    if (accountEl) accountEl.hidden = view !== "account";
    if (profileEl) profileEl.hidden = view !== "profile";
}

function companyHeaderHtml(it) {
    const coName = it.display_name || it.name;
    const tickers = (it.tickers && it.tickers.length) ? it.tickers : [it.ticker];
    // Ticker is plain text here (you're already on the firm's page). EDGAR is a
    // small, deliberate ↗ link (same pattern as filing rows) so the firm header
    // can't open an external page by accident. Only when the firm has a CIK.
    const ticker = tickers.map((t) => `<span class="ticker">$${esc(t)}</span>`)
        .join(`<span class="ticker-sep">·</span>`);
    const edgar = it.cik
        ? `<a class="fl-sec edgar-link" href="https://www.sec.gov/edgar/browse/?CIK=${esc(it.cik)}" target="_blank" rel="noopener" aria-label="Open ${esc(it.ticker)} on SEC EDGAR">↗</a>`
        : "";
    // Name + its EDGAR ↗ as one tight unit so the arrow hugs the company name.
    const nameGroup = `<span class="co-edgar"><span class="coname">${esc(coName)}</span>${edgar}</span>`;
    return `<div class="company-head">
        <a class="back-link" href="#/">← All trending</a>
        <div class="tick-row">${ticker}${nameGroup}${it.is_fund ? `<span class="tag-etf">ETF</span>` : ""}${priceHtml(it, false, true)}</div>
        <div class="attention">${momentum(it)}</div>
    </div>`;
}

function renderCompany(companyEl, it) {
    if (!it) {
        companyEl.innerHTML = `<div class="company-head"><a class="back-link" href="#/">← All trending</a>
            <p class="cmt-empty">Company not found in the current trending set.</p></div>`;
        return;
    }
    // Thread key = CIK, or the ticker for no-CIK ETFs (so they get a discussion too).
    const threadKey = it.cik || it.ticker;
    // Skip the filings card entirely when there's nothing to show (e.g. an ETF) so
    // the landing page doesn't show an empty bordered line.
    const filingsHtml = filingsListHtml(it, "co");
    // Price chart sits between the header (mention/momentum) and the filings card;
    // returns "" when there's no price history, so the section just disappears.
    companyEl.innerHTML = `${companyHeaderHtml(it)}
        ${priceChartHtml(it)}
        ${filingsHtml ? `<article class="card company-card">${filingsHtml}</article>` : ""}
        <section class="company-comments"><h2 class="company-comments-title">Discussion</h2>
            <div class="card-comments" data-cik="${esc(threadKey || "")}"></div>
        </section>`;
    wireChart(companyEl, it);              // clickable filing markers -> jump to row
    const mount = companyEl.querySelector(".card-comments");
    if (threadKey) renderFullThread(threadKey, mount, it);
    else mount.innerHTML = `<p class="cmt-empty">No SEC company record to attach a discussion to.</p>`;
}

let homeEl, companyEl, accountEl, profileEl;
let renderedCompanyKey = null;          // which company is currently in the DOM
const scrollByHash = new Map();         // hash -> last scroll position on that page
const normHash = (h) => (!h || h === "#" || h === "#/") ? "#/" : h;   // "" and "#/" are the same page
let prevHash = normHash(location.hash); // the page we're on, so we can save its scroll

// We restore scroll ourselves (the browser's auto restore fights our re-renders).
if ("scrollRestoration" in history) history.scrollRestoration = "manual";

function applyScroll() {
    const y = scrollByHash.get(normHash(location.hash)) || 0;
    // Re-assert over a few frames: showing a view + async content (comments) can nudge
    // the scroll back to 0 just after we set it, so keep restoring until it sticks.
    let n = 0;
    const tick = () => {
        window.scrollTo(0, y);
        if (++n < 18 && Math.abs(window.scrollY - y) > 2)
            (n < 6 ? requestAnimationFrame(tick) : setTimeout(tick, 30));
    };
    tick();
}

async function route() {
    // Remember where we were on the page we're leaving — route() runs while that page
    // is still in the DOM, so window.scrollY is its position. Lets Back restore the spot.
    scrollByHash.set(prevHash, window.scrollY);
    await ready;
    const r = parseHash();
    showView(r.view);

    if (r.view === "account") {
        account.render(accountEl);
    } else if (r.view === "profile") {
        profile.render(profileEl, r.userId);
    } else if (r.view === "company") {
        // Re-render only when the company actually changes; otherwise keep the existing
        // DOM (and its expanded replies) so coming Back lands on the exact same view.
        const key = r.cik ? `cik/${r.cik}` : `t/${r.ticker}`;
        if (key !== renderedCompanyKey) {
            const it = r.cik ? findByCik(r.cik) : findByTicker(r.ticker);
            renderCompany(companyEl, it);
            renderedCompanyKey = key;
        }
        applyScroll();
    } else {   // home — keep the company DOM around so a Back to it is instant + in place
        if (accountEl) accountEl.innerHTML = "";
        if (profileEl) profileEl.innerHTML = "";
        applyScroll();
    }
    prevHash = normHash(location.hash);
}

export function init() {
    homeEl = document.getElementById("home-view");
    companyEl = document.getElementById("company-view");
    accountEl = document.getElementById("account-view");
    profileEl = document.getElementById("profile-view");
    // "← Back" links (profile / account) return to the actual previous page + scroll,
    // instead of always jumping to home.
    document.addEventListener("click", (e) => {
        const b = e.target.closest("[data-back]");
        if (!b) return;
        e.preventDefault();
        if (history.length > 1) history.back();
        else location.hash = "#/";
    });
    window.addEventListener("hashchange", route);
    route();
}
