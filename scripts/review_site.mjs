// Site reviewer for edgar-media.
// Loads the LIVE deployed site headlessly and collects problems, then:
//   - if FINDINGS_OUT is set, writes a machine-readable findings JSON there
//     (consumed by the autofix workflow's LLM fixer)
//   - if SEND_TELEGRAM !== "0" and creds are present, posts a ranked report
// Checks: page load, console errors, uncaught JS exceptions, broken asset
// requests (>=400), dead internal links, cache-buster (?v=) on local js/css.
// Issues are reported, never thrown — the job stays green; the report is the
// product. Only a crash of the reviewer itself is a failure.
import { chromium } from "playwright";
import { writeFileSync } from "node:fs";

const SITE_URL = process.env.SITE_URL || "https://hxuanheng.github.io/edgar-media/";
const TOKEN = process.env.TELEGRAM_BOT_TOKEN;
const CHAT_ID = process.env.TELEGRAM_CHAT_ID;
const SEND = process.env.SEND_TELEGRAM !== "0";
const FINDINGS_OUT = process.env.FINDINGS_OUT;
const MAX_PER_LIST = 10; // keep Telegram message under the 4096-char limit

const origin = new URL(SITE_URL).origin;

async function sendTelegram(text) {
  if (!TOKEN || !CHAT_ID) {
    console.log("Telegram creds missing; report:\n" + text);
    return;
  }
  const res = await fetch(`https://api.telegram.org/bot${TOKEN}/sendMessage`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ chat_id: CHAT_ID, text: text.slice(0, 4096), disable_web_page_preview: true }),
  });
  if (!res.ok) {
    console.error("Telegram send failed:", res.status, await res.text());
    process.exitCode = 1;
  } else {
    console.log("Telegram report sent.");
  }
}

function fmtList(items) {
  const shown = items.slice(0, MAX_PER_LIST).map((s) => `  - ${s}`);
  if (items.length > MAX_PER_LIST) shown.push(`  ...+${items.length - MAX_PER_LIST} more`);
  return shown.join("\n");
}

(async () => {
  const consoleErrors = [];
  const pageErrors = [];
  const badResponses = [];
  let loadOk = true;
  let loadNote = "";

  const browser = await chromium.launch();
  const page = await browser.newPage();
  page.on("console", (msg) => {
    if (msg.type() === "error") consoleErrors.push(msg.text().slice(0, 300));
  });
  page.on("pageerror", (err) => pageErrors.push(String(err.message || err).slice(0, 300)));
  page.on("response", (resp) => {
    if (resp.status() >= 400) badResponses.push(`${resp.status()} ${resp.url()}`);
  });

  try {
    const resp = await page.goto(SITE_URL, { waitUntil: "networkidle", timeout: 45000 });
    if (!resp || !resp.ok()) {
      loadOk = false;
      loadNote = resp ? `HTTP ${resp.status()}` : "no response";
    }
  } catch (e) {
    loadOk = false;
    loadNote = String(e.message || e).slice(0, 200);
  }

  let links = [];
  let assetRefs = [];
  try {
    links = await page.$$eval("a[href]", (els) => els.map((a) => a.href));
    assetRefs = await page.$$eval("script[src], link[rel=stylesheet][href]", (els) =>
      els.map((e) => e.getAttribute("src") || e.getAttribute("href")),
    );
  } catch {}
  await browser.close();

  // dead internal links (same origin, deduped, no anchors)
  const internal = [...new Set(links)].filter((h) => h.startsWith(origin) && !h.includes("#")).slice(0, 60);
  const deadLinks = [];
  await Promise.all(
    internal.map(async (url) => {
      try {
        let r = await fetch(url, { method: "HEAD", redirect: "follow" });
        if (r.status === 405) r = await fetch(url, { method: "GET", redirect: "follow" });
        if (r.status >= 400) deadLinks.push(`${r.status} ${url}`);
      } catch {
        deadLinks.push(`ERR ${url}`);
      }
    }),
  );

  // cache-buster on local js/css (live-DOM view; complements the git check)
  const unbusted = [];
  const versions = new Set();
  for (const s of assetRefs) {
    if (!s) continue;
    const isLocal = !/^https?:\/\//.test(s) || s.startsWith(origin);
    if (!isLocal || !/\.(js|css)(\?|$)/.test(s)) continue;
    const m = s.match(/[?&]v=([^&]+)/i);
    if (!m) unbusted.push(s);
    else versions.add(m[1]);
  }

  // code-level findings are what the LLM fixer should act on
  const codeFindings = [
    ...pageErrors.map((m) => ({ type: "js-exception", message: m })),
    ...consoleErrors.map((m) => ({ type: "console-error", message: m })),
  ];

  const stamp = new Date().toISOString().slice(0, 16).replace("T", " ");
  const findings = {
    url: SITE_URL,
    stampUTC: stamp,
    loadOk,
    loadNote,
    codeFindings,
    brokenRequests: badResponses,
    deadLinks,
    cacheBuster: { unbusted, versions: [...versions] },
  };

  // ranked human report
  const lines = [];
  let issueCount = 0;
  if (!loadOk) {
    issueCount++;
    lines.push(`CRITICAL: site did not load (${loadNote})`);
  }
  if (pageErrors.length) {
    issueCount += pageErrors.length;
    lines.push(`JS EXCEPTIONS (${pageErrors.length}):\n${fmtList(pageErrors)}`);
  }
  if (consoleErrors.length) {
    issueCount += consoleErrors.length;
    lines.push(`CONSOLE ERRORS (${consoleErrors.length}):\n${fmtList(consoleErrors)}`);
  }
  if (badResponses.length) {
    issueCount += badResponses.length;
    lines.push(`BROKEN REQUESTS (${badResponses.length}):\n${fmtList(badResponses)}`);
  }
  if (deadLinks.length) {
    issueCount += deadLinks.length;
    lines.push(`DEAD LINKS (${deadLinks.length}):\n${fmtList(deadLinks)}`);
  }
  if (unbusted.length) {
    issueCount += unbusted.length;
    lines.push(`UNBUSTED ASSETS — missing ?v= (${unbusted.length}):\n${fmtList(unbusted)}`);
  }
  if (versions.size > 1) {
    issueCount++;
    lines.push(`INCONSISTENT cache-buster: multiple V values [${[...versions].join(", ")}]`);
  }
  findings.issueCount = issueCount;

  if (FINDINGS_OUT) {
    writeFileSync(FINDINGS_OUT, JSON.stringify(findings, null, 2));
    console.log(`findings -> ${FINDINGS_OUT} (${issueCount} issues, ${codeFindings.length} code)`);
  }

  if (SEND) {
    const header =
      issueCount === 0
        ? `edgar-media site review — ALL CLEAR\n${SITE_URL}\n${stamp} UTC`
        : `edgar-media site review — ${issueCount} issue(s)\n${SITE_URL}\n${stamp} UTC`;
    const footer = versions.size === 1 ? `\ncache-buster V = ${[...versions][0]}` : "";
    await sendTelegram(`${header}\n\n${lines.join("\n\n") || "No problems found."}${footer}`);
  }
})().catch((e) => {
  console.error(e);
  if (SEND) {
    sendTelegram(`edgar-media site review CRASHED:\n${String(e.message || e).slice(0, 500)}`).finally(() =>
      process.exit(1),
    );
  } else {
    process.exit(1);
  }
});
