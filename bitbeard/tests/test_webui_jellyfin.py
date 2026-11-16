"""Tests that the Webui serves the self-service Jellyfin password page
(ticket #32).

The Webui is a dumb nginx static layer; like the other Webui tests these
pin the shipped single-file page (webui/index.html): the section's
markup, its capability-gated visibility, and the flash mechanics.
"""

from pathlib import Path

WEBUI_DIR = Path(__file__).resolve().parent.parent / "webui"


def read(name):
    return (WEBUI_DIR / name).read_text(encoding="utf-8")


# --- The section --------------------------------------------------------------


def test_the_section_exists_on_the_user_page_and_starts_hidden():
    html = read("index.html")

    assert 'id="jellyfin-password-section"' in html
    # Hidden in the markup; the capability flag un-hides it.
    assert 'id="jellyfin-password-section" hidden' in html


def test_the_page_uses_the_shared_stylesheet():
    html = read("index.html")

    assert 'href="/style.css"' in html


def test_every_field_has_an_explicit_label():
    html = read("index.html")

    assert 'for="jf-new-password"' in html
    assert 'for="jf-confirm-password"' in html


def test_both_password_fields_are_password_inputs_with_a_minimum():
    html = read("index.html")

    assert 'type="password" id="jf-new-password" minlength="8" required' in html
    assert 'type="password" id="jf-confirm-password" minlength="8" required' in html


def test_the_message_area_is_a_live_region():
    html = read("index.html")

    assert 'id="jf-password-message" aria-live="polite"' in html


# --- The honest prose ------------------------------------------------------------


def test_the_prose_says_the_provisioning_password_is_never_shown():
    html = read("index.html")

    assert "never shown" in html
    assert "replaces" in html


def test_the_prose_says_the_page_is_open_for_later_resets():
    html = read("index.html")

    assert "come back" in html


# --- The submission ----------------------------------------------------------------


def test_the_form_posts_to_the_api_endpoint():
    html = read("index.html")

    assert "/me/jellyfin-password" in html
    assert 'method: "POST"' in html
    assert "Content-Type" in html


def test_the_two_passwords_are_matched_before_sending():
    html = read("index.html")

    assert "password !== confirm" in html
    # USER-FACING
    assert "do not match" in html


def test_the_success_flash_speaks_the_api_message():
    html = read("index.html")

    assert 'class="success"' in html
    assert "data.message" in html


def test_refusals_render_inline_in_the_flash_area():
    html = read("index.html")

    assert 'class="error"' in html
    assert "data.detail" in html


def test_passwords_never_leave_the_page_as_plaintext_in_a_url():
    html = read("index.html")

    # The submission is a JSON POST body, never a query string.
    assert "JSON.stringify({ new_password: password })" in html


# --- Capability-gated visibility ----------------------------------------------------


def test_visibility_follows_the_auth_me_capability_flag():
    html = read("index.html")

    # The page learns (a) authenticated from the same /auth/me response
    # it already asks for, and (b) integration on + (c) Media Server is
    # Jellyfin from this one flag.
    assert "jellyfin_password_available" in html
    assert "jellyfin-password-section').hidden" in html
    assert "!me.jellyfin_password_available" in html
