"""Link-in-bio page rendering for the xPST dashboard.

A self-hosted, mobile-first "Link in Bio" page (Content360-style) served by
the FastAPI dashboard at ``/bio``. Social links are derived automatically
from the enabled accounts in config that carry a handle (youtube, x,
instagram, tiktok, threads); custom links come from the ``bio``
config section.

Pure functions here — no FastAPI dependency — so the renderer is trivially
unit-testable.
"""

from __future__ import annotations

import html
from urllib.parse import quote

# (config attr, display label, handle getter)
_PLATFORMS: list[tuple[str, str, str]] = [
    ("youtube", "YouTube", "username"),
    ("x", "X", "username"),
    ("instagram", "Instagram", "username"),
    ("tiktok", "TikTok", "username"),
    ("threads", "Threads", "threads_user_id"),
]

_BIO_CSS = """
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
        -webkit-font-smoothing: antialiased;
    }
    @media (prefers-color-scheme: dark) {
        body { background: linear-gradient(180deg, #161617 0%, #1d1d1f 100%);
               color: #f5f5f7; }
        .card { background: #242426; box-shadow: 0 8px 32px rgba(0,0,0,.5); }
        .btn { background: #343437; color: #f5f5f7; }
        .btn:hover { background: #3d3d41; }
        .subtitle { color: #a1a1a6; }
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
    .avatar {
        width: 84px; height: 84px;
        border-radius: 50%;
        margin: 0 auto 16px;
        background: linear-gradient(135deg, #0a84ff, #bf5af2);
        color: #fff;
        font-size: 36px;
        font-weight: 600;
        display: flex; align-items: center; justify-content: center;
    }
    h1 { font-size: 22px; font-weight: 700; letter-spacing: -0.02em; }
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
        transition: background .15s ease, transform .1s ease;
    }
    .btn:hover { background: #e8e8ec; transform: translateY(-1px); }
    .btn:active { transform: translateY(0); }
    footer { margin-top: 28px; font-size: 12px; color: #86868b; }
    footer a { color: #86868b; }
"""

_EDIT_CSS = _BIO_CSS + """
    .card { text-align: left; }
    label.field { display: block; font-size: 13px; font-weight: 600;
                  margin: 16px 0 6px; color: #6e6e73; }
    input[type=text], input[type=url] {
        width: 100%; padding: 12px 14px; border-radius: 12px;
        border: 1px solid #d2d2d7; background: #fff; color: #1d1d1f;
        font-size: 15px;
    }
    @media (prefers-color-scheme: dark) {
        input[type=text], input[type=url] {
            background: #1c1c1e; border-color: #3a3a3c; color: #f5f5f7;
        }
    }
    .link-row { display: flex; gap: 8px; align-items: center; margin-top: 10px; }
    .link-row input { flex: 1; }
    .link-row .remove { flex: 0 0 auto; display: flex; align-items: center;
                        gap: 4px; font-size: 13px; color: #86868b;
                        white-space: nowrap; }
    button.save {
        margin-top: 20px; width: 100%; padding: 14px;
        border: none; border-radius: 14px; background: #0a84ff; color: #fff;
        font-size: 16px; font-weight: 600; cursor: pointer;
    }
    button.save:hover { background: #0071e3; }
    .hint { font-size: 12px; color: #86868b; margin-top: 8px; }
    .flash { background: #30d15833; border: 1px solid #30d15866;
             border-radius: 12px; padding: 10px 14px; font-size: 14px;
             margin-bottom: 12px; }
"""


def _url_for(platform: str, handle: str) -> str:
    """Build the public profile URL for a platform handle (or '' if unusable)."""
    h = handle.strip().lstrip("@")
    if not h:
        return ""
    safe = quote(h, safe="._-")
    if platform == "youtube":
        return f"https://youtube.com/@{safe}"
    if platform == "x":
        return f"https://x.com/{safe}"
    if platform == "instagram":
        return f"https://instagram.com/{safe}"
    if platform == "tiktok":
        return f"https://tiktok.com/@{safe}"
    if platform == "threads":
        return f"https://threads.net/@{safe}"
    return ""


def collect_links(config) -> list[dict]:
    """Return the ordered list of links for the bio page.

    Social links come from config accounts that are enabled AND have a
    usable handle. Custom links come from ``config.bio.links`` (http/https
    only, so a malicious or typo'd config cannot produce javascript: links).
    """
    links: list[dict] = []
    for attr, label, handle_field in _PLATFORMS:
        account = getattr(config, attr, None)
        if account is None or not getattr(account, "enabled", False):
            continue
        handle = str(getattr(account, handle_field, "") or "").strip()
        url = _url_for(attr, handle)
        if url:
            links.append({"label": label, "url": url})

    for item in config.bio.links:
        label = str(item.get("label", "") or "").strip()
        url = str(item.get("url", "") or "").strip()
        if label and url.startswith(("http://", "https://")):
            links.append({"label": label, "url": url})

    return links


def render_bio_page(config) -> str:
    """Render the public, mobile-first link-in-bio HTML page."""
    handle = (config.bio.handle or "My Links").strip()
    initial = handle[0].upper() if handle else "•"
    links = collect_links(config)
    items = "\n".join(
        f'      <a class="btn" href="{html.escape(link["url"])}" '
        f'target="_blank" rel="noopener noreferrer">{html.escape(link["label"])}</a>'
        for link in links
    )
    empty = (
        '<p class="subtitle">No links yet — add some via '
        '<a href="/bio/edit">/bio/edit</a></p>'
        if not links
        else '<p class="subtitle">Follow me everywhere</p>'
    )
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="index,follow">
<title>{html.escape(handle)} · xPST Link in Bio</title>
<style>{_BIO_CSS}</style>
</head>
<body>
<main class="card">
  <div class="avatar">{html.escape(initial)}</div>
  <h1>{html.escape(handle)}</h1>
  {empty}
  <div class="links">
{items}
  </div>
  <footer>Powered by <a href="/">xPST</a></footer>
</main>
</body>
</html>
"""


def render_bio_edit_page(config, saved: bool = False) -> str:
    """Render the auth-protected admin form for editing the bio page."""
    handle = config.bio.handle
    rows = []
    for i, link in enumerate(config.bio.links):
        rows.append(f"""    <div class="link-row">
      <input type="text" name="label_{i}" value="{html.escape(str(link.get('label', '')))}" placeholder="Label">
      <input type="url" name="url_{i}" value="{html.escape(str(link.get('url', '')))}" placeholder="https://...">
      <label class="remove"><input type="checkbox" name="remove_{i}" value="1"> Remove</label>
    </div>""")
    rows_html = "\n".join(rows)
    flash = '<div class="flash">Saved.</div>' if saved else ""
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Edit Link in Bio · xPST</title>
<style>{_EDIT_CSS}</style>
</head>
<body>
<main class="card">
  <h1>Edit Link in Bio</h1>
  {flash}
  <form method="post" action="/bio/edit">
    <label class="field" for="handle">Display name</label>
    <input type="text" id="handle" name="handle" value="{html.escape(handle)}" placeholder="Your name or brand">
    <label class="field">Social links (auto — edit in config accounts)</label>
    <p class="hint">YouTube, X, Instagram, TikTok and Threads links
    are generated automatically from enabled accounts with a handle.</p>
    <label class="field">Custom links</label>
{rows_html}
    <div class="link-row">
      <input type="text" name="new_label" placeholder="New label">
      <input type="url" name="new_url" placeholder="https://...">
    </div>
    <button class="save" type="submit">Save</button>
  </form>
  <p class="hint"><a href="/bio">View page</a></p>
</main>
</body>
</html>
"""
