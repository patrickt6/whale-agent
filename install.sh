#!/bin/sh
#
# Install Whale Agent:
#
#   curl -fsSL https://whale-agent.com/install | sh
#
# Default mode ("tool"): installs uv if it is missing, then `uv tool install whale-agent`.
# Running it again upgrades the tool. No sudo, nothing outside your home directory.
#
#   WHALE_INSTALL_SOURCE  what uv installs (default: whale-agent from PyPI). Before the
#                         PyPI release use git+https://github.com/patrickt6/whale-agent.git
#   WHALE_INSTALL_MODE    "source" clones the repo into a .venv instead (old behaviour)
#   WHALE_UV_INSTALLER    uv install script URL (default https://astral.sh/uv/install.sh)
#
# Source mode only:
#   WHALE_HOME            where to clone (default ~/whale-agent)
#   WHALE_REPO_URL        what to clone (default https://github.com/patrickt6/whale-agent.git)
#   WHALE_SKIP_PIP        1 skips the pip install (the tests run offline)

set -eu

say() { printf '%s\n' "$*"; }
die() { printf 'install: %s\n' "$*" >&2; exit 1; }
has() { command -v "$1" >/dev/null 2>&1; }

install_source() {
    WHALE_HOME="${WHALE_HOME:-$HOME/whale-agent}"
    WHALE_REPO_URL="${WHALE_REPO_URL:-https://github.com/patrickt6/whale-agent.git}"
    has git || die "git is required. Install git, then run this again."

    PYTHON=""
    for candidate in python3.13 python3.12 python3.11 python3; do
        if has "$candidate" &&
            "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
            PYTHON="$candidate"
            break
        fi
    done
    [ -n "$PYTHON" ] || die "Python 3.11 or newer is required (python3 --version)."

    if [ -d "$WHALE_HOME/.git" ]; then
        say "Updating $WHALE_HOME"
        git -C "$WHALE_HOME" pull --ff-only
    elif [ -e "$WHALE_HOME" ]; then
        die "$WHALE_HOME exists and is not a git clone. Move it, or set WHALE_HOME."
    else
        say "Cloning into $WHALE_HOME"
        git clone "$WHALE_REPO_URL" "$WHALE_HOME"
    fi

    cd "$WHALE_HOME"
    if [ ! -x .venv/bin/python ]; then
        say "Creating .venv"
        "$PYTHON" -m venv .venv
    fi
    if [ "${WHALE_SKIP_PIP:-0}" != "1" ]; then
        .venv/bin/python -m pip install --quiet --upgrade pip
        .venv/bin/python -m pip install --quiet -e .
    fi

    if [ ! -f .env ] && [ -f .env.example ]; then
        # Personal values in the example (emails, contact strings) are blanked.
        sed -E \
            -e 's/^(WHALE_EMAIL_TO|WHALE_EMAIL_FROM|SMTP_USERNAME|OPERATOR_ALERT_EMAIL)=("[^"]*"|[^ #]*)/\1=/' \
            -e 's/^([A-Z_]*USER_AGENT)=("[^"]*"|[^ #]*)/\1=/' \
            -e 's/^([A-Z_]+)=("[^"]*@[^"]*"|[^ #]*@[^ #]*)/\1=/' \
            .env.example > .env
        chmod 600 .env
        say "Wrote .env (blank; fill it in with ./whale settings)"
    fi
    chmod +x whale 2>/dev/null || true

    say ""
    say "Whale Agent is installed."
    say "Next:  cd $WHALE_HOME && ./whale"
}

install_tool() {
    os="$(uname -s)"
    arch="$(uname -m)"
    case "$os" in
        Darwin | Linux) ;;
        *) die "unsupported system: $os. On Windows use WSL, or: pipx install whale-agent" ;;
    esac
    say "Installing Whale Agent ($os $arch)"

    bin_dir="${XDG_BIN_HOME:-$HOME/.local/bin}"
    UV=""
    if has uv; then
        UV="$(command -v uv)"
    elif [ -x "$bin_dir/uv" ]; then
        UV="$bin_dir/uv"
    else
        installer="${WHALE_UV_INSTALLER:-https://astral.sh/uv/install.sh}"
        say "uv not found; installing it from $installer"
        if has curl; then
            curl -LsSf "$installer" | sh
        elif has wget; then
            wget -qO- "$installer" | sh
        else
            die "need curl or wget to install uv"
        fi
        for candidate in "$bin_dir/uv" "$HOME/.local/bin/uv" "$HOME/.cargo/bin/uv"; do
            if [ -x "$candidate" ]; then
                UV="$candidate"
                break
            fi
        done
        [ -n "$UV" ] || die "uv installed but not found in $bin_dir; open a new shell and run this again"
    fi

    source_spec="${WHALE_INSTALL_SOURCE:-whale-agent}"
    if "$UV" tool list 2>/dev/null | grep -q '^whale-agent '; then
        say "Whale Agent is already installed; upgrading"
        "$UV" tool upgrade whale-agent
    else
        "$UV" tool install "$source_spec"
    fi

    tool_bin="$("$UV" tool dir --bin 2>/dev/null || printf '%s' "$bin_dir")"
    case ":$PATH:" in
        *":$tool_bin:"*) ;;
        *)
            say ""
            say "$tool_bin is not on your PATH. Add this line to your shell profile"
            say "(~/.zshrc or ~/.bashrc), then open a new terminal:"
            say ""
            say "  export PATH=\"$tool_bin:\$PATH\""
            ;;
    esac

    say ""
    say "Whale Agent is installed."
    say "Run: whale"
}

case "${WHALE_INSTALL_MODE:-tool}" in
    tool) install_tool ;;
    source) install_source ;;
    *) die "WHALE_INSTALL_MODE must be tool or source, not ${WHALE_INSTALL_MODE}" ;;
esac
