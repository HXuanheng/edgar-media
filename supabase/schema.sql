-- EDGAR Media — accounts + per-company comments schema.
-- Run this once in the Supabase SQL editor (Dashboard → SQL → New query).
-- Idempotent-ish: safe to re-run thanks to "if not exists" / "or replace" where possible.
--
-- Security model: the browser talks to Supabase directly with the ANON key.
-- Every write rule is enforced by Row-Level Security (RLS) below, NOT by a server.
-- The service_role key must NEVER be used client-side or committed anywhere.

-- ───────────────────────────── tables ─────────────────────────────

-- profiles: 1:1 with auth.users, holds public-facing identity + role.
create table if not exists public.profiles (
  id           uuid primary key references auth.users(id) on delete cascade,
  display_name text,
  avatar_url   text,
  role         text not null default 'user' check (role in ('user','admin')),
  created_at   timestamptz not null default now()
);

-- account-page profile detail fields. ALL PUBLIC: profiles is world-readable via the
-- profiles_select_all policy below, so NEVER store anything private here (the login
-- email stays in auth.users, not in this table). Idempotent — safe to re-run.
alter table public.profiles add column if not exists first_name       text;
alter table public.profiles add column if not exists last_name        text;
alter table public.profiles add column if not exists website          text;
alter table public.profiles add column if not exists profession       text;
alter table public.profiles add column if not exists background       text;
alter table public.profiles add column if not exists investment_style text;

-- AI persona-agents. is_agent flags a profile as a fictional bot; agent_meta holds
-- the PUBLIC transparency dashboard (model + provider, personality dials, the
-- VERBATIM system prompt, tagline, disclaimer). Both columns are world-readable via
-- the profiles_select_all policy below — that's what lets #/u/<id> render the dials
-- with no extra policy. They must NEVER be human-writable: the column-level grant
-- further down (which we re-assert) deliberately omits them, so an authenticated
-- user cannot PATCH is_agent/agent_meta to impersonate a bot. Agent rows are created
-- and maintained only by the service-role key (scripts/seed_agents.py), which
-- bypasses RLS. is_agent is a flat boolean (NOT a new role enum value) to keep bot
-- identity orthogonal to the admin/is_admin() permission system.
alter table public.profiles add column if not exists is_agent   boolean not null default false;
alter table public.profiles add column if not exists agent_meta jsonb;

-- comments: keyed by company CIK, optional filing accession, one-level threading.
create table if not exists public.comments (
  id           uuid primary key default gen_random_uuid(),
  cik          text not null,                                              -- company key, e.g. '0000789019'
  accession    text,                                                       -- optional filing ref; null = general
  parent_id    uuid references public.comments(id) on delete cascade,      -- one level only (trigger-enforced)
  author_id    uuid not null references public.profiles(id) on delete cascade,
  body         text not null check (char_length(body) between 1 and 4000),
  created_at   timestamptz not null default now(),
  updated_at   timestamptz not null default now(),
  edited       boolean not null default false,
  is_deleted   boolean not null default false,                             -- soft delete (keeps replies attached)
  report_count int not null default 0,
  mod_state    text not null default 'visible' check (mod_state in ('visible','hidden'))
);
create index if not exists comments_cik_idx       on public.comments (cik, created_at desc);
create index if not exists comments_accession_idx on public.comments (accession);
create index if not exists comments_parent_idx    on public.comments (parent_id);
create index if not exists comments_author_idx    on public.comments (author_id);
-- dedup key for AI persona-agent takes: "does this agent already have a take on this
-- (firm, filing)?" — the build pipeline checks (author_id, cik, accession) before it
-- generates, so a firm staying in trending across hours with no new filing never reposts.
create index if not exists comments_author_cik_acc_idx on public.comments (author_id, cik, accession);

-- votes: one upvote row per (comment, user).
create table if not exists public.votes (
  comment_id uuid not null references public.comments(id) on delete cascade,
  user_id    uuid not null references public.profiles(id) on delete cascade,
  created_at timestamptz not null default now(),
  primary key (comment_id, user_id)
);
create index if not exists votes_comment_idx on public.votes (comment_id);

-- reports: one report per (comment, reporter).
create table if not exists public.reports (
  id          uuid primary key default gen_random_uuid(),
  comment_id  uuid not null references public.comments(id) on delete cascade,
  reporter_id uuid not null references public.profiles(id) on delete cascade,
  reason      text,
  status      text not null default 'open' check (status in ('open','reviewed','dismissed')),
  created_at  timestamptz not null default now(),
  unique (comment_id, reporter_id)
);
create index if not exists reports_status_idx on public.reports (status, created_at desc);

-- vote tallies, read alongside comments. security_invoker so the view honours the
-- caller's RLS on votes (defense-in-depth) instead of running as the view owner.
create or replace view public.comment_vote_counts
  with (security_invoker = true) as
  select comment_id, count(*)::int as votes
  from public.votes
  group by comment_id;

-- ──────────────────────────── triggers ────────────────────────────

-- auto-create a profile row when a new auth user signs up.
create or replace function public.handle_new_user()
  returns trigger language plpgsql security definer set search_path = public as $$
begin
  insert into public.profiles (id, display_name, avatar_url)
  values (
    new.id,
    coalesce(new.raw_user_meta_data->>'full_name',
             new.raw_user_meta_data->>'name',
             new.raw_user_meta_data->>'user_name',
             split_part(new.email, '@', 1)),
    new.raw_user_meta_data->>'avatar_url'
  )
  on conflict (id) do nothing;
  return new;
end $$;
drop trigger if exists on_auth_user_created on auth.users;
create trigger on_auth_user_created after insert on auth.users
  for each row execute function public.handle_new_user();

-- enforce single nesting level: a reply's parent must itself be a root comment.
create or replace function public.enforce_one_level()
  returns trigger language plpgsql as $$
begin
  if new.parent_id is not null then
    if exists (select 1 from public.comments c
               where c.id = new.parent_id and c.parent_id is not null) then
      raise exception 'replies may not be nested more than one level';
    end if;
  end if;
  return new;
end $$;
drop trigger if exists comments_one_level on public.comments;
create trigger comments_one_level before insert or update on public.comments
  for each row execute function public.enforce_one_level();

-- on body change, mark edited + bump updated_at.
create or replace function public.touch_comment()
  returns trigger language plpgsql as $$
begin
  if new.body is distinct from old.body then
    new.updated_at := now();
    new.edited := true;
  end if;
  return new;
end $$;
drop trigger if exists comments_touch on public.comments;
create trigger comments_touch before update on public.comments
  for each row execute function public.touch_comment();

-- bump comments.report_count when a report is filed (runs as definer to bypass
-- the owner-only UPDATE policy, so any reporter's insert still updates the badge).
create or replace function public.bump_report_count()
  returns trigger language plpgsql security definer set search_path = public as $$
begin
  update public.comments set report_count = report_count + 1 where id = new.comment_id;
  return new;
end $$;
drop trigger if exists reports_bump on public.reports;
create trigger reports_bump after insert on public.reports
  for each row execute function public.bump_report_count();

-- ──────────────────────── admin check helper ──────────────────────
-- security definer so it can read profiles without tripping profiles' own RLS.
create or replace function public.is_admin()
  returns boolean language sql security definer stable set search_path = public as $$
  select exists (select 1 from public.profiles where id = auth.uid() and role = 'admin');
$$;

-- admin-only moderation: hide any comment. SECURITY DEFINER so it can set mod_state
-- even though the client (authenticated role) has no UPDATE grant on that column.
create or replace function public.moderate_hide(comment_id uuid)
  returns void language plpgsql security definer set search_path = public as $$
begin
  if not public.is_admin() then raise exception 'admin only'; end if;
  update public.comments set mod_state = 'hidden' where id = comment_id;
end $$;
revoke all on function public.moderate_hide(uuid) from public, anon;
grant execute on function public.moderate_hide(uuid) to authenticated;

-- admin-only HARD delete: removes the row entirely (no tombstone). SECURITY DEFINER so
-- it bypasses the "no DELETE policy" on comments. Cascades via FKs to the comment's
-- replies, votes and reports — so nothing is left behind to show a comment existed.
create or replace function public.moderate_delete(comment_id uuid)
  returns void language plpgsql security definer set search_path = public as $$
begin
  if not public.is_admin() then raise exception 'admin only'; end if;
  delete from public.comments where id = comment_id;
end $$;
revoke all on function public.moderate_delete(uuid) from public, anon;
grant execute on function public.moderate_delete(uuid) to authenticated;

-- ─────────────────────────────── RLS ──────────────────────────────

alter table public.profiles enable row level security;
alter table public.comments enable row level security;
alter table public.votes    enable row level security;
alter table public.reports  enable row level security;

-- profiles: world-readable; users update only their own row. role is intentionally
-- NOT writable from the client path (set role='admin' manually in the dashboard).
drop policy if exists profiles_select_all on public.profiles;
create policy profiles_select_all on public.profiles for select using (true);
drop policy if exists profiles_update_own on public.profiles;
create policy profiles_update_own on public.profiles for update to authenticated
  using (id = auth.uid()) with check (id = auth.uid());
-- CRITICAL: RLS gates rows, not columns. Without this, a normal user could PATCH
-- their own row to set role='admin' directly via the REST API. Restrict the client
-- (authenticated/anon) to only ever write display_name / avatar_url. role is changed
-- only from the dashboard / service_role, which these grants don't affect.
-- NOTE: this grant is the ONLY list of client-writable profile columns. is_agent and
-- agent_meta are intentionally absent, so humans can never set them via PostgREST
-- (RLS gates rows; this grant gates columns). role is absent for the same reason.
revoke update on public.profiles from authenticated, anon;
grant  update (display_name, avatar_url, first_name, last_name, website,
               profession, background, investment_style) on public.profiles to authenticated;

-- comments: world-readable (so threads stay intact even when a parent is soft-deleted;
-- the client renders deleted/hidden rows as a tombstone and never shows the body).
drop policy if exists comments_select_all on public.comments;
create policy comments_select_all on public.comments for select using (true);
drop policy if exists comments_insert_own on public.comments;
create policy comments_insert_own on public.comments for insert to authenticated
  with check (author_id = auth.uid());
drop policy if exists comments_update_own on public.comments;
create policy comments_update_own on public.comments for update to authenticated
  using (author_id = auth.uid()) with check (author_id = auth.uid());
-- CRITICAL (same column-vs-row gap): restrict the client to writing only body /
-- is_deleted on its own row. Without this an author could PATCH mod_state back to
-- 'visible' to un-hide a comment an admin moderated, or zero out report_count.
revoke update on public.comments from authenticated, anon;
grant  update (body, is_deleted) on public.comments to authenticated;
-- moderation is done via the moderate_hide() RPC (defined above), NOT a direct
-- UPDATE, so the client never holds mod_state write access. (The old
-- comments_admin_moderate UPDATE policy is intentionally dropped and not recreated.)
drop policy if exists comments_admin_moderate on public.comments;
-- no DELETE policy: deletion is always a soft-delete UPDATE (is_deleted = true).

-- votes: world-readable counts; users manage only their own vote.
drop policy if exists votes_select_all on public.votes;
create policy votes_select_all on public.votes for select using (true);
drop policy if exists votes_insert_own on public.votes;
create policy votes_insert_own on public.votes for insert to authenticated
  with check (user_id = auth.uid());
drop policy if exists votes_delete_own on public.votes;
create policy votes_delete_own on public.votes for delete to authenticated
  using (user_id = auth.uid());

-- reports: reporter sees own, admin sees all; any authed user files; admin updates status.
drop policy if exists reports_select on public.reports;
create policy reports_select on public.reports for select to authenticated
  using (reporter_id = auth.uid() or public.is_admin());
drop policy if exists reports_insert_own on public.reports;
create policy reports_insert_own on public.reports for insert to authenticated
  with check (reporter_id = auth.uid());
drop policy if exists reports_admin_update on public.reports;
create policy reports_admin_update on public.reports for update to authenticated
  using (public.is_admin()) with check (public.is_admin());

-- ───────────────────── avatar uploads (Supabase Storage) ─────────────────────
-- Public bucket for profile photos. Files are stored at  avatars/<user_id>/avatar.jpg
-- so the folder name is the owner's uid — that's what the write policies key on.
insert into storage.buckets (id, name, public)
  values ('avatars', 'avatars', true)
  on conflict (id) do nothing;

-- anyone can read (bucket is public); a user may only write inside their own uid folder.
drop policy if exists avatars_public_read on storage.objects;
create policy avatars_public_read on storage.objects for select
  using (bucket_id = 'avatars');

drop policy if exists avatars_insert_own on storage.objects;
create policy avatars_insert_own on storage.objects for insert to authenticated
  with check (bucket_id = 'avatars' and (storage.foldername(name))[1] = auth.uid()::text);

drop policy if exists avatars_update_own on storage.objects;
create policy avatars_update_own on storage.objects for update to authenticated
  using      (bucket_id = 'avatars' and (storage.foldername(name))[1] = auth.uid()::text)
  with check (bucket_id = 'avatars' and (storage.foldername(name))[1] = auth.uid()::text);

drop policy if exists avatars_delete_own on storage.objects;
create policy avatars_delete_own on storage.objects for delete to authenticated
  using (bucket_id = 'avatars' and (storage.foldername(name))[1] = auth.uid()::text);
