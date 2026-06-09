// Auth: session state, the header sign-in/out slot, and the sign-in modal.
// Supports Google OAuth, GitHub OAuth, and magic-link email. Stays fully dormant
// when Supabase isn't configured (header renders exactly as before).

import { supabase, isConfigured, initClient } from "./supabaseClient.js";
import { SUPABASE_URL, SUPABASE_ANON_KEY } from "./config.js";
import { esc } from "./util.js";

const HASH_KEY = "post_signin_hash";   // remember the deep link across the OAuth round-trip

// Which sign-in methods to show, auto-detected from the project so we never show a
// button for a provider that isn't enabled (otherwise clicking it errors
// "provider is not enabled"). Defaults to email-only until settings load.
let providers = { google: false, github: false, email: true };
async function fetchProviders() {
    try {
        const r = await fetch(`${SUPABASE_URL}/auth/v1/settings`, { headers: { apikey: SUPABASE_ANON_KEY } });
        const s = await r.json();
        providers = {
            google: !!s.external?.google,
            github: !!s.external?.github,
            email:  s.external?.email !== false,
        };
    } catch { /* keep defaults -> only magic link shows */ }
}

let currentUser = null;
let currentProfile = null;
let started = false;
const listeners = new Set();

export function getUser() { return currentUser; }
export function getProfile() { return currentProfile; }
export function isAdmin() { return currentProfile?.role === "admin"; }

// Subscribe to auth changes; fires immediately with the current state, returns an
// unsubscribe fn.
export function onChange(cb) {
    listeners.add(cb);
    cb(currentUser, currentProfile);
    return () => listeners.delete(cb);
}
function emit() { listeners.forEach((cb) => cb(currentUser, currentProfile)); }

export async function signInGoogle() { return oauth("google"); }
export async function signInGitHub() { return oauth("github"); }
async function oauth(provider) {
    localStorage.setItem(HASH_KEY, location.hash || "");
    await initClient();
    await supabase.auth.signInWithOAuth({
        provider,
        options: { redirectTo: location.origin + location.pathname },
    });
}
export async function signInMagicLink(email) {
    localStorage.setItem(HASH_KEY, location.hash || "");
    await initClient();
    return supabase.auth.signInWithOtp({
        email,
        options: { emailRedirectTo: location.origin + location.pathname },
    });
}
export async function signOut() { await initClient(); await supabase.auth.signOut(); }

async function loadProfile(user) {
    if (!user) return null;
    const { data } = await supabase
        .from("profiles")
        .select("id, display_name, avatar_url, role")
        .eq("id", user.id)
        .maybeSingle();
    if (data) return data;
    // trigger hasn't landed yet (rare race on first sign-in) — fall back to metadata.
    const m = user.user_metadata || {};
    return {
        id: user.id,
        display_name: m.full_name || m.name || m.user_name || (user.email || "").split("@")[0],
        avatar_url: m.avatar_url || null,
        role: "user",
    };
}

export async function init() {
    if (started || !isConfigured) return;
    started = true;

    await initClient();   // lazily loads the SDK; does not block app.js's data fetch
    await fetchProviders();

    // Pick up an existing session, then keep it in sync.
    const { data: { session } } = await supabase.auth.getSession();
    currentUser = session?.user ?? null;
    currentProfile = await loadProfile(currentUser);
    renderSlot();
    buildModal();
    emit();

    supabase.auth.onAuthStateChange(async (event, sess) => {
        currentUser = sess?.user ?? null;
        currentProfile = await loadProfile(currentUser);
        // After OAuth / magic-link return, strip the ?code=… and restore the deep
        // link the user was on when they hit "Sign in".
        if (event === "SIGNED_IN") {
            const saved = localStorage.getItem(HASH_KEY);
            localStorage.removeItem(HASH_KEY);
            if (location.search.includes("code=")) {
                history.replaceState({}, "", location.origin + location.pathname);
            }
            if (saved && saved !== location.hash) location.hash = saved;   // fires the router
        }
        renderSlot();
        emit();
    });
}

// ───────────────────────── header slot ─────────────────────────
function renderSlot() {
    const slot = document.getElementById("auth-slot");
    if (!slot) return;
    if (currentUser) {
        const name = currentProfile?.display_name || "Account";
        const avatar = currentProfile?.avatar_url
            ? `<img class="auth-avatar" src="${esc(currentProfile.avatar_url)}" alt="" referrerpolicy="no-referrer">`
            : `<span class="auth-avatar auth-avatar-fallback">${esc(name.slice(0, 1).toUpperCase())}</span>`;
        slot.innerHTML = `
            <span class="auth-me" title="${esc(name)}">${avatar}<span class="auth-name">${esc(name)}</span></span>
            <button class="auth-btn" data-auth="signout">Sign out</button>`;
        slot.querySelector('[data-auth="signout"]').addEventListener("click", signOut);
    } else {
        slot.innerHTML = `<button class="auth-btn" data-auth="open">Sign in</button>`;
        slot.querySelector('[data-auth="open"]').addEventListener("click", openModal);
    }
}

// ───────────────────────── sign-in modal ───────────────────────
function buildModal() {
    const host = document.getElementById("auth-modal");
    if (!host || host.dataset.built) return;
    host.dataset.built = "1";
    const oauthBtns = [
        providers.google ? `<button class="oauth-btn" data-auth="google">Continue with Google</button>` : "",
        providers.github ? `<button class="oauth-btn" data-auth="github">Continue with GitHub</button>` : "",
    ].join("");
    const divider = oauthBtns ? `<div class="modal-or"><span>or</span></div>` : "";
    host.innerHTML = `
        <div class="modal-backdrop" data-auth="close"></div>
        <div class="modal-card" role="dialog" aria-modal="true" aria-label="Sign in">
            <button class="modal-x" data-auth="close" aria-label="Close">✕</button>
            <h2 class="modal-title">Sign in to comment</h2>
            ${oauthBtns}${divider}
            <form class="magic-form" data-auth="magic">
                <input type="email" name="email" placeholder="you@email.com" required autocomplete="email">
                <button type="submit" class="auth-btn auth-btn-primary">Send magic link</button>
            </form>
            <p class="modal-msg" data-auth="msg" hidden></p>
        </div>`;

    host.querySelectorAll('[data-auth="close"]').forEach((el) =>
        el.addEventListener("click", closeModal));
    host.querySelector('[data-auth="google"]')?.addEventListener("click", signInGoogle);
    host.querySelector('[data-auth="github"]')?.addEventListener("click", signInGitHub);
    host.querySelector('[data-auth="magic"]').addEventListener("submit", async (e) => {
        e.preventDefault();
        const email = e.target.email.value.trim();
        const msg = host.querySelector('[data-auth="msg"]');
        if (!email) return;
        const { error } = await signInMagicLink(email);
        msg.hidden = false;
        msg.textContent = error
            ? `Couldn't send link: ${error.message}`
            : "Check your email for the sign-in link.";
        msg.classList.toggle("modal-msg-err", !!error);
    });
    document.addEventListener("keydown", (e) => {
        if (e.key === "Escape" && !host.hidden) closeModal();
    });
}
export function openModal() {
    const host = document.getElementById("auth-modal");
    if (host) host.hidden = false;
}
function closeModal() {
    const host = document.getElementById("auth-modal");
    if (host) host.hidden = true;
}
