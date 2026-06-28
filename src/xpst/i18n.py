"""Simple i18n framework for xPST desktop app.

Provides a tr() lookup function that loads translation strings from
JSON files in ~/.xpst/translations/<lang>.json.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Current language (default English)
_current_lang: str = "en"
_translations: dict[str, str] = {}
_translations_dir: Path = Path("~/.xpst/translations").expanduser()


def set_language(lang: str) -> None:
    """Switch the active language and reload translations."""
    global _current_lang, _translations
    _current_lang = lang
    _translations = {}
    _load_translations(lang)


def get_language() -> str:
    """Return the current language code."""
    return _current_lang


def get_available_languages() -> list[str]:
    """Return a list of available language codes from translation files.

    Scans both the user translations directory and the bundled i18n
    directory for .json files and returns their stems as language codes.

    Returns:
        Sorted list of language code strings (e.g. ['en', 'es', 'fr']).
    """
    langs: list[str] = []
    for d in (_translations_dir, _translations_dir_bundled):
        if d and d.exists():
            for f in d.glob("*.json"):
                code = f.stem
                if code not in langs:
                    langs.append(code)
    if not langs:
        langs = ["en"]
    return sorted(langs)


def _load_translations(lang: str) -> None:
    """Load translation strings from ~/.xpst/translations/<lang>.json."""
    global _translations
    path = _translations_dir / f"{lang}.json"
    if not path.exists():
        if lang != "en":
            logger.debug("Translation file not found for '%s', falling back to 'en'", lang)
        return

    try:
        with open(path, encoding="utf-8") as f:
            _translations = json.load(f)
        logger.info("Loaded %d translations for '%s'", len(_translations), lang)
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Failed to load translations for '%s': %s", lang, exc)


def tr(key: str, fallback: str | None = None, **kwargs: Any) -> str:
    """Translate a key string using the current language.

    If the key is not found, returns the fallback (or the key itself).
    Supports simple {placeholder} substitution via kwargs.

    Args:
        key: Translation key (e.g. "settings.title").
        fallback: Default value if key not found. Defaults to key.
        **kwargs: Placeholder values for string formatting.

    Returns:
        Translated (or fallback) string.
    """
    result = _translations.get(key, fallback if fallback is not None else key)
    if kwargs:
        try:
            result = result.format(**kwargs)
        except (KeyError, IndexError, ValueError):
            pass
    return result


def load_translations_for(lang: str | None = None) -> dict[str, str]:
    """Load and return translations for the given language.

    Args:
        lang: Language code. If None, uses the current language.

    Returns:
        Dict mapping translation keys to translated strings.
    """
    target = lang or _current_lang
    path = _translations_dir / f"{target}.json"
    if not path.exists():
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


# Auto-load English on import — try bundled dir first, then user dir
try:
    _translations_dir.mkdir(parents=True, exist_ok=True)
except Exception:
    pass

# Also check for bundled translations (relative to this file)
_bundled_dir = Path(__file__).resolve().parent / "i18n"
_translations_dir_bundled = _bundled_dir if (_bundled_dir / "en.json").exists() else None

# Override load to also check bundled dir
def _load_translations(lang: str) -> None:
    """Load translation strings from ~/.xpst/translations/<lang>.json
    or from the bundled i18n/ directory."""
    global _translations

    # Try user dir first
    path = _translations_dir / f"{lang}.json"
    if not path.exists() and _translations_dir_bundled:
        # Fall back to bundled translations
        path = _translations_dir_bundled / f"{lang}.json"

    if not path.exists():
        if lang != "en":
            logger.debug("Translation file not found for '%s'", lang)
        return

    try:
        with open(path, encoding="utf-8") as f:
            _translations = json.load(f)
        logger.info("Loaded %d translations for '%s'", len(_translations), lang)
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Failed to load translations for '%s': %s", lang, exc)


try:
    _load_translations("en")
except Exception:
    pass
