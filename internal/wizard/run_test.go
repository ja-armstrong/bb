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
// flow; the Optional Subsystems answers are appended by the callers.
func airvpnMandatory() string {
	return strings.Join([]string{
		"1", // provider: AirVPN
		"vpnuser",
		"vpnpass",
		"", // own .ovpn path: empty (keep bundled, with warning)
		"", // SERVICE_TOKEN: accept generated
		"", // ADMIN_PASSWORD: accept generated
		"Europe/Berlin",
		"50",
		"24",
		"/media",
	}, "\n") + "\n"
}

// airvpnInputs scripts a full flow that skips both Optional Subsystems
// (empty answers): media server and bot.
func airvpnInputs() string {
	return airvpnMandatory() + "\n\n"
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
		"Suggested SERVICE_TOKEN",
		"Suggested ADMIN_PASSWORD",
		"TZ (IANA timezone",
		"DEFAULT_QUOTA_GB",
		"SESSION_TTL_HOURS",
		"BASE_DOWNLOAD_DIR",
		"Media Server (number",
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

	// A generated secret was shown and its confirmation offered.
	if !strings.Contains(out, "Press Enter to accept the suggested SERVICE_TOKEN") {
		t.Errorf("generated-secret confirmation missing:\n%s", out)
	}
	// The honest note about pre-boot VPN validation.
	if !strings.Contains(out, "cannot be truly checked before boot") {
		t.Errorf("honest VPN note missing:\n%s", out)
	}
	// The bundled-file note for a non-NordVPN provider without a file.
	if !strings.Contains(out, "still holds the bundled NordVPN configuration") {
		t.Errorf("bundled NordVPN note missing:\n%s", out)
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

func TestRunNordVPNKeepsBundledConfig(t *testing.T) {
	stackDir := makeStackDir(t)
	comp := newFakeCompose()
	bundled, _ := os.ReadFile(filepath.Join(stackDir, "bitbeard", "openvpn", "custom.ovpn"))

	// NordVPN: no own .ovpn path is asked, so one prompt fewer.
	inputs := strings.Join([]string{
		"nordvpn", // provider by name
		"vpnuser",
		"vpnpass",
		"", // SERVICE_TOKEN: accept generated
		"", // ADMIN_PASSWORD: accept generated
		"UTC", "50", "24", "/media",
		"", // Media Server: skip
		"", // Bot: skip
	}, "\n") + "\n"
	out, _, err := runWizard(t, stackDir, inputs, comp, nil)
	if err != nil {
		t.Fatalf("Run: %v\noutput:\n%s", err, out)
	}
	after, _ := os.ReadFile(filepath.Join(stackDir, "bitbeard", "openvpn", "custom.ovpn"))
	if string(after) != string(bundled) {
		t.Errorf("bundled custom.ovpn was modified:\n%s", after)
	}
	if !strings.Contains(out, "Keeping the bundled NordVPN configuration") {
		t.Errorf("keep-note missing:\n%s", out)
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
		itoa(nCustom), src, "", "", "UTC", "10", "12", "", "", "",
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
	// Two empty answers: the Media Server switch and the Bot enable offers
	// both default to no, so nothing changes and nothing is rewritten.
	out, res, err := runWizard(t, stackDir, "\n\n", comp, nil)
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
	if !strings.Contains(out, "change-me") {
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
