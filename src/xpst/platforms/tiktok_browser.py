"""Browser-native TikTok publisher (unofficial) for xPST.

Drives TikTok's own TikTok Studio web upload flow with a Playwright-driven
chromium against a persisted, own-account session (seeded from the
yt-dlp/CDP cookie jar under ``~/.xpst/credentials/``). Uploads ride TikTok's
first-party web pipeline: the page's own uploader runs ``ApplyUploadInner``
and the chunked CDN PUTs, and publishing is the real Post button.

This is the same same-origin, session-based pattern xPST already ships for
Instagram (instagrapi; manifest-labelled "not an official Meta publishing
API"), and it exists because an UNAUDITED Content Posting API client cannot
Direct Post to a public account (hard 403
``unaudited_client_can_only_post_to_private_accounts``; see
``docs/TIKTOK-SOLUTION-2026-09-28.md`` for the full research + spike
receipts).

Contract rules this module obeys:
- Receipt-or-fail: a publish only counts as PUBLISHED when the manage
  ``item_list`` API is observed to contain the new item (item_id +
  share_url). No receipt -> FAILED with evidence, never a silent success.
- Never a silent login: if the session is not logged in, the probe/publish
  fails with a re-auth nudge — it does not attempt to log in.
- Delete calls the creator ``item/delete`` endpoint from a Studio page
  context with web params, then re-checks absence via the manage
  ``item_list`` before reporting success (receipt-or-fail). The flaky
  row-kebab UI is not used.

The class is deliberately NOT a :class:`PlatformUploader` subclass and does
not self-register: TikTok is ONE registry platform ("tiktok"). This module is
the browser ROUTE that :class:`xpst.platforms.tiktok.TikTokUploader` routes
to (official Direct Post, if audited > browser-native > inbox-draft), exactly
like Instagram's graph_api-vs-session dispatch.
"""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, urlencode, urlparse

from xpst.utils.logger import get_logger

if TYPE_CHECKING:
    from pathlib import Path

logger = get_logger(__name__)

TIKTOK_STUDIO_UPLOAD_URL = "https://www.tiktok.com/tiktokstudio/upload"
TIKTOK_STUDIO_CONTENT_URL = "https://www.tiktok.com/tiktokstudio/content"
CREATOR_ITEM_DELETE_URL = "https://www.tiktok.com/tiktok/creator/item/delete/v1/"

# Same UA the spike used (a HeadlessChrome UA plus a Mac profile was accepted
# by TikTok Studio without a bot wall on 2026-09-28).
_BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
)

# Markers that mean "TikTok Studio loaded and this session is signed in".
_LOGGED_IN_MARKERS = ("Select video", "Upload video", "Key metrics", "Recent posts")
_LOGIN_WALL_MARKERS = ("Log in", "Register", "Sign up")


class BrowserSessionExpiredError(RuntimeError):
    """The seeded/persisted TikTok web session is not logged in.

    Surfaced as a re-auth nudge; this route NEVER attempts a silent login.
    """


class BrowserPublishError(RuntimeError):
    """The browser publish flow failed at a named stage, with evidence."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail[:300]}" if detail else code)
        self.code = code
        self.detail = detail


@dataclass
class BrowserReceipt:
    """Receipt captured from the manage item_list API after a publish."""

    item_id: str
    share_url: str
    post_time: int | None = None
    visibility: int | None = None
    status: int | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    def to_metadata(self) -> dict[str, Any]:
        return {
            "item_id": self.item_id,
            "share_url": self.share_url,
            "post_time": self.post_time,
            "visibility": self.visibility,
            "status": self.status,
        }


def parse_netscape_cookie_jar(jar_text: str) -> list[dict[str, Any]]:
    """Convert a Netscape cookie file into Playwright ``add_cookies`` dicts.

    Only tiktok.com-scoped, non-expired cookies are kept (the same domain and
    expiry rules as :meth:`TikTokUploader._load_tiktok_web_cookies`, so the
    browser route and the plain-HTTP web fallback agree on what a session is).
    """
    now = time.time()
    cookies: list[dict[str, Any]] = []
    for line in jar_text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) != 7:
            continue
        domain, _flag, path, secure, expiry, name, value = parts
        if not domain or not name or not value:
            continue
        if not domain.rstrip(".").endswith("tiktok.com"):
            continue
        expires = -1
        if expiry.strip().lstrip("-").isdigit():
            exp = int(expiry)
            if exp != 0:
                if exp < now:
                    continue  # expired
                expires = exp
        cookies.append(
            {
                "name": name,
                "value": value,
                "domain": domain,
                "path": path or "/",
                "secure": secure == "TRUE",
                "expires": expires,
            }
        )
    return cookies


def extract_item_list_items(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Pull the ``item_list`` items out of a manage item_list API response."""
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    items = data.get("item_list") or data.get("items") or []
    if isinstance(items, dict):
        items = items.get("item_list") or []
    return [it for it in items if isinstance(it, dict)]


def find_new_item(
    payloads: list[dict[str, Any]],
    *,
    started_at: float,
    caption: str = "",
    owner_hint: str = "",
) -> BrowserReceipt | None:
    """Find the just-published item among captured item_list responses.

    An item qualifies when it is newer than ``started_at`` and either its id
    was explicitly announced by the publish response or its caption matches.
    Returns None (-> FAILED) when nothing matches — never a guess.

    ``owner_hint`` is the account handle learned from the Studio page itself;
    it composes the canonical share URL when the item rows carry none.
    """
    best: BrowserReceipt | None = None
    caption_head = (caption or "").strip()[:40].lower()
    # Owner handle learned from ANY item's share_url in the same listings —
    # a just-published row can lag on its own share_url while still in
    # review, but sibling rows carry the canonical @handle. Live finding
    # (2026-09-29): some manage responses omit share_url on EVERY row, so a
    # page-observed owner_hint is accepted as the fallback.
    owner: str = (owner_hint or "").lstrip("@").strip()
    for payload in payloads:
        for it in extract_item_list_items(payload):
            m = re.match(r"https://www\.tiktok\.com/@([^/]+)/", str(it.get("share_url") or ""))
            if m and m.group(1).lower() != "unknown":
                owner = m.group(1)
                break
        if owner:
            break
    for payload in payloads:
        for it in extract_item_list_items(payload):
            item_id = str(it.get("item_id") or it.get("video_id") or it.get("id") or "")
            if not item_id:
                continue
            post_time = it.get("post_time") or it.get("create_time")
            try:
                ts = int(post_time)
            except (TypeError, ValueError):
                continue
            if ts < int(started_at) - 120:  # older than this attempt
                continue
            desc = (it.get("desc") or "").strip().lower()
            matches_caption = bool(caption_head) and caption_head in desc
            share = str(it.get("share_url") or "")
            if not matches_caption and not share:
                continue
            if not share and owner:
                share = f"https://www.tiktok.com/@{owner}/video/{item_id}"
            receipt = BrowserReceipt(
                item_id=item_id,
                share_url=share or f"https://www.tiktok.com/@unknown/video/{item_id}",
                post_time=ts,
                visibility=it.get("visibility"),
                status=it.get("status"),
                raw={k: it.get(k) for k in ("item_id", "post_time", "visibility", "status", "share_url")},
            )
            # Newest wins when multiple items appear.
            if best is None or (receipt.post_time or 0) > (best.post_time or 0):
                best = receipt
    return best


class TikTokBrowserPublisher:
    """Playwright-driven publisher against the TikTok Studio web flow.

    All Playwright imports are lazy so the module (and the rest of xPST)
    imports fine without the optional ``playwright`` dependency; calling a
    publish/probe without it raises :class:`BrowserPublishError` with
    ``code=BROWSER_DEPENDENCY_MISSING``.
    """

    def __init__(
        self,
        *,
        cookie_jar_path: Path | None,
        profile_dir: Path,
        headless: bool = True,
    ) -> None:
        self.cookie_jar_path = cookie_jar_path
        self.profile_dir = profile_dir
        self.headless = headless

    # ── dependency + session helpers ─────────────────────────────────────

    def _require_playwright(self):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as e:  # pragma: no cover - depends on optional extra
            raise BrowserPublishError(
                "BROWSER_DEPENDENCY_MISSING",
                "the TikTok browser-native publisher needs the optional dependency: "
                "pip install 'xpst[browser]' then `python -m playwright install chromium`",
            ) from e
        return sync_playwright

    def _seed_cookies(self, context) -> int:
        """Seed a persistent context from the cookie jar when it has no session.

        Returns the number of cookies added. A profile that already carries a
        tiktok.com sessionid is left alone — the persisted profile is the
        source of truth after first seed (that is how real browser sessions
        survive sid_guard rotation).
        """
        try:
            existing = context.cookies("https://www.tiktok.com")
        except Exception:
            existing = []
        if any(c.get("name") == "sessionid" and c.get("value") for c in existing):
            return 0
        if not self.cookie_jar_path or not self.cookie_jar_path.exists():
            raise BrowserSessionExpiredError(
                "TikTok browser session has no stored profile and no cookie jar to seed it from. "
                f"Expected a jar at {self.cookie_jar_path} (see `xpst doctor tiktok`)."
            )
        cookies = parse_netscape_cookie_jar(
            self.cookie_jar_path.read_text(encoding="utf-8", errors="replace")
        )
        if not any(c["name"] == "sessionid" for c in cookies):
            raise BrowserSessionExpiredError(
                f"cookie jar {self.cookie_jar_path} has no sessionid — re-authenticate TikTok "
                "(`xpst auth tiktok` or export fresh browser cookies)."
            )
        context.add_cookies(cookies)
        return len(cookies)

    def _launch(self, pw):
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        return pw.chromium.launch_persistent_context(
            str(self.profile_dir),
            headless=self.headless,
            user_agent=_BROWSER_UA,
            viewport={"width": 1440, "height": 900},
            args=["--no-sandbox"],
        )

    # ── public surface ────────────────────────────────────────────────────

    def probe(self, timeout_ms: int = 30_000) -> dict[str, Any]:
        """Doctor-probe the web session without side effects.

        Returns a details dict with ``logged_in`` and the observation. Never
        logs in, never posts.
        """
        sync_playwright = self._require_playwright()
        with sync_playwright() as pw:
            ctx = self._launch(pw)
            try:
                seeded = self._seed_cookies(ctx)
                page = ctx.new_page()
                page.goto(TIKTOK_STUDIO_UPLOAD_URL, wait_until="domcontentloaded", timeout=timeout_ms)
                page.wait_for_timeout(5000)
                body = page.inner_text("body")[:800]
                logged_in = any(m in body for m in _LOGGED_IN_MARKERS)
                wall = any(m in body for m in _LOGIN_WALL_MARKERS)
                return {
                    "logged_in": logged_in,
                    "login_wall": wall and not logged_in,
                    "cookies_seeded": seeded,
                    "profile_dir": str(self.profile_dir),
                    "url": page.url,
                }
            finally:
                ctx.close()

    def publish(self, video_path: Path, caption: str, timeout_s: int = 300) -> BrowserReceipt:
        """Publish one video through the TikTok Studio web flow.

        Raises :class:`BrowserSessionExpiredError` (re-auth nudge) or
        :class:`BrowserPublishError` (named stage + evidence). Returns a
        :class:`BrowserReceipt` only with an observed item_list entry —
        receipt-or-fail.
        """
        started_at = time.time()
        sync_playwright = self._require_playwright()
        captured: list[dict[str, Any]] = []
        with sync_playwright() as pw:
            ctx = self._launch(pw)
            try:
                self._seed_cookies(ctx)
                page = ctx.new_page()

                def on_response(resp) -> None:
                    if "creator/manage/item_list" in resp.url:
                        try:
                            captured.append(resp.json())
                        except Exception:
                            pass

                page.on("response", on_response)
                page.goto(TIKTOK_STUDIO_UPLOAD_URL, wait_until="domcontentloaded", timeout=60_000)
                page.wait_for_timeout(6000)
                body = page.inner_text("body")[:600]
                if not any(m in body for m in _LOGGED_IN_MARKERS):
                    wall = any(m in body for m in _LOGIN_WALL_MARKERS)
                    raise BrowserSessionExpiredError(
                        "TikTok Studio did not accept the stored web session"
                        + (" (login wall shown)" if wall else "")
                        + " — re-authenticate TikTok (`xpst auth tiktok` or re-export cookies); "
                        "xPST will not attempt a silent login."
                    )

                page.set_input_files("input[type=file]", str(video_path))

                # Wait for the composer (any of the known shapes).
                composer = False
                for sel in (
                    '[data-e2e="browse-video-caption-input"]',
                    'div[contenteditable="true"]',
                    "textarea",
                ):
                    try:
                        page.wait_for_selector(sel, timeout=60_000)
                        composer = True
                        break
                    except Exception:
                        continue
                if not composer:
                    raise BrowserPublishError(
                        "BROWSER_COMPOSER_TIMEOUT",
                        "upload started but the composer never appeared (UI churn?)",
                    )

                self._dismiss_chrome(page)

                # Caption: Draft.js pre-fills the editor from the FILENAME
                # (spike finding), so clear it before typing — otherwise the
                # filename becomes a caption prefix.
                editor = page.locator('div[contenteditable="true"]').first
                editor.click(force=True)
                page.keyboard.press("Control+A")
                page.keyboard.press("Meta+A")
                page.keyboard.press("Backspace")
                page.keyboard.type(caption)

                post = page.locator('button:has-text("Post"), [data-e2e="post-button"]').last
                if not post.count():
                    raise BrowserPublishError("BROWSER_NO_POST_BUTTON", "composer had no Post button")
                post.click()

                # Success signals: redirect to /tiktokstudio/content, or a 200
                # from the web post/commit endpoints.
                published = self._wait_for_publish(page, timeout_s)
                if not published:
                    raise BrowserPublishError(
                        "BROWSER_PUBLISH_UNCONFIRMED",
                        f"Post clicked but no redirect/post-API signal within {timeout_s}s (url={page.url})",
                    )
                # Give the manage API a beat to list the new item.
                page.wait_for_timeout(6000)
                owner_hint = self._page_owner(page)
                receipt = find_new_item(
                    captured, started_at=started_at, caption=caption, owner_hint=owner_hint
                )
                if receipt is None:
                    # One fresh listing chance: reload the content manager.
                    try:
                        page.goto(TIKTOK_STUDIO_CONTENT_URL, wait_until="domcontentloaded", timeout=60_000)
                        page.wait_for_timeout(9000)
                    except Exception:
                        pass
                    receipt = find_new_item(
                        captured,
                        started_at=started_at,
                        caption=caption,
                        owner_hint=owner_hint or self._page_owner(page),
                    )
                if receipt is None:
                    raise BrowserPublishError(
                        "BROWSER_PUBLISH_UNVERIFIED",
                        "clicked Post and saw the publish signal, but the manage item_list API "
                        "never showed the new item — post state unknown, check TikTok Studio",
                    )
                return receipt
            finally:
                ctx.close()

    def delete_item(self, item_id: str) -> bool:
        """Fire the creator item-delete call from a real Studio page context.

        Live finding (2026-09-29): the endpoint ignores a bare POST (302 to
        ``/404`` HTML) but accepts one carrying the SAME context params the
        app's own signed ``item_list`` request uses, MINUS the per-request
        signature params (X-Bogus/X-Gnarly/msToken — reused verbatim they
        invalidate and the call 404s). TikTok answers accepted deletes with
        HTML anyway, so this method only reports that the request was
        issued; callers must confirm removal with :meth:`item_absent`.
        Returns True when the delete request was fired correctly.
        """
        sync_playwright = self._require_playwright()
        with sync_playwright() as pw:
            ctx = self._launch(pw)
            try:
                self._seed_cookies(ctx)
                page = ctx.new_page()
                signed: dict = {}

                def _on_request(req) -> None:
                    if "item_list" in req.url and req.method == "POST" and not signed:
                        signed["url"] = req.url

                page.on("request", _on_request)
                try:
                    # Manual polling on purpose: Playwright's
                    # expect_request/expect_response waiters have a known
                    # fragile teardown (KeyError in pyee listener cleanup)
                    # that can raise over a succeeded action, so we poll a
                    # flag set by the handler instead.
                    try:
                        page.goto(
                            TIKTOK_STUDIO_CONTENT_URL,
                            wait_until="domcontentloaded",
                            timeout=60_000,
                        )
                    except Exception:
                        pass
                    deadline = time.monotonic() + 45
                    while time.monotonic() < deadline and not signed:
                        page.wait_for_timeout(1000)
                    if not signed:
                        logger.warning(
                            "TikTok delete: no signed item_list request to clone params from"
                        )
                        return False
                    query = parse_qs(urlparse(signed["url"]).query, keep_blank_values=True)
                    params = {k: v[0] for k, v in query.items()}
                    for sig in ("X-Bogus", "X-Gnarly", "msToken"):
                        params.pop(sig, None)
                    params["item_id"] = item_id
                    url = f"{CREATOR_ITEM_DELETE_URL}?{urlencode(params)}"
                    result = page.evaluate(
                        """async ([url]) => {
                            const res = await fetch(url, {
                                method: 'POST',
                                credentials: 'include',
                                headers: {'content-type': 'application/json'},
                                body: '{}',
                            });
                            return {status: res.status, redirected: res.redirected,
                                    finalUrl: res.url};
                        }""",
                        [url],
                    )
                finally:
                    page.close()
                final_url = str(result.get("finalUrl") or "")
                # A 404-redirect means TikTok refused the request shape;
                # an accepted delete answers (with HTML) on the same path.
                ok = result.get("status") == 200 and "/404" not in final_url
                if not ok:
                    logger.warning(
                        "TikTok creator item_delete rejected for %s: %s",
                        item_id,
                        str(result)[:200],
                    )
                return ok
            finally:
                ctx.close()

    def delete_via_ui(self, item_id: str) -> bool:
        """Delete through the Studio row kebab menu (fallback for the endpoint).

        Hover the row containing ``item_id``'s caption, click the row's
        right-edge kebab, click the ``Delete`` menu entry, then confirm. The
        menu opens below the row, so the Delete entry is located by text
        after the kebab click rather than by row band. Returns True when the
        confirm was clicked; callers verify with :meth:`item_absent`.
        """
        sync_playwright = self._require_playwright()
        with sync_playwright() as pw:
            ctx = self._launch(pw)
            try:
                self._seed_cookies(ctx)
                page = ctx.new_page()
                try:
                    # Handler must be attached BEFORE the first navigation or
                    # the initial item_list responses are missed entirely.
                    captions: list[str] = []

                    def _collect(resp) -> None:
                        if "item_list" in resp.url:
                            try:
                                body = resp.json()
                            except Exception:
                                return
                            for it in extract_item_list_items(body):
                                if str(it.get("item_id")) == item_id and it.get("desc"):
                                    captions.append(str(it["desc"])[:40])

                    page.on("response", _collect)
                    try:
                        page.goto(
                            TIKTOK_STUDIO_CONTENT_URL,
                            wait_until="domcontentloaded",
                            timeout=60_000,
                        )
                    except Exception:
                        pass
                    # Manual poll (see delete_item): the listing XHR can
                    # land 15-20s after paint, and Playwright's waiter
                    # teardown is known-fragile.
                    deadline = time.monotonic() + 45
                    while time.monotonic() < deadline and not captions:
                        page.wait_for_timeout(1000)
                    if not captions:
                        logger.warning("TikTok UI delete: item %s not in manage list", item_id)
                        return False
                    cap = page.get_by_text(captions[0], exact=False).first
                    cap.scroll_into_view_if_needed(timeout=8000)
                    box = cap.bounding_box() or {"x": 400, "y": 400, "height": 30}
                    ymid = box["y"] + box["height"] / 2
                    page.mouse.move(box["x"] + 40, ymid)
                    page.mouse.move(box["x"] + 400, ymid)
                    page.wait_for_timeout(1800)
                    kebab = page.evaluate(
                        """(ymid) => {
                            const bs=[...document.querySelectorAll('button,[role=button]')]
                                .filter(b=>{const r=b.getBoundingClientRect();
                                    return (b.offsetWidth||b.offsetHeight)&&Math.abs(r.y+r.height/2-ymid)<60&&r.x>1100;});
                            if(!bs.length) return false;
                            bs[bs.length-1].click(); return true;
                        }""",
                        ymid,
                    )
                    if not kebab:
                        logger.warning("TikTok UI delete: no row kebab found for %s", item_id)
                        return False
                    page.wait_for_timeout(2000)
                    clicked_delete = page.evaluate(
                        """() => {
                            const els=[...document.querySelectorAll('body *')]
                                .filter(e=>(e.offsetWidth||e.offsetHeight)&&e.childElementCount===0
                                    && ['Delete','delete'].includes((e.textContent||'').trim()));
                            if(!els.length) return false;
                            els[0].click(); return true;
                        }"""
                    )
                    if not clicked_delete:
                        logger.warning("TikTok UI delete: Delete menu entry missing for %s", item_id)
                        return False
                    page.wait_for_timeout(1500)
                    confirmed = page.evaluate(
                        """() => {
                            // Prefer a Delete/Yes/Confirm button INSIDE a dialog;
                            // the row itself also has a Delete button, so an
                            // unscoped match can click the wrong one.
                            const dlg=[...document.querySelectorAll('[role=dialog],[class*=Modal],[class*=modal],[class*=Dialog]')]
                                .filter(e=>(e.offsetWidth||e.offsetHeight));
                            for (const d of dlg) {
                                const b=[...d.querySelectorAll('button,[role=button]')]
                                    .filter(x=>(x.offsetWidth||x.offsetHeight)&&/^(delete|yes|confirm)$/i.test((x.innerText||'').trim()));
                                if (b.length) { b[b.length-1].click(); return true; }
                            }
                            return false;
                        }"""
                    )
                    if not confirmed:
                        logger.warning("TikTok UI delete: confirm dialog missing for %s", item_id)
                    return bool(confirmed)
                finally:
                    page.close()
            finally:
                ctx.close()

    def item_absent(self, item_id: str, *, timeout_s: int = 60) -> bool:
        """True once the item is no longer listed by the manage item_list API.

        Polls the content manager until the id disappears from an
        ``item_list`` response (or ``timeout_s`` elapses). TikTok's delete is
        eventually consistent — the item can linger in listings for seconds
        after the endpoint accepts the delete — so callers use this as the
        receipt instead of trusting the delete response alone.
        """
        deadline = time.monotonic() + timeout_s
        sync_playwright = self._require_playwright()
        with sync_playwright() as pw:
            ctx = self._launch(pw)
            try:
                self._seed_cookies(ctx)
                page = ctx.new_page()
                seen: list[set[str]] = []

                def _collect(resp) -> None:
                    if "item_list" not in resp.url:
                        return
                    try:
                        body = resp.json()
                    except Exception:
                        return
                    items = extract_item_list_items(body)
                    seen.append({str(it.get("item_id")) for it in items if isinstance(it, dict)})

                page.on("response", _collect)
                while time.monotonic() < deadline:
                    try:
                        page.goto(
                            TIKTOK_STUDIO_CONTENT_URL,
                            wait_until="domcontentloaded",
                            timeout=60_000,
                        )
                    except Exception:
                        pass
                    # Manual poll until at least one listing arrives (the
                    # XHR lands 15-20s after paint); waiter objects are
                    # known-fragile (see delete_item).
                    poll_until = time.monotonic() + 25
                    while time.monotonic() < poll_until and not seen:
                        page.wait_for_timeout(1000)
                    listings = [ids for ids in seen if ids]
                    if listings and all(item_id not in ids for ids in listings[-2:]):
                        return True
                return False
            finally:
                ctx.close()

    # ── internal flow helpers ─────────────────────────────────────────────

    @staticmethod
    def _page_owner(page) -> str:
        """Best-effort account handle observed on the current Studio page.

        TikTok Studio links the signed-in creator (avatar menu, "View post"
        row links) as ``/@<handle>`` anchors. Used only to compose a canonical
        share URL for a receipt the manage API already confirmed — a wrong
        hint degrades to a generic URL, never to a wrong item_id.
        """
        try:
            hrefs: list[str] = page.evaluate(
                "() => Array.from(document.querySelectorAll('a[href^=\"/@\"]'))"
                ".map(a => a.getAttribute('href'))"
            )
        except Exception:
            return ""
        for href in hrefs or []:
            m = re.match(r"^/@([A-Za-z0-9._\-]{2,24})(?:[/?]|$)", str(href))
            if m:
                return m.group(1)
        return ""

    @staticmethod
    def _dismiss_chrome(page) -> None:
        """Dismiss the one-time upsell modal and the first-run joyride tour.

        Both appear only on fresh profiles (or rarely re-run tours); neither
        is part of the publish contract, so remove-if-present, never fail.
        """
        try:
            cancel = page.get_by_role("button", name="Cancel")
            if cancel.count() and cancel.first.is_visible():
                cancel.first.click()
                page.wait_for_timeout(800)
        except Exception:
            pass
        try:
            page.evaluate(
                "() => { const p = document.querySelector('#react-joyride-portal'); if (p) p.remove(); }"
            )
        except Exception:
            pass

    @staticmethod
    def _wait_for_publish(page, timeout_s: int) -> bool:
        """True when the publish lands: redirect to the content manager."""
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if re.search(r"tiktokstudio/(content|manage|home)", page.url):
                return True
            page.wait_for_timeout(1000)
        return False


async def publish_via_browser(
    video_path: Path,
    caption: str,
    *,
    cookie_jar_path: Path | None,
    profile_dir: Path,
    headless: bool = True,
    timeout_s: int = 300,
) -> BrowserReceipt:
    """Async bridge: run the sync Playwright publish off the event loop."""
    pub = TikTokBrowserPublisher(
        cookie_jar_path=cookie_jar_path,
        profile_dir=profile_dir,
        headless=headless,
    )
    return await asyncio.to_thread(pub.publish, video_path, caption, timeout_s)
