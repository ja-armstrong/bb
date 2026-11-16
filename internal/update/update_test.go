package update

import (
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/ja-armstrong/bb/internal/doctor"
	"github.com/ja-armstrong/bb/internal/prereqs"
)

// fakeRunner is the prereqs.Runner fake: detection can succeed or fail, and
// any privileged (installing) call is recorded so tests can prove update
// never installs anything.
type fakeRunner struct {
	detectErr bool // true: neither docker nor compose detected
	installed []string
}

func (r *fakeRunner) CombinedOutput(name string, args ...string) (string, error) {
	if r.detectErr {
		return "", errors.New("not found")
	}
	return name + ": ok", nil
}

func (r *fakeRunner) RunInteractive(name string, args ...string) error {
	r.installed = append(r.installed, name)
	return nil
}

// fakeGit is the Git fake, parameterised per scenario. HeadRev answers
// `before` on the first call and `after` afterwards, mimicking a pull that
// moved the revision.
type fakeGit struct {
	clean   bool
	cleanEr error
	before  string
	after   string
	revErr  error
	pullEr  error
	pulled  int
	revs    int
}

func (g *fakeGit) IsClean(dir string) (bool, error) {
	if g.cleanEr != nil {
		return false, g.cleanEr
	}
	return g.clean, nil
}

func (g *fakeGit) Pull(dir string) error {
	g.pulled++
	return g.pullEr
}

func (g *fakeGit) HeadRev(dir string) (string, error) {
	if g.revErr != nil {
		return "", g.revErr
	}
	g.revs++
	if g.revs == 1 {
		return g.before, nil
	}
	return g.after, nil
}

// fakeCompose is the Compose fake: every call is recorded in the shared
// step log so tests can assert the stage order.
type fakeCompose struct {
	log   *[]string
	build bool // build succeeds when true
	up    bool // up succeeds when true
}

func (c fakeCompose) Build(dir string) error {
	*c.log = append(*c.log, "build:"+dir)
	if !c.build {
		return errors.New("build boom")
	}
	return nil
}

func (c fakeCompose) Up(dir string) error {
	*c.log = append(*c.log, "up:"+dir)
	if !c.up {
		return errors.New("up boom")
	}
	return nil
}

// harness wires a full fake Options and returns it plus the shared step log.
type harness struct {
	git      *fakeGit
	runner   *fakeRunner
	log      *[]string
	opts     Options
	stackDir string
}

func newHarness(t *testing.T, g *fakeGit, r *fakeRunner) harness {
	t.Helper()
	dir := t.TempDir() // a real directory: Run stats it before anything else
	// The compose directory the wizard's FindComposeDir expects.
	composeDir := filepath.Join(dir, "bitbeard")
	if err := os.MkdirAll(composeDir, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(composeDir, "docker-compose.yml"), []byte("services: {}\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	var log []string
	h := harness{
		git:      g,
		runner:   r,
		log:      &log,
		stackDir: dir,
		opts: Options{
			StackDir: dir,
			Git:      g,
			Compose:  fakeCompose{log: &log, build: true, up: true},
			Runner:   r,
			Doctor: func(doctor.Options) doctor.Result {
				log = append(log, "doctor")
				return doctor.Result{}
			},
		},
	}
	return h
}

const revBefore = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
const revAfter = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"

// TestUpdateHappyPath: a clean tree plus new upstream revisions leads to
// build, then up, then doctor last — and the doctor's verdict is reported.
func TestUpdateHappyPath(t *testing.T) {
	h := newHarness(t, &fakeGit{clean: true, before: revBefore, after: revAfter}, &fakeRunner{})
	doctorFailed := false
	h.opts.Doctor = func(doctor.Options) doctor.Result {
		*h.log = append(*h.log, "doctor")
		return doctor.Result{Failed: doctorFailed}
	}

	res, err := Run(h.opts)
	if err != nil {
		t.Fatalf("Run returned error: %v", err)
	}
	if !res.Pulled || !res.Rebuilt || !res.Restarted || res.Noop {
		t.Fatalf("unexpected result flags: %+v", res)
	}
	if res.DoctorFailed != doctorFailed {
		t.Fatalf("DoctorFailed = %v, want %v", res.DoctorFailed, doctorFailed)
	}
	want := []string{"build", "up", "doctor"}
	for i, w := range want {
		if i >= len(*h.log) || !strings.HasPrefix((*h.log)[i], w) {
			t.Fatalf("step log = %v, want %v in order", *h.log, want)
		}
	}
	if got := len(*h.log); got != len(want) {
		t.Fatalf("step log has %d entries, want %d: %v", got, len(want), *h.log)
	}
	if h.git.pulled != 1 {
		t.Fatalf("pull called %d times, want 1", h.git.pulled)
	}
}

// TestUpdateDirtyTreeRefused: a dirty working tree stops everything before
// any side effect — no pull, no build, no up, no doctor.
func TestUpdateDirtyTreeRefused(t *testing.T) {
	h := newHarness(t, &fakeGit{clean: false, before: revBefore, after: revAfter}, &fakeRunner{})

	_, err := Run(h.opts)
	if err == nil {
		t.Fatal("dirty tree: Run must fail")
	}
	if h.git.pulled != 0 {
		t.Fatal("dirty tree: pull must not run")
	}
	if len(*h.log) != 0 {
		t.Fatalf("dirty tree: steps ran: %v", *h.log)
	}
	if len(h.runner.installed) != 0 {
		t.Fatal("dirty tree: nothing must be installed")
	}
}

// TestUpdatePullFailureStopsCleanly: a failed pull reports and stops before
// the rebuild; the stack is untouched.
func TestUpdatePullFailureStopsCleanly(t *testing.T) {
	h := newHarness(t, &fakeGit{clean: true, before: revBefore, after: revAfter, pullEr: errors.New("network down")}, &fakeRunner{})

	_, err := Run(h.opts)
	if err == nil {
		t.Fatal("pull failure: Run must fail")
	}
	if !strings.Contains(err.Error(), "stack was left untouched") {
		t.Fatalf("pull failure error should say the stack was left untouched, got: %v", err)
	}
	if len(*h.log) != 0 {
		t.Fatalf("pull failure: steps ran: %v", *h.log)
	}
}

// TestUpdateNoopStopsBeforeRebuild: with nothing new upstream the run
// reports a clean no-op and stops before the rebuild — no build, no up, no
// doctor.
func TestUpdateNoopStopsBeforeRebuild(t *testing.T) {
	h := newHarness(t, &fakeGit{clean: true, before: revAfter, after: revAfter}, &fakeRunner{})

	res, err := Run(h.opts)
	if err != nil {
		t.Fatalf("no-op: Run must succeed, got %v", err)
	}
	if !res.Noop {
		t.Fatalf("no-op: Noop flag not set: %+v", res)
	}
	if res.Pulled || res.Rebuilt || res.Restarted || res.DoctorFailed {
		t.Fatalf("no-op: unexpected result flags: %+v", res)
	}
	if len(*h.log) != 0 {
		t.Fatalf("no-op: steps ran: %v", *h.log)
	}
}

// TestUpdateVanishedPrereqFlagged: a prerequisite that vanished between
// installs is flagged before the rebuild, and nothing is auto-installed.
func TestUpdateVanishedPrereqFlagged(t *testing.T) {
	h := newHarness(t, &fakeGit{clean: true, before: revBefore, after: revAfter}, &fakeRunner{detectErr: true})

	_, err := Run(h.opts)
	if err == nil {
		t.Fatal("vanished prereq: Run must fail")
	}
	if !strings.Contains(err.Error(), "Docker") {
		t.Fatalf("vanished prereq: error must name the missing prerequisite, got: %v", err)
	}
	if len(*h.log) != 0 {
		t.Fatalf("vanished prereq: steps ran: %v", *h.log)
	}
	if n := len(h.runner.installed); n != 0 {
		t.Fatalf("vanished prereq: update must never install, but %d privileged calls ran: %v", n, h.runner.installed)
	}
}

// TestUpdateNeverCallsInstaller: across every stopping point of a full
// pull-to-doctor run, the prereq installer is never invoked — update only
// ever uses check-only detection.
func TestUpdateNeverCallsInstaller(t *testing.T) {
	h := newHarness(t, &fakeGit{clean: true, before: revBefore, after: revAfter}, &fakeRunner{})

	if _, err := Run(h.opts); err != nil {
		t.Fatalf("Run returned error: %v", err)
	}
	if n := len(h.runner.installed); n != 0 {
		t.Fatalf("update must never call the prereq installer, but %d privileged calls ran: %v", n, h.runner.installed)
	}
}

// TestUpdateDoctorLastEvenWhenDoctorFails: a failing doctor still runs
// after build and up, and its failure is reported via DoctorFailed (the
// caller exits non-zero) without turning into a Run error.
func TestUpdateDoctorLastEvenWhenDoctorFails(t *testing.T) {
	h := newHarness(t, &fakeGit{clean: true, before: revBefore, after: revAfter}, &fakeRunner{})
	h.opts.Doctor = func(doctor.Options) doctor.Result {
		*h.log = append(*h.log, "doctor")
		return doctor.Result{Failed: true}
	}

	res, err := Run(h.opts)
	if err != nil {
		t.Fatalf("doctor failure is reported via DoctorFailed, not an error: %v", err)
	}
	if !res.DoctorFailed {
		t.Fatalf("DoctorFailed not set: %+v", res)
	}
	want := []string{"build", "up", "doctor"}
	for i, w := range want {
		if i >= len(*h.log) || !strings.HasPrefix((*h.log)[i], w) {
			t.Fatalf("step log = %v, want %v in order", *h.log, want)
		}
	}
	if got := len(*h.log); got != len(want) {
		t.Fatalf("step log has %d entries, want %d: %v", got, len(want), *h.log)
	}
}

// TestUpdateBuildFailureStopsBeforeUp: a failed build prevents the restart
// and the doctor.
func TestUpdateBuildFailureStopsBeforeUp(t *testing.T) {
	h := newHarness(t, &fakeGit{clean: true, before: revBefore, after: revAfter}, &fakeRunner{})
	h.opts.Compose = fakeCompose{log: h.log, build: false, up: true}

	_, err := Run(h.opts)
	if err == nil {
		t.Fatal("build failure: Run must fail")
	}
	if got := *h.log; len(got) != 1 || !strings.HasPrefix(got[0], "build") {
		t.Fatalf("build failure: only build should have run, log: %v", got)
	}
}

// TestUpdateMissingStackDir: a stack directory that does not exist is a
// clean error pointing at setup, with no side effects.
func TestUpdateMissingStackDir(t *testing.T) {
	h := newHarness(t, &fakeGit{clean: true, before: revBefore, after: revAfter}, &fakeRunner{})
	h.opts.StackDir = h.stackDir + "/does-not-exist"

	_, err := Run(h.opts)
	if err == nil {
		t.Fatal("missing stack dir: Run must fail")
	}
	if !strings.Contains(err.Error(), "bitbeard setup") {
		t.Fatalf("missing stack dir: error should point at setup, got: %v", err)
	}
	if len(*h.log) != 0 {
		t.Fatalf("missing stack dir: steps ran: %v", *h.log)
	}
}

// TestShort checks the display form of a revision.
func TestShort(t *testing.T) {
	cases := []struct{ in, want string }{
		{revBefore, "aaaaaaaa"},
		{"abc", "abc"},
		{"", ""},
	}
	for _, c := range cases {
		if got := short(c.in); got != c.want {
			t.Fatalf("short(%q) = %q, want %q", c.in, got, c.want)
		}
	}
}

// Compile-time interface checks so the production types cannot drift from
// the boundaries they implement.
var (
	_ Git            = gitCLI{}
	_ Compose        = StackCompose{}
	_ prereqs.Runner = (*fakeRunner)(nil)
)
