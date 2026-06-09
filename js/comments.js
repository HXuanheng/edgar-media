// Comments: Supabase CRUD + threaded rendering for both the inline card panel
// and the dedicated per-company page. Keyed by company CIK, optional filing
// accession, one nesting level. All user text is escaped via esc() / never
// injected as raw HTML.

import { supabase, isConfigured, initClient } from "./supabaseClient.js";
import { esc, timeAgo } from "./util.js";
import { getUser, isAdmin, openModal, onChange } from "./auth.js";

const state = new WeakMap();   // mount element -> { cik, company, full, onCount }

// All currently-mounted threads. A SINGLE auth subscription (not one per mount)
// re-renders the connected ones on sign-in/out and prunes detached ones, so opening
// many panels / navigating between company pages can't leak listeners or storm.
const activeMounts = new Set();
let authSubscribed = false;
function ensureAuthSub() {
    if (authSubscribed) return;
    authSubscribed = true;
    let first = true;
    onChange(() => {
        if (first) { first = false; return; }   // skip the immediate fire on subscribe
        for (const m of [...activeMounts]) {
            if (document.contains(m)) render(m);
            else activeMounts.delete(m);
        }
    });
}

// ───────────────────────────── data ops ─────────────────────────────

async function loadThread(cik) {
    const { data: rows, error } = await supabase
        .from("comments")
        .select("id, cik, accession, parent_id, author_id, body, created_at, updated_at, edited, is_deleted, mod_state, report_count, author:author_id(display_name, avatar_url)")
        .eq("cik", String(cik))
        .order("created_at", { ascending: true });
    if (error) throw error;

    const ids = (rows || []).map((r) => r.id);
    const counts = {};
    const myVotes = new Set();
    if (ids.length) {
        const { data: vc } = await supabase
            .from("comment_vote_counts").select("comment_id, votes").in("comment_id", ids);
        vc?.forEach((v) => { counts[v.comment_id] = v.votes; });
        const u = getUser();
        if (u) {
            const { data: mv } = await supabase
                .from("votes").select("comment_id").eq("user_id", u.id).in("comment_id", ids);
            mv?.forEach((v) => myVotes.add(v.comment_id));
        }
    }
    return { rows: rows || [], counts, myVotes };
}

function postComment(cik, body, accession, parentId) {
    return supabase.from("comments").insert({
        cik: String(cik),
        accession: accession || null,
        parent_id: parentId || null,
        author_id: getUser().id,
        body,
    });
}
const editComment   = (id, body) => supabase.from("comments").update({ body }).eq("id", id);
const deleteComment = (id) => supabase.from("comments").update({ is_deleted: true }).eq("id", id);
const reportComment = (id, reason) =>
    supabase.from("reports").insert({ comment_id: id, reporter_id: getUser().id, reason: reason || null });

async function toggleVote(id, voted) {
    const uid = getUser().id;
    if (voted) return supabase.from("votes").delete().eq("comment_id", id).eq("user_id", uid);
    return supabase.from("votes").insert({ comment_id: id, user_id: uid });
}

// ───────────────────────────── rendering ────────────────────────────

function filingRef(accession, company) {
    if (!accession) return "";
    const f = (company?.filings || []).find((x) => x.accession === accession);
    if (f) {
        return `<a class="cmt-filing" href="${esc(f.index_url)}" target="_blank" rel="noopener">re: ${esc(f.form)} · ${esc(f.date)} ↗</a>`;
    }
    return `<span class="cmt-filing">re: filing</span>`;
}

function avatarHtml(c) {
    const name = c.author?.display_name || "anon";
    return c.author?.avatar_url
        ? `<img class="auth-avatar" src="${esc(c.author.avatar_url)}" alt="" referrerpolicy="no-referrer">`
        : `<span class="auth-avatar auth-avatar-fallback">${esc(name.slice(0, 1).toUpperCase())}</span>`;
}

function commentHtml(c, replies, company, ctx) {
    const mine = ctx.user && c.author_id === ctx.user.id;
    const removed = c.is_deleted || c.mod_state === "hidden";
    const name = c.author?.display_name || "anon";

    const meta = `<div class="comment-meta">
        ${removed ? "" : avatarHtml(c)}
        <span class="comment-author">${esc(removed ? "—" : name)}</span>
        <span class="comment-time">${esc(timeAgo(c.created_at))}</span>
        ${c.edited && !removed ? `<span class="comment-edited">· edited</span>` : ""}
        ${removed ? "" : filingRef(c.accession, company)}
    </div>`;

    const bodyHtml = removed
        ? `<p class="comment-body comment-removed">[comment removed]</p>`
        : `<p class="comment-body">${esc(c.body)}</p>`;

    let actions = "";
    if (!removed) {
        const voted = ctx.myVotes.has(c.id);
        const votes = ctx.counts[c.id] || 0;
        actions += `<button class="vote-btn ${voted ? "voted" : ""}" data-act="vote" data-id="${c.id}" data-voted="${voted ? 1 : 0}" title="Upvote">▲ <span class="vote-n">${votes}</span></button>`;
        if (ctx.full && ctx.user && !c.parent_id)
            actions += `<button class="cmt-link" data-act="reply" data-id="${c.id}">Reply</button>`;
        if (mine) {
            actions += `<button class="cmt-link" data-act="edit" data-id="${c.id}">Edit</button>`;
            actions += `<button class="cmt-link" data-act="delete" data-id="${c.id}">Delete</button>`;
        } else if (ctx.user) {
            actions += `<button class="cmt-link" data-act="report" data-id="${c.id}">Report</button>`;
        }
        if (ctx.isAdmin && !mine)
            actions += `<button class="cmt-link cmt-link-danger" data-act="remove" data-id="${c.id}">Remove</button>`;
    }

    const editForm = (mine && !removed)
        ? `<form class="cmt-edit-form" data-act="edit-submit" data-id="${c.id}" hidden>
             <textarea name="body" required>${esc(c.body)}</textarea>
             <div class="cmt-composer-row">
               <button type="submit" class="auth-btn auth-btn-primary">Save</button>
               <button type="button" class="cmt-link" data-act="edit-cancel" data-id="${c.id}">Cancel</button>
             </div></form>`
        : "";

    const replyForm = (ctx.full && ctx.user && !c.parent_id && !removed)
        ? `<form class="cmt-reply-form" data-act="reply-submit" data-parent="${c.id}" data-accession="${esc(c.accession || "")}" hidden>
             <textarea name="body" placeholder="Reply…" required></textarea>
             <div class="cmt-composer-row">
               <button type="submit" class="auth-btn auth-btn-primary">Reply</button>
               <button type="button" class="cmt-link" data-act="reply-cancel" data-id="${c.id}">Cancel</button>
             </div></form>`
        : "";

    let repliesHtml = "";
    if (ctx.full && replies.length) {
        repliesHtml = `<div class="comment-replies">${replies.map((r) => commentHtml(r, [], company, ctx)).join("")}</div>`;
    } else if (!ctx.full && replies.length) {
        repliesHtml = `<div class="comment-replies-count">${replies.length} ${replies.length === 1 ? "reply" : "replies"}</div>`;
    }

    return `<div class="comment ${c.parent_id ? "reply" : ""} ${removed ? "deleted" : ""}" data-cid="${c.id}">
        ${meta}
        <div class="comment-body-wrap" data-bodywrap="${c.id}">${bodyHtml}</div>
        ${editForm}
        <div class="cmt-actions">${actions}</div>
        ${replyForm}
        ${repliesHtml}
    </div>`;
}

function composerHtml(ctx, company) {
    if (!ctx.user)
        return `<div class="cmt-signin"><button class="auth-btn" data-act="signin">Sign in to comment</button></div>`;
    const opts = (company?.filings || [])
        .filter((f) => f.accession)
        .map((f) => `<option value="${esc(f.accession)}">${esc(f.form)} · ${esc(f.date)}</option>`)
        .join("");
    return `<form class="cmt-composer" data-act="new-submit">
        <textarea name="body" placeholder="Add a comment…" required></textarea>
        <div class="cmt-composer-row">
            <select name="accession" aria-label="Reference a filing">
                <option value="">General (no filing)</option>${opts}
            </select>
            <button type="submit" class="auth-btn auth-btn-primary">Post</button>
        </div></form>`;
}

const INLINE_ROOTS = 3;

async function render(mount) {
    const st = state.get(mount);
    if (!st) return;
    const { cik, company, full } = st;
    const ctx = { full, user: getUser(), isAdmin: isAdmin(), counts: {}, myVotes: new Set() };

    let rows = [];
    try {
        const data = await loadThread(cik);
        rows = data.rows;
        ctx.counts = data.counts;
        ctx.myVotes = data.myVotes;
    } catch (e) {
        mount.innerHTML = `<p class="cmt-flash cmt-flash-err">Couldn't load comments (${esc(e.message || "error")}).</p>`;
        return;
    }

    const roots = rows.filter((r) => !r.parent_id);
    const byParent = {};
    rows.filter((r) => r.parent_id).forEach((r) => { (byParent[r.parent_id] ||= []).push(r); });
    const total = rows.filter((r) => !(r.is_deleted || r.mod_state === "hidden")).length;
    st.onCount?.(total);

    // Inline panel previews the most RECENT roots (so a just-posted comment is
    // visible); the full page shows everything in chronological order.
    const shownRoots = full ? roots : roots.slice(-INLINE_ROOTS);
    const listHtml = shownRoots.length
        ? shownRoots.map((c) => commentHtml(c, byParent[c.id] || [], company, ctx)).join("")
        : `<p class="cmt-empty">No comments yet.${ctx.user ? " Be the first." : ""}</p>`;

    const head = `<div class="cmt-head">${total} ${total === 1 ? "comment" : "comments"}</div>`;
    let footer = "";
    if (!full && company?.ticker) {
        footer = `<a class="cmt-viewall" href="#/company/${esc(company.ticker)}">View full discussion${total > INLINE_ROOTS ? ` (${total})` : ""} →</a>`;
    }

    mount.innerHTML = `${head}${composerHtml(ctx, company)}<div class="cmt-flash" hidden></div><div class="cmt-list">${listHtml}</div>${footer}`;
}

function flash(mount, text, isErr) {
    const el = mount.querySelector(".cmt-flash");
    if (!el) return;
    el.hidden = false;
    el.textContent = text;
    el.classList.toggle("cmt-flash-err", !!isErr);
}

async function guard(mount, promise, okMsg) {
    const { error } = await promise;
    if (error) { flash(mount, error.message || "Action failed.", true); return false; }
    if (okMsg) flash(mount, okMsg, false);
    return true;
}

function wire(mount) {
    if (mount.dataset.wired) return;
    mount.dataset.wired = "1";

    mount.addEventListener("click", async (e) => {
        const btn = e.target.closest("[data-act]");
        if (!btn || btn.tagName === "FORM") return;
        const act = btn.dataset.act;
        const id = btn.dataset.id;

        if (act === "signin") { openModal(); return; }
        if (act === "vote") {
            if (!getUser()) return openModal();
            btn.disabled = true;   // block the double-click race -> duplicate-key error
            const voted = btn.dataset.voted === "1";
            if (await guard(mount, toggleVote(id, voted))) render(mount);
            else btn.disabled = false;
            return;
        }
        if (act === "reply") {
            const f = mount.querySelector(`.cmt-reply-form[data-parent="${id}"]`);
            if (f) { f.hidden = !f.hidden; if (!f.hidden) f.querySelector("textarea")?.focus(); }
            return;
        }
        if (act === "reply-cancel") {
            mount.querySelector(`.cmt-reply-form[data-parent="${id}"]`)?.setAttribute("hidden", "");
            return;
        }
        if (act === "edit") {
            mount.querySelector(`[data-bodywrap="${id}"]`)?.setAttribute("hidden", "");
            const f = mount.querySelector(`.cmt-edit-form[data-id="${id}"]`);
            if (f) { f.hidden = false; f.querySelector("textarea")?.focus(); }
            return;
        }
        if (act === "edit-cancel") {
            mount.querySelector(`.cmt-edit-form[data-id="${id}"]`)?.setAttribute("hidden", "");
            mount.querySelector(`[data-bodywrap="${id}"]`)?.removeAttribute("hidden");
            return;
        }
        if (act === "delete") {
            if (!confirm("Delete your comment?")) return;
            if (await guard(mount, deleteComment(id))) render(mount);
            return;
        }
        if (act === "remove") {
            if (!confirm("Remove this comment (admin)?")) return;
            // admin moderation goes through the SECURITY DEFINER RPC (sets mod_state),
            // not a direct UPDATE — the client has no mod_state write grant.
            if (await guard(mount, supabase.rpc("moderate_hide", { comment_id: id }))) render(mount);
            return;
        }
        if (act === "report") {
            const reason = prompt("Report this comment — reason (optional):", "");
            if (reason === null) return;   // cancelled
            await guard(mount, reportComment(id, reason), "Reported. Thanks.");
            return;
        }
    });

    mount.addEventListener("submit", async (e) => {
        const form = e.target.closest("form[data-act]");
        if (!form) return;
        e.preventDefault();
        const st = state.get(mount);
        const act = form.dataset.act;
        const body = form.body?.value.trim();
        if (act !== "new-submit" && act !== "reply-submit" && act !== "edit-submit") return;
        if (!body) return;

        if (act === "new-submit") {
            const accession = form.accession?.value || null;
            if (await guard(mount, postComment(st.cik, body, accession, null))) { form.reset(); render(mount); }
        } else if (act === "reply-submit") {
            const accession = form.dataset.accession || null;
            if (await guard(mount, postComment(st.cik, body, accession, form.dataset.parent))) render(mount);
        } else if (act === "edit-submit") {
            if (await guard(mount, editComment(form.dataset.id, body))) render(mount);
        }
    });
}

// ───────────────────────────── public API ───────────────────────────

function mountThread(mount, cik, company, { full = false, onCount = null } = {}) {
    if (!isConfigured) {
        mount.innerHTML = `<p class="cmt-empty">Comments aren't enabled yet.</p>`;
        return;
    }
    state.set(mount, { cik, company, full, onCount });
    wire(mount);
    for (const m of [...activeMounts]) if (!document.contains(m)) activeMounts.delete(m);
    activeMounts.add(mount);
    ensureAuthSub();
    initClient().then(() => render(mount));   // SDK may still be loading on first paint
}

export function renderInlinePanel(cik, mount, company, onCount) {
    mountThread(mount, cik, company, { full: false, onCount });
}
export function renderFullThread(cik, mount, company) {
    mountThread(mount, cik, company, { full: true });
}
