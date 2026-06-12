// Applies a new cache-buster version to index.html: rewrites both
// `var V = "..."` and the standalone styles.css?v=... to NEW_V.
// NEW_V comes from check_cachebuster.mjs's suggestedV (passed by the workflow).
// Deterministic string edit only — no logic changes.
import { readFileSync, writeFileSync } from "node:fs";

const NEW_V = process.env.NEW_V;
if (!NEW_V) {
  console.error("NEW_V env required");
  process.exit(1);
}

let html = readFileSync("index.html", "utf8");
const before = html;
html = html.replace(/(var\s+V\s*=\s*")[^"]+(")/, `$1${NEW_V}$2`);
html = html.replace(/(styles\.css\?v=)[^"'\s]+/, `$1${NEW_V}`);

if (html === before) {
  console.error("no cache-buster tokens found to replace");
  process.exit(1);
}
writeFileSync("index.html", html);
console.log(`bumped cache-buster -> ${NEW_V}`);
