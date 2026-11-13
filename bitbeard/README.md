# Bitbeard

Bitbeard is a self-hosted media automation stack: a Discord bot and web frontend for
searching torrent indexers and submitting downloads, a Download Manager driving
qBittorrent, and an admin panel for users, subsystems, logs, and media files.

The stack is split into four services plus the supporting VPN, torrent, indexer and
media-server containers:

| Service | Role | Port (internal) |
|---|---|---|
| `api` | Identity, Sessions, Audit Ledger, Admin Panel API | 9870 |
| `manager` | Torrent lifecycle, qBittorrent, search, Media Root file ops | 9871 |
| `bot` | Discord Frontend and Completion Event webhook | 9872 |
| `webui` | Static Webui and Admin Panel, proxying `/api` to the API | 80 (host 8081) |

The Webui is published at `localhost:8081` through gluetun. It shows a login page
when unauthenticated; the API issues HTTP-only Session cookies (expiry from
`SESSION_TTL_HOURS`). The API, Download Manager
and Bot stay internal to the shared gluetun network namespace.

## Quick setup

1. **Environment configuration**
   - Copy `.env.example` to `.env`
   - Set `ADMIN_PASSWORD` and `SERVICE_TOKEN` (and the rest of your values)
   - Put your VPN config file in `./openvpn`; NordVPN is included by default

2. **Start services**
   ```bash
   docker compose up -d
   ```

3. **Configure services**
   - **Jackett**: visit `http://localhost:9117` to add trackers
   - **Plex**: visit `http://localhost:32400` to complete setup

## First boot

The API creates `data/bitbeard.db` on the shared volume with the `users`, `sessions`,
`audit`, `downloads` and `link_codes` tables. When no Account exists yet, it seeds the
bootstrap Admin from `ADMIN_USER` / `ADMIN_PASSWORD`. Later restarts leave the store
untouched.

## Using the Bot

The Bot is a thin Frontend: it authenticates to the API as a Service Account and
relays the acting Discord user on every call. Send it a message in Discord:

- **any text** — a Search through the API; results page 25 at a time
  (Prev/Next), and picking one asks for a Category before submitting
- **a pasted `magnet:?` link** — asks for a Category, then submits
- **`progress`** — your In-flight Downloads with live progress and ETA
- **`cancel`** — pick one of your In-flight Downloads to cancel
  (cancelling is scoped to your own Downloads)

Quota rejections from the API are relayed verbatim to the channel. To link your
Discord ID to your Account, generate a Link Code in the Webui ("Link Discord",
with your Discord user ID), then send any message to the Bot from Discord — the
next interaction verifies and spends the code.

## Development

```bash
pip install -r requirements-dev.txt
python -m pytest tests
```

## Security warnings

**Bitbeard has some config defaults for Jackett and qBittorrent. They can be exploited
if you port forward for remote management.**

- **qBittorrent**: if you expose port 8080 externally, **change the default password**.
- **Jackett**: if you expose port 9117 externally, **change the API key**.
