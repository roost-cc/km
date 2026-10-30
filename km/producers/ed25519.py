"""Ed25519 private-key producer.

Mints a fresh Ed25519 private key (PKCS8 PEM) per entry. No public key
is emitted: it can be derived from the private key on demand
(``Ed25519PrivateKey.public_key()``) and historical verification is the
publisher's job (e.g. Infisical's per-secret version history).
"""

from __future__ import annotations

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519

from km.producers.contract import Producer, Secret


def _mint_pem() -> str:
    priv = ed25519.Ed25519PrivateKey.generate()
    return priv.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("ascii")


class Ed25519(Producer):
    def produce(self, entries, ctx):
        return [Secret(name=e.name, value=_mint_pem()) for e in entries]
