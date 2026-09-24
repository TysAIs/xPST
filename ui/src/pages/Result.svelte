<script>
  import { onMount } from "svelte";
  import {
    destinationOutcomes,
    resultDescription,
    resultHeadline,
    resultTone,
    toneStatus,
  } from "../lib/firstRun.js";
  import { clearLastPost, getLastPost, reloadLastPost, subscribeLastPost } from "../lib/session.js";
  import Card from "../lib/components/Card.svelte";
  import EmptyState from "../lib/components/EmptyState.svelte";
  import ErrorState from "../lib/components/ErrorState.svelte";
  import PlatformBadge from "../lib/components/PlatformBadge.svelte";
  import StatusBadge from "../lib/components/StatusBadge.svelte";

  let record = $state(getLastPost());

  onMount(() => {
    reloadLastPost();
    record = getLastPost();
    return subscribeLastPost((next) => {
      record = next;
    });
  });

  const result = $derived(record?.result ?? null);
  const tone = $derived(resultTone(result));
  const outcomes = $derived(destinationOutcomes(result));
  const caption = $derived(result?.caption ?? record?.request?.caption ?? "");
  const mediaPaths = $derived(record?.request?.media_paths ?? []);
  const failed = $derived(outcomes.filter((row) => row.attempted && !row.success));
</script>

<header class="xpst-page-header">
  <div>
    <h1>Post result</h1>
    <p>Exactly what the engine did — per destination, including failures and retry guidance.</p>
  </div>
  <a class="xpst-button" data-variant="secondary" href="#/compose">Compose another</a>
</header>

{#if record?.error}
  <ErrorState
    title="The post request failed"
    message={`${record.error} Nothing was uploaded by this attempt.`}
    retry={() => (location.hash = "#/compose")}
    retryLabel="Back to Compose"
  />
{:else if !result}
  <EmptyState
    title={resultHeadline(null)}
    description={resultDescription(null)}
    actionLabel="Compose a post"
    actionHref="#/compose"
  />
{:else}
  <section class="xpst-section" aria-labelledby="result-heading">
    <div class="xpst-section__heading">
      <h2 id="result-heading">{resultHeadline(result)}</h2>
      <StatusBadge status={toneStatus(tone)} label={tone === "success" ? "Success" : tone === "partial" ? "Partial" : tone.startsWith("dry-run") ? "Dry run" : "Failed"} />
    </div>
    <Card description={resultDescription(result)}>
      <dl class="xpst-plan-summary">
        <div>
          <dt>Mode</dt>
          <dd>{result.dry_run ? "Dry run — no upload was attempted" : "Real post attempt"}</dd>
        </div>
        <div>
          <dt>Uploaded</dt>
          <dd>{result.uploaded ? `Yes — ${result.uploaded_count} destination(s) confirmed a published post` : "No upload was confirmed"}</dd>
        </div>
        <div>
          <dt>Requested</dt>
          <dd>{result.requested?.join(", ") || "none"}</dd>
        </div>
        {#if result.video_id}
          <div>
            <dt>Engine video id</dt>
            <dd>{result.video_id}</dd>
          </div>
        {/if}
        {#if caption}
          <div>
            <dt>Caption sent</dt>
            <dd>{caption}</dd>
          </div>
        {/if}
        {#if mediaPaths.length}
          <div>
            <dt>Media</dt>
            <dd>{mediaPaths.join(", ")}</dd>
          </div>
        {/if}
        {#if record?.at}
          <div>
            <dt>Recorded</dt>
            <dd>{record.at}</dd>
          </div>
        {/if}
      </dl>
      <div class="xpst-inline-actions">
        <a class="xpst-button" data-variant="secondary" href="#/activity">See recorded failures</a>
        <button class="xpst-button" data-variant="secondary" type="button" onclick={clearLastPost}>Clear this result</button>
      </div>
    </Card>
  </section>

  <section class="xpst-section" aria-labelledby="destinations-heading">
    <div class="xpst-section__heading">
      <h2 id="destinations-heading">Per destination</h2>
    </div>
    {#if outcomes.length === 0}
      <EmptyState title="No destination was recorded" description="The engine returned no destinations for this request." />
    {:else}
      <div class="xpst-table-wrap">
        <table class="xpst-table">
          <caption class="sr-only">Outcome per destination</caption>
          <thead>
            <tr>
              <th scope="col">Destination</th>
              <th scope="col">Outcome</th>
              <th scope="col">Detail</th>
              <th scope="col">Caption sent</th>
            </tr>
          </thead>
          <tbody>
            {#each outcomes as row (row.platform)}
              <tr>
                <th scope="row"><PlatformBadge platform={row.platform} size={16} /></th>
                <td><StatusBadge status={row.status} label={row.label} /></td>
                <td data-muted="true">
                  {#if row.postUrl}
                    <a class="xpst-inline-link" href={row.postUrl}>{row.postUrl}</a>
                  {:else if row.detail}
                    {row.detail}
                  {:else}
                    No error was reported.
                  {/if}
                </td>
                <td data-muted="true">
                  {#if row.caption}
                    {row.caption}
                  {:else}
                    —
                  {/if}
                </td>
              </tr>
            {/each}
          </tbody>
        </table>
      </div>
    {/if}
  </section>

  {#if failed.length}
    <Card title="What to do next" description="Every failed destination below returned no published post.">
      <ul>
        {#each failed as row (row.platform)}
          <li>
            <strong>{row.platform}</strong>: {row.detail || "no error detail"}
            {#if row.retryable === false} (not retryable){/if}
          </li>
        {/each}
      </ul>
      <p class="xpst-card__description">Fix the blocker, then compose again. xPST never marks a failed upload as complete.</p>
    </Card>
  {/if}
{/if}