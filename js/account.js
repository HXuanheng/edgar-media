// Account / profile page (#/account). A signed-in user edits their PUBLIC profile:
// display name, real name, website, profession, investment style, short bio. The
// login email is shown read-only — it lives in auth.users and is never stored in
// `profiles` (which is world-readable), so it stays private.
//
// Everything written here goes to public.profiles; the column-level UPDATE grant in
// supabase/schema.sql is what lets the client save these fields.

import { supabase, isConfigured, initClient } from "./supabaseClient.js";
import { getUser, openModal, onChange, reloadProfile } from "./auth.js";
import { esc } from "./util.js";

// Investment-style options (stored as plain text). Kept short + recognisable; can
// later flavour the persona-agents or filter the feed.
const STYLES = ["", "Value", "Growth", "Momentum / trend", "Index / passive",
    "Dividend / income", "Day trading", "Swing trading", "Options", "Crypto", "Other"];

const FIELDS = [
    { key: "display_name", label: "Display name", type: "text", hint: "Shown next to your comments." },
    { key: "first_name", label: "First name", type: "text" },
    { key: "last_name", label: "Surname", type: "text" },
    { key: "website", label: "Website", type: "url", placeholder: "https://" },
    { key: "profession", label: "Profession", type: "text" },
    { key: "investment_style", label: "Investment style", type: "select", options: STYLES },
    { key: "background", label: "Background", type: "textarea", hint: "A short bio." },
];

function fieldHtml(f, p) {
    const val = p[f.key] || "";
    const hint = f.hint ? `<span class="acc-hint">${esc(f.hint)}</span>` : "";
    if (f.type === "textarea")
        return `<label class="acc-field"><span class="acc-label">${esc(f.label)}</span>
            <textarea name="${f.key}" rows="4" maxlength="2000">${esc(val)}</textarea>${hint}</label>`;
    if (f.type === "select")
        return `<label class="acc-field"><span class="acc-label">${esc(f.label)}</span>
            <select name="${f.key}">${f.options.map((o) =>
                `<option value="${esc(o)}"${o === val ? " selected" : ""}>${esc(o || "—")}</option>`).join("")}</select>${hint}</label>`;
    return `<label class="acc-field"><span class="acc-label">${esc(f.label)}</span>
        <input type="${f.type}" name="${f.key}" value="${esc(val)}" maxlength="200"
            placeholder="${esc(f.placeholder || "")}" autocomplete="off">${hint}</label>`;
}

export async function render(container) {
    if (!container) return;
    if (!isConfigured) {
        container.innerHTML = `<p class="cmt-empty">Accounts aren’t configured.</p>`;
        return;
    }

    const user = getUser();
    if (!user) {
        container.innerHTML = `<div class="account-page">
            <a class="back-link" href="#/">← Back</a>
            <h1 class="account-title">Your account</h1>
            <p class="cmt-empty">Sign in to view and edit your profile.</p>
            <button class="auth-btn auth-btn-primary" data-acc="signin">Sign in</button>
        </div>`;
        container.querySelector('[data-acc="signin"]')?.addEventListener("click", openModal);
        // Session may still be resolving on a fresh load / direct deep link — re-render
        // once the user appears (and only if we're still on the account route).
        const off = onChange((u) => {
            if (u && location.hash.replace(/^#/, "").match(/^\/account\/?$/i)) { off(); render(container); }
        });
        return;
    }

    await initClient();
    const { data: prof } = await supabase
        .from("profiles")
        .select("display_name, first_name, last_name, website, profession, background, investment_style")
        .eq("id", user.id)
        .maybeSingle();
    const p = prof || {};

    container.innerHTML = `<div class="account-page">
        <a class="back-link" href="#/">← Back</a>
        <h1 class="account-title">Your account</h1>
        <p class="acc-email">Signed in as <strong>${esc(user.email || "")}</strong> <span class="acc-private">· email is private</span></p>
        <form class="acc-form" data-acc="form">
            ${FIELDS.map((f) => fieldHtml(f, p)).join("")}
            <p class="acc-note">Profile details are public — anyone can see them next to your comments. Your email is never shown.</p>
            <div class="acc-actions">
                <button type="submit" class="auth-btn auth-btn-primary">Save</button>
                <span class="acc-flash" data-acc="flash" hidden></span>
            </div>
        </form>
    </div>`;

    const form = container.querySelector('[data-acc="form"]');
    const flash = container.querySelector('[data-acc="flash"]');
    form.addEventListener("submit", async (e) => {
        e.preventDefault();
        const fd = new FormData(form);
        const patch = {};
        FIELDS.forEach((f) => { patch[f.key] = (fd.get(f.key) || "").toString().trim() || null; });
        const btn = form.querySelector('button[type="submit"]');
        btn.disabled = true;
        const { error } = await supabase.from("profiles").update(patch).eq("id", user.id);
        btn.disabled = false;
        flash.hidden = false;
        flash.textContent = error ? `Couldn’t save: ${error.message}` : "Saved ✓";
        flash.classList.toggle("cmt-flash-err", !!error);
        if (!error) reloadProfile();   // refresh the header name/avatar
    });
    window.scrollTo(0, 0);
}
