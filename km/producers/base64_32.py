"""Base64-32 producer — base64 (with padding) of 32 random bytes."""

from __future__ import annotations

from km.generators import generate_base64_32
from km.producers.contract import Producer, Secret


class Base6432(Producer):
    def produce(self, entries, ctx):
        return [Secret(name=e.name, value=generate_base64_32()) for e in entries]
