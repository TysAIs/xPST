<script>
  import { AlertCircle, AlertTriangle, CheckCircle2, CircleHelp, MinusCircle } from "@lucide/svelte";
  import { statusLabel } from "../labels.js";

  const STATUS_ICONS = {
    success: CheckCircle2,
    ok: CheckCircle2,
    healthy: CheckCircle2,
    warning: AlertTriangle,
    degraded: AlertTriangle,
    danger: AlertCircle,
    error: AlertCircle,
    invalid: AlertCircle,
    disabled: MinusCircle,
    unknown: CircleHelp,
    // Honest token-state badges (xpst.token_state) — rendered verbatim so the
    // UI never has to guess a colour for "expiring" or "source only".
    connected: CheckCircle2,
    expiring: AlertTriangle,
    needs_reauth: AlertCircle,
    source_only: MinusCircle,
  };

  let { status = "unknown", label = undefined } = $props();
  const normalized = $derived(String(status || "unknown").toLowerCase());
  const Icon = $derived(STATUS_ICONS[normalized] ?? CircleHelp);
  const displayLabel = $derived(label ?? statusLabel(normalized));
</script>

<span class="xpst-status-badge" data-status={normalized}>
  <Icon size={14} strokeWidth={2.25} aria-hidden="true" />
  <span>{displayLabel}</span>
</span>
