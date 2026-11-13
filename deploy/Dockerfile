# SPDX-License-Identifier: GPL-3.0-only
# Part of LEM. See the accompanying LICENSE for license terms and warranty disclaimer.

FROM python:3.12-slim
WORKDIR /srv
COPY server/requirements.txt ./requirements.txt
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir -r requirements.txt && useradd --create-home lem
COPY server /srv/server
COPY shared /srv/shared
USER lem
WORKDIR /srv/server
CMD ["uvicorn", "run:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--proxy-headers", "--forwarded-allow-ips", "*"]
