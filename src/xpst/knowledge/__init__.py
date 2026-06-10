"""xPST Knowledge Base — optional subsystem.

Imported lazily by the CLI/MCP only when ``xpst[knowledge]`` is installed.
Nothing in this package may be imported by the cross-poster core.
Heavy dependencies (faster_whisper) are imported inside functions, never here.
"""
from __future__ import annotations

__all__ = ["__version__"]
__version__ = "0.1.0"
