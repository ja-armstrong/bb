package doctor

// doctor_test.go tests every check against fakes: a fake Compose that
// records Up calls (doctor must never make any), a fake Prober serving
// canned HTTP statuses, and a fake prereqs.Runner serving canned detection.
// Stdlib only.

import (
	"errors"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/ja-armstrong/bb/internal/prereqs"
	"github.com/ja-armstrong/bb/internal/wizard"
)

// fakeCompose is the wizard.Compose boundary. Up is recorded so tests can
// assert the doctor never starts anything.
type fakeCompose struct {
	ps      string
	psErr   error
	upCalls int
}

func (f *fakeCompose) Up(dir string) error {
	f.upCalls++
	return errors.New("Up must never be called by the doctor")
}

func (f *fakeCompose) PS(dir string) (string, error) {
	return f.ps, f.psErr
}

// fakeProber is the Prober boundary: canned statuses and errors per URL,
// with every probed URL recorded.
type fakeProber struct {
	status map[string]int
	err    map[string]error
	probed []string
}

func (f *fakeProber) Get(url string) (int, error) {
	f.probed = append(f.probed, url)
	if e, ok := f.err[url]; ok {
		return 0, e
	}
	if s, ok := f.status[url]; ok {
		return s, nil
	}
	return http.StatusOK, nil
}

// fakeRunner is the prereqs.Runner boundary for detection: canned results
// per command, with RunInteractive recorded (detection must never escalate).
type fakeRunner struct {
	results   map[string]string // "docker --version" -> combined output
	sudoCalls int
}

func (f *fakeRunner) CombinedOutput(name string, args ...string) (string, error) {
	key := name + " " + strings.Join(args, " ")
	if _, ok := f.results[key]; ok {
		return f.results[key], nil
	}
	return "", fmt.Errorf("exec: %q not found", name)
}

func (f *fakeRunner) RunInteractive(name string, args ...string) error {
	f.sudoCalls++
	return errors.New("RunInteractive must never be called by the doctor")
}

// dockerOK returns a Runner fake with Docker and Docker Compose present.
func dockerOK() *fakeRunner {
	return &fakeRunner{results: map[string]string{
		"docker --version":         "Docker version 27.0.0",
		"docker compose version":   "Docker Compose version v2.27.0",
		"docker-compose --version": "Docker Compose version v2.27.0",
	}}
}

// psHealthy is `docker compose ps --format json` output for a fully
// healthy mandatory stack, as JSON lines.
const psHealthy = `{"Name":"/gluetun","State":"running","ExitCode":0}
{"Name":"/init-configs","State":"exited","ExitCode":0}
{"Name":"/qbittorrent","State":"running","ExitCode":0}
{"Name":"/jackett","State":"running","ExitCode":0}
{"Name":"/api","State":"running","ExitCode":0}
{"Name":"/manager","State":"running","ExitCode":0}
{"Name":"/webui","State":"running","ExitCode":0}
`

// validEnv is a .env body that passes parse and every guardrail.
const validEnv = `VPN_PROVIDER=nordvpn
OPENVPN_USER=u
OPENVPN_PASSWORD=p
TZ=UTC
SERVICE_TOKEN=tok
ADMIN_USER=admin
ADMIN_PASSWORD=pw
DEFAULT_QUOTA_GB=50
SESSION_TTL_HOURS=24
BASE_DOWNLOAD_DIR=/media
DATABASE_PATH=/data/bitbeard.db
`

// setupStack writes a minimal stack directory: bitbeard/docker-compose.yml,
// a valid .env, and a .gitignore covering .env.
func setupStack(t *testing.T) string {
	t.Helper()
	stack := t.TempDir()
	composeDir := filepath.Join(stack, "bitbeard")
	if err := writeFile(filepath.Join(composeDir, "docker-compose.yml"), "services: {}\n"); err != nil {
		t.Fatal(err)
	}
	if err := writeFile(filepath.Join(composeDir, ".env"), validEnv); err != nil {
		t.Fatal(err)
	}
	if err := writeFile(filepath.Join(composeDir, ".gitignore"), "/.env\n"); err != nil {
		t.Fatal(err)
	}
	return stack
}

// writeFile creates the file with parent directories.
func writeFile(path, content string) error {
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		return err
	}
	return os.WriteFile(path, []byte(content), 0o644)
}

// runDoctor assembles Options from the given fakes and runs the doctor,
// returning the Result and the report.
func runDoctor(t *testing.T, stackDir string, comp *fakeCompose, prober *fakeProber, runner *fakeRunner) (Result, string) {
	t.Helper()
	var out strings.Builder
	res := Run(Options{
		StackDir: stackDir,
		Compose:  comp,
		Prober:   prober,
		Runner:   runner,
		Out:      &out,
	})
	return res, out.String()
}

// findCheck returns the check with the given name.
func findCheck(t *testing.T, res Result, name string) Check {
	t.Helper()
	for _, c := range res.Checks {
		if c.Name == name {
			return c
		}
	}
	t.Fatalf("no check named %q in %+v", name, res.Checks)
	return Check{}
}

// assertPass fails the test unless the named check passed.
func assertPass(t *testing.T, res Result, name string) {
	t.Helper()
	if c := findCheck(t, res, name); !c.Pass {
		t.Errorf("check %q failed: %+v", name, c)
	}
}

// assertFail fails the test unless the named check failed and its detail
// and fix are set (a failure must name the problem and point at the fix).
func assertFail(t *testing.T, res Result, name, wantInDetail, wantInFix string) {
	t.Helper()
	c := findCheck(t, res, name)
	if c.Pass {
		t.Fatalf("check %q passed, want failure: %+v", name, c)
	}
	if wantInDetail != "" && !strings.Contains(c.Detail, wantInDetail) {
		t.Errorf("check %q detail %q does not mention %q", name, c.Detail, wantInDetail)
	}
	if wantInFix != "" && !strings.Contains(c.Fix, wantInFix) {
		t.Errorf("check %q fix %q does not mention %q", name, c.Fix, wantInFix)
	}
}

// TestRunHealthyStack is the all-green case: every check passes, the report
// says so plainly, and not a single mutating call was made.
func TestRunHealthyStack(t *testing.T) {
	stack := setupStack(t)
	comp := &fakeCompose{ps: psHealthy}
	prober := &fakeProber{status: map[string]int{WebUIURL: 200, APIHealthURL: 200}}
	runner := dockerOK()

	res, report := runDoctor(t, stack, comp, prober, runner)

	if res.Failed {
		t.Errorf("healthy stack reported Failed; report:\n%s", report)
	}
	for _, c := range res.Checks {
		if !c.Pass {
			t.Errorf("check %q failed on a healthy stack: %+v", c.Name, c)
		}
	}
	// Check-only, always: no compose up, no privileged command.
	if comp.upCalls != 0 {
		t.Errorf("doctor called Compose.Up %d times", comp.upCalls)
	}
	if runner.sudoCalls != 0 {
		t.Errorf("doctor ran %d privileged commands", runner.sudoCalls)
	}
	// Exactly the two reachability probes, both GETs (the Prober interface
	// has no other method).
	if len(prober.probed) != 2 || prober.probed[0] != WebUIURL || prober.probed[1] != APIHealthURL {
		t.Errorf("probed %v, want [%s %s]", prober.probed, WebUIURL, APIHealthURL)
	}
	// The report closes with a plain summary.
	if !strings.Contains(report, "Summary: all 14 checks passed") {
		t.Errorf("report lacks the healthy summary; report:\n%s", report)
	}
}

// TestRunPrereqDetection covers the detect-only prerequisite checks.
func TestRunPrereqDetection(t *testing.T) {
	stack := setupStack(t)
	comp := &fakeCompose{ps: psHealthy}
	prober := &fakeProber{}
	runner := &fakeRunner{} // nothing found

	res, report := runDoctor(t, stack, comp, prober, runner)
	if !res.Failed {
		t.Error("missing prerequisites must fail the run")
	}
	assertFail(t, res, "Docker", "not found", "bitbeard setup")
	assertFail(t, res, "Docker Compose", "not found", "bitbeard setup")
	assertPass(t, res, ".env guardrails")
	if !strings.Contains(report, "Summary: 12 of 14 checks passed; 2 failed") {
		t.Errorf("wrong summary; report:\n%s", report)
	}
	if runner.sudoCalls != 0 {
		t.Errorf("detection must not escalate: %d privileged calls", runner.sudoCalls)
	}

	// Docker present, compose plugin missing, standalone missing.
	half := &fakeRunner{results: map[string]string{"docker --version": "Docker version 27.0.0"}}
	res, _ = runDoctor(t, stack, comp, prober, half)
	assertPass(t, res, "Docker")
	assertFail(t, res, "Docker Compose", "not found", "bitbeard setup")
}

// TestRunEnvGuardrails covers .env: healthy, missing, unparseable, missing
// keys, change-me placeholders, and the gitignore probe.
func TestRunEnvGuardrails(t *testing.T) {
	stack := setupStack(t)
	comp := &fakeCompose{ps: psHealthy}
	prober := &fakeProber{}
	runner := dockerOK()

	// change-me values fail the guardrail with the keys named; an empty
	// required key is named too.
	env := strings.Replace(validEnv, "SERVICE_TOKEN=tok", "SERVICE_TOKEN=change-me-to-a-long-random-token", 1)
	env = strings.Replace(env, "ADMIN_PASSWORD=pw", "ADMIN_PASSWORD=", 1)
	env += "JACKETT_API_KEY=CHANGE-ME\n"
	if err := writeFile(filepath.Join(stack, "bitbeard", ".env"), env); err != nil {
		t.Fatal(err)
	}
	res, _ := runDoctor(t, stack, comp, prober, runner)
	assertPass(t, res, ".env")
	assertFail(t, res, ".env guardrails", "SERVICE_TOKEN still carries its change-me placeholder", "bitbeard setup")
	assertFail(t, res, ".env guardrails", "JACKETT_API_KEY", "")
	// The empty required key must also be named.
	c := findCheck(t, res, ".env guardrails")
	if !strings.Contains(c.Detail, "ADMIN_PASSWORD is empty") {
		t.Errorf("guardrail detail does not name the empty ADMIN_PASSWORD: %+v", c)
	}

	// A missing .env fails once, pointing at setup; no guardrail check runs.
	stack2 := t.TempDir()
	if err := writeFile(filepath.Join(stack2, "bitbeard", "docker-compose.yml"), "services: {}\n"); err != nil {
		t.Fatal(err)
	}
	res, _ = runDoctor(t, stack2, comp, prober, runner)
	assertFail(t, res, ".env", "not found", "bitbeard setup")
	for _, c := range res.Checks {
		if c.Name == ".env guardrails" {
			t.Error("no guardrail check should run without a .env")
		}
	}

	// An unparseable .env fails with the parse error.
	stack3 := setupStack(t)
	if err := writeFile(filepath.Join(stack3, "bitbeard", ".env"), "not a key value line\n"); err != nil {
		t.Fatal(err)
	}
	res, _ = runDoctor(t, stack3, comp, prober, runner)
	assertFail(t, res, ".env", "could not be parsed", "bitbeard setup")

	// A .env not covered by .gitignore fails the gitignore check; the fix
	// points at the file and the doctor still changes nothing itself.
	stack4 := setupStack(t)
	if err := writeFile(filepath.Join(stack4, "bitbeard", ".gitignore"), "node_modules\n"); err != nil {
		t.Fatal(err)
	}
	res, _ = runDoctor(t, stack4, comp, prober, runner)
	assertFail(t, res, ".env is gitignored", ".gitignore", "changes nothing")

	// The custom VPN provider does not need OpenVPN credentials.
	stack5 := setupStack(t)
	custom := strings.Replace(validEnv, "VPN_PROVIDER=nordvpn", "VPN_PROVIDER=custom", 1)
	custom = strings.Replace(custom, "OPENVPN_USER=u\n", "", 1)
	custom = strings.Replace(custom, "OPENVPN_PASSWORD=p\n", "", 1)
	if err := writeFile(filepath.Join(stack5, "bitbeard", ".env"), custom); err != nil {
		t.Fatal(err)
	}
	res, _ = runDoctor(t, stack5, comp, prober, runner)
	assertPass(t, res, ".env guardrails")
}

// TestRunStackHealth covers the container checks: missing containers, wrong
// states, the gluetun hint, and the init container semantics.
func TestRunStackHealth(t *testing.T) {
	stack := setupStack(t)
	prober := &fakeProber{}
	runner := dockerOK()

	// gluetun restarting (the VPN-credentials failure mode) and qbittorrent
	// missing entirely.
	ps := strings.Replace(psHealthy, `{"Name":"/gluetun","State":"running","ExitCode":0}`,
		`{"Name":"/gluetun","State":"restarting","ExitCode":0}`, 1)
	ps = strings.Replace(ps, `{"Name":"/qbittorrent","State":"running","ExitCode":0}`+"\n", "", 1)
	comp := &fakeCompose{ps: ps}
	res, _ := runDoctor(t, stack, comp, prober, runner)
	assertFail(t, res, "gluetun", "is restarting (expected running)", "VPN credentials")
	assertFail(t, res, "qbittorrent", "is not part of the running stack", "docker compose up -d")
	assertPass(t, res, "jackett")

	// A stopped container names the state and the logs command.
	ps = strings.Replace(psHealthy, `{"Name":"/api","State":"running","ExitCode":0}`,
		`{"Name":"/api","State":"exited","ExitCode":137}`, 1)
	comp = &fakeCompose{ps: ps}
	res, _ = runDoctor(t, stack, comp, prober, runner)
	assertFail(t, res, "api", "is exited (expected running)", "docker compose logs api")

	// init-configs: exited 0, running (first boot), absent, and failed.
	for _, tc := range []struct {
		name string
		ps   string
		pass bool
		det  string
	}{
		{"exited 0", strings.Replace(psHealthy, `{"Name":"/init-configs","State":"exited","ExitCode":0}`, `{"Name":"/init-configs","State":"exited","ExitCode":0}`, 1), true, "exited 0"},
		{"still running", strings.Replace(psHealthy, `{"Name":"/init-configs","State":"exited","ExitCode":0}`, `{"Name":"/init-configs","State":"running","ExitCode":0}`, 1), true, "first boot"},
		{"absent", strings.Replace(psHealthy, `{"Name":"/init-configs","State":"exited","ExitCode":0}`+"\n", "", 1), true, "cleaned up"},
		{"failed", strings.Replace(psHealthy, `{"Name":"/init-configs","State":"exited","ExitCode":0}`, `{"Name":"/init-configs","State":"exited","ExitCode":1}`, 1), false, "exit code 1"},
	} {
		comp = &fakeCompose{ps: tc.ps}
		res, _ = runDoctor(t, stack, comp, prober, runner)
		c := findCheck(t, res, "init-configs")
		if c.Pass != tc.pass {
			t.Errorf("init-configs %s: pass = %v, want %v (%+v)", tc.name, c.Pass, tc.pass, c)
		}
		if tc.pass && !strings.Contains(c.Detail, tc.det) {
			t.Errorf("init-configs %s: detail %q lacks %q", tc.name, c.Detail, tc.det)
		}
		if !tc.pass {
			assertFail(t, res, "init-configs", "expected a clean exit 0", "docker compose up init-configs")
		}
	}

	// docker compose ps failing is reported once as "Running stack".
	comp = &fakeCompose{psErr: errors.New("cannot connect to the Docker daemon")}
	res, _ = runDoctor(t, stack, comp, prober, runner)
	assertFail(t, res, "Running stack", "cannot connect to the Docker daemon", "Docker daemon")
	for _, name := range wizard.MandatorySubsystemNames() {
		for _, c := range res.Checks {
			if c.Name == name {
				t.Errorf("no container check for %q should run when ps fails", name)
			}
		}
	}

	// Garbage ps output is reported, not crashed on.
	comp = &fakeCompose{ps: "not json"}
	res, _ = runDoctor(t, stack, comp, prober, runner)
	assertFail(t, res, "Running stack", "could not read the docker compose ps output", "bitbeard doctor")
}

// TestRunReachability covers the Webui and API probes.
func TestRunReachability(t *testing.T) {
	stack := setupStack(t)
	comp := &fakeCompose{ps: psHealthy}
	runner := dockerOK()

	// Webui answers but with the wrong status; API unreachable (transport
	// error) — each failure names its URL and the fix.
	prober := &fakeProber{
		status: map[string]int{WebUIURL: http.StatusBadGateway},
		err:    map[string]error{APIHealthURL: errors.New("connection refused")},
	}
	res, _ := runDoctor(t, stack, comp, prober, runner)
	assertFail(t, res, "Webui answers on host port 8081", "answered 502", "docker compose logs webui")
	assertFail(t, res, "API answers (GET /api/health through the Webui proxy)", "connection refused", "docker compose logs api")

	// Both green.
	prober = &fakeProber{}
	res, _ = runDoctor(t, stack, comp, prober, runner)
	assertPass(t, res, "Webui answers on host port 8081")
	assertPass(t, res, "API answers (GET /api/health through the Webui proxy)")
}

// TestRunMissingStackDirectory covers the early-out: no docker-compose.yml
// anywhere under the stack dir fails once and runs no dependent checks.
func TestRunMissingStackDirectory(t *testing.T) {
	stack := t.TempDir() // empty
	comp := &fakeCompose{ps: psHealthy}
	prober := &fakeProber{}
	runner := dockerOK()

	res, _ := runDoctor(t, stack, comp, prober, runner)
	if !res.Failed {
		t.Error("an empty stack directory must fail the run")
	}
	assertFail(t, res, "Stack directory", "no docker-compose.yml", "bitbeard setup")
	for _, c := range res.Checks {
		switch c.Name {
		case ".env", ".env guardrails", "gluetun", "Webui answers on host port 8081":
			t.Errorf("dependent check %q should not run without a stack directory", c.Name)
		}
	}
	if comp.upCalls != 0 {
		t.Errorf("doctor called Compose.Up %d times", comp.upCalls)
	}
}

// TestSummaryAndExitSignal covers the summary counts and the Failed flag
// the caller turns into a non-zero exit.
func TestSummaryAndExitSignal(t *testing.T) {
	stack := setupStack(t)
	comp := &fakeCompose{ps: psHealthy}
	prober := &fakeProber{}
	runner := dockerOK()

	res, report := runDoctor(t, stack, comp, prober, runner)
	if res.Failed {
		t.Error("healthy run must not set Failed")
	}
	if !strings.Contains(report, "Summary: all 14 checks passed. The stack looks healthy.") {
		t.Errorf("healthy summary wrong; report:\n%s", report)
	}

	// One failure flips Failed and the summary counts it.
	prober = &fakeProber{status: map[string]int{WebUIURL: http.StatusServiceUnavailable}}
	res, report = runDoctor(t, stack, comp, prober, runner)
	if !res.Failed {
		t.Error("a failed check must set Failed")
	}
	if !strings.Contains(report, "Summary: 13 of 14 checks passed; 1 failed") {
		t.Errorf("failing summary wrong; report:\n%s", report)
	}
	if !strings.Contains(report, "rerun `bitbeard doctor` to confirm") {
		t.Errorf("failing summary should point back at the doctor; report:\n%s", report)
	}
}

// TestFindStackDir covers discovery: explicit wins, state file next, home
// default last.
func TestFindStackDir(t *testing.T) {
	if got := FindStackDir("/explicit"); got != "/explicit" {
		t.Errorf("FindStackDir explicit = %q, want /explicit", got)
	}
	// Without an explicit dir the discovery paths depend on the machine's
	// home; both branches must produce an absolute, non-empty path.
	got := FindStackDir("")
	if !filepath.IsAbs(got) {
		t.Errorf("FindStackDir discovered %q, want an absolute path", got)
	}
}

// TestHTTPProber covers the production Prober against a real local server.
func TestHTTPProber(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet {
			t.Errorf("prober sent %s, want GET", r.Method)
		}
		w.WriteHeader(http.StatusOK)
		fmt.Fprint(w, `{"status":"ok"}`)
	}))
	defer srv.Close()

	p := HTTPProber{}
	status, err := p.Get(srv.URL)
	if err != nil || status != http.StatusOK {
		t.Errorf("Get = %d, %v; want 200, nil", status, err)
	}

	// A closed server is a transport error, not a panic.
	srv2 := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {}))
	url2 := srv2.URL
	srv2.Close()
	if _, err := p.Get(url2); err == nil {
		t.Error("Get on a closed server should error")
	}
}

// TestRunRealProberAgainstFakeAPI exercises the full HTTP check path with
// an httptest server standing in for the Webui proxy.
func TestRunRealProberAgainstFakeAPI(t *testing.T) {
	stack := setupStack(t)
	comp := &fakeCompose{ps: psHealthy}
	runner := dockerOK()

	// A Webui proxy that forwards /api/health as the API would answer.
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/":
			w.WriteHeader(http.StatusOK)
		case "/api/health":
			w.WriteHeader(http.StatusOK)
			fmt.Fprint(w, `{"status":"ok","service":"api"}`)
		default:
			w.WriteHeader(http.StatusNotFound)
		}
	}))
	defer srv.Close()

	// The doctor's URLs are fixed to the published ports; exercise the
	// prober directly with the fake server to keep the test hermetic.
	p := HTTPProber{}
	for _, url := range []string{srv.URL, srv.URL + "/api/health"} {
		status, err := p.Get(url)
		if err != nil || status != http.StatusOK {
			t.Errorf("Get(%s) = %d, %v; want 200, nil", url, status, err)
		}
	}

	// And a stack whose published port answers 502 (proxy up, API down).
	prober := &fakeProber{status: map[string]int{WebUIURL: 200, APIHealthURL: http.StatusBadGateway}}
	res, _ := runDoctor(t, stack, comp, prober, runner)
	assertFail(t, res, "API answers (GET /api/health through the Webui proxy)", "answered 502", "docker compose logs api")
	assertPass(t, res, "Webui answers on host port 8081")
}

// Compile-time interface checks: the production types satisfy the
// boundaries, and prereqs.NewRunner fits the doctor's Runner.
var (
	_ wizard.Compose = (*fakeCompose)(nil)
	_ Prober         = (*fakeProber)(nil)
	_ Prober         = HTTPProber{}
	_ prereqs.Runner = (*fakeRunner)(nil)
)
