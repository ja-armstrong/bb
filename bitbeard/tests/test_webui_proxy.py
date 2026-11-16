"""Tests that the Webui's nginx holds its side of the XFF contract.

The Webui is a dumb nginx static/proxy layer; like the other Webui
tests these pin the shipped configuration. The XFF contract
(docs/proxy.md) says the front proxy overwrites ``X-Forwarded-For``
with the real client address and everything behind it passes the
incoming value through verbatim — appending here would give a
client-supplied fake IP a ride in front of the real one, into the
API's per-IP throttle and Audit Ledger, which both read the first
entry.
"""

from pathlib import Path

WEBUI_DIR = Path(__file__).resolve().parent.parent / "webui"
REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def read_conf():
    return (WEBUI_DIR / "default.conf").read_text(encoding="utf-8")


# --- The /api/ proxy location ------------------------------------------------


def test_the_api_proxy_passes_the_incoming_xff_through_verbatim():
    conf = read_conf()

    # $http_x_forwarded_for is the incoming header, untouched: the front
    # proxy already did the overwrite, and the API reads the first (only)
    # entry.
    assert "$http_x_forwarded_for" in conf


def test_the_api_proxy_does_not_append_to_the_xff_chain():
    conf = read_conf()

    # $proxy_add_x_forwarded_for is nginx's appending variant: it would
    # let the client's fake IP precede the real one in the header the
    # API trusts. The directive itself must use the verbatim passthrough.
    assert (
        "proxy_set_header X-Forwarded-For $http_x_forwarded_for;" in conf
    )
    assert (
        "proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;"
        not in conf
    )


def test_the_api_proxy_still_sets_the_other_forwarding_headers():
    conf = read_conf()

    # The headers nginx itself owns stay: the peer it saw and the
    # scheme it served the request over.
    assert "X-Real-IP $remote_addr" in conf
    assert "X-Forwarded-Proto $scheme" in conf
    assert "Host $host" in conf


# --- The contract is written down ----------------------------------------------


def test_the_proxy_doc_states_the_contract():
    doc = (REPO_ROOT / "docs" / "proxy.md").read_text(encoding="utf-8")

    assert "overwrites" in doc.lower()
    assert "verbatim" in doc.lower()
    # The footgun that earns its loud warning: Secure cookies over
    # plain HTTP silently break login.
    assert "plain http" in doc.lower()
    assert "PUBLIC_HTTPS=0" in doc


def test_the_reference_caddyfile_covers_the_deployment():
    caddyfile = (REPO_ROOT / "docs" / "proxy" / "Caddyfile").read_text(
        encoding="utf-8"
    )

    # Two sites: the Webui and Jellyfin's subdomain.
    assert "reverse_proxy" in caddyfile
    assert "8081" in caddyfile  # the Webui's published host port
    assert "8096" in caddyfile  # Jellyfin's published host port
    # The edge is the overwrite: client-supplied XFF must not survive,
    # so no trusted_proxies unless another proxy really sits in front.
    assert "X-Forwarded-For" in caddyfile
