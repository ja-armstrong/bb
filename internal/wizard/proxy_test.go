package wizard

// proxy_test.go wires the reverse-proxy stage into Run end to end with
// fake boundaries: the skip path touches nothing, the yes path installs,
// writes and marks the integration, and the rerun offers reconfiguration.
// No live command, resolver or network call happens anywhere.

import (
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/ja-armstrong/bb/internal/proxy"
)

// fakeProxyRunner records privileged commands and serves canned
// CombinedOutput results.
type fakeProxyRunner struct {
	results map[string]string
	ran     []string
}

func (f *fakeProxyRunner) CombinedOutput(name string, args ...string) (string, error) {
	key := name + " " + strings.Join(args, " ")
	if out, ok := f.results[key]; ok {
		return out, nil
	}
	return "", fmt.Errorf("exec: %q not found", name)
}

func (f *fakeProxyRunner) RunInteractive(name string, args ...string) error {
	f.ran = append(f.ran, name+" "+strings.Join(args, " "))
	return nil
}

// yesDNS resolves everything to the canned public IP.
type yesDNS struct{}

func (yesDNS) LookupHost(string) ([]string, error) { return []string{"203.0.113.7"}, nil }

// fixedIP answers the public-IP echo.
type fixedIP struct{}

func (fixedIP) Current() (string, error) { return "203.0.113.7", nil }

// freePorts reports every port free.
type freePorts struct{}

func (freePorts) Busy(int) (bool, error) { return false, nil }

// proxyTestDeps builds fake Deps with every boundary green and the host
// paths inside a temporary directory.
func proxyTestDeps(t *testing.T) (*proxy.Deps, *fakeProxyRunner) {
	t.Helper()
	dir := strings.ReplaceAll(t.TempDir(), "\\", "/")
	r := &fakeProxyRunner{results: map[string]string{
		"caddy version":             "v2.8.0 h1:...",
		"systemctl is-active caddy": "active",
	}}
	main := dir + "/Caddyfile"
	r.results["caddy validate --config "+main+" --adapter caddyfile"] = "Valid"
	return &proxy.Deps{
		Run:         r,
		DNS:         yesDNS{},
		IP:          fixedIP{},
		Ports:       freePorts{},
		SnippetPath: dir + "/caddy.d/bitbeard.caddy",
		MainPath:    main,
	}, r
}

// runProxyWizard runs Run with fake proxy deps.
func runProxyWizard(t *testing.T, stackDir, input string, comp *fakeCompose, discord DiscordChecker, deps *proxy.Deps) (string, Result, error) {
	t.Helper()
	var out strings.Builder
	res, err := Run(Options{
		StackDir: stackDir,
		In:       strings.NewReader(input),
		Out:      &out,
		Compose:  comp,
		Rand:     newRand(),
		Discord:  discord,
		Proxy:    deps,
	})
	return out.String(), res, err
}

// TestRunProxyDeclineKeepsLANOnly covers the skip path: nothing installed,
// nothing written, and the .env carries no Caddy state at all.
func TestRunProxyDeclineKeepsLANOnly(t *testing.T) {
	stackDir := makeStackDir(t)
	comp := &fakeCompose{psOut: healthyPSWith("jellyfin")}
	deps, ran := proxyTestDeps(t)
	input := airvpnMandatory() + "jellyfin\n\n" + "n\n"
	out, _, err := runProxyWizard(t, stackDir, input, comp, ok200, deps)
	if err != nil {
		t.Fatalf("Run: %v\noutput:\n%s", err, out)
	}
	if !strings.Contains(out, "Staying LAN-only") {
		t.Errorf("the LAN-only note missing:\n%s", out)
	}
	if strings.Contains(out, "reverse_proxy") {
		t.Errorf("no snippet may be generated on the skip path:\n%s", out)
	}
	if len(ran.ran) != 0 {
		t.Errorf("the skip path ran %d privileged commands: %v", len(ran.ran), ran.ran)
	}
	e := readEnvFile(t, stackDir)
	if e[proxy.EnvEnabledKey] != "" {
		t.Errorf("%s = %q, want empty", proxy.EnvEnabledKey, e[proxy.EnvEnabledKey])
	}
	if e["PUBLIC_HTTPS"] != "1" {
		t.Errorf("PUBLIC_HTTPS = %q, want the default 1", e["PUBLIC_HTTPS"])
	}
}

// TestRunProxyYesEndToEnd covers the full yes path with Jellyfin on: the
// derived names are shown back, the stage runs, the markers land in .env,
// and the boot summary prints the public URLs.
func TestRunProxyYesEndToEnd(t *testing.T) {
	stackDir := makeStackDir(t)
	comp := &fakeCompose{psOut: healthyPSWith("jellyfin")}
	deps, ran := proxyTestDeps(t)
	// Media Server jellyfin, Bot skipped, reverse proxy yes, base domain,
	// ACME email.
	input := airvpnMandatory() + "jellyfin\n\n" + "y\nexample.com\nops@example.com\n"
	out, _, err := runProxyWizard(t, stackDir, input, comp, ok200, deps)
	if err != nil {
		t.Fatalf("Run: %v\noutput:\n%s", err, out)
	}

	// The derived names were shown back.
	if !strings.Contains(out, "https://bitbeard.example.com (the Webui)") ||
		!strings.Contains(out, "https://jellyfin.example.com (the Media Server)") {
		t.Errorf("the derived names were not shown back:\n%s", out)
	}
	// The snippet was generated from the answers (both sites, the
	// contract upstreams) and the privileged steps were shown.
	if !strings.Contains(out, "bitbeard.example.com {\n\t# The Webui's nginx") ||
		!strings.Contains(out, "jellyfin.example.com {\n\t# Jellyfin") {
		t.Errorf("the generated snippet missing from the transcript:\n%s", out)
	}
	for _, frag := range []string{"sudo mkdir -p", "sudo install -m 644", "sudo systemctl reload caddy"} {
		if len(ranContaining(ran.ran, frag)) == 0 {
			t.Errorf("%q was never run (ran: %v)", frag, ran.ran)
		}
	}
	if len(ranContaining(ran.ran, "apt-get")) != 0 {
		t.Errorf("caddy was reported installed; no install may run: %v", ran.ran)
	}

	// The state markers and the PUBLIC_HTTPS default are in .env.
	e := readEnvFile(t, stackDir)
	if e[proxy.EnvEnabledKey] != "1" {
		t.Errorf("%s = %q, want 1", proxy.EnvEnabledKey, e[proxy.EnvEnabledKey])
	}
	if e[proxy.EnvDomainKey] != "example.com" {
		t.Errorf("%s = %q, want example.com", proxy.EnvDomainKey, e[proxy.EnvDomainKey])
	}
	if e["PUBLIC_HTTPS"] != "1" {
		t.Errorf("PUBLIC_HTTPS = %q, want the default 1", e["PUBLIC_HTTPS"])
	}
	if probs := e.Problems(); len(probs) > 0 {
		t.Errorf("written .env invalid: %v", probs)
	}

	// The boot summary prints the public URLs alongside the local ones.
	if !strings.Contains(out, "Public Webui: https://bitbeard.example.com") {
		t.Errorf("the public Webui URL missing from the summary:\n%s", out)
	}
	if !strings.Contains(out, "Public Media Server: https://jellyfin.example.com") {
		t.Errorf("the public Media Server URL missing from the summary:\n%s", out)
	}
	// The standing DNS warning names both hosts.
	if !strings.Contains(out, "DNS for bitbeard.example.com and jellyfin.example.com must point") {
		t.Errorf("the DNS warning missing:\n%s", out)
	}
}

// TestRunProxyYesWithoutJellyfinWritesOneSite covers the Media Server
// choice steering the snippet: a Plex (or none) choice writes only the
// Webui block and says so plainly.
func TestRunProxyYesWithoutJellyfinWritesOneSite(t *testing.T) {
	stackDir := makeStackDir(t)
	comp := &fakeCompose{psOut: healthyPSWith("plex")}
	deps, _ := proxyTestDeps(t)
	input := airvpnMandatory() + "plex\n\n" + "y\nexample.com\n\n"
	out, _, err := runProxyWizard(t, stackDir, input, comp, nil, deps)
	if err != nil {
		t.Fatalf("Run: %v\noutput:\n%s", err, out)
	}
	if strings.Contains(out, "jellyfin.example.com {") {
		t.Errorf("a jellyfin site block was generated without Jellyfin:\n%s", out)
	}
	if !strings.Contains(out, "no jellyfin site block is written") {
		t.Errorf("the plain no-jellyfin note missing:\n%s", out)
	}
	e := readEnvFile(t, stackDir)
	if e[proxy.EnvEnabledKey] != "1" || e[proxy.EnvDomainKey] != "example.com" {
		t.Errorf("markers wrong: %q / %q", e[proxy.EnvEnabledKey], e[proxy.EnvDomainKey])
	}
	if strings.Contains(out, "Public Media Server") {
		t.Errorf("a public Media Server URL was printed without Jellyfin:\n%s", out)
	}
}

// TestRunRerunProxyDeclinedShowsState covers the rerun state line: the
// integration on is shown with its derived names, declining changes
// nothing, and the boot summary still prints the public URLs from .env.
func TestRunRerunProxyDeclinedShowsState(t *testing.T) {
	stackDir := makeStackDir(t)
	before := writeValidEnv(t, stackDir, "jellyfin", Env{
		proxy.EnvEnabledKey: "1",
		proxy.EnvDomainKey:  "old.example.com",
	})
	comp := &fakeCompose{psOut: healthyPSWith("jellyfin")}
	deps, ran := proxyTestDeps(t)
	// No VPN switch, no Media Server switch, no Bot enable, no
	// reconfigure.
	out, res, err := runProxyWizard(t, stackDir, "\n\n\n\n", comp, nil, deps)
	if err != nil {
		t.Fatalf("Run: %v\noutput:\n%s", err, out)
	}
	if res.WroteEnv {
		t.Error("a declined rerun rewrote .env")
	}
	after, _ := os.ReadFile(filepath.Join(stackDir, "bitbeard", ".env"))
	if string(after) != before {
		t.Errorf("a declined rerun changed .env:\n%s", after)
	}
	if len(ran.ran) != 0 {
		t.Errorf("a declined rerun ran privileged commands: %v", ran.ran)
	}
	if !strings.Contains(out, "Public access: behind Caddy (https://bitbeard.old.example.com, https://jellyfin.old.example.com)") {
		t.Errorf("the rerun state line missing:\n%s", out)
	}
	if !strings.Contains(out, "Public Webui: https://bitbeard.old.example.com") {
		t.Errorf("the boot summary lost the public URL:\n%s", out)
	}
}

// TestRunRerunProxyReconfigure covers accepting the offer: the full stage
// re-runs with the new answers, the snippet is regenerated, and .env's
// domain record follows.
func TestRunRerunProxyReconfigure(t *testing.T) {
	stackDir := makeStackDir(t)
	writeValidEnv(t, stackDir, "jellyfin", Env{
		proxy.EnvEnabledKey: "1",
		proxy.EnvDomainKey:  "old.example.com",
	})
	comp := &fakeCompose{psOut: healthyPSWith("jellyfin")}
	deps, ran := proxyTestDeps(t)
	// No VPN switch, no Media Server switch, no Bot enable, reconfigure
	// yes, new base domain, no email.
	out, res, err := runProxyWizard(t, stackDir, "\n\n\ny\nnew.example.com\n\n", comp, nil, deps)
	if err != nil {
		t.Fatalf("Run: %v\noutput:\n%s", err, out)
	}
	if !res.WroteEnv {
		t.Error("a reconfigured rerun must rewrite .env with the new domain")
	}
	e := readEnvFile(t, stackDir)
	if e[proxy.EnvEnabledKey] != "1" || e[proxy.EnvDomainKey] != "new.example.com" {
		t.Errorf("markers wrong after reconfigure: %q / %q", e[proxy.EnvEnabledKey], e[proxy.EnvDomainKey])
	}
	if len(ranContaining(ran.ran, "sudo install -m 644")) < 2 {
		t.Errorf("the snippet and the main Caddyfile were not reinstalled: %v", ran.ran)
	}
	if !strings.Contains(out, "Public Webui: https://bitbeard.new.example.com") {
		t.Errorf("the boot summary kept the old domain:\n%s", out)
	}
}

// ranContaining returns the recorded lines containing frag.
func ranContaining(lines []string, frag string) []string {
	var out []string
	for _, l := range lines {
		if strings.Contains(l, frag) {
			out = append(out, l)
		}
	}
	return out
}
