"""OpenKyrozen source-checkout launch compatibility."""
from openkyrozen.interfaces.web.app import app
from openkyrozen.interfaces.web.app import main_entry

if __name__ == "__main__":
    main_entry()
