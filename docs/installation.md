# Install, update, and recover

## Requirements

The published v2.0.6 release supports Python 3.12 and 3.13. Python 3.14 is excluded by the package metadata. The official installers provision Python through `uv`; a source checkout needs Git and a supported Python. The optional Bubble Tea terminal UI is a Go binary distributed with the release. Docker uses Python 3.12. See the live [release page](https://github.com/EvanProgramming/OpenKyrozen/releases/latest) before copying a pinned command: installer examples below identify the current release as of 5 October 2026.

## Install the published release

On macOS or Linux:

```sh
curl -fsSL https://raw.githubusercontent.com/EvanProgramming/OpenKyrozen/v2.0.6/install.sh | sh
kyrozen --version
```

On Windows PowerShell:

```powershell
irm https://raw.githubusercontent.com/EvanProgramming/OpenKyrozen/v2.0.6/install.ps1 | iex
kyrozen --version
```

The installers download the pinned release and its matching terminal UI. Follow the installer output if it says that `~/.local/bin` must be added to `PATH`; open a new shell before checking the command again. On Windows, `install.ps1 -Help` prints the pinned release and prerequisite summary. Installer scripts install code and binaries; persistent agent state lives separately under `~/.kyrozen`.

For a direct Python install, add the optional web server dependencies explicitly:

```sh
uv tool install --python 3.12 --force --with fastapi --with uvicorn \
  https://github.com/EvanProgramming/OpenKyrozen/releases/download/v2.0.6/openkyrozen-2.0.6-py3-none-any.whl
kyrozen --version
```

This wheel path does not install the standalone Bubble Tea binary. A package install can use the Rich terminal interface, which also serves as a recovery interface when the TUI is unavailable.

## Run from a source checkout

```sh
git clone https://github.com/EvanProgramming/OpenKyrozen.git
cd OpenKyrozen
make install
make run
```

`make install` creates `venv/`, installs the project and optional integrations, and downloads Playwright Chromium for browser tests. `make install-core` installs the smaller web and Anthropic development environment without the complete browser extras. On Windows, use the supplied `setup.bat` and `run.bat`; the Makefile targets macOS, Linux, and WSL.

To run against a project directory, use `kyrozen --project /path/to/project` or `kyrozen-web --project /path/to/project`. Bare `kyrozen` uses the persistent global workspace. See [usage](usage.md) for workspace rules.

## Start the optional web interface

```sh
kyrozen-web --host 127.0.0.1 --port 8000
```

Open `http://127.0.0.1:8000`. To bind beyond loopback, set a non-empty `KYROZEN_SERVER_TOKEN` before starting the server and protect that secret. The server refuses unauthenticated non-loopback clients when no token is configured. See [security](security.md) and the [API guide](api.md).

## Configure a provider

Interactive onboarding lets you select a configured provider. Headless use reads provider credentials from its documented environment variable or the encrypted user configuration flow. For example:

```sh
export KYROZEN_PROVIDER=deepseek
export DEEPSEEK_API_KEY=your-key
kyrozen --project /path/to/project
```

The value shown is a shell placeholder; replace it locally and never commit a real key. See [providers](providers.md) for all integrations and [configuration](configuration.md) for model, agent, server, and data settings.

## Updates and recovery

For an installed release, type `/update` in the running agent. The updater selects one verified source revision for Python and the terminal binary, checks installed package provenance, and stages the replacement TUI before activation. Restart when prompted so the new process loads the installed version. Updates need network access and package dependencies; errors and partial outcomes include recovery instructions.

The published v2.0.6 one-line installers are pinned. The update command tracks the latest verified `main` revision, so it can install a development revision newer than the release version. If startup reports an incomplete update, exit and rerun the official installer. `kyrozen --help` is also intended to remain available for recovery. Historical Windows installations that predate the staged updater should exit the application and run the current official installer before updating.

## State and uninstalling

Agent state and configuration are held under `~/.kyrozen` and `~/.kyrozen_config.json`; the encrypted configuration file contains protected credentials. A source checkout also creates `venv/` and may create a rebuildable Chroma index. For Docker, mount `/data` as a persistent volume; the database and vector index are configured there.

Removing an installed command does not automatically remove user state. Before deleting state, stop all running instances and make a private backup. To remove state deliberately, remove the appropriate home-directory data only after checking that it contains nothing else you need. For database recovery or migration, read [memory and storage](memory-and-storage.md).
