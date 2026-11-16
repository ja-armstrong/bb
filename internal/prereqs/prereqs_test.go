package prereqs

import (
	"bytes"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// fakeRunner fakes command execution: detection results come from fail,
// privileged commands are recorded in ran, and after runs after each
// privileged command so a test can flip detection results once an install
// has "happened".
type fakeRunner struct {
	fail  map[string]bool // command key -> command fails
	ran   []string        // privileged commands, in order
	after func(command string)
}

func (f *fakeRunner) key(name string, args ...string) string {
	return strings.Join(append([]string{name}, args...), " ")
}

func (f *fakeRunner) CombinedOutput(name string, args ...string) (string, error) {
	if f.fail[f.key(name, args...)] {
		return "", errors.New("exit status 1")
	}
	return "ok", nil
}

func (f *fakeRunner) RunInteractive(name string, args ...string) error {
	cmd := f.key(name, args...)
	f.ran = append(f.ran, cmd)
	if f.fail[cmd] {
		return errors.New("exit status 100")
	}
	if f.after != nil {
		f.after(cmd)
	}
	return nil
}

// writeOSRelease writes os-release content to a temp file and returns its
// path.
func writeOSRelease(t *testing.T, content string) string {
	t.Helper()
	path := filepath.Join(t.TempDir(), "os-release")
	if err := os.WriteFile(path, []byte(content), 0o644); err != nil {
		t.Fatal(err)
	}
	return path
}

const ubuntuOSRelease = "ID=ubuntu\nID_LIKE=debian\n"

// runEnsure calls Ensure with the platform pinned to linux (tests may run
// on any OS) and returns the result, the captured output, and the error.
func runEnsure(t *testing.T, r Runner, osReleasePath string) (Result, string, error) {
	t.Helper()
	orig := goOS
	t.Cleanup(func() { goOS = orig })
	goOS = "linux"

	var out bytes.Buffer
	res, err := Ensure(r, &out, osReleasePath)
	return res, out.String(), err
}

func TestEnsureNothingMissing(t *testing.T) {
	fr := &fakeRunner{fail: map[string]bool{}}
	res, out, err := runEnsure(t, fr, writeOSRelease(t, ubuntuOSRelease))
	if err != nil {
		t.Fatalf("err = %v, want nil", err)
	}
	if res.Status != StatusOK {
		t.Errorf("Status = %q, want %q", res.Status, StatusOK)
	}
	if !res.DockerPresent || !res.ComposePresent {
		t.Errorf("detection = %v/%v, want true/true", res.DockerPresent, res.ComposePresent)
	}
	if len(fr.ran) != 0 {
		t.Errorf("privileged commands ran = %v, want none", fr.ran)
	}
	if !strings.Contains(out, "already installed") {
		t.Errorf("output %q should note that everything is already installed", out)
	}
}

func TestEnsureUbuntuInstallsMissingDocker(t *testing.T) {
	fr := &fakeRunner{fail: map[string]bool{
		"docker --version":         true,
		"docker compose version":   true,
		"docker-compose --version": false,
	}}
	fr.after = func(cmd string) {
		if cmd == "sudo apt-get install -y docker.io" {
			delete(fr.fail, "docker --version")
		}
	}
	res, out, err := runEnsure(t, fr, writeOSRelease(t, ubuntuOSRelease))
	if err != nil {
		t.Fatalf("err = %v, want nil", err)
	}
	if res.Status != StatusFixed {
		t.Errorf("Status = %q, want %q", res.Status, StatusFixed)
	}
	want := []string{"sudo apt-get update", "sudo apt-get install -y docker.io"}
	if strings.Join(fr.ran, "\n") != strings.Join(want, "\n") {
		t.Errorf("ran = %v, want %v", fr.ran, want)
	}
	if len(res.Installed) != 1 || res.Installed[0] != "Docker" {
		t.Errorf("Installed = %v, want [Docker]", res.Installed)
	}
	for _, shown := range want {
		if !strings.Contains(out, "  $ "+shown) {
			t.Errorf("output should show the exact command %q before running it", shown)
		}
	}
}

func TestEnsureUbuntuInstallsMissingComposeWithFallback(t *testing.T) {
	fr := &fakeRunner{fail: map[string]bool{
		"docker-compose --version":                  true,
		"docker compose version":                    true,
		"sudo apt-get install -y docker-compose-v2": true, // primary package unavailable
	}}
	fr.after = func(cmd string) {
		if cmd == "sudo apt-get install -y docker-compose" {
			delete(fr.fail, "docker-compose --version")
			delete(fr.fail, "docker compose version")
		}
	}
	res, out, err := runEnsure(t, fr, writeOSRelease(t, ubuntuOSRelease))
	if err != nil {
		t.Fatalf("err = %v, want nil", err)
	}
	if res.Status != StatusFixed {
		t.Errorf("Status = %q, want %q", res.Status, StatusFixed)
	}
	want := []string{
		"sudo apt-get update",
		"sudo apt-get install -y docker-compose-v2",
		"sudo apt-get install -y docker-compose",
	}
	if strings.Join(fr.ran, "\n") != strings.Join(want, "\n") {
		t.Errorf("ran = %v, want %v", fr.ran, want)
	}
	if !strings.Contains(out, "fallback") {
		t.Errorf("output %q should explain the fallback attempt", out)
	}
}

func TestEnsureComposeInstallFailsAllPackages(t *testing.T) {
	fr := &fakeRunner{fail: map[string]bool{
		"docker-compose --version":                  true,
		"docker compose version":                    true,
		"sudo apt-get install -y docker-compose-v2": true,
		"sudo apt-get install -y docker-compose":    true,
	}}
	res, out, err := runEnsure(t, fr, writeOSRelease(t, ubuntuOSRelease))
	if err == nil {
		t.Fatal("err = nil, want a StepError")
	}
	var stepErr *StepError
	if !errors.As(err, &stepErr) {
		t.Fatalf("err = %T (%v), want *StepError", err, err)
	}
	if len(stepErr.Commands) != 2 {
		t.Errorf("Commands = %v, want both attempted packages", stepErr.Commands)
	}
	if res.Status != StatusFailed {
		t.Errorf("Status = %q, want %q", res.Status, StatusFailed)
	}
	if !strings.Contains(out, "stopped here") {
		t.Errorf("output %q should describe where the stage stopped", out)
	}
}

func TestEnsureUnsupportedDistroPrintsInstructionsAndStops(t *testing.T) {
	fr := &fakeRunner{fail: map[string]bool{
		"docker --version":         true,
		"docker compose version":   true,
		"docker-compose --version": true,
	}}
	res, out, err := runEnsure(t, fr, writeOSRelease(t, "ID=fedora\nID_LIKE=\"fedora rhel\"\n"))
	if !errors.Is(err, ErrUnsupportedDistro) {
		t.Fatalf("err = %v, want ErrUnsupportedDistro", err)
	}
	if res.Status != StatusManual {
		t.Errorf("Status = %q, want %q", res.Status, StatusManual)
	}
	if len(fr.ran) != 0 {
		t.Errorf("privileged commands ran = %v, want none", fr.ran)
	}
	if !strings.Contains(out, "sudo dnf install -y moby-engine docker-compose") {
		t.Errorf("output %q should contain the exact install command", out)
	}
	if !strings.Contains(out, "Nothing was changed") {
		t.Errorf("output %q should say nothing was changed", out)
	}
}

func TestEnsureUnsupportedDistroEverythingInstalled(t *testing.T) {
	fr := &fakeRunner{fail: map[string]bool{}}
	res, out, err := runEnsure(t, fr, writeOSRelease(t, "ID=fedora\n"))
	if err != nil {
		t.Fatalf("err = %v, want nil", err)
	}
	if res.Status != StatusOK {
		t.Errorf("Status = %q, want %q", res.Status, StatusOK)
	}
	if len(fr.ran) != 0 {
		t.Errorf("privileged commands ran = %v, want none", fr.ran)
	}
	if strings.Contains(out, "not supported") {
		t.Errorf("output %q should not complain about support when nothing is missing", out)
	}
}

func TestEnsureMissingOSRelease(t *testing.T) {
	fr := &fakeRunner{fail: map[string]bool{
		"docker --version":         true,
		"docker compose version":   true,
		"docker-compose --version": true,
	}}
	res, out, err := runEnsure(t, fr, filepath.Join(t.TempDir(), "absent"))
	if !errors.Is(err, ErrUnsupportedDistro) {
		t.Fatalf("err = %v, want ErrUnsupportedDistro", err)
	}
	if res.Status != StatusManual {
		t.Errorf("Status = %q, want %q", res.Status, StatusManual)
	}
	if len(fr.ran) != 0 {
		t.Errorf("privileged commands ran = %v, want none", fr.ran)
	}
	if !strings.Contains(out, "could not be identified") {
		t.Errorf("output %q should say the distro could not be identified", out)
	}
}

func TestEnsureNonLinux(t *testing.T) {
	orig := goOS
	t.Cleanup(func() { goOS = orig })
	goOS = "darwin"

	fr := &fakeRunner{fail: map[string]bool{
		"docker --version":         true,
		"docker compose version":   true,
		"docker-compose --version": true,
	}}
	var out bytes.Buffer
	res, err := Ensure(fr, &out, writeOSRelease(t, ubuntuOSRelease))
	if !errors.Is(err, ErrUnsupportedDistro) {
		t.Fatalf("err = %v, want ErrUnsupportedDistro", err)
	}
	if res.Status != StatusManual {
		t.Errorf("Status = %q, want %q", res.Status, StatusManual)
	}
	if len(fr.ran) != 0 {
		t.Errorf("privileged commands ran = %v, want none", fr.ran)
	}
	if !strings.Contains(out.String(), "darwin") {
		t.Errorf("output %q should name the platform", out.String())
	}
}

func TestDetectComposeVariants(t *testing.T) {
	tests := []struct {
		name        string
		fail        map[string]bool
		wantDocker  bool
		wantCompose bool
	}{
		{name: "nothing installed", fail: map[string]bool{"docker --version": true, "docker compose version": true, "docker-compose --version": true}},
		{name: "docker only", fail: map[string]bool{"docker compose version": true, "docker-compose --version": true}, wantDocker: true},
		{name: "compose plugin", fail: map[string]bool{}, wantDocker: true, wantCompose: true},
		{name: "standalone compose", fail: map[string]bool{"docker compose version": true}, wantDocker: true, wantCompose: true},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			docker, compose := detect(&fakeRunner{fail: tt.fail})
			if docker != tt.wantDocker || compose != tt.wantCompose {
				t.Errorf("detect = %v/%v, want %v/%v", docker, compose, tt.wantDocker, tt.wantCompose)
			}
		})
	}
}
