// vpnfile.go handles openvpn/custom.ovpn in the custom provider's file
// flow: a user's .ovpn file is copied over what is there — visibly, and
// only after an explicit confirmation. Known providers never touch this
// file: gluetun runs natively from VPN_SERVICE_PROVIDER plus
// OPENVPN_USER/OPENVPN_PASSWORD with no .ovpn file at all, and switching
// back to a known provider removes the copy again so no unused config is
// left mounted.

package wizard

import (
	"errors"
	"fmt"
	"io"
	"io/fs"
	"os"
	"path/filepath"
)

// ovpnRelPath is the OpenVPN config the gluetun Subsystem mounts, relative
// to the compose directory.
var ovpnRelPath = filepath.Join("openvpn", "custom.ovpn")

// applyVPNConfig replaces <composeDir>/openvpn/custom.ovpn with the user's
// .ovpn file (the custom provider's whole connection, credentials
// included). confirm is asked (with the visible note) whenever an existing
// file would be replaced; a refusal aborts the whole wizard with an error
// rather than overwriting silently.
func applyVPNConfig(composeDir, src string, out io.Writer, confirm func(string) bool) error {
	dst := filepath.Join(composeDir, ovpnRelPath)

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

// removeVPNConfig deletes the copied openvpn/custom.ovpn: the cleanup of
// the switch back to a known provider, which leaves gluetun native with no
// .ovpn file in play. A missing file is not an error. The original .ovpn
// the operator supplied is never touched — only the stack's copy is.
func removeVPNConfig(composeDir string, out io.Writer) error {
	dst := filepath.Join(composeDir, ovpnRelPath)
	err := os.Remove(dst)
	switch {
	case err == nil:
		// USER-FACING
		fmt.Fprintf(out, "Removed the unused %s copy — your original .ovpn file is untouched.\n", ovpnRelPath)
		return nil
	case errors.Is(err, fs.ErrNotExist):
		return nil
	default:
		// USER-FACING
		return fmt.Errorf("removing %s: %w", ovpnRelPath, err)
	}
}
