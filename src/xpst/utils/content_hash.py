"""Content hashing utilities for deduplication.

Provides content-based hashing to detect duplicate videos across platforms.
Uses video filename, file size, and partial file content to generate a
fingerprint that identifies the same video regardless of platform-specific
metadata or re-encoding.
"""

import hashlib
from pathlib import Path


def compute_content_hash(file_path: Path | None = None, filename: str | None = None) -> str:
    """Compute a content hash for deduplication.

    Combines filename and partial file content (first/last 64KB) to create a
    fingerprint that identifies the same video even after minor re-encoding.

    Args:
        file_path: Path to the video file. If provided, reads partial content.
        filename: Video filename. Used as fallback when no file path available.

    Returns:
        Hex digest string (SHA-256, first 16 chars for compactness).
    """
    h = hashlib.sha256()

    if filename:
        h.update(filename.encode("utf-8"))

    if file_path and file_path.exists():
        try:
            file_size = file_path.stat().st_size
            h.update(str(file_size).encode())

            with open(file_path, "rb") as f:
                # Read first 64KB for fingerprint
                chunk = f.read(65536)
                if chunk:
                    h.update(chunk)

                # Read last 64KB for fingerprint (helps with trailers/encoders)
                if file_size > 65536:
                    f.seek(max(0, file_size - 65536))
                    chunk = f.read(65536)
                    if chunk:
                        h.update(chunk)
        except OSError:
            pass

    return h.hexdigest()[:16]


def compute_caption_hash(caption: str) -> str:
    """Compute a normalized hash of a caption for similarity detection.

    Normalizes whitespace and case before hashing so that minor caption
    variations (extra spaces, different casing) still match.

    Args:
        caption: Post caption text.

    Returns:
        Hex digest string (SHA-256, first 16 chars).
    """
    normalized = " ".join(caption.lower().split())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]


def captions_are_similar(caption_a: str, caption_b: str, threshold: float = 0.85) -> bool:
    """Check if two captions are similar enough to be considered duplicates.

    Uses a simple word-overlap (Jaccard) similarity metric.

    Args:
        caption_a: First caption.
        caption_b: Second caption.
        threshold: Similarity threshold (0.0 to 1.0). Default 0.85.

    Returns:
        True if captions are similar above the threshold.
    """
    words_a = set(caption_a.lower().split())
    words_b = set(caption_b.lower().split())

    if not words_a or not words_b:
        return False

    intersection = words_a & words_b
    union = words_a | words_b

    if not union:
        return False

    similarity = len(intersection) / len(union)
    return similarity >= threshold
