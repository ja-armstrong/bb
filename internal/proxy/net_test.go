package proxy

// net_test.go pins the verdicts drawn from the network boundaries: the
// warn-only DNS pre-flight table and the PUBLIC_HTTPS spelling table.
// Everything runs against fakes — no resolver, no HTTP, no dials.

import (
	"errors"
	"fmt"
	"strings"
	"testing"
)

// TestJudgeDNS covers the pre-flight table: offline skip, unresolved
// names, mismatched addresses, the unknown-public-IP note, and the all-
// matched pass. No verdict ever blocks setup.
func TestJudgeDNS(t *testing.T) {
	errLookup := errors.New("no such host")
	errIP := errors.New("dial tcp: offline")

	cases := []struct {
		name     string
		findings []hostFinding
		pub      string
		pubErr   error
		offline  bool
		matched  bool
		warnFor  []string // hosts whose warnings must appear
		note     bool     // the public-IP note must appear
	}{
		{
			name: "all matched",
			findings: []hostFinding{
				{host: "bitbeard.example.com", ips: []string{"203.0.113.7"}},
				{host: "jellyfin.example.com", ips: []string{"203.0.113.7"}},
			},
			pub:     "203.0.113.7",
			matched: true,
		},
		{
			name: "one name does not resolve",
			findings: []hostFinding{
				{host: "bitbeard.example.com", ips: []string{"203.0.113.7"}},
				{host: "jellyfin.example.com", err: errLookup},
			},
			pub:     "203.0.113.7",
			warnFor: []string{"jellyfin.example.com does not resolve"},
		},
		{
			name: "resolved somewhere else",
			findings: []hostFinding{
				{host: "bitbeard.example.com", ips: []string{"198.51.100.9"}},
			},
			pub:     "203.0.113.7",
			warnFor: []string{"bitbeard.example.com resolves to 198.51.100.9"},
		},
		{
			name: "public IP unknown, names resolved",
			findings: []hostFinding{
				{host: "bitbeard.example.com", ips: []string{"203.0.113.7"}},
			},
			pubErr: errIP,
			note:   true,
		},
		{
			name: "offline: lookups and IP both fail",
			findings: []hostFinding{
				{host: "bitbeard.example.com", err: errLookup},
				{host: "jellyfin.example.com", err: errLookup},
			},
			pubErr:  errIP,
			offline: true,
		},
		{
			name: "lookup fails but IP service answers: not offline",
			findings: []hostFinding{
				{host: "bitbeard.example.com", err: errLookup},
			},
			pub:     "203.0.113.7",
			warnFor: []string{"bitbeard.example.com does not resolve"},
		},
	}
	for _, tc := range cases {
		v := judgeDNS(tc.findings, tc.pub, tc.pubErr)
		if v.offline != tc.offline {
			t.Errorf("%s: offline = %v, want %v (verdict %+v)", tc.name, v.offline, tc.offline, v)
		}
		if v.matched != tc.matched {
			t.Errorf("%s: matched = %v, want %v", tc.name, v.matched, tc.matched)
		}
		if tc.offline {
			continue
		}
		for _, host := range tc.warnFor {
			if !containsWarning(v.warnings, host) {
				t.Errorf("%s: warnings %q lack %q", tc.name, v.warnings, host)
			}
		}
		if tc.note && len(v.notes) == 0 {
			t.Errorf("%s: want the public-IP note", tc.name)
		}
	}
}

// containsWarning reports whether any warning contains the fragment.
func containsWarning(warnings []string, fragment string) bool {
	for _, w := range warnings {
		if strings.Contains(w, fragment) {
			return true
		}
	}
	return false
}

// TestJudgeDNSOfflineNoteIsPlain keeps the offline verdict free of
// alarming warnings: the note path in preflightDNS prints a skip line,
// which the test mirrors by asserting no warnings are attached.
func TestJudgeDNSOfflineNoteIsPlain(t *testing.T) {
	v := judgeDNS([]hostFinding{{host: "bitbeard.example.com", err: fmt.Errorf("offline")}}, "", errors.New("offline"))
	if !v.offline || len(v.warnings) != 0 {
		t.Errorf("offline verdict = %+v, want offline with no warnings", v)
	}
}

// TestPublicHTTPSDisabled covers the explicit-off spellings; unset and
// unknown values count as the default (on).
func TestPublicHTTPSDisabled(t *testing.T) {
	off := []string{"0", "false", "no", "off", "FALSE", " No "}
	for _, v := range off {
		if !PublicHTTPSDisabled(v) {
			t.Errorf("PublicHTTPSDisabled(%q) = false, want true", v)
		}
	}
	on := []string{"", "1", "true", "yes", "on", "whatever"}
	for _, v := range on {
		if PublicHTTPSDisabled(v) {
			t.Errorf("PublicHTTPSDisabled(%q) = true, want false", v)
		}
	}
}
