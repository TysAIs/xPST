#!/usr/bin/env bash
# xPST Tauri shell — deep-link + single-instance E2E (Phase 3).
#
# Builds the macOS .app (cargo tauri build --bundles app — never the dmg
# stage: bundle_dmg.sh's Finder AppleScript times out in headless sessions),
# then proves:
#   1. Info.plist CFBundleURLTypes contains the xpst:// scheme (generated from
#      tauri.conf.json → plugins.deep-link.desktop.schemes by the bundler).
#   2. Single instance: launching the binary twice leaves exactly ONE process
#      with a window; the second launch exits immediately.
#   3. Deep link at runtime: `open "xpst://callback?..."` reaches the RUNNING
#      app — verified by the DEEPLINK_RECEIVED marker/log line the shell
#      writes on receipt (src-tauri/src/lib.rs).
#   4. Engine forwarding: the shell POSTs the URL to
#      http://127.0.0.1:<port>/oauth/callback (engine's dashboard, port
#      XPST_ENGINE_PORT, default 8080). Without an engine up, the shell logs
#      outcome=engine_unreachable — that is still a PASS for shell routing;
#      with the engine up, outcome=forwarded:200 is required here and the
#      engine must be the REAL FastAPI dashboard (started via
#      scripts/engine_entry.py), whose OAUTH_CALLBACK_RECEIVED log line
#      proves the actual route handled the forwarded URL.
#
# Scheme registration / lsregister fallback (dev machines):
#   Info.plist registration normally happens at install time (Finder /
#   `open`-ing the .app once). When you run a freshly-built .app in place —
#   or an older build previously claimed xpst:// — LaunchServices may not
#   route the scheme. The script therefore re-registers with:
#     /System/Library/Frameworks/CoreServices.framework/Frameworks/\
#       LaunchServices.framework/Support/lsregister -f <path-to-.app>
#   which is idempotent and requires no Dock/Finder restart. If deep links
#   still don't reach a dev build after this, quit ALL instances and re-run —
#   LaunchServices routes to whichever registered instance is running.
#
# Usage: scripts/deeplink-e2e.sh [--skip-build]
# Exit codes: 0 PASS, 1 FAIL.

set -u
cd "$(dirname "$0")/.." # repo root

REPO_ROOT="$(pwd)"
APP_BUNDLE="$REPO_ROOT/src-tauri/target/release/bundle/macos/xPST.app"
BIN_DIR="$APP_BUNDLE/Contents/MacOS"
WORK="$(mktemp -d /tmp/xpst-deeplink-e2e.XXXXXX)"
export XPST_SHELL_LOG="$WORK/shell.log"
export XPST_DEEPLINK_MARKER="$WORK/deeplink-marker.txt"
export XPST_ENGINE_PORT="${XPST_ENGINE_PORT:-8080}"
DEEPLINK_URL="xpst://callback?code=e2e&state=abc"
LSREGISTER="/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister"
FAILED=0

fail() { echo "FAIL: $*"; FAILED=1; }
pass() { echo "PASS: $*"; }
cleanup() {
  pkill -f "$BIN_DIR" 2>/dev/null
  if [ -n "${ENGINE_PID:-}" ]; then kill "$ENGINE_PID" 2>/dev/null; fi
}
trap cleanup EXIT

if [[ "${1:-}" != "--skip-build" ]]; then
  echo "== Building xPST.app (cargo tauri build --bundles app) =="
  cargo tauri build --bundles app || { fail "build"; exit 1; }
fi

[[ -d "$APP_BUNDLE" ]] || { fail "no app bundle at $APP_BUNDLE"; exit 1; }
BIN="$(ls "$BIN_DIR" | head -1)"
BIN_PATH="$BIN_DIR/$BIN"
echo "App bundle: $APP_BUNDLE (binary: $BIN_PATH)"

echo
echo "== 1. Info.plist CFBundleURLTypes check (bundler-generated) =="
PLIST="$APP_BUNDLE/Contents/Info.plist"
if /usr/libexec/PlistBuddy -c 'Print :CFBundleURLTypes' "$PLIST" 2>/dev/null \
   | grep -q "xpst"; then
  pass "Info.plist registers scheme xpst"
else
  fail "CFBundleURLTypes missing xpst scheme in $PLIST"
fi

echo
echo "== 2. lsregister -f (scheme re-registration fallback) =="
"$LSREGISTER" -f "$APP_BUNDLE" && pass "lsregister -f ok (scheme claim refreshed)" \
  || fail "lsregister -f failed"

# 2b. Dev machines often carry MULTIPLE registered copies of the app (stale
# worktrees/clone leftovers), and `lsregister -f` refreshes registration but
# does NOT change which copy LaunchServices treats as the DEFAULT handler for
# xpst:// — the URL then silently routes to a stale clone and this E2E fails
# with no marker. Force the freshly-built app to be the default handler.
if command -v swift >/dev/null 2>&1; then
  cat > "$WORK/setdefault.swift" <<'SWIFT'
import CoreServices
import Foundation
let appPath = CommandLine.arguments[1]
guard let bundle = Bundle(url: URL(fileURLWithPath: appPath)),
      let bid = bundle.bundleIdentifier else {
    print("NO_BUNDLE_ID"); exit(1)
}
LSSetDefaultHandlerForURLScheme("xpst" as CFString, bid as CFString)
let probe = URL(string: "xpst://selfcheck")!
if let handler = LSCopyDefaultApplicationURLForURL(probe as CFURL, .all, nil)?.takeRetainedValue() {
    print("DEFAULT_HANDLER=\(handler)")
} else {
    print("DEFAULT_HANDLER=none")
}
SWIFT
  echo "== 2b. default-handler pinning for xpst:// =="
  OUT="$(swift "$WORK/setdefault.swift" "$APP_BUNDLE" 2>/dev/null)"
  echo "$OUT"
  EXPECTED="file://$APP_BUNDLE/"
  if echo "$OUT" | grep -q "DEFAULT_HANDLER=$EXPECTED"; then
    pass "default xpst:// handler is the freshly-built app"
  else
    fail "default xpst:// handler is not this app (got: $OUT)"
  fi
fi

echo
echo "== 3. Single instance: launch twice =="
rm -f "$XPST_SHELL_LOG" "$XPST_DEEPLINK_MARKER"
"$BIN_PATH" >/dev/null 2>&1 &
sleep 3 # window settle (cold boot ~0.25 s median; 3 s is generous)
COUNT1=$(pgrep -f "$BIN_DIR" | wc -l | tr -d ' ')
echo "process listing after first launch:"; pgrep -fl "$BIN_DIR" || true
[[ "$COUNT1" == "1" ]] || fail "expected 1 instance after first launch, found $COUNT1"

# GUI evidence: the app's window must actually be on screen (not headless).
APP_PID="$(pgrep -f "$BIN_DIR" | head -1)"
if command -v swift >/dev/null 2>&1 && [[ -n "$APP_PID" ]]; then
  cat > "$WORK/winlister.swift" <<'SWIFT'
import CoreGraphics
import Foundation
let pid = Int32(CommandLine.arguments[1])!
let opts = CGWindowListOption([.optionOnScreenOnly, .excludeDesktopElements])
if let info = CGWindowListCopyWindowInfo(opts, kCGNullWindowID) as? [[String: Any]] {
    let mine = info.filter { ($0[kCGWindowOwnerPID as String] as? Int32) == pid }
    for w in mine {
        let name = w[kCGWindowName as String] as? String ?? ""
        let owner = w[kCGWindowOwnerName as String] as? String ?? ""
        print("WINDOW owner=\(owner) name=\(name)")
    }
    print("WINDOW_COUNT=\(mine.count)")
}
SWIFT
  WIN_OUT="$(swift "$WORK/winlister.swift" "$APP_PID" 2>/dev/null)"
  echo "GUI window evidence (CGWindowList, pid $APP_PID):"
  echo "$WIN_OUT"
  echo "$WIN_OUT" | grep -q "WINDOW_COUNT=[1-9]" \
    && pass "app window is on screen (live GUI)" \
    || fail "no on-screen window found for pid $APP_PID"
fi

"$BIN_PATH" >/dev/null 2>&1 &
SECOND_PID=$!
sleep 3
COUNT2=$(pgrep -f "$BIN_DIR" | wc -l | tr -d ' ')
echo "process listing after second launch (pid $SECOND_PID should be gone):"
pgrep -fl "$BIN_DIR" || true
if [[ "$COUNT2" == "1" ]]; then
  if ! kill -0 "$SECOND_PID" 2>/dev/null; then
    pass "second launch (pid $SECOND_PID) exited; exactly 1 process remains"
  else
    pass "exactly 1 process remains, but second pid $SECOND_PID still alive?!"
    fail "second instance did not exit"
  fi
else
  fail "expected 1 process after second launch, found $COUNT2"
fi

echo
echo "== 4. Deep link at runtime (with engine forwarding) =="
rm -f "$XPST_DEEPLINK_MARKER"
ENGINE_PID=""
if command -v uv >/dev/null 2>&1; then
  # REAL engine: run the actual FastAPI dashboard (the same _create_app the
  # PyInstaller sidecar bundles) on 127.0.0.1:$XPST_ENGINE_PORT, so
  # outcome=forwarded:200 proves the REAL POST /oauth/callback route —
  # not a stub HTTP server.
  XPST_DASHBOARD_PORT="$XPST_ENGINE_PORT" PYTHONPATH="$REPO_ROOT/src" \
    uv run --quiet --extra dev python scripts/engine_entry.py >"$WORK/engine.log" 2>&1 &
  ENGINE_PID=$!
  for _ in $(seq 1 40); do # up to 20 s (uv env sync can be slow on cold start)
    if curl -sf -o /dev/null "http://127.0.0.1:$XPST_ENGINE_PORT/health"; then break; fi
    sleep 0.5
  done
fi

if [[ -n "$ENGINE_PID" ]] && curl -sf -o /dev/null "http://127.0.0.1:$XPST_ENGINE_PORT/health"; then
  pass "real engine dashboard healthy on 127.0.0.1:$XPST_ENGINE_PORT"
  # Direct probe of the real route (mirrors what the shell will POST).
  CB_STATUS="$(curl -s -o "$WORK/curl-body.json" -w '%{http_code}' -X POST \
    -H 'Content-Type: application/json' \
    -d '{"source":"deeplink-e2e","url":"xpst://callback?code=e2e-direct&state=e2e-direct"}' \
    "http://127.0.0.1:$XPST_ENGINE_PORT/oauth/callback")"
  echo "direct POST /oauth/callback -> HTTP $CB_STATUS body: $(cat "$WORK/curl-body.json" 2>/dev/null)"
  [[ "$CB_STATUS" == "200" ]] \
    && pass "real dashboard /oauth/callback route answers 200" \
    || fail "real dashboard /oauth/callback route got HTTP $CB_STATUS"
else
  echo "NOTE: uv/engine unavailable — skipped live engine check"
fi

open "$DEEPLINK_URL" || fail "open $DEEPLINK_URL"

RECEIVED=""
for _ in $(seq 1 30); do # up to 15 s
  if [[ -s "$XPST_DEEPLINK_MARKER" ]]; then RECEIVED="$(cat "$XPST_DEEPLINK_MARKER")"; break; fi
  sleep 0.5
done
if [[ "$RECEIVED" == "$DEEPLINK_URL" ]]; then
  pass "deep link received verbatim: $RECEIVED"
else
  fail "marker mismatch (got: '${RECEIVED:-<none>}' want: '$DEEPLINK_URL')"
fi

grep -q "DEEPLINK_RECEIVED url=$DEEPLINK_URL" "$XPST_SHELL_LOG" \
  && pass "log line DEEPLINK_RECEIVED present" \
  || fail "no DEEPLINK_RECEIVED in $XPST_SHELL_LOG"

if [[ -n "$ENGINE_PID" ]]; then
  sleep 1
  if grep -q "DEEPLINK_FORWARDED url=$DEEPLINK_URL outcome=forwarded:200" "$XPST_SHELL_LOG" \
     && grep -q "OAUTH_CALLBACK_RECEIVED" "$WORK/engine.log"; then
    pass "URL forwarded to the REAL engine route at 127.0.0.1:$XPST_ENGINE_PORT/oauth/callback (200)"
    echo "engine-side evidence (real dashboard route log line):"
    grep "OAUTH_CALLBACK_RECEIVED" "$WORK/engine.log"
    echo "shell-side outcome line:"
    grep "DEEPLINK_FORWARDED" "$XPST_SHELL_LOG"
  else
    fail "forwarding outcome not forwarded:200 (shell: $(grep DEEPLINK_FORWARDED "$XPST_SHELL_LOG" || echo none); engine: $(grep -c OAUTH_CALLBACK_RECEIVED "$WORK/engine.log" 2>/dev/null || echo 0) route lines)"
  fi
else
  echo "NOTE: no engine — skipped live engine check; shell-side outcome: $(grep -o 'outcome=[a-z_:0-9]*' "$XPST_SHELL_LOG" | tail -1)"
fi

echo
echo "== shell log =="
cat "$XPST_SHELL_LOG" || true

if [[ "$FAILED" == "0" ]]; then
  echo
  echo "ALL E2E CHECKS PASSED"
  exit 0
else
  echo
  echo "E2E FAILED"
  exit 1
fi
