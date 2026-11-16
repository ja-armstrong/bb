package prereqs

import (
	"reflect"
	"strings"
	"testing"
)

func TestBuildPlan(t *testing.T) {
	ubuntu := distro{ID: "ubuntu", IDLike: []string{"debian"}}
	debian := distro{ID: "debian"}

	tests := []struct {
		name        string
		d           distro
		needDocker  bool
		needCompose bool
		operator    string
		want        []string // display lines, in order
	}{
		{
			name: "nothing missing yields an empty plan",
		},
		{
			name:        "ubuntu missing both",
			d:           ubuntu,
			needDocker:  true,
			needCompose: true,
			operator:    "operator",
			want: []string{
				"sudo apt-get update",
				"sudo apt-get install -y docker.io",
				"sudo usermod -aG docker operator",
				"sudo apt-get install -y docker-compose-v2",
			},
		},
		{
			name:       "ubuntu missing only docker",
			d:          ubuntu,
			needDocker: true,
			operator:   "operator",
			want: []string{
				"sudo apt-get update",
				"sudo apt-get install -y docker.io",
				"sudo usermod -aG docker operator",
			},
		},
		{
			name:       "unknown operator skips the group step",
			d:          ubuntu,
			needDocker: true,
			want: []string{
				"sudo apt-get update",
				"sudo apt-get install -y docker.io",
			},
		},
		{
			name:        "ubuntu missing only compose",
			d:           ubuntu,
			needCompose: true,
			operator:    "operator",
			want: []string{
				"sudo apt-get update",
				"sudo apt-get install -y docker-compose-v2",
			},
		},
		{
			name:        "debian missing both",
			d:           debian,
			needDocker:  true,
			needCompose: true,
			operator:    "operator",
			want: []string{
				"sudo apt-get update",
				"sudo apt-get install -y docker.io",
				"sudo usermod -aG docker operator",
				"sudo apt-get install -y docker-compose",
			},
		},
		{
			name:        "debian descendant missing only compose",
			d:           distro{ID: "kali", IDLike: []string{"debian"}},
			needCompose: true,
			operator:    "operator",
			want: []string{
				"sudo apt-get update",
				"sudo apt-get install -y docker-compose",
			},
		},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			plan := buildPlan(tt.d, tt.needDocker, tt.needCompose, tt.operator)
			var got []string
			for _, a := range plan {
				got = append(got, a.primary.display())
			}
			if !reflect.DeepEqual(got, tt.want) {
				t.Errorf("plan displays =\n%q\nwant\n%q", got, tt.want)
			}
		})
	}
}

func TestComposePackageChain(t *testing.T) {
	tests := []struct {
		name string
		d    distro
		want []string
	}{
		{name: "ubuntu tries v2 first", d: distro{ID: "ubuntu"}, want: []string{"docker-compose-v2", "docker-compose"}},
		{name: "ubuntu descendant tries v2 first", d: distro{ID: "linuxmint", IDLike: []string{"ubuntu", "debian"}}, want: []string{"docker-compose-v2", "docker-compose"}},
		{name: "debian tries docker-compose first", d: distro{ID: "debian"}, want: []string{"docker-compose", "docker-compose-v2"}},
		{name: "debian descendant tries docker-compose first", d: distro{ID: "raspbian", IDLike: []string{"debian"}}, want: []string{"docker-compose", "docker-compose-v2"}},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			if got := composePackageChain(tt.d); !reflect.DeepEqual(got, tt.want) {
				t.Errorf("composePackageChain(%+v) = %v, want %v", tt.d, got, tt.want)
			}
		})
	}
}

func TestActionCommandsFallbackOrder(t *testing.T) {
	a := action{
		primary:   sudo("apt-get", "install", "-y", "docker-compose-v2"),
		fallbacks: []step{sudo("apt-get", "install", "-y", "docker-compose")},
	}
	cmds := a.commands()
	if len(cmds) != 2 {
		t.Fatalf("len(commands) = %d, want 2", len(cmds))
	}
	if got := cmds[0].display(); got != "sudo apt-get install -y docker-compose-v2" {
		t.Errorf("commands[0] = %q", got)
	}
	if got := cmds[1].display(); got != "sudo apt-get install -y docker-compose" {
		t.Errorf("commands[1] = %q", got)
	}
}

func TestInstallInstructions(t *testing.T) {
	tests := []struct {
		name     string
		d        distro
		contains string
	}{
		{name: "fedora", d: distro{ID: "fedora"}, contains: "dnf install"},
		{name: "arch", d: distro{ID: "arch", IDLike: []string{"arch"}}, contains: "pacman"},
		{name: "opensuse", d: distro{ID: "opensuse-leap", IDLike: []string{"suse"}}, contains: "zypper"},
		{name: "alpine", d: distro{ID: "alpine"}, contains: "apk add"},
		{name: "unknown", d: distro{}, contains: "docs.docker.com"},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			got := installInstructions(tt.d)
			if !strings.Contains(got, tt.contains) {
				t.Errorf("installInstructions(%+v) = %q, want it to contain %q", tt.d, got, tt.contains)
			}
		})
	}
}

func TestShellQuote(t *testing.T) {
	tests := []struct {
		arg  string
		want string
	}{
		{arg: "apt-get", want: "apt-get"},
		{arg: "-y", want: "-y"},
		{arg: "docker.io", want: "docker.io"},
		{arg: "has space", want: `"has space"`},
		{arg: "", want: `""`},
		{arg: "a;b", want: `"a;b"`},
		{arg: "a$b", want: `"a$b"`},
	}
	for _, tt := range tests {
		if got := shellQuote(tt.arg); got != tt.want {
			t.Errorf("shellQuote(%q) = %q, want %q", tt.arg, got, tt.want)
		}
	}
}
