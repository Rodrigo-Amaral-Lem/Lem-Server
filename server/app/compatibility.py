# SPDX-License-Identifier: GPL-3.0-only
# Part of LEM. See the accompanying LICENSE for license terms and warranty disclaimer.

"""Server-owned client release policy; never infer compatibility from authentication.

Versionless clients are rejected too, so releases predating the handshake cannot
bypass it by calling login directly. Update policy in .env and recreate the API.
"""

import os
import re
from urllib.parse import urlparse

VERSION = "0.5.1"
# Consolidation changes the release number, not the checklist-v7 protocol floor.
MINIMUM_CLIENT = "0.2.6"
HEADER = "X-Lem-Client-Version"


def version_tuple(value):
    if not isinstance(value, str) or not re.fullmatch(
        r"(?:0|[1-9][0-9]{0,5})\.(?:0|[1-9][0-9]{0,5})\.(?:0|[1-9][0-9]{0,5})", value
    ):
        return None
    return tuple(map(int, value.split(".")))


class ClientPolicy:
    def __init__(self):
        self.minimum = os.getenv("LEM_MIN_CLIENT_VERSION", MINIMUM_CLIENT)
        self.latest = os.getenv("LEM_LATEST_CLIENT_VERSION", VERSION)
        self.update_url = os.getenv("LEM_UPDATE_URL", "")
        low, high = version_tuple(self.minimum), version_tuple(self.latest)
        if low is None or high is None or low > high:
            raise ValueError(
                "Invalid Lem client policy: require minimum <= latest (major.minor.patch)"
            )
        # A stale deployment .env must not admit clients that cannot represent
        # the new checklist/units. Preserve stricter administrator policy values.
        if low < version_tuple(MINIMUM_CLIENT):
            self.minimum = MINIMUM_CLIENT
        if high < version_tuple(self.minimum):
            self.latest = self.minimum
        if self.update_url:
            url = urlparse(self.update_url)
            if url.scheme != "https" or not url.netloc or url.username or url.password:
                raise ValueError("LEM_UPDATE_URL must be an HTTPS download/instructions URL")

    def evaluate(self, version):
        current = version_tuple(version)
        compatible = current is not None and version_tuple(
            self.minimum
        ) <= current <= version_tuple(self.latest)
        update = compatible and current < version_tuple(self.latest)
        if not compatible:
            message = f"Esta versão da app ({version or 'não identificada'}) não é compatível com o servidor. Instale a versão {self.latest} para iniciar sessão."
        elif update:
            message = f"Está disponível a versão {self.latest}. Pode continuar a trabalhar; atualize a app quando possível."
        else:
            message = "A app está atualizada e é compatível com o servidor."
        return dict(
            policy_version=1,
            current_version=version or "",
            minimum_version=self.minimum,
            latest_version=self.latest,
            compatible=compatible,
            update_available=update,
            update_url=self.update_url,
            message=message,
        )
