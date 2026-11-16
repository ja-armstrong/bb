package wizard

// validate.go holds the live checks the wizard applies while prompting:
// every answer is validated at prompt time, so bad input is rejected before
// anything is written or booted.

import (
	"fmt"
	"net/url"
	"os"
	"path/filepath"
	"strconv"
	"strings"
)

// trimSpace is strings.TrimSpace, kept here so providers.go reads cleanly.
func trimSpace(s string) string { return strings.TrimSpace(s) }

// lower is strings.ToLower.
func lower(s string) string { return strings.ToLower(s) }

// Validator turns a raw answer into its canonical value, or explains what
// is wrong with it. Returning the empty string with a nil error is allowed
// only for validators that accept empty input (documented per key).
type Validator func(string) (string, error)

// rejectChangeMe is the shared refusal behind "never accept a change-me
// value".
func rejectChangeMe(k, v string) error {
	if IsChangeMe(v) {
		// USER-FACING
		return fmt.Errorf("%s cannot contain a change-me placeholder; enter a real value", k)
	}
	return nil
}

// ValidateNonEmpty accepts any non-empty answer without a change-me
// placeholder.
func ValidateNonEmpty(k string) Validator {
	return func(v string) (string, error) {
		v = trimSpace(v)
		if v == "" {
			// USER-FACING
			return "", fmt.Errorf("%s cannot be empty", k)
		}
		if err := rejectChangeMe(k, v); err != nil {
			return "", err
		}
		return v, nil
	}
}

// ValidateProvider canonicalizes and validates a VPN provider name against
// gluetun's supported list.
func ValidateProvider(v string) (string, error) {
	v = normalizeProvider(v)
	if v == "" {
		// USER-FACING
		return "", fmt.Errorf("VPN provider cannot be empty; choose one from the list")
	}
	if !KnownVPNProvider(v) {
		// USER-FACING
		return "", fmt.Errorf("%q is not a provider gluetun supports; choose one from the list", v)
	}
	return v, nil
}

// ValidatePositiveInt accepts a whole number greater than zero.
func ValidatePositiveInt(k string) Validator {
	return func(v string) (string, error) {
		v = trimSpace(v)
		if !IsPositiveInt(v) {
			// USER-FACING
			return "", fmt.Errorf("%s must be a whole number greater than zero", k)
		}
		return v, nil
	}
}

// IsPositiveInt reports whether v parses as an integer greater than zero.
func IsPositiveInt(v string) bool {
	n, err := strconv.Atoi(trimSpace(v))
	return err == nil && n > 0
}

// ValidateTZ accepts an IANA timezone name ("Europe/Berlin") or "UTC".
func ValidateTZ(v string) (string, error) {
	v = trimSpace(v)
	if !ValidTZ(v) {
		// USER-FACING
		return "", fmt.Errorf("TZ must be an IANA timezone like Europe/Berlin, or UTC")
	}
	return v, nil
}

// ValidTZ reports whether v looks like a timezone docker and the stack
// accept. Full tzdata lookup is deliberately not attempted; the shape check
// catches typos without pretending to be tzdata.
func ValidTZ(v string) bool {
	if v == "UTC" {
		return true
	}
	parts := strings.Split(v, "/")
	if len(parts) < 2 || len(parts) > 3 {
		return false
	}
	for _, p := range parts {
		if p == "" || strings.ContainsAny(p, " \t\\") {
			return false
		}
	}
	return true
}

// ValidateBaseDir accepts the Media Root as an absolute path; an empty
// answer takes the stack default /media.
func ValidateBaseDir(v string) (string, error) {
	v = trimSpace(v)
	if v == "" {
		return "/media", nil // the stack default; documented in the prompt
	}
	if !ValidAbsPath(v) {
		// USER-FACING
		return "", fmt.Errorf("BASE_DOWNLOAD_DIR must be an absolute path starting with / (as the containers see it)")
	}
	return v, nil
}

// ValidAbsPath reports whether v is an absolute POSIX path without ".."
// segments. It is the Media Root, the file manager's hard sandbox boundary.
func ValidAbsPath(v string) bool {
	if !strings.HasPrefix(v, "/") || strings.ContainsAny(v, "\r\n") {
		return false
	}
	for _, seg := range strings.Split(v, "/") {
		if seg == ".." {
			return false
		}
	}
	return true
}

// ValidateServerCountries accepts an empty answer (gluetun picks a server
// itself) or a comma-separated list of country names for gluetun's server
// filter, without empty entries.
func ValidateServerCountries(v string) (string, error) {
	v = trimSpace(v)
	if v == "" {
		return "", nil
	}
	if err := rejectChangeMe("SERVER_COUNTRIES", v); err != nil {
		return "", err
	}
	for _, part := range strings.Split(v, ",") {
		if trimSpace(part) == "" {
			// USER-FACING
			return "", fmt.Errorf("SERVER_COUNTRIES is %q; use comma-separated country names without empty entries", v)
		}
	}
	return v, nil
}

// ValidateJellyfinURLOptional accepts an empty answer (the documented
// default applies) or a Jellyfin server's base URL: http(s) with a host,
// no path rewriting — the client appends /System/Info and the user
// endpoints itself.
func ValidateJellyfinURLOptional(v string) (string, error) {
	v = trimSpace(v)
	if v == "" {
		return "", nil
	}
	u, err := url.Parse(v)
	if err != nil || u.Host == "" || (u.Scheme != "http" && u.Scheme != "https") {
		// USER-FACING
		return "", fmt.Errorf("JELLYFIN_URL is %q; expected the Jellyfin server's base URL like http://host.docker.internal:8096", v)
	}
	return v, nil
}

// ValidateOVPNPath accepts a path to an existing .ovpn (or .conf) file.
func ValidateOVPNPath(v string) (string, error) {
	v = trimSpace(v)
	if v == "" {
		// USER-FACING
		return "", fmt.Errorf("a path to your .ovpn file is required")
	}
	return checkOVPNPath(v)
}

// ValidateOptionalOVPNPath accepts an empty answer (keep what is there) or
// a path to an existing .ovpn file.
func ValidateOptionalOVPNPath(v string) (string, error) {
	v = trimSpace(v)
	if v == "" {
		return "", nil
	}
	return checkOVPNPath(v)
}

// checkOVPNPath verifies the file exists and has an OpenVPN config
// extension, returning its cleaned path.
func checkOVPNPath(v string) (string, error) {
	v = filepath.Clean(v)
	fi, err := os.Stat(v)
	if err != nil {
		// USER-FACING
		return "", fmt.Errorf("cannot read %s: %v", v, err)
	}
	if fi.IsDir() {
		// USER-FACING
		return "", fmt.Errorf("%s is a directory; expected an .ovpn file", v)
	}
	ext := strings.ToLower(filepath.Ext(v))
	if ext != ".ovpn" && ext != ".conf" {
		// USER-FACING
		return "", fmt.Errorf("%s is not an .ovpn file", v)
	}
	return v, nil
}
