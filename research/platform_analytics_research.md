# Platform Analytics API Research
## XPST Project — Free/Local Analytics Data Sources

---

## 1. YouTube — Best Free Analytics Source

### Two APIs Available (Both Free)

**YouTube Data API v3** — Public data, free with API key
- **Cost**: Free. Default quota: 10,000 units/day (can request increase to 100k)
- **Key Endpoints & Quota Costs**:
  - `videos.list` → 1 unit — get viewCount, likeCount, commentCount per video
  - `channels.list` → 1 unit — get subscriberCount, viewCount, videoCount
  - `commentThreads.list` → 1 unit — get comment data
  - `subscriptions.list` → 1 unit — get subscription data
  - `search.list` → 100 units (separate 100/day limit)
- **Available Public Metrics**: views, likes, comments, subscriber count, video count
- **Limitations**: No time-series data, no impressions/reach, no watch time breakdowns

**YouTube Analytics API** — Channel owner data, free with OAuth
- **Cost**: Free. Same Google Cloud project, no extra charges
- **Core Metrics Available** (all free):
  - `views` — total views
  - `likes`, `dislikes` — engagement ratings
  - `comments` — comment count
  - `shares` — share count
  - `subscribersGained`, `subscribersLost` — sub changes
  - `estimatedMinutesWatched` — total watch time
  - `averageViewDuration` — avg watch length (seconds)
  - `averageViewPercentage` — avg % watched
  - `engagedViews` — views past initial seconds
  - `redViews` — YouTube Premium views
- **Additional Metrics**: audience retention, card clicks/CTR, playlist metrics, livestream metrics
- **Dimensions**: video, channel, day, country, traffic source, device type, age, gender
- **Reports**: Real-time custom queries (Analytics API) or bulk daily exports (Reporting API)
- **Revenue Metrics** (if monetized): estimatedRevenue, estimatedAdRevenue, CPM

### Bottom Line
YouTube is the **richest free analytics source**. The Data API gives public counts (views/likes/comments), and the Analytics API gives deep channel-owner analytics (watch time, retention, demographics, traffic sources) — all free with a Google API key + OAuth.

---

## 2. Instagram — Via instagrapi (Unofficial/Private API)

### Library: instagrapi (⭐ 6.3k stars, MIT License)
- **Requires**: Business/Professional account (free to switch in IG settings)
- **Authentication**: Username + password with session persistence

### Insights Methods Available (All Free, via Private API):
- `insights_media(media_pk)` — single post insights
- `insights_media_feed_all(post_type, time_frame, data_ordering, count)` — all feed media with insights
- `insights_account()` — account-level insights (activity, audience, content tabs)

### Metrics Available Per Post:
- **Reach** (REACH_COUNT)
- **Impressions** (IMPRESSION_COUNT)
- **Likes** (LIKE_COUNT)
- **Comments** (COMMENT_COUNT)
- **Saves** (SAVE_COUNT)
- **Shares** (SHARE_COUNT)
- **Video Views** (VIDEO_VIEW_COUNT)
- **Bio Link Clicks** (BIO_LINK_CLICK)
- **Profile Views** (PROFILE_VIEW)
- **Follows** (FOLLOW)

### Filtering/Sorting Options:
- **Post Type**: ALL, CAROUSEL_V2, IMAGE, SHOPPING, VIDEO
- **Time Frame**: ONE_WEEK, ONE_MONTH, THREE_MONTHS, SIX_MONTHS, ONE_YEAR, TWO_YEARS
- **Sort By**: Any metric above (e.g., sort by REACH_COUNT)

### Limitations:
- Uses Instagram's private API (not officially supported)
- Account may get temporarily blocked if rate limits exceeded
- Business/Professional account required
- No historical data beyond what IG retains (typically 2 years)

### Bottom Line
instagrapi provides **comprehensive post insights** (reach, impressions, saves, shares, likes, comments) for free via the private API. Just needs a business account login.

---

## 3. X/Twitter — Via twikit (Unofficial Scraper)

### Library: twikit (⭐ 4.5k stars, MIT License)
- **Requires**: Twitter/X account (username + email + password)
- **Authentication**: Cookie-based, persists sessions

### Available Tweet Metrics (from Tweet object):
- `favorite_count` — likes
- `retweet_count` — retweets/ reposts
- `view_count` — tweet views/impressions
- `reply_count` — replies
- `bookmark_count` — bookmarks

### Available User Metrics:
- `followers_count`
- `following_count`
- `statuses_count` (total tweets)
- `favourites_count` (total likes given)

### Key Methods:
- `search_tweet(query, product)` — search tweets, get engagement metrics
- `get_user_tweets(user_id, type)` — get user's tweets with metrics
- `get_user_by_screen_name(username)` — get user stats
- `get_user_followers/get_user_following()` — follower lists
- `get_trends(category)` — trending topics

### What You CANNOT Get:
- **Impressions** (the Analytics dashboard shows this but it's NOT in the API/scraper data)
- **Engagement rate** (must calculate: (likes+retweets+replies)/views)
- **Demographics** (age, gender, location of viewers)
- **Link clicks** (only in Twitter Analytics dashboard, not scrapeable)
- **Detailed engagement breakdown** (only available in ads.twitter.com or analytics.twitter.com)

### Limitations:
- Uses Twitter's internal/undocumented API
- Account risk (could get suspended for scraping)
- Rate limits apply (documented in twikit's ratelimits.md)
- No access to Twitter Analytics dashboard data (impressions breakdown, link clicks, profile visits)

### Bottom Line
twikit gives **basic engagement metrics** (likes, retweets, views, replies, bookmarks) for free. But **detailed analytics** (impressions, link clicks, profile visits, demographics) are NOT accessible — those are only in Twitter's Analytics dashboard.

---

## 4. TikTok — Worst Case, Very Limited Free Access

### Official APIs (Effectively Not Available):
- **TikTok Research API**: Academic/researchers only. Requires institutional affiliation, project approval. 30-day rolling historical window. No commercial use. ~1000 calls/day.
- **TikTok Business API**: Ad performance data only, for advertisers. Not for general analytics.
- **Commercial Data Partnership**: Enterprise only, custom pricing ($$$$).

### Unofficial Options:
- **TikTok-Api** (davidteather, ⭐ 5.1k stars): Uses Playwright + ms_token cookie
  - Can get: user info (followerCount, heartCount/likes, videoCount), video stats (playCount, diggCount/likes, commentCount, shareCount)
  - **Limitations**: ms_token expires every few hours, bot detection breaks it frequently, needs residential proxies
  - **Status**: Actively maintained but fragile

### Available Public Metrics (via scraping/unofficial):
- Video: playCount (views), diggCount (likes), commentCount, shareCount, collectCount (saves)
- User: followerCount, heartCount (total likes), videoCount, followingCount

### What You CANNOT Get Free:
- Reach / impressions (not publicly shown on TikTok)
- Watch time / average view duration
- Audience demographics
- Traffic source breakdown
- Profile views
- Any historical time-series data

### Bottom Line
TikTok is the **hardest platform** for free analytics. The unofficial TikTok-Api can scrape basic video stats (views, likes, comments, shares) but is **fragile and unreliable**. No official free API exists for general use. Best approach: scrape basic public stats and accept the maintenance burden.

---

## 5. Open Source Analytics Dashboard Projects

### Tier 1 — Large, Active, Multi-Platform (Recommended)

**Postiz** (⭐ 31.6k stars, AGPL-3.0)
- github.com/gitroomhq/postiz-app
- Self-hosted social media scheduling + analytics
- **Platforms**: Instagram, YouTube, X, TikTok, LinkedIn, Reddit, Facebook, Pinterest, Threads, Bluesky, Discord, Mastodon, Slack, Dribbble
- **Tech**: NextJS + NestJS + Prisma + PostgreSQL + Temporal
- **Analytics**: Performance measurement, engagement tracking
- **Status**: Very active (2,600 commits, 78 contributors, v2.21.8)
- **Use for XPST**: Best reference for multi-platform integration patterns

**Mixpost** (⭐ 3.3k stars, MIT)
- github.com/inovector/mixpost
- Self-hosted, no subscriptions, no limits
- **Platforms**: 10+ social networks
- **Tech**: PHP/Laravel + Vue.js
- **Analytics**: Audience behavior insights per platform
- **Status**: Active (v2.6.0, March 2026)
- **Note**: Lite version is open source; Pro is commercial

**Socioboard** (⭐ ~3k stars, GPL)
- github.com/socioboard/Socioboard-5.0
- Social media management, analytics, and reporting
- **Platforms**: 9 social media networks
- **Tech**: Node.js/Express backend, PHP frontend
- **Status**: Older project, less actively maintained

### Tier 2 — Specialized / Smaller

**NafisRayan/Social-Media-Dashboard**
- Real-time insights, interactive data visualization
- GitHub hosted, smaller project

**instagrapi** (⭐ 6.3k stars) — Library, not dashboard
- Can serve as Instagram data source for custom dashboards

**TikTok-Api** (⭐ 5.1k stars) — Library, not dashboard
- Can serve as TikTok data source (fragile)

### Recommendations for XPST:
1. **Use Postiz as architectural reference** — it handles 14+ platforms and has working OAuth integrations
2. **Build custom dashboard** using the free APIs/scrapers identified above
3. **Data sources**: YouTube Analytics API + Data API (richest), instagrapi (good), twikit (basic), TikTok-Api (fragile)
4. **Consider**: Using Postiz's open-source integrations as data layer, building custom analytics visualization on top

---

## Summary Table

| Platform | Free Analytics Data | Library/Method | Reliability | Richness |
|----------|-------------------|---------------|-------------|----------|
| YouTube | views, likes, comments, subs, watch time, retention, demographics, traffic sources | YouTube Analytics API + Data API v3 | ⭐⭐⭐⭐⭐ Official, free | ⭐⭐⭐⭐⭐ |
| Instagram | reach, impressions, saves, shares, likes, comments, profile views | instagrapi (private API) | ⭐⭐⭐ Unofficial, risk of blocks | ⭐⭐⭐⭐ |
| X/Twitter | likes, retweets, views, replies, bookmarks | twikit (scraper) | ⭐⭐⭐ Unofficial, account risk | ⭐⭐⭐ |
| TikTok | views, likes, comments, shares (public only) | TikTok-Api (scraper) | ⭐⭐ Fragile, breaks often | ⭐⭐ |
