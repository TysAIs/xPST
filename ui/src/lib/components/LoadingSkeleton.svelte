<script>
  let { rows = 3, label = "Loading", lineHeight = "16px" } = $props();
  const rowIndexes = $derived(Array.from({ length: Math.max(1, rows) }, (_, index) => index));
</script>

<div
  class="xpst-skeleton"
  style={`--xpst-skeleton-height: ${lineHeight}`}
  role="status"
  aria-live="polite"
  aria-busy="true"
  aria-label={label}
>
  <span class="sr-only">{label}</span>
  {#each rowIndexes as index (index)}
    <div class="xpst-skeleton__line" style={`width: ${index === rowIndexes.length - 1 ? "64%" : "100%"}`}></div>
  {/each}
</div>
