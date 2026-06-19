// Public profile page (#/u/<user_id>). Read-only view of any user's PUBLIC profile
// (the profiles table is world-readable). NEVER shows email — it isn't in this table
// (it lives in auth.users), so there's nothing private to leak here.

import { supabase, isConfigured, initClient } from "./supabaseClient.js";
import { getUser } from "./auth.js";
import { esc } from "./util.js";

function monthYear(iso) {
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return "";
    return d.toLocaleDateString(undefined, { month: "short", year: "numeric" });
}

// Normalise a user-entered website to a safe http(s) link, or "" if it isn't one.
function safeUrl(raw) {
    const s = (raw || "").trim();
    if (!s) return "";
    const withScheme = /^https?:\/\//i.test(s) ? s : `https://${s}`;
    try {
        const u = new URL(withScheme);
        return (u.protocol === "http:" || u.protocol === "https:") ? u.href : "";
    } catch { return ""; }
}

// AI persona-agent transparency dials, rendered as labeled 0..100 bars. The end
// labels orient each scale (e.g. risk_aversion 100 = risk-averse).
const AGENT_DIALS = [
    { key: "risk_aversion", label: "Risk aversion", lo: "risk-seeking", hi: "risk-averse" },
    { key: "financial_literacy", label: "Financial literacy", lo: "casual", hi: "expert" },
    { key: "creativity", label: "Creativity (temperature)", lo: "literal", hi: "creative" },
    { key: "diligence", label: "Diligence", lo: "skims", hi: "thorough" },
    { key: "time_horizon", label: "Time horizon", lo: "day-trade", hi: "decades" },
    { key: "skepticism", label: "Skepticism", lo: "trusting", hi: "skeptical" },
    { key: "verbosity", label: "Verbosity", lo: "terse", hi: "wordy" },
];
export const AGENT_STYLE_LABELS = {
    value: "Value investor", momentum: "Momentum trader",
    forensic_short: "Forensic short-seller", quant: "Quant", macro: "Macro strategist",
};

function dialRow(d, dials) {
    const v = Math.max(0, Math.min(100, Number(dials?.[d.key] ?? 0)));
    return `<div class="agent-dial">
        <div class="agent-dial-label"><span>${esc(d.label)}</span><span class="agent-dial-val">${v}</span></div>
        <div class="agent-dial-bar"><span style="width:${v}%"></span></div>
        <div class="agent-dial-ends"><span>${esc(d.lo)}</span><span>${esc(d.hi)}</span></div>
    </div>`;
}

// Full transparency dashboard for a fictional agent: identity, model+provider,
// personality dials, and the VERBATIM system prompt (nothing hidden) + disclaimer.
function agentCardHtml(p, name, avatar) {
    const m = p.agent_meta || {};
    const dials = m.dials || {};
    const styleLabel = AGENT_STYLE_LABELS[m.style] || m.style || "";
    // Built fresh from the agent's name (not the DB-stored m.disclaimer, which may be
    // a stale seed) so the wording stays in sync with scripts/seed_agents.py's template.
    const disclaimer = `${name} is a fictional AI character for education and entertainment. Its takes are auto-generated and are NOT investment advice.`;
    const row = (label, valueHtml) =>
        valueHtml ? `<div class="pf-row"><dt>${esc(label)}</dt><dd>${valueHtml}</dd></div>` : "";
    const metaRows = [
        row("Style", styleLabel ? esc(styleLabel) : ""),
        row("Model", m.model ? `<code>${esc(m.model)}</code>` : ""),
        row("Provider", m.provider ? esc(m.provider) : ""),
    ].join("");
    return `<div class="profile-page">
        <a class="back-link" href="#/" data-back>← Back</a>
        <article class="card profile-card agent-profile">
            <div class="profile-head">
                <span class="acc-avatar profile-avatar">${avatar}</span>
                <div class="profile-id">
                    <h1 class="profile-name">${esc(name)}<span class="profile-badge agent-badge-lg">🤖 AI agent</span></h1>
                    ${m.tagline ? `<p class="agent-tagline">“${esc(m.tagline)}”</p>` : ""}
                </div>
            </div>
            <p class="agent-disclaimer-banner">${esc(disclaimer)}</p>
            ${metaRows ? `<dl class="profile-fields">${metaRows}</dl>` : ""}
            <h2 class="agent-section-title">Personality dials</h2>
            <div class="agent-dials">${AGENT_DIALS.map((d) => dialRow(d, dials)).join("")}</div>
            ${m.system_prompt ? `<details class="agent-sysprompt-wrap">
                <summary class="agent-section-title">System prompt</summary>
                <pre class="agent-sysprompt">${esc(m.system_prompt)}</pre>
            </details>` : ""}
        </article>
    </div>`;
}

export async function render(container, userId) {
    if (!container) return;
    if (!isConfigured) {
        container.innerHTML = `<p class="cmt-empty">Accounts aren’t configured.</p>`;
        return;
    }

    await initClient();
    const { data: p } = await supabase
        .from("profiles")
        .select("id, display_name, avatar_url, first_name, last_name, website, profession, background, investment_style, created_at, role, is_agent, agent_meta")
        .eq("id", userId)
        .maybeSingle();

    if (!p) {
        container.innerHTML = `<div class="profile-page">
            <a class="back-link" href="#/" data-back>← Back</a>
            <p class="cmt-empty">This profile doesn’t exist.</p></div>`;
        if (location.hash.replace(/^#/, "").startsWith("/u/")) window.scrollTo(0, 0);   // don't clobber scroll if we've navigated away
        return;
    }

    const name = p.display_name || "anon";
    const realName = [p.first_name, p.last_name].filter(Boolean).join(" ");
    const avatar = p.avatar_url
        ? `<img class="acc-avatar-img" src="${esc(p.avatar_url)}" alt="" referrerpolicy="no-referrer">`
        : `<span class="acc-avatar-img acc-avatar-fallback">${esc(name.slice(0, 1).toUpperCase())}</span>`;

    // Fictional AI persona-agent: render the transparency dashboard instead of the
    // human profile fields.
    if (p.is_agent) {
        container.innerHTML = agentCardHtml(p, name, avatar);
        if (location.hash.replace(/^#/, "").startsWith("/u/")) window.scrollTo(0, 0);
        return;
    }

    const url = safeUrl(p.website);
    const since = monthYear(p.created_at);
    const isMe = getUser()?.id === p.id;

    // Clear "Label: value" rows; each only shows when its field is filled.
    const row = (label, valueHtml) =>
        valueHtml ? `<div class="pf-row"><dt>${esc(label)}</dt><dd>${valueHtml}</dd></div>` : "";
    const websiteHtml = url
        ? `<a href="${esc(url)}" target="_blank" rel="noopener nofollow">${esc(url.replace(/^https?:\/\//, ""))}</a>` : "";
    const fields = [
        row("Name", realName ? esc(realName) : ""),
        row("Profession", p.profession ? esc(p.profession) : ""),
        row("Investment style", p.investment_style ? esc(p.investment_style) : ""),
        row("Website", websiteHtml),
        row("About", p.background ? `<span class="pf-bio">${esc(p.background)}</span>` : ""),
        row("Member since", since ? esc(since) : ""),
    ].join("");

    container.innerHTML = `<div class="profile-page">
        <a class="back-link" href="#/" data-back>← Back</a>
        <article class="card profile-card">
            <div class="profile-head">
                <span class="acc-avatar profile-avatar">${avatar}</span>
                <div class="profile-id">
                    <h1 class="profile-name">${esc(name)}${p.role === "admin" ? `<span class="profile-badge">admin</span>` : ""}</h1>
                </div>
            </div>
            ${fields ? `<dl class="profile-fields">${fields}</dl>` : `<p class="cmt-empty">No profile details yet.</p>`}
            ${isMe ? `<p class="profile-edit"><a href="#/account">Edit your profile →</a></p>` : ""}
        </article>
    </div>`;
    if (location.hash.replace(/^#/, "").startsWith("/u/")) window.scrollTo(0, 0);   // don't clobber scroll if we've navigated away
}
