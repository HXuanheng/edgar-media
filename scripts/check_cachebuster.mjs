// Deterministic cache-buster checker for edgar-media.
// The chore (per project memory): when js/ or styles.css change on a deploy,
// `var V` in index.html must be bumped AND styles.css?v= kept in sync.
// This detects two failure modes with zero ambiguity:
//   - outOfSync: styles.css?v=<x> does not equal var V
//   - stale:     js/ or styles.css have a commit newer than the commit that
//                last changed the `var V` line (i.e. assets shipped, V forgot)
// Prints a JSON verdict to stdout; the workflow decides whether to open a PR.
// Requires full git history (actions/checkout fetch-depth: 0).
import { readFileSync } from "node:fs";
import { execFileSync } from "node:child_process";

const INDEX = "index.html";

function git(args) {
  try {
    return execFileSync("git", args, { encoding: "utf8" }).trim();
  } catch {
    return "";
  }
}

const html = readFileSync(INDEX, "utf8");

// current V and the styles.css buster
const vMatch = html.match(/var\s+V\s*=\s*"([^"]+)"/);
const styleMatch = html.match(/styles\.css\?v=([^"'\s]+)/);
const currentV = vMatch ? vMatch[1] : null;
const styleV = styleMatch ? styleMatch[1] : null;

const reasons = [];
let outOfSync = false;
if (!currentV) {
  reasons.push("could not find `var V` in index.html");
} else if (styleV && styleV !== currentV) {
  outOfSync = true;
  reasons.push(`styles.css?v=${styleV} != var V "${currentV}"`);
}

// git staleness: last asset change vs last change to the V line
const assetTime = git(["log", "-1", "--format=%cI", "--", "js", "styles.css"]);
const vLineTime = git(["log", "-1", "--format=%cI", "-G", 'var\\s+V\\s*=', "--", INDEX]);
let stale = false;
if (assetTime && vLineTime && new Date(assetTime) > new Date(vLineTime)) {
  stale = true;
  reasons.push(
    `assets changed ${assetTime.slice(0, 10)} after V last set ${vLineTime.slice(0, 10)}`,
  );
}

// suggested next V: today's date + suffix letter (increment if already today)
function pad(n) {
  return String(n).padStart(2, "0");
}
const now = new Date();
const today = `${now.getUTCFullYear()}-${pad(now.getUTCMonth() + 1)}-${pad(now.getUTCDate())}`;
let suggestedV;
if (currentV && currentV.startsWith(today)) {
  const suffix = currentV.slice(today.length); // e.g. "e"
  const last = suffix.slice(-1);
  const next = last && last >= "a" && last < "z" ? String.fromCharCode(last.charCodeAt(0) + 1) : "a";
  suggestedV = suffix ? today + suffix.slice(0, -1) + next : today + "a";
} else {
  suggestedV = today + "a";
}

const verdict = {
  needsBump: outOfSync || stale,
  outOfSync,
  stale,
  currentV,
  styleV,
  suggestedV,
  reasons,
};
console.log(JSON.stringify(verdict, null, 2));
