package fetch

import (
	"errors"
	"fmt"
	"io"
	"os"
	"os/exec"
	"strings"
)

// GitCLI runs git from PATH. It is the production Git implementation.
type GitCLI struct{}

// Clone clones repoURL into dest.
func (GitCLI) Clone(repoURL, dest string) error {
	if _, err := exec.LookPath("git"); err != nil {
		// USER-FACING
		return errors.New("git is required to fetch the stack but was not found on PATH")
	}
	cmd := exec.Command("git", "clone", "--", repoURL, dest)
	var stderr strings.Builder
	cmd.Stderr = &stderr
	if err := cmd.Run(); err != nil {
		// USER-FACING
		return fmt.Errorf("cloning %s to %s failed: %v\n%s", repoURL, dest, err, strings.TrimSpace(stderr.String()))
	}
	return nil
}

// RemoteURL returns the URL of the origin remote of the repository at dir.
func (GitCLI) RemoteURL(dir string) (string, error) {
	cmd := exec.Command("git", "-C", dir, "remote", "get-url", "origin")
	out, err := cmd.Output()
	if err != nil {
		return "", err
	}
	return strings.TrimSpace(string(out)), nil
}

// Setup runs the fetch stage end to end, writing progress notes to out. It
// resolves the per-user locations (clone target and state file) and calls Run.
func Setup(out io.Writer) error {
	home, err := os.UserHomeDir()
	if err != nil {
		return err
	}
	configDir, err := os.UserConfigDir()
	if err != nil {
		return err
	}
	statePath := StatePath(configDir)
	cfg := Config{Target: DefaultTarget(home)}

	// Note the recovery before it happens, so the second clone does not
	// come as a surprise.
	if st, err := LoadState(statePath); err == nil && st.StackPath != "" {
		if _, serr := os.Stat(st.StackPath); serr != nil {
			// USER-FACING
			fmt.Fprintf(out, "The remembered stack location %s is gone; fetching the stack again.\n", st.StackPath)
		}
	} else {
		// USER-FACING
		fmt.Fprintf(out, "Fetching the stack repository to %s.\n", cfg.Target)
	}

	res, err := Run(cfg, GitCLI{}, statePath)
	if err != nil {
		return err
	}
	if res.Cloned {
		// USER-FACING
		fmt.Fprintf(out, "Stack repository cloned to %s.\n", res.StackPath)
	} else {
		// USER-FACING
		fmt.Fprintf(out, "Using the existing stack repository at %s.\n", res.StackPath)
	}
	return nil
}
