"""Import-safety tests: the app module must import cleanly, without touching
the network, both as a package member and as a script (the Dockerfile runs
``python bitbeard.py`` with flat siblings on sys.path)."""
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_bitbeard_module_imports_as_package_member():
    import bitbeard.bitbeard  # noqa: F401  (must not raise)


def test_bitbeard_module_imports_as_script():
    """Simulate script mode: put the bitbeard/ dir itself on sys.path, so the
    module is imported as top-level ``bitbeard`` with flat siblings — the
    fallback import branch must work."""
    result = subprocess.run(
        [
            sys.executable, "-c",
            "import sys; sys.path.insert(0, 'bitbeard'); import bitbeard; "
            "print(bitbeard.__file__)",
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().endswith("bitbeard.py")
