# Font Choice: Inter

## Context

xPST needs a consistent font for its logo, dashboard, and UI elements.

## Research

### What YouTube Studio Uses
- **Roboto** — Google's flagship font, designed by Christian Robertson
- Clean, geometric, modern sans-serif
- Optimized for screen readability at all sizes

### What Apple Uses  
- **SF Pro** (San Francisco) — Apple's system font
- Neo-grotesque design, extremely legible
- Proprietary, not available for redistribution

## Candidates Evaluated

| Font | Designer | License | Roboto-like | SF Pro-like | Verdict |
|------|----------|---------|:-----------:|:-----------:|---------|
| **Inter** | Rasmus Andersson | OFL (open source) | ✅ | ✅ | **Selected** |
| Geist | Vercel | OFL | ✅ | ✅ | Excellent, but newer with less ecosystem support |
| Plus Jakarta Sans | Tokotype | OFL | ✅ | ❌ (more rounded) | Good but too playful |
| DM Sans | Colophon | OFL | ✅ | ❌ (more geometric) | Good but narrower personality |

## Decision: **Inter**

### Why Inter

1. **Bridges both aesthetics**: Inter has the geometric clarity of Roboto combined with the refined proportions of SF Pro
2. **Designed for screens**: Built specifically for computer displays with tall x-height and open letterforms
3. **Massive adoption**: Used by GitHub, Linear, Vercel, and thousands of production apps
4. **Complete weight range**: From Thin (100) to Black (900), plus italics — covers all design needs
5. **Open source**: OFL license, free for commercial use
6. **Excellent at all sizes**: Legible at 12px for UI text and striking at 120px for logos
7. **Variable font support**: Single file with adjustable weight for dynamic UI

### Where to Get Inter
- Official: https://rsms.me/inter/
- Google Fonts: https://fonts.google.com/specimen/Inter
- GitHub: https://github.com/rsms/inter

## Usage in xPST

- **Logo/Icons**: Inter Bold (weight 700)
- **Dashboard UI**: Inter Regular (400) and Inter Medium (500)
- **Code/Technical**: Inter Mono (monospace variant) or JetBrains Mono
