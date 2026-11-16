# Bitbeard behind a reverse proxy

Bitbeard's target deployment is always behind a reverse proxy (Caddy)
and always HTTPS. This page is the operator's one-stop setup for that
deployment, and the single place the proxy contract is written down.

```
browser ──TLS──> Caddy (edge) ──> Webui nginx (:8081) ──> API (:9870)
                                   Jellyfin (:8096) <── (subdomain)
```

## The X-Forwarded-For contract

The API reads the **first** entry of `X-Forwarded-For` (XFF) twice, for
things that only work if the value is the real client:

- the per-IP throttle on `POST /register` (anonymous flooders share a
  bucket by address);
- the Audit Ledger's `source_ip` on anonymous Registration Requests.

XFF is client-controllable: any HTTP client can put arbitrary IPs in
it. That is safe **only** if exactly one hop overwrites the header with
the client it actually saw. The contract, in one sentence:

> **The front proxy overwrites XFF with the real client address;
> everything behind it passes the incoming XFF through verbatim; the
> API reads the first (and, per the contract, only) entry.**

Each layer's job:

- **Caddy (edge)** — overwrites XFF by default. A client-supplied
  `X-Forwarded-For` is ignored unless you have told Caddy the sender is
  a trusted proxy (`trusted_proxies`). Do not add `trusted_proxies` on
  the edge unless a CDN or another proxy really sits in front of it —
  that re-opens the spoofing hole.
- **Webui nginx** — passes the incoming XFF through **verbatim**
  (`proxy_set_header X-Forwarded-For $http_x_forwarded_for;` in
  `webui/default.conf`). It must not append (`$proxy_add_x_forwarded_for`
  would give the client's fake IP a ride in front of the real one).
- **API** — reads the first entry (`client_ip` in `api/main.py`). With
  the contract held, that first entry is the only entry.

A Caddyfile implementing this deployment is committed at
[`docs/proxy/Caddyfile`](Caddyfile) — copy it, edit the two site names
and the two upstream ports for your host, reload Caddy.

## Secure session cookies and `PUBLIC_HTTPS`

The API's Session cookie (the login cookie) carries the `Secure` flag
when `PUBLIC_HTTPS=1` — the default in `.env.example` — or when the
request actually arrived over TLS (`X-Forwarded-Proto: https`, which
the proxies set). `Secure` cookies are only ever sent back over HTTPS,
which is what you want on a public deployment.

**The plain-HTTP footgun.** Browsers refuse to *send* Secure cookies
over plain HTTP. If you serve Bitbeard over plain HTTP (a LAN box with
no TLS, hitting `http://<host>:8081`) while `PUBLIC_HTTPS` is left at
its default `1`, login will *appear to succeed and then silently do
nothing*: the API sets a Secure cookie the browser stores but refuses
to send back on the next request, so every page reads as logged-out.

If you serve plain HTTP, set in `.env`:

```
PUBLIC_HTTPS=0
```

and restart the stack. That is the escape hatch; the HTTPS deployment
leaves the default alone. (`X-Forwarded-Proto: https` also enables
Secure on individual requests, so a deployment mis-set to `0` that is
in fact served over TLS still gets the right flag.)

## Jellyfin dashboard settings (set once)

Jellyfin derives its idea of "who is connecting" from the same
XFF chain, but only believes the header for proxies you name in its
dashboard. Open the Jellyfin dashboard once after first boot and set:

- **Networking → Known proxies**: the Caddy address(es) — e.g.
  `172.17.0.1` or the docker network address Caddy connects from, or
  the host's LAN IP. **Changing this requires a Jellyfin restart**
  (Dashboard → Shutdown, or `docker restart jellyfin`), which is easy
  to miss: until the restart, XFF-derived client IPs are not trusted
  and Jellyfin logs the proxy's own address everywhere.
- **Networking → Local network addresses**: the LAN range clients may
  count as "local", e.g. `192.168.1.0/24` (comma-separated for more
  than one). This drives the local-vs-remote distinction.
- **Networking → remote access switches** (*Allow remote connections*
  and *Allow remote access for this server* / per-user equivalents):
  these judgments are made per source IP, so with Known proxies unset
  every viewer looks like the proxy's address — either all-remote or
  all-local, never the truth.

Without these three, Jellyfin's logs, its rate limiting and its
per-user remote-access decisions all see the wrong IP.

## If Jellyfin is only reachable through the proxy

The default compose publishes Jellyfin on `0.0.0.0:8096`, so it is
reachable directly on the LAN. If you instead bind 8096 to localhost so
only the proxy serves it, set `JELLYFIN_URL` in `.env` to the public
URL (`https://jellyfin.example.com`) instead of the default
`http://localhost:8096` — anything that points people at Jellyfin
should hand them the URL that actually works.
