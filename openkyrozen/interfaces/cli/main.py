"""Rich CLI entry point with lightweight help/version paths."""
from __future__ import annotations
import sys
from types import SimpleNamespace
from openkyrozen import __version__


def main():
    if sys.version_info >= (3, 14):
        raise SystemExit("OpenKyrozen requires Python 3.12 or 3.13.")
    if any(arg in sys.argv[1:] for arg in ("--help", "--version")):
        from .onboarding import _cli_parser
        _cli_parser(SimpleNamespace(__version__=__version__)).parse_args()
        return
    from openkyrozen.app.bootstrap import build_application
    application = build_application(surface="cli")
    try:
        application.runtime.main()
    finally:
        application.close()


if __name__ == "__main__":
    main()
