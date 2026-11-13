# SPDX-License-Identifier: GPL-3.0-only
# Part of LEM. See the accompanying LICENSE for license terms and warranty disclaimer.

"""Bound bodies before JSON/multipart parsing, including streamed requests."""

from fastapi import HTTPException
from starlette.responses import JSONResponse


class RequestBodyLimit:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = dict(scope.get("headers", []))
        multipart = headers.get(b"content-type", b"").lower().startswith(b"multipart/form-data")
        limit = 21 * 1024 * 1024 if multipart else 512 * 1024
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
        consumed = 0

        async def limited_receive():
            nonlocal consumed
            message = await receive()
            if message["type"] == "http.request":
                consumed += len(message.get("body", b""))
                if consumed > limit:
                    raise HTTPException(413, "Pedido demasiado grande")
            return message

        await self.app(scope, limited_receive, send)
