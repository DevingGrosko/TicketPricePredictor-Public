"""Start with gunicorn Flask_App.staging_wsgi:app, never the production entrypoint."""
from Flask_App.staging_site import create_app

app = create_app()
