"""Tests that the Webui can generate a Link Code (ticket #7).

The Webui is a dumb nginx static layer; like the other Webui tests these
pin the shipped single-file SPA (webui/index.html).
"""

from pathlib import Path

WEBUI_DIR = Path(__file__).resolve().parent.parent / "webui"


def read(name):
    return (WEBUI_DIR / name).read_text(encoding="utf-8")


def test_the_webui_can_generate_a_link_code_for_a_discord_id():
    html = read("index.html")

    assert 'id="link-form"' in html
    assert 'id="link-discord-id"' in html
    assert "/link/code" in html
    assert "discord_id" in html


def test_the_link_code_result_tells_the_user_how_to_finish_linking():
    html = read("index.html")

    # The code is shown once, with the verification step spelled out:
    # the binding completes on the next Bot interaction, not in the form.
    assert "Link Code" in html
    assert "Send any message to the Bot" in html


def test_link_code_failures_render_inline():
    html = read("index.html")

    assert 'id="link-message"' in html
    assert "${data.detail || \"Failed to generate Link Code\"}" in html
