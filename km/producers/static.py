"""Static producer — no-op; preserves whatever's already in ``secrets:``.

A ``producer: static`` group asserts "these values are not auto-rotated;
just keep them as the manifest currently records." Useful for secrets
the operator updates out of band but still wants under the unified
manifest schema (so audit/push/validate apply).
"""

from __future__ import annotations

from km.producers.contract import Producer, ProducerContractError, Secret


class Static(Producer):
    def produce(self, entries, ctx):
        results: list[Secret] = []
        for e in entries:
            current = ctx.current_secrets.get(e.name)
            if current is None or current == "":
                raise ProducerContractError(
                    f"static producer: secret {e.name!r} (group {e.group_name!r}) "
                    "has no current value in secrets: block — "
                    "static is a no-op, not a way to mint values"
                )
            results.append(Secret(name=e.name, value=current))
        return results
