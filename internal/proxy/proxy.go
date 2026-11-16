// Package proxy implements the Caddy reverse-proxy stage of Bitbeard
// Bootstrap: when the wizard asks for it, it turns a host Caddy into the
// public edge of the stack — installs the distro package if missing,
// writes a managed snippet with the two site blocks generated from the
// operator's answers, makes the main Caddyfile import it (never clobbering
// operator content), validates the resulting configuration, and starts or
// reloads the service. Every privileged command is shown before it runs.
//
// The stage is the wizard's only writer of reverse-proxy configuration
// and the doctor's source of the service check; the decision logic (which
// sites, which import lines, which verdicts) is pure and table-tested.
// Caddy is host infrastructure on purpose (ticket #34): the public door
// survives `docker compose down`, and the API's Docker socket stays the
// only socket the stack holds (ADR-0003).
package proxy

import (
	"errors"
	"fmt"
	"io"
	"os"
	"strconv"
	"strings"

	"github.com/ja-armstrong/bb/internal/prereqs"
)

// Runner is the invocation layer between the stage and the operating
// system, the same shape as prereqs.Runner so production code can pass
// prereqs.NewRunner() straight through.
type Runner interface {
	// CombinedOutput runs an unprivileged command and returns its combined
	// output. Used for detection and validation only.
	CombinedOutput(name string, args ...string) (string, error)
	// RunInteractive runs a privileged command with the process's standard
	// streams attached, so sudo can prompt for a password in the terminal.
	RunInteractive(name string, args ...string) error
}

// Deps carries the boundaries the stage talks to. A nil field is replaced
// with the production implementation; the paths may be overridden (tests
// point them at a temporary directory).
type Deps struct {
	// Run executes commands (sudo etc.); nil means real execution.
	Run Runner
	// DNS resolves the derived names; nil means the machine's resolver.
	DNS DNSChecker
	// IP fetches the machine's public IP; nil means the echo service.
	IP PublicIPSource
	// Ports probes host ports; nil means a localhost TCP dial.
	Ports PortProber
	// OSReleasePath overrides /etc/os-release (tests only).
	OSReleasePath string
	// SnippetPath overrides the managed snippet path (tests only).
	SnippetPath string
	// MainPath overrides the main Caddyfile path (tests only).
	MainPath string
}

// paths is the resolved set of host locations.
type paths struct {
	osRelease string
	snippet   string
	main      string
}

// resolve fills the paths, defaulting the empty ones.
func (d Deps) resolve() paths {
	p := paths{osRelease: defaultOSReleasePath, snippet: DefaultSnippetPath, main: DefaultMainPath}
	if d.OSReleasePath != "" {
		p.osRelease = d.OSReleasePath
	}
	if d.SnippetPath != "" {
		p.snippet = d.SnippetPath
	}
	if d.MainPath != "" {
		p.main = d.MainPath
	}
	return p
}

// defaults returns d with every nil boundary replaced by its production
// implementation.
func (d Deps) defaults() Deps {
	if d.Run == nil {
		d.Run = prereqs.ExecRunner{}
	}
	if d.DNS == nil {
		d.DNS = netDNS{}
	}
	if d.IP == nil {
		d.IP = ipify{}
	}
	if d.Ports == nil {
		d.Ports = dialProber{}
	}
	return d
}

// Apply runs the reverse-proxy stage end to end: ports, DNS pre-flight,
// install, snippet, import, validate, apply. ask is the wizard's yes/no
// question (used only for consent to touch an operator-owned Caddyfile);
// out receives every shown command, prompt result and note.
//
// On any refusal or failure nothing is applied to the running service;
// what did land on disk by then is named in the error, and every step is
// idempotent so a rerun continues cleanly.
func Apply(a Answers, d Deps, out io.Writer, ask func(question string) (bool, error)) error {
	d = d.defaults()
	p := d.resolve()

	// Defence in depth: the wizard validated the answers, but Apply owns
	// the contract with the files it writes, so it re-checks.
	domain, err := ValidateDomain(a.Domain)
	if err != nil {
		return fmt.Errorf("the reverse-proxy stage got an invalid base domain: %v", err)
	}
	a.Domain = domain
	if a.Email != "" {
		email, err := ValidateEmail(a.Email)
		if err != nil {
			return fmt.Errorf("the reverse-proxy stage got an invalid ACME email: %v", err)
		}
		a.Email = email
	}

	// Ports first: refusing here means nothing was installed and nothing
	// written — the cheapest possible stop.
	if err := checkPorts(d.Ports, out); err != nil {
		return err
	}
	preflightDNS(a, d, out)
	if err := ensureInstalled(d.Run, p.osRelease, out); err != nil {
		return err
	}
	if err := writeSnippet(a, d.Run, out, p); err != nil {
		return err
	}
	if err := ensureImport(a, d.Run, out, ask, p); err != nil {
		return err
	}
	if err := validateConfig(d.Run, out, p); err != nil {
		return err
	}
	if err := applyService(d.Run, out); err != nil {
		return err
	}
	warnDNS(a, out)
	return nil
}

// Hosts returns the two public names for a's base domain.
func (a Answers) Hosts() (webui, jellyfin string) {
	return DeriveHosts(a.Domain)
}

// configuredHosts lists the site names a writes: the Webui always, the
// Media Server only when its site block is part of the answers.
func (a Answers) configuredHosts() []string {
	webui, jellyfin := a.Hosts()
	hosts := []string{webui}
	if a.Jellyfin {
		hosts = append(hosts, jellyfin)
	}
	return hosts
}

// checkPorts refuses to fight for 80/443: busy ports mean another web
// server owns them, and the ticket's answer is the manual path of
// docs/proxy.md, not a port war. An unknown probe result (neither a
// connection nor a refusal) is treated as busy — conservative on purpose.
func checkPorts(p PortProber, out io.Writer) error {
	// USER-FACING
	fmt.Fprintln(out, "Checking host ports 80 and 443 (Caddy needs both) ...")
	var busy []string
	for _, port := range []int{80, 443} {
		b, err := p.Busy(port)
		switch {
		case err != nil:
			// USER-FACING
			busy = append(busy, fmt.Sprintf("%d (could not be probed: %v)", port, err))
		case b:
			busy = append(busy, strconv.Itoa(port))
		}
	}
	if len(busy) > 0 {
		// USER-FACING
		fmt.Fprintf(out, "Port(s) %s on this host are already in use, so Caddy cannot serve HTTP/HTTPS there.\n", strings.Join(busy, ", "))
		// USER-FACING
		fmt.Fprintln(out, "The wizard refuses to fight for the ports. Follow the manual reverse-proxy path in docs/proxy.md instead: configure the server that owns the ports with the reference Caddyfile (docs/proxy/Caddyfile) or free the ports and rerun `bitbeard setup`. Nothing was installed or written.")
		return fmt.Errorf("host port(s) %s are busy; refusing to set up Caddy", strings.Join(busy, ", "))
	}
	// USER-FACING
	fmt.Fprintln(out, "Ports 80 and 443 are free.")
	return nil
}

// preflightDNS runs the warn-only DNS pre-flight: both derived names are
// resolved and compared against the machine's public IP. A mismatch is a
// plain warning — DNS can be fixed after setup — and an offline machine
// skips the whole check with a note.
func preflightDNS(a Answers, d Deps, out io.Writer) {
	hosts := a.configuredHosts()
	findings := make([]hostFinding, 0, len(hosts))
	for _, h := range hosts {
		ips, err := d.DNS.LookupHost(h)
		findings = append(findings, hostFinding{host: h, ips: ips, err: err})
	}
	pub, pubErr := d.IP.Current()

	v := judgeDNS(findings, pub, pubErr)
	switch {
	case v.offline:
		// USER-FACING
		fmt.Fprintln(out, "Skipping the DNS pre-flight: names could not be resolved and the public-IP service did not answer (this machine looks offline).")
		return
	case len(v.warnings) > 0:
		// USER-FACING
		fmt.Fprintln(out, "DNS pre-flight (warn-only):")
		for _, w := range v.warnings {
			// USER-FACING
			fmt.Fprintf(out, "  - %s\n", w)
		}
	default:
		// USER-FACING
		fmt.Fprintf(out, "DNS pre-flight: %s already point(s) at this machine's public IP.\n", strings.Join(hosts, " and "))
	}
	for _, n := range v.notes {
		// USER-FACING
		fmt.Fprintf(out, "  (note: %s)\n", n)
	}
}

// warnDNS prints the standing DNS warning with both configured names: TLS
// certificates will not issue until their DNS points at this machine, and
// Caddy keeps retrying until then (ticket #34 owner addendum).
func warnDNS(a Answers, out io.Writer) {
	hosts := a.configuredHosts()
	// USER-FACING
	fmt.Fprintf(out, "Reminder: DNS for %s must point at this machine's public IP before TLS certificates will issue; Caddy keeps retrying until then.\n", strings.Join(hosts, " and "))
}

// defaultOSReleasePath is where Linux publishes distribution information;
// Deps.OSReleasePath overrides it (tests point it at a temporary file).
const defaultOSReleasePath = "/etc/os-release"

// osRelease is the parsed bits of /etc/os-release the stage needs.
type osRelease struct {
	id     string
	idLike string
}

// parseOSRelease reads the KEY=value lines of an os-release file.
func parseOSRelease(data string) osRelease {
	var d osRelease
	for _, line := range strings.Split(data, "\n") {
		line = strings.TrimSpace(line)
		if line == "" || strings.HasPrefix(line, "#") {
			continue
		}
		k, v, ok := strings.Cut(line, "=")
		if !ok {
			continue
		}
		v = strings.Trim(v, `"'`)
		switch k {
		case "ID":
			d.id = strings.ToLower(v)
		case "ID_LIKE":
			d.idLike = strings.ToLower(v)
		}
	}
	return d
}

// debianFamily reports whether the distro installs packages with apt.
func (d osRelease) debianFamily() bool {
	if d.id == "debian" || d.id == "ubuntu" {
		return true
	}
	return strings.Contains(d.idLike, "debian")
}

// ensureInstalled makes sure the caddy binary exists, installing the
// distro package on apt-family distros with the shown-sudo pattern, and
// pointing at the manual path everywhere else. Already-installed is a
// skip.
func ensureInstalled(r Runner, osReleasePath string, out io.Writer) error {
	if _, err := r.CombinedOutput("caddy", "version"); err == nil {
		// USER-FACING
		fmt.Fprintln(out, "Caddy is already installed — skipping installation.")
		return nil
	}
	data, err := os.ReadFile(osReleasePath)
	if err != nil {
		// USER-FACING
		fmt.Fprintf(out, "Caddy is not installed and the distribution could not be identified (%v).\n", err)
		// USER-FACING
		fmt.Fprintln(out, "Install Caddy yourself (https://caddyserver.com/docs/install or your package manager), or follow the manual reverse-proxy path in docs/proxy.md, then rerun `bitbeard setup`. Nothing was installed or written.")
		return fmt.Errorf("caddy is not installed and the distribution could not be identified")
	}
	d := parseOSRelease(string(data))
	if !d.debianFamily() {
		// USER-FACING
		fmt.Fprintf(out, "Caddy is not installed, and this distribution (%s) is not one the wizard installs packages on.\n", d.id)
		// USER-FACING
		fmt.Fprintln(out, "Install Caddy yourself (https://caddyserver.com/docs/install or your package manager), or follow the manual reverse-proxy path in docs/proxy.md, then rerun `bitbeard setup`. Nothing was installed or written.")
		return fmt.Errorf("caddy is not installed on %s; install it manually", d.id)
	}

	// USER-FACING
	fmt.Fprintln(out, "Installing Caddy (the distro package). Each step needs administrator rights; sudo may ask for your password. The exact command is shown before it runs.")
	for _, cmd := range [][]string{
		{"apt-get", "update"},
		{"apt-get", "install", "-y", "caddy"},
	} {
		args := append([]string{"sudo"}, cmd...)
		// USER-FACING
		fmt.Fprintf(out, "  $ %s\n", strings.Join(args, " "))
		if err := r.RunInteractive(args[0], args[1:]...); err != nil {
			return fmt.Errorf("installing Caddy failed (%s): %v — the wizard stopped; fix the cause and rerun `bitbeard setup`", strings.Join(args, " "), err)
		}
	}
	if _, err := r.CombinedOutput("caddy", "version"); err != nil {
		return fmt.Errorf("caddy was installed but `caddy version` still fails: %v — rerun `bitbeard setup` to continue", err)
	}
	return nil
}

// writeSnippet writes the managed snippet: the generated site blocks are
// shown, the directory is created and the file installed with shown sudo
// commands. The temp file is written unprivileged; `sudo install` does the
// privileged copy, so the Runner needs no stdin-piping ability.
func writeSnippet(a Answers, r Runner, out io.Writer, p paths) error {
	content := Snippet(a)
	// USER-FACING
	fmt.Fprintf(out, "Writing the managed snippet %s with the site blocks generated from your answers (shown below):\n", p.snippet)
	// USER-FACING
	fmt.Fprintln(out, strings.TrimRight(content, "\n"))
	dir := dirOf(p.snippet)
	// USER-FACING
	fmt.Fprintf(out, "  $ sudo mkdir -p %s\n", dir)
	if err := r.RunInteractive("sudo", "mkdir", "-p", dir); err != nil {
		return fmt.Errorf("creating %s failed: %v — fix the cause and rerun `bitbeard setup`", dir, err)
	}
	tmp, err := writeTemp("bitbeard-caddy-*.caddy", content)
	if err != nil {
		return err
	}
	defer os.Remove(tmp)
	// USER-FACING
	fmt.Fprintf(out, "  $ sudo install -m 644 %s %s\n", tmp, p.snippet)
	if err := r.RunInteractive("sudo", "install", "-m", "644", tmp, p.snippet); err != nil {
		return fmt.Errorf("installing the snippet at %s failed: %v — the content is shown above and can also be found in docs/proxy.md; fix the cause and rerun `bitbeard setup`", p.snippet, err)
	}
	return nil
}

// writeTemp writes content to a fresh temporary file and returns its path.
func writeTemp(pattern, content string) (string, error) {
	f, err := os.CreateTemp("", pattern)
	if err != nil {
		return "", fmt.Errorf("writing the temporary file for the privileged copy: %v", err)
	}
	defer f.Close()
	if _, err := f.WriteString(content); err != nil {
		os.Remove(f.Name())
		return "", fmt.Errorf("writing the temporary file for the privileged copy: %v", err)
	}
	return f.Name(), nil
}

// ensureImport makes the main Caddyfile import the snippet directory,
// with three cases: the file does not exist (create it — nothing is being
// clobbered), it exists without the import (show the diff, ask, add only
// on yes), or it already imports the directory (nothing to do). An
// operator-owned Caddyfile is never rewritten beyond the consented
// one-line append, and a refusal stops the stage before anything is
// applied.
func ensureImport(a Answers, r Runner, out io.Writer, ask func(string) (bool, error), p paths) error {
	line := importLineFor(p.snippet)
	data, err := os.ReadFile(p.main)
	switch {
	case errors.Is(err, os.ErrNotExist):
		content := MainCaddyfileContent(a.Email, line)
		// USER-FACING
		fmt.Fprintf(out, "No %s yet — creating it with the global options block (the ACME email, if given) and the import line (shown below):\n", p.main)
		// USER-FACING
		fmt.Fprintln(out, strings.TrimRight(content, "\n"))
		return installFile(r, out, p.main, content)
	case err != nil:
		// USER-FACING
		fmt.Fprintf(out, "The wizard could not read %s (%v), so it cannot check for the import line; it changes nothing.\n", p.main, err)
		return fmt.Errorf("reading %s: %v — add %q yourself and run `sudo systemctl reload caddy`, or see docs/proxy.md", p.main, err, line)
	}
	if HasSnippetImport(string(data), p.snippet) {
		// USER-FACING
		fmt.Fprintf(out, "%s already imports the snippet directory — nothing to change.\n", p.main)
		return nil
	}
	next := WithImportAdded(string(data), line)
	// USER-FACING
	fmt.Fprintf(out, "%s exists but does not import the snippet directory. The only change would be (existing content as context, added lines with +):\n", p.main)
	for _, l := range Diff(string(data), next) {
		// USER-FACING
		fmt.Fprintln(out, l)
	}
	ok, err := ask(fmt.Sprintf("Add the import line to %s? (the file is yours; this appends exactly the line shown)", p.main))
	if err != nil {
		return err
	}
	if !ok {
		// USER-FACING
		fmt.Fprintln(out, "The import line was not added, so the wizard stops here without applying anything: the managed snippet is on disk but unused until the main Caddyfile imports it.")
		// USER-FACING
		fmt.Fprintf(out, "Add %q to %s yourself and run `sudo systemctl reload caddy`, or rerun `bitbeard setup` to be asked again. See docs/proxy.md.\n", line, p.main)
		return errors.New("the import line was not added (declined)")
	}
	return installFile(r, out, p.main, next)
}

// installFile installs content at path through a temporary file and a
// shown `sudo install` (mode 0644, the mode the distro's own Caddyfile
// ships with).
func installFile(r Runner, out io.Writer, path, content string) error {
	tmp, err := writeTemp("bitbeard-caddyfile-*", content)
	if err != nil {
		return err
	}
	defer os.Remove(tmp)
	// USER-FACING
	fmt.Fprintf(out, "  $ sudo install -m 644 %s %s\n", tmp, path)
	if err := r.RunInteractive("sudo", "install", "-m", "644", tmp, path); err != nil {
		return fmt.Errorf("installing %s failed: %v — fix the cause and rerun `bitbeard setup`", path, err)
	}
	return nil
}

// validateConfig runs `caddy validate` on the resulting configuration.
// A failure changes nothing further: the service is not reloaded and the
// operator is told exactly what is on disk and how to go back.
func validateConfig(r Runner, out io.Writer, p paths) error {
	// USER-FACING
	fmt.Fprintln(out, "Validating the Caddy configuration ...")
	output, err := r.CombinedOutput("caddy", "validate", "--config", p.main, "--adapter", "caddyfile")
	if err != nil {
		// USER-FACING
		fmt.Fprintf(out, "The Caddy configuration is invalid, so nothing was applied (the running caddy service was not touched):\n%s\n", strings.TrimRight(output, "\n"))
		// USER-FACING
		fmt.Fprintf(out, "The snippet (%s) and the import line are on disk; fix the error shown above and rerun `bitbeard setup`, or delete the snippet to undo. See docs/proxy.md.\n", p.snippet)
		return fmt.Errorf("caddy validate failed: %v", err)
	}
	// USER-FACING
	fmt.Fprintln(out, "The Caddy configuration is valid.")
	return nil
}

// applyService starts or reloads caddy with shown systemctl commands:
// reload when it is already running, enable --now when it is not.
func applyService(r Runner, out io.Writer) error {
	active, err := ServiceActive(r)
	if err == nil && active {
		// USER-FACING
		fmt.Fprintln(out, "Caddy is running — reloading it:")
		// USER-FACING
		fmt.Fprintln(out, "  $ sudo systemctl reload caddy")
		if err := r.RunInteractive("sudo", "systemctl", "reload", "caddy"); err != nil {
			return fmt.Errorf("reloading caddy failed: %v — the configuration is valid and on disk; run `sudo systemctl reload caddy` yourself, then rerun `bitbeard doctor`", err)
		}
		return nil
	}
	// USER-FACING
	fmt.Fprintln(out, "Starting and enabling the caddy service:")
	// USER-FACING
	fmt.Fprintln(out, "  $ sudo systemctl enable --now caddy")
	if err := r.RunInteractive("sudo", "systemctl", "enable", "--now", "caddy"); err != nil {
		return fmt.Errorf("starting caddy failed: %v — `journalctl -u caddy` shows why; fix the cause and rerun `bitbeard setup`", err)
	}
	return nil
}

// ServiceActive reports whether the host's caddy service is running,
// via a read-only `systemctl is-active caddy` (works unprivileged). The
// doctor's only Caddy probe — check-only, like everything there.
//
// systemctl answers "inactive" (or "failed") with a non-zero exit when
// the service is not running — that is a systemd answer, not a failure
// to ask, and it reports as not-running. Only an unanswerable probe
// (systemd missing, a dbus hiccup) comes back as an error.
func ServiceActive(r Runner) (bool, error) {
	out, err := r.CombinedOutput("systemctl", "is-active", "caddy")
	state := strings.TrimSpace(out)
	if err != nil {
		switch state {
		case "inactive", "failed":
			return false, nil
		}
		return false, err
	}
	return state == "active", nil
}

// The .env keys of the integration's state. CADDY_ENABLED switches the
// doctor's check and the boot summary on; CADDY_DOMAIN carries the base
// domain for the summary (display only — the serving configuration lives
// in the managed snippet, which the operator owns).
const (
	EnvEnabledKey = "CADDY_ENABLED"
	EnvDomainKey  = "CADDY_DOMAIN"
)

// Enabled reports whether the wizard's reverse-proxy integration is
// switched on in env (CADDY_ENABLED=1).
func Enabled(env map[string]string) bool {
	return strings.TrimSpace(env[EnvEnabledKey]) == "1"
}

// DomainFromEnv returns the base domain recorded in env, or "".
func DomainFromEnv(env map[string]string) string {
	return strings.TrimSpace(env[EnvDomainKey])
}

// WarnPublicHTTPS verifies the PUBLIC_HTTPS side of the deal without
// forcing anything: the public HTTPS deployment implies PUBLIC_HTTPS=1
// (already the default), so an explicitly disabled value gets a plain
// warning about the Secure-cookie plain-HTTP footgun of docs/proxy.md —
// the operator's explicit setting is never overwritten.
func WarnPublicHTTPS(env map[string]string, out io.Writer) {
	if !PublicHTTPSDisabled(env["PUBLIC_HTTPS"]) {
		return
	}
	// USER-FACING
	fmt.Fprintln(out, "Note: PUBLIC_HTTPS is explicitly off in .env while the public reverse proxy is being set up. Public visitors will hit the Secure-cookie plain-HTTP footgun (docs/proxy.md): set PUBLIC_HTTPS=1 in .env unless you really serve plain HTTP only, then rerun `bitbeard setup`.")
}

// Compile-time check: the production runner satisfies the boundary.
var _ Runner = prereqs.ExecRunner{}
