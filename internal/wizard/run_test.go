package wizard

import (
	"bytes"
	"encoding/hex"
	"errors"
	"io"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// fakeCompose records Up calls and serves canned ps output.
type fakeCompose struct {
	upDirs []string
	upErr  error
	psOut  string
	psErr  error
}

func (f *fakeCompose) Up(dir string) error {
	f.upDirs = append(f.upDirs, dir)
	return f.upErr
}

func (f *fakeCompose) PS(string) (string, error) { return f.psOut, f.psErr }

// randSeed is a deterministic 48-byte randomness source; tests compute the
// expected generated secrets from it.
const randSeed = "0123456789abcdef0123456789abcdef0123456789abcdef"

// newRand returns a fresh randomness source with randSeed's bytes.
func newRand() io.Reader { return strings.NewReader(randSeed) }

// expectedToken returns what randomHex(newRand(), 24) and
// randomHex(newRand(), 16) produce for randSeed, without consuming input.
func expectedToken(t *testing.T) (token, adminPass string) {
	t.Helper()
	return hex.EncodeToString([]byte(randSeed[:24])), hex.EncodeToString([]byte(randSeed[24:40]))
}

// makeStackDir builds a stack directory shaped like the cloned repository:
// docker-compose.yml, .env.example, .gitignore and the bundled NordVPN
// config under bitbeard/.
func makeStackDir(t *testing.T) string {
	t.Helper()
	root := t.TempDir()
	stack := filepath.Join(root, "bitbeard")
	if err := os.MkdirAll(filepath.Join(stack, "openvpn"), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(stack, "docker-compose.yml"), []byte("services: {}\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(stack, ".env.example"), []byte(fallbackEnvExample), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(stack, ".gitignore"), []byte("/.env\n/media\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(stack, "openvpn", "custom.ovpn"), []byte("client\nremote 1.2.3.4 1194\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	return root
}

// fallbackEnvExample mirrors the real .env.example's parseable values.
const fallbackEnvExample = "JACKETT_API_KEY=examplejackettkey\nQBITTORRENT_USERNAME=admin\nQBITTORRENT_PASSWORD=adminadmin\nADMIN_USER=admin\nDATABASE_PATH=/data/bitbeard.db\nAPI_URL=http://127.0.0.1:9870\nMANAGER_URL=http://127.0.0.1:9871\nBOT_URL=http://127.0.0.1:9872\n"

// runWizard runs Run against a stack dir with scripted input. discord is
// the DiscordChecker (nil means the real API — pass a fake whenever a bot
// token is entered).
func runWizard(t *testing.T, stackDir, input string, comp *fakeCompose, discord DiscordChecker) (string, Result, error) {
	t.Helper()
	var out bytes.Buffer
	res, err := Run(Options{
		StackDir: stackDir,
		In:       strings.NewReader(input),
		Out:      &out,
		Compose:  comp,
		Rand:     newRand(),
		Discord:  discord,
	})
	return out.String(), res, err
}

// healthyPS is ps output for the mandatory stack, all running.
var healthyPS = "" +
	`{"Name":"gluetun","State":"running","ExitCode":0}` + "\n" +
	`{"Name":"qbittorrent","State":"running","ExitCode":0}` + "\n" +
	`{"Name":"jackett","State":"running","ExitCode":0}` + "\n" +
	`{"Name":"api","State":"running","ExitCode":0}` + "\n" +
	`{"Name":"manager","State":"running","ExitCode":0}` + "\n" +
	`{"Name":"webui","State":"running","ExitCode":0}` + "\n" +
	`{"Name":"init-configs","State":"exited","ExitCode":0}` + "\n"

// newFakeCompose returns a fake compose reporting a healthy stack.
func newFakeCompose() *fakeCompose {
	return &fakeCompose{psOut: healthyPS}
}

// airvpnMandatory scripts the mandatory prompts of a built-in-provider
// flow (native mode: no .ovpn file is asked); the Optional Subsystems
// answers are appended by the callers.
func airvpnMandatory() string {
	return strings.Join([]string{
		"1", // provider: AirVPN
		"vpnuser",
		"vpnpass",
		"", // SERVER_COUNTRIES: empty (gluetun auto-picks)
		"", // SERVICE_TOKEN: accept generated
		"", // ADMIN_PASSWORD: accept generated
		"Europe/Berlin",
		"50",
		"24",
		"/media",
	}, "\n") + "\n"
}

// airvpnInputs scripts a full flow that skips both Optional Subsystems
// and the reverse proxy (empty answers): media server, bot, Caddy.
func airvpnInputs() string {
	return airvpnMandatory() + "\n\n\n"
}

// TestRunFullBuiltInProviderFlow covers the happy path on a fresh machine.
func TestRunFullBuiltInProviderFlow(t *testing.T) {
	stackDir := makeStackDir(t)
	comp := newFakeCompose()
	out, res, err := runWizard(t, stackDir, airvpnInputs(), comp, nil)
	if err != nil {
		t.Fatalf("Run: %v\noutput:\n%s", err, out)
	}

	envPath := filepath.Join(stackDir, "bitbeard", ".env")
	if res.EnvPath != envPath || !res.WroteEnv || !res.Booted {
		t.Errorf("result = %+v", res)
	}
	if len(comp.upDirs) != 1 || comp.upDirs[0] != filepath.Join(stackDir, "bitbeard") {
		t.Errorf("compose.Up called with %v", comp.upDirs)
	}

	data, err := os.ReadFile(envPath)
	if err != nil {
		t.Fatalf("read .env: %v", err)
	}
	e, err := ParseEnv(string(data))
	if err != nil {
		t.Fatalf("parse .env: %v", err)
	}
	if probs := e.Problems(); len(probs) > 0 {
		t.Errorf("written .env invalid: %v", probs)
	}
	token, adminPass := expectedToken(t)
	want := Env{
		"VPN_PROVIDER":      "airvpn",
		"OPENVPN_USER":      "vpnuser",
		"OPENVPN_PASSWORD":  "vpnpass",
		"TZ":                "Europe/Berlin",
		"SERVICE_TOKEN":     token,
		"ADMIN_PASSWORD":    adminPass,
		"DEFAULT_QUOTA_GB":  "50",
		"SESSION_TTL_HOURS": "24",
		"BASE_DOWNLOAD_DIR": "/media",
	}
	for _, k := range envKeys {
		if k == "COMPOSE_PROFILES" {
			if e[k] != "" {
				t.Errorf("COMPOSE_PROFILES = %q, want empty", e[k])
			}
			continue
		}
		if w, isPrompted := want[k]; isPrompted && e[k] != w {
			t.Errorf("%s = %q, want %q", k, e[k], w)
		}
	}
	// Seeded from the stack's own .env.example.
	if e["JACKETT_API_KEY"] != "examplejackettkey" {
		t.Errorf("JACKETT_API_KEY = %q, want the .env.example value", e["JACKETT_API_KEY"])
	}

	// Prompt-per-key sequencing: the questions appear in exactly this order.
	order := []string{
		"VPN provider (number or name):",
		"OPENVPN_USER",
		"OPENVPN_PASSWORD",
		"SERVER_COUNTRIES (optional",
		"Suggested SERVICE_TOKEN",
		"Suggested ADMIN_PASSWORD",
		"TZ (IANA timezone",
		"DEFAULT_QUOTA_GB",
		"SESSION_TTL_HOURS",
		"BASE_DOWNLOAD_DIR",
		"Media Server (empty = skip)",
		"DISCORD_BOT_TOKEN (empty",
	}
	last := -1
	for _, frag := range order {
		i := strings.Index(out, frag)
		if i == -1 {
			t.Errorf("output missing %q:\n%s", frag, out)
			continue
		}
		if i < last {
			t.Errorf("prompt %q appears out of order:\n%s", frag, out)
		}
		last = i
	}

	// Native mode: no .ovpn file is asked for or touched.
	if strings.Contains(out, "Path to your .ovpn file") {
		t.Errorf("a known provider must not be asked for an .ovpn file:\n%s", out)
	}

	// A generated secret was shown and its confirmation offered.
	if !strings.Contains(out, "Press Enter to accept the suggested SERVICE_TOKEN") {
		t.Errorf("generated-secret confirmation missing:\n%s", out)
	}
	// The honest note about pre-boot VPN validation.
	if !strings.Contains(out, "cannot be truly checked before boot") {
		t.Errorf("honest VPN note missing:\n%s", out)
	}
	// No bundled-file note: native mode leaves openvpn/custom.ovpn alone
	// and gluetun never reads it.
	if strings.Contains(out, "bundled NordVPN configuration") {
		t.Errorf("unexpected bundled-file note:\n%s", out)
	}
	// .gitignore already covers .env: nothing appended, no note.
	if strings.Contains(out, "Added /.env") {
		t.Errorf("unexpected .gitignore edit note:\n%s", out)
	}
	// Success summary with the verified Subsystems.
	if !strings.Contains(out, "The mandatory stack is up.") {
		t.Errorf("success summary missing:\n%s", out)
	}
}

// TestRunNordVPNKeepsBundledConfig covers native mode end to end: NordVPN
// needs no .ovpn file at all — gluetun picks and authenticates servers
// from VPN_SERVICE_PROVIDER plus the credentials — and the bundled
// openvpn/custom.ovpn is neither asked about nor modified.
func TestRunNordVPNKeepsBundledConfig(t *testing.T) {
	stackDir := makeStackDir(t)
	comp := newFakeCompose()
	bundled, _ := os.ReadFile(filepath.Join(stackDir, "bitbeard", "openvpn", "custom.ovpn"))

	// NordVPN: creds and an optional server-selection answer, no file.
	inputs := strings.Join([]string{
		"nordvpn", // provider by name
		"vpnuser",
		"vpnpass",
		"Netherlands", // SERVER_COUNTRIES
		"",            // SERVICE_TOKEN: accept generated
		"",            // ADMIN_PASSWORD: accept generated
		"UTC", "50", "24", "/media",
		"", // Media Server: skip
		"", // Bot: skip
		"", // Reverse proxy: skip
	}, "\n") + "\n"
	out, _, err := runWizard(t, stackDir, inputs, comp, nil)
	if err != nil {
		t.Fatalf("Run: %v\noutput:\n%s", err, out)
	}
	if strings.Contains(out, "Path to your .ovpn file") {
		t.Errorf("the .ovpn prompt fired for a known provider:\n%s", out)
	}
	after, _ := os.ReadFile(filepath.Join(stackDir, "bitbeard", "openvpn", "custom.ovpn"))
	if string(after) != string(bundled) {
		t.Errorf("bundled custom.ovpn was modified:\n%s", after)
	}
	e := readEnvFile(t, stackDir)
	if e["OPENVPN_CUSTOM_CONFIG"] != "" {
		t.Errorf("OPENVPN_CUSTOM_CONFIG = %q, want empty in native mode", e["OPENVPN_CUSTOM_CONFIG"])
	}
	if e["SERVER_COUNTRIES"] != "Netherlands" {
		t.Errorf("SERVER_COUNTRIES = %q, want Netherlands", e["SERVER_COUNTRIES"])
	}
	if probs := e.Problems(); len(probs) > 0 {
		t.Errorf("native-mode .env invalid: %v", probs)
	}
}

func TestRunCustomOVPNPathCopiesAndConfirms(t *testing.T) {
	stackDir := makeStackDir(t)
	src := filepath.Join(t.TempDir(), "my-tunnel.ovpn")
	if err := os.WriteFile(src, []byte("client\nremote 5.6.7.8 1194\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	comp := newFakeCompose()

	nCustom := len(VPNProviders) + 1
	inputs := strings.Join([]string{
		itoa(nCustom), // provider: custom .ovpn file
		src,           // path to the file
		"",            // SERVICE_TOKEN
		"",            // ADMIN_PASSWORD
		"UTC",
		"10",
		"12",
		"",  // BASE_DOWNLOAD_DIR default /media
		"",  // Media Server: skip
		"",  // Bot: skip
		"",  // Reverse proxy: skip
		"y", // confirm replacing the bundled custom.ovpn
	}, "\n") + "\n"
	out, _, err := runWizard(t, stackDir, inputs, comp, nil)
	if err != nil {
		t.Fatalf("Run: %v\noutput:\n%s", err, out)
	}

	data, err := os.ReadFile(filepath.Join(stackDir, "bitbeard", "openvpn", "custom.ovpn"))
	if err != nil {
		t.Fatalf("read custom.ovpn: %v", err)
	}
	if string(data) != "client\nremote 5.6.7.8 1194\n" {
		t.Errorf("custom.ovpn = %q, want the user's file", data)
	}
	envData, _ := os.ReadFile(filepath.Join(stackDir, "bitbeard", ".env"))
	e, _ := ParseEnv(string(envData))
	if e["VPN_PROVIDER"] != customProvider {
		t.Errorf("VPN_PROVIDER = %q, want custom", e["VPN_PROVIDER"])
	}
	if e["OPENVPN_USER"] != "" || e["OPENVPN_PASSWORD"] != "" {
		t.Errorf("custom path should leave VPN creds empty, got %q/%q", e["OPENVPN_USER"], e["OPENVPN_PASSWORD"])
	}
	if e["OPENVPN_CUSTOM_CONFIG"] != customConfigMountPath {
		t.Errorf("OPENVPN_CUSTOM_CONFIG = %q, want %q", e["OPENVPN_CUSTOM_CONFIG"], customConfigMountPath)
	}
	if probs := e.Problems(); len(probs) > 0 {
		t.Errorf("custom-path .env invalid: %v", probs)
	}
	if !strings.Contains(out, "replaced") {
		t.Errorf("overwrite note missing:\n%s", out)
	}
	if !strings.Contains(out, "BASE_DOWNLOAD_DIR=/media") {
		t.Errorf("defaulted BASE_DOWNLOAD_DIR not summarized:\n%s", out)
	}
}

func TestRunCustomOVPNRefusalAborts(t *testing.T) {
	stackDir := makeStackDir(t)
	src := filepath.Join(t.TempDir(), "my-tunnel.ovpn")
	os.WriteFile(src, []byte("client\n"), 0o644)
	comp := newFakeCompose()

	nCustom := len(VPNProviders) + 1
	inputs := strings.Join([]string{
		itoa(nCustom), src, "", "", "UTC", "10", "12", "", "", "", "",
		"n", // refuse to replace the bundled file
	}, "\n") + "\n"
	_, _, err := runWizard(t, stackDir, inputs, comp, nil)
	if err == nil || !strings.Contains(err.Error(), "refused to replace") {
		t.Errorf("err = %v, want refusal error", err)
	}
	if len(comp.upDirs) != 0 {
		t.Error("stack booted despite the refusal")
	}
	data, _ := os.ReadFile(filepath.Join(stackDir, "bitbeard", "openvpn", "custom.ovpn"))
	if string(data) != "client\nremote 1.2.3.4 1194\n" {
		t.Errorf("bundled file was overwritten: %q", data)
	}
}

func TestRunRerunWithValidEnvSkipsToBoot(t *testing.T) {
	stackDir := makeStackDir(t)
	envPath := filepath.Join(stackDir, "bitbeard", ".env")
	valid := validExampleEnv()
	if err := os.WriteFile(envPath, []byte(valid), 0o600); err != nil {
		t.Fatal(err)
	}

	comp := newFakeCompose()
	// Four empty answers: the VPN provider switch offer, the Media Server
	// switch offer, the Bot enable offer and the reverse-proxy reconfigure
	// offer all default to no, so nothing changes and nothing is rewritten.
	out, res, err := runWizard(t, stackDir, "\n\n\n\n", comp, nil)
	if err != nil {
		t.Fatalf("Run: %v\noutput:\n%s", err, out)
	}
	if !res.Booted || res.WroteEnv {
		t.Errorf("result = %+v, want booted without rewriting .env", res)
	}
	if len(comp.upDirs) != 1 {
		t.Errorf("compose.Up called %d times, want 1", len(comp.upDirs))
	}
	if !strings.Contains(out, "skipping the mandatory prompts") {
		t.Errorf("skip note missing:\n%s", out)
	}
	after, _ := os.ReadFile(envPath)
	if string(after) != valid {
		t.Error("a valid .env was rewritten on rerun")
	}
}

func TestRunRerunWithInvalidEnvOffersRedoAndDeclines(t *testing.T) {
	stackDir := makeStackDir(t)
	envPath := filepath.Join(stackDir, "bitbeard", ".env")
	broken := strings.ReplaceAll(validExampleEnv(), "SERVICE_TOKEN=", "SERVICE_TOKEN=change-me")
	if err := os.WriteFile(envPath, []byte(broken), 0o600); err != nil {
		t.Fatal(err)
	}

	comp := newFakeCompose()
	out, res, err := runWizard(t, stackDir, "n\n", comp, nil)
	if err != nil {
		t.Fatalf("Run: %v\noutput:\n%s", err, out)
	}
	if !strings.Contains(out, "Change SERVICE_TOKEN") {
		t.Errorf("problems not explained:\n%s", out)
	}
	if len(comp.upDirs) != 0 {
		t.Error("stack booted despite the declined redo")
	}
	if res.Booted || res.WroteEnv {
		t.Errorf("result = %+v, want nothing done", res)
	}
	if !strings.Contains(out, "Nothing was changed") {
		t.Errorf("decline message missing:\n%s", out)
	}
	after, _ := os.ReadFile(envPath)
	if string(after) != broken {
		t.Error("the .env was changed despite the declined redo")
	}
}

func TestRunRerunWithInvalidEnvRedoes(t *testing.T) {
	stackDir := makeStackDir(t)
	envPath := filepath.Join(stackDir, "bitbeard", ".env")
	broken := strings.ReplaceAll(validExampleEnv(), "ADMIN_PASSWORD=", "ADMIN_PASSWORD=change-me")
	if err := os.WriteFile(envPath, []byte(broken), 0o600); err != nil {
		t.Fatal(err)
	}

	comp := newFakeCompose()
	out, res, err := runWizard(t, stackDir, "y\n"+airvpnInputs(), comp, nil)
	if err != nil {
		t.Fatalf("Run: %v\noutput:\n%s", err, out)
	}
	if !res.WroteEnv || !res.Booted {
		t.Errorf("result = %+v, want env rewritten and booted", res)
	}
	data, _ := os.ReadFile(envPath)
	e, _ := ParseEnv(string(data))
	if IsChangeMe(e["ADMIN_PASSWORD"]) {
		t.Errorf("redo kept the change-me value: %q", e["ADMIN_PASSWORD"])
	}
	if !strings.Contains(out, "has problems") {
		t.Errorf("problems header missing:\n%s", out)
	}
}

func TestRunVerifiesMandatorySubsystems(t *testing.T) {
	stackDir := makeStackDir(t)
	comp := &fakeCompose{psOut: strings.ReplaceAll(healthyPS, `{"Name":"manager","State":"running","ExitCode":0}`+"\n", "")}
	_, res, err := runWizard(t, stackDir, airvpnInputs(), comp, nil)
	if err == nil {
		t.Fatal("Run succeeded although manager is not running")
	}
	if !strings.Contains(err.Error(), "manager") {
		t.Errorf("error %v does not name the missing container", err)
	}
	if !res.Booted {
		t.Error("Booted should still be true: the boot ran, the check failed")
	}
}

func TestRunBootFailureIsReported(t *testing.T) {
	stackDir := makeStackDir(t)
	comp := &fakeCompose{upErr: errors.New("docker daemon not running")}
	_, res, err := runWizard(t, stackDir, airvpnInputs(), comp, nil)
	if err == nil || !strings.Contains(err.Error(), "docker compose up failed") {
		t.Errorf("err = %v, want boot failure", err)
	}
	if res.Booted {
		t.Error("Booted set despite failure")
	}
}

func TestRunEOFStopsCleanly(t *testing.T) {
	stackDir := makeStackDir(t)
	comp := newFakeCompose()
	_, res, err := runWizard(t, stackDir, "", comp, nil)
	if err == nil || !strings.Contains(err.Error(), "input ended") {
		t.Errorf("err = %v, want input-ended error", err)
	}
	if len(comp.upDirs) != 0 {
		t.Error("stack booted despite EOF")
	}
	if res.WroteEnv {
		t.Error(".env written despite EOF")
	}
	if _, statErr := os.Stat(filepath.Join(stackDir, "bitbeard", ".env")); statErr == nil {
		t.Error(".env file exists despite EOF")
	}
}

func TestRunMissingStackLayoutIsReported(t *testing.T) {
	root := t.TempDir()
	comp := newFakeCompose()
	_, _, err := runWizard(t, root, "", comp, nil)
	if err == nil || !strings.Contains(err.Error(), "docker-compose.yml") {
		t.Errorf("err = %v, want missing-compose error", err)
	}
}

// TestRunRerunSwitchesKnownToCustom covers the rerun switch to the custom
// provider: the .ovpn file is asked for and copied, the native mode's keys
// are cleared, and the config path lands in .env.
func TestRunRerunSwitchesKnownToCustom(t *testing.T) {
	stackDir := makeStackDir(t)
	writeValidEnv(t, stackDir, "", nil)
	src := filepath.Join(t.TempDir(), "my-tunnel.ovpn")
	if err := os.WriteFile(src, []byte("client\nremote 5.6.7.8 1194\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	comp := newFakeCompose()
	inputs := strings.Join([]string{
		"y",                         // change the VPN provider
		itoa(len(VPNProviders) + 1), // custom
		src,                         // path to the .ovpn file
		"",                          // Media Server: skip
		"",                          // Bot: skip
		"",                          // Reverse proxy: skip
		"y",                         // confirm replacing what is at openvpn/custom.ovpn
	}, "\n") + "\n"
	out, res, err := runWizard(t, stackDir, inputs, comp, nil)
	if err != nil {
		t.Fatalf("Run: %v\noutput:\n%s", err, out)
	}
	if !res.WroteEnv || !res.Booted {
		t.Errorf("result = %+v, want .env rewritten and booted", res)
	}
	e := readEnvFile(t, stackDir)
	if e["VPN_PROVIDER"] != customProvider {
		t.Errorf("VPN_PROVIDER = %q, want custom", e["VPN_PROVIDER"])
	}
	if e["OPENVPN_CUSTOM_CONFIG"] != customConfigMountPath {
		t.Errorf("OPENVPN_CUSTOM_CONFIG = %q, want %q", e["OPENVPN_CUSTOM_CONFIG"], customConfigMountPath)
	}
	if e["OPENVPN_USER"] != "" || e["OPENVPN_PASSWORD"] != "" {
		t.Errorf("native credentials survived the switch: %q/%q", e["OPENVPN_USER"], e["OPENVPN_PASSWORD"])
	}
	if e["SERVER_COUNTRIES"] != "" {
		t.Errorf("SERVER_COUNTRIES = %q, want cleared", e["SERVER_COUNTRIES"])
	}
	if probs := e.Problems(); len(probs) > 0 {
		t.Errorf("switched .env invalid: %v", probs)
	}
	data, err := os.ReadFile(filepath.Join(stackDir, "bitbeard", "openvpn", "custom.ovpn"))
	if err != nil || string(data) != "client\nremote 5.6.7.8 1194\n" {
		t.Errorf("custom.ovpn = %q (err %v), want the user's file", data, err)
	}
	if !strings.Contains(out, "Copied your OpenVPN configuration") {
		t.Errorf("copy note missing:\n%s", out)
	}
}

// TestRunRerunSwitchesCustomToKnown covers the reverse switch: credentials
// and server selection are asked, the config path is cleared and the
// copied .ovpn is removed, leaving no stale file or env behind.
func TestRunRerunSwitchesCustomToKnown(t *testing.T) {
	stackDir := makeStackDir(t)
	writeValidEnv(t, stackDir, "", Env{
		"VPN_PROVIDER":          customProvider,
		"OPENVPN_CUSTOM_CONFIG": customConfigMountPath,
	})
	customPath := filepath.Join(stackDir, "bitbeard", "openvpn", "custom.ovpn")
	if err := os.WriteFile(customPath, []byte("client\nremote 5.6.7.8 1194\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	comp := newFakeCompose()
	inputs := strings.Join([]string{
		"y",       // change the VPN provider
		"nordvpn", // known provider by name
		"vpnuser2",
		"vpnpass2",
		"Netherlands", // SERVER_COUNTRIES
		"y",           // delete the now-unused copy
		"",            // Media Server: skip
		"",            // Bot: skip
		"",            // Reverse proxy: skip
	}, "\n") + "\n"
	out, res, err := runWizard(t, stackDir, inputs, comp, nil)
	if err != nil {
		t.Fatalf("Run: %v\noutput:\n%s", err, out)
	}
	if !res.WroteEnv || !res.Booted {
		t.Errorf("result = %+v, want .env rewritten and booted", res)
	}
	e := readEnvFile(t, stackDir)
	if e["VPN_PROVIDER"] != "nordvpn" {
		t.Errorf("VPN_PROVIDER = %q, want nordvpn", e["VPN_PROVIDER"])
	}
	if e["OPENVPN_USER"] != "vpnuser2" || e["OPENVPN_PASSWORD"] != "vpnpass2" {
		t.Errorf("credentials = %q/%q, want the new ones", e["OPENVPN_USER"], e["OPENVPN_PASSWORD"])
	}
	if e["SERVER_COUNTRIES"] != "Netherlands" {
		t.Errorf("SERVER_COUNTRIES = %q, want Netherlands", e["SERVER_COUNTRIES"])
	}
	if e["OPENVPN_CUSTOM_CONFIG"] != "" {
		t.Errorf("OPENVPN_CUSTOM_CONFIG = %q, want cleared", e["OPENVPN_CUSTOM_CONFIG"])
	}
	if probs := e.Problems(); len(probs) > 0 {
		t.Errorf("switched .env invalid: %v", probs)
	}
	if _, err := os.Stat(customPath); !os.IsNotExist(err) {
		t.Errorf("the unused custom.ovpn survived the switch (stat err %v)", err)
	}
	if !strings.Contains(out, "Removed the unused "+filepath.Join("openvpn", "custom.ovpn")) {
		t.Errorf("removal note missing:\n%s", out)
	}
}

// TestRunRerunCustomToKnownCanKeepTheCopy covers the operator declining
// the cleanup: the file stays (unused) by explicit choice, the env is
// still switched cleanly.
func TestRunRerunCustomToKnownCanKeepTheCopy(t *testing.T) {
	stackDir := makeStackDir(t)
	writeValidEnv(t, stackDir, "", Env{
		"VPN_PROVIDER":          customProvider,
		"OPENVPN_CUSTOM_CONFIG": customConfigMountPath,
	})
	customPath := filepath.Join(stackDir, "bitbeard", "openvpn", "custom.ovpn")
	if err := os.WriteFile(customPath, []byte("client\nremote 5.6.7.8 1194\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	comp := newFakeCompose()
	inputs := strings.Join([]string{
		"y",       // change the VPN provider
		"nordvpn", // known provider by name
		"vpnuser2",
		"vpnpass2",
		"",  // SERVER_COUNTRIES: auto-pick
		"n", // keep the copy
		"",  // Media Server: skip
		"",  // Bot: skip
		"",  // Reverse proxy: skip
	}, "\n") + "\n"
	out, _, err := runWizard(t, stackDir, inputs, comp, nil)
	if err != nil {
		t.Fatalf("Run: %v\noutput:\n%s", err, out)
	}
	if _, statErr := os.Stat(customPath); statErr != nil {
		t.Errorf("the copy was removed although the operator kept it: %v", statErr)
	}
	if !strings.Contains(out, "gluetun ignores it while VPN_PROVIDER=nordvpn") {
		t.Errorf("keep-note missing:\n%s", out)
	}
	e := readEnvFile(t, stackDir)
	if e["VPN_PROVIDER"] != "nordvpn" || e["OPENVPN_CUSTOM_CONFIG"] != "" {
		t.Errorf("env not switched cleanly: provider %q, config %q", e["VPN_PROVIDER"], e["OPENVPN_CUSTOM_CONFIG"])
	}
}

// validExampleEnv is a valid .env as the wizard would have written it.
func validExampleEnv() string {
	token, adminPass := hex.EncodeToString([]byte(randSeed[:24])), hex.EncodeToString([]byte(randSeed[24:40]))
	return renderEnv(mergeEnv(fallbackEnv, Env{
		"VPN_PROVIDER":      "nordvpn",
		"OPENVPN_USER":      "vpnuser",
		"OPENVPN_PASSWORD":  "vpnpass",
		"TZ":                "UTC",
		"SERVICE_TOKEN":     token,
		"ADMIN_PASSWORD":    adminPass,
		"DEFAULT_QUOTA_GB":  "50",
		"SESSION_TTL_HOURS": "24",
		"BASE_DOWNLOAD_DIR": "/media",
		"COMPOSE_PROFILES":  "",
	}))
}
