package prereqs

import (
	"os"
	"os/exec"
)

// ExecRunner is the production Runner: real command execution.
type ExecRunner struct{}

// NewRunner returns the Runner used outside tests.
func NewRunner() Runner { return ExecRunner{} }

// CombinedOutput runs an unprivileged command and returns its combined
// output.
func (ExecRunner) CombinedOutput(name string, args ...string) (string, error) {
	b, err := exec.Command(name, args...).CombinedOutput()
	return string(b), err
}

// RunInteractive runs a privileged command with the process's standard
// streams attached, so sudo can prompt for a password in the terminal and
// package-manager progress stays visible.
func (ExecRunner) RunInteractive(name string, args ...string) error {
	cmd := exec.Command(name, args...)
	cmd.Stdin = os.Stdin
	cmd.Stdout = os.Stdout
	cmd.Stderr = os.Stderr
	return cmd.Run()
}
