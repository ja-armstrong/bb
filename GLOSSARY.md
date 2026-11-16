# Bitbeard

Bitbeard is a self-hosted media automation stack: a Discord bot and web frontend for
searching torrent indexers and submitting downloads, a download manager driving
qBittorrent, and an admin panel for users, subsystems, logs, and media files.

## Language

### Services

**API**:
The central FastAPI service. Owns identity, sessions, the Audit Ledger, and all admin
capabilities including docker control. Every Frontend talks only to the API.
_Avoid_: backend, bitbeard service, server

**Download Manager**:
The service that owns the torrent lifecycle, the qBittorrent client, search (Jackett/
Torznab), and all file operations on the Media Root.
_Avoid_: manager, downloader, downloads service

**Frontend**:
A thin client that reaches the stack only through the API: the Webui, the Admin Panel,
or the Bot. No Frontend holds privileged access of its own.
_Avoid_: ui, client, app

**Webui**:
The user-facing Frontend for searching, submitting downloads, and managing the
Account's own downloads, served by nginx.
_Avoid_: web UI, dashboard

**Admin Panel**:
The Admin-only Frontend served at `/admin` inside the Webui container: users, subsystem
control, Audit Ledger, file manager, VPN settings.
_Avoid_: admin ui, control panel, dashboard

**Bot**:
The Discord Frontend. Authenticates to the API as a Service Account and relays the
acting Discord user on every request.
_Avoid_: discord bot (acceptable in prose)

**Subsystem**:
A docker-compose service in this stack, visible to and controllable from the Admin
Panel.
_Avoid_: component, module, service (when distinguishing one Subsystem from another)

**Optional Subsystem**:
A Subsystem that may be absent from the running stack: not enabled during Bootstrap,
or disabled later from the Admin Panel. Currently the Bot and the Media Server; all
others are mandatory and restartable only.
_Avoid_: non-critical service

**Media Server**:
The Optional Subsystem that serves the Media Root to viewers outside the stack. Plex
or Jellyfin, chosen once at Bootstrap. No Frontend or service talks to it; it reads
the Media Root directly — except the API's one optional provisioning call, which
creates the matching Jellyfin user when a Registration Request is approved (off
unless configured).
_Avoid_: plex (when naming the role), jellyfin (when naming the role)

### Bootstrap

**Bootstrap**:
The setup program that prepares a machine to run the stack: installs prerequisites,
fetches the repository, collects credentials, and brings the stack up. Rerunning it
enables a skipped Optional Subsystem or revisits any credential.
_Avoid_: installer, setup script, wizard (for the program as a whole)

**Doctor**:
The Bootstrap's check-only mode: verifies prerequisites, configuration, and stack
health, changing nothing.
_Avoid_: status, diagnostics

### Identity & audit

**Account**:
A login in the user store with a Role (Admin or User), optionally linked to a Discord
ID. Created by an Admin from the Admin Panel; one bootstrap Admin is seeded from env.
_Avoid_: user (when naming the record), profile, login (as noun)

**Registration Request**:
A request from an anonymous visitor to create an Account (username, hashed password,
optional note), pending Admin approval. Approving creates the Account; denying
discards the request.
_Avoid_: signup, invite, sign-up form

**Role**:
Either Admin (Admin Panel access, user management, subsystem control, file manager,
ledger) or User (Webui only).

**Session**:
Server-side login state stored in SQLite and presented as an HTTP-only cookie. Issued
by the API on login; revocable server-side.
_Avoid_: token, JWT

**Service Account**:
A non-human Account used by a Frontend to authenticate to the API. The Bot uses one.
_Avoid_: machine user, bot account

**Link Code**:
A one-time code, generated in the Webui, that binds an Account to a Discord ID.
_Avoid_: invite code, pairing token

**Actor**:
The Account responsible for an audited action — directly, or via a linked Discord ID,
or a Service Account acting for a Discord user.
_Avoid_: user (in log entries), initiator

**Audit Ledger**:
The SQLite record of every consequential action: timestamp, Actor, action, details.
Includes logins, registration requests, searches, downloads, file operations, and
all admin actions.
_Avoid_: logs, activity log (in prose when naming the store)

### Media & torrents

**Download**:
A torrent submitted through the stack, owned by the submitting Actor. The unit users
manage on the Webui and the unit of quota accounting — even when it contains many
files.
_Avoid_: torrent (when naming the owned record), item

**In-flight Download**:
A Download submitted but not yet completed. Its projected size counts against the
owner's Storage Quota immediately.
_Avoid_: pending download, active torrent (that term means the qBittorrent lifecycle)

**Storage Quota**:
The maximum Media Root bytes an Account may own, counting on-disk and In-flight
Downloads. Admins are exempt. Enforced at submission: a Download that would exceed the
cap is rejected.
_Avoid_: disk limit, space cap, storage limit

**Media Root**:
`BASE_DOWNLOAD_DIR` — the directory tree the file manager may operate on. Hard sandbox
boundary: no file operation may escape it.
_Avoid_: media dir, downloads folder, /media

**Category**:
Movie, TV Show, Audiobook, or Other — the subdirectory of the Media Root a
download lands in.

**Active Torrent**:
A torrent currently owned by the Download Manager's lifecycle: downloading, seeding, or
not yet drained. Its paths are Protected.
_Avoid_: running torrent, in-progress download

**Protected Path**:
A file or directory inside an Active Torrent's save path. File operations refuse to
touch it.
_Avoid_: locked file, reserved path

### Events & search

**Lifecycle Event**:
An event emitted by the Download Manager about a torrent (submitted, completed,
drained). Sent to the API for the Audit Ledger and to the Bot's webhook for Discord
announcement.
_Avoid_: announcer message, notification

**Completion Event**:
The Lifecycle Event emitted when a torrent finishes; the trigger for Discord
announcement.
_Avoid_: done signal, finished event

**Indexer**:
A torrent tracker aggregator queried through Jackett's Torznab API.
_Avoid_: tracker (when meaning the Jackett-backed search source)

**Search**:
A Torznab query against all configured Indexers, submitted by an Actor through any
Frontend.
_Avoid_: query (as noun for the recorded action)
