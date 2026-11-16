// Package prereqs implements the prerequisite stage of the Bootstrap: it
// detects Docker and Docker Compose, skips what is already installed, and
// installs what is missing on Ubuntu/Debian — showing every privileged
// command to the operator before running it. Installing Docker also adds
// the operator to the `docker` group. On other distros it prints the exact
// install commands and stops cleanly, changing nothing.
package prereqs

import (
	"errors"
	"fmt"
	"io"
	"runtime"
	"strings"
)

// Runner is the invocation layer between this package and the operating
// system. It is small so tests can replace it with a fake; production uses
// NewRunner.
type Runner interface {
	// CombinedOutput runs an unprivileged command and returns its combined
	// output. Used for detection only.
	CombinedOutput(name string, args ...string) (string, error)
	// RunInteractive runs a privileged command with the process's standard
	// streams attached, so sudo can prompt for a password in the terminal
	// and package-manager progress stays visible.
	RunInteractive(name string, args ...string) error
}

// Status describes how the prerequisite stage ended.
type Status string

const (
	StatusOK     Status = "ok"     // everything was already installed
	StatusFixed  Status = "fixed"  // missing pieces were installed
	StatusManual Status = "manual" // instructions printed for the operator
	StatusFailed Status = "failed" // an install step failed
)

// Result reports what the prerequisite stage did.
type Result struct {
	Status         Status
	DockerPresent  bool
	ComposePresent bool
	Installed      []string // prerequisites installed by this run
	// DaemonReachable is true when this process could reach the Docker
	// daemon at the end of the stage (probed with `docker ps`). The setup
	// flow uses it to stop with a new-session notice when the operator
	// cannot yet reach the daemon — group membership only takes effect in
	// new login sessions. The doctor ignores it and keeps reporting the
	// daemon as its normal check.
	DaemonReachable bool
}

// ErrUnsupportedDistro is returned when the stage cannot install anything
// itself; the exact install commands have been printed to out and nothing
// was changed on the machine.
var ErrUnsupportedDistro = errors.New("unsupported distribution")

// goOS is runtime.GOOS; a var so tests can pin the platform.
var goOS = runtime.GOOS

// defaultOSReleasePath is where Linux publishes distribution information.
const defaultOSReleasePath = "/etc/os-release"

// Ensure runs the prerequisite stage. It writes user-facing output (shown
// commands, prompts, notes) to out, detects Docker and Docker Compose, and
// on supported distros installs what is missing. osReleasePath overrides
// the default /etc/os-release; "" means the default.
//
// When the returned error wraps ErrUnsupportedDistro, the exact install
// commands have been printed and the machine was left untouched.
func Ensure(r Runner, out io.Writer, osReleasePath string) (Result, error) {
	docker, compose := detect(r)
	if docker && compose {
		// USER-FACING
		fmt.Fprintln(out, "Docker and Docker Compose are already installed — skipping.")
		return Result{
			Status:          StatusOK,
			DockerPresent:   true,
			ComposePresent:  true,
			DaemonReachable: daemonReachable(r),
		}, nil
	}

	if goOS != "linux" {
		// USER-FACING
		fmt.Fprintf(out, "bitbeard setup prepares Linux machines; this machine is %s.\n", goOS)
		// USER-FACING
		fmt.Fprintln(out, "Install Docker and Docker Compose yourself, then rerun `bitbeard setup` on a Linux host. Nothing was changed.")
		return Result{Status: StatusManual, DockerPresent: docker, ComposePresent: compose}, ErrUnsupportedDistro
	}

	d, err := readOSRelease(firstNonEmpty(osReleasePath, defaultOSReleasePath))
	if err != nil {
		// USER-FACING
		fmt.Fprintf(out, "This Linux distribution could not be identified (%v).\n", err)
		printManualInstructions(out, distro{})
		return Result{Status: StatusManual, DockerPresent: docker, ComposePresent: compose}, ErrUnsupportedDistro
	}
	if !d.supported() {
		printManualInstructions(out, d)
		return Result{Status: StatusManual, DockerPresent: docker, ComposePresent: compose}, ErrUnsupportedDistro
	}

	if docker {
		// USER-FACING
		fmt.Fprintln(out, "Docker is already installed.")
	}
	if compose {
		// USER-FACING
		fmt.Fprintln(out, "Docker Compose is already installed.")
	}

	var missing []string
	if !docker {
		missing = append(missing, "Docker")
	}
	if !compose {
		missing = append(missing, "Docker Compose")
	}

	// USER-FACING
	fmt.Fprintf(out, "Installing missing prerequisites on %s: %s.\n", d.ID, strings.Join(missing, " and "))
	// USER-FACING
	fmt.Fprintln(out, "Each step needs administrator rights; sudo may ask for your password. The exact command is shown before it runs.")

	operator := operatorName(r)
	plan := buildPlan(d, !docker, !compose, operator)
	if err := runPlan(r, out, plan); err != nil {
		return Result{Status: StatusFailed, DockerPresent: docker, ComposePresent: compose}, err
	}

	if !docker && operator != "" {
		// USER-FACING
		fmt.Fprintln(out, "Added the operator to the `docker` group: membership takes effect in new login sessions.")
	}

	// Describe the end state: re-check what actually works now.
	docker, compose = detect(r)
	if !docker || !compose {
		var still []string
		if !docker {
			still = append(still, "Docker")
		}
		if !compose {
			still = append(still, "Docker Compose")
		}
		// USER-FACING
		fmt.Fprintf(out, "error: installation finished, but %s is still not detected. Run `bitbeard doctor` for details.\n", strings.Join(still, " and "))
		return Result{Status: StatusFailed, DockerPresent: docker, ComposePresent: compose},
			fmt.Errorf("installed %s but detection still fails", strings.Join(missing, " and "))
	}

	// USER-FACING
	fmt.Fprintf(out, "Installed: %s.\n", strings.Join(missing, " and "))
	// USER-FACING
	fmt.Fprintln(out, "Prerequisite stage complete: Docker and Docker Compose are ready.")
	return Result{
		Status:          StatusFixed,
		DockerPresent:   true,
		ComposePresent:  true,
		Installed:       missing,
		DaemonReachable: daemonReachable(r),
	}, nil
}

// Detect reports which prerequisites are already present, without changing
// anything. Check-only: unlike Ensure it never installs anything. The Doctor
// reuses this for its prerequisite checks.
func Detect(r Runner) (docker, compose bool) {
	return detect(r)
}

// daemonReachable reports whether this process can reach the Docker daemon
// right now. `docker ps` is the most direct probe: it forces a round trip
// to the daemon, so it fails both when the daemon is down and when the
// calling session lacks permission to the Docker socket — the freshly added
// `docker` group, whose membership only takes effect in new login sessions.
// A fresh-login check like `id -nG` would not do: the group can be listed
// while remaining ineffective in the session that just ran the setup.
func daemonReachable(r Runner) bool {
	_, err := r.CombinedOutput("docker", "ps")
	return err == nil
}

// operatorName reports the login name of the operator running this stage.
// Empty when it cannot be determined; the group step is skipped then.
func operatorName(r Runner) string {
	out, err := r.CombinedOutput("whoami")
	if err != nil {
		return ""
	}
	return strings.TrimSpace(out)
}

// detect reports which prerequisites are already present, without changing
// anything. Docker Compose may exist as the `docker compose` plugin or the
// standalone `docker-compose` binary; the standalone one is also checked
// when the docker CLI itself is absent.
func detect(r Runner) (docker, compose bool) {
	if _, err := r.CombinedOutput("docker", "--version"); err == nil {
		docker = true
		if _, err := r.CombinedOutput("docker", "compose", "version"); err == nil {
			return true, true
		}
	}
	if _, err := r.CombinedOutput("docker-compose", "--version"); err == nil {
		compose = true
	}
	return docker, compose
}

// printManualInstructions writes the exact install commands the operator
// must run on a distro this stage will not touch.
func printManualInstructions(out io.Writer, d distro) {
	// USER-FACING
	fmt.Fprintln(out, "This distribution is not supported for automatic installation.")
	// USER-FACING
	fmt.Fprintln(out, "Install Docker and Docker Compose yourself with:")
	// USER-FACING
	fmt.Fprintf(out, "\n  $ %s\n\n", installInstructions(d))
	// USER-FACING
	fmt.Fprintln(out, "Then rerun `bitbeard setup`. Nothing was changed on this machine.")
}

// runPlan shows and runs each privileged action in order, stopping at the
// first failure.
func runPlan(r Runner, out io.Writer, plan []action) error {
	for _, a := range plan {
		if err := runAction(r, out, a); err != nil {
			return err
		}
	}
	return nil
}

// runAction shows the exact command before running it, tries the fallbacks
// if the primary fails, and leaves the machine in a describable state when
// everything failed.
func runAction(r Runner, out io.Writer, a action) error {
	cmds := a.commands()
	var attempted []string
	var lastErr error
	for _, s := range cmds {
		// USER-FACING
		fmt.Fprintf(out, "  $ %s\n", s.display())
		err := r.RunInteractive(s.name, s.args...)
		if err == nil {
			return nil
		}
		attempted = append(attempted, s.display())
		lastErr = err
		if len(attempted) < len(cmds) {
			// USER-FACING
			fmt.Fprintf(out, "That failed (%v). Trying the fallback package instead.\n", err)
		}
	}

	// Describe the state so the operator knows exactly where things stand.
	// USER-FACING
	fmt.Fprintf(out, "error: %v\n", lastErr)
	// USER-FACING
	fmt.Fprintf(out, "Attempted: %s.\n", strings.Join(attempted, "; "))
	// USER-FACING
	fmt.Fprintln(out, "The prerequisite stage stopped here: only the commands shown above may have taken effect. Fix the cause and rerun `bitbeard setup` to continue.")
	return &StepError{Commands: attempted, Err: lastErr}
}

// StepError reports a failed privileged step, listing every command that
// was shown and run for it.
type StepError struct {
	// Commands are the attempted command lines, primary first.
	Commands []string
	// Err is the last underlying error.
	Err error
}

func (e *StepError) Error() string {
	// USER-FACING
	return fmt.Sprintf("prerequisite step failed: %s: %v", strings.Join(e.Commands, " | "), e.Err)
}

func (e *StepError) Unwrap() error { return e.Err }

func firstNonEmpty(a, b string) string {
	if a != "" {
		return a
	}
	return b
}
