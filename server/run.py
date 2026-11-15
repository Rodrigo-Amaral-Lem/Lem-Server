# SPDX-License-Identifier: GPL-3.0-only
# Part of LEM. See the accompanying LICENSE for license terms and warranty disclaimer.

# ASGI entry point: python -m uvicorn run:app --app-dir server from the Server repository root.
# Importing this module creates the configured app/database and can bootstrap an empty account table.
# Set DATABASE_URL and initial ADMIN_* deliberately; see docs/deployment.md.

from app.main import create_app
app=create_app()
