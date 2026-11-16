package wizard

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestValidateProvider(t *testing.T) {
	for _, in := range []string{"NordVPN", " NORDVPN ", "nordvpn", "Custom", "Private Internet Access"} {
		if _, err := ValidateProvider(in); err != nil {
			t.Errorf("ValidateProvider(%q): %v", in, err)
		}
	}
	for _, in := range []string{"", "acme", "nord vpn", "NordVPN!"} {
		if _, err := ValidateProvider(in); err == nil {
			t.Errorf("ValidateProvider(%q) = nil error, want one", in)
		}
	}
	// The hardcoded list matches gluetun's constants.
	for _, p := range VPNProviders {
		if p.ID != strings.ToLower(strings.TrimSpace(p.ID)) {
			t.Errorf("provider id %q is not lowercase/trimmed", p.ID)
		}
	}
	if !KnownVPNProvider(customProvider) || !KnownVPNProvider(nordvpnDefault) {
		t.Error("custom and nordvpn must both be known providers")
	}
}

func TestValidatePositiveInt(t *testing.T) {
	for _, v := range []string{"1", "50", " 24 "} {
		if _, err := ValidatePositiveInt("K")(v); err != nil {
			t.Errorf("ValidatePositiveInt(%q): %v", v, err)
		}
	}
	for _, v := range []string{"", "0", "-1", "1.5", "abc"} {
		if _, err := ValidatePositiveInt("K")(v); err == nil {
			t.Errorf("ValidatePositiveInt(%q) = nil error, want one", v)
		}
	}
}

func TestValidateTZ(t *testing.T) {
	for _, v := range []string{"UTC", "Etc/UTC", "Europe/Berlin", "America/New_York", " Europe/Berlin "} {
		if _, err := ValidateTZ(v); err != nil {
			t.Errorf("ValidateTZ(%q): %v", v, err)
		}
	}
	for _, v := range []string{"", "Berlin", "Europe/ Berlin", "CET"} {
		if _, err := ValidateTZ(v); err == nil {
			t.Errorf("ValidateTZ(%q) = nil error, want one", v)
		}
	}
}

func TestValidateBaseDir(t *testing.T) {
	if v, err := ValidateBaseDir(""); err != nil || v != "/media" {
		t.Errorf("ValidateBaseDir(\"\") = %q, %v; want /media, nil", v, err)
	}
	if _, err := ValidateBaseDir("/mnt/media/tv"); err != nil {
		t.Errorf("ValidateBaseDir(/mnt/media/tv): %v", err)
	}
	for _, v := range []string{"media", "media/tv", "/a/../b"} {
		if _, err := ValidateBaseDir(v); err == nil {
			t.Errorf("ValidateBaseDir(%q) = nil error, want one", v)
		}
	}
}

func TestValidateNonEmpty(t *testing.T) {
	if _, err := ValidateNonEmpty("K")("  hi "); err != nil {
		t.Errorf("ValidateNonEmpty(hi): %v", err)
	}
	for _, v := range []string{"", "   ", "change-me"} {
		if _, err := ValidateNonEmpty("K")(v); err == nil {
			t.Errorf("ValidateNonEmpty(%q) = nil error, want one", v)
		}
	}
}

func TestValidateOVPNPath(t *testing.T) {
	dir := t.TempDir()
	ovpn := filepath.Join(dir, "my-vpn.ovpn")
	if err := os.WriteFile(ovpn, []byte("client\ndev tun\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	got, err := ValidateOVPNPath(ovpn)
	if err != nil {
		t.Fatalf("ValidateOVPNPath: %v", err)
	}
	if got != filepath.Clean(ovpn) {
		t.Errorf("ValidateOVPNPath = %q, want %q", got, filepath.Clean(ovpn))
	}
	if _, err := ValidateOVPNPath(dir); err == nil {
		t.Error("directory accepted as .ovpn file")
	}
	if _, err := ValidateOVPNPath(filepath.Join(dir, "missing.ovpn")); err == nil {
		t.Error("missing file accepted")
	}
	txt := filepath.Join(dir, "wrong.txt")
	os.WriteFile(txt, []byte("x"), 0o644)
	if _, err := ValidateOVPNPath(txt); err == nil {
		t.Error(".txt accepted as .ovpn")
	}
	// Optional variant: empty means "keep what is there".
	if v, err := ValidateOptionalOVPNPath(""); err != nil || v != "" {
		t.Errorf("ValidateOptionalOVPNPath(\"\") = %q, %v; want \"\", nil", v, err)
	}
	if v, err := ValidateOptionalOVPNPath(ovpn); err != nil || v == "" {
		t.Errorf("ValidateOptionalOVPNPath(ovpn) = %q, %v", v, err)
	}
}

func TestRandomHexDeterministicAndLength(t *testing.T) {
	src := strings.NewReader("0123456789abcdef0123456789abcdef0123456789abcdef")
	a, err := randomHex(src, 24)
	if err != nil {
		t.Fatalf("randomHex: %v", err)
	}
	if len(a) != 48 {
		t.Errorf("randomHex(24) len = %d, want 48", len(a))
	}
	b, _ := randomHex(strings.NewReader("0123456789abcdef0123456789abcdef0123456789abcdef"), 24)
	if a != b {
		t.Errorf("randomHex not deterministic with same source: %q vs %q", a, b)
	}
	if _, err := randomHex(strings.NewReader("short"), 24); err == nil {
		t.Error("randomHex succeeded despite exhausted source")
	}
}
