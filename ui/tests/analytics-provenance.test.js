// Analytics provenance tests (D5).
//
// The rule being protected: a number on the Analytics page is never shown
// without its provenance, and a platform with nothing to show says "No data"
// rather than printing a zero that reads as "this post got no views".
// Recorded and live rows must be distinguishable both in the view model and
// in the CSS the page ships.

import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { join, resolve } from "node:path";
import { test } from "node:test";
import { compile } from "svelte/compiler";

import {
  isEmptyReport,
  metricCell,
  outcomeRows,
  platformRows,
  relativeAge,
  reportHeadline,
  sourceBadge,
} from "../src/lib/analytics.js";

const UI_ROOT = resolve(import.meta.dirname, "..");

async function text(path) {
  return readFile(path, "utf8");
}

const RECORDED_REPORT = {
  generated_at: "2026-09-15T10:40:00+00:00",
  live: false,
  data_source: "recorded",
  platforms: {
    youtube: {
      platform: "youtube",
      ownership_verified: true,
      ownership_checked: true,
      has_data: true,
      data_source: "recorded",
      data_source_label: "Recorded",
      last_captured: "2026-09-14T14:21:17.998894+00:00",
      staleness: "fresh",
      metrics_available: ["comments", "likes", "views"],
      totals: { views: 347, likes: 2, comments: 0 },
      outcomes: [
        {
          post_id: "9AZLHvNMMv4",
          url: "https://youtube.com/shorts/9AZLHvNMMv4",
          status: "published",
          outcome_source: "snapshot",
          metric_source: "recorded",
          captured_at: "2026-09-14T14:21:17+00:00",
          metrics: { views: 12, likes: 0, comments: 0 },
        },
      ],
    },
    instagram: {
      platform: "instagram",
      ownership_verified: true,
      ownership_checked: true,
      has_data: false,
      data_source: null,
      data_source_label: "No data",
      metrics_available: ["comments", "likes", "saves", "shares", "views"],
      totals: null,
      outcomes: [],
    },
  },
};

const LIVE_REPORT = {
  ...RECORDED_REPORT,
  live: true,
  data_source: "live",
  platforms: {
    ...RECORDED_REPORT.platforms,
    youtube: {
      ...RECORDED_REPORT.platforms.youtube,
      data_source: "live",
      data_source_label: "Live (fetched now)",
      outcomes: [
        { ...RECORDED_REPORT.platforms.youtube.outcomes[0], metric_source: "live" },
      ],
    },
  },
};

test("a platform with no data renders 'No data' for every metric, never 0", () => {
  const [youtube, instagram] = platformRows(RECORDED_REPORT);
  assert.equal(instagram.hasData, false);
  assert.equal(metricCell(instagram, "views"), "No data");
  assert.equal(metricCell(instagram, "likes"), "No data");
  assert.equal(metricCell(instagram, "comments"), "No data");
  assert.equal(youtube.hasData, true);
  assert.equal(metricCell(youtube, "views"), "347");
});

test("a metric the platform cannot report is a dash, not a zero", () => {
  const [youtube] = platformRows(RECORDED_REPORT);
  // YouTube reports no share count; the row must not invent one.
  assert.equal(metricCell(youtube, "shares"), "—");
  // Likes/comments ARE reported (including a real 0), so they show numbers.
  assert.equal(metricCell(youtube, "likes"), "2");
  assert.equal(metricCell(youtube, "comments"), "0");
});

test("recorded and live rows carry different badge tones and labels", () => {
  const recorded = sourceBadge(RECORDED_REPORT.platforms.youtube);
  const live = sourceBadge(LIVE_REPORT.platforms.youtube);
  const none = sourceBadge(RECORDED_REPORT.platforms.instagram);
  assert.equal(recorded.label, "Recorded");
  assert.equal(live.label, "Live");
  assert.equal(none.label, "No data");
  assert.equal(recorded.tone, "recorded");
  assert.equal(live.tone, "live");
  assert.equal(none.tone, "none");
  assert.notEqual(recorded.tone, live.tone);
  assert.match(recorded.title, /captured/);
  assert.match(live.title, /platform API/);
});

test("per-post rows keep the metric source label per post", () => {
  const recordedPost = outcomeRows(RECORDED_REPORT)[0];
  const livePost = outcomeRows(LIVE_REPORT)[0];
  assert.equal(recordedPost.sourceLabel, "Recorded");
  assert.equal(livePost.sourceLabel, "Live");
  assert.equal(recordedPost.postId, "9AZLHvNMMv4");
  assert.equal(recordedPost.views, 12);
});

test("headline names the provenance of the whole report", () => {
  assert.match(reportHeadline(RECORDED_REPORT), /Recorded figures/);
  assert.match(reportHeadline(LIVE_REPORT), /Live figures/);
  assert.match(reportHeadline(null), /Loading/);
  assert.equal(isEmptyReport(RECORDED_REPORT), false);
  assert.equal(isEmptyReport({ platforms: { youtube: { has_data: false } } }), true);
});

test("the Analytics page compiles and renders the source badge", async () => {
  const source = await text(join(UI_ROOT, "src/pages/Analytics.svelte"));
  const { js, warnings } = compile(source, { filename: "Analytics.svelte", generate: "client" });
  assert.ok(js.code.length > 0, "Analytics.svelte compiled to nothing");
  const fatal = (warnings ?? []).filter((warning) => warning.code === "missing-declaration");
  assert.deepEqual(fatal, [], `Analytics.svelte has undeclared references: ${JSON.stringify(fatal)}`);
  assert.match(source, /xpst-source-badge/);
  assert.match(source, /data-source=\{row\.badge\.tone\}/);
  assert.match(source, /data-source=\{post\.metricSource\}/);
  assert.match(source, /metricCell/);
});

test("the recorded and live badges are visually distinct in the stylesheet", async () => {
  const css = await text(join(UI_ROOT, "src/app.css"));
  const live = css.match(/\.xpst-source-badge\[data-source="live"\]\s*\{[^}]*\}/);
  const recorded = css.match(/\.xpst-source-badge\[data-source="recorded"\]\s*\{[^}]*\}/);
  const none = css.match(/\.xpst-source-badge\[data-source="none"\]\s*\{[^}]*\}/);
  assert.ok(live, "no live badge rule");
  assert.ok(recorded, "no recorded badge rule");
  assert.ok(none, "no no-data badge rule");
  const liveBody = live[0];
  const recordedBody = recorded[0];
  assert.match(liveBody, /--xpst-color-success/);
  assert.match(recordedBody, /--xpst-color-text-secondary/);
  assert.notEqual(liveBody, recordedBody);
  assert.match(css, /td\[data-has-data="false"\]/);
});

test("the API client asks the outcomes endpoint for live data explicitly", async () => {
  const api = await text(join(UI_ROOT, "src/lib/api.js"));
  assert.match(api, /\/api\/analytics\/outcomes/);
  assert.match(api, /outcomeReport/);
});

// Staleness honesty (analytics-accuracy audit 2026-08-28 follow-up): a
// recorded row must say how old it is — "as of Xh ago" — so a stale
// snapshot is never read as a live number.

test("relativeAge formats staleness hours as human age copy", () => {
  assert.equal(relativeAge(0), "just now");
  assert.equal(relativeAge(0.5), "30m ago");
  assert.equal(relativeAge(2.75), "2h ago");
  assert.equal(relativeAge(30), "1d ago");
  assert.equal(relativeAge(50.9), "2d ago");
  assert.equal(relativeAge(null), null);
  assert.equal(relativeAge(undefined), null);
  assert.equal(relativeAge(-1), null);
});

test("a recorded badge carries an 'as of Xh ago' staleness note", () => {
  const fresh = sourceBadge({
    data_source: "recorded",
    staleness: "fresh",
    staleness_hours: 2.5,
    last_captured: "2026-09-14T14:21:17+00:00",
  });
  assert.equal(fresh.stalenessNote, "as of 2h ago");
  assert.equal(fresh.stale, false);
  assert.match(fresh.title, /captured 2h ago/);

  const stale = sourceBadge({
    data_source: "recorded",
    staleness: "stale",
    staleness_hours: 30,
    last_captured: "2026-09-13T08:00:00+00:00",
  });
  assert.equal(stale.stalenessNote, "as of 1d ago");
  assert.equal(stale.stale, true);
  assert.match(stale.title, /stale: older than 24h/);

  const live = sourceBadge({ data_source: "live" });
  assert.equal(live.stalenessNote, null);
});

test("the Analytics page renders the staleness note and the CSS styles it", async () => {
  const source = await text(join(UI_ROOT, "src/pages/Analytics.svelte"));
  assert.match(source, /xpst-staleness-note/);
  assert.match(source, /row\.badge\.stalenessNote/);
  const css = await text(join(UI_ROOT, "src/app.css"));
  const note = css.match(/\.xpst-staleness-note\s*\{[^}]*\}/);
  assert.ok(note, "no .xpst-staleness-note rule");
  assert.match(css, /\.xpst-staleness-note\[data-stale="true"\]/);
});
