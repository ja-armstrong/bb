# Bitbeard

Bitbeard is a self-hosted media automation stack: a Discord bot and web frontend for
searching torrent indexers and submitting downloads, a Download Manager driving
qBittorrent, and an Admin Panel for users, subsystems, logs, and media files.
See [`bitbeard/README.md`](bitbeard/README.md) for the stack itself.

## Installing the Bootstrap binary

`bitbeard` is the Bootstrap binary: the deployment entry point for the stack. A fresh
machine needs nothing but the binary.

```sh
curl -fsSL https://raw.githubusercontent.com/ja-armstrong/bb/main/install.sh | sh
```

The installer detects your architecture (linux amd64 / arm64), fetches the latest
release artifact, and installs it to `~/.local/bin/bitbeard`.

## Manual install

If the one-liner does not suit you, download the binary by hand from the
[Releases page](https://github.com/ja-armstrong/bb/releases):

1. Download `bitbeard-linux-amd64` (x86_64) or `bitbeard-linux-arm64` (aarch64/ARM)
   for the latest release.
2. Install it:

   ```sh
   chmod +x bitbeard-linux-amd64
   mkdir -p ~/.local/bin
   mv bitbeard-linux-amd64 ~/.local/bin/bitbeard
   ```

3. Make sure `~/.local/bin` is on your `PATH`, e.g.
   `echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.profile` (then start a new shell).

## Verify

```sh
bitbeard --version   # prints the version and commit
bitbeard help        # lists setup, doctor and update
```

## Commands

- `bitbeard setup` — first run and any rerun: installs prerequisites, fetches the stack
  to `~/bitbeard`, asks for every key, boots, and verifies. Skipped optional
  Subsystems (the Bot, a Media Server) can be enabled later by rerunning it.
- `bitbeard doctor` — check-only: prerequisites, `.env` guardrails, stack health.
- `bitbeard update` — pulls the stack, rebuilds, restarts, and runs doctor.

## Releasing

Tagging a commit `v…` (e.g. `v0.1.0`) and pushing the tag triggers
[`.github/workflows/release.yml`](.github/workflows/release.yml), which builds
`bitbeard-linux-amd64` and `bitbeard-linux-arm64` and publishes them to a GitHub
Release for that tag.
