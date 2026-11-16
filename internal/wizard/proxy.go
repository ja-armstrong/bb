package wizard

// proxy.go wires the Caddy reverse-proxy stage into the wizard. The
// prompts are asked here (the wizard owns the prompt engine and the flow
// order); the decisions and privileged actions live in internal/proxy
// behind its Deps boundary, so every system step is injectable and the
// tests never touch the network or the host.

import (
	"fmt"
	"io"

	"github.com/ja-armstrong/bb/internal/proxy"
)

// Explanations shown before the reverse-proxy questions.

// USER-FACING
const proxyExplanation = `The stack always answers on the machine's own ports (the Webui on 8081, the
Media Server on 8096), reachable from the LAN. If this machine will also be
reachable from the public internet, the wizard can set up Caddy — a host
reverse proxy — in front of the stack: automatic HTTPS with certificates it
issues and renews itself, with the Webui and the Media Server each on a
subdomain of one base domain. Caddy is host infrastructure: it keeps
serving even when the containers are down. Answering no keeps the stack
LAN-only; nothing is installed or written. The manual reverse-proxy path
(docs/proxy.md) can be done at any time instead.
`

// USER-FACING
const proxyDomainExplanation = `The base domain whose DNS points at this machine, e.g. example.com. Two
public names derive from it: bitbeard.<domain> for the Webui and
jellyfin.<domain> for the Media Server — create both in your DNS and point
them here. TLS certificates for both are issued automatically once the
names resolve.
`

// USER-FACING
const proxyEmailExplanation = `An email address for the TLS certificate issuer (Let's Encrypt): expiry
notices go there. Leave empty to omit it; Caddy still issues certificates.
`

// collectProxy asks whether the stack will be publicly exposed behind a
// reverse proxy and, on yes, collects the base domain and the optional
// ACME email, showing the derived public names back. jellyfin decides
// whether the derived jellyfin.<domain> site block is written — a Media
// Server other than Jellyfin would leave that block proxying nothing.
// A no (the default) keeps the stack LAN-only and returns nil.
func collectProxy(pr *prompter, out io.Writer, jellyfin bool) (*proxy.Answers, error) {
	ok, err := pr.askYesNo("Will the stack be publicly exposed behind a reverse proxy (Caddy)?")
	if err != nil {
		return nil, err
	}
	if !ok {
		// USER-FACING
		fmt.Fprintln(out, "Staying LAN-only: no reverse proxy is set up and nothing was installed or written.")
		return nil, nil
	}
	return collectProxyDetails(pr, out, jellyfin)
}

// collectProxyDetails asks the base domain and the optional ACME email —
// the part of the stage both the fresh yes and the rerun reconfigure
// answer share.
func collectProxyDetails(pr *prompter, out io.Writer, jellyfin bool) (*proxy.Answers, error) {
	domain, err := pr.askText("Base domain (e.g. example.com)", proxyDomainExplanation, proxy.ValidateDomain)
	if err != nil {
		return nil, err
	}
	webui, jelly := proxy.DeriveHosts(domain)
	// USER-FACING
	fmt.Fprintf(out, "The public names will be:\n  https://%s (the Webui)\n", webui)
	if jellyfin {
		// USER-FACING
		fmt.Fprintf(out, "  https://%s (the Media Server)\n", jelly)
	} else {
		// USER-FACING
		fmt.Fprintln(out, "  (no Media Server name: the Media Server choice above was not Jellyfin, so no jellyfin site block is written — rerun `bitbeard setup` after enabling Jellyfin to add it)")
	}

	email, err := pr.askText("ACME email (optional; empty = none)", proxyEmailExplanation, proxy.ValidateEmail)
	if err != nil {
		return nil, err
	}
	return &proxy.Answers{Domain: domain, Email: email, Jellyfin: jellyfin}, nil
}

// collectProxyRerun offers the reverse-proxy stage on a rerun: the current
// state is shown first, declining changes nothing, and accepting re-runs
// the stage — which regenerates the managed snippet from the new answers,
// replacing any manual edits to it (said plainly in the question).
func collectProxyRerun(pr *prompter, out io.Writer, e Env, jellyfin bool) (*proxy.Answers, error) {
	if proxy.Enabled(e) {
		if domain := proxy.DomainFromEnv(e); domain != "" {
			webui, jelly := proxy.DeriveHosts(domain)
			// USER-FACING
			fmt.Fprintf(out, "Public access: behind Caddy (https://%s, https://%s).\n", webui, jelly)
		} else {
			// USER-FACING
			fmt.Fprintln(out, "Public access: behind Caddy (the base domain is not recorded in .env; the serving configuration lives in /etc/caddy/caddy.d/bitbeard.caddy).")
		}
	} else {
		// USER-FACING
		fmt.Fprintln(out, "Public access: LAN-only (no reverse proxy).")
	}
	ok, err := pr.askYesNo("Set up or reconfigure the public reverse proxy (Caddy)? (reconfiguring regenerates the managed snippet, replacing manual edits to it)")
	if err != nil {
		return nil, err
	}
	if !ok {
		return nil, nil
	}
	return collectProxyDetails(pr, out, jellyfin)
}

// applyProxy runs the reverse-proxy stage and marks its success in the
// .env map: CADDY_ENABLED=1 plus the base domain, so the boot summary, the
// doctor and later reruns all see the integration. The caller rewrites
// .env from the map afterwards; the markers are only ever written after
// the stage actually applied. PUBLIC_HTTPS is verified, never forced.
func applyProxy(a *proxy.Answers, o Options, out io.Writer, pr *prompter, e Env) error {
	deps := proxy.Deps{}
	if o.Proxy != nil {
		deps = *o.Proxy
	}
	ask := func(question string) (bool, error) { return pr.askYesNo(question) }
	if err := proxy.Apply(*a, deps, out, ask); err != nil {
		return err
	}
	e[proxy.EnvEnabledKey] = "1"
	e[proxy.EnvDomainKey] = a.Domain
	// USER-FACING
	fmt.Fprintf(out, "  %s=1\n  %s=%s\n", proxy.EnvEnabledKey, proxy.EnvDomainKey, a.Domain)
	proxy.WarnPublicHTTPS(e, out)
	return nil
}
