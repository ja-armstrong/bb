"""Make the repo root (the service packages) importable from the tests.

Tests also act as the deployment's browser: Bitbeard is always served
behind the HTTPS reverse proxy (docs/proxy.md), so Session cookies are
issued with the Secure flag by default (PUBLIC_HTTPS=1). A real browser
over TLS sends Secure cookies happily; a cookie jar over plain http://
would refuse — the plain-HTTP footgun the docs warn about. So the
TestClient's default origin is https://testserver, and every test that
logs in through the API keeps working like the deployment it imitates.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from starlette.testclient import TestClient  # noqa: E402

_original_testclient_init = TestClient.__init__


def _https_testclient_init(self, *args, **kwargs):
    kwargs.setdefault("base_url", "https://testserver")
    _original_testclient_init(self, *args, **kwargs)


TestClient.__init__ = _https_testclient_init
