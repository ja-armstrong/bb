// Package fetch implements the fetch stage of Bitbeard Bootstrap: it makes
// sure the stack repository is available on this machine, cloning it on first
// run and reusing an existing clone afterwards. The clone location is
// remembered in a per-user state file so later commands find the stack
// without re-deriving it.
package fetch

import (
	"errors"
	"fmt"
	"io/fs"
	"os"
	"path/filepath"
	"strings"
)

// RepoURL is the source of the stack repository.
const RepoURL = "https://github.com/ja-armstrong/bb.git"

// modulePath is the Go module declared at the repository root. Its presence
// in a go.mod is the repo marker used when git cannot be asked.
const modulePath = "github.com/ja-armstrong/bb"

// Git is the small slice of git the fetch stage needs, so tests can fake it.
type Git interface {
	// Clone clones repoURL into dest.
	Clone(repoURL, dest string) error
	// RemoteURL returns the URL of the origin remote of the repository at
	// dir, or an error when dir is not a git repository.
	RemoteURL(dir string) (string, error)
}

// Config parameterises Run.
type Config struct {
	// RepoURL to clone; empty means RepoURL.
	RepoURL string
	// Target is where to clone when nothing is remembered; empty means
	// DefaultTarget of the user's home directory.
	Target string
}

// Result reports what Run did.
type Result struct {
	// StackPath is where the stack repository now lives.
	StackPath string
	// Cloned is true when Run cloned the repository on this run.
	Cloned bool
}

// DefaultTarget is where the stack is cloned when nothing else is known.
func DefaultTarget(home string) string {
	return filepath.Join(home, "bitbeard")
}

// Run makes sure the stack repository is available: it reuses an existing
// clone, records the location in the state file at statePath, and clones only
// when there is nothing to reuse. A remembered location that has vanished is
// recovered by re-cloning (first-run behaviour). A directory that exists but
// is not the stack repository is never touched; Run fails with a clear error.
func Run(cfg Config, g Git, statePath string) (Result, error) {
	repoURL := cfg.RepoURL
	if repoURL == "" {
		repoURL = RepoURL
	}

	// A remembered location wins when it still holds the stack.
	if st, err := LoadState(statePath); err == nil && st.StackPath != "" {
		v, cerr := classify(st.StackPath, repoURL, g)
		if cerr == nil {
			if v == verdictForeign {
				// USER-FACING
				return Result{}, fmt.Errorf("%s is remembered as the stack location but is not the Bitbeard stack repository; remove or rename it and run \"bitbeard setup\" again", st.StackPath)
			}
			if v == verdictBitbeard {
				return Result{StackPath: st.StackPath}, nil
			}
			// verdictMissing: the remembered location is gone; re-clone below.
		}
	}

	// First-run behaviour: clone to the target unless it already holds the
	// stack (reuse, never re-clone).
	target := cfg.Target
	if target == "" {
		home, err := os.UserHomeDir()
		if err != nil {
			return Result{}, err
		}
		target = DefaultTarget(home)
	}

	v, err := classify(target, repoURL, g)
	if err != nil {
		return Result{}, err
	}
	switch v {
	case verdictForeign:
		// USER-FACING
		return Result{}, fmt.Errorf("%s already exists but is not the Bitbeard stack repository; remove or rename it and run \"bitbeard setup\" again", target)
	case verdictBitbeard:
		if err := SaveState(statePath, State{StackPath: target}); err != nil {
			return Result{}, err
		}
		return Result{StackPath: target}, nil
	}

	if err := g.Clone(repoURL, target); err != nil {
		return Result{}, err
	}
	// Verify the fresh clone before recording it.
	v, err = classify(target, repoURL, g)
	if err != nil || v != verdictBitbeard {
		// USER-FACING
		return Result{}, errors.New("the fresh clone at " + target + " does not look like the Bitbeard stack repository")
	}
	if err := SaveState(statePath, State{StackPath: target}); err != nil {
		return Result{}, err
	}
	return Result{StackPath: target, Cloned: true}, nil
}

// verdict is what classify decided about a directory.
type verdict int

const (
	verdictMissing  verdict = iota // nothing (or an empty directory) there: safe to clone
	verdictBitbeard                // a clone of the stack repository
	verdictForeign                 // occupied by something else: refuse to touch it
)

// classify decides whether dir is missing, a stack clone, or something else.
// It prefers asking git for the origin remote; the go.mod module marker is
// the fallback for directories git cannot identify.
func classify(dir, repoURL string, g Git) (verdict, error) {
	fi, err := os.Stat(dir)
	if err != nil {
		if errors.Is(err, fs.ErrNotExist) {
			return verdictMissing, nil
		}
		return verdictMissing, err
	}
	if !fi.IsDir() {
		return verdictForeign, nil
	}
	if entries, err := os.ReadDir(dir); err == nil && len(entries) == 0 {
		return verdictMissing, nil // an empty directory is as good as absent
	}
	if url, err := g.RemoteURL(dir); err == nil {
		if url = normalizeURL(url); url != "" && url == normalizeURL(repoURL) {
			return verdictBitbeard, nil
		}
		return verdictForeign, nil
	}
	if hasBitbeardMarker(dir) {
		return verdictBitbeard, nil
	}
	return verdictForeign, nil
}

// hasBitbeardMarker reports whether dir carries the repo marker: a go.mod
// declaring the bitbeard module.
func hasBitbeardMarker(dir string) bool {
	data, err := os.ReadFile(filepath.Join(dir, "go.mod"))
	if err != nil {
		return false
	}
	return strings.Contains(string(data), "module "+modulePath)
}

// normalizeURL reduces the many spellings of a repository URL (https / ssh /
// scp-style, with or without a .git suffix) to one comparable form.
func normalizeURL(raw string) string {
	u := strings.TrimSpace(raw)
	u = strings.TrimSuffix(u, "/")
	u = strings.TrimSuffix(u, ".git")
	if i := strings.Index(u, "://"); i >= 0 {
		u = u[i+3:]
	}
	// Drop a user@ prefix (scp-style: git@github.com:owner/repo).
	if i := strings.Index(u, "@"); i >= 0 {
		if slash := strings.Index(u, "/"); slash == -1 || i < slash {
			u = u[i+1:]
		}
	}
	// scp-style host:path means host/path.
	if i := strings.Index(u, ":"); i >= 0 {
		if slash := strings.Index(u, "/"); slash == -1 || i < slash {
			u = u[:i] + "/" + u[i+1:]
		}
	}
	return strings.ToLower(u)
}
