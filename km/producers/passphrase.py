"""Passphrase producer — uniform random strings.

Each secret entry's ``format:`` controls length and alphabet. It may
be:

- omitted or a string: defaults (length=32, alphabet=alphanumeric).
- a mapping with ``length`` and/or ``alphabet`` keys.

``alphabet`` may be a named preset:

    alphanumeric        A-Z a-z 0-9        (default; 62 chars)
    alpha               A-Z a-z            (52)
    lower-alphanumeric  a-z 0-9            (36)
    upper-alphanumeric  A-Z 0-9            (36)
    numeric             0-9                (10)
    hex                 0-9 a-f            (16)
    hex-upper           0-9 A-F            (16)
    urlsafe             alphanumeric + -_  (64)
    printable           ! .. ~             (94, every printable ASCII)

…or an explicit character set as a string of length ≥ 2 (the chars
themselves are the allowed set; duplicates are tolerated but pointless).

Examples::

    secrets:
      - name: simple_default            # 32-char alphanumeric

      - name: long_passphrase
        format: { length: 64 }

      - name: numeric_pin
        format: { length: 6, alphabet: numeric }

      - name: hex_token
        format: { length: 40, alphabet: hex }

      - name: custom_alphabet
        format:
          length: 20
          alphabet: "abcdef0123456789-_"
"""

from __future__ import annotations

from collections.abc import Mapping

from km.generators import ALPHANUMERIC, random_string
from km.producers.contract import (
    ManifestEntry,
    Producer,
    ProducerContractError,
    Secret,
)

DEFAULT_LENGTH = 32

ALPHABETS: dict[str, str] = {
    "alphanumeric": ALPHANUMERIC,
    "alpha": "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz",
    "lower-alphanumeric": "abcdefghijklmnopqrstuvwxyz0123456789",
    "upper-alphanumeric": "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789",
    "numeric": "0123456789",
    "hex": "0123456789abcdef",
    "hex-upper": "0123456789ABCDEF",
    "urlsafe": ALPHANUMERIC + "-_",
    "printable": "".join(chr(c) for c in range(0x21, 0x7F)),
}


def _resolve_format(entry: ManifestEntry) -> tuple[int, str]:
    fmt = entry.format
    if fmt is None or isinstance(fmt, str):
        return DEFAULT_LENGTH, ALPHANUMERIC
    if not isinstance(fmt, Mapping):
        raise ProducerContractError(
            f"passphrase: secret {entry.name!r}: `format:` must be a string or "
            f"mapping (got {type(fmt).__name__})"
        )
    length = fmt.get("length", DEFAULT_LENGTH)
    if not isinstance(length, int) or isinstance(length, bool) or length < 1:
        raise ProducerContractError(
            f"passphrase: secret {entry.name!r}: `format.length` must be a "
            f"positive int (got {length!r})"
        )
    alphabet_raw = fmt.get("alphabet", "alphanumeric")
    if not isinstance(alphabet_raw, str) or not alphabet_raw:
        raise ProducerContractError(
            f"passphrase: secret {entry.name!r}: `format.alphabet` must be a "
            f"non-empty string (got {alphabet_raw!r})"
        )
    if alphabet_raw in ALPHABETS:
        chars = ALPHABETS[alphabet_raw]
    elif len(alphabet_raw) >= 2:
        chars = alphabet_raw
    else:
        raise ProducerContractError(
            f"passphrase: secret {entry.name!r}: alphabet {alphabet_raw!r} is "
            f"neither a named preset ({', '.join(sorted(ALPHABETS))}) nor an "
            "explicit character set of length >= 2"
        )
    return length, chars


class Passphrase(Producer):
    def produce(self, entries, ctx):
        results: list[Secret] = []
        for e in entries:
            length, chars = _resolve_format(e)
            results.append(Secret(name=e.name, value=random_string(length, chars)))
        return results
