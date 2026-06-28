"""The core CLI must not import heavy KB dependencies at import time."""
import subprocess
import sys


def test_importing_cli_does_not_load_faster_whisper():
    code = (
        "import sys; import xpst.cli; "
        "assert 'faster_whisper' not in sys.modules, "
        "'core CLI must not import faster_whisper at import time'; "
        "print('OK')"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr
    assert "OK" in result.stdout


def test_importing_cli_does_not_load_fastembed_or_lancedb():
    code = (
        "import sys; import xpst.cli; "
        "assert 'fastembed' not in sys.modules, "
        "'core CLI must not import fastembed at import time'; "
        "assert 'lancedb' not in sys.modules, "
        "'core CLI must not import lancedb at import time'; "
        "print('OK')"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr
    assert "OK" in result.stdout


def test_knowledge_package_imports_without_faster_whisper():
    import xpst.knowledge

    assert xpst.knowledge.__version__
