"""Tests that the Webui shows quota usage and the inline rejection state.

The Webui is a dumb nginx static layer; like the Admin Panel tests these
pin the shipped single-file SPA (webui/index.html).
"""

from pathlib import Path

WEBUI_DIR = Path(__file__).resolve().parent.parent / "webui"


def read(name):
    return (WEBUI_DIR / name).read_text(encoding="utf-8")


def test_the_search_page_shows_quota_usage_vs_cap():
    html = read("index.html")

    assert 'id="quota-info"' in html
    assert "/me/quota" in html
    assert "loadQuota" in html
    # Unlimited and exempt Accounts show a "no cap" state, not a zero.
    assert "unlimited" in html
    assert "no cap" in html


def test_the_download_submission_carries_the_torznab_size_and_title():
    html = read("index.html")

    # The pre-check and the ownership record need the real numbers.
    assert "size_bytes: pendingSizeBytes" in html
    assert "pendingTitle" in html
    # A result's Download button hands the form its size and title.
    assert "prefillDownload(" in html


def test_the_quota_rejection_is_rendered_inline_on_the_download_form():
    html = read("index.html")

    # The submit handler renders the API's rejection detail in the form's
    # own message element: the inline "quota exceeded" state.
    assert 'id="download-message"' in html
    assert "${data.detail" in html
