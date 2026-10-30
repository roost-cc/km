"""UUID v4 producer."""

from __future__ import annotations

from km.generators import generate_uuid
from km.producers.contract import Producer, Secret


class Uuid(Producer):
    def produce(self, entries, ctx):
        return [Secret(name=e.name, value=generate_uuid()) for e in entries]
