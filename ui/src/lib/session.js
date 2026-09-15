// Session-scoped record of the last post attempt, so the result screen can be
// opened/refreshed (or linked to) and still show exactly what the engine did.
//
// Deliberately framework-free: a small pub/sub store that works in the app,
// in `node --test`, and with no `sessionStorage` at all.

const STORAGE_KEY = "xpst.lastPost";
const VERSION = 1;

function storage() {
  try {
    return typeof sessionStorage === "undefined" ? null : sessionStorage;
  } catch {
    return null;
  }
}

export function emptyLastPost() {
  return { version: VERSION, result: null, error: null, request: null, at: null };
}

function normalize(value) {
  if (!value || typeof value !== "object") return emptyLastPost();
  return {
    version: VERSION,
    result: value.result ?? null,
    error: value.error ?? null,
    request: value.request ?? null,
    at: value.at ?? null,
  };
}

function load() {
  const store = storage();
  if (!store) return emptyLastPost();
  try {
    const raw = store.getItem(STORAGE_KEY);
    return raw ? normalize(JSON.parse(raw)) : emptyLastPost();
  } catch {
    return emptyLastPost();
  }
}

let state = load();
const listeners = new Set();

function persist() {
  const store = storage();
  if (!store) return;
  try {
    if (state.result === null && state.error === null && state.request === null) {
      store.removeItem(STORAGE_KEY);
      return;
    }
    store.setItem(STORAGE_KEY, JSON.stringify(state));
  } catch {
    // A full or unavailable sessionStorage must never break the flow.
  }
}

function emit() {
  for (const listener of listeners) {
    try {
      listener(state);
    } catch {
      // A broken listener must not stop the others.
    }
  }
}

/** Current record: `{result, error, request, at}`. */
export function getLastPost() {
  return state;
}

/** Replace the record (pass `null` result for a failed request). */
export function setLastPost({ result = null, error = null, request = null, at = null } = {}) {
  state = normalize({ result, error, request, at: at ?? new Date().toISOString() });
  persist();
  emit();
  return state;
}

/** Clear the record (the result screen shows its empty state again). */
export function clearLastPost() {
  state = emptyLastPost();
  persist();
  emit();
  return state;
}

/** Subscribe to changes; returns an unsubscribe function. */
export function subscribeLastPost(listener) {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

/** Re-read from sessionStorage (used on mount so a refresh is truthful). */
export function reloadLastPost() {
  state = load();
  emit();
  return state;
}