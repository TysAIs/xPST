// Analytics page view-model.
//
// The rule this module exists to enforce: an analytics number is only shown
// next to the post it came from, and it always says whether it is LIVE (just
// fetched from the platform) or RECORDED (the last snapshot xPST persisted).
// A platform with nothing to show says "No data" — never a zero, which reads
// as "this post got no views".

export const SOURCE_LABEL = {
  live: "Live",
  recorded: "Recorded",
};

/** Title/tone for a platform row's provenance badge. */
export function sourceBadge(entry) {
  const source = entry?.data_source;
  if (source === "live") {
    return { label: SOURCE_LABEL.live, tone: "live", title: "Fetched from the platform API during this run" };
  }
  if (source === "recorded") {
    return {
      label: SOURCE_LABEL.recorded,
      tone: "recorded",
      title: entry?.last_captured
        ? `Last snapshot captured ${entry.last_captured}`
        : "Last snapshot xPST persisted",
    };
  }
  return { label: "No data", tone: "none", title: entry?.note || "Nothing recorded for this platform yet" };
}

/**
 * Cell text for a metric. `null`/`undefined` renders as a dash, and a
 * platform that reports no data at all renders "No data" for every metric —
 * a real zero is only ever shown when the platform actually returned one.
 */
export function metricCell(entry, key) {
  // Accepts either the API shape (has_data/metrics_available) or the row shape
  // produced by platformRows(), so the template cannot accidentally read the
  // wrong field and print a value for an empty platform.
  const hasData = entry?.has_data ?? entry?.hasData;
  if (!hasData) return "No data";
  const totals = entry.totals;
  if (!totals || totals[key] === null || totals[key] === undefined) return "—";
  const available = entry.metrics_available ?? entry.metricsAvailable;
  if (Array.isArray(available) && !available.includes(key)) return "—";
  return Number(totals[key]).toLocaleString("en-US");
}

/** Per-post rows for the detail table, newest capture first. */
export function outcomeRows(report) {
  const rows = [];
  for (const [platform, entry] of Object.entries(report?.platforms ?? {})) {
    for (const outcome of entry?.outcomes ?? []) {
      if (!outcome.metrics) continue;
      rows.push({
        platform,
        postId: outcome.post_id,
        url: outcome.url || null,
        status: outcome.status,
        outcomeSource: outcome.outcome_source,
        metricSource: outcome.metric_source,
        sourceLabel: outcome.metric_source === "live" ? SOURCE_LABEL.live : SOURCE_LABEL.recorded,
        capturedAt: outcome.captured_at || null,
        publishedAt: outcome.published_at || null,
        views: outcome.metrics.views ?? null,
        likes: outcome.metrics.likes ?? null,
        comments: outcome.metrics.comments ?? null,
        shares: outcome.metrics.shares ?? null,
      });
    }
  }
  rows.sort((a, b) => {
    const at = a.capturedAt || a.publishedAt || "";
    const bt = b.capturedAt || b.publishedAt || "";
    return bt.localeCompare(at);
  });
  return rows;
}

/**
 * Platform rows for the summary table, in the engine's own order. `null`
 * totals survive into the view model so the template can render "No data"
 * instead of inventing a zero.
 */
export function platformRows(report) {
  return Object.values(report?.platforms ?? {}).map((entry) => ({
    platform: entry.platform,
    hasData: Boolean(entry.has_data),
    posts: entry.posts_with_metrics ?? 0,
    ownershipLabel: entry.ownership_verified
      ? "Owned posts only"
      : entry.ownership_checked
        ? "Ownership unverified — no numbers shown"
        : "No posts recorded",
    badge: sourceBadge(entry),
    metricsAvailable: entry.metrics_available ?? [],
    totals: entry.totals,
  }));
}

/** Headline for the page: what the report covers and where it came from. */
export function reportHeadline(report) {
  if (!report) return "Loading analytics";
  if (report.data_source === "live") return "Live figures fetched from the platform APIs just now";
  if (report.data_source === "recorded") return "Recorded figures from xPST's last snapshot capture";
  return "No analytics recorded yet";
}

/** True when no platform on the report has any data to show. */
export function isEmptyReport(report) {
  return !Object.values(report?.platforms ?? {}).some((entry) => entry?.has_data);
}
