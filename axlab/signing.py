"""Signing-key derivation for AxLab.

Every module derives its own key from one master secret, so keys are
independent: one derived key does not reveal the master or any other key.

The master secret comes from ``AXLAB_MASTER_KEY`` (or ``AXIOM_MASTER_KEY``,
for runs that share keys with an AXIOM install).  It is read when a key is
first needed, not at import.  There is no built-in default: a missing key is
an error, never a silent fixed key.

  python -c "import secrets; print(secrets.token_hex(32))"
  export AXLAB_MASTER_KEY="<64-char hex>"

The benchmarks set a fresh random key for their own process when none is
set, because what they sign (agent templates, rows, messages) exists only
for that run.
"""
from __future__ import annotations

import hashlib
import hmac
import os

ENV_VARS = ("AXLAB_MASTER_KEY", "AXIOM_MASTER_KEY")


def master_key() -> bytes:
    for name in ENV_VARS:
        raw = os.environ.get(name, "")
        if raw:
            return raw.encode("utf-8")
    raise RuntimeError(
        "AXLAB_MASTER_KEY not set. Generate one: "
        "python -c \"import secrets; print(secrets.token_hex(32))\" "
        "then: export AXLAB_MASTER_KEY=\"<hex>\""
    )


def derive_key(salt: bytes) -> bytes:
    """Module-specific key: HMAC-SHA256(salt, master).

    HMAC is the standard PRF-based KDF primitive; unlike a raw
    ``SHA256(master || salt)`` it is not open to length extension.
    """
    if not isinstance(salt, (bytes, bytearray)):
        raise TypeError("salt must be bytes")
    return hmac.new(salt, master_key(), hashlib.sha256).digest()
