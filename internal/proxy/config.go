package proxy

// config.go is the pure half of the reverse-proxy stage: the answers it
// is driven by, the domain validation, the derived public names, and the
// generated Caddyfile text. Everything here is string-in, string-out so
// the tests can pin the exact config the #33 contract demands without
// touching a system.

import (
	"fmt"
	"strings"
)

// Answers is what the wizard collected for the reverse-proxy stage.
type Answers struct {
	// Domain is the validated base domain (e.g. example.com); the two
	// public names derive from it.
	Domain string
	// Email is the optional ACME email; "" means none.
	Email string
	// Jellyfin is true when the derived jellyfin.<domain> site block is
	// written — the wizard passes its Jellyfin profile choice here.
	Jellyfin bool
}

// The host paths and the upstream ports of the managed configuration.
// The upstreams are the Webui's and Jellyfin's published host ports (the
// same numbers the doctor probes and the reference Caddyfile points at).
const (
	UpstreamWebui    = "127.0.0.1:8081"
	UpstreamJellyfin = "127.0.0.1:8096"

	// DefaultSnippetDir and friends are the host locations of the managed
	// configuration. The snippet is a separate file under a dedicated
	// directory so the main Caddyfile never has to be rewritten, only
	// imported from.
	DefaultSnippetDir  = "/etc/caddy/caddy.d"
	DefaultSnippetPath = DefaultSnippetDir + "/bitbeard.caddy"
	DefaultMainPath    = "/etc/caddy/Caddyfile"
)

// importLineFor returns the import line that pulls the snippet directory
// into the main Caddyfile.
func importLineFor(snippetPath string) string {
	return "import " + dirOf(snippetPath) + "/*"
}

// dirOf returns the directory part of a slash-separated path (the proxy
// package only ever sees absolute Unix paths).
func dirOf(path string) string {
	if i := strings.LastIndex(path, "/"); i > 0 {
		return path[:i]
	}
	return DefaultSnippetDir
}

// DeriveHosts returns the two public names for a base domain: the Webui
// on bitbeard.<domain>, Jellyfin on jellyfin.<domain>. Derived, not asked
// twice (ticket #34, owner addendum); nonstandard names remain a snippet
// edit away because the operator owns the generated file.
func DeriveHosts(domain string) (webui, jellyfin string) {
	return "bitbeard." + domain, "jellyfin." + domain
}

// ValidateDomain normalizes and validates a base domain: trimmed, lower-
// cased, without a trailing dot. A valid answer has at least two labels
// (example.com, not just com), only [a-z0-9-] label characters without
// leading or trailing dashes, and is not an IP address — the derived
// names are DNS names Let's Encrypt must be able to issue for.
func ValidateDomain(v string) (string, error) {
	d := strings.ToLower(strings.TrimSpace(v))
	d = strings.TrimSuffix(d, ".")
	if d == "" {
		// USER-FACING
		return "", fmt.Errorf("the base domain cannot be empty")
	}
	if strings.ContainsAny(d, " \t/") {
		// USER-FACING
		return "", fmt.Errorf("the base domain is a plain DNS name like example.com — no spaces, slashes or paths")
	}
	// The two names derive as bitbeard.<domain> and jellyfin.<domain>;
	// an answer that already starts with one of those prefixes would
	// derive doubled names (bitbeard.bitbeard.…), which is never what
	// the operator means.
	if strings.HasPrefix(d, "bitbeard.") || strings.HasPrefix(d, "jellyfin.") {
		// USER-FACING
		return "", fmt.Errorf("%q looks like one of the derived names; give the base domain only (e.g. example.com) — bitbeard.<domain> and jellyfin.<domain> are derived from it", d)
	}
	labels := strings.Split(d, ".")
	if len(labels) < 2 {
		// USER-FACING
		return "", fmt.Errorf("%q has no dot; give a domain with at least two labels, e.g. example.com", d)
	}
	if len(labels) >= 4 && isAllDigits(strings.Join(labels, "")) {
		// USER-FACING
		return "", fmt.Errorf("%q looks like an IP address; give a DNS domain name like example.com", d)
	}
	for _, l := range labels {
		if l == "" {
			// USER-FACING
			return "", fmt.Errorf("%q has an empty label (a stray dot); give a plain DNS name like example.com", d)
		}
		if len(l) > 63 {
			// USER-FACING
			return "", fmt.Errorf("the label %q is longer than 63 characters", l)
		}
		for _, r := range l {
			if (r < 'a' || r > 'z') && (r < '0' || r > '9') && r != '-' {
				// USER-FACING
				return "", fmt.Errorf("%q contains a character DNS names cannot use; use letters, digits and dashes only", d)
			}
		}
		if strings.HasPrefix(l, "-") || strings.HasSuffix(l, "-") {
			// USER-FACING
			return "", fmt.Errorf("the label %q starts or ends with a dash; DNS labels cannot", l)
		}
	}
	if len(d) > 253 {
		// USER-FACING
		return "", fmt.Errorf("%q is longer than the 253 characters a DNS name allows", d)
	}
	return d, nil
}

// isAllDigits reports whether s is digits only.
func isAllDigits(s string) bool {
	for _, r := range s {
		if r < '0' || r > '9' {
			return false
		}
	}
	return len(s) > 0
}

// ValidateEmail validates the optional ACME email: empty is allowed (the
// operator may deliberately omit it), anything else must look like an
// address — one @, non-empty on both sides, a DNS-shaped domain part.
func ValidateEmail(v string) (string, error) {
	v = strings.TrimSpace(v)
	if v == "" {
		return "", nil
	}
	if strings.Count(v, "@") != 1 {
		// USER-FACING
		return "", fmt.Errorf("%q is not an email address (exactly one @ expected); leave empty to skip the ACME email", v)
	}
	local, domain, _ := strings.Cut(v, "@")
	if local == "" {
		// USER-FACING
		return "", fmt.Errorf("%q has nothing before the @", v)
	}
	d, err := ValidateDomain(domain)
	if err != nil {
		// USER-FACING
		return "", fmt.Errorf("the domain part of %q is not a valid domain: %v", v, err)
	}
	return local + "@" + d, nil
}

// Snippet renders the managed Caddyfile snippet for a: the site blocks
// generated from the answers, written verbatim to the snippet path.
//
// It complies with the X-Forwarded-For contract of docs/proxy.md
// (ticket #33): this Caddy is the first server the client touches, so it
// OVERWRITES XFF with the real client address — which Caddy does by
// default, with no directive and no trusted_proxies option. The websockets
// of the Jellyfin activity feed are likewise handled natively by
// reverse_proxy; adding legacy websocket directives would be wrong, so the
// contract lives in the comments where the operator reads it.
func Snippet(a Answers) string {
	webui, jellyfin := DeriveHosts(a.Domain)
	var b strings.Builder
	// USER-FACING
	b.WriteString("# Managed snippet for Bitbeard, written by `bitbeard setup`.\n")
	// USER-FACING
	b.WriteString("# The operator owns this file: edit it freely, but a reconfigure run of\n")
	// USER-FACING
	b.WriteString("# `bitbeard setup` regenerates it from the wizard's answers.\n")
	// USER-FACING
	b.WriteString("#\n")
	// USER-FACING
	b.WriteString("# The XFF contract (docs/proxy.md): this Caddy is the FIRST server the\n")
	// USER-FACING
	b.WriteString("# client touches, so it OVERWRITES X-Forwarded-For with the real client\n")
	// USER-FACING
	b.WriteString("# address. Caddy does that by default — incoming X-Forwarded-For values\n")
	// USER-FACING
	b.WriteString("# from clients are ignored (untrusted) unless trusted_proxies says\n")
	// USER-FACING
	b.WriteString("# otherwise, so client-supplied fake IPs never survive the edge. Do NOT\n")
	// USER-FACING
	b.WriteString("# add a trusted_proxies global option unless another proxy or CDN really\n")
	// USER-FACING
	b.WriteString("# sits in front of this Caddy — that is what re-opens the spoofing hole\n")
	// USER-FACING
	b.WriteString("# the contract exists to close.\n")
	// USER-FACING
	b.WriteString("#\n")
	// USER-FACING
	b.WriteString("# Automatic HTTPS is on: TLS certificates for both names are issued and\n")
	// USER-FACING
	b.WriteString("# renewed by Caddy itself, once their DNS points at this machine.\n")
	// USER-FACING
	b.WriteString("\n")
	// USER-FACING
	b.WriteString(webui + " {\n")
	// USER-FACING
	b.WriteString("\t# The Webui's nginx, published on the host as 8081: it serves the\n")
	// USER-FACING
	b.WriteString("\t# static pages and proxies /api/ to the API; Caddy terminates TLS.\n")
	// USER-FACING
	b.WriteString("\treverse_proxy " + UpstreamWebui + "\n")
	// USER-FACING
	b.WriteString("}\n")
	if a.Jellyfin {
		// USER-FACING
		b.WriteString("\n" + jellyfin + " {\n")
		// USER-FACING
		b.WriteString("\t# Jellyfin, published on the host as 8096. Dashboard, playback and\n")
		// USER-FACING
		b.WriteString("\t# the websocket-based activity feed all go through this one reverse\n")
		// USER-FACING
		b.WriteString("\t# proxy; websockets are handled natively, no extra directives.\n")
		// USER-FACING
		b.WriteString("\treverse_proxy " + UpstreamJellyfin + "\n")
		// USER-FACING
		b.WriteString("}\n")
	}
	return b.String()
}

// MainCaddyfileContent renders the content of a main Caddyfile the wizard
// creates itself (none existed): the global options block carrying the
// optional ACME email, then the import line. Only ever written when the
// file does not exist — an operator-owned Caddyfile is never overwritten,
// only appended to with consent.
func MainCaddyfileContent(email, importLine string) string {
	var b strings.Builder
	b.WriteString("{\n")
	if email != "" {
		b.WriteString("\temail " + email + "\n")
	}
	b.WriteString("}\n\n")
	b.WriteString(importLine + "\n")
	return b.String()
}

// HasSnippetImport reports whether a main Caddyfile already imports the
// snippet directory (the exact line, the wildcard form, or a single file
// from the directory all count). Comment lines do not.
func HasSnippetImport(content, snippetPath string) bool {
	dir := dirOf(snippetPath)
	for _, line := range strings.Split(content, "\n") {
		t := strings.TrimSpace(line)
		if strings.HasPrefix(t, "import") && strings.Contains(t, dir) {
			return true
		}
	}
	return false
}

// WithImportAdded returns the content of an existing main Caddyfile with
// the import line appended — the only edit the wizard ever makes to an
// operator-owned file, and only with consent.
func WithImportAdded(content, importLine string) string {
	if strings.TrimSpace(content) == "" {
		return importLine + "\n"
	}
	if !strings.HasSuffix(content, "\n") {
		content += "\n"
	}
	return content + "\n" + importLine + "\n"
}

// Diff renders a simple line diff of next against old: existing lines as
// context, appended lines with a leading "+ ". WithImportAdded only ever
// appends, so this exact rendering covers the wizard's one edit.
func Diff(old, next string) []string {
	oldLines := splitContentLines(old)
	nextLines := splitContentLines(next)
	var out []string
	for _, l := range oldLines {
		out = append(out, "  "+l)
	}
	for i, l := range nextLines {
		if i >= len(oldLines) {
			out = append(out, "+ "+l)
		}
	}
	return out
}

// splitContentLines splits into lines, dropping the empty artifact a
// trailing newline produces (real blank lines inside the content stay).
func splitContentLines(s string) []string {
	lines := strings.Split(s, "\n")
	if len(lines) > 0 && lines[len(lines)-1] == "" {
		lines = lines[:len(lines)-1]
	}
	return lines
}
