package wizard

// env.go owns the .env file the wizard writes and re-reads: the exact key
// set and order, generation from seeded defaults plus prompted values,
// parsing for rerun detection, and validation of an existing file.

import (
	"bufio"
	"errors"
	"fmt"
	"io"
	"io/fs"
	"os"
	"path/filepath"
	"sort"
	"strings"
)

// Env is a set of .env key/value pairs.
type Env map[string]string

// envSection is one commented block of the generated .env.
type envSection struct {
	title string
	keys  []string
}

// envSections is every key the wizard's .env carries, in write order,
// grouped as in bitbeard/.env.example. The full set is written so the stack
// behaves like the documented manual install (a copy of .env.example).
var envSections = []envSection{
	// USER-FACING
	{"VPN / network", []string{"OPENVPN_USER", "OPENVPN_PASSWORD", "VPN_PROVIDER", "TZ"}},
	// USER-FACING
	{"Media servers", []string{"PLEX_CLAIM"}},
	// USER-FACING
	{"Indexers and the torrent client (owned by the Download Manager)", []string{"JACKETT_API_KEY", "QBITTORRENT_USERNAME", "QBITTORRENT_PASSWORD", "BASE_DOWNLOAD_DIR"}},
	// USER-FACING
	{"Identity and service-to-service auth", []string{"SERVICE_TOKEN", "ADMIN_USER", "ADMIN_PASSWORD", "DEFAULT_QUOTA_GB", "SESSION_TTL_HOURS", "DATABASE_PATH"}},
	// USER-FACING
	{"Bot Frontend", []string{"DISCORD_BOT_TOKEN", "DISCORD_ANNOUNCE_CHANNEL_ID", "API_URL"}},
	// USER-FACING
	{"Internal hops (all Subsystems share gluetun's netns, so localhost is right)", []string{"MANAGER_URL", "BOT_URL"}},
	// USER-FACING
	{"Optional Subsystems", []string{"COMPOSE_PROFILES"}},
}

// envKeys is the flat, ordered key list of the generated .env.
var envKeys = flattenSections(envSections)

// requiredKeys must all be present and non-empty for an existing .env to
// count as valid on rerun. OPENVPN_USER/OPENVPN_PASSWORD are validated
// separately (they may be empty only when VPN_PROVIDER=custom).
var requiredKeys = []string{
	"VPN_PROVIDER", "TZ", "SERVICE_TOKEN", "ADMIN_USER", "ADMIN_PASSWORD",
	"DEFAULT_QUOTA_GB", "SESSION_TTL_HOURS", "BASE_DOWNLOAD_DIR", "DATABASE_PATH",
}

// openvpnCredsKeys are required unless the custom .ovpn path is chosen.
var openvpnCredsKeys = []string{"OPENVPN_USER", "OPENVPN_PASSWORD"}

// keyComments carry the .env.example explanations into the generated file.
var keyComments = map[string]string{
	// USER-FACING
	"SERVICE_TOKEN": "# Shared bearer token for API <-> Download Manager and manager -> Bot webhook.",
	// USER-FACING
	"ADMIN_PASSWORD": "# Bootstrap Admin seeded by the API on first boot; log in with ADMIN_USER and this.",
	// USER-FACING
	"DEFAULT_QUOTA_GB": "# Global default Storage Quota in GB; Admins are exempt.",
	// USER-FACING
	"SESSION_TTL_HOURS": "# How long a login Session lasts before the API refuses it (hours).",
	// USER-FACING
	"DATABASE_PATH": "# Single SQLite database on the shared data volume.",
	// USER-FACING
	"COMPOSE_PROFILES": "# Which Optional Subsystems to boot, space-separated (bot, plex, jellyfin).\n# Managed by `bitbeard setup` and the Admin Panel; empty boots only the\n# mandatory Subsystems.",
	// USER-FACING
	"PLEX_CLAIM": "# Optional claim token for the Plex Subsystem. `bitbeard setup` never asks\n# for one — claim tokens expire within minutes — claim the server at\n# http://localhost:32400/web after boot instead.",
}

// fallbackEnv is used for keys the wizard does not prompt for when the
// stack's own .env.example cannot be read. It mirrors .env.example.
var fallbackEnv = Env{
	"JACKETT_API_KEY":             "ffgvybxouy2czbjnbem8vir00y4lyvsj",
	"QBITTORRENT_USERNAME":        "admin",
	"QBITTORRENT_PASSWORD":        "adminadmin",
	"ADMIN_USER":                  "admin",
	"DATABASE_PATH":               "/data/bitbeard.db",
	"API_URL":                     "http://127.0.0.1:9870",
	"MANAGER_URL":                 "http://127.0.0.1:9871",
	"BOT_URL":                     "http://127.0.0.1:9872",
	"DISCORD_BOT_TOKEN":           "",
	"DISCORD_ANNOUNCE_CHANNEL_ID": "",
	"PLEX_CLAIM":                  "",
}

// renderEnv renders e as the full .env contents: every key of envKeys in
// order, seeded defaults for the keys the wizard did not collect, and
// COMPOSE_PROFILES from the Optional Subsystems stage (empty means the
// mandatory-only stack).
func renderEnv(e Env) string {
	var b strings.Builder
	// USER-FACING
	b.WriteString("# Bitbeard stack configuration, written by `bitbeard setup`.\n")
	// USER-FACING
	b.WriteString("# Plaintext on purpose: docker compose reads this file. It is gitignored\n# and holds credentials — keep it that way.\n\n")
	for _, s := range envSections {
		// USER-FACING
		b.WriteString("# " + s.title + "\n")
		for _, k := range s.keys {
			if c, ok := keyComments[k]; ok {
				b.WriteString(c + "\n")
			}
			b.WriteString(k + "=" + quoteValue(e[k]) + "\n")
		}
		b.WriteString("\n")
	}
	return b.String()
}

// writeEnvFile writes e to path with owner-only permissions.
func writeEnvFile(path string, e Env) error {
	return os.WriteFile(path, []byte(renderEnv(e)), 0o600)
}

// quoteValue quotes a value for the .env file when it contains characters
// docker compose's parser treats specially.
func quoteValue(v string) string {
	if strings.ContainsAny(v, " \t#\"'$") {
		return `"` + strings.ReplaceAll(v, `"`, `\"`) + `"`
	}
	return v
}

// ParseEnv reads a .env-style file. Blank lines and # comments are skipped;
// every other line must be KEY=VALUE. Surrounding quotes are stripped.
func ParseEnv(data string) (Env, error) {
	e := Env{}
	sc := bufio.NewScanner(strings.NewReader(data))
	n := 0
	for sc.Scan() {
		n++
		line := strings.TrimSpace(sc.Text())
		if line == "" || strings.HasPrefix(line, "#") {
			continue
		}
		line = strings.TrimPrefix(line, "export ")
		i := strings.Index(line, "=")
		if i <= 0 {
			// USER-FACING
			return nil, fmt.Errorf("line %d is not KEY=value: %q", n, line)
		}
		k := strings.TrimSpace(line[:i])
		v := strings.TrimSpace(line[i+1:])
		e[k] = unquote(v)
	}
	if err := sc.Err(); err != nil {
		return nil, err
	}
	return e, nil
}

// unquote strips one layer of matching surrounding quotes.
func unquote(v string) string {
	if len(v) >= 2 {
		if (v[0] == '"' && v[len(v)-1] == '"') || (v[0] == '\'' && v[len(v)-1] == '\'') {
			return v[1 : len(v)-1]
		}
	}
	return v
}

// Problems reports every reason e is not a valid .env for the mandatory
// stack. An empty result means the file is fine and the wizard may skip
// prompting on rerun.
func (e Env) Problems() []string {
	var probs []string

	// Required keys: present, non-empty, no change-me.
	for _, k := range requiredKeys {
		v, ok := e[k]
		switch {
		case !ok:
			// USER-FACING
			probs = append(probs, fmt.Sprintf("%s is missing", k))
		case v == "":
			// USER-FACING
			probs = append(probs, fmt.Sprintf("%s is empty", k))
		case IsChangeMe(v):
			// USER-FACING
			probs = append(probs, fmt.Sprintf("%s still carries its change-me placeholder; set a real value", k))
		}
	}

	// VPN provider: must name a provider gluetun supports (or custom).
	prov := normalizeProvider(e["VPN_PROVIDER"])
	if prov == "" {
		// USER-FACING
		probs = append(probs, `VPN_PROVIDER is empty; expected one of gluetun's supported providers or "custom"`)
	} else if !KnownVPNProvider(prov) {
		// USER-FACING
		probs = append(probs, fmt.Sprintf("VPN_PROVIDER is %q; expected one of gluetun's supported providers or %q", e["VPN_PROVIDER"], customProvider))
	}

	// OpenVPN credentials: required unless the custom .ovpn path is chosen.
	if prov != customProvider {
		for _, k := range openvpnCredsKeys {
			v, ok := e[k]
			switch {
			case !ok:
				// USER-FACING
				probs = append(probs, fmt.Sprintf("%s is missing; it is required unless VPN_PROVIDER=%s", k, customProvider))
			case v == "":
				// USER-FACING
				probs = append(probs, fmt.Sprintf("%s is empty; it is required unless VPN_PROVIDER=%s", k, customProvider))
			case IsChangeMe(v):
				// USER-FACING
				probs = append(probs, fmt.Sprintf("%s still carries its change-me placeholder; set a real value", k))
			}
		}
	}

	// Counts.
	for _, k := range []string{"DEFAULT_QUOTA_GB", "SESSION_TTL_HOURS"} {
		if v := e[k]; v != "" && !IsPositiveInt(v) {
			// USER-FACING
			probs = append(probs, fmt.Sprintf("%s is %q; expected a whole number greater than zero", k, v))
		}
	}

	// Timezone.
	if v := e["TZ"]; v != "" && !ValidTZ(v) {
		// USER-FACING
		probs = append(probs, fmt.Sprintf("TZ is %q; expected an IANA timezone like Europe/Berlin or UTC", v))
	}

	// Media Root: absolute path.
	if v := e["BASE_DOWNLOAD_DIR"]; v != "" && !ValidAbsPath(v) {
		// USER-FACING
		probs = append(probs, fmt.Sprintf("BASE_DOWNLOAD_DIR is %q; expected an absolute path starting with /", v))
	}

	return probs
}

// IsChangeMe reports whether v is (or contains) a change-me placeholder,
// case-insensitive. The wizard never accepts such a value.
func IsChangeMe(v string) bool {
	return strings.Contains(strings.ToLower(v), "change-me")
}

// GuardrailProblems reports how e fails the .env guardrails the Doctor
// checks: a required key missing or empty (the OpenVPN credentials only when
// VPN_PROVIDER needs them), or a change-me placeholder still sitting in any
// value. It is check-only: it touches nothing but the map it is given. The
// wizard enforces the same rules when it writes .env; the Doctor re-checks
// the file that is actually on disk.
func GuardrailProblems(e Env) []string {
	var probs []string

	req := append([]string(nil), requiredKeys...)
	if normalizeProvider(e["VPN_PROVIDER"]) != customProvider {
		req = append(req, openvpnCredsKeys...)
	}
	required := make(map[string]bool, len(req))
	for _, k := range req {
		required[k] = true
		v, ok := e[k]
		switch {
		case !ok:
			// USER-FACING
			probs = append(probs, fmt.Sprintf("%s is missing", k))
		case v == "":
			// USER-FACING
			probs = append(probs, fmt.Sprintf("%s is empty", k))
		case IsChangeMe(v):
			// USER-FACING
			probs = append(probs, fmt.Sprintf("%s still carries its change-me placeholder; set a real value", k))
		}
	}

	// Any other value carrying a placeholder is a guardrail failure too.
	keys := make([]string, 0, len(e))
	for k := range e {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	for _, k := range keys {
		if !required[k] && IsChangeMe(e[k]) {
			// USER-FACING
			probs = append(probs, fmt.Sprintf("%s still carries its change-me placeholder; set a real value", k))
		}
	}
	return probs
}

// EnvIsIgnored reports whether dir/.gitignore ignores .env. Check-only:
// unlike EnsureEnvIgnored it never writes anything, so the Doctor can use it.
func EnvIsIgnored(dir string) bool {
	path := filepath.Join(dir, ".gitignore")
	data, err := os.ReadFile(path)
	if err != nil {
		return false
	}
	for _, line := range strings.Split(string(data), "\n") {
		t := strings.TrimSpace(line)
		if t != "" && !strings.HasPrefix(t, "#") && strings.Contains(t, ".env") {
			return true
		}
	}
	return false
}

// EnsureEnvIgnored verifies dir/.gitignore ignores .env, appending a line
// when it does not, and notes what it did. The credentials file must never
// end up in git.
func EnsureEnvIgnored(dir string, out io.Writer) error {
	if EnvIsIgnored(dir) {
		return nil
	}
	path := filepath.Join(dir, ".gitignore")
	data, err := os.ReadFile(path)
	if err != nil && !errors.Is(err, fs.ErrNotExist) {
		return err
	}
	f, err := os.OpenFile(path, os.O_APPEND|os.O_CREATE|os.O_WRONLY, 0o644)
	if err != nil {
		return err
	}
	defer f.Close()
	if len(data) > 0 && !strings.HasSuffix(string(data), "\n") {
		if _, err := f.WriteString("\n"); err != nil {
			return err
		}
	}
	if _, err := f.WriteString("/.env\n"); err != nil {
		return err
	}
	// USER-FACING
	fmt.Fprintf(out, "Added /.env to %s so the credentials file stays out of git.\n", path)
	return nil
}

// flattenSections returns every key of sections in order.
func flattenSections(sections []envSection) []string {
	var keys []string
	for _, s := range sections {
		keys = append(keys, s.keys...)
	}
	return keys
}
