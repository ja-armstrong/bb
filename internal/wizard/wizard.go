// Package wizard implements the configuration and boot stage of Bitbeard
// Bootstrap. It prompts for every required key one at a time (with an
// explanation and live validation per key), asks for the Optional
// Subsystems — the Media Server choice and the Bot's Discord keys, which
// land in COMPOSE_PROFILES — writes the stack's .env, keeps or replaces
// the OpenVPN configuration file, boots the stack, and verifies the
// mandatory Subsystems plus any enabled Optional Subsystems are running.
// On rerun, a valid .env is kept wholesale and only the missing optional
// choices and keys are asked; the Media Server can also be switched.
package wizard

import (
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strings"
)

// Options parameterises Run.
type Options struct {
	// StackDir is where the stack repository was cloned by the fetch stage.
	StackDir string
	// In is where answers are read from (os.Stdin outside tests).
	In io.Reader
	// Out is where prompts and progress are written (os.Stdout outside
	// tests).
	Out io.Writer
	// Compose is the docker-compose boundary; nil means the real CLI.
	Compose Compose
	// Rand is the randomness for generated secrets; nil means crypto/rand.
	Rand io.Reader
	// Discord live-checks bot tokens; nil means the real Discord API.
	Discord DiscordChecker
}

// Result reports what the configuration and boot stage did.
type Result struct {
	// ComposeDir is the stack directory the .env and docker-compose.yml
	// live in (the bitbeard/ directory of the cloned repository).
	ComposeDir string
	// EnvPath is the .env the stage uses or wrote.
	EnvPath string
	// WroteEnv is true when this run wrote .env itself.
	WroteEnv bool
	// Booted is true when `docker compose up -d` ran.
	Booted bool
}

// collected is what the prompts produced.
type collected struct {
	env     Env
	ovpnSrc string // path to a user .ovpn file, "" when keeping the bundled one
	// media is the chosen Media Server profile: profileJellyfin,
	// profilePlex, or "" (none). It lands in COMPOSE_PROFILES, never in a
	// per-server key.
	media string
	// bot is true when the Bot keys were provided.
	bot bool
}

// discord returns the configured DiscordChecker, defaulting to the real
// Discord API.
func (o Options) discord() DiscordChecker {
	if o.Discord != nil {
		return o.Discord
	}
	return DiscordAPI{}
}

// Run executes the configuration and boot stage against the cloned stack at
// o.StackDir. An existing valid .env is kept and only the Optional
// Subsystems stage runs (missing choices and keys, plus the offer to switch
// the Media Server) before boot; a broken one is diagnosed and the operator
// offered a full rerun of the wizard.
func Run(o Options) (Result, error) {
	if o.Out == nil {
		o.Out = os.Stdout
	}
	if o.In == nil {
		o.In = os.Stdin
	}
	out := o.Out
	var res Result

	composeDir, err := FindComposeDir(o.StackDir)
	if err != nil {
		return res, err
	}
	res.ComposeDir = composeDir
	res.EnvPath = filepath.Join(composeDir, ".env")

	pr := newPrompter(o.In, out)

	// Rerun behaviour: a valid .env is kept wholesale; only the Optional
	// Subsystems stage runs (missing choices and keys, plus the offer to
	// switch the Media Server), then boot re-applies the profiles.
	if data, rerr := os.ReadFile(res.EnvPath); rerr == nil {
		e, perr := ParseEnv(string(data))
		switch {
		case perr != nil:
			// USER-FACING
			fmt.Fprintf(out, "The existing .env at %s could not be parsed: %v\n", res.EnvPath, perr)
		case len(e.Problems()) == 0:
			// USER-FACING
			fmt.Fprintf(out, "Found a valid .env at %s — skipping the mandatory prompts.\n", res.EnvPath)

			stage, err := collectOptionalRerun(pr, out, e, o.discord())
			if err != nil {
				return res, err
			}
			if stage.changed {
				// Overlay the newly collected keys on the kept values;
				// the prompted stage always wins, the rest is preserved.
				ne := mergeEnv(e, stage.env)
				ne["COMPOSE_PROFILES"] = stage.profiles
				// USER-FACING
				fmt.Fprintf(out, "Updating %s:\n", res.EnvPath)
				for _, k := range []string{"DISCORD_BOT_TOKEN", "DISCORD_ANNOUNCE_CHANNEL_ID"} {
					if v, ok := stage.env[k]; ok {
						// USER-FACING
						fmt.Fprintf(out, "  %s=%s\n", k, v)
					}
				}
				// USER-FACING
				fmt.Fprintf(out, "  COMPOSE_PROFILES=%s\n", stage.profiles)
				if err := writeEnvFile(res.EnvPath, ne); err != nil {
					// USER-FACING
					return res, fmt.Errorf("writing %s: %w", res.EnvPath, err)
				}
				res.WroteEnv = true

				// Defend against a bug in our own generation: what we
				// just wrote must parse and validate.
				if data, err := os.ReadFile(res.EnvPath); err != nil {
					// USER-FACING
					return res, fmt.Errorf("re-reading %s: %w", res.EnvPath, err)
				} else if w, perr := ParseEnv(string(data)); perr != nil {
					// USER-FACING
					return res, fmt.Errorf("the freshly written .env does not parse: %v", perr)
				} else if probs := w.Problems(); len(probs) > 0 {
					// USER-FACING
					return res, fmt.Errorf("the freshly written .env failed validation: %s", strings.Join(probs, "; "))
				}
			} else {
				// USER-FACING
				fmt.Fprintln(out, "The Optional Subsystems are already as requested — nothing was changed.")
			}

			if err := boot(o, composeDir, out, &res, stage.profiles); err != nil {
				return res, err
			}
			return res, nil
		default:
			// USER-FACING
			fmt.Fprintf(out, "The existing .env at %s has problems:\n", res.EnvPath)
			for _, p := range e.Problems() {
				// USER-FACING
				fmt.Fprintf(out, "  - %s\n", p)
			}
		}
		// USER-FACING
		fmt.Fprintln(out, "The configuration wizard can rewrite .env from fresh answers (the existing file is replaced).")
		redo, err := pr.askYesNo("Run the configuration wizard?")
		if err != nil {
			return res, err
		}
		if !redo {
			// USER-FACING
			fmt.Fprintln(out, "Nothing was changed and the stack was not started.")
			return res, nil
		}
	}

	c, err := collect(o, out, pr)
	if err != nil {
		return res, err
	}

	// Build the full .env: seeded defaults for unprompted keys and the
	// prompted values on top, with COMPOSE_PROFILES from the Optional
	// Subsystems stage.
	profiles := deriveProfiles("", c.media, c.bot)
	e := mergeEnv(exampleSeed(composeDir), c.env)
	e["COMPOSE_PROFILES"] = profiles

	// USER-FACING
	fmt.Fprintf(out, "Writing %s with:\n", res.EnvPath)
	for _, k := range envKeys {
		if v, prompted := c.env[k]; prompted {
			// USER-FACING
			fmt.Fprintf(out, "  %s=%s\n", k, v)
		}
	}
	// USER-FACING
	fmt.Fprintf(out, "  COMPOSE_PROFILES=%s\n", profiles)

	if err := writeEnvFile(res.EnvPath, e); err != nil {
		// USER-FACING
		return res, fmt.Errorf("writing %s: %w", res.EnvPath, err)
	}
	res.WroteEnv = true

	// Defend against a bug in our own generation: what we just wrote must
	// parse and validate.
	if data, err := os.ReadFile(res.EnvPath); err != nil {
		// USER-FACING
		return res, fmt.Errorf("re-reading %s: %w", res.EnvPath, err)
	} else if w, perr := ParseEnv(string(data)); perr != nil {
		// USER-FACING
		return res, fmt.Errorf("the freshly written .env does not parse: %v", perr)
	} else if probs := w.Problems(); len(probs) > 0 {
		// USER-FACING
		return res, fmt.Errorf("the freshly written .env failed validation: %s", strings.Join(probs, "; "))
	}

	if err := EnsureEnvIgnored(composeDir, out); err != nil {
		// USER-FACING
		return res, fmt.Errorf("checking .gitignore: %w", err)
	}

	if err := applyVPNConfig(composeDir, c.ovpnSrc, e["VPN_PROVIDER"], out, func(question string) bool {
		ok, err := pr.askYesNo(question)
		return err == nil && ok
	}); err != nil {
		return res, err
	}

	if err := boot(o, composeDir, out, &res, profiles); err != nil {
		return res, err
	}
	return res, nil
}

// collect asks for every required key, one at a time, in order.
func collect(o Options, out io.Writer, pr *prompter) (collected, error) {
	var c collected
	c.env = Env{}

	// USER-FACING
	fmt.Fprintln(out, "bitbeard setup — configuration and first boot")
	// USER-FACING
	fmt.Fprintf(out, "The stack lives in %s. I will ask for every required setting, write them to .env there, then boot the mandatory Subsystems.\n", o.StackDir)
	// USER-FACING
	fmt.Fprintln(out, "Every answer is checked as you type, and no change-me placeholder is accepted. Values are stored in plaintext in .env, which is gitignored.")

	// VPN provider.
	prov, err := pr.askProvider(providerExplanation)
	if err != nil {
		return c, err
	}
	c.env["VPN_PROVIDER"] = prov.ID

	if prov.ID == customProvider {
		// The custom path embeds its credentials in the .ovpn file, so the
		// OpenVPN user/password are not asked.
		src, err := pr.askText("Path to your .ovpn file", customFileExplanation, ValidateOVPNPath)
		if err != nil {
			return c, err
		}
		c.ovpnSrc = src
	} else {
		user, err := pr.askText("OPENVPN_USER (your VPN service username)", credsExplanation, ValidateNonEmpty("OPENVPN_USER"))
		if err != nil {
			return c, err
		}
		pass, err := pr.askText("OPENVPN_PASSWORD (your VPN service password)", credsExplanation, ValidateNonEmpty("OPENVPN_PASSWORD"))
		if err != nil {
			return c, err
		}
		c.env["OPENVPN_USER"] = user
		c.env["OPENVPN_PASSWORD"] = pass

		// Any provider other than NordVPN should bring its own .ovpn file,
		// because the bundled one is a NordVPN config.
		if prov.ID != nordvpnDefault {
			src, err := pr.askText("Path to your .ovpn file (empty keeps the bundled NordVPN file)", otherProviderFileExplanation, ValidateOptionalOVPNPath)
			if err != nil {
				return c, err
			}
			c.ovpnSrc = src
		}
	}

	// Generated secrets, shown for confirmation.
	token, err := randomHex(o.Rand, 24)
	if err != nil {
		return c, err
	}
	t, err := pr.askGenerated("SERVICE_TOKEN", serviceTokenExplanation, token, 12)
	if err != nil {
		return c, err
	}
	c.env["SERVICE_TOKEN"] = t

	adminPass, err := randomHex(o.Rand, 16)
	if err != nil {
		return c, err
	}
	a, err := pr.askGenerated("ADMIN_PASSWORD", adminPasswordExplanation, adminPass, 12)
	if err != nil {
		return c, err
	}
	c.env["ADMIN_PASSWORD"] = a

	// Operational defaults.
	tz, err := pr.askText("TZ (IANA timezone, e.g. Europe/Berlin or UTC)", tzExplanation, ValidateTZ)
	if err != nil {
		return c, err
	}
	c.env["TZ"] = tz

	quota, err := pr.askText("DEFAULT_QUOTA_GB (default Storage Quota in GB)", quotaExplanation, ValidatePositiveInt("DEFAULT_QUOTA_GB"))
	if err != nil {
		return c, err
	}
	c.env["DEFAULT_QUOTA_GB"] = quota

	ttl, err := pr.askText("SESSION_TTL_HOURS (Session lifetime in hours)", ttlExplanation, ValidatePositiveInt("SESSION_TTL_HOURS"))
	if err != nil {
		return c, err
	}
	c.env["SESSION_TTL_HOURS"] = ttl

	baseDir, err := pr.askText("BASE_DOWNLOAD_DIR (the Media Root, as the containers see it; empty means /media)", baseDirExplanation, ValidateBaseDir)
	if err != nil {
		return c, err
	}
	c.env["BASE_DOWNLOAD_DIR"] = baseDir

	// Optional Subsystems: the Media Server choice and the Bot keys.
	stage, err := collectOptionalFresh(pr, o.discord())
	if err != nil {
		return c, err
	}
	c.media = stage.media
	c.bot = stage.bot
	for k, v := range stage.env {
		c.env[k] = v
	}

	return c, nil
}

// promptExplanations are the explanation blocks shown before each question.
// Each names what the key is for and where the value comes from.

// USER-FACING
const providerExplanation = `Bitbeard tunnels every Subsystem through gluetun, which connects to your VPN provider.
Choose the provider from gluetun's supported list, or pick the last option to connect
with your own .ovpn file instead. Your VPN credentials are asked next; the connection
is only made when the stack boots.
`

// USER-FACING
const credsExplanation = `These authenticate the VPN connection: your provider's service credentials (for
NordVPN, the "Manual connection setup" credentials from your NordVPN dashboard — not
your website login). They cannot be truly checked before boot; if gluetun keeps
restarting after boot, these are the first thing to re-check.
`

// USER-FACING
const customFileExplanation = `Your .ovpn file carries the whole connection, including your credentials, so no VPN
username/password is asked. It will be copied to openvpn/custom.ovpn, replacing the
bundled NordVPN config (with a confirmation first).
`

// USER-FACING
const otherProviderFileExplanation = `The bundled openvpn/custom.ovpn is a NordVPN configuration, and gluetun connects
through exactly that file — so for another provider, point at an .ovpn file from your
provider to actually use its servers. Leave empty to keep the bundled file for now
(a note will remind you).
`

// USER-FACING
const serviceTokenExplanation = `The shared bearer token for service-to-service calls (API <-> Download Manager,
manager -> Bot webhook). A strong value is generated for you.
`

// USER-FACING
const adminPasswordExplanation = `The password of the bootstrap Admin Account, seeded on first boot and used to log
into the Webui and Admin Panel as the user in ADMIN_USER ("admin"). A strong value is
generated for you; it is stored in .env, so you can look it up again.
`

// USER-FACING
const tzExplanation = `The timezone Subsystems run in (IANA name, e.g. Europe/Berlin or UTC). Set it to
yours so log timestamps and schedules read correctly.
`

// USER-FACING
const quotaExplanation = `The default Storage Quota in GB: the maximum Media Root bytes an Account may own,
counted at submission time. Admins are exempt.
`

// USER-FACING
const ttlExplanation = `How long a login Session lasts before the API refuses it, in hours.
`

// USER-FACING
const baseDirExplanation = `The Media Root: the directory tree the file manager may operate on, as the
containers see it. The stack mounts ./media there; keep the default unless you know
the stack well.
`

// mergeEnv copies seed and overlays prompted, which always wins.
func mergeEnv(seed, prompted Env) Env {
	e := Env{}
	for k, v := range seed {
		e[k] = v
	}
	for k, v := range prompted {
		e[k] = v
	}
	return e
}

// exampleSeed reads the stack's .env.example for the unprompted keys, so
// the generated .env behaves exactly like the documented manual install. It
// falls back to built-in values when the example cannot be read.
func exampleSeed(composeDir string) Env {
	seed := Env{}
	for k, v := range fallbackEnv {
		seed[k] = v
	}
	data, err := os.ReadFile(filepath.Join(composeDir, ".env.example"))
	if err != nil {
		return seed
	}
	if e, err := ParseEnv(string(data)); err == nil {
		for k, v := range e {
			seed[k] = v
		}
	}
	return seed
}

// FindComposeDir locates the directory holding docker-compose.yml: the
// bitbeard/ directory of the cloned repository, or the repository root as a
// fallback. Read-only. Shared with the Doctor, which re-derives the same
// directory to check the live stack.
func FindComposeDir(stackDir string) (string, error) {
	for _, d := range []string{filepath.Join(stackDir, "bitbeard"), stackDir} {
		if fi, err := os.Stat(filepath.Join(d, "docker-compose.yml")); err == nil && !fi.IsDir() {
			return d, nil
		}
	}
	// USER-FACING
	return "", fmt.Errorf("no docker-compose.yml under %s — the stack repository looks incomplete; delete it and rerun `bitbeard setup`", stackDir)
}

// boot brings the stack up and verifies the mandatory Subsystems and any
// enabled Optional Subsystems are running. profiles is the COMPOSE_PROFILES
// value in effect.
func boot(o Options, composeDir string, out io.Writer, res *Result, profiles string) error {
	comp := o.Compose
	if comp == nil {
		comp = DockerCompose{}
	}
	// USER-FACING
	fmt.Fprintf(out, "Booting the stack: docker compose up -d in %s.\n", composeDir)
	// USER-FACING
	fmt.Fprintln(out, "First boot builds the api, manager and webui images and pulls the rest — this can take several minutes.")
	if err := comp.Up(composeDir); err != nil {
		// USER-FACING
		return fmt.Errorf("docker compose up failed: %v", err)
	}
	res.Booted = true

	// USER-FACING
	fmt.Fprintln(out, "Verifying the mandatory Subsystems are running (docker compose ps)...")
	ps, err := comp.PS(composeDir)
	if err != nil {
		// USER-FACING
		return fmt.Errorf("docker compose ps failed: %v", err)
	}
	cs, err := parseComposePS(ps)
	if err != nil {
		// USER-FACING
		return fmt.Errorf("could not read the docker compose ps output: %v", err)
	}
	notRunning, failedInit := verifyMandatoryRunning(cs)
	if len(notRunning) > 0 || len(failedInit) > 0 {
		// USER-FACING
		return fmt.Errorf("the mandatory stack did not come up cleanly: not running: %s; failed init containers: %s. During first boot this is often just image builds still settling: wait a minute and rerun `bitbeard setup`, which will skip straight to this check. If gluetun keeps restarting, the VPN credentials are the usual cause — see `docker compose logs gluetun` in %s.",
			strings.Join(notRunning, ", "), strings.Join(failedInit, ", "), composeDir)
	}

	// USER-FACING
	fmt.Fprintln(out, "The mandatory stack is up.")

	// Optional Subsystems: only the enabled ones are expected, and each is
	// verified here.
	if missing := verifyOptionalRunning(cs, profiles); len(missing) > 0 {
		// USER-FACING
		return fmt.Errorf("the enabled Optional Subsystems did not come up: not running: %s. Check `docker compose logs <name>` in %s — for the Bot, DISCORD_BOT_TOKEN is the usual cause; a Media Server may still be pulling its image on first boot, in which case rerunning `bitbeard setup` re-checks.",
			strings.Join(missing, ", "), composeDir)
	}

	// USER-FACING
	fmt.Fprintln(out, "  Webui and Admin Panel: http://localhost:8081 — log in as the ADMIN_USER value with the ADMIN_PASSWORD from .env")
	// USER-FACING
	fmt.Fprintln(out, "  Jackett (add your Indexers there): http://localhost:9117")
	// USER-FACING
	fmt.Fprintln(out, "  qBittorrent WebUI: http://localhost:8080")
	// USER-FACING
	fmt.Fprintf(out, "  Stack directory: %s\n", composeDir)
	if hasProfile(profiles, profileJellyfin) {
		// USER-FACING
		fmt.Fprintln(out, "  Jellyfin (Media Server): http://localhost:8096 — open it once to create your admin account (first-run setup)")
	}
	if hasProfile(profiles, profilePlex) {
		// SETTLED DESIGN DECISION (ticket #18): the wizard never collects a
		// PLEX_CLAIM value. Claim tokens expire within minutes, so asking
		// for one here is a race the operator always loses. The plex
		// Subsystem boots unclaimed and is claimed by hand at the URL
		// below; do not "optimize" this into a PLEX_CLAIM prompt.
		// USER-FACING
		fmt.Fprintln(out, "  Plex (Media Server): http://localhost:32400/web — sign in and claim this server there (no claim token was asked for, on purpose)")
	}
	if hasProfile(profiles, profileBot) {
		// USER-FACING
		fmt.Fprintln(out, "  Bot: running; it announces finished Downloads in DISCORD_ANNOUNCE_CHANNEL_ID (invite it to your server if it has not joined yet)")
	}
	// USER-FACING
	fmt.Fprintln(out, "Note: VPN credentials cannot be truly checked before boot. If gluetun keeps restarting, re-check OPENVPN_USER and OPENVPN_PASSWORD with `docker compose logs gluetun` in the stack directory.")
	return nil
}
