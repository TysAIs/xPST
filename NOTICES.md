# Third-Party Notices

This project uses the following open-source libraries. Each library is listed with its license type and a link to its full license text.

## Core Dependencies

### click
- **License**: BSD-3-Clause
- **Repository**: https://github.com/pallets/click
- **License Text**: https://github.com/pallets/click/blob/main/LICENSE.rst

### pyyaml
- **License**: MIT
- **Repository**: https://github.com/yaml/pyyaml
- **License Text**: https://github.com/yaml/pyyaml/blob/main/LICENSE

### rich
- **License**: MIT
- **Repository**: https://github.com/Textualize/rich
- **License Text**: https://github.com/Textualize/rich/blob/master/LICENSE

## Video Downloading

### yt-dlp
- **License**: Unlicense (Public Domain)
- **Repository**: https://github.com/yt-dlp/yt-dlp
- **License Text**: https://github.com/yt-dlp/yt-dlp/blob/master/LICENSE

## Platform APIs

### google-api-python-client
- **License**: Apache-2.0
- **Repository**: https://github.com/googleapis/google-api-python-client
- **License Text**: https://github.com/googleapis/google-api-python-client/blob/main/LICENSE

### google-auth-oauthlib
- **License**: Apache-2.0
- **Repository**: https://github.com/googleapis/google-auth-oauthlib
- **License Text**: https://github.com/googleapis/google-auth-oauthlib/blob/main/LICENSE

### google-auth-httplib2
- **License**: Apache-2.0
- **Repository**: https://github.com/googleapis/google-auth-httplib2
- **License Text**: https://github.com/googleapis/google-auth-httplib2/blob/main/LICENSE

### twikit
- **License**: MIT
- **Repository**: https://github.com/david-lev/twikit
- **License Text**: https://github.com/david-lev/twikit/blob/main/LICENSE

### instagrapi
- **License**: MIT
- **Repository**: https://github.com/subzeroid/instagrapi
- **License Text**: https://github.com/subzeroid/instagrapi/blob/master/LICENSE

## Video Processing

### ffmpeg-python
- **License**: Apache-2.0
- **Repository**: https://github.com/kkroening/ffmpeg-python
- **License Text**: https://github.com/kkroening/ffmpeg-python/blob/master/LICENSE

## Monitoring

### structlog
- **License**: MIT OR Apache-2.0 (Dual License)
- **Repository**: https://github.com/hynek/structlog
- **License Text**: https://github.com/hynek/structlog/blob/main/LICENSE.txt

### prometheus-client
- **License**: Apache-2.0
- **Repository**: https://github.com/prometheus/client_python
- **License Text**: https://github.com/prometheus/client_python/blob/master/LICENSE

## Security

### keyring
- **License**: MIT
- **Repository**: https://github.com/jaraco/keyring
- **License Text**: https://github.com/jaraco/keyring/blob/main/LICENSE

## Dashboard

### nicegui
- **License**: MIT
- **Repository**: https://github.com/zauberzeug/nicegui
- **License Text**: https://github.com/zauberzeug/nicegui/blob/main/LICENSE.txt

### plotly
- **License**: MIT
- **Repository**: https://github.com/plotly/plotly.py
- **License Text**: https://github.com/plotly/plotly.py/blob/master/LICENSE.txt

---

## License Compatibility Summary

All dependencies are compatible with XPST's dual MIT/Apache-2.0 license:

| License Type | Count | Compatibility |
|--------------|-------|---------------|
| MIT | 8 | ✅ Fully compatible |
| Apache-2.0 | 5 | ✅ Fully compatible |
| BSD-3-Clause | 1 | ✅ Fully compatible |
| Unlicense | 1 | ✅ Public domain, fully compatible |
| Dual MIT/Apache-2.0 | 1 | ✅ Fully compatible |

### Notes

1. **yt-dlp** is licensed under the Unlicense, which dedicates the work to the public domain. This is fully compatible with any license.

2. **structlog** uses the same dual MIT/Apache-2.0 licensing model as XPST.

3. All permissive licenses (MIT, Apache-2.0, BSD-3-Clause) are compatible with each other and allow use in both open-source and proprietary projects.

4. There are **no license conflicts** in XPST's dependency tree.

---

## Generating This File

To regenerate this file with current dependency information:

```bash
# Install licensecheck
pip install licensecheck

# Generate license report
licensecheck --format markdown > NOTICES.md

# Or use pip-licenses
pip install pip-licenses
pip-licenses --format=markdown --with-urls --with-license-file > NOTICES.md
```

---

*Last updated: June 2025*
