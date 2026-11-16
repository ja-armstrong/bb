package wizard

import (
	"bytes"
	"os"
	"path/filepath"
	"reflect"
	"regexp"
	"runtime"
	"strings"
	"testing"
)

// envKeyLine matches KEY=value lines of a rendered .env.
var envKeyLine = regexp.MustCompile(`^([A-Z_]+)=(.*)$`)

// parseRenderedEnv extracts keys in file order from rendered output.
func parseRenderedEnv(t *testing.T, rendered string) ([]string, map[string]string) {
	t.Helper()
	var keys []string
	vals := map[string]string{}
	for _, line := range strings.Split(rendered, "\n") {
		if m := envKeyLine.FindStringSubmatch(line); m != nil {
			keys = append(keys, m[1])
			vals[m[1]] = m[2]
		}
	}
	return keys, vals
}

func TestRenderEnvExactKeySetAndOrder(t *testing.T) {
	e := mergeEnv(fallbackEnv, Env{
		"VPN_PROVIDER":      "nordvpn",
		"OPENVPN_USER":      "u",
		"OPENVPN_PASSWORD":  "p",
		"TZ":                "UTC",
		"SERVICE_TOKEN":     "tok",
		"ADMIN_PASSWORD":    "pw",
		"DEFAULT_QUOTA_GB":  "50",
		"SESSION_TTL_HOURS": "24",
		"BASE_DOWNLOAD_DIR": "/media",
	})
	e["COMPOSE_PROFILES"] = ""
	keys, vals := parseRenderedEnv(t, renderEnv(e))

	if !reflect.DeepEqual(keys, envKeys) {
		t.Errorf("rendered keys %v, want exactly envKeys %v", keys, envKeys)
	}
	for _, k := range requiredKeys {
		if vals[k] == "" {
			t.Errorf("rendered .env is missing %s", k)
		}
	}
	if vals["COMPOSE_PROFILES"] != "" {
		t.Errorf("COMPOSE_PROFILES = %q, want empty (mandatory-only stack)", vals["COMPOSE_PROFILES"])
	}
}

func TestRenderEnvQuotesValuesWithSpaces(t *testing.T) {
	_, vals := parseRenderedEnv(t, renderEnv(Env{"BASE_DOWNLOAD_DIR": "/media/my movies"}))
	if got := vals["BASE_DOWNLOAD_DIR"]; got != `"/media/my movies"` {
		t.Errorf("BASE_DOWNLOAD_DIR = %s, want it quoted", got)
	}
}

func TestParseEnvRoundTrip(t *testing.T) {
	in := "# comment\n\nA=1\n  B = two words \nexport C=\"quoted\"\nD='single'\n"
	e, err := ParseEnv(in)
	if err != nil {
		t.Fatalf("ParseEnv: %v", err)
	}
	want := Env{"A": "1", "B": "two words", "C": "quoted", "D": "single"}
	if !reflect.DeepEqual(e, want) {
		t.Errorf("ParseEnv = %v, want %v", e, want)
	}
}

func TestParseEnvRejectsMalformed(t *testing.T) {
	for _, in := range []string{"NOEQUALS\n", "=novar\n", "OK=1\nbroken\n"} {
		if _, err := ParseEnv(in); err == nil {
			t.Errorf("ParseEnv(%q) = nil error, want one", in)
		}
	}
}

func TestWriteEnvFileRoundTripIsValid(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, ".env")
	e := mergeEnv(fallbackEnv, Env{
		"VPN_PROVIDER":          "custom",
		"OPENVPN_CUSTOM_CONFIG": customConfigMountPath,
		"TZ":                    "UTC",
		"SERVICE_TOKEN":         "tok",
		"ADMIN_PASSWORD":        "pw",
		"DEFAULT_QUOTA_GB":      "50",
		"SESSION_TTL_HOURS":     "24",
		"BASE_DOWNLOAD_DIR":     "/media",
		"COMPOSE_PROFILES":      "",
	})
	if err := writeEnvFile(path, e); err != nil {
		t.Fatalf("writeEnvFile: %v", err)
	}
	// Owner-only permissions: the file holds credentials. Windows only
	// carries a read-only bit, so the exact mode is checked elsewhere.
	if runtime.GOOS != "windows" {
		fi, err := os.Stat(path)
		if err != nil {
			t.Fatalf("stat: %v", err)
		}
		if fi.Mode().Perm() != 0o600 {
			t.Errorf(".env mode = %o, want 600", fi.Mode().Perm())
		}
	}
	data, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("read: %v", err)
	}
	parsed, err := ParseEnv(string(data))
	if err != nil {
		t.Fatalf("re-parse: %v", err)
	}
	if probs := parsed.Problems(); len(probs) > 0 {
		t.Errorf("written .env has problems: %v", probs)
	}
	if parsed["COMPOSE_PROFILES"] != "" {
		t.Errorf("COMPOSE_PROFILES = %q, want empty", parsed["COMPOSE_PROFILES"])
	}
}

func TestEnvProblemsValid(t *testing.T) {
	e := mergeEnv(fallbackEnv, Env{
		"VPN_PROVIDER":      "nordvpn",
		"OPENVPN_USER":      "u",
		"OPENVPN_PASSWORD":  "p",
		"TZ":                "Europe/Berlin",
		"SERVICE_TOKEN":     "tok",
		"ADMIN_PASSWORD":    "pw",
		"DEFAULT_QUOTA_GB":  "50",
		"SESSION_TTL_HOURS": "24",
		"BASE_DOWNLOAD_DIR": "/media",
		"DATABASE_PATH":     "/data/bitbeard.db",
		"ADMIN_USER":        "admin",
		"COMPOSE_PROFILES":  "",
	})
	if probs := e.Problems(); len(probs) > 0 {
		t.Errorf("valid .env reported problems: %v", probs)
	}
}

func TestEnvProblemsDetectsAllCases(t *testing.T) {
	base := func() Env {
		return Env{
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
	}
	tests := []struct {
		name   string
		mutate func(Env)
		want   string // substring of the expected problem
	}{
		{"missing key", func(e Env) { delete(e, "TZ") }, "TZ is missing"},
		{"empty key", func(e Env) { e["TZ"] = "" }, "TZ is empty"},
		{"change-me token", func(e Env) { e["SERVICE_TOKEN"] = "change-me-to-a-long-random-token" }, "SERVICE_TOKEN"},
		{"change-me password", func(e Env) { e["ADMIN_PASSWORD"] = "change-me" }, "ADMIN_PASSWORD"},
		{"empty openvpn creds", func(e Env) { e["OPENVPN_USER"] = ""; e["OPENVPN_PASSWORD"] = "" }, "OPENVPN_USER is empty"},
		{"invalid provider", func(e Env) { e["VPN_PROVIDER"] = "acme" }, "VPN_PROVIDER"},
		{"bad quota", func(e Env) { e["DEFAULT_QUOTA_GB"] = "zero" }, "DEFAULT_QUOTA_GB"},
		{"bad ttl", func(e Env) { e["SESSION_TTL_HOURS"] = "0" }, "SESSION_TTL_HOURS"},
		{"bad tz", func(e Env) { e["TZ"] = "Berlin" }, "TZ"},
		{"relative base dir", func(e Env) { e["BASE_DOWNLOAD_DIR"] = "media" }, "BASE_DOWNLOAD_DIR"},
		{"empty creds ok with custom", func(e Env) {
			e["VPN_PROVIDER"] = "custom"
			e["OPENVPN_USER"] = ""
			e["OPENVPN_PASSWORD"] = ""
			e["OPENVPN_CUSTOM_CONFIG"] = customConfigMountPath
		}, ""},
		{"custom without a config", func(e Env) { e["VPN_PROVIDER"] = "custom"; e["OPENVPN_USER"] = ""; e["OPENVPN_PASSWORD"] = "" }, "OPENVPN_CUSTOM_CONFIG is empty"},
		{"custom with a relative config", func(e Env) {
			e["VPN_PROVIDER"] = "custom"
			e["OPENVPN_USER"] = ""
			e["OPENVPN_PASSWORD"] = ""
			e["OPENVPN_CUSTOM_CONFIG"] = "openvpn/custom.ovpn"
		}, "absolute path"},
		{"stale config with a known provider", func(e Env) { e["OPENVPN_CUSTOM_CONFIG"] = customConfigMountPath }, "OPENVPN_CUSTOM_CONFIG is set"},
		{"stale server countries with custom", func(e Env) {
			e["VPN_PROVIDER"] = "custom"
			e["OPENVPN_USER"] = ""
			e["OPENVPN_PASSWORD"] = ""
			e["OPENVPN_CUSTOM_CONFIG"] = customConfigMountPath
			e["SERVER_COUNTRIES"] = "Netherlands"
		}, "SERVER_COUNTRIES"},
	}
	for _, tt := range tests {
		e := base()
		tt.mutate(e)
		probs := e.Problems()
		if tt.want == "" {
			if len(probs) > 0 {
				t.Errorf("%s: unexpected problems %v", tt.name, probs)
			}
			continue
		}
		found := false
		for _, p := range probs {
			if strings.Contains(p, tt.want) {
				found = true
			}
		}
		if !found {
			t.Errorf("%s: problems %v do not mention %q", tt.name, probs, tt.want)
		}
	}
}

func TestIsChangeMe(t *testing.T) {
	for _, v := range []string{"change-me", "CHANGE-ME", "change-me-to-a-long-random-token", "xChange-Mex"} {
		if !IsChangeMe(v) {
			t.Errorf("IsChangeMe(%q) = false, want true", v)
		}
	}
	for _, v := range []string{"", "secret", "CHANGING-MINDS"} {
		if IsChangeMe(v) {
			t.Errorf("IsChangeMe(%q) = true, want false", v)
		}
	}
}

func TestEnsureEnvIgnoredAlreadyCovered(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, ".gitignore")
	if err := os.WriteFile(path, []byte("/.env\n/media\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	var out bytes.Buffer
	if err := EnsureEnvIgnored(dir, &out); err != nil {
		t.Fatalf("EnsureEnvIgnored: %v", err)
	}
	data, _ := os.ReadFile(path)
	if string(data) != "/.env\n/media\n" {
		t.Errorf("gitignore changed: %q", data)
	}
}

func TestEnsureEnvIgnoredAppends(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, ".gitignore")
	if err := os.WriteFile(path, []byte("media/"), 0o644); err != nil {
		t.Fatal(err)
	}
	var out bytes.Buffer
	if err := EnsureEnvIgnored(dir, &out); err != nil {
		t.Fatalf("EnsureEnvIgnored: %v", err)
	}
	data, _ := os.ReadFile(path)
	if !strings.Contains(string(data), "/.env") {
		t.Errorf("gitignore %q does not ignore .env after EnsureEnvIgnored", data)
	}
	// Also creates the file when there is none.
	dir2 := t.TempDir()
	if err := EnsureEnvIgnored(dir2, &out); err != nil {
		t.Fatalf("EnsureEnvIgnored: %v", err)
	}
	data2, _ := os.ReadFile(filepath.Join(dir2, ".gitignore"))
	if !strings.Contains(string(data2), "/.env") {
		t.Errorf("fresh gitignore = %q, want /.env", data2)
	}
}
