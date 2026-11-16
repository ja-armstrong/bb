// Package update implements `bitbeard update`: it pulls the stack clone
// (the location the fetch stage remembered in the state file) to the latest
// revision, re-checks prerequisites without installing anything, rebuilds
// and restarts the stack in the compose directory, and finishes with the
// doctor so the operator sees the stack's health, not silence. A dirty
// working tree, a failed pull, or a vanished prerequisite stops the run
// before anything is rebuilt; the stack is then left exactly as it was.
package update

import (
	"bytes"
	"fmt"
	"io"
	"os"
	"os/exec"
	"strings"

	"github.com/ja-armstrong/bb/internal/doctor"
	"github.com/ja-armstrong/bb/internal/prereqs"
	"github.com/ja-armstrong/bb/internal/wizard"
)

// Git is the small slice of git the update needs, so tests can fake it.
type Git interface {
	// IsClean reports whether the working tree at dir has no local
	// modifications. A directory that is not a git repository is an error.
	IsClean(dir string) (bool, error)
	// Pull fast-forwards the clone at dir to the latest revision.
	Pull(dir string) error
	// HeadRev returns the current revision of the clone at dir.
	HeadRev(dir string) (string, error)
}

// Compose is the slice of docker compose the update needs: rebuild the
// images, then restart the stack.
type Compose interface {
	// Build rebuilds the images defined in dir (docker compose build).
	Build(dir string) error
	// Up restarts the stack defined in dir (docker compose up -d).
	Up(dir string) error
}

// StackCompose is the production docker-compose boundary for update: Build
// is defined here; Up is the wizard's, so the boot behaviour is not
// duplicated.
type StackCompose struct{ wizard.DockerCompose }

// Build runs `docker compose build` in dir, with build progress visible on
// the terminal.
func (StackCompose) Build(dir string) error {
	cmd := exec.Command("docker", "compose", "build")
	cmd.Dir = dir
	cmd.Stdout = os.Stdout
	cmd.Stderr = os.Stderr
	return cmd.Run()
}

// Options parameterise Run.
type Options struct {
	// StackDir is the stack repository root; "" discovers it (the fetch
	// stage's state file, then the default ~/bitbeard).
	StackDir string
	// Out receives progress output (os.Stdout outside tests).
	Out io.Writer
	// Git is the git boundary; nil means the real git CLI.
	Git Git
	// Compose is the docker-compose boundary; nil means the real CLI.
	Compose Compose
	// Runner is the command boundary for prerequisite detection; nil
	// means the production Runner. Check-only: update calls Detect, never
	// Ensure, so nothing is ever installed here.
	Runner prereqs.Runner
	// Doctor is the final verification pass; nil means doctor.Run.
	Doctor func(doctor.Options) doctor.Result
}

// Result reports what the update did.
type Result struct {
	// Pulled is true when the pull brought new revisions.
	Pulled bool
	// Noop is true when nothing new was upstream: the run stopped before
	// the rebuild.
	Noop bool
	// Rebuilt is true when `docker compose build` ran.
	Rebuilt bool
	// Restarted is true when `docker compose up -d` ran.
	Restarted bool
	// DoctorFailed is true when the final doctor pass found problems.
	DoctorFailed bool
}

// Run performs the update against the stack clone. The stages run in order:
// pull (refusing a dirty tree, stopping on a failed pull, no-op when
// nothing is new), a check-only prerequisite re-check, rebuild + up, and
// the doctor last. Anything that stops early leaves the stack untouched.
// The doctor's verdict is in Result.DoctorFailed; exiting non-zero is the
// caller's job.
func Run(o Options) (Result, error) {
	if o.Out == nil {
		o.Out = os.Stdout
	}
	if o.Git == nil {
		o.Git = gitCLI{}
	}
	if o.Compose == nil {
		o.Compose = StackCompose{}
	}
	if o.Runner == nil {
		o.Runner = prereqs.NewRunner()
	}
	if o.Doctor == nil {
		o.Doctor = doctor.Run
	}
	out := o.Out
	var res Result

	stackDir := o.StackDir
	if stackDir == "" {
		stackDir = doctor.FindStackDir("")
	}

	if fi, err := os.Stat(stackDir); err != nil || !fi.IsDir() {
		// USER-FACING
		return res, fmt.Errorf("no stack clone at %s — run `bitbeard setup` first", stackDir)
	}

	clean, err := o.Git.IsClean(stackDir)
	if err != nil {
		return res, err
	}
	if !clean {
		// USER-FACING
		return res, fmt.Errorf("the clone at %s has local changes; `bitbeard update` refuses to overwrite them — commit, stash or remove them and rerun. Nothing was changed.", stackDir)
	}

	before, err := o.Git.HeadRev(stackDir)
	if err != nil {
		return res, err
	}

	// USER-FACING
	fmt.Fprintf(out, "Updating the stack clone at %s (git pull --ff-only)...\n", stackDir)
	if err := o.Git.Pull(stackDir); err != nil {
		// USER-FACING
		return res, fmt.Errorf("pulling the latest revision failed (offline, or the remote is unreachable); the stack was left untouched: %v", err)
	}

	after, err := o.Git.HeadRev(stackDir)
	if err != nil {
		return res, err
	}
	if before == after {
		// Fast no-op: nothing new upstream, so stop before the rebuild.
		// USER-FACING
		fmt.Fprintf(out, "Already up to date: the clone at %s is at the latest revision (%s). Nothing was rebuilt.\n", stackDir, short(after))
		res.Noop = true
		return res, nil
	}
	// USER-FACING
	fmt.Fprintf(out, "Updated the clone: %s → %s.\n", short(before), short(after))
	res.Pulled = true

	// Re-check prerequisites via the check-only detection: a prerequisite
	// that vanished between installs is flagged, never auto-installed.
	docker, compose := prereqs.Detect(o.Runner)
	var missing []string
	if !docker {
		missing = append(missing, "Docker")
	}
	if !compose {
		missing = append(missing, "Docker Compose")
	}
	if len(missing) > 0 {
		// USER-FACING
		return res, fmt.Errorf("no longer detected: %s. Rerun `bitbeard setup` (or `bitbeard doctor` for details); update does not install prerequisites, and nothing was rebuilt or restarted", strings.Join(missing, " and "))
	}

	composeDir, err := wizard.FindComposeDir(stackDir)
	if err != nil {
		return res, err
	}

	// USER-FACING
	fmt.Fprintf(out, "Rebuilding the images: docker compose build in %s.\n", composeDir)
	if err := o.Compose.Build(composeDir); err != nil {
		// USER-FACING
		return res, fmt.Errorf("docker compose build failed: %v", err)
	}
	res.Rebuilt = true

	// USER-FACING
	fmt.Fprintf(out, "Restarting the stack: docker compose up -d in %s.\n", composeDir)
	if err := o.Compose.Up(composeDir); err != nil {
		// USER-FACING
		return res, fmt.Errorf("docker compose up failed: %v", err)
	}
	res.Restarted = true

	// Final pass: the doctor's checks and its summary, so the update ends
	// with the stack's health, not silence.
	// USER-FACING
	fmt.Fprintln(out, "bitbeard update — final verification (the doctor; it changes nothing)")
	d := o.Doctor(doctor.Options{
		StackDir: stackDir,
		Out:      out,
	})
	res.DoctorFailed = d.Failed
	return res, nil
}

// short renders a revision for display: the first 8 characters.
func short(rev string) string {
	if len(rev) > 8 {
		return rev[:8]
	}
	return rev
}

// gitCLI is the production Git: the real git CLI.
type gitCLI struct{}

// IsClean reports whether `git status --porcelain` in dir is empty.
func (gitCLI) IsClean(dir string) (bool, error) {
	out, err := gitCLI{}.output(dir, "status", "--porcelain")
	if err != nil {
		return false, err
	}
	return strings.TrimSpace(out) == "", nil
}

// Pull fast-forwards the clone at dir with `git pull --ff-only`, with the
// pull's progress visible on the terminal. --ff-only refuses to merge a
// diverged history: an update never rewrites local commits.
func (gitCLI) Pull(dir string) error {
	cmd := exec.Command("git", "pull", "--ff-only")
	cmd.Dir = dir
	cmd.Stdout = os.Stdout
	cmd.Stderr = os.Stderr
	if err := cmd.Run(); err != nil {
		// USER-FACING
		return fmt.Errorf("git pull --ff-only failed: %v", err)
	}
	return nil
}

// HeadRev returns `git rev-parse HEAD` of the clone at dir.
func (gitCLI) HeadRev(dir string) (string, error) {
	out, err := gitCLI{}.output(dir, "rev-parse", "HEAD")
	return strings.TrimSpace(out), err
}

// output runs one capture-output git command in dir.
func (gitCLI) output(dir string, args ...string) (string, error) {
	cmd := exec.Command("git", args...)
	cmd.Dir = dir
	var stderr bytes.Buffer
	cmd.Stderr = &stderr
	out, err := cmd.Output()
	if err != nil {
		return "", fmt.Errorf("git %s: %v: %s", strings.Join(args, " "), err, strings.TrimSpace(stderr.String()))
	}
	return string(out), nil
}
