<script>
  import xpstMark from "../../../../assets/icon.png";

  let { showName = true, size = 40 } = $props();
</script>

<a class="xpst-brand" href="#/" aria-label="xPST dashboard">
  <img
    class="xpst-brand__mark"
    src={xpstMark}
    width={size}
    height={size}
    alt=""
    decoding="async"
  />
  {#if showName}<span class="xpst-brand__name">xPST</span>{/if}
</a>
