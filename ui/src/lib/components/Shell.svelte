<script>
  import BrandMark from "./BrandMark.svelte";
  import Nav from "./Nav.svelte";
  import { NAV_ITEMS } from "../api.js";

  let { items = NAV_ITEMS, route = "dashboard", onNavigate = undefined, children } = $props();
</script>

<div class="xpst-shell">
  <aside class="xpst-shell__sidebar">
    <BrandMark />
    <Nav {items} {route} {onNavigate} />
    <p class="xpst-shell__footer" style="margin-top: auto; padding: 0;">Cross-posting control plane</p>
  </aside>

  <div class="xpst-shell__content">
    <header class="xpst-shell__mobile-header">
      <BrandMark showName={false} size={36} />
      <Nav {items} {route} {onNavigate} mobile />
    </header>
    <main class="xpst-shell__main" id="main-content" tabindex="-1">
      {@render children?.()}
    </main>
    <footer class="xpst-shell__footer">xPST runs locally. Data shown here comes from the local engine.</footer>
  </div>
</div>
