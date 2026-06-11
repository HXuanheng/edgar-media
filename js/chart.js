// Per-firm price chart for the company page: a 3-month close line with a dashed
// vertical marker on every SEC filing day, colored + tagged by form type. Pure
// hand-rolled SVG (zero deps), same spirit as the tiny sparkline in util.js but
// full-width and dated. Renders only when price.history exists, so every no-data
// path (foreign/crypto/outage) silently drops the section.
//
// Two exports: priceChartHtml(it) builds the markup; wireChart(companyEl, it)
// attaches one delegated click handler so tapping a marker scrolls to + expands
// the matching filing row in the list below.

import { esc } from "./util.js";

// Forms ranked by investor importance (most material first); color follows the
// rank on a warm->cool ramp, so the heaviest filings read hottest and minor ones
// recede. Tiers grouped by what the form is. Anything unlisted -> muted slate.
// Colors are fixed hexes (read fine in both light/dark themes).
const FORM_RANK = [
    ["10-K", "20-F", "40-F"],                  // annual report
    ["10-Q"],                                  // quarterly report
    ["8-K", "6-K"],                            // material / current events
    ["DEF 14A", "DEFA14A", "PRE 14A"],         // proxy
    ["S-1", "S-4", "F-1", "424B4", "424B5"],   // registration / offering
    ["SC 13D", "SC 13G"],                      // ownership stakes
];
const RANK_COLORS = ["#dc2626", "#ea580c", "#d97706", "#16a34a", "#0891b2", "#7c3aed"];
const DEFAULT_FORM_COLOR = "#94a3b8";
const rankOf = (form) => {
    const i = FORM_RANK.findIndex((g) => g.includes(form));
    return i === -1 ? FORM_RANK.length : i;
};
const formColor = (form) => RANK_COLORS[rankOf(form)] || DEFAULT_FORM_COLOR;

const MON = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
             "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const ms = (d) => Date.parse(d + "T00:00:00Z");           // YYYY-MM-DD -> epoch ms
function fmtDate(d) {
    const [, m, day] = d.split("-");
    return `${MON[+m - 1]} ${+day}`;
}

// SVG coordinate space (CSS scales it to the card width via width:100%).
const W = 680, H = 240;
const PAD_L = 12, PAD_R = 48, PAD_T = 22, PAD_B = 24;
const PLOT_TOP = PAD_T, PLOT_BOT = H - PAD_B;
const MARK_TOP = 8;                                        // marker pin head y

// Selectable time spans (label, days). 1M is the default (the recent picture);
// 3M is the max, since filing markers only exist for the last ~90 days (the SEC
// list window). Both just slice the same 3-month series client-side.
const SPANS = [["1M", 30], ["3M", 90]];
const DEFAULT_SPAN = 30;

// price.history is stored compactly as two parallel CSV strings ({t,c}) to keep the
// committed JSON small. Accept that, the legacy [[date,close],...] array, or nothing.
function unpackHist(h) {
    if (Array.isArray(h)) return h;
    if (h && typeof h.t === "string" && typeof h.c === "string") {
        const d = h.t.split(","), c = h.c.split(",");
        return d.map((dd, i) => [dd, Number(c[i])]).filter((p) => dd != null);
    }
    return [];
}

// Build the chart body (legend + SVG) for one span. Slicing the full series here
// (not refetching) is what makes the span buttons instant.
function buildChartBody(full, it, spanDays) {
    const tEnd = ms(full[full.length - 1][0]);
    const cutoff = tEnd - spanDays * 86400000;
    let hist = full.filter(([d]) => ms(d) >= cutoff);
    if (hist.length < 2) hist = full.slice(-2);              // span shorter than data gap

    const t0 = ms(hist[0][0]), t1 = ms(hist[hist.length - 1][0]);
    const tSpan = (t1 - t0) || 1;
    const closes = hist.map((h) => h[1]);
    const realLo = Math.min(...closes), realHi = Math.max(...closes);
    const padV = (realHi - realLo) * 0.06 || realHi * 0.01 || 1;   // breathing room
    const lo = realLo - padV, hi = realHi + padV;
    const vSpan = (hi - lo) || 1;
    const X = (t) => PAD_L + ((t - t0) / tSpan) * (W - PAD_L - PAD_R);
    const Y = (v) => PLOT_TOP + (1 - (v - lo) / vSpan) * (PLOT_BOT - PLOT_TOP);

    // Price line. Coloured by the direction over THIS window (first vs last close)
    // so the line's colour always matches the shape on screen.
    const upWin = closes[closes.length - 1] >= closes[0];
    const dirCls = upWin ? "pc-up" : "pc-down";
    const pts = hist.map(([d, c]) => `${X(ms(d)).toFixed(1)},${Y(c).toFixed(1)}`)
        .join(" ");
    const lastC = closes[closes.length - 1];
    const lx = X(t1), ly = Y(lastC);
    const lastLbl = Number(lastC).toLocaleString(undefined, {
        minimumFractionDigits: 2, maximumFractionDigits: 2,
    });

    // Faint hi/lo gridlines + labels (the actual window extremes, not padded).
    const grid = [realHi, realLo].map((v) => {
        const y = Y(v).toFixed(1);
        const lbl = Number(v).toLocaleString(undefined, {
            minimumFractionDigits: 2, maximumFractionDigits: 2,
        });
        return `<line class="pc-grid" x1="${PAD_L}" y1="${y}" x2="${W - PAD_R}" y2="${y}"/>`
            + `<text class="pc-axis pc-grid-lbl" x="${PAD_L + 2}" y="${(+y - 3).toFixed(1)}">${lbl}</text>`;
    }).join("");

    // Filing markers: group filings by date (within the visible window), newest-first.
    const filings = (it.cik ? it.filings : null) || [];
    const byDate = new Map();
    for (const f of filings) {
        if (!f.date) continue;
        const t = ms(f.date);
        if (Number.isNaN(t) || t < t0 || t > t1) continue;   // off-chart -> skip
        if (!byDate.has(f.date)) byDate.set(f.date, []);
        byDate.get(f.date).push(f);
    }
    const marks = [...byDate.entries()].map(([date, fs]) => {
        const x = X(ms(date)).toFixed(1);
        const lead = fs[0];                                  // newest that day -> click target
        // Most material form that day tints the line; dots stack importance-first.
        const ranked = [...fs].sort((a, b) => rankOf(a.form) - rankOf(b.form));
        const col = formColor(ranked[0].form);
        const forms = fs.map((f) => f.form).join(", ");
        const dots = ranked.slice(0, 3).map((f, i) =>
            `<circle class="pc-dot" cx="${x}" cy="${MARK_TOP + i * 7}" r="3"`
            + ` fill="${formColor(f.form)}"/>`).join("");
        return `<g class="pc-fmark" data-acc="${esc(lead.accession || "")}"`
            + ` data-date="${esc(date)}" role="button" tabindex="0"`
            + ` aria-label="${esc(forms)} on ${esc(date)}">`
            + `<title>${esc(forms)} · ${esc(date)}</title>`
            + `<rect class="pc-hit" x="${(+x - 5).toFixed(1)}" y="${MARK_TOP}"`
            + ` width="10" height="${(PLOT_BOT - MARK_TOP).toFixed(1)}" fill="transparent"/>`
            + `<line class="pc-fmark-line" x1="${x}" y1="${MARK_TOP}" x2="${x}"`
            + ` y2="${PLOT_BOT}" stroke="${col}"/>`
            + dots + `</g>`;
    }).join("");

    // Legend: one chip per form type present, ordered most-important first.
    const seen = new Set();
    const present = [];
    for (const f of filings) {
        if (!f.form || seen.has(f.form) || !byDate.has(f.date)) continue;
        seen.add(f.form);
        present.push(f.form);
    }
    present.sort((a, b) => (rankOf(a) - rankOf(b)) || a.localeCompare(b));
    const chips = present.map((form) =>
        `<span class="lg-chip"><i style="background:${formColor(form)}"></i>${esc(form)}</span>`);
    const legend = `<div class="chart-legend">${chips.join("")}</div>`;

    const svg = `<svg class="pc-svg" viewBox="0 0 ${W} ${H}" role="img"`
        + ` aria-label="Price history with filing markers">`
        + grid
        + `<polyline class="pc-line ${dirCls}" points="${pts}" fill="none"/>`
        + `<circle class="pc-last-dot ${dirCls}" cx="${lx.toFixed(1)}" cy="${ly.toFixed(1)}" r="3.2"/>`
        + `<text class="pc-axis pc-last-lbl" x="${(lx + 6).toFixed(1)}" y="${(ly + 3.5).toFixed(1)}">${lastLbl}</text>`
        + marks
        + `<text class="pc-axis pc-xstart" x="${PAD_L}" y="${H - 8}">${fmtDate(hist[0][0])}</text>`
        + `<text class="pc-axis pc-xend" x="${W - PAD_R}" y="${H - 8}" text-anchor="end">${fmtDate(hist[hist.length - 1][0])}</text>`
        + `</svg>`;

    return legend + svg;
}

// Change over a span's visible window (first vs last close) -> the labelled %
// shown next to the buttons, coloured to match the line. Slicing mirrors
// buildChartBody so the number always matches the drawn window.
function spanChange(full, spanDays) {
    const cutoff = ms(full[full.length - 1][0]) - spanDays * 86400000;
    let h = full.filter(([d]) => ms(d) >= cutoff);
    if (h.length < 2) h = full.slice(-2);
    const a = h[0][1], b = h[h.length - 1][1];
    const pct = a ? (b - a) / a * 100 : 0;
    return { pct, up: pct >= 0 };
}
function spanChgHtml(full, spanDays) {
    const { pct, up } = spanChange(full, spanDays);
    return `<span class="chart-span-chg ${up ? "chg-up" : "chg-down"}">`
        + `${up ? "▲" : "▼"} ${Math.abs(pct).toFixed(2)}%</span>`;
}

export function priceChartHtml(it) {
    const full = unpackHist(it.price && it.price.history);
    if (full.length < 2) return "";
    const spanBtns = SPANS.map(([lbl, days]) =>
        `<button type="button" class="chart-span${days === DEFAULT_SPAN ? " active" : ""}"`
        + ` data-days="${days}">${lbl}</button>`).join("");
    return `<article class="card chart-card">
        <div class="chart-head">
            <span class="chart-title">Price</span>
            <div class="chart-ctrls">
                ${spanChgHtml(full, DEFAULT_SPAN)}
                <div class="chart-spans">${spanBtns}</div>
            </div>
        </div>
        <div class="price-chart">${buildChartBody(full, it, DEFAULT_SPAN)}</div>
    </article>`;
}

// CSS.escape where available; accessions (digits + dashes) are selector-safe even
// without it, so the fallback only guards quotes/backslashes.
const cssEsc = (s) => (window.CSS && CSS.escape)
    ? CSS.escape(s) : String(s).replace(/["\\]/g, "\\$&");

// Reveal (un-collapse + expand summary + flash) one filing row. Scrolling is the
// caller's job so a multi-row jump scrolls once, not once per row.
function revealRow(row) {
    // If the row lives behind the "show N more" toggle, open it first.
    const moreList = row.closest(".more-rows");
    if (moreList) {
        const cb = moreList.closest(".card-filings")?.querySelector(".more-toggle");
        if (cb) cb.checked = true;
    }
    // Open the AI-summary expander if this row has one.
    const exp = row.querySelector(".fl-expand");
    if (exp) exp.checked = true;
    row.classList.remove("flash");
    void row.offsetWidth;                                  // restart the flash animation
    row.classList.add("flash");
}

export function wireChart(companyEl, it) {
    // Listen on the .price-chart container (stable), not the SVG: switching span
    // replaces the SVG, but the container — and these listeners — persist.
    const box = companyEl.querySelector(".price-chart");
    if (!box) return;
    const jump = (g) => {
        // A marker stands for a whole filing day, so highlight EVERY filing that
        // day (the stacked dots) -- not just the newest. Match by date; fall back
        // to the lead accession only if the date attr is somehow missing.
        const date = g.getAttribute("data-date");
        const acc = g.getAttribute("data-acc");
        let rows = date
            ? [...companyEl.querySelectorAll(`.flrow[data-date="${cssEsc(date)}"]`)]
            : [];
        if (!rows.length && acc) {
            const r = companyEl.querySelector(`.flrow[data-acc="${cssEsc(acc)}"]`);
            if (r) rows = [r];
        }
        if (!rows.length) return;
        // Clear any leftover flash from a previous click so only this marker's
        // rows are highlighted (the class outlives its 1.4s animation otherwise).
        companyEl.querySelectorAll(".flrow.flash")
            .forEach((r) => r.classList.remove("flash"));
        rows.forEach(revealRow);
        rows[0].scrollIntoView({ behavior: "smooth", block: "center" });   // top (newest)
    };
    box.addEventListener("click", (e) => {
        const g = e.target.closest(".pc-fmark");
        if (g) jump(g);
    });
    // Keyboard: markers are focusable (tabindex) -> Enter/Space activates.
    box.addEventListener("keydown", (e) => {
        if (e.key !== "Enter" && e.key !== " ") return;
        const g = e.target.closest(".pc-fmark");
        if (g) { e.preventDefault(); jump(g); }
    });

    // Span buttons: re-slice the same series (no refetch) and swap the chart body.
    const spans = companyEl.querySelector(".chart-spans");
    if (spans) spans.addEventListener("click", (e) => {
        const btn = e.target.closest(".chart-span");
        if (!btn) return;
        const full = unpackHist(it.price && it.price.history);
        if (full.length < 2) return;
        const days = +btn.getAttribute("data-days");
        box.innerHTML = buildChartBody(full, it, days);
        spans.querySelectorAll(".chart-span")
            .forEach((b) => b.classList.toggle("active", b === btn));
        const lbl = companyEl.querySelector(".chart-span-chg");
        if (lbl) lbl.outerHTML = spanChgHtml(full, days);   // refresh labelled % for the span
    });
}
