package proxy

// net.go is the network half of the reverse-proxy stage: the boundaries
// the stage talks to (DNS, the public-IP echo service, host ports) and
// the pure verdicts drawn from what they report. Tests replace every
// boundary with a fake and pin the verdicts; no test touches the network.

import (
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"strconv"
	"strings"
	"syscall"
	"time"
)

// DNSChecker resolves hostnames.
type DNSChecker interface {
	LookupHost(host string) ([]string, error)
}

// netDNS is the production DNSChecker: the machine's resolver.
type netDNS struct{}

// LookupHost resolves host.
func (netDNS) LookupHost(host string) ([]string, error) { return net.LookupHost(host) }

// PublicIPSource reports the machine's current public IP, via a public
// echo service.
type PublicIPSource interface {
	Current() (string, error)
}

// ipify is the production PublicIPSource: a GET of https://api.ipify.org,
// which answers with the caller's public IP as plain text.
type ipify struct {
	// Client is the HTTP client; nil means a 5-second-timeout client.
	Client *http.Client
}

// Current fetches the public IP.
func (s ipify) Current() (string, error) {
	c := s.Client
	if c == nil {
		c = &http.Client{Timeout: 5 * time.Second}
	}
	resp, err := c.Get("https://api.ipify.org")
	if err != nil {
		return "", err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return "", fmt.Errorf("the public-IP service answered %d", resp.StatusCode)
	}
	b, err := io.ReadAll(io.LimitReader(resp.Body, 64))
	if err != nil {
		return "", err
	}
	return strings.TrimSpace(string(b)), nil
}

// PortProber reports whether a host port is in use.
type PortProber interface {
	// Busy reports whether something is listening on the port.
	Busy(port int) (bool, error)
}

// dialProber is the production PortProber: a TCP dial to 127.0.0.1 on the
// port. A connection means something is listening (busy); a connection
// refusal means nothing is (free); any other error is reported as unknown
// and treated conservatively by the stage.
type dialProber struct {
	// Timeout caps one dial; 0 means 2 seconds.
	Timeout time.Duration
}

// Busy probes the port.
func (p dialProber) Busy(port int) (bool, error) {
	t := p.Timeout
	if t == 0 {
		t = 2 * time.Second
	}
	c, err := net.DialTimeout("tcp", net.JoinHostPort("127.0.0.1", strconv.Itoa(port)), t)
	if err == nil {
		c.Close()
		return true, nil
	}
	if errors.Is(err, syscall.ECONNREFUSED) {
		return false, nil
	}
	return false, err
}

// hostFinding pairs a host name with what resolving it produced.
type hostFinding struct {
	host string
	ips  []string
	err  error
}

// dnsVerdict is what the DNS pre-flight concludes. Warn-only by design
// (ticket #34 owner addendum): no verdict blocks setup.
type dnsVerdict struct {
	// offline is true when nothing could be resolved and the public IP
	// could not be fetched either: the machine looks offline, so the
	// pre-flight is skipped with a note instead of alarming warnings.
	offline bool
	// warnings are plain mismatches: a name that does not resolve, or
	// resolves somewhere else.
	warnings []string
	// notes are non-alarming observations, e.g. the public IP being
	// unknown so the comparison could not happen.
	notes []string
	// matched is true when every configured host resolved and points at
	// the machine's public IP.
	matched bool
}

// judgeDNS compares the lookups of the configured hosts against the
// machine's public IP. pubErr is the public-IP fetch's error (nil when it
// succeeded).
func judgeDNS(findings []hostFinding, pub string, pubErr error) dnsVerdict {
	var v dnsVerdict

	// Offline: every lookup failed AND the public IP could not be
	// fetched — there is no network to pre-flight against.
	allLookupFailed := true
	for _, f := range findings {
		if f.err == nil {
			allLookupFailed = false
			break
		}
	}
	if allLookupFailed && pubErr != nil {
		v.offline = true
		return v
	}

	resolved := 0
	contained := 0
	for _, f := range findings {
		if f.err != nil {
			// USER-FACING
			v.warnings = append(v.warnings, fmt.Sprintf("%s does not resolve in DNS yet (%v)", f.host, f.err))
			continue
		}
		resolved++
		if pubErr != nil {
			continue
		}
		if !containsIP(f.ips, pub) {
			// USER-FACING
			v.warnings = append(v.warnings, fmt.Sprintf("%s resolves to %s, not this machine's public IP (%s)", f.host, strings.Join(f.ips, ", "), pub))
			continue
		}
		contained++
	}
	if pubErr != nil {
		// USER-FACING
		v.notes = append(v.notes, fmt.Sprintf("this machine's public IP could not be fetched (%v), so the resolved addresses could not be compared", pubErr))
	}
	v.matched = resolved == len(findings) && contained == len(findings) && pubErr == nil
	return v
}

// containsIP reports whether the exact public-IP string is among the
// resolved addresses. String equality, not net.ParseIP semantics: the
// echo service and the resolver may spell families differently, and a
// false mismatch only produces a warning — the pre-flight never blocks.
func containsIP(ips []string, pub string) bool {
	for _, ip := range ips {
		if ip == pub {
			return true
		}
	}
	return false
}

// PublicHTTPSDisabled reports whether a PUBLIC_HTTPS value explicitly
// turns the public-HTTPS assumption off (the plain-HTTP escape hatch of
// docs/proxy.md). Unset or anything unrecognised counts as the default on.
func PublicHTTPSDisabled(v string) bool {
	switch strings.ToLower(strings.TrimSpace(v)) {
	case "0", "false", "no", "off":
		return true
	}
	return false
}
