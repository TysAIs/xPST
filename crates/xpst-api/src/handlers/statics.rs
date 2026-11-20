//! The static shell: `ui/dist` when a build exists, a built-in HTML page when
//! it does not.
//!
//! Both are public. That is not a convenience: an unpaired phone has to be able
//! to load the page it types its pairing code into, and every value that page
//! renders comes from `/api`, which *is* gated off-loopback.
//!
//! There is no directory listing and no traversal: a requested path is resolved
//! against the UI directory and refused if it escapes.

use std::path::{Component, Path, PathBuf};

use axum::http::{header, StatusCode};
use axum::response::{IntoResponse, Response};

use crate::app::RequestContext;
use crate::handlers::http_error;

/// The fallback page: what `/` answers when no UI build is present.
///
/// A compact rendering of the Python fallback's landing page — the contract is
/// that `/` answers HTML that boots the shell, not that the bytes match.
pub const FALLBACK_INDEX: &str = r#"<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>xPST Dashboard</title>
<style>
    :root { color-scheme: light dark; }
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
        font-family: -apple-system, BlinkMacSystemFont, "Inter", "Segoe UI",
                     Roboto, Helvetica, Arial, sans-serif;
        background: linear-gradient(180deg, #f5f5f7 0%, #ececf0 100%);
        color: #1d1d1f;
        min-height: 100vh;
        display: flex;
        align-items: center;
        justify-content: center;
        padding: 32px 16px;
    }
    @media (prefers-color-scheme: dark) {
        body { background: linear-gradient(180deg, #161617 0%, #1d1d1f 100%); color: #f5f5f7; }
        .card { background: #242426; }
        .btn { background: #343437; color: #f5f5f7; }
        .note { color: #a1a1a6; }
    }
    .card {
        background: #ffffff;
        border-radius: 24px;
        box-shadow: 0 8px 32px rgba(0,0,0,.08);
        padding: 40px 24px 28px;
        width: 100%;
        max-width: 420px;
        text-align: center;
    }
    h1 { font-size: 24px; font-weight: 700; letter-spacing: -0.02em; }
    .subtitle { font-size: 14px; color: #6e6e73; margin: 6px 0 24px; }
    .links { display: flex; flex-direction: column; gap: 12px; }
    .btn {
        display: block;
        padding: 14px 20px;
        border-radius: 14px;
        background: #f2f2f4;
        color: #1d1d1f;
        text-decoration: none;
        font-size: 15px;
        font-weight: 600;
    }
    .note { margin-top: 20px; font-size: 12px; line-height: 1.5; color: #6e6e73; text-align: left; }
</style>
</head>
<body>
    <div class="card">
        <h1>xPST</h1>
        <p class="subtitle">The web UI build is not installed.</p>
        <div class="links">
            <a class="btn" href="/health">Health</a>
            <a class="btn" href="/api/capabilities">Capabilities</a>
            <a class="btn" href="/api/providers">Providers</a>
        </div>
        <p class="note">
            This page is served by the engine itself, so the desktop shell and a
            paired phone still boot without a UI build. Build the web UI, or use
            the JSON API directly.
        </p>
    </div>
</body>
</html>
"#;

/// Content type for an asset, by extension. Unknown extensions are served as
/// `application/octet-stream`, never sniffed.
fn content_type(path: &Path) -> &'static str {
    match path
        .extension()
        .and_then(|extension| extension.to_str())
        .map(str::to_ascii_lowercase)
        .as_deref()
    {
        Some("html" | "htm") => "text/html; charset=utf-8",
        Some("js" | "mjs") => "text/javascript; charset=utf-8",
        Some("css") => "text/css; charset=utf-8",
        Some("json") => "application/json",
        Some("svg") => "image/svg+xml",
        Some("png") => "image/png",
        Some("jpg" | "jpeg") => "image/jpeg",
        Some("webp") => "image/webp",
        Some("gif") => "image/gif",
        Some("ico") => "image/x-icon",
        Some("woff") => "font/woff",
        Some("woff2") => "font/woff2",
        Some("txt") => "text/plain; charset=utf-8",
        Some("map") => "application/json",
        _ => "application/octet-stream",
    }
}

/// Resolve a request path inside `root`, refusing anything that escapes it.
fn resolve(root: &Path, requested: &str) -> Option<PathBuf> {
    let mut resolved = root.to_path_buf();
    for segment in requested.split('/') {
        if segment.is_empty() || segment == "." {
            continue;
        }
        if segment == ".." {
            return None;
        }
        if Path::new(segment)
            .components()
            .any(|component| !matches!(component, Component::Normal(_)))
        {
            return None;
        }
        resolved.push(segment);
    }
    Some(resolved)
}

fn html(body: impl Into<String>) -> Response {
    (
        StatusCode::OK,
        [(header::CONTENT_TYPE, "text/html; charset=utf-8")],
        body.into(),
    )
        .into_response()
}

fn not_found() -> Response {
    http_error(StatusCode::NOT_FOUND, "Not Found")
}

/// `GET /` — the UI index, or the built-in page.
pub fn ui_shell(context: &RequestContext<'_>) -> Response {
    if let Some(dist) = context.state.ui_dist() {
        let index = dist.join("index.html");
        if let Ok(bytes) = std::fs::read(&index) {
            return (
                StatusCode::OK,
                [(header::CONTENT_TYPE, "text/html; charset=utf-8")],
                bytes,
            )
                .into_response();
        }
    }
    html(FALLBACK_INDEX)
}

/// `GET /{*path}` — an asset from `ui/dist`.
///
/// With no UI build nothing is served, and a request for an asset is a 404 —
/// the same answer Python gives when it does not mount the static directory.
pub fn asset(context: &RequestContext<'_>) -> Response {
    let requested = context.param("path").unwrap_or("");
    let Some(dist) = context.state.ui_dist() else {
        return not_found();
    };
    let Some(mut target) = resolve(dist, requested) else {
        return not_found();
    };
    if target.is_dir() {
        target = target.join("index.html");
    }
    let Ok(bytes) = std::fs::read(&target) else {
        return not_found();
    };
    let mime = content_type(&target);
    (StatusCode::OK, [(header::CONTENT_TYPE, mime)], bytes).into_response()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn traversal_and_absolute_paths_are_refused() {
        let root = Path::new("/srv/ui");
        assert_eq!(
            resolve(root, "assets/app.js"),
            Some(PathBuf::from("/srv/ui/assets/app.js"))
        );
        assert_eq!(resolve(root, "../etc/passwd"), None);
        assert_eq!(resolve(root, "assets/../../etc/passwd"), None);
        assert_eq!(
            resolve(root, "/etc/passwd"),
            Some(PathBuf::from("/srv/ui/etc/passwd"))
        );
    }

    #[test]
    fn content_types_are_by_extension() {
        assert_eq!(
            content_type(Path::new("index.html")),
            "text/html; charset=utf-8"
        );
        assert_eq!(
            content_type(Path::new("app.js")),
            "text/javascript; charset=utf-8"
        );
        assert_eq!(
            content_type(Path::new("blob.bin")),
            "application/octet-stream"
        );
    }
}
