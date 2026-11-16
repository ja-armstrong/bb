#!/bin/sh
# Installer for the bitbeard Bootstrap binary.
#
# Documented install path:
#
#   curl -fsSL https://raw.githubusercontent.com/ja-armstrong/bb/main/install.sh | sh
#
# Detects the machine architecture, fetches the latest release artifact for it,
# and installs to ~/.local/bin/bitbeard.
set -eu

REPO="ja-armstrong/bb"
BIN_DIR="${HOME}/.local/bin"

# USER-FACING
say() { printf '%s\n' "$1"; }
# USER-FACING
warn() { printf 'bitbeard-install: %s\n' "$1" >&2; }
# USER-FACING
die() { printf 'bitbeard-install: %s\n' "$1" >&2; exit 1; }

# Map uname -m to the release artifact's arch suffix.
arch="$(uname -m 2>/dev/null || true)"
case "${arch}" in
  x86_64) arch=amd64 ;;
  aarch64 | arm64) arch=arm64 ;;
  # USER-FACING
  *) die "unsupported architecture '${arch}': bitbeard ships linux amd64 and arm64 binaries (see the Releases page for manual options)" ;;
esac

# Resolve the latest release tag without requiring jq.
api_url="https://api.github.com/repos/${REPO}/releases/latest"
tag="$(curl -fsSL "${api_url}" | sed -n 's/.*"tag_name"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' || true)"
# USER-FACING
[ -n "${tag}" ] || die "could not determine the latest release (check your network, or download the binary manually from https://github.com/${REPO}/releases)"

asset_url="https://github.com/${REPO}/releases/download/${tag}/bitbeard-linux-${arch}"

# USER-FACING
say "Installing bitbeard ${tag} for linux/${arch}..."

tmpdir="$(mktemp -d)"
trap 'rm -rf "${tmpdir}"' EXIT

if ! curl -fsSL -o "${tmpdir}/bitbeard" "${asset_url}"; then
  # USER-FACING
  die "download failed: ${asset_url} (check https://github.com/${REPO}/releases for available artifacts)"
fi

chmod +x "${tmpdir}/bitbeard"
mkdir -p "${BIN_DIR}"
mv -f "${tmpdir}/bitbeard" "${BIN_DIR}/bitbeard"

# USER-FACING
say "Installed ${BIN_DIR}/bitbeard (${tag}, linux/${arch})."

# Warn when the install location is not on PATH.
case ":${PATH}:" in
  *":${BIN_DIR}:"*) ;;
  # USER-FACING
  *)
    warn "${BIN_DIR} is not on your PATH."
    # USER-FACING
    warn "Add it, e.g.:  echo 'export PATH=\"\$HOME/.local/bin:\$PATH\"' >> ~/.profile  (then start a new shell)"
    ;;
esac

# USER-FACING
say "Next: run 'bitbeard --version' to verify, then 'bitbeard setup' to bootstrap the stack."
