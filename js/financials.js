// Financials tab for the per-company page. Lazy-loads one small per-firm file
// (data/financials/CIK<cik>.json, written at build time from SEC's XBRL
// companyfacts — see scripts/build_data.py) and renders three collapsible
// statement tables. This is "the data behind the filing/buzz": audited annual
// figures, not a live trading terminal.
//
// renderFinancials(cik, mountEl) is called by router.js the first time the
// Financials tab is revealed. It self-guards (mountEl.dataset.finState) so a
// repeat tab click never refetches; a transient failure clears the guard so a
// later click can retry, while a definitive "no file" (404) does not.

import { esc, fmtUSD, fmtNum } from "./util.js";

// Statement render order + headings. Income opens by default (the headline
// numbers); the other two start collapsed so the panel is short on a phone.
const STATEMENTS = [
    ["income", "Income statement"],
    ["balance", "Balance sheet"],
    ["cashflow", "Cash flow"],
];

function empty(mountEl, msg) {
    mountEl.innerHTML = `<p class="cmt-empty">${esc(msg)}</p>`;
}

function tableHtml(rows, years) {
    const head = `<thead><tr><th class="fin-rowlabel" scope="col"></th>`
        + years.map((y) => `<th scope="col">${esc(y)}</th>`).join("")
        + `</tr></thead>`;
    const body = rows.map((r) => {
        // Per-share figures (diluted EPS) render plain; everything else compacts
        // to $B / $M. The unit comes straight from the build-time extract.
        const fmt = r.unit === "USD/shares" ? (v) => fmtNum(v) : (v) => fmtUSD(v);
        const cells = years.map((y) => {
            const v = r.values[String(y)];
            return `<td>${v === undefined ? "—" : esc(fmt(v))}</td>`;
        }).join("");
        return `<tr><th class="fin-rowlabel" scope="row">${esc(r.label)}</th>${cells}</tr>`;
    }).join("");
    return `<table class="fin-table">${head}<tbody>${body}</tbody></table>`;
}

// One collapsible statement, reusing the site-wide pure-CSS .more-toggle idiom.
// Each statement is wrapped in its own .fin-stmt: the global
// `.more-toggle:checked ~ .more-rows` rule uses the general-sibling combinator,
// so without a wrapper checking one statement would reveal every *later* one too.
function statementHtml(cik, key, title, rows, years, open) {
    const id = `fin-${esc(key)}-${esc(cik)}`;
    return `<div class="fin-stmt">
        <input type="checkbox" id="${id}" class="more-toggle" hidden${open ? " checked" : ""}>
        <label class="fin-head" for="${id}">
            <span class="fin-head-label">${esc(title)}</span>
            <span class="more-open">show</span>
            <span class="more-close">hide</span>
        </label>
        <div class="more-rows"><div class="fin-scroll">${tableHtml(rows, years)}</div></div>
    </div>`;
}

export async function renderFinancials(cik, mountEl) {
    if (!mountEl || mountEl.dataset.finState) return;   // wire-once (per mount)
    if (!cik) { mountEl.dataset.finState = "empty"; empty(mountEl, "No SEC company record for financials."); return; }

    mountEl.dataset.finState = "loading";
    empty(mountEl, "Loading financials…");

    let data;
    try {
        const r = await fetch(`./data/financials/CIK${cik}.json`, { cache: "no-store" });
        if (r.status === 404) {            // no file written -> foreign issuer / fund / no us-gaap
            mountEl.dataset.finState = "empty";
            empty(mountEl, "No XBRL financials filed (foreign issuers and funds report differently).");
            return;
        }
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        data = await r.json();
    } catch (e) {
        mountEl.dataset.finState = "";      // clear guard so a later tab click retries
        empty(mountEl, "Couldn't load financials right now.");
        return;
    }

    const years = data.fiscal_years || [];
    const stmts = data.statements || {};
    const sections = STATEMENTS
        .filter(([key]) => Array.isArray(stmts[key]) && stmts[key].length)
        .map(([key, title]) => statementHtml(cik, key, title, stmts[key], years, key === "income"));

    if (!sections.length) {
        mountEl.dataset.finState = "empty";
        empty(mountEl, "No XBRL financials filed for this company.");
        return;
    }

    const asOf = (data.updated || "").slice(0, 10);
    const note = `<p class="fin-note">Annual figures from ${esc(data.source || "SEC EDGAR XBRL company facts")}`
        + `${asOf ? ` · as of ${esc(asOf)}` : ""}</p>`;
    mountEl.innerHTML = `<article class="card fin-card">${note}${sections.join("")}</article>`;
    mountEl.dataset.finState = "done";
}
