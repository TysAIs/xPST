<script>
  let { title = undefined, description = undefined, class: className = "", children, ...rest } = $props();
</script>

<section class={`xpst-card ${className}`.trim()} {...rest}>
  {#if title || description}
    <header class="xpst-card__header">
      <div>
        {#if title}<h2 class="xpst-card__title">{title}</h2>{/if}
        {#if description}<p class="xpst-card__description">{description}</p>{/if}
      </div>
    </header>
  {/if}
  {@render children?.()}
</section>
