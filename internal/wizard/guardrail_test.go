package wizard

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// TestGuardrailProblemsHealthy covers the pass case: every required key
// present and no placeholder anywhere.
func TestGuardrailProblemsHealthy(t *testing.T) {
	e := Env{
		"VPN_PROVIDER":      "nordvpn",
		"OPENVPN_USER":      "u",
		"OPENVPN_PASSWORD":  "p",
		"TZ":                "UTC",
		"SERVICE_TOKEN":     "tok",
		"ADMIN_USER":        "admin",
		"ADMIN_PASSWORD":    "pw",
		"DEFAULT_QUOTA_GB":  "50",
		"SESSION_TTL_HOURS": "24",
		"BASE_DOWNLOAD_DIR": "/media",
		"DATABASE_PATH":     "/data/bitbeard.db",
	}
	if probs := GuardrailProblems(e); len(probs) != 0 {
		t.Errorf("GuardrailProblems(%v) = %v, want none", e, probs)
	}
}

// TestGuardrailProblemsRequiredKeys covers missing and empty required keys.
func TestGuardrailProblemsRequiredKeys(t *testing.T) {
	e := Env{"VPN_PROVIDER": "nordvpn", "OPENVPN_USER": "u", "OPENVPN_PASSWORD": "p"}
	probs := GuardrailProblems(e)
	joined := strings.Join(probs, "; ")
	// VPN_PROVIDER itself is a required key: here it is set, so it is the
	// others that must be named missing.
	for _, k := range []string{"TZ", "SERVICE_TOKEN", "ADMIN_PASSWORD", "DEFAULT_QUOTA_GB", "SESSION_TTL_HOURS", "BASE_DOWNLOAD_DIR", "DATABASE_PATH"} {
		if !strings.Contains(joined, k+" is missing") {
			t.Errorf("GuardrailProblems did not name %s as missing: %v", k, probs)
		}
	}
	if strings.Contains(joined, "VPN_PROVIDER is missing") {
		t.Errorf("GuardrailProblems wrongly reports the set VPN_PROVIDER: %v", probs)
	}

	e2 := Env{"VPN_PROVIDER": "nordvpn", "OPENVPN_USER": "u", "OPENVPN_PASSWORD": "p", "TZ": "", "ADMIN_PASSWORD": ""}
	probs = GuardrailProblems(e2)
	joined = strings.Join(probs, "; ")
	if !strings.Contains(joined, "TZ is empty") || !strings.Contains(joined, "ADMIN_PASSWORD is empty") {
		t.Errorf("GuardrailProblems did not name the empty keys: %v", probs)
	}
}

// TestGuardrailProblemsVPNCreds mirrors Problems(): the OpenVPN credentials
// are required unless VPN_PROVIDER is custom.
func TestGuardrailProblemsVPNCreds(t *testing.T) {
	e := Env{"VPN_PROVIDER": "nordvpn"}
	probs := GuardrailProblems(e)
	joined := strings.Join(probs, "; ")
	if !strings.Contains(joined, "OPENVPN_USER is missing") || !strings.Contains(joined, "OPENVPN_PASSWORD is missing") {
		t.Errorf("credentials should be required for a VPN provider: %v", probs)
	}

	// The custom provider carries its credentials in the .ovpn file.
	custom := Env{"VPN_PROVIDER": "Custom"}
	probs = GuardrailProblems(custom)
	joined = strings.Join(probs, "; ")
	if strings.Contains(joined, "OPENVPN_USER") || strings.Contains(joined, "OPENVPN_PASSWORD") {
		t.Errorf("credentials should not be required for VPN_PROVIDER=custom: %v", probs)
	}
}

// TestGuardrailProblemsChangeMe covers placeholders, including in values
// outside the required set, and case-insensitivity.
func TestGuardrailProblemsChangeMe(t *testing.T) {
	e := Env{
		"VPN_PROVIDER":      "nordvpn",
		"OPENVPN_USER":      "u",
		"OPENVPN_PASSWORD":  "p",
		"TZ":                "UTC",
		"SERVICE_TOKEN":     "change-me-to-a-long-random-token",
		"ADMIN_PASSWORD":    "Change-Me",
		"DEFAULT_QUOTA_GB":  "50",
		"SESSION_TTL_HOURS": "24",
		"BASE_DOWNLOAD_DIR": "/media",
		"JACKETT_API_KEY":   "CHANGE-ME-please",
	}
	probs := GuardrailProblems(e)
	joined := strings.Join(probs, "; ")
	for _, k := range []string{"SERVICE_TOKEN", "ADMIN_PASSWORD", "JACKETT_API_KEY"} {
		if !strings.Contains(joined, "Change "+k) {
			t.Errorf("GuardrailProblems did not flag %s: %v", k, probs)
		}
	}
}

// TestEnvIsIgnored covers the check-only .gitignore probe.
func TestEnvIsIgnored(t *testing.T) {
	dir := t.TempDir()
	if EnvIsIgnored(dir) {
		t.Error("no .gitignore at all should read as not ignored")
	}
	if err := os.WriteFile(filepath.Join(dir, ".gitignore"), []byte("# config\n/.env\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	if !EnvIsIgnored(dir) {
		t.Error("/.env in .gitignore should read as ignored")
	}
	if err := os.WriteFile(filepath.Join(dir, ".gitignore"), []byte("node_modules\n# /env\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	if EnvIsIgnored(dir) {
		t.Error("a .gitignore without an .env entry should read as not ignored")
	}
}
