// Single shared Supabase client, loaded from CDN as an ES module (no build step).
//
// `supabase` is a live binding: null until initClient() resolves, then the client.
// initClient() lazily dynamic-imports the SDK only when configured and only once.
// Crucially there is NO top-level await here, so importing this module never blocks
// app.js's body (the trending fetch fires immediately, independent of the CDN load).

import { SUPABASE_URL, SUPABASE_ANON_KEY, isConfigured } from "./config.js";

export let supabase = null;
let _initPromise = null;

export function initClient() {
    if (!isConfigured) return Promise.resolve(null);
    if (!_initPromise) {
        _initPromise = import("https://esm.sh/@supabase/supabase-js@2").then(({ createClient }) => {
            supabase = createClient(SUPABASE_URL, SUPABASE_ANON_KEY, {
                auth: {
                    persistSession: true,
                    autoRefreshToken: true,
                    detectSessionInUrl: true,   // exchange the ?code= on OAuth / magic-link return
                    flowType: "pkce",
                },
            });
            return supabase;
        });
    }
    return _initPromise;
}

export { isConfigured };
