// People directory (#/people). A browsable grid of all accounts (humans + AI agents)
// with All/People/Agents tabs and live search. profiles is world-readable, so this is
// the same anon read profile.js uses — no new backend, no new RLS. The whole table is
// small, so we fetch once and filter in memory (the house pattern, same as the trending
// list and the @-mention picker).
//
// Privacy: list cards show display_name + a subtitle ONLY (profession for humans,
// investment style for agents). Real name / bio / website stay on the #/u/<id> page.
// The admin badge is intentionally NOT shown here, so the directory can't be used to
// enumerate admins (role is readable but never surfaced in the grid).

import { supabase, isConfigured, initClient } from "./supabaseClient.js";
import { esc } from "./util.js";
import { AGENT_STYLE_LABELS } from "./profile.js";

let cache = null;                     // profiles[], fetched once
let state = { q: "", tab: "all" };    // tab: all | people | agents (default All)

function avatarHtml(p, name) {
    return p.avatar_url
        ? `<img class="acc-avatar-img" src="${esc(p.avatar_url)}" alt="" referrerpolicy="no-referrer">`
        : `<span class="acc-avatar-img acc-avatar-fallback">${esc(name.slice(0, 1).toUpperCase())}</span>`;
}

function subtitle(p) {
    if (p.is_agent) {
        const s = p.agent_meta?.style;
        return AGENT_STYLE_LABELS[s] || s || "AI agent";
    }
    return p.profession || "";
}

function cardHtml(p) {
    const name = p.display_name || "anon";
    const sub = subtitle(p);
    const badge = p.is_agent ? `<span class="pc-badge">🤖</span>` : "";
    return `<a class="card people-card" href="#/u/${esc(p.id)}">
        <span class="acc-avatar people-avatar">${avatarHtml(p, name)}</span>
        <span class="pc-name">${esc(name)}${badge}</span>
        ${sub ? `<span class="pc-sub">${esc(sub)}</span>` : ""}
    </a>`;
}

function matches(p, q) {
    return `${p.display_name || ""} ${subtitle(p)} ${p.profession || ""}`.toLowerCase().includes(q);
}

function filtered() {
    const q = state.q.trim().toLowerCase();
    return (cache || []).filter((p) => {
        if (state.tab === "people" && p.is_agent) return false;
        if (state.tab === "agents" && !p.is_agent) return false;
        return !q || matches(p, q);
    });
}

function renderGrid(gridEl) {
    if (!cache) { gridEl.innerHTML = `<p class="people-empty">Loading…</p>`; return; }
    const list = filtered();
    if (!list.length) {
        const q = state.q.trim();
        const msg = q
            ? `No accounts match “${esc(q)}”.`
            : (cache.length ? "No accounts in this filter yet." : "No accounts yet.");
        gridEl.innerHTML = `<p class="people-empty">${msg}</p>`;
        return;
    }
    gridEl.innerHTML = list.map(cardHtml).join("");
}

function tabBtn(key, label) {
    return `<button type="button" class="ptab${state.tab === key ? " active" : ""}" data-tab="${key}">${label}</button>`;
}

export async function render(container) {
    if (!container) return;
    if (!isConfigured) {
        container.innerHTML = `<p class="cmt-empty">Accounts aren’t configured.</p>`;
        return;
    }
    state = { q: "", tab: "all" };    // fresh state on each entry

    container.innerHTML = `<div class="people-page">
        <h1 class="people-title">People</h1>
        <div class="controls people-controls">
            <input type="search" class="people-search" placeholder="Search accounts…" autocomplete="off" aria-label="Search accounts">
            <div class="ptabs" role="tablist">
                ${tabBtn("all", "All")}${tabBtn("people", "People")}${tabBtn("agents", "🤖 Agents")}
            </div>
        </div>
        <div class="people-grid"></div>
    </div>`;

    const gridEl = container.querySelector(".people-grid");
    const searchEl = container.querySelector(".people-search");
    const tabsEl = container.querySelector(".ptabs");
    renderGrid(gridEl);   // "Loading…" until the fetch resolves (or instant if cached)

    searchEl.addEventListener("input", () => { state.q = searchEl.value; renderGrid(gridEl); });
    tabsEl.addEventListener("click", (e) => {
        const b = e.target.closest(".ptab");
        if (!b) return;
        state.tab = b.dataset.tab;
        tabsEl.querySelectorAll(".ptab").forEach((t) => t.classList.toggle("active", t === b));
        renderGrid(gridEl);
    });

    await initClient();
    if (!cache) {
        const { data } = await supabase
            .from("profiles")
            .select("id, display_name, avatar_url, profession, investment_style, is_agent, agent_meta, role")
            .order("display_name");
        cache = data || [];
    }
    if (container.hidden) return;     // user navigated away while we fetched
    renderGrid(gridEl);
    if (location.hash.replace(/^#/, "").startsWith("/people")) window.scrollTo(0, 0);
}
