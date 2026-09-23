<script>
  import { onMount } from "svelte";
  import { api } from "../lib/api.js";
  import Card from "../lib/components/Card.svelte";
  import EmptyState from "../lib/components/EmptyState.svelte";
  import ErrorState from "../lib/components/ErrorState.svelte";
  import LoadingSkeleton from "../lib/components/LoadingSkeleton.svelte";
  import PlatformBadge from "../lib/components/PlatformBadge.svelte";
  import {
    isEmptyReport,
    metricCell,
    outcomeRows,
    platformRows,
    reportHeadline,
  } from "../lib/analytics.js";

  let state = $state("loading");
  let report = $state(null);
  let error = $state("");
  let refreshing = $state(false);

  async function load(live = false) {
    if (live) refreshing = true;
    else state = "loading";
    error = "";
    try {
      report = await api.outcomeReport(live);
      state = "ready";
    } catch (cause) {
      error = cause instanceof Error ? cause.message : String(cause);
      state = "error";
    } finally {
      refreshing = false;
    }
  }

  onMount(() => {
    load(false);
  });

  const rows = $derived(platformRows(report));
  const posts = $derived(outcomeRows(report));
  const metricKeys = ["views", "likes", "comments", "shares"];
</script>

<header class="xpst-page-header">
  <div>
    <h1>Analytics</h1>
    <p>
      Every number below belongs to a post this account published. Each row says whether it is a
      recorded snapshot or a live fetch, and a platform with nothing to report says "No data".
    </p>
  </div>
  <button
    type="button"
    class="xpst-button xpst-button--secondary"
    disabled={refreshing}
    onclick={() => load(true)}
  >
    {refreshing ? "Fetching…" : "Fetch live figures"}
  </button>
</header>

{#if state === "loading"}
  <LoadingSkeleton rows={6} label="Loading analytics" onRetry={() => load(false)} />
{:else if state === "error"}
  <ErrorState message={error} retry={() => load(false)} />
{:else}
  <p class="xpst-analytics-source" data-source={report?.data_source ?? "none"}>
    {reportHeadline(report)}
  </p>

  {#if isEmptyReport(report)}
    <EmptyState
      title="No analytics recorded yet"
      description="xPST records metrics after a post publishes and the engine captures a snapshot. Nothing has been captured for this account yet."
      actionLabel="Review videos"
      actionHref="#/videos"
    />
  {:else}
    <Card title="Engagement by platform" description="Sourced from persisted snapshots unless a row says Live.">
      <div class="xpst-table-wrap" style="box-shadow: none;">
        <table class="xpst-table">
          <thead>
            <tr>
              <th scope="col">Platform</th>
              <th scope="col">Posts</th>
              <th scope="col">Views</th>
              <th scope="col">Likes</th>
              <th scope="col">Comments</th>
              <th scope="col">Shares</th>
              <th scope="col">Source</th>
            </tr>
          </thead>
          <tbody>
            {#each rows as row (row.platform)}
              <tr data-has-data={row.hasData}>
                <td><PlatformBadge platform={row.platform} /></td>
                <td>{row.hasData ? row.posts : "No data"}</td>
                {#each metricKeys as key (key)}
                  <td data-metric={key} data-has-data={row.hasData}>{metricCell(row, key)}</td>
                {/each}
                <td>
                  <span
                    class="xpst-source-badge"
                    data-source={row.badge.tone}
                    title={row.badge.title}
                    aria-label={`Metric source: ${row.badge.label}`}
                  >
                    {row.badge.label}
                  </span>
                  <span class="xpst-source-note">{row.ownershipLabel}</span>
                </td>
              </tr>
            {/each}
          </tbody>
        </table>
      </div>
    </Card>

    {#if posts.length > 0}
      <Card title="Per-post outcomes" description="One row per published post that has recorded metrics.">
        <div class="xpst-table-wrap" style="box-shadow: none;">
          <table class="xpst-table">
            <thead>
              <tr>
                <th scope="col">Platform</th>
                <th scope="col">Post</th>
                <th scope="col">Status</th>
                <th scope="col">Views</th>
                <th scope="col">Likes</th>
                <th scope="col">Comments</th>
                <th scope="col">Source</th>
              </tr>
            </thead>
            <tbody>
              {#each posts as post (`${post.platform}:${post.postId}`)}
                <tr>
                  <td><PlatformBadge platform={post.platform} /></td>
                  <td>
                    {#if post.url}
                      <a href={post.url} target="_blank" rel="noopener noreferrer">{post.postId}</a>
                    {:else}
                      {post.postId}
                    {/if}
                  </td>
                  <td data-muted="true">{post.status}</td>
                  <td>{post.views ?? "—"}</td>
                  <td>{post.likes ?? "—"}</td>
                  <td>{post.comments ?? "—"}</td>
                  <td>
                    <span
                      class="xpst-source-badge"
                      data-source={post.metricSource}
                      aria-label={`Metric source: ${post.sourceLabel}`}
                    >
                      {post.sourceLabel}
                    </span>
                  </td>
                </tr>
              {/each}
            </tbody>
          </table>
        </div>
      </Card>
    {/if}
  {/if}
{/if}
