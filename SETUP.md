# Accounts + comments — setup

The site ships with accounts/comments **dormant**. Nothing appears and the page
renders exactly as before until you fill in real Supabase keys in
[`js/config.js`](js/config.js). Everything below is one-time dashboard work that
only you can do (it needs secrets and OAuth app registration).

## 1. Create the Supabase project

1. Sign up at <https://supabase.com>, create a project, pick a region.
2. **SQL Editor → New query →** paste all of [`supabase/schema.sql`](supabase/schema.sql) and run it. This creates `profiles`, `comments`, `votes`, `reports`, the triggers, and all Row-Level Security policies.

## 2. Wire the keys

**Settings → API**, copy two values into `js/config.js`:

```js
export const SUPABASE_URL = "https://<your-ref>.supabase.co";
export const SUPABASE_ANON_KEY = "<the anon / publishable key>";
```

Both are public by design — RLS is the security boundary. **Never** put the
`service_role` key anywhere in this repo or in client code.

## 3. Auth providers

**Authentication → Providers:**

- **Email** — enable; "Confirm email" can stay on. This powers the magic link.
- **Google** — create an OAuth client in the [Google Cloud Console](https://console.cloud.google.com/apis/credentials) (OAuth client → Web application). Authorized redirect URI = `https://<your-ref>.supabase.co/auth/v1/callback`. Paste the client ID/secret into Supabase.
- **GitHub** — create a [GitHub OAuth App](https://github.com/settings/developers). Authorization callback URL = `https://<your-ref>.supabase.co/auth/v1/callback`. Paste the client ID/secret into Supabase.

> The OAuth providers call back to **Supabase**, not to github.io. Supabase then
> redirects the browser back to the site URL below.

## 4. Redirect URLs

**Authentication → URL Configuration:**

- **Site URL:** `https://hxuanheng.github.io/edgar-media/`
- **Redirect URLs (allow-list):** add all of —
  - `https://hxuanheng.github.io/edgar-media/`
  - `http://localhost:8000/`
  - `http://127.0.0.1:8000/`

The trailing `/edgar-media/` subpath matters — the app uses `origin + pathname`,
so the redirect must include it.

## 5. Make yourself admin (moderation)

After signing in once, **Table editor → `profiles` →** set your row's `role` to
`admin`. Admins get a "Remove" button on any comment (soft-delete) and can read
the `reports` table. There is no in-app way to grant admin (by design).

## 6. Test

```bash
python -m http.server 8000      # from the repo root
```

Open <http://localhost:8000/>. Sign in (Google / GitHub / magic link), expand a
card's **💬 Comments**, post with and without a filing reference, reply, upvote,
edit, delete, report; open a company's full page via **View full discussion →**
(`#/company/MSFT`).

## Notes

- The hourly data CI (`.github/workflows/update.yml`) only touches
  `data/*.json` — it never clobbers the new JS/HTML/CSS.
- No build step, no npm: `supabase-js` loads from CDN as an ES module.
- Rate-limiting isn't enforced yet (RLS authenticates but doesn't throttle). If
  spam shows up, add a Supabase Edge Function or a per-author insert-rate trigger.
