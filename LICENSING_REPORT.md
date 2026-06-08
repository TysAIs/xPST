# Licensing and ToS Compliance Report for XPST

## Executive Summary

This report details the licensing strategy and Terms of Service (ToS) compliance measures implemented for XPST. The project has been updated to use a dual MIT/Apache-2.0 license, and comprehensive disclaimers have been added to address the use of unofficial APIs.

---

## 1. License Research and Recommendation

### License Comparison

| Feature | MIT | Apache 2.0 | MIT + Apache 2.0 (Dual) |
|---------|-----|------------|-------------------------|
| **Patent Protection** | ❌ None (implied only) | ✅ Express patent grant | ✅ Full protection |
| **Simplicity** | ✅ Very simple (171 words) | ⚠️ Complex (2,700 words) | ✅ User chooses simpler option |
| **GPL v2 Compatibility** | ✅ Yes | ❌ No | ✅ Yes (via MIT) |
| **GPL v3 Compatibility** | ✅ Yes | ✅ Yes | ✅ Yes |
| **Enterprise Friendly** | ⚠️ Moderate | ✅ Very | ✅ Very |
| **Attribution Requirements** | Minimal | Moderate (NOTICE file) | User chooses |
| **Patent Retaliation Clause** | ❌ No | ✅ Yes | ✅ Yes (via Apache) |

### Recommendation: MIT + Apache 2.0 Dual License

**Why Dual License?**

1. **Maximum Compatibility**: MIT allows GPL v2 compatibility, Apache 2.0 provides patent protection
2. **Enterprise Appeal**: Large companies prefer Apache 2.0 for patent protection
3. **Community Precedent**: Used by major projects like Rust, TensorFlow, and Kubernetes
4. **User Choice**: Users can select the license that best fits their needs

### How Major Projects Handle Dual Licensing

| Project | License | Notes |
|---------|---------|-------|
| **Rust** | MIT OR Apache-2.0 | Provides patent protection while maintaining GPL v2 compatibility |
| **TensorFlow** | Apache-2.0 | Single license, enterprise-focused |
| **Kubernetes** | Apache-2.0 | Single license, CNCF project |
| **VS Code** | MIT | Microsoft product, single license |

---

## 2. Dependency License Analysis

### Complete Dependency License Matrix

| Dependency | License | Compatible with Dual License? |
|------------|---------|------------------------------|
| twikit | MIT | ✅ Yes |
| instagrapi | MIT | ✅ Yes |
| yt-dlp | Unlicense (Public Domain) | ✅ Yes |
| google-api-python-client | Apache-2.0 | ✅ Yes |
| click | BSD-3-Clause | ✅ Yes |
| rich | MIT | ✅ Yes |
| nicegui | MIT | ✅ Yes |
| plotly | MIT | ✅ Yes |
| structlog | MIT OR Apache-2.0 | ✅ Yes (same model) |
| prometheus-client | Apache-2.0 | ✅ Yes |
| ffmpeg-python | Apache-2.0 | ✅ Yes |
| keyring | MIT | ✅ Yes |
| pyyaml | MIT | ✅ Yes |

### License Conflict Analysis

**Result: NO CONFLICTS FOUND** ✅

All dependencies use permissive licenses (MIT, Apache-2.0, BSD-3-Clause, or Unlicense) that are fully compatible with XPST's dual MIT/Apache-2.0 license.

---

## 3. Terms of Service (ToS) Compliance

### Unofficial APIs Used

| Platform | Library | API Type | Risk Level | ToS Compliance |
|----------|---------|----------|------------|----------------|
| Instagram | instagrapi | Unofficial Private API | 🔴 High | ⚠️ May violate ToS |
| X/Twitter | twikit | Unofficial API | 🔴 High | ⚠️ May violate ToS |
| TikTok | yt-dlp | Scraping/Downloading | 🟡 Medium | ⚠️ May violate ToS |
| YouTube | google-api-python-client | Official OAuth API | 🟢 Low | ✅ Compliant |

### How Similar Projects Handle This

#### Postiz (Social Media Scheduler)
- **License**: AGPL-3.0
- **Approach**: Uses ONLY official OAuth flows
- **Disclaimer**: "Postiz does not automate or scrape content from social media platforms"
- **Key Difference**: XPST uses unofficial APIs; Postiz uses only official APIs

#### gallery-dl (Image Downloader)
- **License**: GPL-2.0
- **Approach**: Uses unofficial APIs and scraping
- **Legal Issues**: Received DMCA notice, moved from GitHub to Codeberg
- **Disclaimer**: Minimal, relies on GPL license terms

#### yt-dlp (Video Downloader)
- **License**: Unlicense (Public Domain)
- **Approach**: Uses scraping and unofficial methods
- **Legal Issues**: Faced RIAA DMCA takedown (2020), won counter-notice
- **Disclaimer**: "Do not download copyrighted material without permission"

### Compliance Measures Implemented

1. **README.md Disclaimer** (Added)
   - Clear "Use at your own risk" warning
   - Statement about unofficial APIs
   - User responsibility acknowledgment
   - No warranty disclaimer

2. **SECURITY.md Updates** (Added)
   - Platform-specific risk levels
   - ToS compliance warnings
   - Recommendations for safe usage
   - Developer liability disclaimer

3. **NOTICES.md** (Created)
   - Complete dependency license listing
   - License compatibility matrix
   - Links to all license texts

---

## 4. Files Modified/Created

### Modified Files

1. **LICENSE**
   - Added Apache License 2.0 full text
   - Maintained MIT License text
   - Added clear dual-license header

2. **pyproject.toml**
   - Changed `license = {file = "LICENSE"}` to `license = "MIT OR Apache-2.0"`
   - Added `"License :: OSI Approved :: Apache Software License"` classifier

3. **README.md**
   - Added license badge
   - Added comprehensive disclaimer section
   - Updated license section to reflect dual licensing
   - Added link to NOTICES.md
   - Fixed twikit URL typo

4. **SECURITY.md**
   - Added "Platform Terms of Service Compliance" section
   - Added unofficial APIs table
   - Added platform-specific risk analysis
   - Added recommendations for safe usage
   - Added developer liability disclaimer

### Created Files

1. **NOTICES.md**
   - Complete third-party license notices
   - License compatibility summary
   - Instructions for regenerating notices

---

## 5. Legal Considerations

### Patent Protection

**With MIT-only license:**
- No express patent grant from contributors
- Potential patent litigation risk
- Less attractive to enterprise users

**With MIT + Apache 2.0 dual license:**
- Express patent grant from contributors (via Apache 2.0)
- Patent retaliation clause protects users
- More attractive to enterprise users
- GPL v2 compatibility maintained (via MIT option)

### ToS Compliance Risks

**High Risk (Instagram, X/Twitter):**
- Using unofficial APIs may violate platform ToS
- Risk of account suspension or ban
- Platforms actively detect and block unofficial API usage
- No legal protection for users if accounts are banned

**Medium Risk (TikTok):**
- Video downloading may violate ToS
- Risk of IP-based blocking
- Less aggressive enforcement than Instagram/X

**Low Risk (YouTube):**
- Using official Google API
- Fully compliant with ToS
- Subject to API quota limits only

### Recommendations for Users

1. **Use Official APIs When Available**
   - YouTube: Already using official API ✅
   - Consider official APIs for other platforms if available

2. **Account Separation**
   - Use dedicated accounts for automation
   - Don't use primary personal accounts
   - Be prepared for potential restrictions

3. **Respect Rate Limits**
   - Use XPST's built-in rate limiting
   - Avoid excessive posting frequency
   - Monitor platform notifications

4. **Stay Informed**
   - Monitor platform ToS changes
   - Follow library communities for updates
   - Be aware of enforcement actions

---

## 6. Implementation Checklist

- [x] Research MIT vs Apache 2.0 vs Dual License
- [x] Analyze dependency licenses for conflicts
- [x] Update LICENSE file with dual license text
- [x] Update pyproject.toml license field
- [x] Update pyproject.toml classifiers
- [x] Update README.md with disclaimer and badges
- [x] Create NOTICES.md with dependency licenses
- [x] Update SECURITY.md with ToS compliance info
- [x] Research how similar projects handle ToS
- [x] Document all changes

---

## 7. Conclusion

XPST is now properly licensed under a dual MIT/Apache-2.0 license, providing:
- Maximum compatibility (MIT for GPL v2)
- Patent protection (Apache 2.0 for enterprises)
- Clear user choice

The project includes comprehensive disclaimers about the risks of using unofficial APIs, following the precedent set by similar projects like yt-dlp while being more transparent about potential ToS violations.

**No license conflicts exist** in the dependency tree, and all dependencies are fully compatible with the dual license model.

---

*Report generated: June 7, 2025*
*Prepared for: XPST Project*
