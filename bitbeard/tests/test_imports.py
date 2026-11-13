"""Import-safety tests.

Every service module must import cleanly without environment variables or
network access, so the Docker images can start before configuration is read.
The API in particular must not pull in qBittorrent, Jackett search, or
requests: media work belongs to the Download Manager.
"""
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def run_python(code):
    return subprocess.run(
        [sys.executable, "-c", code],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_all_service_modules_import_without_environment():
    result = run_python(
        "import api.main, manager.main, bot.main, shared.db, shared.security; "
        "print('ok')"
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"


def test_api_does_not_import_qbittorrent_or_search():
    result = run_python(
        "import sys, api.main; "
        "assert 'qbittorrentapi' not in sys.modules, 'qbittorrentapi imported'; "
        "assert 'requests' not in sys.modules, 'requests imported'; "
        "assert 'manager.search' not in sys.modules, 'search imported'; "
        "print('clean')"
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "clean"
