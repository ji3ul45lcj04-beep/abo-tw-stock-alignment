const SITE_URL = "https://ji3ul45lcj04-beep.github.io/abo-tw-stock-alignment/";
const DISPATCH_URL = "https://api.github.com/repos/ji3ul45lcj04-beep/abo-tw-stock-alignment/actions/workflows/scan-and-deploy.yml/dispatches";
const GENERATED_PATTERN = /Generated: (\d{4}-\d{2}-\d{2}) (\d{2}):(\d{2}):(\d{2})/;

function taipeiDateParts(now = new Date()) {
  const parts = new Intl.DateTimeFormat("en-CA", {
    timeZone: "Asia/Taipei",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    hourCycle: "h23"
  }).formatToParts(now);
  return Object.fromEntries(parts.map(({ type, value }) => [type, value]));
}

function snapshotIsFresh(html, targetHour, now = new Date()) {
  const match = GENERATED_PATTERN.exec(html);
  if (!match) return false;

  const current = taipeiDateParts(now);
  const generatedDate = match[1];
  const generatedHour = Number(match[2]);
  const currentDate = `${current.year}-${current.month}-${current.day}`;
  return generatedDate === currentDate && generatedHour >= targetHour;
}

async function readSiteFreshness(targetHour) {
  const response = await fetch(`${SITE_URL}?scheduler_check=${Date.now()}`, {
    headers: { "cache-control": "no-cache" }
  });
  if (!response.ok) return false;
  return snapshotIsFresh(await response.text(), targetHour);
}

async function dispatchWorkflow() {
  const token = process.env.GITHUB_TOKEN;
  if (!token) throw new Error("GITHUB_TOKEN is not configured");

  const response = await fetch(DISPATCH_URL, {
    method: "POST",
    headers: {
      accept: "application/vnd.github+json",
      authorization: `Bearer ${token}`,
      "content-type": "application/json",
      "x-github-api-version": "2022-11-28"
    },
    body: JSON.stringify({ ref: "main" })
  });
  if (response.status !== 204) {
    throw new Error(`GitHub dispatch failed: ${response.status} ${await response.text()}`);
  }
}

function authorized(req) {
  const secret = process.env.CRON_SECRET;
  return Boolean(secret) && req.headers.authorization === `Bearer ${secret}`;
}

function createHandler(targetHour) {
  return async function handler(req, res) {
    if (!authorized(req)) {
      return res.status(401).json({ ok: false, error: "unauthorized" });
    }

    try {
      if (await readSiteFreshness(targetHour)) {
        return res.status(200).json({ ok: true, action: "skipped", targetHour });
      }
      await dispatchWorkflow();
      return res.status(202).json({ ok: true, action: "dispatched", targetHour });
    } catch (error) {
      return res.status(502).json({ ok: false, error: error.message, targetHour });
    }
  };
}

module.exports = { createHandler, snapshotIsFresh, taipeiDateParts };
