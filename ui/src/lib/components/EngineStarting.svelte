<script>
  import { LoaderCircle } from "@lucide/svelte";
  import { ENGINE_STARTING_BODY, ENGINE_STARTING_TITLE } from "../engineStartup.js";

  // Shown while the engine sidecar is still coming up. Deliberately not the
  // error card: nothing is broken, the app has simply not finished starting,
  // and the view recovers on its own (see lib/engineStartup.js).
  let { title = ENGINE_STARTING_TITLE, message = ENGINE_STARTING_BODY } = $props();
</script>

<section class="xpst-state xpst-state--starting" role="status" aria-live="polite" aria-busy="true">
  <LoaderCircle class="xpst-state__icon xpst-state__icon--spin" size={30} strokeWidth={1.9} aria-hidden="true" />
  <div>
    <h2>{title}</h2>
    <p>{message}</p>
  </div>
</section>