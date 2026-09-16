<script>
  import { previewCaption, previewPlan } from "../media.js";

  /**
   * Renders the *selected* asset before it is posted.
   *
   * Images load the engine's cached thumbnail (falling back to the raw stream
   * URL when no thumbnail could be generated); videos use a real `<video>`
   * element pointed at the range-aware stream route, with the generated frame
   * as a poster and `preload="metadata"` so the webview never pulls the whole
   * file just to draw it.
   */
  let { item = null, onClear = undefined } = $props();

  const plan = $derived(previewPlan(item));
  const path = $derived(item?.path ?? "");
  let thumbFailed = $state(false);
  let duration = $state(null);
  let dimensions = $state(null);
  let mediaError = $state("");

  // A new selection starts clean: no stale fallback, no stale metadata.
  $effect(() => {
    path;
    thumbFailed = false;
    duration = null;
    dimensions = null;
    mediaError = "";
  });

  const caption = $derived(previewCaption(item, { duration, dimensions }));
</script>

{#if plan}
  <figure class="xpst-preview" data-kind={plan.kind} data-testid="media-preview">
    <div class="xpst-preview__stage">
      {#if plan.kind === "image"}
        <img
          class="xpst-preview__media"
          src={thumbFailed && plan.fallbackSrc ? plan.fallbackSrc : plan.src}
          alt={item?.name ?? "Selected image"}
          onerror={() => {
            if (!thumbFailed && plan.fallbackSrc) {
              // No generated thumbnail (no ffmpeg, unusual format): the engine
              // can still stream the original, so show that instead of an error.
              thumbFailed = true;
            } else {
              mediaError = "This image could not be displayed.";
            }
          }}
        />
      {:else}
        <video
          class="xpst-preview__media"
          src={plan.src}
          poster={plan.poster}
          controls
          playsinline
          preload={plan.preload}
          aria-label={`Preview of ${item?.name ?? "selected video"}`}
          onloadedmetadata={(event) => {
            const element = event.currentTarget;
            duration = Number.isFinite(element.duration) ? element.duration : null;
            if (element.videoWidth && element.videoHeight) {
              dimensions = `${element.videoWidth}×${element.videoHeight}`;
            }
          }}
          onerror={() => {
            mediaError = "This file could not be previewed — the engine could not read it as video.";
          }}
        ></video>
      {/if}
    </div>
    <figcaption class="xpst-preview__caption">
      <span class="xpst-preview__name" title={plan.path}>{item?.name ?? plan.path}</span>
      <span class="xpst-preview__meta">{caption}</span>
      {#if onClear}
        <button class="xpst-button" data-variant="tertiary" data-size="sm" type="button" onclick={onClear}>Clear</button>
      {/if}
    </figcaption>
    {#if mediaError}
      <p class="xpst-field__error" role="status">{mediaError}</p>
    {/if}
  </figure>
{:else}
  <p class="xpst-field__hint">Nothing selected yet.</p>
{/if}
