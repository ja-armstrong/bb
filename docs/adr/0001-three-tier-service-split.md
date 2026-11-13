# Three-tier service split: Frontends → API → Download Manager

Bitbeard's Discord bot, HTTP API, and download lifecycle all ran as one process sharing
module-level globals. We split them into three tiers: thin Frontends (Bot, Webui) talk
over HTTP to the API, and the API proxies media work to a dedicated Download Manager
service that owns the torrent lifecycle, the qBittorrent client, search, and file
operations. The manager emits Lifecycle Events as webhooks: to the Bot for Discord
announcement, and to the API for the Audit Ledger. The user chose the deeper split over
"bot as HTTP client" so all media-facing logic lives in exactly one service, and frontends
stay thin and replaceable.

## Considered options

- Bot as plain HTTP client of the existing API (lighter, but keeps media logic entangled
  with the API process)
- Monolith unchanged (rejected: the whole point of the effort)

## Consequences

- The manager's completion flow needs webhooks instead of injected `Announcer` objects.
- The API↔manager hop needs service auth (see ADR-0002).
- The manager and API both mount the media volume, but only the manager writes to it.
