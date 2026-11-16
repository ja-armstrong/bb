package prereqs

import (
	"fmt"
	"os"
	"strings"
)

// distro is what the prerequisite stage needs to know about the running
// distribution, parsed from /etc/os-release.
type distro struct {
	// ID is the lowercased ID field, e.g. "ubuntu". Empty when unknown.
	ID string
	// IDLike holds the split ID_LIKE fields, e.g. ["ubuntu", "debian"].
	IDLike []string
}

// supported reports whether this stage can install prerequisites itself:
// Ubuntu/Debian and their descendants.
func (d distro) supported() bool {
	return d.isUbuntuLike() || d.isDebianLike()
}

// isUbuntuLike reports whether the distro is Ubuntu or a descendant of it.
func (d distro) isUbuntuLike() bool {
	return d.ID == "ubuntu" || contains(d.IDLike, "ubuntu")
}

// isDebianLike reports whether the distro is Debian or a descendant of it.
func (d distro) isDebianLike() bool {
	return d.ID == "debian" || contains(d.IDLike, "debian")
}

// isFedoraLike reports whether the distro uses dnf and Fedora packaging.
func (d distro) isFedoraLike() bool {
	return d.ID == "fedora" || contains(d.IDLike, "fedora")
}

// isArchLike reports whether the distro is Arch or a descendant of it.
func (d distro) isArchLike() bool {
	return d.ID == "arch" || contains(d.IDLike, "arch")
}

// isSuseLike reports whether the distro uses zypper and SUSE packaging.
func (d distro) isSuseLike() bool {
	return d.ID == "sles" || contains(d.IDLike, "suse")
}

// isAlpine reports whether the distro is Alpine.
func (d distro) isAlpine() bool {
	return d.ID == "alpine"
}

// readOSRelease reads and parses the os-release file at path.
func readOSRelease(path string) (distro, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return distro{}, fmt.Errorf("reading %s: %w", path, err)
	}
	return parseOSRelease(string(data)), nil
}

// parseOSRelease parses os-release content: KEY=value lines with optional
// single- or double-quoted values, comments and blank lines ignored.
func parseOSRelease(data string) distro {
	var d distro
	for _, line := range strings.Split(data, "\n") {
		line = strings.TrimSpace(line)
		if line == "" || strings.HasPrefix(line, "#") {
			continue
		}
		key, value, ok := strings.Cut(line, "=")
		if !ok {
			continue
		}
		value = stripQuotes(value)
		switch key {
		case "ID":
			d.ID = strings.ToLower(value)
		case "ID_LIKE":
			d.IDLike = strings.Fields(strings.ToLower(value))
		}
	}
	return d
}

// stripQuotes removes one pair of surrounding matching quotes.
func stripQuotes(v string) string {
	if len(v) >= 2 && (v[0] == '"' || v[0] == '\'') && v[len(v)-1] == v[0] {
		return v[1 : len(v)-1]
	}
	return v
}

func contains(list []string, want string) bool {
	for _, v := range list {
		if v == want {
			return true
		}
	}
	return false
}
