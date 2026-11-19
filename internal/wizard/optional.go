package wizard

// optional.go is the Optional Subsystems stage of the configuration and
// boot stage: the Media Server choice (Jellyfin, Plex, or none — exactly
// one at most) and the Bot's Discord keys. Enabling either flips
// COMPOSE_PROFILES, the same value the Admin Panel reads as its Media
// Server source of truth. The stage is rerun-aware: only missing choices
// and keys are asked, a skipped question never erases a previous choice,
// and an already-made Media Server choice can be switched (old profile
// off, new one on).

import (
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strings"
	"time"
)

// The documented default JELLYFIN_URL: how the api container reaches the
// Jellyfin Subsystem's host-published 8096 (extra_hosts host-gateway in
// docker-compose.yml). The wizard, .env.example and the api's config all
// name the same value; this constant is the Go side's single home for it.
const defaultJellyfinURL = "http://host.docker.internal:8096"

// The Optional Subsystem profile names, as COMPOSE_PROFILES and
// docker-compose.yml understand them. Order is the canonical write order:
// the Bot first, then the Media Servers.
const (
	profileBot      = "bot"
	profilePlex     = "plex"
	profileJellyfin = "jellyfin"
)

// deriveProfiles computes the new COMPOSE_PROFILES value. old is the
// previous value ("" on a fresh install); media is profileJellyfin,
// profilePlex, or "" for no Media Server; bot enables the Bot. The Media
// Server slot is replaced wholesale — exactly one Media Server profile is
// ever on — and the Bot is added or left off. Profiles outside this
// stage's vocabulary are preserved verbatim, so a rerun never silently
// drops something the operator set by hand.
func deriveProfiles(old, media string, bot bool) string {
	var out []string
	if bot {
		out = append(out, profileBot)
	}
	if media == profilePlex || media == profileJellyfin {
		out = append(out, media)
	}
	for _, p := range strings.Fields(old) {
		if p != profileBot && p != profilePlex && p != profileJellyfin {
			out = append(out, p)
		}
	}
	return strings.Join(out, " ")
}

// mediaFromProfiles extracts the Media Server profile from a
// COMPOSE_PROFILES value: profilePlex, profileJellyfin, or "" for none.
// Both present would be a hand-edited anomaly; the first one wins.
func mediaFromProfiles(profiles string) string {
	for _, p := range strings.Fields(profiles) {
		if p == profilePlex || p == profileJellyfin {
			return p
		}
	}
	return ""
}

// hasProfile reports whether the space-separated profiles value contains
// the named entry.
func hasProfile(profiles, name string) bool {
	for _, p := range strings.Fields(profiles) {
		if p == name {
			return true
		}
	}
	return false
}

// verifyOptionalRunning checks the parsed ps output: every enabled
// Optional Subsystem's container must be running. It returns the profile
// names whose containers are not up.
func verifyOptionalRunning(cs []composeContainer, profiles string) []string {
	byName := make(map[string]composeContainer, len(cs))
	for _, c := range cs {
		byName[strings.TrimPrefix(c.Name, "/")] = c
	}
	var missing []string
	for _, p := range strings.Fields(profiles) {
		switch p {
		case profileBot, profilePlex, profileJellyfin:
			if c, ok := byName[p]; !ok || c.State != "running" {
				missing = append(missing, p)
			}
		}
	}
	return missing
}

// DiscordChecker live-checks a bot token against Discord's API.
type DiscordChecker interface {
	// CheckToken returns the HTTP status Discord answered for the token,
	// or a transport error when Discord could not be reached at all.
	CheckToken(token string) (int, error)
}

// DiscordAPI is the production DiscordChecker: a GET of
// https://discord.com/api/v10/users/@me carrying the bot token.
type DiscordAPI struct {
	// Client is the HTTP client; nil means a 10-second-timeout client.
	Client *http.Client
	// BaseURL overrides the API root (tests only).
	BaseURL string
}

// CheckToken issues the GET and returns the response status code.
func (d DiscordAPI) CheckToken(token string) (int, error) {
	base := d.BaseURL
	if base == "" {
		base = "https://discord.com/api/v10"
	}
	req, err := http.NewRequest(http.MethodGet, base+"/users/@me", nil)
	if err != nil {
		return 0, err
	}
	req.Header.Set("Authorization", "Bot "+token)
	c := d.Client
	if c == nil {
		c = &http.Client{Timeout: 10 * time.Second}
	}
	resp, err := c.Do(req)
	if err != nil {
		return 0, err
	}
	defer resp.Body.Close()
	_, _ = io.Copy(io.Discard, resp.Body) // drained so the connection is reusable
	return resp.StatusCode, nil
}

// JellyfinChecker live-checks an API token against a Jellyfin server's
// /System/Info (ticket #31). The URL is part of the check: a wrong URL
// and an unreachable server are the same network failure from here.
type JellyfinChecker interface {
	// CheckToken returns the HTTP status Jellyfin answered for the token
	// at baseURL, or a transport error when it could not be reached at all.
	CheckToken(baseURL, token string) (int, error)
}

// JellyfinAPI is the production JellyfinChecker: a GET of
// <baseURL>/System/Info carrying the API token.
type JellyfinAPI struct {
	// Client is the HTTP client; nil means a 10-second-timeout client.
	Client *http.Client
}

// CheckToken issues the GET and returns the response status code.
func (j JellyfinAPI) CheckToken(baseURL, token string) (int, error) {
	u, err := url.Parse(baseURL)
	if err != nil || u.Host == "" || (u.Scheme != "http" && u.Scheme != "https") {
		return 0, fmt.Errorf("%q is not a usable Jellyfin URL", baseURL)
	}
	req, err := http.NewRequest(http.MethodGet, baseURL+"/System/Info", nil)
	if err != nil {
		return 0, err
	}
	// The Authorization form, not X-Emby-Token: recent Jellyfin releases
	// (10.11.x observed) reject a bare API key in X-Emby-Token with 401,
	// while the Authorization header works across versions.
	req.Header.Set(
		"Authorization", fmt.Sprintf(`MediaBrowser Token=%q`, token),
	)
	c := j.Client
	if c == nil {
		c = &http.Client{Timeout: 10 * time.Second}
	}
	resp, err := c.Do(req)
	if err != nil {
		return 0, err
	}
	defer resp.Body.Close()
	_, _ = io.Copy(io.Discard, resp.Body) // drained so the connection is reusable
	return resp.StatusCode, nil
}

// optionalStage is what the Optional Subsystems prompts produced.
type optionalStage struct {
	// media is the Media Server profile after the stage: profileJellyfin,
	// profilePlex, or "" (none).
	media string
	// bot is whether the Bot should end up enabled.
	bot bool
	// env carries newly collected keys (DISCORD_BOT_TOKEN,
	// DISCORD_ANNOUNCE_CHANNEL_ID) on top of what already existed. An
	// empty DISCORD_ANNOUNCE_CHANNEL_ID is a valid state: the Bot runs
	// without announcements.
	env Env
	// profiles is the new COMPOSE_PROFILES value.
	profiles string
	// changed reports whether anything differs from the existing .env.
	changed bool
}

// collectOptionalFresh runs the Optional Subsystems stage on a fresh (or
// fully rewritten) .env: the Media Server choice and the Bot keys are
// asked in full; an empty answer skips each.
func collectOptionalFresh(pr *prompter, check DiscordChecker, jellyfin JellyfinChecker) (optionalStage, error) {
	var s optionalStage
	s.env = Env{}

	media, err := pr.askMediaServer(mediaServerExplanation)
	if err != nil {
		return s, err
	}
	s.media = media

	// Jellyfin provisioning integration (ticket #31): offered only when
	// the chosen Media Server is Jellyfin; an empty token answer leaves
	// the keys unset, which is the integration being off.
	if media == profileJellyfin {
		tok, url, err := pr.askJellyfinIntegration(defaultJellyfinURL, jellyfin)
		if err != nil {
			return s, err
		}
		if tok != "" {
			s.env["JELLYFIN_API_TOKEN"] = tok
			s.env["JELLYFIN_URL"] = url
		}
	}

	tok, err := pr.askBotToken(botExplanation, check)
	if err != nil {
		return s, err
	}
	if tok == "" {
		s.profiles = deriveProfiles("", s.media, false)
		return s, nil
	}
	ch, err := pr.askBotChannel(botChannelExplanation)
	if err != nil {
		return s, err
	}
	s.bot = true
	s.env["DISCORD_BOT_TOKEN"] = tok
	s.env["DISCORD_ANNOUNCE_CHANNEL_ID"] = ch
	s.profiles = deriveProfiles("", s.media, true)
	return s, nil
}

// collectOptionalRerun runs the Optional Subsystems stage against an
// existing valid .env: only what is missing is asked, a skipped question
// keeps the previous value, and the Media Server can be switched.
func collectOptionalRerun(pr *prompter, out io.Writer, e Env, check DiscordChecker, jellyfin JellyfinChecker) (optionalStage, error) {
	var s optionalStage
	s.env = Env{}
	old := e["COMPOSE_PROFILES"]

	// State first, then explicit offers. Answering "no" to an offer keeps
	// the existing choice; the prompt's own skip option removes it.
	cur := mediaFromProfiles(old)
	// USER-FACING
	curLabel := cur
	if curLabel == "" {
		curLabel = "none"
	}
	// USER-FACING
	botLabel := "disabled"
	s.bot = hasProfile(old, profileBot)
	if s.bot {
		botLabel = "enabled"
	}
	// USER-FACING
	fmt.Fprintf(out, "Optional Subsystems: Media Server %s, Bot %s (from COMPOSE_PROFILES=%q).\n", curLabel, botLabel, old)

	// Media Server: switch offer. Skipped (or declined) keeps the current
	// value — a rerun never erases a choice by omission.
	switchMedia, err := pr.askYesNo("Change the Media Server?")
	if err != nil {
		return s, err
	}
	s.media = cur
	if switchMedia {
		m, err := pr.askMediaServer(mediaServerExplanation)
		if err != nil {
			return s, err
		}
		s.media = m
	}

	// Jellyfin provisioning integration: offered only while the token is
	// unset — a configured integration is kept untouched, and a rerun can
	// add it later exactly once. The runtime check (the api side) also
	// names the Media Server, so keys left over after a switch to Plex
	// stay inert.
	if s.media == profileJellyfin && e["JELLYFIN_API_TOKEN"] == "" {
		// USER-FACING
		add, err := pr.askYesNo("Enable Jellyfin provisioning (approving a registration also creates the matching Jellyfin user)?")
		if err != nil {
			return s, err
		}
		if add {
			jtok, jurl, err := pr.askJellyfinIntegration(defaultJellyfinURL, jellyfin)
			if err != nil {
				return s, err
			}
			if jtok != "" {
				s.env["JELLYFIN_API_TOKEN"] = jtok
				s.env["JELLYFIN_URL"] = jurl
			} else {
				// USER-FACING
				fmt.Fprintln(out, "No token given — Jellyfin provisioning stays off.")
			}
		}
	}

	// Bot: only the missing keys are asked; present values are kept.
	tok := e["DISCORD_BOT_TOKEN"]
	ch := e["DISCORD_ANNOUNCE_CHANNEL_ID"]
	if s.bot {
		if tok == "" {
			// USER-FACING
			fmt.Fprintln(out, "The Bot is enabled but DISCORD_BOT_TOKEN is not set — it cannot start without one.")
			ntok, err := pr.askBotToken(botExplanation, check)
			if err != nil {
				return s, err
			}
			if ntok == "" {
				// USER-FACING
				fmt.Fprintln(out, "Leaving DISCORD_BOT_TOKEN unset turns the Bot back off.")
				s.bot = false
			} else {
				s.env["DISCORD_BOT_TOKEN"] = ntok
				if ch == "" {
					nch, err := pr.askBotChannel(botChannelExplanation)
					if err != nil {
						return s, err
					}
					s.env["DISCORD_ANNOUNCE_CHANNEL_ID"] = nch
				}
			}
		} else if ch == "" {
			// A Bot without a channel is a normal state: announcements
			// are simply off. The channel is offered plainly (the same
			// way the Media Server switch is), not demanded; skipping
			// keeps the announcement-free state.
			add, err := pr.askYesNo("Add an announcement channel to the Bot? (it currently runs without announcements)")
			if err != nil {
				return s, err
			}
			if add {
				nch, err := pr.askBotChannel(botChannelExplanation)
				if err != nil {
					return s, err
				}
				if nch != "" {
					s.env["DISCORD_ANNOUNCE_CHANNEL_ID"] = nch
				} else {
					// USER-FACING
					fmt.Fprintln(out, "Leaving the Bot without announcements.")
				}
			}
		}
	} else {
		enable, err := pr.askYesNo("Enable the Bot (the Discord Frontend) now?")
		if err != nil {
			return s, err
		}
		if enable {
			ntok, err := pr.askBotToken(botExplanation, check)
			if err != nil {
				return s, err
			}
			if ntok == "" {
				// USER-FACING
				fmt.Fprintln(out, "No token given — the Bot stays disabled.")
			} else {
				nch, err := pr.askBotChannel(botChannelExplanation)
				if err != nil {
					return s, err
				}
				s.bot = true
				s.env["DISCORD_BOT_TOKEN"] = ntok
				// An empty answer means "no announcements"; a skipped
				// question never erases a previous choice, so an
				// existing channel is only replaced by a real answer.
				if nch != "" || ch == "" {
					s.env["DISCORD_ANNOUNCE_CHANNEL_ID"] = nch
				}
			}
		}
	}

	s.profiles = deriveProfiles(old, s.media, s.bot)
	s.changed = s.profiles != old || len(s.env) > 0
	return s, nil
}

// askMediaServer presents the Media Server choice: Jellyfin (recommended,
// listed first), Plex, or skip. Returns profileJellyfin, profilePlex, or
// "" when the operator skips (no Media Server for now).
func (p *prompter) askMediaServer(explanation string) (string, error) {
	p.explain(explanation)
	type choice struct {
		value string
		label string
	}
	options := []choice{
		// USER-FACING
		{profileJellyfin, "Jellyfin"},
		// USER-FACING
		{profilePlex, "Plex"},
		// USER-FACING
		{"", "Skip (enable one later by rerunning `bitbeard setup`)"},
	}
	for i, o := range options {
		// USER-FACING
		p.printf("  %d. %s\n", i+1, o.label)
	}
	for {
		// USER-FACING
		p.printf("Media Server (empty = skip): ")
		line, err := p.readLine()
		if err != nil {
			return "", err
		}
		if n, ok := parseNumber(line); ok {
			if n >= 1 && n <= len(options) {
				return options[n-1].value, nil
			}
			// USER-FACING
			p.printf("  %d is not one of the choices above\n", n)
			continue
		}
		switch lower(line) {
		case "":
			return "", nil
		case "jellyfin":
			return profileJellyfin, nil
		case "plex":
			return profilePlex, nil
		case "skip", "none":
			return "", nil
		}
		// USER-FACING
		p.printf("  %q is not one of the choices above\n", line)
	}
}

// askBotToken prompts for DISCORD_BOT_TOKEN with live validation against
// Discord. An empty answer skips the Bot entirely (the caller then does
// not ask for the channel). A 401 is Discord's definitive "unknown token"
// and is treated as a rejectable typo: the answer is refused and asked
// again. When Discord cannot be reached — this machine may be offline —
// or answers unexpectedly, the operator may accept the token deliberately
// after being told exactly that.
func (p *prompter) askBotToken(explanation string, check DiscordChecker) (string, error) {
	p.explain(explanation)
	for {
		// USER-FACING
		p.printf("DISCORD_BOT_TOKEN (empty = run without the Bot): ")
		line, err := p.readLine()
		if err != nil {
			return "", err
		}
		tok := trimSpace(line)
		if tok == "" {
			return "", nil
		}
		if err := rejectChangeMe("DISCORD_BOT_TOKEN", tok); err != nil {
			// USER-FACING
			p.printf("  %v\n", err)
			continue
		}
		status, cerr := check.CheckToken(tok)
		switch {
		case cerr != nil:
			// USER-FACING
			p.printf("  Discord could not be reached to check the token (%v). This machine may be offline; you can accept the token unverified and continue deliberately.\n", cerr)
			ok, aerr := p.askYesNo("Accept the token without checking and continue?")
			if aerr != nil {
				return "", aerr
			}
			if ok {
				return tok, nil
			}
		case status == http.StatusUnauthorized:
			// USER-FACING
			p.printf("  Discord rejected that token (401 unknown token) — a typo? Enter it again, or leave empty to skip the Bot.\n")
		case status != http.StatusOK:
			// USER-FACING
			p.printf("  Discord answered %d instead of 200, so the token could not be confirmed.\n", status)
			ok, aerr := p.askYesNo("Accept the token anyway and continue?")
			if aerr != nil {
				return "", aerr
			}
			if ok {
				return tok, nil
			}
		default:
			return tok, nil
		}
	}
}

// askJellyfinIntegration prompts for the Jellyfin provisioning
// integration's two keys, mirroring the Discord token check's shape
// (ticket #31): the API token first — an empty answer skips the whole
// integration — then the URL, pre-filled with the documented default.
// The token is live-validated against Jellyfin's /System/Info at the
// entered URL: a 401 is Jellyfin's definitive "unknown token" and is
// treated as a rejectable typo (the token is asked again, the URL kept);
// when Jellyfin cannot be reached — this machine may be offline, or the
// URL may be wrong — the operator may accept the token deliberately after
// being told exactly that. Returns ("", "", nil) when skipped.
func (p *prompter) askJellyfinIntegration(defaultURL string, check JellyfinChecker) (string, string, error) {
	p.explain(jellyfinIntegrationExplanation)
	target := ""
	for {
		// USER-FACING
		p.printf("JELLYFIN_API_TOKEN (empty = no automatic Jellyfin provisioning): ")
		line, err := p.readLine()
		if err != nil {
			return "", "", err
		}
		tok := trimSpace(line)
		if tok == "" {
			return "", "", nil
		}
		if err := rejectChangeMe("JELLYFIN_API_TOKEN", tok); err != nil {
			// USER-FACING
			p.printf("  %v\n", err)
			continue
		}
		// The URL is asked once, after a real token exists — a skip
		// never pays for it.
		if target == "" {
			// USER-FACING
			url, err := p.askText(
				"JELLYFIN_URL (empty = the documented default "+defaultURL+")",
				"", ValidateJellyfinURLOptional,
			)
			if err != nil {
				return "", "", err
			}
			target = url
			if target == "" {
				target = defaultURL
			}
		}
		status, cerr := check.CheckToken(target, tok)
		switch {
		case cerr != nil:
			// USER-FACING
			p.printf("  Jellyfin could not be reached to check the token (%v). This machine may be offline, or the URL may be wrong; you can accept the token unverified and continue deliberately.\n", cerr)
			ok, aerr := p.askYesNo("Accept the token without checking and continue?")
			if aerr != nil {
				return "", "", aerr
			}
			if ok {
				return tok, target, nil
			}
		case status == http.StatusUnauthorized:
			// USER-FACING
			p.printf("  Jellyfin rejected that token (401 unauthorized) — a typo? Enter it again, or leave empty to skip the integration.\n")
		case status != http.StatusOK:
			// USER-FACING
			p.printf("  Jellyfin answered %d instead of 200, so the token could not be confirmed.\n", status)
			ok, aerr := p.askYesNo("Accept the token anyway and continue?")
			if aerr != nil {
				return "", "", aerr
			}
			if ok {
				return tok, target, nil
			}
		default:
			return tok, target, nil
		}
	}
}

// askBotChannel prompts for DISCORD_ANNOUNCE_CHANNEL_ID. The channel is
// optional: an empty answer is valid and means the Bot runs without
// announcements. A typed answer must be a channel ID.
func (p *prompter) askBotChannel(explanation string) (string, error) {
	return p.askText("DISCORD_ANNOUNCE_CHANNEL_ID (empty = run without announcements)", explanation, ValidateChannelIDOptional)
}

// ValidateChannelID accepts a Discord channel ID: a snowflake, digits
// only.
func ValidateChannelID(v string) (string, error) {
	v = trimSpace(v)
	if v == "" {
		// USER-FACING
		return "", fmt.Errorf("DISCORD_ANNOUNCE_CHANNEL_ID cannot be empty once the Bot is enabled")
	}
	for _, r := range v {
		if r < '0' || r > '9' {
			// USER-FACING
			return "", fmt.Errorf("DISCORD_ANNOUNCE_CHANNEL_ID is %q; expected the channel's ID (digits only — right-click the channel and Copy ID)", v)
		}
	}
	return v, nil
}

// ValidateChannelIDOptional is ValidateChannelID with the empty answer
// allowed: announcing is optional, so "" is a valid value meaning the
// Bot runs without announcements. Anything non-empty must still be a
// channel ID.
func ValidateChannelIDOptional(v string) (string, error) {
	v = trimSpace(v)
	if v == "" {
		return "", nil
	}
	return ValidateChannelID(v)
}

// Explanations shown before the Optional Subsystems questions.

// USER-FACING
const mediaServerExplanation = `A Media Server serves the Media Root to viewers outside the stack: Jellyfin or
Plex — exactly one, or none for now. The choice is written to COMPOSE_PROFILES,
which the Admin Panel reads as its Media Server entry.
Jellyfin is recommended: free, no account needed, and its web UI creates your
admin account on first open. Plex needs no setup token at all; after boot you
claim the server in its web UI (the wizard prints the link). Claim tokens
themselves expire within minutes, so the wizard never asks for one.
`

// USER-FACING
const botExplanation = `The Bot is the Discord Frontend: it relays searches and can announce finished
Downloads in a channel. Enabling it takes one value:
  - DISCORD_BOT_TOKEN: from https://discord.com/developers/applications —
    your application's Bot page, Reset Token.
Announcing is optional: the channel to announce in (DISCORD_ANNOUNCE_CHANNEL_ID,
the channel's ID — Discord: enable Developer Mode, right-click the channel,
Copy ID) can be left empty to run the Bot without announcements, and a channel
can be added later by rerunning ` + "`bitbeard setup`" + `.
The token is checked against Discord's API as you enter it. Leave it empty
to run without the Bot; it can be enabled later by rerunning ` + "`bitbeard setup`" + `.
`

// USER-FACING
const jellyfinIntegrationExplanation = `Optional: when the integration is on, approving a Registration Request also
creates the matching Jellyfin user (same username) on this server, with a
random password the user replaces later from the Webui — the password is
never shown to anyone. It takes two values:
  - JELLYFIN_API_TOKEN: from Jellyfin's Dashboard -> API Keys.
  - JELLYFIN_URL: where the api container reaches this server; the default
    is right for the bundled setup. The token is checked against the server
    as you enter it. Leave the token empty to run without the integration;
    it can be enabled later by rerunning ` + "`bitbeard setup`" + `.
`

// USER-FACING
const botChannelExplanation = `The channel the Bot announces finished Downloads in: the Discord channel's
ID (Developer Mode, right-click the channel, Copy ID) — not its name and not
an invite link. Leave empty to run the Bot without announcements; a channel
can be added later by rerunning ` + "`bitbeard setup`" + `. It cannot be truly checked
before boot; if the Bot announces but stays silent on completions, this is
the first thing to re-check.
`
