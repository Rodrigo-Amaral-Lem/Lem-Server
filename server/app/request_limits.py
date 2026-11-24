# SPDX-License-Identifier: GPL-3.0-only
# Part of LEM. See the accompanying LICENSE for license terms and warranty disclaimer.

"""Bound bodies before JSON/multipart parsing, including streamed requests."""

import asyncio
import re

from fastapi import HTTPException
from starlette.responses import JSONResponse


EVIDENCE_UPLOAD = re.compile(r"/api/reports/[^/]+/photos/[^/]+")


class RequestBodyLimit:
    def __init__(self, app, upload_slots, client_policy):
        self.app = app
        self.upload_slots = upload_slots
        self.client_policy = client_policy

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = dict(scope.get("headers", []))
        multipart = headers.get(b"content-type", b"").lower().startswith(b"multipart/form-data")
        limit = 61 * 1024 * 1024 if multipart else 512 * 1024
        if scope.get("path") == "/api/login":
            limit = 4096
        length = headers.get(b"content-length")
        if length is not None:
            try:
                size = int(length)
                if size < 0:
                    raise ValueError()
            except ValueError:
                return await JSONResponse({"detail": "Content-Length inválido"}, status_code=400)(
                    scope, receive, send
                )
            if size > limit:
                return await JSONResponse({"detail": "Pedido demasiado grande"}, status_code=413)(
                    scope, receive, send
                )
        evidence = scope.get("method") == "PUT" and EVIDENCE_UPLOAD.fullmatch(scope.get("path", ""))
        if evidence:
            # Multipart is parsed before FastAPI executes route dependencies.
            # Give an old client its usual compatibility response before admission.
            version = headers.get(b"x-lem-client-version", b"").decode("latin-1")
            policy = self.client_policy.evaluate(version)
            if not policy["compatible"]:
                return await JSONResponse({"detail": policy}, status_code=426)(scope, receive, send)
            if not self.upload_slots.acquire(blocking=False):
                return await JSONResponse(
                    {"detail": "Servidor ocupado: validação de fotografias; tente novamente"},
                    status_code=503,
                    headers={"Retry-After": "15"},
                )(scope, receive, send)

        consumed = 0

        async def limited_receive():
            nonlocal consumed
            try:
                message = await asyncio.wait_for(receive(), timeout=60) if evidence else await receive()
            except TimeoutError as error:
                raise HTTPException(408, "Tempo de espera pelo envio excedido") from error
            if message["type"] == "http.request":
                consumed += len(message.get("body", b""))
                if consumed > limit:
                    raise HTTPException(413, "Pedido demasiado grande")
            return message

        try:
            await self.app(scope, limited_receive, send)
        finally:
            if evidence:
                self.upload_slots.release()
