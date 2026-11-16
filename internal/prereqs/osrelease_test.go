package prereqs

import "testing"

func TestParseOSRelease(t *testing.T) {
	tests := []struct {
		name     string
		content  string
		wantID   string
		wantLike []string
	}{
		{
			name: "ubuntu",
			content: "PRETTY_NAME=\"Ubuntu 22.04.5 LTS\"\n" +
				"NAME=\"Ubuntu\"\nID=ubuntu\nID_LIKE=debian\nVERSION_ID=\"22.04\"\n",
			wantID:   "ubuntu",
			wantLike: []string{"debian"},
		},
		{
			name: "debian with quoted values",
			content: "PRETTY_NAME=\"Debian GNU/Linux 12 (bookworm)\"\n" +
				"ID=debian\nVERSION=\"12 (bookworm)\"\nVERSION_CODENAME=bookworm\n",
			wantID: "debian",
		},
		{
			name:     "pop is ubuntu-like and debian-like",
			content:  "ID=pop\nID_LIKE=\"ubuntu debian\"\n",
			wantID:   "pop",
			wantLike: []string{"ubuntu", "debian"},
		},
		{
			name:     "linuxmint is debian-like via ID_LIKE",
			content:  "ID=linuxmint\nID_LIKE=\"debian\"\n",
			wantID:   "linuxmint",
			wantLike: []string{"debian"},
		},
		{
			name:    "single quotes are stripped",
			content: "ID='fedora'\n",
			wantID:  "fedora",
		},
		{
			name:    "comments and blank lines are ignored",
			content: "# a comment\n\nID=arch\n",
			wantID:  "arch",
		},
		{
			name:    "malformed lines are ignored",
			content: "garbage line without equals\nID=alpine\n",
			wantID:  "alpine",
		},
		{
			name:    "empty content yields unknown distro",
			content: "",
			wantID:  "",
		},
		{
			name:    "case of ID is normalized",
			content: "ID=Ubuntu\n",
			wantID:  "ubuntu",
		},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			d := parseOSRelease(tt.content)
			if d.ID != tt.wantID {
				t.Errorf("ID = %q, want %q", d.ID, tt.wantID)
			}
			if len(d.IDLike) != len(tt.wantLike) {
				t.Fatalf("IDLike = %v, want %v", d.IDLike, tt.wantLike)
			}
			for i, v := range tt.wantLike {
				if d.IDLike[i] != v {
					t.Errorf("IDLike[%d] = %q, want %q", i, d.IDLike[i], v)
				}
			}
		})
	}
}

func TestDistroSupport(t *testing.T) {
	tests := []struct {
		name        string
		d           distro
		wantSupport bool
		wantUbuntu  bool
		wantDebian  bool
	}{
		{name: "ubuntu", d: distro{ID: "ubuntu"}, wantSupport: true, wantUbuntu: true},
		{name: "debian", d: distro{ID: "debian"}, wantSupport: true, wantDebian: true},
		{name: "pop", d: distro{ID: "pop", IDLike: []string{"ubuntu", "debian"}}, wantSupport: true, wantUbuntu: true, wantDebian: true},
		{name: "kali", d: distro{ID: "kali", IDLike: []string{"debian"}}, wantSupport: true, wantDebian: true},
		{name: "linuxmint", d: distro{ID: "linuxmint", IDLike: []string{"ubuntu", "debian"}}, wantSupport: true, wantUbuntu: true, wantDebian: true},
		{name: "fedora", d: distro{ID: "fedora", IDLike: []string{"fedora", "rhel"}}, wantSupport: false},
		{name: "arch", d: distro{ID: "arch", IDLike: []string{"arch"}}, wantSupport: false},
		{name: "opensuse-leap", d: distro{ID: "opensuse-leap", IDLike: []string{"suse", "sles"}}, wantSupport: false},
		{name: "alpine", d: distro{ID: "alpine"}, wantSupport: false},
		{name: "unknown", d: distro{}, wantSupport: false},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			if got := tt.d.supported(); got != tt.wantSupport {
				t.Errorf("supported() = %v, want %v", got, tt.wantSupport)
			}
			if got := tt.d.isUbuntuLike(); got != tt.wantUbuntu {
				t.Errorf("isUbuntuLike() = %v, want %v", got, tt.wantUbuntu)
			}
			if got := tt.d.isDebianLike(); got != tt.wantDebian {
				t.Errorf("isDebianLike() = %v, want %v", got, tt.wantDebian)
			}
		})
	}
}

func TestReadOSReleaseMissingFile(t *testing.T) {
	if _, err := readOSRelease(t.TempDir() + "/no-such-file"); err == nil {
		t.Fatal("expected an error for a missing os-release file")
	}
}
