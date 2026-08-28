// xPST desktop shell — Tauri 2 wrapper around the Python engine sidecar.
//
// Responsibilities:
//   1. Engine lifecycle: pick a free ephemeral port, spawn the PyInstaller
//      onedir engine (bundle resource `binaries/engine/`) with
//      XPST_DASHBOARD_PORT set, poll GET /health (60s timeout, any HTTP
//      response = up), then navigate the webview to the engine URL. The
//      sidecar is killed on exit/panic/SIGTERM/SIGINT.
//   2. Single instance: a second launch focuses the existing window and exits
//      (tauri-plugin-single-instance, registered FIRST in the builder chain).
//   3. Deep link: `xpst://` URLs (OAuth callbacks) are received at runtime via
//      `deep_link().on_open_url()` (covers both cold-start and running-app
//      opens on desktop; the plugin consumes RunEvent::Opened internally).
//   4. Engine forwarding: each received URL is POSTed as JSON
//      `{"url": "...", "source": "tauri-deep-link"}` to the dashboard engine's
//      `http://127.0.0.1:<port>/oauth/callback` route (port resolution below —
//      matches the port this shell spawned the engine on). The engine-side
//      listener contract lives in `src/xpst/utils/oauth_local.py` (local
//      redirect capture: code + state); the shell is a dumb pipe — it never
//      inspects or consumes the OAuth payload itself.
//
// Boot probes (stderr, consumed by scripts/tauri-smoke.sh):
//   BOOT_TO_VISIBLE_SECS / ENGINE_HEALTH_WAIT_SECS / BOOT_TO_READY_SECS /
//   WEBVIEW_URL — and engine lifecycle log lines under [xpst-shell].
//
// Deep-link proof-of-receipt instrumentation (also used by scripts/deeplink-e2e.sh):
//   - marker file (XPST_DEEPLINK_MARKER, default
//     $TMPDIR/xpst-deeplink-last-url.txt): last received URL, verbatim.
//   - log file (XPST_SHELL_LOG, default $TMPDIR/xpst-tauri-shell.log):
//     SHELL_STARTED / DEEPLINK_RECEIVED / DEEPLINK_FORWARDED lines with
//     outcome=forwarded:<status> | engine_unreachable:<err> | http:<status>.

use std::io::{Read, Write};
use std::net::{TcpListener, TcpStream};
use std::sync::{Arc, Mutex, OnceLock};
use std::time::{Duration, Instant};

use tauri::{Manager, RunEvent, WebviewWindow, Url};
use tauri_plugin_deep_link::DeepLinkExt;
use tauri_plugin_shell::process::{CommandChild, CommandEvent};
use tauri_plugin_shell::ShellExt;
use tauri_plugin_updater::UpdaterExt;

/// How long to wait for the engine to become healthy before giving up.
const ENGINE_HEALTH_TIMEOUT: Duration = Duration::from_secs(60);
/// Interval between health probes.
const ENGINE_POLL_INTERVAL: Duration = Duration::from_millis(150);
/// TCP connect/read timeout for a single health probe.
const PROBE_TIMEOUT: Duration = Duration::from_secs(2);
/// Engine callback host + path (deep-link OAuth forwarding).
const ENGINE_HOST: &str = "127.0.0.1";
const ENGINE_CALLBACK_PATH: &str = "/oauth/callback";
const HTTP_TIMEOUT: Duration = Duration::from_secs(2);

/// Process-start timestamp (set once, used for the boot-time probes).
static BOOT_START: OnceLock<Instant> = OnceLock::new();
/// Port the shell spawned the engine on (set once the engine is up).
static ENGINE_PORT: OnceLock<u16> = OnceLock::new();

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

/// Engine port resolution for deep-link forwarding: an explicit
/// XPST_ENGINE_PORT (e2e/testing against an externally started engine)
/// wins, otherwise the port this shell spawned the engine on, else 8080
/// (matches `xpst dashboard`).
fn engine_port() -> u16 {
    if let Ok(p) = std::env::var("XPST_ENGINE_PORT") {
        if let Ok(p) = p.parse() {
            return p;
        }
    }
    if let Some(p) = ENGINE_PORT.get() {
        return *p;
    }
    8080
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

fn log(msg: &str) {
    shell_log(msg);
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
        log(&format!("DEEPLINK_MARKER_ERROR error={e}"));
    }
    log(&format!("DEEPLINK_RECEIVED url={url_str}"));

    // 2) Bring the shell to the front (user just finished an OAuth consent).
    focus_existing(app);

    // 3) Forward to the engine's OAuth callback route (best effort).
    let outcome = forward_to_engine(&url_str);
    log(&format!("DEEPLINK_FORWARDED url={url_str} outcome={outcome}"));
}

fn focus_existing(app: &tauri::AppHandle) {
    if let Some(win) = app.get_webview_window("main") {
        let _ = win.show();
        let _ = win.unminimize();
        let _ = win.set_focus();
    }
}

/// Shared handle to the spawned engine child process so it can be killed
/// from the exit path and the panic hook.
#[derive(Default)]
struct EngineHandle {
    child: Mutex<Option<CommandChild>>,
}

/// Global access point for the signal handler (which cannot reach Tauri
/// state). Set once the engine child is spawned.
static GLOBAL_ENGINE: OnceLock<Arc<EngineHandle>> = OnceLock::new();

extern "C" fn handle_exit_signal(_sig: libc::c_int) {
    // SIGTERM/SIGINT do NOT produce a Tauri RunEvent::ExitRequested, so
    // without this the engine sidecar would be orphaned when the shell is
    // terminated externally. Kill the child, then exit with 128+signal.
    if let Some(engine) = GLOBAL_ENGINE.get() {
        engine.kill();
    }
    std::process::exit(128 + _sig as i32);
}

fn install_signal_handlers() {
    unsafe {
        libc::signal(libc::SIGTERM, handle_exit_signal as libc::sighandler_t);
        libc::signal(libc::SIGINT, handle_exit_signal as libc::sighandler_t);
    }
}

impl EngineHandle {
    fn kill(&self) {
        if let Some(child) = self.child.lock().unwrap().take() {
            let _ = child.kill();
        }
    }
}

/// Pick a free loopback port by binding an ephemeral listener and
/// immediately releasing it. Small TOCTOU window exists, but the engine
/// is the only consumer and retries are the caller's concern.
pub fn pick_free_port() -> std::io::Result<u16> {
    let listener = TcpListener::bind(("127.0.0.1", 0))?;
    let port = listener.local_addr()?.port();
    drop(listener);
    Ok(port)
}

/// Issue a minimal HTTP/1.1 request and return `true` if the server
/// answers with any valid HTTP response (FastAPI returning *any* HTTP
/// response means it is up — 200, 401, 404 all count).
fn http_responds(addr: &str, path: &str) -> bool {
    let mut stream = match TcpStream::connect(addr) {
        Ok(s) => s,
        Err(_) => return false,
    };
    let _ = stream.set_read_timeout(Some(PROBE_TIMEOUT));
    let _ = stream.set_write_timeout(Some(PROBE_TIMEOUT));
    let request = format!(
        "GET {path} HTTP/1.1\r\nHost: {addr}\r\nConnection: close\r\n\r\n"
    );
    if stream.write_all(request.as_bytes()).is_err() {
        return false;
    }
    let mut buf = [0u8; 128];
    match stream.read(&mut buf) {
        Ok(n) if n >= 5 => buf.starts_with(b"HTTP/"),
        _ => false,
    }
}

/// Poll the engine health endpoint until it responds or `deadline`
/// expires. Returns the elapsed wait time.
fn wait_for_engine_health(port: u16, timeout: Duration) -> Option<Duration> {
    let addr = format!("127.0.0.1:{port}");
    let deadline = Instant::now() + timeout;
    loop {
        if http_responds(&addr, "/health") {
            return Some(BOOT_START.get().map_or(Duration::ZERO, |s| s.elapsed()));
        }
        if Instant::now() >= deadline {
            return None;
        }
        std::thread::sleep(ENGINE_POLL_INTERVAL);
    }
}

/// Show a minimal error page inside the webview (used when the engine
/// fails to become healthy within the timeout).
fn show_engine_error(window: &WebviewWindow) {
    let html = "<!doctype html><html><body style='font-family:-apple-system,sans-serif;\
        display:flex;align-items:center;justify-content:center;height:100vh;margin:0;\
        background:#161617;color:#f5f5f7'><div style='text-align:center'>\
        <h1>xPST could not start</h1>\
        <p>The engine failed to become healthy within 60 seconds.<br>\
        Please restart the application.</p></div></body></html>";
    let js = format!(
        "document.open();document.write({});document.close();",
        serde_json::to_string(html).unwrap_or_default()
    );
    let _ = window.eval(&js);
}

/// Boot the engine sidecar, wait for health, then navigate the main
/// window onto the engine URL. Runs on a background thread from `setup`.
fn boot_engine(app: tauri::AppHandle) {
    let started = BOOT_START.get().copied().unwrap_or_else(Instant::now);

    let port = match pick_free_port() {
        Ok(p) => p,
        Err(e) => {
            log(&format!("FATAL: could not pick a free port: {e}"));
            if let Some(w) = app.get_webview_window("main") {
                show_engine_error(&w);
            }
            return;
        }
    };
    log(&format!("engine port: {port}"));

    // Spawn the engine from the bundle resources (onedir sidecar).
    //
    // NOTE (tradeoff vs externalBin): Tauri `externalBin` requires a single
    // executable file, which forced PyInstaller --onefile — but onefile
    // self-extracts ~45 MB to a temp dir on EVERY launch (~1.3 s), blowing
    // the boot-to-ready <= 1 s gate. Shipping the onedir engine as a bundle
    // resource (`bundle.resources: ["binaries/engine/"]`) removes the
    // extraction step; the cost is manual process management (env, kill)
    // which this module owns.
    let engine_dir = match app.path().resource_dir() {
        Ok(dir) => dir.join("binaries/engine"),
        Err(e) => {
            log(&format!("FATAL: no resource dir: {e}"));
            if let Some(w) = app.get_webview_window("main") {
                show_engine_error(&w);
            }
            return;
        }
    };
    let engine_exe = engine_dir.join("xpst-engine");
    if !engine_exe.exists() {
        log(&format!(
            "FATAL: engine executable missing at {}",
            engine_exe.display()
        ));
        if let Some(w) = app.get_webview_window("main") {
            show_engine_error(&w);
        }
        return;
    }
    let mut command = app.shell().command(&engine_exe);
    command = command.env("XPST_DASHBOARD_PORT", port.to_string());
    command = command.env("XPST_ENGINE_MODE", "tauri");

    let (mut rx, child) = match command.spawn() {
        Ok(pair) => pair,
        Err(e) => {
            log(&format!("FATAL: failed to spawn engine sidecar: {e}"));
            if let Some(w) = app.get_webview_window("main") {
                show_engine_error(&w);
            }
            return;
        }
    };
    app.state::<Arc<EngineHandle>>()
        .child
        .lock()
        .unwrap()
        .replace(child);
    let _ = GLOBAL_ENGINE.set(Arc::clone(
        &app.state::<Arc<EngineHandle>>().inner().clone(),
    ));
    let _ = ENGINE_PORT.set(port);

    // Drain sidecar output so pipes never fill; surface stderr in debug.
    let drain = tauri::async_runtime::spawn(async move {
        while let Some(event) = rx.recv().await {
            match event {
                CommandEvent::Stdout(line) => {
                    log(&format!("engine stdout: {}", String::from_utf8_lossy(&line)));
                }
                CommandEvent::Stderr(line) => {
                    log(&format!("engine stderr: {}", String::from_utf8_lossy(&line)));
                }
                CommandEvent::Terminated(status) => {
                    log(&format!("engine terminated: {status:?}"));
                }
                CommandEvent::Error(err) => {
                    log(&format!("engine error: {err}"));
                }
                _ => {}
            }
        }
    });

    match wait_for_engine_health(port, ENGINE_HEALTH_TIMEOUT) {
        Some(_since_boot) => {
            let health_wait = started.elapsed();
            log(&format!("ENGINE_HEALTH_WAIT_SECS={:.3}", health_wait.as_secs_f64()));

            if let Some(window) = app.get_webview_window("main") {
                let url = tauri::Url::parse(&format!("http://127.0.0.1:{port}/"))
                    .expect("valid engine URL");
                if let Err(e) = window.navigate(url) {
                    log(&format!("navigate failed, falling back to eval: {e}"));
                    let _ = window
                        .eval(&format!("window.location.replace('http://127.0.0.1:{port}/')"));
                }
                let _ = window.show();
                let _ = window.set_focus();
                let total = BOOT_START.get().map_or(health_wait, |s| s.elapsed());
                log(&format!("BOOT_TO_READY_SECS={:.3}", total.as_secs_f64()));

                // Give the webview a moment to commit the navigation, then
                // dump its URL to stderr (used by scripts/tauri-smoke.sh as
                // navigation evidence).
                std::thread::sleep(Duration::from_secs(2));
                match window.url() {
                    Ok(url) => log(&format!("WEBVIEW_URL={url}")),
                    Err(e) => log(&format!("could not read webview url: {e}")),
                }
            }
        }
        None => {
            log("FATAL: engine did not become healthy within timeout");
            if let Some(w) = app.get_webview_window("main") {
                show_engine_error(&w);
            }
        }
    }
    drain.abort();
}

// ---------------------------------------------------------------------------
// Updater E2E (Phase 2).
//
// The setup hook writes boot-version markers so an E2E test can PROVE the
// running process's version (marker file written by the app itself):
//   $WORK/started-<version>.txt — one file per boot (canonical /private/tmp
//   path: tauri-plugin-updater refuses to run when current_exe() crosses a
//   symlink, and /tmp is a symlink to /private/tmp on macOS)
//   $WORK/current.txt           — overwritten with the latest boot's version
//
// The updater check is OPT-IN via XPST_UPDATER_CHECK=1 so normal boots never
// auto-update. On match: check() -> download_and_install() -> restart().
const UPDATER_MARKER_DIR: &str = "/private/tmp/xpst-updater-e2e";

fn write_version_marker(version: &str) {
    if std::fs::create_dir_all(UPDATER_MARKER_DIR).is_err() {
        return;
    }
    let stamp = format!(
        "{} {}",
        version,
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.as_secs().to_string())
            .unwrap_or_default()
    );
    std::fs::write(format!("{UPDATER_MARKER_DIR}/started-{version}.txt"), &stamp).ok();
    std::fs::write(format!("{UPDATER_MARKER_DIR}/current.txt"), &stamp).ok();
}

fn run_updater_check(handle: tauri::AppHandle) {
    std::thread::spawn(move || {
        tauri::async_runtime::block_on(async move {
            let updater = match handle.updater() {
                Ok(u) => u,
                Err(e) => {
                    eprintln!("[xpst-updater] updater() failed: {e}");
                    return;
                }
            };
            match updater.check().await {
                Ok(Some(update)) => {
                    println!(
                        "[xpst-updater] update available: {} -> {}",
                        update.current_version, update.version
                    );
                    let mut downloaded: usize = 0;
                    match update
                        .download_and_install(
                            |chunk, total| {
                                downloaded += chunk;
                                if let Some(t) = total {
                                    println!("[xpst-updater] downloaded {downloaded}/{t} bytes");
                                }
                            },
                            || {
                                println!("[xpst-updater] download finished; installing");
                            },
                        )
                        .await
                    {
                        Ok(()) => {
                            println!(
                                "[xpst-updater] installed; restarting into {}",
                                update.version
                            );
                            handle.restart();
                        }
                        Err(e) => {
                            eprintln!("[xpst-updater] download_and_install failed: {e}");
                        }
                    }
                }
                Ok(None) => println!("[xpst-updater] no update available"),
                Err(e) => eprintln!("[xpst-updater] check failed: {e}"),
            }
        });
    });
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    BOOT_START.get_or_init(Instant::now);
    install_signal_handlers();

    let engine = Arc::new(EngineHandle::default());

    // Kill the engine child if the shell panics.
    let panic_engine = Arc::clone(&engine);
    let default_hook = std::panic::take_hook();
    std::panic::set_hook(Box::new(move |info| {
        panic_engine.kill();
        default_hook(info);
    }));

    let app = tauri::Builder::default()
        // Single-instance MUST be registered first (plugin docs requirement).
        .plugin(tauri_plugin_single_instance::init(|app, args, _cwd| {
            // Second launch: focus the existing window; this process then
            // exits and LaunchServices never shows a second window.
            focus_existing(app);
            // Deep-link forwarding (macOS): when LaunchServices starts a
            // second instance with the URL as argv instead of Apple-eventing
            // the running one, the URL arrives HERE via the single-instance
            // socket. tauri-plugin-deep-link's own handle_cli_arguments()
            // only parses argv on Windows/Linux, so macOS must do it here.
            // Note: we must NOT call focus + return — parse args even when
            // the window already exists.
            for arg in args.iter() {
                if let Ok(url) = arg.parse::<Url>() {
                    if url.scheme() == "xpst" {
                        handle_deep_link(app, &url);
                    }
                }
            }
        }))
        .plugin(tauri_plugin_shell::init())
        .plugin(tauri_plugin_updater::Builder::new().build())
        .plugin(tauri_plugin_deep_link::init())
        .setup(move |app| {
            app.manage(Arc::clone(&engine));

            // Runtime + cold-start deep links (desktop: kAEGetURL Apple
            // events). on_open_url covers both; RunEvent::Opened below is a
            // fallback for events that arrive outside the plugin's window.
            let handle = app.handle().clone();
            handle.clone().deep_link().on_open_url(move |event| {
                for url in event.urls() {
                    handle_deep_link(&handle, &url);
                }
            });
            shell_log(&format!("SHELL_STARTED pid={}", std::process::id()));

            // Updater E2E (Phase 2): boot-version marker + opt-in update check.
            let version = app.package_info().version.to_string();
            write_version_marker(&version);
            if std::env::var("XPST_UPDATER_CHECK").ok().as_deref() == Some("1") {
                run_updater_check(app.handle().clone());
            }

            // In-process boot-to-visible probe: the window is created
            // (visible) from config before setup runs, so elapsed time here
            // is process-start -> window on screen.
            if let Some(start) = BOOT_START.get() {
                log(&format!(
                    "BOOT_TO_VISIBLE_SECS={:.3}",
                    start.elapsed().as_secs_f64()
                ));
            }

            let handle = app.handle().clone();
            std::thread::spawn(move || boot_engine(handle));
            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("error while building tauri application");

    // Deep links are handled exclusively via on_open_url() above — the
    // plugin consumes RunEvent::Opened internally, so handling it here as
    // well would deliver every URL twice (verified in e2e before dedup).
    app.run(|app, event| match event {
        RunEvent::ExitRequested { .. } | RunEvent::Exit => {
            app.state::<Arc<EngineHandle>>().kill();
        }
        _ => {}
    });
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn pick_free_port_returns_distinct_usable_ports() {
        let a = pick_free_port().expect("first port");
        let b = pick_free_port().expect("second port");
        assert_ne!(a, b, "two consecutive picks should differ in practice");
        assert!(a > 0);
        // The chosen port should accept a listener again in most cases.
        let relisten = TcpListener::bind(("127.0.0.1", a));
        assert!(relisten.is_ok(), "port {a} should typically be rebindable");
    }

    #[test]
    fn http_responds_rejects_closed_port() {
        let port = pick_free_port().expect("free port");
        assert!(!http_responds(&format!("127.0.0.1:{port}"), "/health"));
    }

    #[test]
    fn http_responds_accepts_loopback_http_server() {
        let port = pick_free_port().expect("free port");
        let listener = TcpListener::bind(("127.0.0.1", port)).expect("bind");
        std::thread::spawn(move || {
            if let Ok((mut sock, _)) = listener.accept() {
                let mut buf = [0u8; 512];
                let _ = std::io::Read::read(&mut sock, &mut buf);
                let _ = sock.write_all(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok");
            }
        });
        assert!(http_responds(&format!("127.0.0.1:{port}"), "/health"));
    }

    #[test]
    fn wait_for_engine_health_times_out_on_dead_port() {
        let port = pick_free_port().expect("free port");
        let started = Instant::now();
        let res = wait_for_engine_health(port, Duration::from_millis(300));
        assert!(res.is_none());
        assert!(started.elapsed() >= Duration::from_millis(250));
    }
}
