#!/usr/bin/env sh

set -eu

info() { printf '%s\n' "[INFO] $*"; }
fail() { printf '%s\n' "[ERROR] $*" >&2; exit 1; }

printf '%s\n' \
  '  ____  ____  _____ _   _ ____  _____ _   _  ____  _   _ ' \
  ' / __ \/ __ \/ ____| \ | |  _ \| ____| \ | |/ ___|| | | |' \
  '| |  | | |  | | |    |  \| | |_) |  _| |  \| | |    | | | |' \
  '| |__| | |__| | |____| |\  |  __/| |___| |\  | |___ | |_| |' \
  ' \____/\____/ \_____|_| \_|_|   |_____|_| \_|\____| \___/ '
printf '%s\n\n' 'OpenKyrozen computer-native installer'

[ -n "${HOME:-}" ] || fail 'HOME is not set.'

os_name="$(uname -s 2>/dev/null || true)"
case "$os_name" in
    Darwin) platform='macOS' ;;
    Linux) platform='Linux' ;;
    *) fail "Unsupported operating system: $os_name. Use install.ps1 on Windows." ;;
esac

arch="$(uname -m 2>/dev/null || true)"
case "$arch" in
    x86_64|amd64|arm64|aarch64) ;;
    *) fail "Unsupported architecture: $arch" ;;
esac

command -v mkdir >/dev/null 2>&1 || fail 'mkdir is required.'
command -v curl >/dev/null 2>&1 || fail 'curl is required to bootstrap uv.'
[ -w "$HOME" ] || fail "User home is not writable: $HOME"
if ! curl -fsS --max-time 10 https://pypi.org/ >/dev/null; then
    fail 'Network access to PyPI is required to install OpenKyrozen.'
fi

state_dir="$HOME/.kyrozen"
if ! mkdir -p "$state_dir/workspace" "$state_dir/v2"; then
    fail "Could not create writable OpenKyrozen state directories under $state_dir"
fi
chmod 700 "$state_dir" "$state_dir/workspace" "$state_dir/v2" 2>/dev/null || true

PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
export PATH

uv_bin="$(command -v uv 2>/dev/null || true)"
if [ -z "$uv_bin" ]; then
    info "Installing uv for $platform/$arch..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
    export PATH
    uv_bin="$(command -v uv 2>/dev/null || true)"
fi
[ -n "$uv_bin" ] || fail 'uv was not found after installation. Restart the shell and run this installer again.'

python_version=''
if "$uv_bin" python find 3.12 >/dev/null 2>&1; then
    python_version='3.12'
elif "$uv_bin" python find 3.13 >/dev/null 2>&1; then
    python_version='3.13'
else
    info 'Installing supported Python 3.12...'
    if "$uv_bin" python install 3.12 >/dev/null 2>&1; then
        python_version='3.12'
    elif "$uv_bin" python install 3.13 >/dev/null 2>&1; then
        python_version='3.13'
    else
        fail 'Could not install Python 3.12 or 3.13.'
    fi
fi

info "Installing OpenKyrozen from PyPI with Python $python_version..."
"$uv_bin" tool install --python "$python_version" --upgrade 'openkyrozen[web]'
"$uv_bin" tool update-shell >/dev/null 2>&1 || true

profile=''
case "${SHELL:-}" in
    */zsh) profile="${ZDOTDIR:-$HOME}/.zshrc" ;;
    */bash) profile="$HOME/.bashrc" ;;
esac
if [ -n "$profile" ] && { [ ! -e "$profile" ] || [ -w "$profile" ]; }; then
    if ! grep -Fq '$HOME/.local/bin' "$profile" 2>/dev/null; then
        printf '\n# OpenKyrozen user tools\nexport PATH="$HOME/.local/bin:$PATH"\n' >> "$profile" 2>/dev/null || true
    fi
fi

kyrozen_bin="$(command -v kyrozen 2>/dev/null || true)"
[ -n "$kyrozen_bin" ] || [ -x "$HOME/.local/bin/kyrozen" ] || fail 'kyrozen was installed but is not on PATH.'
if [ -z "$kyrozen_bin" ]; then
    kyrozen_bin="$HOME/.local/bin/kyrozen"
fi

info 'Verifying the installation...'
"$kyrozen_bin" --version
"$kyrozen_bin" --help >/dev/null

printf '%s\n' '' 'Installation complete.' \
  '  kyrozen                 Start in the global workspace (~/.kyrozen/workspace)' \
  '  kyrozen --project .     Work directly in the current project' \
  '  kyrozen-web             Start the local web server' \
  '' 'Restart your shell if the kyrozen command is not yet available.' \
  'The first kyrozen launch will guide you through provider setup; this installer never handles API keys.'
