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

export async function render(container, userId) {
    if (!container) return;
    if (!isConfigured) {
        container.innerHTML = `<p class="cmt-empty">Accounts aren’t configured.</p>`;
        return;
    }

    await initClient();
    const { data: p } = await supabase
        .from("profiles")
        .select("id, display_name, avatar_url, first_name, last_name, website, profession, background, investment_style, created_at, role")
        .eq("id", userId)
        .maybeSingle();

    if (!p) {
        container.innerHTML = `<div class="profile-page">
            <a class="back-link" href="#/">← Back</a>
            <p class="cmt-empty">This profile doesn’t exist.</p></div>`;
        window.scrollTo(0, 0);
        return;
    }

    const name = p.display_name || "anon";
    const realName = [p.first_name, p.last_name].filter(Boolean).join(" ");
    const avatar = p.avatar_url
        ? `<img class="acc-avatar-img" src="${esc(p.avatar_url)}" alt="" referrerpolicy="no-referrer">`
        : `<span class="acc-avatar-img acc-avatar-fallback">${esc(name.slice(0, 1).toUpperCase())}</span>`;
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
        <a class="back-link" href="#/">← Back</a>
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
    window.scrollTo(0, 0);
}
