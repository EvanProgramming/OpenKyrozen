"""ASGI factory; importing this module does not initialise the agent."""
from .service import WebService

def create_app(application=None):
    service = WebService(application)
    service.app.state.service = service
    return service.app

def main_entry():
    app.state.service.main_entry()

app = create_app()

if __name__ == "__main__":
    main_entry()
