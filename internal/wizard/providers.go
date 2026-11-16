package wizard

// VPNProvider is one VPN service provider gluetun supports, as offered by
// the wizard's provider prompt. The list is hardcoded from gluetun's
// internal/constants/providers (qdm12/gluetun) so the prompt can validate
// against what the VPN Subsystem actually accepts.
type VPNProvider struct {
	// ID is the value gluetun expects in VPN_SERVICE_PROVIDER.
	ID string
	// Name is how the provider is shown in the prompt.
	Name string
}

// VPNProviders is gluetun's supported VPN service provider list, minus
// "custom" — which the wizard offers as the "bring your own .ovpn file"
// option. Order matches gluetun's All() (alphabetical).
var VPNProviders = []VPNProvider{
	{"airvpn", "AirVPN"},
	{"cyberghost", "Cyberghost"},
	{"expressvpn", "ExpressVPN"},
	{"fastestvpn", "FastestVPN"},
	{"giganews", "Giganews"},
	{"hidemyass", "HideMyAss"},
	{"ipvanish", "IPVanish"},
	{"ivpn", "IVPN"},
	{"mullvad", "Mullvad"},
	{"nordvpn", "NordVPN"},
	{"privado", "Privado"},
	{"private internet access", "Private Internet Access"},
	{"privatevpn", "PrivateVPN"},
	{"protonvpn", "ProtonVPN"},
	{"purevpn", "PureVPN"},
	{"slickvpn", "SlickVPN"},
	{"surfshark", "Surfshark"},
	{"torguard", "TorGuard"},
	{"vpnsecure", "VPNSecure.me"},
	{"vpn unlimited", "VPN Unlimited"},
	{"vyprvpn", "VyprVPN"},
	{"windscribe", "Windscribe"},
}

// customProvider is the VPN_PROVIDER value for a user-supplied .ovpn file:
// gluetun's "custom" provider.
const customProvider = "custom"

// customConfigEnvKey is the .env key carrying the OpenVPN config file
// gluetun reads in custom mode (gluetun's OPENVPN_CUSTOM_CONFIG, a file
// path as the gluetun container sees it). It is only meaningful when
// VPN_PROVIDER=custom; gluetun ignores it otherwise.
const customConfigEnvKey = "OPENVPN_CUSTOM_CONFIG"

// customConfigMountPath is customConfigEnvKey's value in custom mode: the
// copied .ovpn file inside the container, through the stack's
// ./openvpn:/gluetun/openvpn mount.
const customConfigMountPath = "/gluetun/openvpn/custom.ovpn"

// serverCountriesEnvKey is gluetun's optional server-selection key for
// known providers (comma-separated country names; empty = gluetun picks a
// server itself). Verified against gluetun's configuration settings: an
// empty-but-set value is treated like an unset one, so leaving it empty in
// .env never constrains the server pick.
const serverCountriesEnvKey = "SERVER_COUNTRIES"

// isCustomProvider reports whether a VPN_PROVIDER value selects the
// bring-your-own-.ovpn custom provider.
func isCustomProvider(id string) bool {
	return normalizeProvider(id) == customProvider
}

// KnownVPNProvider reports whether id names a provider gluetun supports,
// case-insensitive, including the "custom" pseudo-provider.
func KnownVPNProvider(id string) bool {
	id = normalizeProvider(id)
	if id == customProvider {
		return true
	}
	for _, p := range VPNProviders {
		if p.ID == id {
			return true
		}
	}
	return false
}

// normalizeProvider trims and lowercases a provider id for comparison.
func normalizeProvider(id string) string {
	return lower(trimSpace(id))
}
