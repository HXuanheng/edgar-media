#!/usr/bin/env node
// SessionStart hook: warn if local branch is behind its remote.
// Reason: user sometimes forgets to `git pull` before working, risking
// stale-base commits / force-push wiping remote (bot) commits.
// No shell: execFileSync with argv array so `@{u}` is never brace-expanded.
const { execFileSync } = require("child_process");

function git(args, opts = {}) {
  return execFileSync("git", args, {
    stdio: ["ignore", "pipe", "ignore"],
    ...opts,
  })
    .toString()
    .trim();
}

try {
  // Throws if no upstream is configured -> nothing to compare, stay silent.
  git(["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"]);

  // Refresh remote tracking ref. Bounded so a slow/offline network can't hang
  // session start; on failure we just compare against the last-known ref.
  try {
    execFileSync("git", ["fetch", "--quiet"], {
      stdio: "ignore",
      timeout: 8000,
    });
  } catch (_) {}

  const behind = parseInt(git(["rev-list", "--count", "HEAD..@{u}"]) || "0", 10);
  const ahead = parseInt(git(["rev-list", "--count", "@{u}..HEAD"]) || "0", 10);

  if (behind > 0) {
    const br = git(["rev-parse", "--abbrev-ref", "HEAD"]);
    let msg = `⚠ GIT: local '${br}' is BEHIND remote by ${behind} commit(s). Pull before working: git pull`;
    if (ahead > 0) {
      msg += ` -- also ${ahead} ahead (DIVERGED: history split; pull needs merge/rebase, do NOT force-push or you erase remote commits).`;
    }
    console.log(msg);
  }
} catch (_) {
  // Not a git repo, or no upstream: silent, never block the session.
}
