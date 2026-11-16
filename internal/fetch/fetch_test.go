package fetch

import (
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// fakeGit records clone calls and answers RemoteURL from a dir → URL map.
// Clone simulates a real clone: it creates dest with the go.mod repo marker
// and points the origin remote at the cloned URL.
type fakeGit struct {
	remotes  map[string]string // dir → origin URL; absent key = not a git repo
	cloneErr error             // returned by Clone
	clones   []string          // recorded "url|dest" clone calls
}

func (f *fakeGit) Clone(repoURL, dest string) error {
	f.clones = append(f.clones, repoURL+"|"+dest)
	if f.cloneErr != nil {
		return f.cloneErr
	}
	if f.remotes == nil {
		f.remotes = map[string]string{}
	}
	f.remotes[dest] = repoURL
	if err := os.MkdirAll(filepath.Join(dest, "cmd", "bitbeard"), 0o755); err != nil {
		return err
	}
	return os.WriteFile(filepath.Join(dest, "go.mod"),
		[]byte("module "+modulePath+"\n\ngo 1.22\n"), 0o644)
}

func (f *fakeGit) RemoteURL(dir string) (string, error) {
	url, ok := f.remotes[dir]
	if !ok {
		return "", errors.New("not a git repository")
	}
	return url, nil
}

func TestNormalizeURL(t *testing.T) {
	cases := []struct {
		name string
		raw  string
		want string
	}{
		{name: "plain https", raw: "https://github.com/ja-armstrong/bb", want: "github.com/ja-armstrong/bb"},
		{name: "https with .git", raw: "https://github.com/ja-armstrong/bb.git", want: "github.com/ja-armstrong/bb"},
		{name: "https with trailing slash", raw: "https://github.com/ja-armstrong/bb/", want: "github.com/ja-armstrong/bb"},
		{name: "git with .git and slash", raw: "https://github.com/ja-armstrong/bb.git/", want: "github.com/ja-armstrong/bb"},
		{name: "scp style", raw: "git@github.com:ja-armstrong/bb", want: "github.com/ja-armstrong/bb"},
		{name: "scp style with .git", raw: "git@github.com:ja-armstrong/bb.git", want: "github.com/ja-armstrong/bb"},
		{name: "ssh url", raw: "ssh://git@github.com/ja-armstrong/bb", want: "github.com/ja-armstrong/bb"},
		{name: "case insensitive", raw: "https://GitHub.com/Ja-Armstrong/BB", want: "github.com/ja-armstrong/bb"},
		{name: "different repo", raw: "https://github.com/someone/else", want: "github.com/someone/else"},
		{name: "empty", raw: "", want: ""},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if got := normalizeURL(tc.raw); got != tc.want {
				t.Fatalf("normalizeURL(%q) = %q, want %q", tc.raw, got, tc.want)
			}
		})
	}
}

func TestNormalizeURLOwnSpellingsAgree(t *testing.T) {
	spellings := []string{
		RepoURL,
		"https://github.com/ja-armstrong/bb",
		"git@github.com:ja-armstrong/bb",
		"ssh://git@github.com/ja-armstrong/bb.git",
	}
	first := normalizeURL(spellings[0])
	for _, s := range spellings[1:] {
		if got := normalizeURL(s); got != first {
			t.Fatalf("normalizeURL(%q) = %q, want %q", s, got, first)
		}
	}
}

// seed writes a small file into dir so it counts as an occupied directory.
func seed(t *testing.T, dir, name string) {
	t.Helper()
	if err := os.WriteFile(filepath.Join(dir, name), []byte("x"), 0o644); err != nil {
		t.Fatal(err)
	}
}

func TestClassify(t *testing.T) {
	repoURL := RepoURL

	cases := []struct {
		name    string
		setup   func(t *testing.T) (dir string, g *fakeGit)
		want    verdict
		wantErr bool
	}{
		{
			name: "missing directory",
			setup: func(t *testing.T) (string, *fakeGit) {
				return filepath.Join(t.TempDir(), "absent"), &fakeGit{}
			},
			want: verdictMissing,
		},
		{
			name: "empty directory counts as missing",
			setup: func(t *testing.T) (string, *fakeGit) {
				dir := filepath.Join(t.TempDir(), "empty")
				if err := os.MkdirAll(dir, 0o755); err != nil {
					t.Fatal(err)
				}
				return dir, &fakeGit{}
			},
			want: verdictMissing,
		},
		{
			name: "existing clone with matching remote",
			setup: func(t *testing.T) (string, *fakeGit) {
				dir := t.TempDir()
				seed(t, dir, "README.md")
				return dir, &fakeGit{remotes: map[string]string{dir: repoURL}}
			},
			want: verdictBitbeard,
		},
		{
			name: "matching remote in non-canonical spelling",
			setup: func(t *testing.T) (string, *fakeGit) {
				dir := t.TempDir()
				seed(t, dir, "README.md")
				return dir, &fakeGit{remotes: map[string]string{dir: "git@github.com:ja-armstrong/bb.git"}}
			},
			want: verdictBitbeard,
		},
		{
			name: "existing git repo with different remote",
			setup: func(t *testing.T) (string, *fakeGit) {
				dir := t.TempDir()
				seed(t, dir, "README.md")
				return dir, &fakeGit{remotes: map[string]string{dir: "https://github.com/someone/else"}}
			},
			want: verdictForeign,
		},
		{
			name: "not a git repo but carries the go.mod marker",
			setup: func(t *testing.T) (string, *fakeGit) {
				dir := t.TempDir()
				if err := os.WriteFile(filepath.Join(dir, "go.mod"),
					[]byte("module github.com/ja-armstrong/bb\n"), 0o644); err != nil {
					t.Fatal(err)
				}
				return dir, &fakeGit{}
			},
			want: verdictBitbeard,
		},
		{
			name: "not a git repo and no marker",
			setup: func(t *testing.T) (string, *fakeGit) {
				dir := t.TempDir()
				if err := os.WriteFile(filepath.Join(dir, "notes.txt"), []byte("hi"), 0o644); err != nil {
					t.Fatal(err)
				}
				return dir, &fakeGit{}
			},
			want: verdictForeign,
		},
		{
			name: "go.mod with a different module is foreign",
			setup: func(t *testing.T) (string, *fakeGit) {
				dir := t.TempDir()
				if err := os.WriteFile(filepath.Join(dir, "go.mod"),
					[]byte("module example.com/other\n"), 0o644); err != nil {
					t.Fatal(err)
				}
				return dir, &fakeGit{}
			},
			want: verdictForeign,
		},
		{
			name: "a plain file where a directory is expected",
			setup: func(t *testing.T) (string, *fakeGit) {
				dir := filepath.Join(t.TempDir(), "file")
				if err := os.WriteFile(dir, []byte("x"), 0o644); err != nil {
					t.Fatal(err)
				}
				return dir, &fakeGit{}
			},
			want: verdictForeign,
		},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			dir, g := tc.setup(t)
			got, err := classify(dir, repoURL, g)
			if (err != nil) != tc.wantErr {
				t.Fatalf("classify(%q): err = %v, wantErr = %v", dir, err, tc.wantErr)
			}
			if got != tc.want {
				t.Fatalf("classify(%q) = %v, want %v", dir, got, tc.want)
			}
		})
	}
}

func TestRun(t *testing.T) {
	repoURL := RepoURL

	// fixture builds the per-case world and returns the pieces Run needs.
	type fixture struct {
		target    string
		statePath string
		g         *fakeGit
	}
	mkFixture := func() func(t *testing.T) fixture {
		return func(t *testing.T) fixture {
			base := t.TempDir()
			return fixture{
				target:    filepath.Join(base, "stack"),
				statePath: filepath.Join(base, "config", "bitbeard", "stack.json"),
				g:         &fakeGit{remotes: map[string]string{}},
			}
		}
	}

	cases := []struct {
		name        string
		setup       func(t *testing.T) fixture
		wantCloned  bool
		wantErr     string // substring of the error, empty when no error expected
		wantNoState bool   // the state file must not exist after the run
	}{
		{
			name:       "first run clones to the target",
			setup:      mkFixture(),
			wantCloned: true,
		},
		{
			name: "existing stack clone at target is reused without re-fetching",
			setup: func(t *testing.T) fixture {
				f := mkFixture()(t)
				if err := os.MkdirAll(f.target, 0o755); err != nil {
					t.Fatal(err)
				}
				seed(t, f.target, "README.md")
				f.g.remotes[f.target] = repoURL
				return f
			},
		},
		{
			name: "remembered location is reused",
			setup: func(t *testing.T) fixture {
				f := mkFixture()(t)
				if err := os.MkdirAll(f.target, 0o755); err != nil {
					t.Fatal(err)
				}
				seed(t, f.target, "README.md")
				f.g.remotes[f.target] = repoURL
				if err := SaveState(f.statePath, State{StackPath: f.target}); err != nil {
					t.Fatal(err)
				}
				return f
			},
		},
		{
			name: "vanished remembered location recovers by re-cloning",
			setup: func(t *testing.T) fixture {
				f := mkFixture()(t)
				gone := filepath.Join(filepath.Dir(f.target), "gone")
				if err := SaveState(f.statePath, State{StackPath: gone}); err != nil {
					t.Fatal(err)
				}
				return f
			},
			wantCloned: true,
		},
		{
			name: "foreign directory at target is refused",
			setup: func(t *testing.T) fixture {
				f := mkFixture()(t)
				if err := os.MkdirAll(f.target, 0o755); err != nil {
					t.Fatal(err)
				}
				if err := os.WriteFile(filepath.Join(f.target, "notes.txt"), []byte("mine"), 0o644); err != nil {
					t.Fatal(err)
				}
				return f
			},
			wantErr: "not the Bitbeard stack repository",
		},
		{
			name: "foreign remembered location is refused",
			setup: func(t *testing.T) fixture {
				f := mkFixture()(t)
				if err := os.MkdirAll(f.target, 0o755); err != nil {
					t.Fatal(err)
				}
				seed(t, f.target, "README.md")
				f.g.remotes[f.target] = "https://github.com/someone/else"
				if err := SaveState(f.statePath, State{StackPath: f.target}); err != nil {
					t.Fatal(err)
				}
				return f
			},
			wantErr: "not the Bitbeard stack repository",
		},
		{
			name: "clone failure surfaces and writes no state",
			setup: func(t *testing.T) fixture {
				f := mkFixture()(t)
				f.g.cloneErr = errors.New("git clone boom")
				return f
			},
			wantErr:     "git clone boom",
			wantNoState: true,
		},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			f := tc.setup(t)
			cfg := Config{RepoURL: repoURL, Target: f.target}

			res, err := Run(cfg, f.g, f.statePath)

			if tc.wantErr != "" {
				if err == nil {
					t.Fatalf("Run: want error containing %q, got result %+v and nil error", tc.wantErr, res)
				}
				if !strings.Contains(err.Error(), tc.wantErr) {
					t.Fatalf("Run: err = %v, want it to contain %q", err, tc.wantErr)
				}
				if tc.wantNoState {
					if _, serr := LoadState(f.statePath); !errors.Is(serr, ErrNoState) {
						t.Fatalf("state file written despite failure: LoadState err = %v", serr)
					}
				}
				return
			}
			if err != nil {
				t.Fatalf("Run: unexpected error: %v", err)
			}
			if res.Cloned != tc.wantCloned {
				t.Fatalf("Run: Cloned = %v, want %v", res.Cloned, tc.wantCloned)
			}
			if res.StackPath != f.target {
				t.Fatalf("Run: StackPath = %q, want %q", res.StackPath, f.target)
			}

			// The state file must now remember the resolved location.
			st, err := LoadState(f.statePath)
			if err != nil {
				t.Fatalf("LoadState after Run: %v", err)
			}
			if st.StackPath != f.target {
				t.Fatalf("state StackPath = %q, want %q", st.StackPath, f.target)
			}

			// A reuse run must not have called git clone.
			if !tc.wantCloned && len(f.g.clones) != 0 {
				t.Fatalf("Run reused but called git clone: %v", f.g.clones)
			}
		})
	}
}

func TestRunSecondInvocationReusesWithoutCloning(t *testing.T) {
	base := t.TempDir()
	target := filepath.Join(base, "stack")
	statePath := filepath.Join(base, "config", "bitbeard", "stack.json")
	cfg := Config{RepoURL: RepoURL, Target: target}

	g := &fakeGit{}
	first, err := Run(cfg, g, statePath)
	if err != nil {
		t.Fatalf("first Run: %v", err)
	}
	if !first.Cloned {
		t.Fatal("first Run: Cloned = false, want true")
	}

	second, err := Run(cfg, g, statePath)
	if err != nil {
		t.Fatalf("second Run: %v", err)
	}
	if second.Cloned {
		t.Fatal("second Run: Cloned = true, want false")
	}
	if len(g.clones) != 1 {
		t.Fatalf("git clone called %d times across two runs, want 1", len(g.clones))
	}
}
