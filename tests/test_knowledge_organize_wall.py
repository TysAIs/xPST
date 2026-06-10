"""The Phase 3 organize package must stay dependency-light: importing it (and the
CLI that attaches it) must NOT load fastembed / faster_whisper / lancedb / numpy
at module load. Clustering and routing are pure Python over the embedding ports.
"""
import subprocess
import sys


def _run(code: str):
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr
    assert "OK" in result.stdout


def test_importing_organize_does_not_load_heavy_deps():
    _run(
        "import sys; import xpst.knowledge.organize; "
        "assert 'fastembed' not in sys.modules, 'organize must not import fastembed'; "
        "assert 'faster_whisper' not in sys.modules, 'organize must not import faster_whisper'; "
        "assert 'lancedb' not in sys.modules, 'organize must not import lancedb'; "
        "assert 'numpy' not in sys.modules, 'organize must not import numpy'; "
        "print('OK')"
    )


def test_importing_cli_with_organize_stays_light():
    _run(
        "import sys; import xpst.cli; "
        "assert 'fastembed' not in sys.modules; "
        "assert 'lancedb' not in sys.modules; "
        "assert 'faster_whisper' not in sys.modules; "
        "print('OK')"
    )


def test_organize_submodules_import_clean():
    _run(
        "import xpst.knowledge.organize.router as r; "
        "import xpst.knowledge.organize.cluster as c; "
        "import xpst.knowledge.organize.difficulty as d; "
        "import xpst.knowledge.organize.pipeline as p; "
        "assert r.route_nugget and c.discover_areas and d.tag_difficulty and p.organize_store; "
        "print('OK')"
    )
