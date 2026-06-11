// Account / profile page (#/account). A signed-in user edits their PUBLIC profile:
// photo, display name, real name, website, profession, investment style, short bio.
// The login email is shown read-only — it lives in auth.users and is never stored in
// `profiles` (which is world-readable), so it stays private.
//
// Text fields write to public.profiles (column-level UPDATE grant in schema.sql).
// The photo uploads to the public `avatars` Storage bucket and its public URL is
// saved to profiles.avatar_url (which already exists + is grantable), so the photo
// works independently of the text-field columns.

import { supabase, isConfigured, initClient } from "./supabaseClient.js";
import { getUser, openModal, onChange, reloadProfile } from "./auth.js";
import { esc } from "./util.js";

// Investment-style options (stored as plain text). Kept short + recognisable; can
// later flavour the persona-agents or filter the feed.
const STYLES = ["", "Value", "Growth", "Momentum / trend", "Index / passive",
    "Dividend / income", "Day trading", "Swing trading", "Options", "Crypto", "Other"];

// Only the display name really matters (it's shown on comments); everything else is
// optional and labelled as such.
const FIELDS = [
    { key: "display_name", label: "Display name", type: "text", hint: "Shown next to your comments." },
    { key: "first_name", label: "First name", type: "text", opt: true },
    { key: "last_name", label: "Surname", type: "text", opt: true },
    { key: "website", label: "Website", type: "url", placeholder: "https://", opt: true },
    { key: "profession", label: "Profession", type: "text", opt: true },
    { key: "investment_style", label: "Investment style", type: "select", options: STYLES, opt: true },
    { key: "background", label: "Background", type: "textarea", hint: "A short bio.", opt: true },
];

const AVATAR_BUCKET = "avatars";
const MAX_DIM = 512;          // downscale the long edge to this before upload
const MAX_FILE = 8 * 1024 * 1024;   // reject absurd inputs before we even decode them

function labelHtml(f) {
    const opt = f.opt ? ` <span class="acc-opt">(optional)</span>` : "";
    return `<span class="acc-label">${esc(f.label)}${opt}</span>`;
}

function fieldHtml(f, p) {
    const val = p[f.key] || "";
    const hint = f.hint ? `<span class="acc-hint">${esc(f.hint)}</span>` : "";
    if (f.type === "textarea")
        return `<label class="acc-field">${labelHtml(f)}
            <textarea name="${f.key}" rows="4" maxlength="2000">${esc(val)}</textarea>${hint}</label>`;
    if (f.type === "select")
        return `<label class="acc-field">${labelHtml(f)}
            <select name="${f.key}">${f.options.map((o) =>
                `<option value="${esc(o)}"${o === val ? " selected" : ""}>${esc(o || "—")}</option>`).join("")}</select>${hint}</label>`;
    return `<label class="acc-field">${labelHtml(f)}
        <input type="${f.type}" name="${f.key}" value="${esc(val)}" maxlength="200"
            placeholder="${esc(f.placeholder || "")}" autocomplete="off">${hint}</label>`;
}

function avatarInner(p, user) {
    return p.avatar_url
        ? `<img class="acc-avatar-img" src="${esc(p.avatar_url)}" alt="" referrerpolicy="no-referrer">`
        : `<span class="acc-avatar-img acc-avatar-fallback">${esc((p.display_name || user.email || "?").slice(0, 1).toUpperCase())}</span>`;
}

// Decode the chosen image, draw it onto a canvas scaled to <=MAX_DIM on the long edge,
// and re-encode as a modest JPEG. Keeps avatars small + uniform regardless of source.
async function shrinkImage(file) {
    const dataUrl = await new Promise((res, rej) => {
        const r = new FileReader();
        r.onload = () => res(r.result);
        r.onerror = () => rej(new Error("couldn't read file"));
        r.readAsDataURL(file);
    });
    const img = await new Promise((res, rej) => {
        const i = new Image();
        i.onload = () => res(i);
        i.onerror = () => rej(new Error("not a valid image"));
        i.src = dataUrl;
    });
    const scale = Math.min(1, MAX_DIM / Math.max(img.width, img.height));
    const w = Math.max(1, Math.round(img.width * scale));
    const h = Math.max(1, Math.round(img.height * scale));
    const canvas = document.createElement("canvas");
    canvas.width = w; canvas.height = h;
    canvas.getContext("2d").drawImage(img, 0, 0, w, h);
    return new Promise((res, rej) =>
        canvas.toBlob((b) => (b ? res(b) : rej(new Error("encode failed"))), "image/jpeg", 0.85));
}

async function handleUpload(file, user, p, avBox, status) {
    if (!file) return;
    if (!file.type.startsWith("image/") || file.size > MAX_FILE) {
        status.textContent = "Pick an image under 8 MB."; return;
    }
    status.textContent = "Uploading…";
    try {
        const blob = await shrinkImage(file);
        const path = `${user.id}/avatar.jpg`;        // one file per user, overwritten
        const { error: upErr } = await supabase.storage.from(AVATAR_BUCKET)
            .upload(path, blob, { upsert: true, contentType: "image/jpeg", cacheControl: "3600" });
        if (upErr) throw upErr;
        const { data: pub } = supabase.storage.from(AVATAR_BUCKET).getPublicUrl(path);
        const url = `${pub.publicUrl}?t=${Date.now()}`;   // cache-bust the reused path
        const { error: dbErr } = await supabase.from("profiles").update({ avatar_url: url }).eq("id", user.id);
        if (dbErr) throw dbErr;
        p.avatar_url = url;
        avBox.innerHTML = `<img class="acc-avatar-img" src="${esc(url)}" alt="">`;
        status.textContent = "Photo saved ✓";
        reloadProfile();                              // refresh the header avatar
    } catch (err) {
        status.textContent = `Upload failed: ${err.message || err}`;
    }
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
        .select("display_name, avatar_url, first_name, last_name, website, profession, background, investment_style")
        .eq("id", user.id)
        .maybeSingle();
    const p = prof || {};

    container.innerHTML = `<div class="account-page">
        <a class="back-link" href="#/">← Back</a>
        <h1 class="account-title">Your account</h1>
        <p class="acc-email">Signed in as <strong>${esc(user.email || "")}</strong> <span class="acc-private">· email is private</span></p>
        <p class="acc-viewpublic"><a href="#/u/${esc(user.id)}">View public profile ↗</a></p>

        <div class="acc-avatar-row">
            <span class="acc-avatar" data-acc="avatar">${avatarInner(p, user)}</span>
            <div class="acc-avatar-ctl">
                <label class="auth-btn acc-upload">Change photo
                    <input type="file" accept="image/*" data-acc="file" hidden>
                </label>
                <span class="acc-avatar-status" data-acc="avstatus"></span>
            </div>
        </div>

        <form class="acc-form" data-acc="form">
            ${FIELDS.map((f) => fieldHtml(f, p)).join("")}
            <p class="acc-note">Profile details are public — anyone can see them next to your comments. Your email is never shown.</p>
            <div class="acc-actions">
                <button type="submit" class="auth-btn auth-btn-primary">Save</button>
                <span class="acc-flash" data-acc="flash" hidden></span>
            </div>
        </form>
    </div>`;

    // photo upload
    const avBox = container.querySelector('[data-acc="avatar"]');
    const avStatus = container.querySelector('[data-acc="avstatus"]');
    container.querySelector('[data-acc="file"]')?.addEventListener("change", (e) =>
        handleUpload(e.target.files?.[0], user, p, avBox, avStatus));

    // text fields
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
        if (!error) reloadProfile();   // refresh the header name
    });
    window.scrollTo(0, 0);
}
