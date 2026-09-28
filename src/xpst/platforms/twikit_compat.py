"""Compatibility patches for twikit against X's Sep-2026 web surface.

twikit 2.3.3 is broken against X's March-2026+ web changes (upstream issue
#408 on d60/twikit): the ``ondemand.s`` bundle manifest moved from a quoted
``"ondemand.s":"<hash>"`` pair to a chunk-id map
(``,59924:"ondemand.s"`` + a separate ``{59924:"<hash>"}`` map), the 1.1 API
was deprecated, and user payloads dropped legacy keys.

These patches are applied from ONE home (this module) and imported by every
surface that constructs a twikit client: the uploader
(:mod:`xpst.platforms.x`) AND the analytics collector
(:mod:`xpst.analytics`).  Before QA-2026-09-28 the patches lived inside
``platforms/x.py`` only, so the analytics path — which never imported the
uploader — ran the UNPATCHED library and failed with
``'ClientTransaction' object has no attribute 'key'`` on every post while
posting itself worked (QA defect D1, X half of the root cause).

The patches are applied at import time of this module, are idempotent, and
never raise: an unpatched twikit degrades to its own error paths, which the
caller now surfaces instead of silently reporting zeros.

Regression coverage: ``tests/test_twikit_compat_manifest.py`` replays the
captured Sep-2026 home-page fixture against the patched index extraction.
"""

from __future__ import annotations

import importlib
import logging
import re
import urllib.parse

logger = logging.getLogger(__name__)

__all__ = ["apply_twikit_patches"]

_APPLIED = False


def apply_twikit_patches() -> bool:
    """Patch twikit for X's current web surface. Idempotent; never raises.

    Returns True when the module was imported and patching was attempted at
    least once (the individual patches still log-and-continue on failure).
    """
    global _APPLIED
    if _APPLIED:
        return True
    try:
        importlib.import_module("twikit")
    except Exception as exc:  # twikit absent — nothing to patch
        logger.debug("twikit not importable; compat patches skipped: %s", exc)
        return False
    _APPLIED = True

    # Patch 1: ondemand.s manifest extraction (upstream issue #408).
    try:
        _tx_mod = importlib.import_module("twikit.x_client_transaction.transaction")

        # X's Sep-2026 home page lists chunk names positionally:
        #   ...,59924:"ondemand.s",...
        # and carries a SEPARATE id→hash map with 59924:"<hex hash>".
        _tx_mod.ON_DEMAND_FILE_REGEX = re.compile(
            r""",(\d+):["']ondemand\.s["']""", flags=(re.VERBOSE | re.MULTILINE)
        )
        _tx_mod.ON_DEMAND_HASH_PATTERN = r',{}:"([0-9a-f]+)"'

        async def _patched_get_indices(self, home_page_response, session, headers):  # type: ignore[no-untyped-def]
            key_byte_indices: list[str] = []
            response = self.validate_response(home_page_response) or self.home_page_response
            match = _tx_mod.ON_DEMAND_FILE_REGEX.search(str(response))
            if not match:
                raise Exception("Couldn't find ondemand.s index")
            on_demand_file_index = match.group(1)
            regex = re.compile(_tx_mod.ON_DEMAND_HASH_PATTERN.format(on_demand_file_index))
            hash_match = regex.search(str(response))
            if not hash_match:
                raise Exception("Couldn't find ondemand.s hash")
            filename = hash_match.group(1)
            on_demand_file_url = (
                f"https://abs.twimg.com/responsive-web/client-web/ondemand.s.{filename}a.js"
            )
            on_demand_file_response = await session.request(
                method="GET", url=on_demand_file_url, headers=headers
            )
            key_byte_indices_match = _tx_mod.INDICES_REGEX.finditer(str(on_demand_file_response.text))
            for item in key_byte_indices_match:
                key_byte_indices.append(item.group(2))
            if not key_byte_indices:
                raise Exception("Couldn't get KEY_BYTE indices")
            ints = list(map(int, key_byte_indices))
            return ints[0], ints[1:]

        _tx_mod.ClientTransaction.get_indices = _patched_get_indices
    except Exception as e:
        logger.debug("twikit transaction patch failed: %s", e)

    # Patch 2: user_id() from the twid cookie (the 1.1 API is deprecated).
    try:
        from twikit.client.client import Client

        async def _patched_user_id(self):  # type: ignore[no-untyped-def]
            if self._user_id is not None:
                return self._user_id
            twid = self.get_cookies().get("twid", "")
            if twid:
                decoded = urllib.parse.unquote(twid)
                if "=" in decoded:
                    user_id = decoded.split("=")[-1]
                    self._user_id = user_id
                    return user_id
            # Fallback to original method (may 404 on deprecated 1.1 API)
            response, _ = await self.v11.settings()
            screen_name = response["screen_name"]
            self._user_id = (await self.get_user_by_screen_name(screen_name)).id
            return self._user_id

        Client.user_id = _patched_user_id
    except Exception as e:
        logger.debug("twikit user_id patch failed: %s", e)

    # Patch 3: User.__init__ tolerates legacy keys X stopped sending.
    try:
        from twikit.user import User

        _original_init = User.__init__

        def _safe_init(self, client, data):  # type: ignore[no-untyped-def]
            legacy = data.get("legacy", {})
            defaults = {
                "can_dm": False,
                "can_media_tag": False,
                "created_at": "",
                "default_profile": False,
                "default_profile_image": False,
                "description": "",
                "entities": {"description": {"urls": []}},
                "fast_followers_count": 0,
                "favourites_count": 0,
                "followers_count": 0,
                "friends_count": 0,
                "has_custom_timelines": False,
                "is_translator": False,
                "listed_count": 0,
                "location": "",
                "media_count": 0,
                "name": "",
                "normal_followers_count": 0,
                "pinned_tweet_ids_str": [],
                "possibly_sensitive": False,
                "profile_image_url_https": "",
                "screen_name": "",
                "statuses_count": 0,
                "translator_type": "",
                "verified": False,
                "want_retweets": False,
                "withheld_in_countries": [],
            }
            for key, default in defaults.items():
                if key not in legacy:
                    legacy[key] = default
            entities = legacy.get("entities", {})
            if "description" not in entities:
                entities["description"] = {"urls": []}
            elif "urls" not in entities.get("description", {}):
                entities["description"]["urls"] = []
            if "url" not in entities:
                entities["url"] = {"urls": []}
            elif "urls" not in entities.get("url", {}):
                entities["url"]["urls"] = []
            legacy["entities"] = entities
            data["legacy"] = legacy
            _original_init(self, client, data)

        User.__init__ = _safe_init
    except Exception as e:
        logger.debug("twikit User patch failed: %s", e)

    # Patch 4: get_tweet_by_id crashes on X's Sep-2026 cursor entries
    # (KeyError 'itemContent' — the trailing cursor entry no longer carries
    # content.itemContent.value). The cursor parsing dies AFTER the tweet
    # entry loop, so only reply-pagination was lost; this reimplementation is
    # the upstream body with cursor extraction made value-safe. Live-verified
    # 2026-09-28 in the sense that reads now fail as a clean
    # "TweetNotAvailable"-class error surfaced through the analytics
    # collection-failure path — NOT as a KeyError or a silent zero. Note a
    # second, deeper upstream break remains: X currently answers twikit 2.3.3's
    # TweetDetail query with an EMPTY tweet_results payload, so live X metrics
    # are honestly labeled degraded/failed until twikit or a feature-switch
    # fix lands; the transaction + cursor patches are prerequisites, not the
    # whole story.
    try:
        from twikit.client.client import Client as _TwikitClient
        from twikit.tweet import tweet_from_data as _tweet_from_data
        from twikit.utils import Result as _TwikitResult
        from twikit.utils import find_dict as _find_dict

        def _cursor_value(entry) -> str | None:  # noqa: ANN001
            try:
                return entry["content"]["itemContent"]["value"]
            except (KeyError, TypeError):
                try:
                    return entry["item"]["itemContent"]["value"]
                except (KeyError, TypeError):
                    return None

        async def _patched_get_tweet_by_id(self, tweet_id, cursor=None):  # type: ignore[no-untyped-def]
            from functools import partial

            response, _ = await self.gql.tweet_detail(tweet_id, cursor)
            if "errors" in response:
                from twikit.errors import TweetNotAvailable

                raise TweetNotAvailable(response["errors"][0]["message"])

            entries = _find_dict(response, "entries", find_one=True)[0]
            reply_to: list = []
            replies_list: list = []
            related_tweets: list = []
            tweet = None

            for entry in entries:
                if entry["entryId"].startswith("cursor"):
                    continue
                tweet_object = _tweet_from_data(self, entry)
                if tweet_object is None:
                    continue
                if entry["entryId"].startswith("tweetdetailrelatedtweets"):
                    related_tweets.append(tweet_object)
                    continue
                if entry["entryId"] == f"tweet-{tweet_id}":
                    tweet = tweet_object
                    continue
                if tweet is None:
                    reply_to.append(tweet_object)
                    continue
                replies: list = []
                sr_cursor = None
                show_replies = None
                for reply in entry["content"]["items"][1:]:
                    if "tweetcomposer" in reply["entryId"]:
                        continue
                    if "tweet" in reply.get("entryId"):
                        rpl = _tweet_from_data(self, reply)
                        if rpl is None:
                            continue
                        replies.append(rpl)
                    if "cursor" in reply.get("entryId"):
                        sr_cursor = _cursor_value(reply)
                        if sr_cursor is not None:
                            show_replies = partial(
                                self._show_more_replies, tweet_id, sr_cursor
                            )
                tweet_object.replies = _TwikitResult(replies, show_replies, sr_cursor)
                replies_list.append(tweet_object)
                display_type = _find_dict(entry, "tweetDisplayType", True)
                if display_type and display_type[0] == "SelfThread":
                    tweet.thread = [tweet_object, *replies]

            reply_next_cursor = None
            _fetch_more_replies = None
            if entries and entries[-1]["entryId"].startswith("cursor"):
                reply_next_cursor = _cursor_value(entries[-1])
                if reply_next_cursor is not None:
                    _fetch_more_replies = partial(
                        self._get_more_replies, tweet_id, reply_next_cursor
                    )

            if tweet is None:
                from twikit.errors import TweetNotAvailable

                raise TweetNotAvailable(f"Tweet {tweet_id} not found in detail response")
            tweet.replies = _TwikitResult(replies_list, _fetch_more_replies, reply_next_cursor)
            tweet.reply_to = reply_to
            tweet.related_tweets = related_tweets
            return tweet

        _TwikitClient.get_tweet_by_id = _patched_get_tweet_by_id
    except Exception as e:
        logger.debug("twikit get_tweet_by_id patch failed: %s", e)

    return True
