"""Tests that the Webui ships the My Media page (ticket #6).

The Webui is a dumb nginx static layer; like the other Frontend tests
these pin the shipped single-file SPA (webui/index.html).
"""

from pathlib import Path

WEBUI_DIR = Path(__file__).resolve().parent.parent / "webui"


def read(name):
    return (WEBUI_DIR / name).read_text(encoding="utf-8")


def test_the_my_media_page_lists_the_actors_downloads():
    html = read("index.html")

    assert "My Media" in html
    assert 'id="media-list"' in html
    assert 'id="refresh-media"' in html
    assert 'id="mymedia-message"' in html
    # The listing comes from the scoped /media endpoint and is loaded
    # with the app, alongside quota and progress.
    assert "loadMedia" in html
    assert "${API_BASE}/media`" in html
    assert html.count("loadMedia();") >= 2


def test_in_flight_downloads_show_progress_and_a_cancel_button():
    html = read("index.html")

    assert "d.progress" in html
    assert "d.eta" in html
    assert 'onclick="cancelDownload(' in html
    assert 'mediaAction(id, "cancel")' in html
    # The per-Download cancel hits the scoped endpoint.
    assert "media/${id}/${action}" in html


def test_completed_downloads_have_delete_rename_and_move():
    html = read("index.html")

    assert 'onclick="deleteDownload(' in html
    assert 'onclick="renameDownload(' in html
    assert 'onclick="moveDownload(' in html
    assert 'mediaAction(id, "delete")' in html
    assert 'mediaAction(id, "rename", { name' in html
    assert 'mediaAction(id, "move", { category' in html
    # Deleting frees quota, so the usage line is refreshed afterwards.
    assert "loadQuota();" in html


def test_move_targets_the_six_categories():
    html = read("index.html")

    assert (
        "Move to Category (Movies, Shows, Audiobooks, eBooks, Music, Other)"
        in html
    )


def test_the_submission_form_offers_the_six_categories():
    html = read("index.html")

    for category in ("Movies", "Shows", "Other", "Audiobooks", "eBooks", "Music"):
        assert f'<option value="{category}">{category}</option>' in html


def test_guard_refusals_are_shown_inline():
    html = read("index.html")

    # The mymedia operations render the API's refusal detail (Protected
    # Path / Active Torrent 409s, quota, ownership) on the page.
    assert "${data.detail" in html
