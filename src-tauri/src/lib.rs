// xPST Tauri 2 shell — single-instance + `xpst://` deep-link handling.
//
// Phase 3 responsibilities:
//   1. Single instance: a second launch focuses the existing window and exits
//      (tauri-plugin-single-instance, registered FIRST in the builder chain).
//   2. Deep link: `xpst://` URLs (OAuth callbacks) are received at runtime via
//      `deep_link().on_open_url()` (covers both cold-start and running-app
//      opens on desktop; the plugin consumes RunEvent::Opened internally).
//   3. Engine forwarding: each received URL is POSTed as JSON
//      `{"url": "...", "source": "tauri-deep-link"}` to the dashboard engine's
//      `http://127.0.0.1:<port>/oauth/callback` route (port from
//      XPST_ENGINE_PORT, default 8080 — matches `xpst dashboard`). The
//      engine-side listener contract lives in `src/xpst/utils/oauth_local.py`
//      (local redirect capture: code + state); the shell is a dumb pipe — it
//      never inspects or consumes the OAuth payload itself.
//
// Proof-of-receipt instrumentation (also used by scripts/deeplink-e2e.sh):
//   - marker file (XPST_DEEPLINK_MARKER, default
//     $TMPDIR/xpst-deeplink-last-url.txt): last received URL, verbatim.
//   - log file (XPST_SHELL_LOG, default $TMPDIR/xpst-tauri-shell.log):
//     SHELL_STARTED / DEEPLINK_RECEIVED / DEEPLINK_FORWARDED lines with
//     outcome=forwarded:<status> | engine_unreachable:<err> | http:<status>.

use std::io::{Read, Write};
use std::net::TcpStream;
use std::time::Duration;

use tauri::{Manager, Url};
use tauri_plugin_deep_link::DeepLinkExt;

const ENGINE_HOST: &str = "127.0.0.1";
const ENGINE_CALLBACK_PATH: &str = "/oauth/callback";
const HTTP_TIMEOUT: Duration = Duration::from_secs(2);

fn marker_path() -> std::path::PathBuf {
    std::env::var("XPST_DEEPLINK_MARKER")
        .map(std::path::PathBuf::from)
        .unwrap_or_else(|_| std::env::temp_dir().join("xpst-deeplink-last-url.txt"))
}

fn log_path() -> std::path::PathBuf {
    std::env::var("XPST_SHELL_LOG")
        .map(std::path::PathBuf::from)
        .unwrap_or_else(|_| std::env::temp_dir().join("xpst-tauri-shell.log"))
}

fn engine_port() -> u16 {
    std::env::var("XPST_ENGINE_PORT")
        .ok()
        .and_then(|p| p.parse().ok())
        .unwrap_or(8080)
}

fn shell_log(line: &str) {
    eprintln!("[xpst-shell] {line}");
    if let Ok(mut f) = std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(log_path())
    {
        let _ = writeln!(f, "{line}");
    }
}

/// Forward a received `xpst://` URL to the engine's OAuth callback route.
/// Returns a machine-readable outcome for the log:
///   forwarded:<status> — engine answered with that HTTP status
///   http:<status>      — engine answered but the route rejected it (e.g. 404
///                        on an engine older than the shell)
///   engine_unreachable:<err> — dashboard not running (expected in tests)
fn forward_to_engine(url: &str) -> String {
    let port = engine_port();
    let body = serde_json::json!({ "url": url, "source": "tauri-deep-link" }).to_string();
    let result = (|| -> Result<u16, String> {
        let addr = format!("{ENGINE_HOST}:{port}");
        let mut stream = TcpStream::connect(&addr).map_err(|e| e.to_string())?;
        stream
            .set_read_timeout(Some(HTTP_TIMEOUT))
            .map_err(|e| e.to_string())?;
        stream
            .set_write_timeout(Some(HTTP_TIMEOUT))
            .map_err(|e| e.to_string())?;
        let request = format!(
            "POST {ENGINE_CALLBACK_PATH} HTTP/1.1\r\n\
             Host: {ENGINE_HOST}:{port}\r\n\
             Content-Type: application/json\r\n\
             Content-Length: {}\r\n\
             Connection: close\r\n\
             \r\n\
             {body}",
            body.len()
        );
        stream
            .write_all(request.as_bytes())
            .map_err(|e| e.to_string())?;
        let mut raw = Vec::new();
        stream
            .read_to_end(&mut raw)
            .map_err(|e| e.to_string())?;
        let text = String::from_utf8_lossy(&raw);
        // "HTTP/1.1 200 OK" -> 200
        let status = text
            .split_whitespace()
            .nth(1)
            .and_then(|s| s.parse::<u16>().ok());
        match status {
            Some(code) => Ok(code),
            None => {
                let head: String = text.chars().take(120).collect();
                Err(format!("bad_response: {head}"))
            }
        }
    })();

    match result {
        Ok(status) if (200..300).contains(&status) => format!("forwarded:{status}"),
        Ok(status) => format!("http:{status}"),
        Err(e) => format!("engine_unreachable:{e}"),
    }
}

/// Handle one received deep link: prove receipt, focus the window, forward.
fn handle_deep_link(app: &tauri::AppHandle, url: &Url) {
    let url_str = url.to_string();
    // 1) Proof of receipt — marker file holds the URL verbatim.
    if let Err(e) = std::fs::write(marker_path(), &url_str) {
        shell_log(&format!("DEEPLINK_MARKER_ERROR error={e}"));
    }
    shell_log(&format!("DEEPLINK_RECEIVED url={url_str}"));

    // 2) Bring the shell to the front (user just finished an OAuth consent).
    if let Some(win) = app.get_webview_window("main") {
        let _ = win.show();
        let _ = win.unminimize();
        let _ = win.set_focus();
    }

    // 3) Forward to the engine's OAuth callback route (best effort).
    let outcome = forward_to_engine(&url_str);
    shell_log(&format!("DEEPLINK_FORWARDED url={url_str} outcome={outcome}"));
}

fn focus_existing(app: &tauri::AppHandle) {
    if let Some(win) = app.get_webview_window("main") {
        let _ = win.show();
        let _ = win.unminimize();
        let _ = win.set_focus();
    }
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    let app = tauri::Builder::default()
        // Single-instance MUST be registered first (plugin docs requirement).
        .plugin(tauri_plugin_single_instance::init(|app, _args, _cwd| {
            // Second launch: focus the existing window; this process then
            // exits and LaunchServices never shows a second window.
            focus_existing(app);
        }))
        .plugin(tauri_plugin_updater::Builder::new().build())
        .plugin(tauri_plugin_deep_link::init())
        .setup(|app| {
            let handle = app.handle().clone();
            // Runtime + cold-start deep links (desktop: kAEGetURL Apple
            // events). on_open_url covers both; RunEvent::Opened below is a
            // fallback for events that arrive outside the plugin's window.
            handle.clone().deep_link().on_open_url(move |event| {
                for url in event.urls() {
                    handle_deep_link(&handle, &url);
                }
            });
            shell_log(&format!("SHELL_STARTED pid={}", std::process::id()));
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("error while building tauri application");

    // App::run returns () and panics internally on a fatal event-loop error.
    // NOTE: deep links are handled exclusively via on_open_url() above — the
    // plugin consumes RunEvent::Opened internally, so handling it here as
    // well would deliver every URL twice (verified in e2e before dedup).
    app.run(|_app_handle, _event| {});
}
