# Spec: System split, admin panel, users, audit, file manager

Status of this document: sharpened through grilling on 2026-11-13; big-bang delivery
(the stack is a sandbox with no real traffic, so we build the target state in one go).
Decisions of record: ADR-0001, ADR-0002, ADR-0003. Vocabulary: `GLOSSARY.md`.

## Goal

Replace the monolithic `bitbeard.py` process with a three-tier stack, add real
per-user identity, and add an Admin Panel covering: subsystem status/control, the
Audit Ledger, media file management, and VPN settings/testing. Additionally: users
manage the media they downloaded themselves, and every non-admin Account has a
Storage Quota that gates new submissions.

## Target architecture

```
 Frontends                    API tier                     Media tier
┌───────────────┐
│ Webui (nginx) │──┐      ┌─────────────────┐        ┌────────────────────┐
│  /      users │  ├─────▶│ API (FastAPI)   │───────▶│ Download Manager   │──▶ qBittorrent
│  /admin admins│  │ HTTP │ sessions, roles │ token  │ lifecycle, qbt     │──▶ Jackett (search)
└───────────────┘  │      │ ledger, docker  │        │ search, file ops   │
┌───────────────┐  │      └─────▲───────────┘        └─────────┬──────────┘
│ Bot (discord) │──┘            │   ▲ lifecycle events         │
│  + webhook rcv│◀──── Completion Event ───────────────────────┘
└───────────────┘      (manager → bot webhook, direct)
```

All containers ride `network_mode: service:gluetun` as today. Host ports via gluetun:
8080 (qBittorrent), 9117 (Jackett), 8081 (Webui, now serving `/admin` too). API,
manager, and bot listener stay internal (9870, 9871, bot's own ephemeral port).

### Services after the split

| Subsystem | Image source | Mandatory? | Notes |
|---|---|---|---|
| gluetun | qmcgaw/gluetun | yes | VPN gateway; editable from Admin Panel |
| init-configs | alpine | yes (one-shot) | unchanged |
| qbittorrent | linuxserver/qbittorrent | yes | restartable only |
| jackett | linuxserver/jackett | yes | restartable only |
| plex | linuxserver/plex | yes | restartable only |
| api | `./api` (new Dockerfile) | yes | holds docker.sock (ADR-0003) |
| manager | `./manager` (new Dockerfile) | yes | owns media volume writes |
| webui | `./webui` | **mandatory** (hosts `/admin`; disabling it would lock out the panel — resolved contradiction) | restartable only |
| bot | `./bot` (new Dockerfile) | **optional** — the only disableable Subsystem | webhook receiver + discord client |

### Repo layout (big-bang rewrite)

```
bitbeard/
├── api/            FastAPI: auth, sessions, ledger, admin endpoints, docker control, proxy to manager
├── manager/        DownloadManager + qbt client + search.py + file ops + webhook emitter
├── bot/            discord client (message→search, magnet→submit, cancel/progress) + webhook receiver
├── webui/          index.html (user SPA) + admin.html (admin SPA) + nginx conf (no basic auth)
├── tests/          per-service tests
└── docker-compose.yml, .env.example (updated)
```

## Identity & auth (ADR-0002)

- **Store**: single SQLite DB on a shared volume (`data/bitbeard.db`, WAL mode +
  busy timeout — two writers: API and manager): `users`, `sessions`, `audit` tables
  (API-owned) and `downloads` table (manager-owned). No ORM; plain `sqlite3` +
  parameterized queries.
- **Bootstrap**: on first boot, API seeds an Admin from `ADMIN_USER`/`ADMIN_PASSWORD`
  env. All further Accounts are created in the Admin Panel.
- **Quota assignment**: global default `DEFAULT_QUOTA_GB` in config; Admin may set a
  per-user override (or unlimited) when creating/editing an Account. Admin Role is
  always exempt.
- **Login**: each SPA has a login page → `POST /auth/login` → HTTP-only session cookie
  (random 256-bit token, SQLite-backed, configurable expiry, revocable).
- **Roles**: Admin = everything incl. Admin Panel. User = Webui only. Admins can also
  use the Webui.
- **Account self-service**: users change their own password (`POST /auth/password`).
  Admins reset any Account's password from the panel (no email in this stack).
- **Discord linking**: Admin creates an Account → user logs into Webui → generates a
  one-time Link Code → the account is bound to their Discord ID (entered in the Webui,
  verified against the next bot interaction's reported Discord ID).
- **Bot auth**: Service Account credential from `.env`; every bot-proxied call carries
  the acting Discord user's ID; API resolves linked Account (else records
  `discord:<name>` as Actor).
- **Service token**: shared bearer token `SERVICE_TOKEN` in `.env` for API↔manager and
  manager→bot-webhook calls. Nginx basic auth and `WEBUI_USER`/`WEBUI_PASSWORD` die.
- **CORS**: locked to the Webui origin; no more `*`.

## Audit Ledger

- One SQLite table: `ts, actor, action, details(json)`.
- **Recorded**: logins (success/fail), searches (query + Actor), download
  submissions/cancels, file operations (op type + source/target paths, admin or
  Download-level user ops), admin actions (user create/disable/reset, session revoke,
  quota assign/override/change, subsystem restart/stop/start, VPN config change, VPN
  test), lifecycle events (submitted/completed/drained with Actor).
- **Emitters**: API writes rows for everything it sees; manager POSTs Lifecycle Events
  to `POST /internal/events` on the API (token-authed) and mirrors Completion Events to
  the Bot's webhook.
- **View**: Admin Panel page with filter by Actor/action/date. No auto-purge in v1.

## Ownership & Storage Quota

- **Download records**: on submission the manager inserts a `downloads` row
  (`id, account, discord_name, name, category, save_path, size_bytes, status,
  submitted_at, completed_at`). This is the persistent ownership record — qBittorrent
  state is not consulted for ownership (the manager deletes completed torrents from
  qBittorrent; the files remain and stay owned).
- **Enforcement (pre-check at submit)**: projected usage =
  per-user on-disk counter + Σ sizes of that user's In-flight Downloads + new
  Download's size (Torznab/qBittorrent-reported). If projected > cap → reject with a
  clear "quota exceeded — delete something first" error; the Webui shows it inline and
  the Bot relays it to the Discord channel. No over-commit states are created.
- **Usage counter**: `users.quota_used_bytes`, incremented at submit, adjusted at
  completion if actual size differs, decremented on delete. Kept honest by a
  **reconciliation job** (manager, periodic) that recomputes each user's usage from
  their `downloads` records vs what still exists on disk, and corrects drift from
  out-of-band changes (manual deletions, qBittorrent/Plex side effects). Drift beyond
  a threshold is logged.
- **User media management (Webui "My Media" page)**: Download-level granularity.
  Users see their own completed Downloads with: delete (hard delete of the Download's
  files at `save_path`, drops the record, frees quota), rename (updates record + disk),
  move to another Category (updates record + disk, quota unchanged). In-flight
  Downloads appear with progress + cancel. All operations respect Protected Path
  guards (refuse while the torrent is still an Active Torrent — users cancel instead).
- **Admin interactions**: the Admin file manager operates on the whole Media Root.
  Deleting files inside an owned Download's path (partially or wholly) triggers a
  recompute of that user's usage on the next reconciliation tick, or immediately when
  a whole Download's `save_path` is removed. Admin quota edits (assign/override/
  unlimited) are audited Admin actions.

## Admin Panel (`/admin`, admin SPA, vanilla JS single-file like webui)

1. **Subsystems**: per Subsystem — container state, port/reachability probe, restart /
   stop / start buttons (docker.sock via API, label-filtered to this compose project,
   every action audited). Optional Subsystem (Bot) additionally has disable/enable.
2. **Users**: create Account, set Role, set quota (override/unlimited; default from
   `DEFAULT_QUOTA_GB`), reset password, disable, revoke sessions, view per-Account
   Link Code and current usage vs cap.
3. **Audit**: filterable ledger view.
4. **Files**: browse Media Root tree; delete (hard delete), rename, move. Operations
   refused on Protected Paths (inside any Active Torrent's save path; manager checks
   qBittorrent before executing). Sandboxed to Media Root — path traversal rejected.
   Manager owns these endpoints and the media volume; API enforces Admin role and
   writes ledger rows before forwarding.
5. **VPN**: edit provider, credentials, server (writes to `.env`/openvpn config), test
   (public exit IP check from a netns-sharing container vs expected VPN egress),
   restart gluetun to apply.

## Webui user pages

- **Search / download** (existing flows, now session-authenticated and quota-checked).
- **My Media**: the Account's Downloads — in-flight with progress + cancel; completed
  with delete / rename / move-to-Category, quota usage vs cap displayed, and an inline
  "quota exceeded" state on the search page when the pre-check would reject.

## API surface (target)

```
POST /auth/login | POST /auth/logout | POST /auth/password
GET  /search | POST /download | GET /progress | POST /cancel   (user; proxied to manager)
GET  /media | GET /me/quota                                     (own Downloads + usage/cap)
POST /media/{id}/delete|rename|move                             (own Downloads, proxied to manager)
POST /admin/users | POST /admin/users/{id}/reset | POST /admin/users/{id}/disable
POST /admin/users/{id}/quota
POST /admin/services/{name}/restart|stop|start | GET /admin/services
GET  /admin/audit | GET /admin/files?path= | POST /admin/files/delete|rename|move
GET  /admin/vpn/status | POST /admin/vpn/config | POST /admin/vpn/test
POST /internal/events          (manager → API, token)
POST /internal/webhook/completion (manager → Bot, token; bot-side endpoint)
```

## Behavior changes to flag

- **Downloads now have owners and quotas**: submission pre-checks projected usage
  (on-disk + In-flight) against the Account's cap; over-cap submissions are rejected
  with a user-visible message (Webui inline, Bot relays to Discord). Admin submissions
  bypass the check.
- **Cancel is scoped**: `POST /cancel` cancels only torrents the calling Actor
  submitted (manager already hash-scopes ownership). Admin may cancel all from the
  panel. The old cancel-everything-for-everyone behavior is intentionally gone.
- **Search lives in the manager** now; the API is a proxy and never talks to Jackett.
- **`bitbeard.log`** plain file is replaced by the Audit Ledger + per-service stdout
  logging (docker logs). No structured file logging in v1.
- `DiscordAnnouncer` becomes the manager's webhook emitter; no in-process announcer.

## Out of scope (v1)

- Trash/soft-delete for file operations (user chose hard delete).
- File-level user media management — users manage whole Downloads; the file tree with
  per-file ops stays admin-only.
- Linked-Discord auto-registration; account creation stays Admin-only.
- Per-service egress diagnostics beyond the VPN test (qBittorrent-specific leak checks).
- Ledger retention/purge policy.
