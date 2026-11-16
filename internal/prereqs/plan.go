package prereqs

import (
	"strconv"
	"strings"
)

// step is a single command to show to the operator and run.
type step struct {
	name string
	args []string
}

// display renders the exact command line the operator is shown.
func (s step) display() string {
	parts := make([]string, 0, len(s.args)+1)
	parts = append(parts, shellQuote(s.name))
	for _, a := range s.args {
		parts = append(parts, shellQuote(a))
	}
	return strings.Join(parts, " ")
}

// action is one privileged operation: a primary command plus fallback
// commands that are shown and run only if the primary fails.
type action struct {
	primary   step
	fallbacks []step
}

// commands lists what runAction will try, in order.
func (a action) commands() []step {
	return append([]step{a.primary}, a.fallbacks...)
}

// sudo builds a step that runs name under sudo.
func sudo(name string, args ...string) step {
	return step{name: "sudo", args: append([]string{name}, args...)}
}

// buildPlan constructs the privileged actions needed for the missing
// prerequisites. A distro's own packages are preferred over the upstream
// get.docker.com script: they keep apt as the single owner of the daemon
// and cover both Ubuntu (docker-compose-v2) and Debian (docker-compose,
// which is Compose v2 on trixie) without adding third-party repository keys.
// Installing Docker also adds the operator to the `docker` group, so a new
// login session can reach the daemon without sudo; the step is skipped when
// the operator's name is unknown.
func buildPlan(d distro, needDocker, needCompose bool, operator string) []action {
	if !needDocker && !needCompose {
		return nil
	}
	var plan []action
	plan = append(plan, action{primary: sudo("apt-get", "update")})
	if needDocker {
		plan = append(plan, action{primary: sudo("apt-get", "install", "-y", "docker.io")})
		if operator != "" {
			plan = append(plan, action{primary: sudo("usermod", "-aG", "docker", operator)})
		}
	}
	if needCompose {
		chain := composePackageChain(d)
		var fallbacks []step
		for _, pkg := range chain[1:] {
			fallbacks = append(fallbacks, sudo("apt-get", "install", "-y", pkg))
		}
		plan = append(plan, action{
			primary:   sudo("apt-get", "install", "-y", chain[0]),
			fallbacks: fallbacks,
		})
	}
	return plan
}

// composePackageChain lists the distro's Docker Compose packages in the
// order they should be tried. Ubuntu and its descendants ship Compose v2 as
// docker-compose-v2; Debian and its descendants ship docker-compose (v1 on
// bookworm, v2 on trixie), so the other name is kept as a last-resort
// fallback for either family.
func composePackageChain(d distro) []string {
	if d.isUbuntuLike() {
		return []string{"docker-compose-v2", "docker-compose"}
	}
	return []string{"docker-compose", "docker-compose-v2"}
}

// installInstructions returns the exact commands an operator should run on
// a distro this stage will not install on itself.
func installInstructions(d distro) string {
	switch {
	case d.isFedoraLike():
		return "sudo dnf install -y moby-engine docker-compose"
	case d.isArchLike():
		return "sudo pacman -S --needed docker docker-compose"
	case d.isSuseLike():
		return "sudo zypper install -y docker docker-compose"
	case d.isAlpine():
		return "sudo apk add docker docker-cli-compose"
	default:
		return "install Docker Engine and the Compose plugin with your distro's package manager — see https://docs.docker.com/engine/install/"
	}
}

// shellQuote quotes an argument for display when the shell would interpret
// any part of it.
func shellQuote(arg string) string {
	if arg == "" || strings.ContainsAny(arg, " \t\"'\\$`&|;<>(){}[]*?~#") {
		return strconv.Quote(arg)
	}
	return arg
}
