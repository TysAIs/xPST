<script>
  import { onMount } from "svelte";
  import Button from "./Button.svelte";

  let {
    rows = 3,
    label = "Loading",
    timeoutMs = 8000,
    onRetry = undefined,
  } = $props();

  let timedOut = $state(false);
  const rowIndexes = $derived(Array.from({ length: Math.max(1, rows) }, (_, index) => index));

  onMount(() => {
    const timer = setTimeout(() => {
      timedOut = true;
    }, timeoutMs);
    return () => clearTimeout(timer);
  });
</script>

<div class="xpst-skeleton" role="status" aria-live="polite" aria-busy={!timedOut} aria-label={label}>
  <span class="sr-only">{timedOut ? `${label} is taking longer than expected` : label}</span>
  {#each rowIndexes as index (index)}
    <div class="xpst-skeleton__line" class:xpst-skeleton__line--short={index === rowIndexes.length - 1}></div>
  {/each}
  {#if timedOut}
    <div class="xpst-skeleton__retry">
      <p>{label} is taking longer than expected.</p>
      {#if onRetry}
        <Button variant="secondary" onclick={onRetry}>Retry</Button>
      {/if}
    </div>
  {/if}
</div>
