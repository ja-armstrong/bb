package wizard

import (
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// TestDeriveProfiles covers the COMPOSE_PROFILES derivation: skip, bot,
// plex, jellyfin, combined values, switching, and removal.
func TestDeriveProfiles(t *testing.T) {
	cases := []struct {
		old, media string
		bot        bool
		want       string
	}{
		{"", "", false, ""},                 // skip everything
		{"", "", true, "bot"},               // bot only
		{"", "plex", false, "plex"},         // plex only
		{"", "jellyfin", false, "jellyfin"}, // jellyfin only
		{"", "jellyfin", true, "bot jellyfin"},
		{"", "plex", true, "bot plex"},
		// Switching: the old Media Server profile goes off, the new one on.
		{"plex", "jellyfin", false, "jellyfin"},
		{"jellyfin", "plex", true, "bot plex"},
		// Removal: choosing skip with no old media server yields none.
		{"", "", false, ""},
		{"jellyfin", "", false, ""},
		// Keeping: same value in and out.
		{"bot plex", "plex", true, "bot plex"},
		{"bot", "", true, "bot"},
		// Unknown profiles set by hand survive a rerun.
		{"customextra", "plex", true, "bot plex customextra"},
	}
	for _, c := range cases {
		if got := deriveProfiles(c.old, c.media, c.bot); got != c.want {
			t.Errorf("deriveProfiles(%q, %q, %v) = %q, want %q", c.old, c.media, c.bot, got, c.want)
		}
	}
}

func TestMediaFromProfilesAndHasProfile(t *testing.T) {
	if got := mediaFromProfiles("bot jellyfin"); got != profileJellyfin {
		t.Errorf("mediaFromProfiles = %q, want jellyfin", got)
	}
	if got := mediaFromProfiles("plex"); got != profilePlex {
		t.Errorf("mediaFromProfiles = %q, want plex", got)
	}
	if got := mediaFromProfiles("bot"); got != "" {
		t.Errorf("mediaFromProfiles = %q, want empty", got)
	}
	if !hasProfile("bot jellyfin", "bot") || hasProfile("plex", "bot") {
		t.Error("hasProfile misbehaved")
	}
}

func TestVerifyOptionalRunning(t *testing.T) {
	cs, err := parseComposePS(healthyPSWith("bot", "jellyfin"))
	if err != nil {
		t.Fatal(err)
	}
	if missing := verifyOptionalRunning(cs, "bot jellyfin"); len(missing) != 0 {
		t.Errorf("healthy optional stack reported missing %v", missing)
	}
	if missing := verifyOptionalRunning(cs, "plex"); len(missing) != 1 || missing[0] != "plex" {
		t.Errorf("missing = %v, want [plex]", missing)
	}
	if missing := verifyOptionalRunning(cs, ""); len(missing) != 0 {
		t.Errorf("empty profiles reported missing %v", missing)
	}
	// A disabled-state container is as bad as an absent one.
	down, err := parseComposePS(strings.ReplaceAll(healthyPSWith("bot"),
		`{"Name":"bot","State":"running","ExitCode":0}`, `{"Name":"bot","State":"restarting","ExitCode":0}`))
	if err != nil {
		t.Fatal(err)
	}
	if missing := verifyOptionalRunning(down, "bot"); len(missing) != 1 || missing[0] != "bot" {
		t.Errorf("missing = %v, want [bot]", missing)
	}
}

// healthyPSWith returns ps output for the mandatory stack plus the named
// optional containers, all running.
func healthyPSWith(names ...string) string {
	s := healthyPS
	for _, n := range names {
		s += `{"Name":"` + n + `","State":"running","ExitCode":0}` + "\n"
	}
	return s
}

func TestAskMediaServer(t *testing.T) {
	// Jellyfin is presented first and nudged as recommended.
	out, err := runPrompt(t, "1\n", func(p *prompter) error {
		v, err := p.askMediaServer("explanation\n")
		if v != profileJellyfin {
			t.Errorf("got %q, want jellyfin", v)
		}
		return err
	})
	if err != nil {
		t.Fatalf("askMediaServer: %v", err)
	}
	if !strings.Contains(out, "Jellyfin — recommended") {
		t.Errorf("recommendation missing:\n%s", out)
	}
	if strings.Index(out, "Jellyfin") > strings.Index(out, "Plex") {
		t.Errorf("Jellyfin is not presented first:\n%s", out)
	}

	// By name, number, and the skip forms.
	for _, tc := range []struct{ in, want string }{
		{"2\n", profilePlex},
		{"plex\n", profilePlex},
		{"jellyfin\n", profileJellyfin},
		{"3\n", ""},
		{"skip\n", ""},
		{"\n", ""},
	} {
		v, err := runPromptMediaServer(tc.in)
		if err != nil || v != tc.want {
			t.Errorf("input %q: got %q err %v, want %q nil", tc.in, v, err, tc.want)
		}
	}

	// Garbage is asked again.
	out, err = runPrompt(t, "maybe\n2\n", func(p *prompter) error {
		_, err := p.askMediaServer("explanation\n")
		return err
	})
	if err != nil {
		t.Fatalf("askMediaServer: %v", err)
	}
	if !strings.Contains(out, "is not one of the choices") {
		t.Errorf("refusal missing:\n%s", out)
	}
}

func runPromptMediaServer(input string) (string, error) {
	var out strings.Builder
	p := newPrompter(strings.NewReader(input), &out)
	return p.askMediaServer("explanation\n")
}

// fakeDiscord records CheckToken calls and answers with a scripted
// function.
type fakeDiscord struct {
	resp   func(token string) (int, error)
	calls  int
	tokens []string
}

func (f *fakeDiscord) CheckToken(token string) (int, error) {
	f.calls++
	f.tokens = append(f.tokens, token)
	return f.resp(token)
}

func okDiscord(token string) (int, error) { return 200, nil }

func TestValidateChannelID(t *testing.T) {
	if v, err := ValidateChannelID("123456789012345678"); err != nil || v != "123456789012345678" {
		t.Errorf("ValidateChannelID = %q, %v", v, err)
	}
	for _, bad := range []string{"", "abc", "12 34", "change-me", "-1"} {
		if _, err := ValidateChannelID(bad); err == nil {
			t.Errorf("ValidateChannelID(%q) accepted", bad)
		}
	}
}

func TestAskBotTokenLiveValidation(t *testing.T) {
	// Empty skips without calling Discord.
	f := &fakeDiscord{resp: okDiscord}
	v, err := runPromptBotToken("\n", f)
	if err != nil || v != "" {
		t.Fatalf("empty input: got %q err %v, want empty nil", v, err)
	}
	if f.calls != 0 {
		t.Errorf("Discord called %d times for a skipped prompt", f.calls)
	}

	// A good token is validated once and accepted.
	f = &fakeDiscord{resp: okDiscord}
	v, err = runPromptBotToken("good-token\n", f)
	if err != nil || v != "good-token" {
		t.Fatalf("good token: got %q err %v", v, err)
	}
	if f.calls != 1 {
		t.Errorf("calls = %d, want 1", f.calls)
	}

	// A 401 is a rejectable typo: refused, asked again, and the retry wins.
	f = &fakeDiscord{resp: func(tok string) (int, error) {
		if tok == "good-token" {
			return 200, nil
		}
		return 401, nil
	}}
	v, err = runPromptBotToken("typo-token\ngood-token\n", f)
	if err != nil || v != "good-token" {
		t.Fatalf("401 path: got %q err %v", v, err)
	}
	if f.calls != 2 {
		t.Errorf("calls = %d, want 2", f.calls)
	}

	// A network failure offers the deliberate continue; accepting keeps
	// the token.
	f = &fakeDiscord{resp: func(string) (int, error) { return 0, errors.New("dial tcp: offline") }}
	v, err = runPromptBotToken("good-token\ny\n", f)
	if err != nil || v != "good-token" {
		t.Fatalf("offline-accept path: got %q err %v", v, err)
	}
	if f.calls != 1 {
		t.Errorf("calls = %d, want 1", f.calls)
	}

	// Declining the offer re-asks; empty then skips the Bot.
	v, err = runPromptBotToken("good-token\nn\n\n", f)
	if err != nil || v != "" {
		t.Fatalf("offline-decline path: got %q err %v", v, err)
	}
	if f.calls != 2 {
		t.Errorf("calls = %d, want 2", f.calls)
	}

	// An unexpected status also offers the deliberate continue.
	f = &fakeDiscord{resp: func(string) (int, error) { return 503, nil }}
	v, err = runPromptBotToken("good-token\ny\n", f)
	if err != nil || v != "good-token" {
		t.Fatalf("503-accept path: got %q err %v", v, err)
	}
}

func runPromptBotToken(input string, f *fakeDiscord) (string, error) {
	var out strings.Builder
	p := newPrompter(strings.NewReader(input), &out)
	return p.askBotToken("explanation\n", f)
}

// writeValidEnv writes a valid .env with the given COMPOSE_PROFILES and
// optional extra keys, returning what it wrote.
func writeValidEnv(t *testing.T, stackDir, profiles string, extra Env) string {
	t.Helper()
	e := mergeEnv(fallbackEnv, Env{
		"VPN_PROVIDER":      "nordvpn",
		"OPENVPN_USER":      "vpnuser",
		"OPENVPN_PASSWORD":  "vpnpass",
		"TZ":                "UTC",
		"SERVICE_TOKEN":     "tok",
		"ADMIN_PASSWORD":    "pw",
		"DEFAULT_QUOTA_GB":  "50",
		"SESSION_TTL_HOURS": "24",
		"BASE_DOWNLOAD_DIR": "/media",
		"COMPOSE_PROFILES":  profiles,
	})
	for k, v := range extra {
		e[k] = v
	}
	content := renderEnv(e)
	if err := os.WriteFile(filepath.Join(stackDir, "bitbeard", ".env"), []byte(content), 0o600); err != nil {
		t.Fatal(err)
	}
	return content
}

func readEnvFile(t *testing.T, stackDir string) Env {
	t.Helper()
	data, err := os.ReadFile(filepath.Join(stackDir, "bitbeard", ".env"))
	if err != nil {
		t.Fatalf("read .env: %v", err)
	}
	e, err := ParseEnv(string(data))
	if err != nil {
		t.Fatalf("parse .env: %v", err)
	}
	return e
}

// ok200 answers 200 for every token.
var ok200 = &fakeDiscord{resp: okDiscord}

// TestRunFreshJellyfinAndBot covers the fresh path with both optionals:
// profiles derivation, live validation, boot verification, and the
// first-run URL print.
func TestRunFreshJellyfinAndBot(t *testing.T) {
	stackDir := makeStackDir(t)
	comp := &fakeCompose{psOut: healthyPSWith("bot", "jellyfin")}
	input := airvpnMandatory() + "jellyfin\ntok-good\n123456789012345678\n"
	out, res, err := runWizard(t, stackDir, input, comp, ok200)
	if err != nil {
		t.Fatalf("Run: %v\noutput:\n%s", err, out)
	}
	if !res.Booted {
		t.Errorf("result = %+v, want booted", res)
	}
	e := readEnvFile(t, stackDir)
	if e["COMPOSE_PROFILES"] != "bot jellyfin" {
		t.Errorf("COMPOSE_PROFILES = %q, want %q", e["COMPOSE_PROFILES"], "bot jellyfin")
	}
	if e["DISCORD_BOT_TOKEN"] != "tok-good" || e["DISCORD_ANNOUNCE_CHANNEL_ID"] != "123456789012345678" {
		t.Errorf("bot keys = %q / %q", e["DISCORD_BOT_TOKEN"], e["DISCORD_ANNOUNCE_CHANNEL_ID"])
	}
	if probs := e.Problems(); len(probs) > 0 {
		t.Errorf("written .env invalid: %v", probs)
	}
	// The Jellyfin first-run URL is printed after the verified boot.
	if !strings.Contains(out, "Jellyfin (Media Server): http://localhost:8096") {
		t.Errorf("Jellyfin URL missing:\n%s", out)
	}
	// The Bot keys were summarized.
	if !strings.Contains(out, "DISCORD_BOT_TOKEN=tok-good") {
		t.Errorf("bot token not summarized:\n%s", out)
	}
}

// TestRunFreshPlexNeverCollectsClaim covers the Plex path: enabled with no
// claim token, and the manual /web claim URL printed after boot.
func TestRunFreshPlexNeverCollectsClaim(t *testing.T) {
	stackDir := makeStackDir(t)
	comp := &fakeCompose{psOut: healthyPSWith("plex")}
	input := airvpnMandatory() + "plex\n\n"
	out, _, err := runWizard(t, stackDir, input, comp, nil)
	if err != nil {
		t.Fatalf("Run: %v\noutput:\n%s", err, out)
	}
	e := readEnvFile(t, stackDir)
	if e["COMPOSE_PROFILES"] != "plex" {
		t.Errorf("COMPOSE_PROFILES = %q, want plex", e["COMPOSE_PROFILES"])
	}
	if e["PLEX_CLAIM"] != "" {
		t.Errorf("PLEX_CLAIM = %q, want empty — the wizard never collects one", e["PLEX_CLAIM"])
	}
	if !strings.Contains(out, "http://localhost:32400/web") {
		t.Errorf("Plex claim URL missing:\n%s", out)
	}
}

// TestRunRerunSwitchMediaServer covers switching: old profile off, new one
// on, exactly one at the end.
func TestRunRerunSwitchMediaServer(t *testing.T) {
	stackDir := makeStackDir(t)
	writeValidEnv(t, stackDir, "plex", nil)
	comp := &fakeCompose{psOut: healthyPSWith("jellyfin")}
	// Change VPN provider? no. Change Media Server? yes -> choice 1
	// (jellyfin). Bot disabled -> stay disabled.
	out, res, err := runWizard(t, stackDir, "\ny\n1\n\n", comp, nil)
	if err != nil {
		t.Fatalf("Run: %v\noutput:\n%s", err, out)
	}
	if !res.WroteEnv {
		t.Errorf("result = %+v, want .env rewritten", res)
	}
	e := readEnvFile(t, stackDir)
	if e["COMPOSE_PROFILES"] != "jellyfin" {
		t.Errorf("COMPOSE_PROFILES = %q, want jellyfin", e["COMPOSE_PROFILES"])
	}
	if strings.Contains(e["COMPOSE_PROFILES"], "plex") {
		t.Errorf("the old plex profile survived the switch: %q", e["COMPOSE_PROFILES"])
	}
	if !strings.Contains(out, "COMPOSE_PROFILES=jellyfin") {
		t.Errorf("switch not summarized:\n%s", out)
	}
	if !strings.Contains(out, "http://localhost:8096") {
		t.Errorf("Jellyfin URL missing after switch:\n%s", out)
	}
}

// TestRunRerunSkipKeepsExisting covers the no-op rerun: skipping every
// offer changes nothing, and a previously valid choice survives.
func TestRunRerunSkipKeepsExisting(t *testing.T) {
	stackDir := makeStackDir(t)
	before := writeValidEnv(t, stackDir, "plex", nil)
	comp := &fakeCompose{psOut: healthyPSWith("plex")}
	// No to the VPN provider switch, the Media Server switch and enabling
	// the Bot.
	out, res, err := runWizard(t, stackDir, "\n\n\n", comp, nil)
	if err != nil {
		t.Fatalf("Run: %v\noutput:\n%s", err, out)
	}
	if res.WroteEnv {
		t.Errorf("result = %+v, want .env untouched", res)
	}
	after, _ := os.ReadFile(filepath.Join(stackDir, "bitbeard", ".env"))
	if string(after) != before {
		t.Errorf("a skipped rerun rewrote .env:\n%s", after)
	}
	if !res.Booted {
		t.Error("boot should still run and verify")
	}
	if !strings.Contains(out, "nothing was changed") {
		t.Errorf("no-change note missing:\n%s", out)
	}

	// Same with the Bot fully configured: still nothing to ask or change.
	stackDir = makeStackDir(t)
	before = writeValidEnv(t, stackDir, "bot plex", Env{
		"DISCORD_BOT_TOKEN":           "tok",
		"DISCORD_ANNOUNCE_CHANNEL_ID": "123",
	})
	comp = &fakeCompose{psOut: healthyPSWith("bot", "plex")}
	out, res, err = runWizard(t, stackDir, "\n\n\n", comp, ok200)
	if err != nil {
		t.Fatalf("Run: %v\noutput:\n%s", err, out)
	}
	if res.WroteEnv {
		t.Errorf("result = %+v, want .env untouched", res)
	}
	after, _ = os.ReadFile(filepath.Join(stackDir, "bitbeard", ".env"))
	if string(after) != before {
		t.Errorf("a fully-configured rerun rewrote .env:\n%s", after)
	}
	if strings.Contains(out, "DISCORD_BOT_TOKEN (empty") {
		t.Errorf("the token was asked although already set:\n%s", out)
	}
}

// TestRunRerunPromptsOnlyMissing covers enabling the Bot on rerun: the
// Media Server is not re-asked (skip keeps it) and only the Bot keys are
// collected.
func TestRunRerunPromptsOnlyMissing(t *testing.T) {
	stackDir := makeStackDir(t)
	writeValidEnv(t, stackDir, "", nil)
	comp := &fakeCompose{psOut: healthyPSWith("bot")}
	// Keep Media Server (no), enable the Bot (yes), token, channel. The
	// leading answer declines the VPN provider switch.
	out, res, err := runWizard(t, stackDir, "\n\ny\ntok-rerun\n123456789012345678\n", comp, ok200)
	if err != nil {
		t.Fatalf("Run: %v\noutput:\n%s", err, out)
	}
	if !res.WroteEnv {
		t.Errorf("result = %+v, want .env rewritten", res)
	}
	e := readEnvFile(t, stackDir)
	if e["COMPOSE_PROFILES"] != "bot" {
		t.Errorf("COMPOSE_PROFILES = %q, want bot:\n%s", e["COMPOSE_PROFILES"], out)
	}
	if e["DISCORD_BOT_TOKEN"] != "tok-rerun" {
		t.Errorf("DISCORD_BOT_TOKEN = %q", e["DISCORD_BOT_TOKEN"])
	}
	// The Media Server prompt did not fire again: no choice was re-asked.
	if strings.Contains(out, "Media Server (number") {
		t.Errorf("the full Media Server prompt fired although only the switch offer should:\n%s", out)
	}
	if !strings.Contains(out, "Change the Media Server?") {
		t.Errorf("the switch offer missing:\n%s", out)
	}
	if !strings.Contains(out, "DISCORD_BOT_TOKEN=tok-rerun") {
		t.Errorf("token not summarized:\n%s", out)
	}
}

// TestRunRerunMissingBotTokenOnly covers the partial-Bot state: the bot
// profile is on, the channel is set, only the token is asked.
func TestRunRerunMissingBotTokenOnly(t *testing.T) {
	stackDir := makeStackDir(t)
	writeValidEnv(t, stackDir, "bot", Env{"DISCORD_ANNOUNCE_CHANNEL_ID": "123456789012345678"})
	comp := &fakeCompose{psOut: healthyPSWith("bot")}
	// No VPN switch, no media switch, then the token.
	out, res, err := runWizard(t, stackDir, "\n\ntok-late\n", comp, ok200)
	if err != nil {
		t.Fatalf("Run: %v\noutput:\n%s", err, out)
	}
	if !res.WroteEnv {
		t.Errorf("result = %+v, want .env rewritten", res)
	}
	e := readEnvFile(t, stackDir)
	if e["DISCORD_BOT_TOKEN"] != "tok-late" {
		t.Errorf("DISCORD_BOT_TOKEN = %q, want tok-late", e["DISCORD_BOT_TOKEN"])
	}
	if e["DISCORD_ANNOUNCE_CHANNEL_ID"] != "123456789012345678" {
		t.Errorf("the existing channel was not preserved: %q", e["DISCORD_ANNOUNCE_CHANNEL_ID"])
	}
	if e["COMPOSE_PROFILES"] != "bot" {
		t.Errorf("COMPOSE_PROFILES = %q, want bot", e["COMPOSE_PROFILES"])
	}
	if !strings.Contains(out, "DISCORD_BOT_TOKEN is not set") {
		t.Errorf("missing-token explanation missing:\n%s", out)
	}
}

// TestRunRerunBotEnabledWithoutTokenTurnsOff covers the honest corner: the
// bot profile is on but no token was ever set; skipping the prompt turns
// the Bot back off instead of booting a container that cannot start.
func TestRunRerunBotEnabledWithoutTokenTurnsOff(t *testing.T) {
	stackDir := makeStackDir(t)
	writeValidEnv(t, stackDir, "bot", nil)
	comp := newFakeCompose()
	// No VPN switch, no media switch, no token.
	_, _, err := runWizard(t, stackDir, "\n\n\n", comp, ok200)
	if err != nil {
		t.Fatalf("Run: %v", err)
	}
	e := readEnvFile(t, stackDir)
	if e["COMPOSE_PROFILES"] != "" {
		t.Errorf("COMPOSE_PROFILES = %q, want empty (Bot off)", e["COMPOSE_PROFILES"])
	}
}

// TestRunRerunVerifiesOptionalDown covers the post-boot verification of
// enabled Optional Subsystems: an enabled profile whose container is not
// running fails the run and names the profile.
func TestRunRerunVerifiesOptionalDown(t *testing.T) {
	stackDir := makeStackDir(t)
	writeValidEnv(t, stackDir, "jellyfin", nil)
	comp := newFakeCompose() // ps reports only the mandatory containers
	_, res, err := runWizard(t, stackDir, "\n\n\n", comp, nil)
	if err == nil {
		t.Fatal("Run succeeded although jellyfin is not running")
	}
	if !strings.Contains(err.Error(), "jellyfin") {
		t.Errorf("error %v does not name the missing container", err)
	}
	if !res.Booted {
		t.Error("Booted should still be true: the boot ran, the check failed")
	}
	if !strings.Contains(err.Error(), "the enabled Optional Subsystems did not come up") {
		t.Errorf("optional verification message missing: %v", err)
	}
}

// TestRunFreshBotTokenOfflineDeliberateContinue covers the end-to-end
// deliberate-continue path: Discord unreachable, the operator accepts the
// token anyway, and the Bot still lands in COMPOSE_PROFILES.
func TestRunFreshBotTokenOfflineDeliberateContinue(t *testing.T) {
	stackDir := makeStackDir(t)
	comp := &fakeCompose{psOut: healthyPSWith("bot")}
	offline := &fakeDiscord{resp: func(string) (int, error) { return 0, errors.New("dial tcp: offline") }}
	input := airvpnMandatory() + "\ntok-offline\ny\n123456789012345678\n"
	out, _, err := runWizard(t, stackDir, input, comp, offline)
	if err != nil {
		t.Fatalf("Run: %v\noutput:\n%s", err, out)
	}
	e := readEnvFile(t, stackDir)
	if e["COMPOSE_PROFILES"] != "bot" {
		t.Errorf("COMPOSE_PROFILES = %q, want bot", e["COMPOSE_PROFILES"])
	}
	if e["DISCORD_BOT_TOKEN"] != "tok-offline" {
		t.Errorf("DISCORD_BOT_TOKEN = %q", e["DISCORD_BOT_TOKEN"])
	}
	if !strings.Contains(out, "may be offline") {
		t.Errorf("offline note missing:\n%s", out)
	}
}
