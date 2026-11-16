package wizard

import (
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// fakeJellyfin records CheckToken calls and answers with a scripted
// function (the Jellyfin mirror of fakeDiscord).
type fakeJellyfin struct {
	resp   func(baseURL, token string) (int, error)
	calls  int
	urls   []string
	tokens []string
}

func (f *fakeJellyfin) CheckToken(baseURL, token string) (int, error) {
	f.calls++
	f.urls = append(f.urls, baseURL)
	f.tokens = append(f.tokens, token)
	return f.resp(baseURL, token)
}

func okJellyfin(baseURL, token string) (int, error) { return 200, nil }

func TestValidateJellyfinURLOptional(t *testing.T) {
	for _, in := range []string{"", "   "} {
		v, err := ValidateJellyfinURLOptional(in)
		if err != nil || v != "" {
			t.Errorf("ValidateJellyfinURLOptional(%q) = %q, %v, want empty nil", in, v, err)
		}
	}
	if v, err := ValidateJellyfinURLOptional(" http://10.0.0.5:8096 "); err != nil || v != "http://10.0.0.5:8096" {
		t.Errorf("trimmed = %q, %v", v, err)
	}
	for _, in := range []string{"https://jellyfin.lan:8096", "http://host.docker.internal:8096"} {
		if v, err := ValidateJellyfinURLOptional(in); err != nil || v != in {
			t.Errorf("ValidateJellyfinURLOptional(%q) = %q, %v", in, v, err)
		}
	}
	for _, bad := range []string{"jellyfin.lan:8096", "ftp://x", "http://", "change-me"} {
		if _, err := ValidateJellyfinURLOptional(bad); err == nil {
			t.Errorf("ValidateJellyfinURLOptional(%q) accepted", bad)
		}
	}
}

// TestAskJellyfinIntegrationValidated covers the accepted path: token +
// default URL and token + custom URL.
func TestAskJellyfinIntegrationValidated(t *testing.T) {
	f := &fakeJellyfin{resp: okJellyfin}
	tok, url, err := runPromptJellyfin("jf-token\n\n", f)
	if err != nil || tok != "jf-token" {
		t.Fatalf("got %q err %v", tok, err)
	}
	if url != defaultJellyfinURL {
		t.Errorf("url = %q, want the documented default %q", url, defaultJellyfinURL)
	}
	if f.calls != 1 || f.urls[0] != defaultJellyfinURL {
		t.Errorf("calls = %d urls = %v, want one check at the default", f.calls, f.urls)
	}

	// A typed URL is kept and validated against.
	f = &fakeJellyfin{resp: okJellyfin}
	tok, url, err = runPromptJellyfin("jf-token\nhttp://10.0.0.5:8096\n", f)
	if err != nil || tok != "jf-token" || url != "http://10.0.0.5:8096" {
		t.Fatalf("got %q / %q err %v", tok, url, err)
	}
	if f.urls[0] != "http://10.0.0.5:8096" {
		t.Errorf("the custom URL was not used for the check: %v", f.urls)
	}
}

// TestAskJellyfinIntegrationSkip covers skip: an empty token ends the
// integration before the URL is asked and without any check.
func TestAskJellyfinIntegrationSkip(t *testing.T) {
	f := &fakeJellyfin{resp: okJellyfin}
	tok, url, err := runPromptJellyfin("\n", f)
	if err != nil || tok != "" || url != "" {
		t.Fatalf("got %q / %q err %v, want empty empty nil", tok, url, err)
	}
	if f.calls != 0 {
		t.Errorf("Jellyfin called %d times for a skipped prompt", f.calls)
	}
}

// TestAskJellyfinIntegration401Reprompts covers the definitive auth
// failure: refused, asked again, and the retry wins without re-asking
// the URL.
func TestAskJellyfinIntegration401Reprompts(t *testing.T) {
	f := &fakeJellyfin{resp: func(baseURL, token string) (int, error) {
		if token == "good-token" {
			return 200, nil
		}
		return 401, nil
	}}
	out, tok, url, err := runPromptJellyfinOut("typo-token\n\ngood-token\n", f)
	if err != nil || tok != "good-token" {
		t.Fatalf("got %q err %v", tok, err)
	}
	if url != defaultJellyfinURL {
		t.Errorf("url = %q, want the default", url)
	}
	if f.calls != 2 {
		t.Errorf("calls = %d, want 2", f.calls)
	}
	if strings.Count(out, "JELLYFIN_URL (empty") != 1 {
		t.Errorf("the URL prompt fired more than once:\n%s", out)
	}
	if !strings.Contains(out, "401 unauthorized") {
		t.Errorf("401 explanation missing:\n%s", out)
	}
}

// TestAskJellyfinIntegrationOfflineDeliberateContinue covers the network
// failure: the offer to accept unverified, and the decline path looping
// back to the token (where empty skips).
func TestAskJellyfinIntegrationOfflineDeliberateContinue(t *testing.T) {
	offline := func(string, string) (int, error) { return 0, errors.New("dial tcp: offline") }

	// Accepting keeps the token and URL.
	f := &fakeJellyfin{resp: offline}
	tok, url, err := runPromptJellyfin("jf-token\n\ny\n", f)
	if err != nil || tok != "jf-token" || url != defaultJellyfinURL {
		t.Fatalf("accept path: got %q / %q err %v", tok, url, err)
	}
	if f.calls != 1 {
		t.Errorf("calls = %d, want 1", f.calls)
	}

	// Declining re-prompts the token; empty then skips the integration.
	f = &fakeJellyfin{resp: offline}
	tok, url, err = runPromptJellyfin("jf-token\n\nn\n\n", f)
	if err != nil || tok != "" || url != "" {
		t.Fatalf("decline path: got %q / %q err %v, want skip", tok, url, err)
	}
	if f.calls != 1 {
		t.Errorf("calls = %d, want 1", f.calls)
	}

	// An unexpected status also offers the deliberate continue.
	f = &fakeJellyfin{resp: func(string, string) (int, error) { return 503, nil }}
	tok, _, err = runPromptJellyfin("jf-token\n\ny\n", f)
	if err != nil || tok != "jf-token" {
		t.Fatalf("503-accept path: got %q err %v", tok, err)
	}
}

func runPromptJellyfin(input string, f *fakeJellyfin) (string, string, error) {
	_, tok, url, err := runPromptJellyfinOut(input, f)
	return tok, url, err
}

func runPromptJellyfinOut(input string, f *fakeJellyfin) (string, string, string, error) {
	var out strings.Builder
	p := newPrompter(strings.NewReader(input), &out)
	tok, url, err := p.askJellyfinIntegration(defaultJellyfinURL, f)
	return out.String(), tok, url, err
}

// okJellyfinChecker is the 200-for-everything fake, ready to hand to
// runWizard.
var okJellyfinChecker = &fakeJellyfin{resp: okJellyfin}

// TestRunFreshJellyfinProvisioningCollected covers the fresh path with
// the integration accepted: both keys land in .env, the profiles are
// unchanged, and the written file still validates.
func TestRunFreshJellyfinProvisioningCollected(t *testing.T) {
	stackDir := makeStackDir(t)
	comp := &fakeCompose{psOut: healthyPSWith("jellyfin")}
	// Media server jellyfin, token, URL default, bot no. The fresh path
	// offers the integration through the token prompt itself: an empty
	// token answer skips it.
	input := airvpnMandatory() + "jellyfin\njf-token\n\n\n"
	out, res, err := runWizard(t, stackDir, input, comp, nil, okJellyfinChecker)
	if err != nil {
		t.Fatalf("Run: %v\noutput:\n%s", err, out)
	}
	if !res.Booted {
		t.Errorf("result = %+v, want booted", res)
	}
	e := readEnvFile(t, stackDir)
	if e["COMPOSE_PROFILES"] != "jellyfin" {
		t.Errorf("COMPOSE_PROFILES = %q, want jellyfin", e["COMPOSE_PROFILES"])
	}
	if e["JELLYFIN_API_TOKEN"] != "jf-token" {
		t.Errorf("JELLYFIN_API_TOKEN = %q, want jf-token", e["JELLYFIN_API_TOKEN"])
	}
	if e["JELLYFIN_URL"] != defaultJellyfinURL {
		t.Errorf("JELLYFIN_URL = %q, want %q", e["JELLYFIN_URL"], defaultJellyfinURL)
	}
	if probs := e.Problems(); len(probs) > 0 {
		t.Errorf("written .env invalid: %v", probs)
	}
	if !strings.Contains(out, "JELLYFIN_API_TOKEN=jf-token") {
		t.Errorf("integration keys not summarized:\n%s", out)
	}
}

// TestRunFreshJellyfinProvisioningDeclined covers skip on the fresh
// path: no keys are written and Jellyfin is never called.
func TestRunFreshJellyfinProvisioningDeclined(t *testing.T) {
	stackDir := makeStackDir(t)
	comp := &fakeCompose{psOut: healthyPSWith("jellyfin")}
	untouched := &fakeJellyfin{resp: okJellyfin}
	// Media server jellyfin, integration no (empty answer), bot no.
	input := airvpnMandatory() + "jellyfin\n\n\n"
	out, _, err := runWizard(t, stackDir, input, comp, nil, untouched)
	if err != nil {
		t.Fatalf("Run: %v\noutput:\n%s", err, out)
	}
	e := readEnvFile(t, stackDir)
	if e["JELLYFIN_API_TOKEN"] != "" || e["JELLYFIN_URL"] != defaultJellyfinURL {
		t.Errorf("jellyfin keys = %q / %q, want unset / default", e["JELLYFIN_API_TOKEN"], e["JELLYFIN_URL"])
	}
	if untouched.calls != 0 {
		t.Errorf("Jellyfin called %d times although declined", untouched.calls)
	}
}

// TestRunRerunAddsJellyfinProvisioningLater covers the rerun path: the
// integration is unset, the offer is accepted, and the keys land in the
// rewritten .env while the profiles stay as they were.
func TestRunRerunAddsJellyfinProvisioningLater(t *testing.T) {
	stackDir := makeStackDir(t)
	writeValidEnv(t, stackDir, "jellyfin", nil)
	comp := &fakeCompose{psOut: healthyPSWith("jellyfin")}
	// No VPN switch, no media switch, integration yes, token, URL
	// default, bot no.
	out, res, err := runWizard(t, stackDir, "\n\ny\njf-token\n\n\n", comp, nil, okJellyfinChecker)
	if err != nil {
		t.Fatalf("Run: %v\noutput:\n%s", err, out)
	}
	if !res.WroteEnv {
		t.Errorf("result = %+v, want .env rewritten", res)
	}
	e := readEnvFile(t, stackDir)
	if e["JELLYFIN_API_TOKEN"] != "jf-token" {
		t.Errorf("JELLYFIN_API_TOKEN = %q, want jf-token", e["JELLYFIN_API_TOKEN"])
	}
	if e["JELLYFIN_URL"] != defaultJellyfinURL {
		t.Errorf("JELLYFIN_URL = %q, want %q", e["JELLYFIN_URL"], defaultJellyfinURL)
	}
	if e["COMPOSE_PROFILES"] != "jellyfin" {
		t.Errorf("COMPOSE_PROFILES = %q, want jellyfin", e["COMPOSE_PROFILES"])
	}
	if !strings.Contains(out, "JELLYFIN_API_TOKEN=jf-token") {
		t.Errorf("integration keys not summarized:\n%s", out)
	}
}

// TestRunRerunKeepsConfiguredJellyfinProvisioning covers the quiet path:
// with the token already set, the integration is not asked and a
// skip-everything rerun changes nothing.
func TestRunRerunKeepsConfiguredJellyfinProvisioning(t *testing.T) {
	stackDir := makeStackDir(t)
	before := writeValidEnv(t, stackDir, "jellyfin", Env{
		"JELLYFIN_API_TOKEN": "jf-token",
		"JELLYFIN_URL":       "http://10.0.0.5:8096",
	})
	comp := &fakeCompose{psOut: healthyPSWith("jellyfin")}
	// No VPN switch, no media switch, no to the Bot — the integration
	// must not be asked at all.
	out, res, err := runWizard(t, stackDir, "\n\n\n", comp, nil, okJellyfinChecker)
	if err != nil {
		t.Fatalf("Run: %v\noutput:\n%s", err, out)
	}
	if res.WroteEnv {
		t.Errorf("result = %+v, want .env untouched", res)
	}
	after, _ := os.ReadFile(filepath.Join(stackDir, "bitbeard", ".env"))
	if string(after) != before {
		t.Errorf("a fully-configured rerun rewrote .env:\n%s", after)
	}
	if strings.Contains(out, "JELLYFIN_API_TOKEN (empty") {
		t.Errorf("the integration was asked although already set:\n%s", out)
	}
}

// TestRunRerunProvisioningOfferFollowsTheMediaServer covers the switch:
// a Media Server switch to jellyfin surfaces the offer on the same rerun;
// declining keeps the stack as it was.
func TestRunRerunProvisioningOfferFollowsTheMediaServer(t *testing.T) {
	stackDir := makeStackDir(t)
	writeValidEnv(t, stackDir, "plex", nil)
	comp := &fakeCompose{psOut: healthyPSWith("jellyfin")}
	// No VPN switch, media switch yes -> jellyfin, integration yes,
	// token, URL default, bot no.
	_, res, err := runWizard(t, stackDir, "\ny\n1\ny\njf-token\n\n\n", comp, nil, okJellyfinChecker)
	if err != nil {
		t.Fatalf("Run: %v", err)
	}
	e := readEnvFile(t, stackDir)
	if e["COMPOSE_PROFILES"] != "jellyfin" || e["JELLYFIN_API_TOKEN"] != "jf-token" {
		t.Errorf("env = %q / %q", e["COMPOSE_PROFILES"], e["JELLYFIN_API_TOKEN"])
	}
	if !res.WroteEnv {
		t.Error("result should have WroteEnv: the .env was rewritten")
	}
}
