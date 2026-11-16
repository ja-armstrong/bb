package fetch

import (
	"encoding/json"
	"errors"
	"fmt"
	"io/fs"
	"os"
	"path/filepath"
)

// State is what the Bootstrap remembers about this machine between runs.
type State struct {
	// StackPath is where the stack repository was cloned.
	StackPath string `json:"stack_path"`
}

// ErrNoState means the state file does not exist yet: the caller should
// behave as on first run.
var ErrNoState = errors.New("no state file")

// StatePath is the location of the per-user state file under configDir.
func StatePath(configDir string) string {
	return filepath.Join(configDir, "bitbeard", "stack.json")
}

// LoadState reads the state file. It returns ErrNoState when the file is
// missing. The state file is a cache of derived facts: a malformed file is
// reported as an error, but callers may treat any error like ErrNoState and
// re-derive everything.
func LoadState(path string) (State, error) {
	data, err := os.ReadFile(path)
	if errors.Is(err, fs.ErrNotExist) {
		return State{}, ErrNoState
	}
	if err != nil {
		return State{}, fmt.Errorf("reading state file %s: %w", path, err)
	}
	var st State
	if err := json.Unmarshal(data, &st); err != nil {
		// USER-FACING
		return State{}, fmt.Errorf("state file %s is malformed; it can safely be deleted and will be recreated", path)
	}
	return st, nil
}

// SaveState writes the state file, creating parent directories as needed.
func SaveState(path string, st State) error {
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		return fmt.Errorf("creating state directory: %w", err)
	}
	data, err := json.MarshalIndent(st, "", "  ")
	if err != nil {
		return err
	}
	data = append(data, '\n')
	if err := os.WriteFile(path, data, 0o644); err != nil {
		return fmt.Errorf("writing state file %s: %w", path, err)
	}
	return nil
}
