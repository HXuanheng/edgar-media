// Supabase connection config.
//
// These two values are PUBLIC by design — the anon (publishable) key is meant to
// ship in client code; Row-Level Security (see supabase/schema.sql) is the security
// boundary. NEVER put the service_role key here.
//
// To turn on accounts + comments:
//   1. Create a Supabase project (https://supabase.com).
//   2. Run supabase/schema.sql in the SQL editor.
//   3. Settings → API → copy "Project URL" and the "anon public" key below.
//   4. Configure auth providers + redirect URLs (see SETUP.md).
//
// Until both values are filled in with real ones, the comments/accounts UI stays
// completely dormant and the site renders exactly as before (isConfigured === false).

export const SUPABASE_URL = "https://YOUR-PROJECT-REF.supabase.co";
export const SUPABASE_ANON_KEY = "YOUR-ANON-PUBLIC-KEY";

export const isConfigured =
  /^https:\/\/[a-z0-9-]+\.supabase\.co\/?$/.test(SUPABASE_URL) &&
  SUPABASE_ANON_KEY.length > 20 &&
  !SUPABASE_ANON_KEY.startsWith("YOUR-");
