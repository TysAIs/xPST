# Safe operating envelope (publishing)

Derived from vendor policies and rate-limit documentation read 2026-11-17, plus
community-reported behaviour where vendors are silent. Every row states its
confidence. **No platform publishes a "posts per day before you get flagged"
number** — the maxima below are engineering policy with safety margin, not a
compliance certificate.

Confidence tags: **[V]** vendor-documented · **[C]** community/practitioner ·
**[U]** unknown or adaptive.

## The three rules that matter more than any per-platform number

1. **Never the same asset across two accounts on the same platform.** X prohibits
   "duplicative or substantially similar posts … over multiple accounts you
   operate" **[V]**; Meta treats repetitive content as a spam indicator **[V]**;
   TikTok treats unoriginal/reused content as ineligible for the For You feed
   **[V]**. Cross-platform (the same video to YouTube *and* TikTok) is fine.
   Same platform, two accounts, same video is not. The prohibition carries no
   time bound, so xPST's enforcement (`xpst-provider-x`, `DuplicatePolicy`)
   refuses without one: an asset published to one account stays refused for any
   other account until a human changes that policy.
2. **Jitter every interval by ±30–50 %, log-normal.** Metronomic cadence is the
   clearest automation signature **[C]**. Never schedule on the hour.
3. **One in-flight write per account per platform, and a hard stop on any
   challenge.** A challenge trips a breaker for that account, not for the
   request. Retrying into a lock is how a soft block becomes a suspension **[C]**.

## Per platform

| Platform | Min interval | Max posts/day (single account) | Documented ceiling | Same asset on 2 accounts | On 429 / challenge |
|---|---|---|---|---|---|
| YouTube | 30 min | 4–6 per channel | 100 uploads/day per project **[V]** | Low ban risk, high **monetisation** risk ("reused content") **[V]** | Quota 403: stop until midnight PT; never parallel-retry |
| X | 20–30 min | 8–12 | 2,400/day; API 100/15 min **[V]** | **Prohibited explicitly [V]** | Wait `x-rate-limit-reset`; lock → stop 24–72 h, human |
| Instagram (official) | 30–60 min | 6–10 | 100/day, 50 stated for carousels **[V, conflicting]** | High risk **[V]** | Stop 24 h; `ChallengeRequired` → human |
| Instagram (direct) | 45–90 min; 1/day first week | 3–6 | none published | Very high risk **[C]** | Stop 24–72 h; same device/session, no proxy change |
| Threads | 20–30 min | 8–12 | 250/day **[V]** | Moderate–high | Gate on `threads_publishing_limit` |
| Facebook Page | 30 min | 4–8 | none published (app-level only) **[V]** | Moderate | Stop 24 h; read `X-Business-Use-Case-Usage` |
| TikTok (official) | 2–4 h | 1–3 | ~15/day, shared across all clients **[V]** | High **[V]** | `spam_risk_too_many_posts` → stop 24 h; `spam_risk_user_banned` → indefinite + human |
| TikTok (direct) | 3–6 h | 1–2 | none published | Very high **[C]** | Stop at first captcha/slider; human completes it |
| Bluesky | 10 min | 10–20 | 1,666 creates/hour; 11,666/day **[V]** | Low–moderate | Honour 429; reuse one session (repeated `createSession` is the top 429 cause) |
| Mastodon | 10–15 min | 20–30 (instance-dependent) | 300 req/5 min; 30 media/30 min **[V]** | Low technically; instance rules vary | Honour headers; silence → contact admin, never evade |

## Behaviours that look automated even on the official API

- Metronomic cadence, identical captions, burst-then-silence, link-dominant
  feeds, unrequested reply/mention automation, a brand-new account posting at
  mature-account volume, 24/7 activity with no quiet window **[C]**.
- Engagement-only automation (auto-like, auto-follow) is prohibited on X and
  TikTok and is the fastest route to a ban **[V]**.

## Folklore we will not encode as fact

"More than N posts/day gets you shadowbanned" (no platform publishes N);
`navigator.webdriver` detection (plausible, unverifiable, irrelevant to official
APIs); "residential proxies are safe, datacentre is not" (instagrapi's own
guidance says consistency matters more than proxy class); "X shadowbans
third-party clients" (X's actual stated position is stronger: non-API automation
is grounds for suspension).
