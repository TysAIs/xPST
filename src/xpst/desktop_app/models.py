"""Post and Notification list models for xPST desktop app.

QAbstractListModel exposing posted videos and notification history
from StateManager as models consumable by QML ListView/Repeater.
"""

import json
from datetime import datetime
from typing import Any

from PySide6.QtCore import (
    QAbstractListModel,
    QModelIndex,
    Qt,
    QByteArray,
)

# Optional: state manager for real data
try:
    from xpst.state import StateManager
except ImportError:
    StateManager = None  # type: ignore[assignment,misc]


class PostListModel(QAbstractListModel):
    """ListModel exposing posted videos to QML.

    Roles map to QML role names for delegate bindings:
        title, caption, platform, status, timestamp, thumbnail, postId
    """

    # Custom roles starting at Qt.UserRole + 1
    TitleRole = Qt.UserRole + 1
    CaptionRole = Qt.UserRole + 2
    PlatformRole = Qt.UserRole + 3
    StatusRole = Qt.UserRole + 4
    TimestampRole = Qt.UserRole + 5
    ThumbnailRole = Qt.UserRole + 6
    PostIdRole = Qt.UserRole + 7

    _ROLE_NAMES: dict[int, QByteArray] = {}

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._posts: list[dict[str, Any]] = []
        self._ROLE_NAMES = {
            self.TitleRole: QByteArray(b"title"),
            self.CaptionRole: QByteArray(b"caption"),
            self.PlatformRole: QByteArray(b"platform"),
            self.StatusRole: QByteArray(b"status"),
            self.TimestampRole: QByteArray(b"timestamp"),
            self.ThumbnailRole: QByteArray(b"thumbnail"),
            self.PostIdRole: QByteArray(b"postId"),
        }

    # ── QAbstractListModel interface ─────────────────────────────────

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: B008
        return len(self._posts)

    def data(self, index: QModelIndex, role: int = Qt.DisplayRole) -> Any:
        if not index.isValid() or index.row() < 0 or index.row() >= len(self._posts):
            return None

        post = self._posts[index.row()]

        if role == self.TitleRole:
            return post.get("title", "")
        if role == self.CaptionRole:
            return post.get("caption", "")
        if role == self.PlatformRole:
            return post.get("platform", "")
        if role == self.StatusRole:
            return post.get("status", "posted")
        if role == self.TimestampRole:
            return post.get("timestamp", "")
        if role == self.ThumbnailRole:
            return post.get("thumbnail", "")
        if role == self.PostIdRole:
            return post.get("postId", "")

        return None

    def roleNames(self) -> dict[int, QByteArray]:
        return self._ROLE_NAMES

    # ── Public API ───────────────────────────────────────────────────

    def load_from_state(self, state_dir: str = "~/.xpst") -> None:
        """Reload the model from StateManager state.json.

        Each posted video becomes one row, with one entry per platform
        it was posted to.  Gracefully handles missing/corrupt files.
        """
        if StateManager is None:
            return

        try:
            sm = StateManager(state_dir=state_dir)
            posted = sm._state.get("posted_videos", {})
        except Exception:
            posted = {}

        new_posts: list[dict[str, Any]] = []
        for video_id, video_data in posted.items():
            caption = video_data.get("caption") or ""
            tiktok_url = video_data.get("tiktok_url") or ""
            downloaded_at = video_data.get("downloaded_at") or ""

            # Create a row per platform this video was posted to
            posted_to = video_data.get("posted_to", {})
            if not posted_to:
                # Video tracked but not yet posted anywhere
                new_posts.append({
                    "title": video_id,
                    "caption": caption[:120],
                    "platform": "tiktok",
                    "status": "source",
                    "timestamp": downloaded_at,
                    "thumbnail": "",
                    "postId": video_id,
                })
            else:
                for platform, pinfo in posted_to.items():
                    ts = pinfo.get("timestamp") or downloaded_at
                    url = pinfo.get("url") or ""
                    pid = pinfo.get("id") or video_id
                    new_posts.append({
                        "title": video_id,
                        "caption": caption[:120],
                        "platform": platform,
                        "status": "posted",
                        "timestamp": ts,
                        "thumbnail": url,
                        "postId": pid,
                    })

        # Sort newest first
        new_posts.sort(key=lambda p: p.get("timestamp", ""), reverse=True)

        self.beginResetModel()
        self._posts = new_posts
        self.endResetModel()

    def update_data(self, posts: list[dict[str, Any]]) -> None:
        """Replace the model data with an external list of post dicts."""
        self.beginResetModel()
        self._posts = posts
        self.endResetModel()

    def get_posts_json(self) -> str:
        """Return all posts as a JSON string."""
        return json.dumps(self._posts, default=str)

    def get_post_captions(self, post_id: str) -> dict[str, str]:
        """Return per-platform captions for a given postId."""
        result: dict[str, str] = {}
        for p in self._posts:
            if p.get("postId") == post_id:
                result[p.get("platform", "")] = p.get("caption", "")
        return result

    def update_caption(self, post_id: str, platform: str, new_caption: str) -> None:
        """Update caption for a specific post/platform entry in the model."""
        for i, p in enumerate(self._posts):
            if p.get("postId") == post_id and p.get("platform") == platform:
                p["caption"] = new_caption
                idx = self.index(i)
                self.dataChanged.emit(idx, idx, [self.CaptionRole])
                break


class NotificationListModel(QAbstractListModel):
    """In-memory notification history for toast notifications.

    Roles: message, isError, timestamp
    """

    MessageRole = Qt.UserRole + 1
    IsErrorRole = Qt.UserRole + 2
    TimestampRole = Qt.UserRole + 3

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._notifications: list[dict[str, Any]] = []

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: B008
        return len(self._notifications)

    def data(self, index: QModelIndex, role: int = Qt.DisplayRole) -> Any:
        if not index.isValid() or index.row() < 0 or index.row() >= len(self._notifications):
            return None
        n = self._notifications[index.row()]
        if role == self.MessageRole:
            return n.get("message", "")
        if role == self.IsErrorRole:
            return n.get("isError", False)
        if role == self.TimestampRole:
            return n.get("timestamp", "")
        return None

    def roleNames(self) -> dict[int, QByteArray]:
        return {
            self.MessageRole: QByteArray(b"message"),
            self.IsErrorRole: QByteArray(b"isError"),
            self.TimestampRole: QByteArray(b"timestamp"),
        }

    def add_notification(self, message: str, is_error: bool = False) -> None:
        """Add a notification to the history and notify views."""
        self.beginInsertRows(QModelIndex(), 0, 0)
        self._notifications.insert(0, {
            "message": message,
            "isError": is_error,
            "timestamp": datetime.now().isoformat(),
        })
        self.endInsertRows()

    def clear(self) -> None:
        """Clear all notifications."""
        self.beginResetModel()
        self._notifications.clear()
        self.endResetModel()

    def unread_count(self) -> int:
        """Return total notification count (simple proxy for unread)."""
        return len(self._notifications)
