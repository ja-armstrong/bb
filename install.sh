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

# USER-FACING
# Ask a yes/no question on the controlling terminal (/dev/tty), because stdin
# belongs to the curl pipe when the installer is piped to sh. Returns 0 for
# yes; returns non-zero for no, EOF, or a missing terminal — in which case
# nothing was prompted and nothing should be changed (CI-safe).
ask_tty() {
  # Prompt and read on the controlling terminal only. A failed /dev/tty open
  # (no terminal attached) is silenced and treated as "no": nothing prompted,
  # nothing changed (CI-safe).
  # USER-FACING
  if { printf '%s ' "$1" > "/dev/tty" && read tty_answer < "/dev/tty"; } 2>/dev/null; then
    case "${tty_answer}" in
      [yY] | [yY][eE][sS]) return 0 ;;
      *) return 1 ;;
    esac
  fi
  return 1
}

# USER-FACING
# Append the PATH line to an rc file exactly once (grep first, never
# duplicate). Returns 0 when the file was changed, 1 when the entry was
# already present and nothing was touched.
add_path_line() {
  rc_file="$1"
  if grep -qF '.local/bin' "${rc_file}" 2>/dev/null; then
    # USER-FACING
    say "PATH entry already present in ${rc_file}"
    return 1
  fi
  mkdir -p "$(dirname "${rc_file}")"
  # If the target exists but has no trailing newline, start our line on a
  # fresh one so the operator's last line is not glued to the PATH export.
  if [ -f "${rc_file}" ] && [ -s "${rc_file}" ]; then
    if [ -n "$(tail -c 1 "${rc_file}")" ]; then
      printf '\n' >> "${rc_file}"
    fi
  fi
  printf '%s\n' "${path_line}" >> "${rc_file}"
  # USER-FACING
  say "Added to ${rc_file}:"
  # USER-FACING
  say "  ${path_line}"
  return 0
}

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

# Offer PATH help only when the install location is not already on PATH.
case ":${PATH}:" in
  *":${BIN_DIR}:"*) ;;
  *)
    # The binary was installed by this run, so a PATH fix here also unlocks
    # the setup seeding offer below.
    # USER-FACING
    if ask_tty "Add ~/.local/bin to your PATH? [y/N]"; then
      shell_name="$(basename "${SHELL:-}" 2>/dev/null || true)"
      # USER-FACING
      path_line='export PATH="$HOME/.local/bin:$PATH"'
      changed=0
      case "${shell_name}" in
        zsh)
          add_path_line "${HOME}/.zshrc" && changed=1
          ;;
        fish)
          # fish: delegate to the fish config with its own canonical helper.
          # USER-FACING
          path_line='fish_add_path ~/.local/bin'
          add_path_line "${HOME}/.config/fish/config.fish" && changed=1
          ;;
        *)
          if [ -f "${HOME}/.profile" ]; then
            add_path_line "${HOME}/.profile" && changed=1
          else
            add_path_line "${HOME}/.bashrc" && changed=1
          fi
          ;;
      esac

      if [ "${changed}" -eq 1 ]; then
        # USER-FACING
        if ask_tty "Run bitbeard setup now? [y/N]"; then
          # The -c shell exits when setup finishes, returning control to the
          # operator's original shell. PATH is exported explicitly because
          # e.g. zsh -l -c does not source .zshrc; do not rely on rc files.
          rm -rf "${tmpdir}"
          if [ -n "${SHELL:-}" ]; then
            exec "${SHELL}" -l -c 'export PATH="$HOME/.local/bin:$PATH"; bitbeard setup'
          else
            exec /bin/bash -l -c 'export PATH="$HOME/.local/bin:$PATH"; bitbeard setup'
          fi
        fi
        # USER-FACING
        if ask_tty "Drop into a new shell now? [y/N]"; then
          rm -rf "${tmpdir}"
          if [ -n "${SHELL:-}" ]; then
            exec "${SHELL}" -l
          else
            exec /bin/bash -l
          fi
        fi
        # USER-FACING
        say "Open a new shell to pick up the PATH change, then run 'bitbeard setup'."
      fi
    else
      # USER-FACING
      warn "${BIN_DIR} is not on your PATH."
      # USER-FACING
      warn "Add it, e.g.:  echo 'export PATH=\"\$HOME/.local/bin:\$PATH\"' >> ~/.profile  (then start a new shell)"
    fi
    ;;
esac

# USER-FACING
say "Next: run 'bitbeard --version' to verify, then 'bitbeard setup' to bootstrap the stack."
