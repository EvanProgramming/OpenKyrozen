export function detectInstallPlatform(hints = {}) {
  const platform = [hints.userAgentDataPlatform, hints.platform, hints.userAgent]
    .filter(Boolean)
    .join(" ")
    .toLowerCase();
  return /windows|win32|win64/.test(platform) ? "windows" : "unix";
}

export function installCommand(platform) {
  return platform === "windows"
    ? "irm https://raw.githubusercontent.com/EvanProgramming/OpenKyrozen/v2.0.5/install.ps1 | iex"
    : "curl -fsSL https://raw.githubusercontent.com/EvanProgramming/OpenKyrozen/v2.0.5/install.sh | sh";
}
