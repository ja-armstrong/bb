package wizard

// vpnfile.go handles openvpn/custom.ovpn: the bundled NordVPN default is
// kept as-is; a user's own .ovpn file is copied over it — visibly, and only
// after an explicit confirmation when something is already there.

import (
	"fmt"
	"io"
	"os"
	"path/filepath"
)

// ovpnRelPath is the OpenVPN config the gluetun Subsystem mounts, relative
// to the compose directory.
var ovpnRelPath = filepath.Join("openvpn", "custom.ovpn")

// applyVPNConfig keeps or replaces <composeDir>/openvpn/custom.ovpn.
//
// src is the user's .ovpn path; empty means "keep what is there". provider
// is the chosen VPN_PROVIDER value. confirm is asked (with the visible
// note) whenever an existing file would be replaced; a refusal aborts the
// whole wizard with an error rather than overwriting silently.
func applyVPNConfig(composeDir, src, provider string, out io.Writer, confirm func(string) bool) error {
	dst := filepath.Join(composeDir, ovpnRelPath)

	if src == "" {
		switch provider {
		case nordvpnDefault:
			// USER-FACING
			fmt.Fprintf(out, "Keeping the bundled NordVPN configuration at %s.\n", ovpnRelPath)
		case customProvider:
			// Not reachable through the normal flow (the custom path always
			// asks for a file), but honest if it ever is.
			// USER-FACING
			return fmt.Errorf("VPN_PROVIDER=%s needs an .ovpn file, but none was given", customProvider)
		default:
			// USER-FACING
			fmt.Fprintf(out, "Note: %s still holds the bundled NordVPN configuration. gluetun connects through that file, so the tunnel will use NordVPN servers, not %s servers. Replace the file later (any .ovpn from your provider) and run `docker compose up -d` again to apply it.\n", ovpnRelPath, provider)
		}
		return nil
	}

	data, err := os.ReadFile(src)
	if err != nil {
		// USER-FACING
		return fmt.Errorf("reading your .ovpn file: %w", err)
	}
	if _, err := os.Stat(dst); err == nil {
		// USER-FACING
		note := fmt.Sprintf("Copying %s over the existing %s — the old file will be replaced.", src, ovpnRelPath)
		if !confirm(note) {
			// USER-FACING
			return fmt.Errorf("refused to replace the existing %s; nothing was changed", ovpnRelPath)
		}
	}
	if err := os.MkdirAll(filepath.Dir(dst), 0o755); err != nil {
		// USER-FACING
		return fmt.Errorf("creating %s: %w", filepath.Dir(dst), err)
	}
	if err := os.WriteFile(dst, data, 0o644); err != nil {
		// USER-FACING
		return fmt.Errorf("writing %s: %w", dst, err)
	}
	// USER-FACING
	fmt.Fprintf(out, "Copied your OpenVPN configuration from %s to %s.\n", src, ovpnRelPath)
	return nil
}
