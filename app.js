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
    if (nameMatch === "mismatch") {
        return `<div class="filing-stale">
            <span class="warn">⚠ unverified ticker/name match</span><br>
            <span class="filing-form">${esc(f.form)}</span>
            <span class="filing-meta">· ${esc(meta)}</span><br>
            <a class="filing-meta" href="${esc(f.index_url)}" target="_blank" rel="noopener">view on SEC ↗</a>
        </div>`;
    }
    if (f.fresh) {
        return `<div class="filing-fresh">
            <span class="badge">🔥 ${esc(f.form)} · ${esc(meta)}</span><br>
            <a href="${esc(f.index_url)}" target="_blank" rel="noopener">${esc(f.desc || f.form)}</a>
        </div>`;
    }
    return `<div class="filing-stale">
        <span class="filing-form">${esc(f.form)}</span>
        <span class="filing-meta">· ${esc(meta)}</span><br>
        <a class="filing-meta" href="${esc(f.index_url)}" target="_blank" rel="noopener">view on SEC ↗</a>
    </div>`;
}

function cardHtml(it, i) {
    const verified = it.name_match === "verified";
    const hot = verified && it.filing && it.filing.fresh;
    const coName = it.display_name || it.name;
    const redditUrl = `https://www.reddit.com/r/wallstreetbets/search/?q=%24${encodeURIComponent(it.ticker)}`;
    return `<article class="card ${hot ? "hot" : ""}">
        <div class="rank">${i + 1}</div>
        <div class="tick">
            <div class="tick-row">
                <a class="ticker" href="${redditUrl}" target="_blank" rel="noopener">$${esc(it.ticker)}</a>
                <span class="coname">${esc(coName)}</span>
            </div>
            <div class="attention">${momentum(it)}</div>
        </div>
        <div class="filing">${filingHtml(it.filing, it.name_match)}</div>
    </article>`;
}

// --- load ---
fetch("./data/trending.json", { cache: "no-store" })
    .then((r) => { if (!r.ok) throw new Error(r.status); return r.json(); })
    .then((data) => {
        $("#updated").textContent =
            `Updated ${timeAgo(data.updated_utc)} · ${data.count} trending tickers · ` +
            `attention from Reddit, filings from SEC EDGAR`;
        const items = data.items || [];
        $("#list").innerHTML = items.length
            ? items.map(cardHtml).join("")
            : `<div class="loading">No data yet.</div>`;
    })
    .catch((e) => {
        $("#updated").textContent = "";
        $("#list").innerHTML =
            `<div class="loading">Couldn't load data (${esc(e.message)}). ` +
            `The hourly updater may not have run yet.</div>`;
    });
