// Boot-window loading.
//
// The desktop shell shows the window before the engine sidecar answers, so a
// view mounted in the first second of a launch legitimately fails to reach
// /api/* (see API_ERROR_ENGINE_STARTING in api.js). Measured on the installed
// bundle: BOOT_TO_VISIBLE_SECS=0.165, ENGINE_HEALTH_WAIT_SECS=0.690. The old
// behaviour surfaced that race as a red "Could not load this view /
// /api/summary → response was not JSON" card for the first ~1.5s of every
// launch — the first thing the user saw.
//
// This module owns the honest answer instead: a calm "starting the engine"
// state, bounded automatic retries, and the real error card only for a failure
// the app cannot wait out. Everything here is pure (no DOM, no Svelte) so it is
// unit-testable with `node --test`.

import { ApiError, errorMessage, isEngineStarting } from "./api.js";

/** Heading/body for the state shown while the engine comes up. */
export const ENGINE_STARTING_TITLE = "Starting the local engine";
export const ENGINE_STARTING_BODY =
  "xPST is bringing its local engine up — this usually takes a second. Nothing is wrong; the app connects on its own.";

/**
 * Delay before boot retry `attempt` (1-based). Short first steps so a launch
 * that is only a few hundred milliseconds behind settles without a visible
 * stall, then a capped ladder so a dead engine still ends in a real card
 * instead of an infinite spinner.
 */
const RETRY_LADDER_MS = [200, 250, 350, 500, 650, 800, 1000, 1200, 1500, 1800, 2000];

export function bootRetryDelayMs(attempt) {
  const index = Math.max(1, Number(attempt) || 1) - 1;
  return RETRY_LADDER_MS[Math.min(index, RETRY_LADDER_MS.length - 1)];
}

/** 14 attempts over the ladder above ≈ 14s of grace before we call it broken. */
export const BOOT_RETRY_ATTEMPTS = 14;

/**
 * A boot-window failure the app can still wait out: the engine is not
 * answering yet, or it is answering before it can serve its own data routes
 * (5xx). Anything else — a refusal with a reason — is handed straight back so
 * the user sees the truth immediately.
 */
export function isBootTransient(error) {
  if (isEngineStarting(error)) return true;
  return error instanceof ApiError && error.status >= 500;
}

/**
 * Copy for the card shown when the boot wait ran out: after `attempts` the
 * engine still had not answered, which is a real failure the user must act on
 * (the shell's own 60s timeout will also replace the page).
 */
export function bootFailureMessage(result) {
  if (result?.engineStarting) {
    return "The local engine did not answer while the app was starting. Restart xPST; if it keeps happening, run Diagnostics.";
  }
  return errorMessage(result?.error);
}

/**
 * Run `load` until it succeeds or the engine stays unreachable for `attempts`.
 *
 * Returns `{ok, value, error, attempts, engineStarting}`. `onWaiting(attempt)`
 * fires before each retry so the caller can render the starting state — only
 * for errors that are genuinely the engine coming up.
 */
export async function loadWhileEngineStarts(
  load,
  {
    attempts = BOOT_RETRY_ATTEMPTS,
    sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms)),
    isTransient = isBootTransient,
    onWaiting = undefined,
  } = {}
) {
  let error;
  let tries = 0;
  for (let attempt = 1; attempt <= attempts; attempt += 1) {
    if (attempt > 1) await sleep(bootRetryDelayMs(attempt - 1));
    try {
      const value = await load();
      return { ok: true, value, error: undefined, attempts: attempt, engineStarting: false };
    } catch (cause) {
      error = cause;
      tries = attempt;
      if (!isTransient(cause)) break;
      onWaiting?.(attempt, cause);
    }
  }
  return {
    ok: false,
    value: undefined,
    error,
    attempts: tries,
    engineStarting: isEngineStarting(error),
  };
}