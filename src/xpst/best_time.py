"""Best time to post analysis.

Analyzes metric snapshots to find the hour-of-week with highest average
engagement per platform. Uses the existing ``metric_snapshots`` table in
``analytics.db`` — no new tables or dependencies.

The analysis is purely historical: it looks at when posts were published
(derived from ``captured_at`` timestamps) and correlates with engagement
metrics to recommend optimal posting times.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

from xpst.utils.logger import get_logger

logger = get_logger(__name__)

# Day names for readable output
DAY_NAMES = [
    "Monday", "Tuesday", "Wednesday", "Thursday",
    "Friday", "Saturday", "Sunday",
]


class BestTimeAnalyzer:
    """Analyze historical metrics to recommend best posting times."""

    def __init__(self, db_path: Path | str | None = None) -> None:
        if db_path is None:
            db_path = Path("~/.xpst/analytics.db").expanduser()
        self.db_path = Path(db_path).expanduser()

    def _connect(self):
        import sqlite3
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    def analyze(self, platform: str | None = None) -> list[dict[str, Any]]:
        """Return best posting times per platform.

        Each result has:
        - platform: str
        - day_of_week: int (0=Monday, 6=Sunday)
        - day_name: str
        - hour: int (0-23, UTC)
        - avg_engagement_rate: float
        - post_count: int

        Results are sorted by avg_engagement_rate descending (best first).
        """
        query = """
            SELECT platform, captured_at, views, likes, comments, shares
            FROM metric_snapshots
        """
        params: tuple = ()
        if platform:
            query += " WHERE platform = ?"
            params = (platform,)

        try:
            with self._connect() as conn:
                rows = conn.execute(query, params).fetchall()
        except Exception as e:
            logger.debug(f"BestTimeAnalyzer query failed: {e}")
            return []

        if not rows:
            return []

        # Group snapshots by platform + hour-of-week
        # Use the FIRST snapshot per post (closest to publish time) for
        # engagement correlation.
        seen_posts: set[str] = set()
        buckets: dict[tuple[str, int, int], list[float]] = defaultdict(list)

        for row in rows:
            p = row["platform"]
            post_id = f"{p}:{row['captured_at']}"
            if post_id in seen_posts:
                continue
            seen_posts.add(post_id)

            views = row["views"] or 0
            if views == 0:
                continue

            engagement = (row["likes"] or 0) + (row["comments"] or 0) + (row["shares"] or 0)
            rate = (engagement / views) * 100

            try:
                dt = datetime.fromisoformat(row["captured_at"].replace("Z", "+00:00"))
            except (ValueError, AttributeError):
                continue

            day_of_week = dt.weekday()
            hour = dt.hour
            buckets[(p, day_of_week, hour)].append(rate)

        # Compute averages
        results = []
        for (p, day, hour), rates in buckets.items():
            avg_rate = sum(rates) / len(rates)
            results.append({
                "platform": p,
                "day_of_week": day,
                "day_name": DAY_NAMES[day],
                "hour": hour,
                "avg_engagement_rate": round(avg_rate, 1),
                "post_count": len(rates),
            })

        # Sort by avg_engagement_rate descending
        results.sort(key=lambda x: x["avg_engagement_rate"], reverse=True)
        return results

    def best_for_platform(self, platform: str) -> dict[str, Any] | None:
        """Return the single best time slot for a platform."""
        results = self.analyze(platform=platform)
        return results[0] if results else None

    def best_overall(self) -> dict[str, list[dict[str, Any]]]:
        """Return best times per platform as a dict.

        Returns: {platform: [top 3 time slots]}
        """
        results = self.analyze()
        by_platform: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for r in results:
            by_platform[r["platform"]].append(r)
        # Top 3 per platform
        return {p: slots[:3] for p, slots in by_platform.items()}
