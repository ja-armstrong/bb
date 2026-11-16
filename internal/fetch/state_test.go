package fetch

import (
	"errors"
	"os"
	"path/filepath"
	"testing"
)

func TestStatePath(t *testing.T) {
	sep := string(filepath.Separator)
	cases := []struct {
		name      string
		configDir string
		want      string
	}{
		{
			name:      "home config dir",
			configDir: filepath.Join(sep, "home", "u", ".config"),
			want:      filepath.Join(sep, "home", "u", ".config", "bitbeard", "stack.json"),
		},
		{
			name:      "plain dir",
			configDir: "conf",
			want:      filepath.Join("conf", "bitbeard", "stack.json"),
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if got := StatePath(tc.configDir); got != tc.want {
				t.Fatalf("StatePath(%q) = %q, want %q", tc.configDir, got, tc.want)
			}
		})
	}
}

func TestSaveLoadStateRoundTrip(t *testing.T) {
	path := filepath.Join(t.TempDir(), "nested", "state.json")
	want := State{StackPath: filepath.Join(string(filepath.Separator), "home", "u", "bitbeard")}

	if err := SaveState(path, want); err != nil {
		t.Fatalf("SaveState: %v", err)
	}
	got, err := LoadState(path)
	if err != nil {
		t.Fatalf("LoadState: %v", err)
	}
	if got != want {
		t.Fatalf("round trip = %+v, want %+v", got, want)
	}
}

func TestLoadStateMissing(t *testing.T) {
	_, err := LoadState(filepath.Join(t.TempDir(), "absent.json"))
	if !errors.Is(err, ErrNoState) {
		t.Fatalf("LoadState on missing file: err = %v, want ErrNoState", err)
	}
}

func TestLoadStateMalformed(t *testing.T) {
	path := filepath.Join(t.TempDir(), "state.json")
	if err := os.WriteFile(path, []byte("{not json"), 0o644); err != nil {
		t.Fatalf("writing malformed state: %v", err)
	}
	_, err := LoadState(path)
	if err == nil {
		t.Fatal("LoadState on malformed file: want error, got nil")
	}
	if errors.Is(err, ErrNoState) {
		t.Fatalf("LoadState on malformed file: err = %v, want a distinct error, not ErrNoState", err)
	}
}

func TestLoadStateEmptyPathCountsAsNoState(t *testing.T) {
	path := filepath.Join(t.TempDir(), "state.json")
	if err := os.WriteFile(path, []byte("{}\n"), 0o644); err != nil {
		t.Fatalf("writing state: %v", err)
	}
	st, err := LoadState(path)
	if err != nil {
		t.Fatalf("LoadState: %v", err)
	}
	if st.StackPath != "" {
		t.Fatalf("StackPath = %q, want empty", st.StackPath)
	}
}
