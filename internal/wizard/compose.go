package wizard

// compose.go is the docker-compose boundary of the boot stage, kept behind
// a small interface so tests can fake it, plus the check that the mandatory
// Subsystems came up.

import (
	"bytes"
	"encoding/json"
	"fmt"
	"os"
	"os/exec"
	"strings"
)

// Compose is the slice of docker compose the boot stage needs.
type Compose interface {
	// Up starts the stack defined in dir (docker compose up -d).
	Up(dir string) error
	// PS lists the stack's containers as `docker compose ps --format json`
	// output (one JSON object per line).
	PS(dir string) (string, error)
}

// DockerCompose runs the real docker compose CLI.
type DockerCompose struct{}

// Up runs `docker compose up -d` in dir, with compose's progress visible on
// the terminal.
func (DockerCompose) Up(dir string) error {
	cmd := exec.Command("docker", "compose", "up", "-d")
	cmd.Dir = dir
	cmd.Stdout = os.Stdout
	cmd.Stderr = os.Stderr
	return cmd.Run()
}

// PS runs `docker compose ps --format json` in dir and returns its stdout.
func (DockerCompose) PS(dir string) (string, error) {
	cmd := exec.Command("docker", "compose", "ps", "--format", "json")
	cmd.Dir = dir
	var stderr bytes.Buffer
	cmd.Stderr = &stderr
	out, err := cmd.Output()
	if err != nil {
		return "", fmt.Errorf("%v: %s", err, strings.TrimSpace(stderr.String()))
	}
	return string(out), nil
}

// composeContainer is one entry of `docker compose ps --format json`.
type composeContainer struct {
	Name     string `json:"Name"`
	State    string `json:"State"`
	ExitCode int    `json:"ExitCode"`
}

// mandatoryRunning must all be running for the mandatory stack to count as
// up: the VPN tunnel, the torrent client, the Indexer service, and the three
// core services.
var mandatoryRunning = []string{"gluetun", "qbittorrent", "jackett", "api", "manager", "webui"}

// mandatoryExited are one-shot init containers that must have exited 0.
var mandatoryExited = []string{"init-configs"}

// parseComposePS accepts both JSON-lines output and a top-level JSON array.
func parseComposePS(s string) ([]composeContainer, error) {
	s = strings.TrimSpace(s)
	if s == "" {
		// USER-FACING
		return nil, fmt.Errorf("empty output")
	}
	var arr []composeContainer
	if err := json.Unmarshal([]byte(s), &arr); err == nil {
		return arr, nil
	}
	var cs []composeContainer
	for _, line := range strings.Split(s, "\n") {
		line = strings.TrimSpace(line)
		if line == "" {
			continue
		}
		var c composeContainer
		if err := json.Unmarshal([]byte(line), &c); err != nil {
			// USER-FACING
			return nil, fmt.Errorf("could not parse %q: %w", line, err)
		}
		cs = append(cs, c)
	}
	return cs, nil
}

// verifyMandatoryRunning checks the parsed ps output: every container of
// mandatoryRunning must be in state "running", and every container of
// mandatoryExited must have exited 0. It returns the names that are not up
// and the init containers that failed.
func verifyMandatoryRunning(cs []composeContainer) (notRunning, failedInit []string) {
	byName := make(map[string]composeContainer, len(cs))
	for _, c := range cs {
		byName[strings.TrimPrefix(c.Name, "/")] = c
	}
	for _, name := range mandatoryRunning {
		c, ok := byName[name]
		if !ok || c.State != "running" {
			notRunning = append(notRunning, name)
		}
	}
	for _, name := range mandatoryExited {
		c, ok := byName[name]
		if !ok {
			continue // it may have been cleaned up after completing successfully
		}
		if c.State == "running" {
			continue
		}
		if c.State != "exited" || c.ExitCode != 0 {
			failedInit = append(failedInit, fmt.Sprintf("%s (exit %d)", name, c.ExitCode))
		}
	}
	return notRunning, failedInit
}
