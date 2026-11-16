"""Tests that the Admin Panel is served at /admin from the Webui container.

The Webui is a dumb nginx static/proxy layer (ADR-0002): the Admin Panel is
the single-file ``admin.html`` SPA served at ``/admin``, proxying the API
under the same origin so the Session cookie flows. There is no Python here
to boot, so these tests pin the nginx config and the shipped files.
"""

from pathlib import Path

WEBUI_DIR = Path(__file__).resolve().parent.parent / "webui"


def read(name):
    return (WEBUI_DIR / name).read_text(encoding="utf-8")


def test_the_admin_panel_spa_exists_and_targets_the_ledger_api():
    html = read("admin.html")

    assert 'id="login-view"' in html
    # The filterable ledger view: Actor, action and date filters.
    assert 'id="filter-actor"' in html
    assert 'id="filter-action"' in html
    assert 'id="filter-start"' in html
    assert 'id="filter-end"' in html
    assert "/admin/audit" in html


def test_the_admin_panel_enforces_the_admin_role_before_revealing_pages():
    html = read("admin.html")

    # The SPA checks the Role the API reports and refuses non-Admins.
    assert "role !== 'admin'" in html


def test_the_admin_panel_ships_the_users_page():
    html = read("admin.html")

    # The Users page: the create form, the per-Account action buttons and
    # the quota controls against the Account endpoints of ticket #8.
    assert 'id="page-users"' in html
    assert 'id="user-create-form"' in html
    assert 'id="user-rows"' in html
    assert "/admin/users" in html
    assert "revoke_sessions" in html
    # The Users nav button is live, no longer a placeholder.
    assert '<button data-page="users" disabled' not in html


def test_the_admin_panel_ships_the_files_page():
    html = read("admin.html")

    # The Files page of ticket #10: the tree browser rooted at the Media
    # Root plus delete/rename/move against the admin file endpoints.
    assert 'id="page-files"' in html
    assert 'id="file-rows"' in html
    assert 'id="files-up"' in html
    assert "/admin/files" in html
    assert "admin/files/${op}" in html
    assert 'data-file-op="delete"' in html
    assert 'data-file-op="rename"' in html
    assert 'data-file-op="move"' in html
    # The Files nav button is live, no longer a placeholder.
    assert '<button data-page="files" disabled' not in html


def test_the_admin_panel_ships_the_vpn_page():
    html = read("admin.html")

    # The VPN page of ticket #10: the settings form, the status + test
    # views, and the gluetun restart that applies the changes.
    assert 'id="page-vpn"' in html
    assert 'id="vpn-config-form"' in html
    assert 'id="vpn-status-rows"' in html
    assert 'id="vpn-test"' in html
    assert "/admin/vpn/config" in html
    assert "/admin/vpn/test" in html
    assert "/admin/vpn/status" in html
    # Applying the settings restarts gluetun (the #9 Subsystems action).
    assert "admin/services/gluetun/restart" in html
    # The VPN nav button is live, no longer a placeholder.
    assert '<button data-page="vpn" disabled' not in html


def test_the_admin_panel_ships_the_registrations_page():
    html = read("admin.html")

    # The Registrations page of ticket #30: the pending queue with the
    # stored fields, the approve/deny actions against the review
    # endpoints, the confirm gate on the destructive deny, and the plain
    # empty state.
    assert 'id="page-registrations"' in html
    assert 'id="registration-rows"' in html
    assert "/admin/registrations" in html
    assert "approve" in html
    assert "deny" in html
    assert "confirm(" in html
    assert "No pending requests." in html
    # The nav button is live, no longer a placeholder.
    assert '<button data-page="registrations" disabled' not in html


def test_nginx_serves_admin_html_at_admin():
    conf = read("default.conf")

    assert "location /admin" in conf
    assert "admin.html" in conf


def test_the_webui_image_ships_the_admin_panel():
    dockerfile = read("Dockerfile")

    assert "COPY admin.html" in dockerfile


def test_the_admin_panel_keeps_nginx_a_dumb_static_proxy_layer():
    conf = read("default.conf")

    # Same session flow as the Webui: the API is proxied under /api/.
    assert "location /api/" in conf
    # No basic auth anywhere: the API owns authentication.
    assert "auth_basic" not in conf
