<script>
  import { LoaderCircle } from "@lucide/svelte";

  let {
    variant = "primary",
    size = "md",
    type = "button",
    href = undefined,
    disabled = false,
    loading = false,
    fullWidth = false,
    ariaLabel = undefined,
    children,
    ...rest
  } = $props();

  const isDisabled = $derived(disabled || loading);
  const classes = $derived(
    ["xpst-button", loading ? "xpst-button--loading" : ""].filter(Boolean).join(" ")
  );
</script>

{#if href && !isDisabled}
  <a
    class={classes}
    data-variant={variant}
    data-size={size}
    data-full-width={fullWidth ? "true" : undefined}
    href={href}
    aria-label={ariaLabel}
    {...rest}
  >
    {#if loading}
      <LoaderCircle class="xpst-button__spinner" size={16} aria-hidden="true" />
    {/if}
    <span>{@render children?.()}</span>
  </a>
{:else}
  <button
    class={classes}
    data-variant={variant}
    data-size={size}
    data-full-width={fullWidth ? "true" : undefined}
    {type}
    disabled={isDisabled}
    aria-label={ariaLabel}
    aria-busy={loading ? "true" : undefined}
    {...rest}
  >
    {#if loading}
      <LoaderCircle class="xpst-button__spinner" size={16} aria-hidden="true" />
    {/if}
    <span>{@render children?.()}</span>
  </button>
{/if}

<style>
  :global(.xpst-button:focus-visible) {
    outline: 3px solid var(--xpst-color-focus);
    outline-offset: var(--xpst-focus-offset);
  }
</style>
