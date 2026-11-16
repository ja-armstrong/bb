"""Tests that the Webui serves the public Registration Request page.

The Webui is a dumb nginx static layer; like the other Webui tests these
pin the shipped single-file page (webui/register.html) and the wiring
that serves and links it (ticket #29).
"""

from pathlib import Path

WEBUI_DIR = Path(__file__).resolve().parent.parent / "webui"


def read(name):
    return (WEBUI_DIR / name).read_text(encoding="utf-8")


# --- The landing page --------------------------------------------------------


def test_the_register_page_exists_as_its_own_single_file_page():
    html = read("register.html")

    assert "<!DOCTYPE html>" in html
    assert 'id="register-form"' in html


def test_the_register_page_is_served_by_nginx_at_a_distinct_path():
    conf = read("default.conf")

    assert "location /register" in conf
    assert "/register.html" in conf


def test_the_register_page_is_shipped_in_the_container():
    dockerfile = read("Dockerfile")

    assert "COPY register.html" in dockerfile


def test_the_login_page_links_to_the_register_page():
    html = read("index.html")

    assert 'href="/register"' in html
    assert "Request an account" in html


def test_the_register_page_links_back_to_the_login_page():
    html = read("register.html")

    assert 'href="/"' in html


# --- The form ----------------------------------------------------------------


def test_the_form_collects_username_password_confirm_and_note():
    html = read("register.html")

    assert 'id="register-username"' in html
    assert 'id="register-password"' in html
    assert 'id="register-confirm"' in html
    assert 'id="register-note"' in html
    assert "/register" in html  # the API endpoint the form posts to


def test_the_form_checks_the_two_passwords_match_before_sending():
    html = read("register.html")

    assert "password !== confirm" in html
    # USER-FACING
    assert "do not match" in html


def test_passwords_never_leave_the_page_as_plaintext_in_a_url():
    html = read("register.html")

    # The submission is a JSON POST body, never a query string.
    assert "method: \"POST\"" in html
    assert "Content-Type" in html


# --- The honest confirmation ---------------------------------------------------


def test_the_confirmation_does_not_promise_timing_or_email():
    html = read("register.html")

    # The confirmation is the API's message, shown via the flash area.
    assert "data.message" in html
    assert 'class="success"' in html
    # The page's own prose is honest too: reviewed manually, reach out for approval.
    assert "reviewed manually" in html.lower()
    assert "reach out via discord or email" in html.lower()


def test_failures_render_inline_in_the_flash_area():
    html = read("register.html")

    assert 'id="register-message"' in html
    assert "aria-live" in html
    assert "data.detail" in html


# --- No Discord in the form itself ----------------------------------------------
# The prose may mention discord as an out-of-band contact; the FORM must not
# collect anything Discord-related, and the account flow needs no linking.


def test_discord_has_no_part_in_the_registration_flow():
    html = read("register.html")

    assert 'id="register-discord"' not in html
    assert "link code" not in html.lower()
    assert "link_code" not in html


# --- Shared styling and a11y ------------------------------------------------------


def test_the_page_uses_the_shared_stylesheet():
    html = read("register.html")

    assert 'href="/style.css"' in html


def test_every_field_has_an_explicit_label():
    html = read("register.html")

    for input_id in (
        "register-username", "register-password",
        "register-confirm", "register-note",
    ):
        assert f'for="{input_id}"' in html
