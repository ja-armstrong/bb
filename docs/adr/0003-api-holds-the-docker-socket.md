# The API holds the docker socket

The Admin Panel can restart Subsystems and disable the Optional Subsystem (the Bot),
which requires authority over Docker. We mount `/var/run/docker.sock` into the API
service and expose admin-role-gated, Audit-Ledger-recorded control endpoints, restricted
to containers labeled as belonging to this compose project. No other service mounts the
socket, and the Admin Panel remains a pure frontend with no special privileges.

## Considered options

- A dedicated supervisor service owning the socket with a tiny REST surface — rejected:
  still root-equivalent, so the isolation gain is mostly cosmetic while doubling the
  number of privileged services to build and patch
- No in-panel docker control (status only, restart by hand on the host) — rejected by
  the user; one-click restarts are a core want

## Consequences

- The socket is root-equivalent on the host; that is an accepted tax of panel-driven
  restarts. Mitigations are label filtering, role gating, and auditing every action —
  not elimination.
- Docker actions appear in the Audit Ledger like any other admin action.
