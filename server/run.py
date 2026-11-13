# ASGI entry point: uvicorn run:app --app-dir ../Server/server from ongoing_work.
# Importing this module creates the configured app/database and can bootstrap an empty account table.
# Set DATABASE_URL and initial ADMIN_* deliberately; see docs/deployment.md.

from app.main import create_app
app=create_app()
