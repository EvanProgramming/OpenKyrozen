#!/usr/bin/env sh

set -eu

release_version='2.0.3'
release_tag="v$release_version"
go_version='1.27.1'
release_base_url="https://github.com/EvanProgramming/OpenKyrozen/releases/download/$release_tag"
release_wheel_url="$release_base_url/openkyrozen-$release_version-py3-none-any.whl"
tui_source_url="$release_base_url/openkyrozen-tui-$release_version.tar.gz"
tui_checksum_url="$tui_source_url.sha256"

interactive=0
if [ -t 1 ] && [ "${NO_COLOR:-}" != '1' ]; then
    interactive=1
fi
if [ "$interactive" -eq 1 ]; then
    cyan='\033[36m'; white='\033[97m'; muted='\033[90m'; green='\033[32m'; amber='\033[33m'; reset='\033[0m'
else
    cyan=''; white=''; muted=''; green=''; amber=''; reset=''
fi

info() { printf '%b%s%b %s\n' "$muted" '[INFO]' "$reset" "$*"; }
warn() { printf '%b%s%b %s\n' "$amber" '[WARN]' "$reset" "$*" >&2; }
fail() { printf '%b%s%b %s\n' "$amber" '[ERROR]' "$reset" "$*" >&2; exit 1; }

printf '%b%s%b\n' "$cyan" 'OPENKYROZEN' "$reset"
printf '%b%s%b\n\n' "$white" 'OpenKyrozen computer-native installer' "$reset"

[ -n "${HOME:-}" ] || fail 'HOME is not set.'

os_name="$(uname -s 2>/dev/null || true)"
case "$os_name" in
    Darwin) platform='macOS'; go_os='darwin' ;;
    Linux) platform='Linux'; go_os='linux' ;;
    *) fail "Unsupported operating system: $os_name. Use install.ps1 on Windows." ;;
esac

arch="$(uname -m 2>/dev/null || true)"
case "$arch" in
    x86_64|amd64) go_arch='amd64' ;;
    arm64|aarch64) go_arch='arm64' ;;
    *) fail "Unsupported architecture: $arch" ;;
esac

command -v mkdir >/dev/null 2>&1 || fail 'mkdir is required.'
command -v curl >/dev/null 2>&1 || fail 'curl is required to bootstrap uv.'
[ -w "$HOME" ] || fail "User home is not writable: $HOME"
if ! curl -fsSL --max-time 20 -o /dev/null "$release_wheel_url"; then
    fail "GitHub release asset is unavailable: $release_tag"
fi

state_dir="$HOME/.kyrozen"
if ! mkdir -p "$state_dir/workspace" "$state_dir/v2" "$state_dir/bin" "$state_dir/toolchains/go"; then
    fail "Could not create writable OpenKyrozen state directories under $state_dir"
fi
chmod 700 "$state_dir" "$state_dir/workspace" "$state_dir/v2" "$state_dir/bin" 2>/dev/null || true

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

info "Installing OpenKyrozen $release_tag with Python $python_version..."
install_openkyrozen() {
    "$uv_bin" "$@" tool install --python "$python_version" --force \
      --with fastapi --with uvicorn "$release_wheel_url"
}
if ! install_openkyrozen; then
    info 'uv cache was incomplete; retrying without the existing cache...'
    install_openkyrozen --no-cache
fi
"$uv_bin" tool update-shell >/dev/null 2>&1 || true

checksum() {
    if command -v sha256sum >/dev/null 2>&1; then
        sha256sum "$1" | awk '{print $1}'
    else
        shasum -a 256 "$1" | awk '{print $1}'
    fi
}

go_sha256=''
case "$go_os-$go_arch" in
    darwin-arm64) go_sha256='ee215d57e0ec269c60cc9ceca68e6bda321ba9ee5afe24f4b0988703c2d87d12' ;;
    darwin-amd64) go_sha256='8f8f52c6649542cf027bbc9b9c68d1ec042f9f34808a40413f0b8b3b66f3caa4' ;;
    linux-amd64) go_sha256='63d339f0da5ab53635a56f2490a7984dfe12dfcff22ad749f63edaf590168445' ;;
    linux-arm64) go_sha256='3450b45a3f9ee8568792736a5c5e70a1f2e9b36c35a8f74958c03e51d7d92bec' ;;
esac

go_is_compatible() {
    go_candidate="$1"
    go_text="$("$go_candidate" version 2>/dev/null || true)"
    go_major="$(printf '%s\n' "$go_text" | sed -n 's/.*go\([0-9][0-9]*\)\..*/\1/p')"
    go_minor="$(printf '%s\n' "$go_text" | sed -n 's/.*go[0-9][0-9]*\.\([0-9][0-9]*\).*/\1/p')"
    go_patch="$(printf '%s\n' "$go_text" | sed -n 's/.*go[0-9][0-9]*\.[0-9][0-9]*\.\([0-9][0-9]*\).*/\1/p')"
    go_patch="${go_patch:-0}"
    [ -n "$go_major" ] && [ -n "$go_minor" ] || return 1
    [ "$go_major" -gt 1 ] || {
        [ "$go_major" -eq 1 ] && {
            [ "$go_minor" -gt 25 ] || {
                [ "$go_minor" -eq 25 ] && [ "${go_patch:-0}" -ge 8 ]
            }
        }
    }
}

go_bin="$(command -v go 2>/dev/null || true)"
if [ -n "$go_bin" ] && go_is_compatible "$go_bin"; then
    info "Reusing compatible Go toolchain: $($go_bin version)"
else
    go_bin="$state_dir/toolchains/go/$go_version/bin/go"
    if [ ! -x "$go_bin" ]; then
        info "Installing Go $go_version locally for the terminal UI..."
        go_tmp="$(mktemp -d "$state_dir/.go-download.XXXXXX")"
        go_archive="$go_tmp/go$go_version.$go_os-$go_arch.tar.gz"
        go_url="https://go.dev/dl/go$go_version.$go_os-$go_arch.tar.gz"
        if ! curl -fsSL --retry 2 "$go_url" -o "$go_archive"; then
            warn 'Go download failed; the Rich fallback remains available.'
        elif [ "$(checksum "$go_archive")" != "$go_sha256" ]; then
            warn 'Go checksum verification failed; the terminal UI was not installed.'
        else
            mkdir -p "$go_tmp/extract"
            if tar -xzf "$go_archive" -C "$go_tmp/extract" && [ -x "$go_tmp/extract/go/bin/go" ]; then
                if [ ! -e "$state_dir/toolchains/go/$go_version" ]; then
                    mv "$go_tmp/extract/go" "$state_dir/toolchains/go/$go_version"
                    go_bin="$state_dir/toolchains/go/$go_version/bin/go"
                else
                    warn 'A Go toolchain directory already exists but is not usable; keeping it unchanged.'
                fi
            else
                warn 'Go archive extraction failed; the Rich fallback remains available.'
            fi
        fi
        rm -rf "$go_tmp"
    fi
fi

build_tui() {
    [ -x "$go_bin" ] || return 1
    tui_tmp="$(mktemp -d "$state_dir/.tui-build.XXXXXX")"
    source_archive="$tui_tmp/openkyrozen-tui.tar.gz"
    source_checksum="$tui_tmp/openkyrozen-tui.tar.gz.sha256"
    build_output="$tui_tmp/openkyrozen-tui"
    if ! curl -fsSL --retry 2 "$tui_source_url" -o "$source_archive" || \
       ! curl -fsSL --retry 2 "$tui_checksum_url" -o "$source_checksum"; then
        rm -rf "$tui_tmp"
        return 1
    fi
    expected_checksum="$(awk '{print $1}' "$source_checksum")"
    if [ -z "$expected_checksum" ] || [ "$(checksum "$source_archive")" != "$expected_checksum" ]; then
        rm -rf "$tui_tmp"
        return 1
    fi
    mkdir "$tui_tmp/source"
    if ! tar -xzf "$source_archive" -C "$tui_tmp/source"; then
        rm -rf "$tui_tmp"
        return 1
    fi
    if ! (cd "$tui_tmp/source" && "$go_bin" build -trimpath -ldflags "-s -w -X main.version=$release_version" -o "$build_output"); then
        rm -rf "$tui_tmp"
        return 1
    fi
    chmod 700 "$build_output"
    # Replace the installed binary only after a complete successful build.
    mv "$build_output" "$state_dir/bin/openkyrozen-tui"
    rm -rf "$tui_tmp"
}

if build_tui; then
    info 'Bubble Tea terminal UI installed.'
else
    warn 'Bubble Tea source asset is unavailable or failed to build; kyrozen will use the Rich fallback.'
fi

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
[ -n "$kyrozen_bin" ] || kyrozen_bin="$HOME/.local/bin/kyrozen"

info 'Verifying the installation...'
installed_version="$($kyrozen_bin --version)"
printf '%s\n' "$installed_version"
case "$installed_version" in
    *"OpenKyrozen $release_version"*) ;;
    *) fail "Installed version does not match GitHub release $release_tag: $installed_version" ;;
esac
"$kyrozen_bin" --help >/dev/null

printf '%s\n' '' 'Installation complete.' \
  '  kyrozen                 Start the full-screen terminal UI' \
  '  kyrozen --project .     Work directly in the current project' \
  '  kyrozen-web             Start the local web server' \
  '' 'Restart your shell if the kyrozen command is not yet available.' \
  'The first kyrozen launch will guide you through provider setup; this installer never handles API keys.'
