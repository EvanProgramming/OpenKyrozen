"""Configure APT's signed Ubuntu archive mirror list on ephemeral CI runners."""
from pathlib import Path
import re


MIRRORS = (
    "https://archive.ubuntu.com/ubuntu/",
    "https://mirrors.kernel.org/ubuntu/",
    "https://azure.archive.ubuntu.com/ubuntu/",
)
UBUNTU_ARCHIVE = re.compile(
    r"(?<!\S)https?://(?:(?:azure\.archive|archive|security)\.ubuntu\.com"
    r"|mirrors(?:\.edge)?\.kernel\.org)/ubuntu/?(?=\s|$)"
)
RUNNER_MIRROR_SOURCE = re.compile(r"(?<!\S)mirror\+file:/etc/apt/apt-mirrors\.txt(?=\s|$)")


def configure_ubuntu_mirror(root: Path) -> None:
    mirrorlist = root / "openkyrozen-ubuntu-mirrors.list"
    source = f"mirror+file:{mirrorlist}"
    prior = root / "apt-mirrors.txt"
    known_runner_list = prior.is_file() and UBUNTU_ARCHIVE.search(prior.read_text()) is not None
    duplicate_sources = re.compile(rf"{re.escape(source)}(?:[ \t]+{re.escape(source)})+")
    changes = {}
    found = False
    for relative in ("sources.list", "sources.list.d/ubuntu.sources"):
        path = root / relative
        if not path.is_file():
            continue
        before = path.read_text()
        lines = []
        for line in before.splitlines(keepends=True):
            if not line.lstrip().startswith("#"):
                line = UBUNTU_ARCHIVE.sub(source, line)
                if known_runner_list:
                    line = RUNNER_MIRROR_SOURCE.sub(source, line)
                line = duplicate_sources.sub(source, line)
                found |= source in line.split()
            lines.append(line)
        after = "".join(lines)
        if after != before:
            changes[path] = after
    if not found:
        raise ValueError("No supported Ubuntu archive source found")
    entries = "".join(f"{url}\tpriority:{index}\n" for index, url in enumerate(MIRRORS, 1))
    if not mirrorlist.is_file() or mirrorlist.read_text() != entries:
        mirrorlist.write_text(entries)
    for path, after in changes.items():
        path.write_text(after)


if __name__ == "__main__":
    configure_ubuntu_mirror(Path("/etc/apt"))
