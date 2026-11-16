package proxy

// config_test.go pins the pure half of the stage: domain validation, the
// derived names, the generated snippet against the #33 XFF contract, and
// the import-line decisions. No system, no network.

import (
	"strings"
	"testing"
)

// TestValidateDomain covers the table: normalization, the shape rules,
// and the rejections that protect the derived names.
func TestValidateDomain(t *testing.T) {
	cases := []struct {
		name  string
		in    string
		want  string
		noErr bool
	}{
		{"plain", "example.com", "example.com", true},
		{"trimmed and lowered", "  Example.COM. ", "example.com", true},
		{"trailing dot", "example.com.", "example.com", true},
		{"three labels", "media.sub.example.co.uk", "media.sub.example.co.uk", true},
		{"dashes inside labels", "my-media.example-1.com", "my-media.example-1.com", true},
		{"empty", "", "", false},
		{"spaces", "exa mple.com", "", false},
		{"slash (a URL)", "https://example.com", "", false},
		{"one label only", "localhost", "", false},
		{"IPv4", "203.0.113.7", "", false},
		{"empty label", "example..com", "", false},
		{"leading dash", "-example.com", "", false},
		{"trailing dash", "example-.com", "", false},
		{"bad character", "exämple.com", "", false},
		{"underscore", "my_example.com", "", false},
		{"label too long", strings.Repeat("x", 64) + ".com", "", false},
		{"derived prefix bitbeard", "bitbeard.example.com", "", false},
		{"derived prefix jellyfin", "jellyfin.example.com", "", false},
		{"too long overall", strings.Repeat("a", 250) + ".com", "", false},
	}
	for _, tc := range cases {
		got, err := ValidateDomain(tc.in)
		if tc.noErr {
			if err != nil {
				t.Errorf("%s: ValidateDomain(%q) = %q, %v; want %q", tc.name, tc.in, got, err, tc.want)
				continue
			}
			if got != tc.want {
				t.Errorf("%s: ValidateDomain(%q) = %q, want %q", tc.name, tc.in, got, tc.want)
			}
			continue
		}
		if err == nil {
			t.Errorf("%s: ValidateDomain(%q) accepted %q; want an error", tc.name, tc.in, got)
		}
	}
}

// TestDeriveHosts pins the derivation: the Webui on bitbeard.<domain>,
// Jellyfin on jellyfin.<domain>, the apex left free.
func TestDeriveHosts(t *testing.T) {
	webui, jellyfin := DeriveHosts("example.com")
	if webui != "bitbeard.example.com" || jellyfin != "jellyfin.example.com" {
		t.Errorf("DeriveHosts = %q, %q", webui, jellyfin)
	}
}

// TestSnippet checks the generated snippet against the #33 contract: the
// derived hostnames, the two upstream ports, no trusted_proxies anywhere
// (the edge overwrites XFF by default and must stay unspoofable), the
// websocket note for Jellyfin, and the automatic-HTTPS note.
func TestSnippet(t *testing.T) {
	a := Answers{Domain: "example.com", Email: "ops@example.com", Jellyfin: true}
	s := Snippet(a)

	if !strings.Contains(s, "bitbeard.example.com {") {
		t.Errorf("snippet lacks the derived Webui site:\n%s", s)
	}
	if !strings.Contains(s, "jellyfin.example.com {") {
		t.Errorf("snippet lacks the derived Jellyfin site:\n%s", s)
	}
	if !strings.Contains(s, "reverse_proxy 127.0.0.1:8081") {
		t.Errorf("snippet lacks the Webui upstream:\n%s", s)
	}
	if !strings.Contains(s, "reverse_proxy 127.0.0.1:8096") {
		t.Errorf("snippet lacks the Jellyfin upstream:\n%s", s)
	}
	// No trusted_proxies directive anywhere: the snippet only mentions it
	// in the contract's comments, never as configuration.
	for _, line := range strings.Split(s, "\n") {
		l := strings.TrimSpace(line)
		if strings.HasPrefix(l, "#") {
			continue
		}
		if strings.Contains(l, "trusted_proxies") {
			t.Errorf("a non-comment line carries trusted_proxies (the XFF contract forbids it on the edge):\n%s", s)
		}
	}
	if !strings.Contains(s, "OVERWRITES X-Forwarded-For") {
		t.Errorf("snippet does not state the XFF overwrite contract:\n%s", s)
	}
	if !strings.Contains(s, "websocket") {
		t.Errorf("snippet does not mention websocket support:\n%s", s)
	}
	if !strings.Contains(s, "Automatic HTTPS") {
		t.Errorf("snippet does not mention automatic TLS:\n%s", s)
	}
	// The ACME email is a global option, not a per-site one: it belongs
	// in the main Caddyfile the wizard creates, never in the snippet.
	if strings.Contains(s, "ops@example.com") {
		t.Errorf("the ACME email leaked into the snippet:\n%s", s)
	}
}

// TestSnippetWithoutJellyfin covers the Media-Server-less (or Plex) case:
// only the Webui block is written, no jellyfin host anywhere.
func TestSnippetWithoutJellyfin(t *testing.T) {
	s := Snippet(Answers{Domain: "example.com", Jellyfin: false})
	if !strings.Contains(s, "bitbeard.example.com {") {
		t.Errorf("snippet lacks the Webui site:\n%s", s)
	}
	if strings.Contains(s, "jellyfin") || strings.Contains(s, "8096") {
		t.Errorf("a snippet without Jellyfin must not carry the Jellyfin site:\n%s", s)
	}
}

// TestMainCaddyfileContent covers the fresh-machine creation: the global
// options block with the optional email, then the import line.
func TestMainCaddyfileContent(t *testing.T) {
	withEmail := MainCaddyfileContent("ops@example.com", "import /etc/caddy/caddy.d/*")
	if !strings.Contains(withEmail, "\temail ops@example.com\n") {
		t.Errorf("created Caddyfile lacks the ACME email:\n%s", withEmail)
	}
	if !strings.HasSuffix(withEmail, "import /etc/caddy/caddy.d/*\n") {
		t.Errorf("created Caddyfile does not end with the import line:\n%s", withEmail)
	}
	if !strings.HasPrefix(withEmail, "{\n") {
		t.Errorf("created Caddyfile must start with the global options block:\n%s", withEmail)
	}

	without := MainCaddyfileContent("", "import /etc/caddy/caddy.d/*")
	if strings.Contains(without, "email") {
		t.Errorf("an omitted email must not appear:\n%s", without)
	}
	if !strings.Contains(without, "{\n}\n") {
		t.Errorf("the empty global options block must still be valid Caddyfile:\n%s", without)
	}
}

// TestHasSnippetImport covers the import detection: the exact line, the
// wildcard form, a single file from the directory, and comment lines that
// must not count.
func TestHasSnippetImport(t *testing.T) {
	cases := []struct {
		name    string
		content string
		want    bool
	}{
		{"exact", "import /etc/caddy/caddy.d/*\n", true},
		{"untrimmed", "  import /etc/caddy/caddy.d/*\n", true},
		{"single file", "import /etc/caddy/caddy.d/bitbeard.caddy\n", true},
		{"commented out", "# import /etc/caddy/caddy.d/*\n", false},
		{"missing", "example.com {\n\treverse_proxy 127.0.0.1:8081\n}\n", false},
		{"other import", "import /etc/caddy/other.d/*\n", false},
	}
	for _, tc := range cases {
		if got := HasSnippetImport(tc.content, DefaultSnippetPath); got != tc.want {
			t.Errorf("%s: HasSnippetImport = %v, want %v", tc.name, got, tc.want)
		}
	}
}

// TestWithImportAdded covers the one append the wizard ever makes to an
// operator-owned file: content preserved byte for byte, the import line
// at the end, a newline separating them.
func TestWithImportAdded(t *testing.T) {
	line := "import /etc/caddy/caddy.d/*"

	// Normal content with a trailing newline.
	next := WithImportAdded("example.com {\n\treverse_proxy 127.0.0.1:8081\n}\n", line)
	if next != "example.com {\n\treverse_proxy 127.0.0.1:8081\n}\n\n"+line+"\n" {
		t.Errorf("WithImportAdded = %q", next)
	}

	// Content without a trailing newline gains one before the import.
	next = WithImportAdded("example.com {\n}", line)
	if next != "example.com {\n}\n\n"+line+"\n" {
		t.Errorf("WithImportAdded (no trailing newline) = %q", next)
	}

	// Empty content is just the line.
	if next := WithImportAdded("", line); next != line+"\n" {
		t.Errorf("WithImportAdded (empty) = %q", next)
	}

	// Whitespace-only content is treated as empty.
	if next := WithImportAdded("\n\n", line); next != line+"\n" {
		t.Errorf("WithImportAdded (blank) = %q", next)
	}
}

// TestDiff covers the shown-diff rendering: old lines as context, the
// appended import line(s) with a + prefix.
func TestDiff(t *testing.T) {
	old := "example.com {\n\treverse_proxy 127.0.0.1:8081\n}\n"
	line := "import /etc/caddy/caddy.d/*"
	got := Diff(old, WithImportAdded(old, line))
	want := []string{
		"  example.com {",
		"  \treverse_proxy 127.0.0.1:8081",
		"  }",
		"+ ",
		"+ " + line,
	}
	if len(got) != len(want) {
		t.Fatalf("Diff = %q, want %q", got, want)
	}
	for i := range want {
		if got[i] != want[i] {
			t.Errorf("Diff[%d] = %q, want %q", i, got[i], want[i])
		}
	}
}

// TestValidateEmail covers the optional email: empty is fine, one @ with
// a valid domain part is fine, everything else is refused.
func TestValidateEmail(t *testing.T) {
	cases := []struct {
		name  string
		in    string
		want  string
		noErr bool
	}{
		{"empty allowed", "", "", true},
		{"valid", "ops@example.com", "ops@example.com", true},
		{"lowered domain", "Ops@EXAMPLE.com", "Ops@example.com", true},
		{"two ats", "a@b@c.com", "", false},
		{"no at", "ops.example.com", "", false},
		{"empty local", "@example.com", "", false},
		{"bad domain part", "ops@example", "", false},
	}
	for _, tc := range cases {
		got, err := ValidateEmail(tc.in)
		if tc.noErr {
			if err != nil || got != tc.want {
				t.Errorf("%s: ValidateEmail(%q) = %q, %v; want %q", tc.name, tc.in, got, err, tc.want)
			}
			continue
		}
		if err == nil {
			t.Errorf("%s: ValidateEmail(%q) accepted %q", tc.name, tc.in, got)
		}
	}
}
