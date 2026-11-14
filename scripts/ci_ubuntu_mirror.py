"""Use an available Ubuntu archive mirror in ephemeral Linux CI runners."""
from pathlib import Path
import re


MIRROR = "https://mirrors.kernel.org/ubuntu/"
UBUNTU_ARCHIVE = re.compile(
    r"(?<!\S)https?://(?:azure\.archive|archive|security)\.ubuntu\.com/ubuntu/?(?=\s|$)"
)


def configure_ubuntu_mirror(root: Path) -> None:
    found = False
    for relative in ("sources.list", "sources.list.d/ubuntu.sources", "apt-mirrors.txt"):
        path = root / relative
        if not path.is_file():
            continue
        before = path.read_text()
        after = UBUNTU_ARCHIVE.sub(MIRROR, before)
        found |= MIRROR in after
        if after != before:
            path.write_text(after)
    if not found:
        raise ValueError("No supported Ubuntu archive source found")


if __name__ == "__main__":
    configure_ubuntu_mirror(Path("/etc/apt"))
