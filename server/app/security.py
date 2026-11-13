# SPDX-License-Identifier: GPL-3.0-only
# Part of LEM. See the accompanying LICENSE for license terms and warranty disclaimer.

# Credential primitives used by authentication. Password and token representations differ:
# passwords use salted scrypt; random session tokens use an SHA-256 lookup digest.

import hashlib, hmac, ipaddress, secrets
from fastapi import HTTPException


def hash_password(password):
    if len(password) < 12:
        raise HTTPException(422, "Password com pelo menos 12 caracteres")
    salt = secrets.token_hex(16)
    value = hashlib.scrypt(password.encode(), salt=salt.encode(), n=16384, r=8, p=1).hex()
    return salt + ":" + value


# Compare the derived key in constant time against the stored salted password encoding.
def verify(password, encoded):
    salt, expected = encoded.split(":")
    value = hashlib.scrypt(password.encode(), salt=salt.encode(), n=16384, r=8, p=1).hex()
    return hmac.compare_digest(value, expected)


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def client_network(address):
    """Rate-limit key for a client address.

    One IPv6 subscriber normally controls a whole /64, so per-address buckets
    would let it rotate addresses freely. IPv4-mapped addresses count as IPv4.
    """
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return address
    if ip.version == 6:
        if ip.ipv4_mapped:
            return str(ip.ipv4_mapped)
        return str(ipaddress.ip_network((ip, 64), strict=False))
    return str(ip)


class LoginLimiter:
    """Bounded independent account/IP/pair buckets for the single API worker.

    A pair-only bucket permits password spraying and rotating source addresses.
    The account bucket is deliberately less restrictive than the pair bucket.
    Entries expire after a minute; capacity is fail-closed, never evicting an
    active bucket to make room for an attacker's next alias.
    """

    def __init__(self, clock=None, capacity=10000):
        import time
        import threading

        self.clock = clock or time.monotonic
        self.capacity = capacity
        self.buckets = {}
        self.lock = threading.Lock()

    def consume(self, address, email):
        email = email.strip().lower()
        account = digest(email)
        address = client_network(address)
        keys = [(("pair", address, account), 5), (("ip", address), 30), (("account", account), 20)]
        with self.lock:
            t = self.clock()
            for key in list(self.buckets):
                recent = [v for v in self.buckets[key] if v > t - 60]
                if recent:
                    self.buckets[key] = recent
                else:
                    del self.buckets[key]
            if any(len(self.buckets.get(k, [])) >= maximum for k, maximum in keys):
                raise HTTPException(
                    429,
                    "Aguarde um minuto antes de tentar novamente",
                    headers={"Retry-After": "60"},
                )
            if len(self.buckets) + sum(k not in self.buckets for k, _ in keys) > self.capacity:
                raise HTTPException(
                    429,
                    "Aguarde um minuto antes de tentar novamente",
                    headers={"Retry-After": "60"},
                )
            for key, _ in keys:
                self.buckets.setdefault(key, []).append(t)
