<script>
  let {
    id,
    label,
    hint = undefined,
    error = undefined,
    required = false,
    value = "",
    ...inputProps
  } = $props();

  const hintId = $derived(hint ? `${id}-hint` : undefined);
  const errorId = $derived(error ? `${id}-error` : undefined);
  const describedBy = $derived([hintId, errorId].filter(Boolean).join(" ") || undefined);
</script>

<div class="xpst-field">
  <label class="xpst-field__label" for={id}>
    {label}{#if required}<span aria-hidden="true"> *</span><span class="sr-only"> (required)</span>{/if}
  </label>
  <input
    class="xpst-field__input"
    {id}
    {value}
    required={required || undefined}
    aria-invalid={error ? "true" : undefined}
    aria-describedby={describedBy}
    {...inputProps}
  />
  {#if hint}<p class="xpst-field__hint" id={hintId}>{hint}</p>{/if}
  {#if error}<p class="xpst-field__error" id={errorId}>{error}</p>{/if}
</div>
