# Sessions at the API; SQLite for identity and audit; service token for API↔manager

Nginx basic auth with a single shared credential was the only authentication in the
stack, and the FastAPI on 9870 was unauthenticated with wide-open CORS. We moved all
authentication into the API: server-side Sessions (SQLite-backed, HTTP-only cookies)
issued at login, with Roles (Admin/User) enforced at the API boundary. Nginx becomes a
dumb static/proxy layer. The API↔manager hop authenticates with a bearer token from
`.env`; the API forwards the acting Account as a trusted header on proxied calls so
actions stay attributable end to end. The Bot authenticates as a Service Account and
sends the acting Discord user's ID; the API resolves it to a linked Account for the
Audit Ledger. This is the project's first persistent state — user store and Audit
Ledger live in one SQLite database on a volume, with a bootstrap Admin seeded from env
on first boot.

## Considered options

- JWTs in localStorage (stateless but hard to revoke, XSS-sensitive)
- Per-user htpasswd at the nginx edge (no logout, credentials cached by browsers)
- A dedicated auth service (more moving parts than a self-hosted stack needs)
- Docker-network trust for API↔manager with no token (fragile to topology changes)

## Consequences

- The Bot needs a small HTTP listener to receive Completion Event webhooks, also
  token-authed.
- Linking a Discord ID to an Account happens via a one-time Link Code generated in the
  Webui.
