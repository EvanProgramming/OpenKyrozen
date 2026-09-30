"""OpenKyrozen source-checkout launch compatibility."""
from openkyrozen.interfaces.cli.diagnostics import main

if __name__ == "__main__":
    raise SystemExit(main())
