// Dashboard API token handling for the web UI.
//
// Every mutating engine route (POST /api/post, POST /api/connect/<platform>,
// POST /api/onboarding*, POST /api/preflight) requires an API token: loopback
// is not an authorisation boundary, so the engine refuses an unauthenticated
// write from any process on the machine or any page open in a browser.
//
// The token reaches this page out-of-band — it is never baked into the served
// HTML (that would hand it to every local process that can GET "/"):
//
//   1. window.__XPST_API_TOKEN  — injected by a native shell (desktop app)
//   2. #xpst_token=<value>      — per-launch fragment opened by `xpst ui`
//   3. sessionStorage           — remembered for the rest of the tab session
//
// A fragment is used instead of a query string because fragments are never
// sent to the server (so the token cannot reach an access log), and it is
// stripped from the address bar as soon as the app boots.

export const TOKEN_STORAGE_KEY = "xpst_api_token";
export const TOKEN_FRAGMENT_PREFIX = "#xpst_token=";
//: A paired phone's own device token. It must survive the browser being
//: closed (the operator reopens the app and expects the true engine state, not
//: a login screen), so it lives in localStorage rather than sessionStorage.
export const DEVICE_TOKEN_STORAGE_KEY = "xpst_device_token";

function safeStorage(storage) {
  if (storage !== undefined) return storage;
  try {
    return typeof sessionStorage === "undefined" ? null : sessionStorage;
  } catch {
    return null;
  }
}

function persistentStorage(storage) {
  if (storage !== undefined) return storage;
  try {
    return typeof localStorage === "undefined" ? null : localStorage;
  } catch {
    return null;
  }
}

/** The paired-device token this browser remembers, or null. */
export function getDeviceToken({ storage = undefined } = {}) {
  const store = persistentStorage(storage);
  try {
    const stored = store?.getItem(DEVICE_TOKEN_STORAGE_KEY);
    return stored || null;
  } catch {
    return null;
  }
}

/** Remember (or clear, with a falsy value) this browser's device token. */
export function setDeviceToken(token, { storage = undefined } = {}) {
  const store = persistentStorage(storage);
  try {
    if (token) store?.setItem(DEVICE_TOKEN_STORAGE_KEY, token);
    else store?.removeItem(DEVICE_TOKEN_STORAGE_KEY);
  } catch {
    /* storage can be disabled by policy; the session token still works */
  }
}

/** Token carried by a `#xpst_token=<value>` fragment, or null. */
export function parseTokenFragment(hash) {
  const raw = String(hash ?? "");
  if (!raw.startsWith(TOKEN_FRAGMENT_PREFIX)) return null;
  const value = raw.slice(TOKEN_FRAGMENT_PREFIX.length);
  if (!value) return null;
  try {
    return decodeURIComponent(value);
  } catch {
    return value;
  }
}

/**
 * The token this page should send, or null when none is available.
 *
 * Read-only routes never need it; mutating routes will 401 without it, and the
 * UI surfaces the engine's own refusal message in that case.
 */
export function getApiToken({ globalObject = globalThis, storage = undefined, deviceStorage = undefined } = {}) {
  const injected = globalObject?.__XPST_API_TOKEN;
  if (typeof injected === "string" && injected) return injected;
  const store = safeStorage(storage);
  try {
    const stored = store?.getItem(TOKEN_STORAGE_KEY);
    if (stored) return stored;
  } catch {
    /* storage can be disabled mid-session */
  }
  // A paired phone holds a durable device token instead of a per-launch one.
  return getDeviceToken({ storage: deviceStorage });
}

/** Remember (or clear, with a falsy value) the token for this tab session. */
export function setApiToken(token, { storage = undefined } = {}) {
  const store = safeStorage(storage);
  try {
    if (token) store?.setItem(TOKEN_STORAGE_KEY, token);
    else store?.removeItem(TOKEN_STORAGE_KEY);
  } catch {
    /* storage can be disabled mid-session */
  }
}

/**
 * Consume a `#xpst_token=` fragment: remember it and take it out of the
 * address bar so it cannot be copied out of a URL or a screenshot.
 *
 * Returns the captured token, or null when the fragment carried none.
 */
export function captureTokenFromFragment({
  locationObject = globalThis.location,
  historyObject = globalThis.history,
  storage = undefined,
} = {}) {
  const token = parseTokenFragment(locationObject?.hash);
  if (!token) return null;
  setApiToken(token, { storage });
  try {
    const path = locationObject.pathname ?? "/";
    const search = locationObject.search ?? "";
    historyObject?.replaceState(null, "", `${path}${search}#/`);
  } catch {
    /* history rewrite is cosmetic; the token is already stored */
  }
  return token;
}

/** Headers carrying the token, or {} when the page has none. */
export function tokenHeaders(options = {}) {
  const token = getApiToken(options);
  return token ? { "X-API-Token": token } : {};
}
