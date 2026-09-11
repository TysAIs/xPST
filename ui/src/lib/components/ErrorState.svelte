<script>
  import { AlertCircle } from "@lucide/svelte";
  import Button from "./Button.svelte";

  let {
    title = "Could not load this view",
    message = "Check the local engine and try again.",
    retry = undefined,
    retryLabel = "Retry",
    children,
  } = $props();
</script>

<section class="xpst-state xpst-state--error" role="alert">
  <AlertCircle class="xpst-state__icon" size={30} strokeWidth={1.9} aria-hidden="true" />
  <div>
    <h2>{title}</h2>
    <p>{message}</p>
  </div>
  {#if retry}
    <div class="xpst-state__actions">
      <Button variant="secondary" onclick={retry}>{retryLabel}</Button>
    </div>
  {/if}
  {@render children?.()}
</section>
