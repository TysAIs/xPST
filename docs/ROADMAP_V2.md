# xPST v2.0 — Universal Cross-Platform Content Manager

## Vision
One tool to post from ANY platform to ALL others. Upload once, distribute everywhere.
Free. Local. Open source. Bulletproof.

## Architecture Overview

```
┌─────────────────────────────────────────────────────┐
│                   xPST v2.0                     │
├─────────────────────────────────────────────────────┤
│  Sources (Download)  │  Targets (Upload)             │
│  ┌─────────────┐     │  ┌─────────────────┐         │
│  │ Instagram   │────▶│  │ YouTube Shorts  │         │
│  │ TikTok      │────▶│  │ Instagram Reels │         │
│  │ YouTube     │────▶│  │ X/Twitter       │         │
│  │ X/Twitter   │────▶│  │ TikTok*         │         │
│  │ Local Files │────▶│  │ (stitch only)   │         │
│  └─────────────┘     │  └─────────────────┘         │
├─────────────────────────────────────────────────────┤
│  Content Types:                                      │
│  • Single Video → All platforms                      │
│  • Carousel → IG native, X thread, YT/TikTok stitch │
├─────────────────────────────────────────────────────┤
│  Dashboard (NiceGUI) on localhost:8080               │
│  • YouTube Studio-style analytics                    │
│  • Post management with links                        │
│  • Platform health monitoring                        │
│  • Content calendar                                  │
└─────────────────────────────────────────────────────┘
```

## Feature Breakdown

### Phase 1: Multi-Source Support
- Any platform as source (not just TikTok)
- Download from: Instagram, TikTok, YouTube, X, local files
- `xpst pull --from instagram --to all`
- `xpst post --video local.mp4 --to all`

### Phase 2: Carousel Support
- **Instagram**: `album_upload()` — native, up to 20 items
- **X/Twitter**: Thread creation via `reply_to` — 1 video or 4 images per tweet
- **YouTube**: Compile to single Short with FFmpeg
- **TikTok**: Compile to video with FFmpeg (no free photo API)

### Phase 3: Analytics Dashboard
- NiceGUI on localhost:8080
- Per-post analytics from all platforms
- YouTube Analytics API (views, likes, watch time, demographics)
- Instagram insights (reach, impressions, saves, shares)
- X metrics (likes, retweets, views, bookmarks)
- TikTok metrics (plays, likes, comments, shares — best effort)

### Phase 4: Source Detection
- Auto-detect content type (video vs carousel)
- Auto-detect source platform from URL
- `xpst post --url https://www.instagram.com/reel/...`

## Technical Decisions

### Dashboard: NiceGUI
- Built on Vue/Quasar (Material Design, dark mode, responsive)
- Native Plotly chart integration
- Runs as normal Python (no special CLI)
- FastAPI backend for API routes
- Mobile-responsive out of the box

### Carousel Strategy
| Source | Instagram | X/Twitter | YouTube | TikTok |
|--------|-----------|-----------|---------|--------|
| Instagram carousel | Native album | Thread | Stitch video | Stitch video |
| TikTok slideshow | Download images | Thread | Stitch video | N/A |
| YouTube video | Single clip | Single tweet | N/A | N/A |
| Local files | Per type | Thread | Stitch video | Stitch video |

### Analytics Data Sources
| Platform | Method | Metrics |
|----------|--------|---------|
| YouTube | Analytics API v2 | views, likes, comments, watch time, subs, demographics |
| Instagram | instagrapi `insights_*()` | reach, impressions, saves, shares, likes, comments |
| X/Twitter | twikit tweet object | likes, retweets, views, replies, bookmarks |
| TikTok | tiktok-api scrape | plays, likes, comments, shares (fragile) |
