const test = require("node:test");
const assert = require("node:assert/strict");

const { snapshotIsFresh, taipeiDateParts } = require("../lib/dispatch");

test("Taipei date conversion crosses the UTC date boundary", () => {
  const parts = taipeiDateParts(new Date("2026-11-19T22:10:00Z"));
  assert.equal(`${parts.year}-${parts.month}-${parts.day} ${parts.hour}`, "2026-11-20 06");
});

test("morning snapshot is fresh for the morning window", () => {
  const html = "Generated: 2026-11-20 09:37:48 | Rows: 17";
  assert.equal(snapshotIsFresh(html, 6, new Date("2026-11-20T08:00:00Z")), true);
});

test("morning snapshot is stale for the afternoon window", () => {
  const html = "Generated: 2026-11-20 09:37:48 | Rows: 17";
  assert.equal(snapshotIsFresh(html, 14, new Date("2026-11-20T08:00:00Z")), false);
});

test("previous-day snapshot is stale", () => {
  const html = "Generated: 2026-11-19 19:52:03 | Rows: 23";
  assert.equal(snapshotIsFresh(html, 6, new Date("2026-11-20T00:00:00Z")), false);
});
