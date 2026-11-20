<script>
  import { Inbox } from "@lucide/svelte";
  import Button from "./Button.svelte";

  let {
    title = "Nothing here yet",
    description = "There is no data to show in this view.",
    actionLabel = undefined,
    actionHref = undefined,
    onAction = undefined,
    children,
  } = $props();
</script>

<section class="xpst-state" aria-live="polite">
  <Inbox class="xpst-state__icon" size={30} strokeWidth={1.75} aria-hidden="true" />
  <h2>{title}</h2>
  <p>{description}</p>
  {#if actionLabel}
    <div class="xpst-state__actions">
      <Button variant="secondary" href={actionHref} onclick={onAction}>{actionLabel}</Button>
    </div>
  {/if}
  {@render children?.()}
</section>
