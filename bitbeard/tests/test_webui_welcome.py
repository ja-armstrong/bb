"""Tests for the Webui's first-login welcome modal.

The Webui is a dumb nginx static layer; like the other Webui tests these
pin the shipped single-file page (webui/index.html): the modal's markup,
its server-flagged auto-show, the Jellyfin capability gating, and the
re-open affordance in the userbar.
"""

from pathlib import Path

WEBUI_DIR = Path(__file__).resolve().parent.parent / "webui"


def read(name):
    return (WEBUI_DIR / name).read_text(encoding="utf-8")


# --- The dialog ----------------------------------------------------------------


def test_the_modal_is_a_dialog_over_an_overlay_and_starts_hidden():
    html = read("index.html")

    assert 'class="modal-overlay" id="welcome-overlay" hidden' in html
    assert 'role="dialog" aria-modal="true"' in html
    assert 'aria-labelledby="welcome-title"' in html


def test_the_quick_jumps_cover_every_tab():
    html = read("index.html")

    for page in ("search", "downloads", "media", "account"):
        assert f'data-jump="{page}"' in html


def test_the_jumps_close_the_modal_and_open_their_tab():
    html = read("index.html")

    assert "showPage(page);" in html
    assert "this.close();" in html


# --- The auto-show and the flag ---------------------------------------------------


def test_the_modal_auto_shows_only_while_the_flag_is_unset():
    html = read("index.html")

    assert "me.tutorial_seen" in html
    assert "!me.tutorial_seen" in html
    assert "welcome.open()" in html


def test_dismissing_posts_the_seen_flag_to_the_api():
    html = read("index.html")

    assert "/me/tutorial-seen" in html
    assert 'method: "POST"' in html


def test_a_failed_flag_post_still_closes_the_modal():
    # Network trouble must never trap the user in the welcome modal.
    html = read("index.html")

    assert ".catch(() => {});" in html


# --- The re-open affordance ---------------------------------------------------------


def test_the_userbar_has_a_help_button():
    html = read("index.html")

    assert 'id="help-btn"' in html
    assert 'aria-label="???"' in html
    assert "welcome.open();" in html


# --- Jellyfin gating ----------------------------------------------------------------


def test_the_jellyfin_block_rides_the_capability_flag():
    html = read("index.html")

    assert 'id="welcome-jellyfin" hidden' in html
    assert "jellyfin_password_available" in html
    assert "applyCapability()" in html


def test_the_to_jellyfin_button_opens_the_served_url():
    html = read("index.html")

    assert 'id="to-jellyfin"' in html
    assert 'target="_blank"' in html
    assert 'rel="noopener"' in html
    # The URL comes from /auth/me, not a hardcoded constant.
    assert "me.jellyfin_url" in html


def test_the_password_shortcut_lands_on_the_account_tab():
    html = read("index.html")

    assert 'id="welcome-jellyfin-now"' in html
    assert "showPage('account')" in html
    assert "jellyfin-password-section" in html
    assert "scrollIntoView" in html


# --- The copy -----------------------------------------------------------------------


def test_the_modal_speaks_the_owner_copy():
    html = " ".join(read("index.html").split())

    assert "avast, etc." in html
    assert "proooomise" in html
    assert "webrip like a good little pirate" in html
    # The slice-maintenance addendum, verbatim.
    assert (
        "you are also responsible for maintaining your slice" in html
    )
    assert (
        "i can change limits on a case by case basis" in html
    )


def test_the_jellyfin_prose_explains_the_random_password():
    html = " ".join(read("index.html").split())

    assert "automatically generated" in html
    assert "set a new password through bitbeard" in html


def test_the_buttons_speak_the_owner_labels():
    html = read("index.html")

    assert ">oki</button>" in html
    assert "TO JELLYFIN" in html
    assert "let me change my jellyfin password now" in html
