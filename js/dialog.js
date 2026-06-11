// Styled in-app dialogs (confirm + prompt) that replace the native browser popups,
// so destructive actions don't show the ugly "<site> says…" chrome. Promise-based;
// reuses the sign-in modal styling (.modal / .modal-card / .modal-backdrop).
//
//   await confirmDialog({ title, message, confirmLabel, danger })  -> boolean
//   await promptDialog({ title, message, placeholder })            -> string | null  (null = cancelled)

import { esc } from "./util.js";

// One shared host element, created on demand if index.html doesn't ship it.
function host() {
    let el = document.getElementById("app-dialog");
    if (!el) {
        el = document.createElement("div");
        el.id = "app-dialog";
        el.className = "modal";
        el.hidden = true;
        document.body.appendChild(el);
    }
    return el;
}

export function confirmDialog({ title, message = "", confirmLabel = "Confirm", cancelLabel = "Cancel", danger = false } = {}) {
    return new Promise((resolve) => {
        const el = host();
        el.innerHTML = `
            <div class="modal-backdrop" data-dlg="cancel"></div>
            <div class="modal-card dialog-card" role="dialog" aria-modal="true" aria-label="${esc(title)}">
                <h2 class="modal-title">${esc(title)}</h2>
                ${message ? `<p class="dialog-message">${esc(message)}</p>` : ""}
                <div class="dialog-actions">
                    <button type="button" class="auth-btn" data-dlg="cancel">${esc(cancelLabel)}</button>
                    <button type="button" class="auth-btn ${danger ? "auth-btn-danger" : "auth-btn-primary"}" data-dlg="ok">${esc(confirmLabel)}</button>
                </div>
            </div>`;
        el.hidden = false;

        let done = false;
        const finish = (val) => {
            if (done) return;
            done = true;
            document.removeEventListener("keydown", onKey);
            el.hidden = true; el.innerHTML = "";
            resolve(val);
        };
        const onKey = (e) => {
            if (e.key === "Escape") { e.preventDefault(); finish(false); }
            else if (e.key === "Enter") { e.preventDefault(); finish(true); }
        };
        document.addEventListener("keydown", onKey);
        el.querySelectorAll('[data-dlg="cancel"]').forEach((b) => b.addEventListener("click", () => finish(false)));
        el.querySelector('[data-dlg="ok"]').addEventListener("click", () => finish(true));
        el.querySelector('[data-dlg="ok"]').focus();
    });
}

export function promptDialog({ title, message = "", placeholder = "", confirmLabel = "Submit", cancelLabel = "Cancel", initial = "" } = {}) {
    return new Promise((resolve) => {
        const el = host();
        el.innerHTML = `
            <div class="modal-backdrop" data-dlg="cancel"></div>
            <div class="modal-card dialog-card" role="dialog" aria-modal="true" aria-label="${esc(title)}">
                <h2 class="modal-title">${esc(title)}</h2>
                ${message ? `<p class="dialog-message">${esc(message)}</p>` : ""}
                <form class="dialog-form" data-dlg="form">
                    <input type="text" class="dialog-input" name="val" placeholder="${esc(placeholder)}" value="${esc(initial)}" autocomplete="off">
                    <div class="dialog-actions">
                        <button type="button" class="auth-btn" data-dlg="cancel">${esc(cancelLabel)}</button>
                        <button type="submit" class="auth-btn auth-btn-primary">${esc(confirmLabel)}</button>
                    </div>
                </form>
            </div>`;
        el.hidden = false;

        let done = false;
        const finish = (val) => {
            if (done) return;
            done = true;
            document.removeEventListener("keydown", onKey);
            el.hidden = true; el.innerHTML = "";
            resolve(val);
        };
        const onKey = (e) => { if (e.key === "Escape") { e.preventDefault(); finish(null); } };
        document.addEventListener("keydown", onKey);
        el.querySelectorAll('[data-dlg="cancel"]').forEach((b) => b.addEventListener("click", () => finish(null)));
        el.querySelector('[data-dlg="form"]').addEventListener("submit", (e) => {
            e.preventDefault();
            finish(el.querySelector(".dialog-input").value.trim());
        });
        const inp = el.querySelector(".dialog-input");
        inp.focus(); inp.select();
    });
}
