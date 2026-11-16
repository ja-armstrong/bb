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
// has "happened". out supplies canned CombinedOutput results (whoami); any
// command without an entry returns "ok".
type fakeRunner struct {
	fail  map[string]bool // command key -> command fails
	out   map[string]string
	ran   []string // privileged commands, in order
	after func(command string)
}

func (f *fakeRunner) key(name string, args ...string) string {
	return strings.Join(append([]string{name}, args...), " ")
}

func (f *fakeRunner) CombinedOutput(name string, args ...string) (string, error) {
	if f.fail[f.key(name, args...)] {
		return "", errors.New("exit status 1")
	}
	if s, ok := f.out[f.key(name, args...)]; ok {
		return s, nil
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
	fr := &fakeRunner{
		fail: map[string]bool{
			"docker --version":         true,
			"docker compose version":   true,
			"docker-compose --version": false,
		},
		out: map[string]string{"whoami": "operator"},
	}
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
	want := []string{"sudo apt-get update", "sudo apt-get install -y docker.io", "sudo usermod -aG docker operator"}
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
	if !strings.Contains(out, "docker` group") && !strings.Contains(out, "docker group") {
		t.Errorf("output %q should note the operator was added to the docker group", out)
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

// TestEnsureDaemonReachable covers the decision the setup flow acts on:
// whether this process can reach the Docker daemon at the end of the stage.
// `docker ps` failing is exactly the case the ticket fixes — the operator
// was just added to the `docker` group, whose membership only takes effect
// in new login sessions — and setup must stop instead of walking into an
// obscure "permission denied" when the wizard boots the stack.
func TestEnsureDaemonReachable(t *testing.T) {
	t.Run("daemon reachable with everything installed", func(t *testing.T) {
		fr := &fakeRunner{fail: map[string]bool{}}
		res, out, err := runEnsure(t, fr, writeOSRelease(t, ubuntuOSRelease))
		if err != nil {
			t.Fatalf("err = %v, want nil", err)
		}
		if res.Status != StatusOK {
			t.Errorf("Status = %q, want %q", res.Status, StatusOK)
		}
		if !res.DaemonReachable {
			t.Error("DaemonReachable = false, want true: the session can reach the daemon, so setup must continue")
		}
		if len(fr.ran) != 0 {
			t.Errorf("privileged commands ran = %v, want none", fr.ran)
		}
		if !strings.Contains(out, "already installed") {
			t.Errorf("output %q should note that everything is already installed", out)
		}
	})

	t.Run("daemon unreachable with everything installed", func(t *testing.T) {
		fr := &fakeRunner{fail: map[string]bool{"docker ps": true}}
		res, _, err := runEnsure(t, fr, writeOSRelease(t, ubuntuOSRelease))
		if err != nil {
			t.Fatalf("err = %v, want nil: the stage succeeded, only daemon access is missing", err)
		}
		if res.Status != StatusOK {
			t.Errorf("Status = %q, want %q", res.Status, StatusOK)
		}
		if res.DaemonReachable {
			t.Error("DaemonReachable = true, want false: `docker ps` cannot reach the daemon")
		}
		if !res.DockerPresent || !res.ComposePresent {
			t.Errorf("detection = %v/%v, want true/true: the binaries are present", res.DockerPresent, res.ComposePresent)
		}
		if len(fr.ran) != 0 {
			t.Errorf("privileged commands ran = %v, want none", fr.ran)
		}
	})

	t.Run("daemon unreachable right after installing Docker", func(t *testing.T) {
		fr := &fakeRunner{
			fail: map[string]bool{
				"docker --version":         true,
				"docker compose version":   true,
				"docker-compose --version": true,
				"docker ps":                true, // group membership not yet effective in this session
			},
			out: map[string]string{"whoami": "operator"},
		}
		fr.after = func(cmd string) {
			switch cmd {
			case "sudo apt-get install -y docker.io":
				delete(fr.fail, "docker --version")
			case "sudo apt-get install -y docker-compose-v2":
				delete(fr.fail, "docker compose version")
			}
		}
		res, out, err := runEnsure(t, fr, writeOSRelease(t, ubuntuOSRelease))
		if err != nil {
			t.Fatalf("err = %v, want nil: the stage succeeded, setup stops on daemon access instead", err)
		}
		if res.Status != StatusFixed {
			t.Errorf("Status = %q, want %q", res.Status, StatusFixed)
		}
		if res.DaemonReachable {
			t.Error("DaemonReachable = true, want false: `docker ps` cannot reach the daemon in this session")
		}
		if !strings.Contains(strings.Join(fr.ran, "\n"), "sudo usermod -aG docker operator") {
			t.Errorf("privileged commands ran = %v, want the operator added to the docker group", fr.ran)
		}
		if !strings.Contains(out, "docker` group") {
			t.Errorf("output %q should note the operator was added to the docker group", out)
		}
	})
}

func TestOperatorName(t *testing.T) {
	t.Run("whoami output is trimmed", func(t *testing.T) {
		fr := &fakeRunner{fail: map[string]bool{}, out: map[string]string{"whoami": "operator\n"}}
		if got := operatorName(fr); got != "operator" {
			t.Errorf("operatorName = %q, want %q", got, "operator")
		}
	})
	t.Run("whoami failure yields an empty name", func(t *testing.T) {
		fr := &fakeRunner{fail: map[string]bool{"whoami": true}}
		if got := operatorName(fr); got != "" {
			t.Errorf("operatorName = %q, want empty", got)
		}
	})
}
