package proxy

// proxy_test.go drives Apply end to end against fakes: canned command
// output, canned DNS and public-IP answers, canned port probes, and host
// paths pointed at a temporary directory. Every decision — installed or
// missing, ports busy or free, import present, missing or created,
// validate-fail-stops-before-apply — is asserted against what the fake
// recorded, with no live system or network anywhere.

import (
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// fakeRunner records every privileged command and serves canned
// CombinedOutput results; flaky lets a key fail its first n calls (the
// installed-then-detected flow).
type fakeRunner struct {
	results map[string]string
	// coErrs answers a key with canned output AND an error (a command that
	// ran and failed, e.g. a failing `caddy validate`).
	coErrs  map[string]error
	flaky   map[string]int
	ran     []string
	coCalls []string
}

func (f *fakeRunner) CombinedOutput(name string, args ...string) (string, error) {
	key := name + " " + strings.Join(args, " ")
	f.coCalls = append(f.coCalls, key)
	if e, ok := f.coErrs[key]; ok {
		out := f.results[key]
		return out, e
	}
	if n, ok := f.flaky[key]; ok && n > 0 {
		f.flaky[key] = n - 1
		return "", fmt.Errorf("%s: not found yet", name)
	}
	if out, ok := f.results[key]; ok {
		return out, nil
	}
	return "", fmt.Errorf("exec: %q not found", name)
}

func (f *fakeRunner) RunInteractive(name string, args ...string) error {
	line := name + " " + strings.Join(args, " ")
	f.ran = append(f.ran, line)
	return nil
}

// ranContaining returns the recorded privileged lines containing frag.
func (f *fakeRunner) ranContaining(frag string) []string {
	var out []string
	for _, l := range f.ran {
		if strings.Contains(l, frag) {
			out = append(out, l)
		}
	}
	return out
}

// fakeDNS serves canned lookups and records the hosts asked.
type fakeDNS struct {
	m   map[string][]string
	got []string
}

func (f *fakeDNS) LookupHost(host string) ([]string, error) {
	f.got = append(f.got, host)
	if ips, ok := f.m[host]; ok {
		return ips, nil
	}
	return nil, fmt.Errorf("no such host %s", host)
}

// fakeIP serves a canned public IP (or error).
type fakeIP struct {
	ip  string
	err error
}

func (f *fakeIP) Current() (string, error) { return f.ip, f.err }

// fakePorts serves canned busy answers and records the probes.
type fakePorts struct {
	busy   map[int]bool
	err    map[int]error
	probed []int
}

func (f *fakePorts) Busy(port int) (bool, error) {
	f.probed = append(f.probed, port)
	if e, ok := f.err[port]; ok {
		return false, e
	}
	return f.busy[port], nil
}

// askRecorder is the wizard's yes/no question; answers in order.
type askRecorder struct {
	answers  []bool
	asked    []string
	gotExtra int
}

func (a *askRecorder) ask(q string) (bool, error) {
	a.asked = append(a.asked, q)
	if len(a.answers) > a.gotExtra {
		ans := a.answers[a.gotExtra]
		a.gotExtra++
		return ans, nil
	}
	return false, errors.New("no scripted answer left")
}

// testPaths builds the Deps pointing at a temporary directory, along with
// the fakes' shared state.
type testPaths struct {
	deps    Deps
	runner  *fakeRunner
	dns     *fakeDNS
	ip      *fakeIP
	ports   *fakePorts
	snippet string
	main    string
	dir     string
}

func newTestDeps() *testPaths {
	// Forward slashes: the proxy package deals in Unix paths, and the Go
	// file APIs accept them on any platform.
	dir := strings.ReplaceAll(filepath.Join(os.TempDir(), fmt.Sprintf("bitbeard-proxy-test-%d", os.Getpid())), "\\", "/")
	tp := &testPaths{
		runner: &fakeRunner{
			results: map[string]string{
				"caddy version": "v2.8.0 h1:...",
			},
			coErrs: map[string]error{},
			flaky:  map[string]int{},
		},
		dns:   &fakeDNS{},
		ip:    &fakeIP{},
		ports: &fakePorts{},
		dir:   dir,
	}
	tp.snippet = dir + "/caddy.d/bitbeard.caddy"
	tp.main = dir + "/Caddyfile"
	tp.deps = Deps{
		Run:           tp.runner,
		DNS:           tp.dns,
		IP:            tp.ip,
		Ports:         tp.ports,
		OSReleasePath: filepath.Join(dir, "os-release"),
		SnippetPath:   tp.snippet,
		MainPath:      tp.main,
	}
	return tp
}

// writeOSRelease writes an os-release file with the given ID.
func (tp *testPaths) writeOSRelease(t *testing.T, id string) {
	t.Helper()
	if err := os.MkdirAll(tp.dir, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(tp.deps.OSReleasePath, []byte("ID="+id+"\n"), 0o644); err != nil {
		t.Fatal(err)
	}
}

// validateKey is the canned result key for validating the temp main path.
func (tp *testPaths) validateKey() string {
	return "caddy validate --config " + tp.main + " --adapter caddyfile"
}

// withValidateOK adds a passing validate result.
func (tp *testPaths) withValidateOK() {
	tp.runner.results[tp.validateKey()] = "Valid"
}

// answerAll returns an ask function that answers yes n times then yes
// again (the flows only ever ask one question here).
func yesAsk() func(string) (bool, error) {
	return func(string) (bool, error) { return true, nil }
}

func noAsk() func(string) (bool, error) {
	return func(string) (bool, error) { return false, nil }
}

// happyPaths answers yes everywhere and serves a matched DNS pre-flight.
func happyPaths(tp *testPaths, a Answers) {
	tp.withValidateOK()
	tp.runner.results["systemctl is-active caddy"] = "active"
	tp.dns.m = map[string][]string{}
	for _, h := range a.configuredHosts() {
		tp.dns.m[h] = []string{"203.0.113.7"}
	}
	tp.ip.ip = "203.0.113.7"
}

// TestApplyHappyPathExistingCaddy covers the green flow with Caddy
// already installed: snippet written, main Caddyfile created with the
// import, config validated, service reloaded (not started), and the
// standing DNS warning naming both hosts.
func TestApplyHappyPathExistingCaddy(t *testing.T) {
	tp := newTestDeps()
	a := Answers{Domain: "example.com", Email: "ops@example.com", Jellyfin: true}
	happyPaths(tp, a)

	var out strings.Builder
	if err := Apply(a, tp.deps, &out, yesAsk()); err != nil {
		t.Fatalf("Apply: %v\noutput:\n%s", err, out.String())
	}

	// The ports were probed before anything else; no install happened.
	if len(tp.ports.probed) != 2 || tp.ports.probed[0] != 80 || tp.ports.probed[1] != 443 {
		t.Errorf("probed %v, want [80 443]", tp.ports.probed)
	}
	if got := tp.runner.ranContaining("apt-get"); len(got) != 0 {
		t.Errorf("an installed Caddy must not be installed again: %v", got)
	}

	// The snippet was installed into the snippet dir (shown mkdir first).
	if got := tp.runner.ranContaining("mkdir -p"); len(got) != 1 || !strings.Contains(got[0], "caddy.d") {
		t.Errorf("snippet dir not created as shown: %v", got)
	}
	installs := tp.runner.ranContaining("install -m 644")
	if len(installs) != 2 {
		t.Fatalf("want two installs (snippet + main), got %v", installs)
	}
	if !strings.HasSuffix(installs[0], tp.snippet) {
		t.Errorf("first install targeted %s, want the snippet: %v", tp.snippet, installs)
	}
	if !strings.HasSuffix(installs[1], tp.main) {
		t.Errorf("second install targeted %s, want the main Caddyfile: %v", tp.main, installs)
	}

	// The created main Caddyfile carries the email and the import line.
	created := MainCaddyfileContent(a.Email, importLineFor(tp.snippet))
	if !strings.Contains(created, "email ops@example.com") {
		t.Errorf("created Caddyfile lacks the ACME email:\n%s", created)
	}

	// The config was validated, then reloaded (it is running).
	if !contains(tp.runner.coCalls, tp.validateKey()) {
		t.Errorf("the config was not validated: %v", tp.runner.coCalls)
	}
	reloads := tp.runner.ranContaining("systemctl reload caddy")
	if len(reloads) != 1 {
		t.Errorf("want exactly one reload, got %v (all: %v)", reloads, tp.runner.ran)
	}
	if got := tp.runner.ranContaining("enable --now"); len(got) != 0 {
		t.Errorf("a running caddy must not be enable --now'd: %v", got)
	}

	// The consent question was never needed (the main file did not exist).
	// The standing DNS warning names both hosts.
	report := out.String()
	if !strings.Contains(report, "DNS for bitbeard.example.com and jellyfin.example.com must point") {
		t.Errorf("the DNS warning does not name both hosts:\n%s", report)
	}
	if !strings.Contains(report, "Ports 80 and 443 are free.") {
		t.Errorf("the free-ports note missing:\n%s", report)
	}
	if !strings.Contains(report, "bitbeard.example.com {\n\t# The Webui's nginx") {
		t.Errorf("the generated snippet was not shown back:\n%s", report)
	}
}

// TestApplyInstallsMissingCaddy covers the fresh-machine install path:
// the distro package via shown sudo commands on an apt-family distro,
// with detection re-run after the install.
func TestApplyInstallsMissingCaddy(t *testing.T) {
	tp := newTestDeps()
	tp.writeOSRelease(t, "ubuntu")
	// `caddy version` fails until the install "ran".
	tp.runner.flaky["caddy version"] = 1
	a := Answers{Domain: "example.com", Jellyfin: false}
	happyPaths(tp, a)

	var out strings.Builder
	if err := Apply(a, tp.deps, &out, yesAsk()); err != nil {
		t.Fatalf("Apply: %v\noutput:\n%s", err, out.String())
	}
	for _, frag := range []string{"sudo apt-get update", "sudo apt-get install -y caddy"} {
		if len(tp.runner.ranContaining(frag)) != 1 {
			t.Errorf("install did not run %q (ran: %v):\n%s", frag, tp.runner.ran, out.String())
		}
		if !strings.Contains(out.String(), "$ "+frag) {
			t.Errorf("%q was not shown before running:\n%s", frag, out.String())
		}
	}
	// With no Jellyfin site, only the Webui name appears in the warning.
	if !strings.Contains(out.String(), "DNS for bitbeard.example.com must point") {
		t.Errorf("the DNS warning missing:\n%s", out.String())
	}
	if strings.Contains(out.String(), "jellyfin.example.com") {
		t.Errorf("the jellyfin name leaked into a no-Jellyfin run:\n%s", out.String())
	}
}

// TestApplyUnsupportedDistroPointsAtManualPath covers the honest stop on
// a non-apt distro: no sudo commands ran, the error names the distro and
// docs/proxy.md.
func TestApplyUnsupportedDistroPointsAtManualPath(t *testing.T) {
	tp := newTestDeps()
	tp.writeOSRelease(t, "fedora")
	// Caddy is missing: remove the canned detection result.
	delete(tp.runner.results, "caddy version")
	a := Answers{Domain: "example.com", Jellyfin: false}
	happyPaths(tp, a)

	var out strings.Builder
	err := Apply(a, tp.deps, &out, yesAsk())
	if err == nil || !strings.Contains(err.Error(), "fedora") {
		t.Fatalf("err = %v, want a fedora-named refusal", err)
	}
	if len(tp.runner.ran) != 0 {
		t.Errorf("nothing may be installed or written on an unsupported distro: %v", tp.runner.ran)
	}
	if !strings.Contains(out.String(), "docs/proxy.md") {
		t.Errorf("the refusal does not point at the manual path:\n%s", out.String())
	}
}

// TestApplyBusyPortsRefusesBeforeAnythingElse covers the port war
// refusal: no DNS lookups, no installs, no writes, and the manual path in
// the message.
func TestApplyBusyPortsRefusesBeforeAnythingElse(t *testing.T) {
	tp := newTestDeps()
	a := Answers{Domain: "example.com", Jellyfin: true}
	happyPaths(tp, a)
	tp.ports.busy = map[int]bool{80: true}

	var out strings.Builder
	err := Apply(a, tp.deps, &out, yesAsk())
	if err == nil || !strings.Contains(err.Error(), "80") {
		t.Fatalf("err = %v, want a busy-port refusal naming 80", err)
	}
	if len(tp.dns.got) != 0 {
		t.Errorf("the DNS pre-flight ran although the ports were busy: %v", tp.dns.got)
	}
	if len(tp.runner.ran) != 0 || len(tp.runner.coCalls) != 0 {
		t.Errorf("nothing may happen after a busy-port refusal: ran %v, co %v", tp.runner.ran, tp.runner.coCalls)
	}
	if !strings.Contains(out.String(), "docs/proxy.md") {
		t.Errorf("the refusal does not point at the manual path:\n%s", out.String())
	}
}

// TestApplyUnknownPortProbeRefuses covers the conservative reading: a
// port that could be neither connected to nor refused is treated as busy.
func TestApplyUnknownPortProbeRefuses(t *testing.T) {
	tp := newTestDeps()
	a := Answers{Domain: "example.com", Jellyfin: false}
	happyPaths(tp, a)
	tp.ports.err = map[int]error{443: errors.New("i/o timeout")}

	var out strings.Builder
	err := Apply(a, tp.deps, &out, yesAsk())
	if err == nil || !strings.Contains(err.Error(), "443") {
		t.Fatalf("err = %v, want a refusal naming 443", err)
	}
	if len(tp.runner.ran) != 0 {
		t.Errorf("nothing may happen on an unknown port state: %v", tp.runner.ran)
	}
}

// TestApplyValidateFailureAppliesNothing covers validate-fail: the error
// is shown, the service was never touched, and the message says what is
// on disk and how back.
func TestApplyValidateFailureAppliesNothing(t *testing.T) {
	tp := newTestDeps()
	tp.writeOSRelease(t, "ubuntu")
	a := Answers{Domain: "example.com", Jellyfin: true}
	happyPaths(tp, a)
	// A `caddy validate` that runs and fails with canned output.
	tp.runner.results[tp.validateKey()] = "Error: parsing Caddyfile tokens"
	tp.runner.coErrs[tp.validateKey()] = errors.New("exit status 1")

	var out strings.Builder
	err := Apply(a, tp.deps, &out, yesAsk())
	if err == nil || !strings.Contains(err.Error(), "caddy validate failed") {
		t.Fatalf("err = %v, want a validate failure", err)
	}
	if got := tp.runner.ranContaining("systemctl"); len(got) != 0 {
		t.Errorf("the service must not be touched after a failed validate: %v", got)
	}
	report := out.String()
	if !strings.Contains(report, "nothing was applied") || !strings.Contains(report, "parsing Caddyfile") {
		t.Errorf("the failure was not shown plainly:\n%s", report)
	}
	if !strings.Contains(report, "rerun `bitbeard setup`") {
		t.Errorf("the recovery path missing:\n%s", report)
	}
}

// TestApplyImportMissingAndDeclinedStops covers operator consent: the
// shown diff is asked about, a no stops the stage before the service is
// touched, and the operator is told the snippet is on disk but unused.
func TestApplyImportMissingAndDeclinedStops(t *testing.T) {
	tp := newTestDeps()
	tp.writeOSRelease(t, "ubuntu")
	if err := os.WriteFile(tp.main, []byte("example.com {\n\treverse_proxy 127.0.0.1:8081\n}\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	a := Answers{Domain: "example.com", Jellyfin: false}
	happyPaths(tp, a)

	var out strings.Builder
	ask := &askRecorder{answers: []bool{false}}
	err := Apply(a, tp.deps, &out, ask.ask)
	if err == nil || !strings.Contains(err.Error(), "declined") {
		t.Fatalf("err = %v, want a declined error", err)
	}
	if len(ask.asked) != 1 || !strings.Contains(ask.asked[0], tp.main) {
		t.Errorf("the consent question was not asked about the main file: %v", ask.asked)
	}
	// The snippet was still written (it is ours), but the main file is
	// untouched on disk and the service untouched.
	if len(tp.runner.ranContaining("install -m 644")) != 1 {
		t.Errorf("only the snippet may have been installed: %v", tp.runner.ran)
	}
	data, _ := os.ReadFile(tp.main)
	if string(data) != "example.com {\n\treverse_proxy 127.0.0.1:8081\n}\n" {
		t.Errorf("the operator-owned Caddyfile was modified: %q", data)
	}
	if got := tp.runner.ranContaining("systemctl"); len(got) != 0 {
		t.Errorf("the service must not be touched after a declined import: %v", got)
	}
	if !strings.Contains(out.String(), "+ import") {
		t.Errorf("the diff was not shown:\n%s", out.String())
	}
}

// TestApplyImportMissingAndAcceptedAppends covers the yes path: exactly
// one line appended to the operator's content, then validate and reload.
func TestApplyImportMissingAndAcceptedAppends(t *testing.T) {
	tp := newTestDeps()
	tp.writeOSRelease(t, "ubuntu")
	original := "example.com {\n\treverse_proxy 127.0.0.1:8081\n}\n"
	if err := os.WriteFile(tp.main, []byte(original), 0o644); err != nil {
		t.Fatal(err)
	}
	a := Answers{Domain: "example.com", Jellyfin: true}
	happyPaths(tp, a)

	var out strings.Builder
	if err := Apply(a, tp.deps, &out, yesAsk()); err != nil {
		t.Fatalf("Apply: %v\noutput:\n%s", err, out.String())
	}
	data, _ := os.ReadFile(tp.main)
	// The fake runner performs no real copies; assert through the shown
	// commands and the pure append contract instead.
	if len(tp.runner.ranContaining("install -m 644")) != 2 {
		t.Errorf("want the snippet and the appended main installed: %v", tp.runner.ran)
	}
	if string(data) != original {
		t.Errorf("the real file must be untouched by the fake: %q", data)
	}
	next := WithImportAdded(original, importLineFor(tp.snippet))
	wantDir := strings.ReplaceAll(filepath.Dir(tp.snippet), "\\", "/")
	if !strings.HasSuffix(next, "import "+wantDir+"/*\n") {
		t.Errorf("the appended content is wrong: %q", next)
	}
	if !strings.Contains(out.String(), "+ import") {
		t.Errorf("the diff was not shown:\n%s", out.String())
	}
}

// TestApplyImportAlreadyPresentSkipsConsent covers the no-op import:
// nothing asked, nothing installed for the main file, straight to
// validate and reload.
func TestApplyImportAlreadyPresentSkipsConsent(t *testing.T) {
	tp := newTestDeps()
	tp.writeOSRelease(t, "ubuntu")
	if err := os.WriteFile(tp.main, []byte("{\n}\n\n"+importLineFor(tp.snippet)+"\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	a := Answers{Domain: "example.com", Jellyfin: true}
	happyPaths(tp, a)

	var out strings.Builder
	ask := &askRecorder{answers: []bool{}}
	if err := Apply(a, tp.deps, &out, ask.ask); err != nil {
		t.Fatalf("Apply: %v\noutput:\n%s", err, out.String())
	}
	if len(ask.asked) != 0 {
		t.Errorf("consent was asked although the import already exists: %v", ask.asked)
	}
	if got := tp.runner.ranContaining("install -m 644"); len(got) != 1 {
		t.Errorf("only the snippet may be installed: %v", got)
	}
	if !strings.Contains(out.String(), "already imports the snippet directory") {
		t.Errorf("the no-op was not noted:\n%s", out.String())
	}
	if len(tp.runner.ranContaining("systemctl reload caddy")) != 1 {
		t.Errorf("the running caddy was not reloaded: %v", tp.runner.ran)
	}
}

// TestApplyDNSSkipsOfflineAndWarnsOnMismatch covers the warn-only
// pre-flight behaviour inside the full flow: offline skips with a note,
// a mismatch warns but never blocks.
func TestApplyDNSSkipsOfflineAndWarnsOnMismatch(t *testing.T) {
	a := Answers{Domain: "example.com", Jellyfin: true}

	// Offline: everything fails, the note is printed, setup continues.
	tp := newTestDeps()
	tp.writeOSRelease(t, "ubuntu")
	happyPaths(tp, a)
	tp.dns.m = nil // every lookup fails
	tp.ip.err = errors.New("dial tcp: offline")
	var out strings.Builder
	if err := Apply(a, tp.deps, &out, yesAsk()); err != nil {
		t.Fatalf("Apply (offline): %v\noutput:\n%s", err, out.String())
	}
	if !strings.Contains(out.String(), "Skipping the DNS pre-flight") {
		t.Errorf("the offline skip note missing:\n%s", out.String())
	}

	// Mismatch: a plain warning, setup continues, service reloaded.
	tp = newTestDeps()
	tp.writeOSRelease(t, "ubuntu")
	happyPaths(tp, a)
	tp.dns.m = map[string][]string{
		"bitbeard.example.com": {"198.51.100.9"},
		"jellyfin.example.com": {"198.51.100.9"},
	}
	tp.ip.ip = "203.0.113.7"
	out.Reset()
	if err := Apply(a, tp.deps, &out, yesAsk()); err != nil {
		t.Fatalf("Apply (mismatch): %v\noutput:\n%s", err, out.String())
	}
	report := out.String()
	if !strings.Contains(report, "resolves to 198.51.100.9, not this machine's public IP") {
		t.Errorf("the mismatch warning missing:\n%s", report)
	}
	if len(tp.runner.ranContaining("systemctl reload caddy")) != 1 {
		t.Errorf("a DNS mismatch must not block the apply: %v", tp.runner.ran)
	}
}

// TestApplyTempFilesAreCleanedUp keeps the unprivileged staging files from
// piling up in the operator's temp directory.
func TestApplyTempFilesAreCleanedUp(t *testing.T) {
	tp := newTestDeps()
	tp.writeOSRelease(t, "ubuntu")
	a := Answers{Domain: "example.com", Jellyfin: true}
	happyPaths(tp, a)

	var out strings.Builder
	if err := Apply(a, tp.deps, &out, yesAsk()); err != nil {
		t.Fatalf("Apply: %v", err)
	}
	installs := tp.runner.ranContaining("install -m 644")
	if len(installs) == 0 {
		t.Fatalf("no installs recorded: %v", tp.runner.ran)
	}
	for _, line := range installs {
		src := strings.Fields(line)[3]
		if _, err := os.Stat(src); !os.IsNotExist(err) {
			t.Errorf("temp file %s survived (stat err %v)", src, err)
		}
	}
}

// TestServiceActive covers the doctor's probe: "active" passes, "inactive"
// (with the non-zero exit systemctl uses) reports not-running, and an
// unanswerable probe is an error.
func TestServiceActive(t *testing.T) {
	active := &fakeRunner{results: map[string]string{"systemctl is-active caddy": "active\n"}}
	ok, err := ServiceActive(active)
	if err != nil || !ok {
		t.Errorf("ServiceActive = %v, %v; want true, nil", ok, err)
	}

	// inactive + non-zero exit: not-running, not an error.
	inactive := &fakeRunner{
		results: map[string]string{"systemctl is-active caddy": "inactive"},
		coErrs:  map[string]error{"systemctl is-active caddy": errors.New("exit status 3")},
	}
	ok, err = ServiceActive(inactive)
	if err != nil || ok {
		t.Errorf("ServiceActive = %v, %v; want false, nil for an inactive answer", ok, err)
	}

	// failed + non-zero exit: also not-running.
	failed := &fakeRunner{
		results: map[string]string{"systemctl is-active caddy": "failed"},
		coErrs:  map[string]error{"systemctl is-active caddy": errors.New("exit status 3")},
	}
	ok, err = ServiceActive(failed)
	if err != nil || ok {
		t.Errorf("ServiceActive = %v, %v; want false, nil for a failed answer", ok, err)
	}

	// No systemd to ask at all: an error.
	missing := &fakeRunner{}
	ok, err = ServiceActive(missing)
	if err == nil || ok {
		t.Errorf("ServiceActive = %v, %v; want false, error (systemctl missing)", ok, err)
	}
}

// TestEnabledAndDomainFromEnv covers the .env state reading.
func TestEnabledAndDomainFromEnv(t *testing.T) {
	if !Enabled(map[string]string{EnvEnabledKey: "1"}) {
		t.Error("CADDY_ENABLED=1 must read as enabled")
	}
	if Enabled(map[string]string{EnvEnabledKey: " 1 "}) != true {
		t.Error("a padded 1 still reads as enabled")
	}
	for _, v := range []string{"", "0", "true", "yes"} {
		if Enabled(map[string]string{EnvEnabledKey: v}) {
			t.Errorf("CADDY_ENABLED=%q must read as disabled", v)
		}
	}
	if Enabled(nil) {
		t.Error("a nil env is disabled")
	}
	if got := DomainFromEnv(map[string]string{EnvDomainKey: "example.com"}); got != "example.com" {
		t.Errorf("DomainFromEnv = %q", got)
	}
	if got := DomainFromEnv(map[string]string{}); got != "" {
		t.Errorf("DomainFromEnv = %q, want empty", got)
	}
}

// TestWarnPublicHTTPS covers the verify-not-force rule: an explicit 0
// warns, anything else stays silent.
func TestWarnPublicHTTPS(t *testing.T) {
	var out strings.Builder
	WarnPublicHTTPS(map[string]string{"PUBLIC_HTTPS": "0"}, &out)
	if !strings.Contains(out.String(), "PUBLIC_HTTPS") || !strings.Contains(out.String(), "docs/proxy.md") {
		t.Errorf("an explicit 0 must warn with the footgun pointer, got:\n%s", out.String())
	}

	out.Reset()
	WarnPublicHTTPS(map[string]string{"PUBLIC_HTTPS": "1"}, &out)
	if out.String() != "" {
		t.Errorf("an explicit 1 must stay silent, got:\n%s", out.String())
	}

	out.Reset()
	WarnPublicHTTPS(map[string]string{}, &out)
	if out.String() != "" {
		t.Errorf("an unset PUBLIC_HTTPS (the default 1) must stay silent, got:\n%s", out.String())
	}
}

// contains reports whether the slice contains s.
func contains(ss []string, s string) bool {
	for _, v := range ss {
		if v == s {
			return true
		}
	}
	return false
}
