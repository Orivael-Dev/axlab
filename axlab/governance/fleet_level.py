"""Fleet escalation level — one kill switch for every agent run (Layer 4).

The NodeXLoop Fleet ladder (simRuntime.ts LEVELS), applied to agents:

    0 NORMAL
    1 L1 WARNING    flagged in receipts, nothing else changes
    2 L2 THROTTLE   side effects need a human; one model slot
    3 L3 SUSPEND    no new model calls; runs checkpoint and can resume
    4 L4 TERMINATE  in-flight work is dropped; runs end and cannot resume

Every writer and reader — the NodeXLoop extension, AXIOM loop runs, the
slim agent fabric, the ``nxl_set_level`` MCP tool, this module's CLI — shares
one file, ``.nodexloop/governance.json``.  A stop works the same from any of
them, with no process talking to another.

Scopes: ``fleet`` (everything), ``loop:<graph name>``, ``run:<run id>``,
``fabric:<name>``.  A reader's level is the highest over the scopes it is in;
``fleet`` is always one of them.

Easy to pull, hard to release:

  raise    anyone who can write the file, no key.  Only ever goes up.
  release  a ``base`` section HMAC'd with the release key (NXL_RELEASE_KEY,
           else ~/.nodexloop/release.key).  The only way down: it sets the
           baseline per scope and acknowledges the raises it has seen.

  missing file             -> NORMAL
  unreadable / bad JSON    -> fleet L3
  base that does not verify (tampered, or no key here) -> fleet L3

What this is not: a security boundary against code running as the same user.
Such code can read the key file or edit the runner.  The point is that a stop
always works and a release is always deliberate and receipted — the same
claim, and the same limit, as ModelDev's subprocess runner.

Cost: ``FleetSwitch.level()`` re-stats the file at most every ``poll_s``
(default 50 ms); between polls it is a monotonic-clock compare and a dict
lookup per scope.  Reaction time to a raise is therefore <= poll_s.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import sys
import time
from pathlib import Path
from typing import Iterable, Optional

LEVELS = ("NORMAL", "L1 WARNING", "L2 THROTTLE", "L3 SUSPEND", "L4 TERMINATE")
NORMAL, WARNING, THROTTLE, SUSPEND, TERMINATE = range(5)

FILE_NAME = "governance.json"
RECEIPTS_NAME = "governance-receipts.jsonl"
_SCOPE_RE = re.compile(r"^(fleet|(loop|run|fabric|agent):\S{1,200})$")
_LOCK_STALE_S = 5.0


def canonical(obj) -> bytes:
    """Sorted keys at every depth, no spaces, UTF-8 — byte-identical to
    nodexloop moduleResolver.canonicalJson."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")


def parse_level(v) -> int:
    """4, "4", "L4", "l4 terminate", "TERMINATE" -> 4."""
    if isinstance(v, int) and not isinstance(v, bool):
        n = v
    else:
        s = str(v).strip().upper()
        n = None
        m = re.match(r"^L?([0-4])\b", s)
        if m:
            n = int(m.group(1))
        else:
            for i, name in enumerate(LEVELS):
                if s == name or s == name.split()[-1]:
                    n = i
        if n is None:
            raise ValueError(f"not a fleet level: {v!r}")
    if not 0 <= n < len(LEVELS):
        raise ValueError(f"fleet level out of range: {v!r}")
    return n


def check_scope(scope: str) -> str:
    if not _SCOPE_RE.match(scope or ""):
        raise ValueError(f"bad scope {scope!r} (fleet | loop:<name> | run:<id> | fabric:<name>)")
    return scope


def default_path(start: str | Path = ".") -> Path:
    env = os.environ.get("NXL_GOVERNANCE_FILE")
    return Path(env) if env else Path(start) / ".nodexloop" / FILE_NAME


# ── release key ──────────────────────────────────────────────────────────────

def release_key_path() -> Path:
    return Path.home() / ".nodexloop" / "release.key"


def release_key(create: bool = False) -> Optional[bytes]:
    """The key that signs a release.  None when there is none and create=False."""
    env = os.environ.get("NXL_RELEASE_KEY", "").strip()
    if env:
        return bytes.fromhex(env)
    p = release_key_path()
    try:
        return bytes.fromhex(p.read_text(encoding="ascii").strip())
    except FileNotFoundError:
        if not create:
            return None
    except ValueError:
        return None
    p.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="ascii") as f:
        f.write(secrets.token_hex(32))
    return release_key(create=False)


def _base_mac(base: dict, key: bytes) -> str:
    body = {k: v for k, v in base.items() if k != "mac"}
    return hmac.new(key, canonical(body), hashlib.sha256).hexdigest()


# ── evaluation ───────────────────────────────────────────────────────────────

def evaluate(doc, key: Optional[bytes]) -> tuple[dict, str]:
    """Effective level per scope from a parsed document, and a note.

    Pure: the same document and key always give the same answer.
    """
    if doc is None:
        return {}, ""
    if not isinstance(doc, dict):
        return {"fleet": SUSPEND}, "unreadable governance file"
    levels: dict[str, int] = {}
    note = ""
    base = doc.get("base")
    acks: dict = {}
    if base is not None:
        ok = (isinstance(base, dict) and key is not None
              and hmac.compare_digest(str(base.get("mac", "")), _base_mac(base, key)))
        if not ok:
            levels["fleet"] = SUSPEND
            note = "release does not verify" + ("" if key else " (no release key here)")
        else:
            for s, lv in (base.get("levels") or {}).items():
                try:
                    levels[s] = max(levels.get(s, 0), parse_level(lv))
                except ValueError:
                    levels["fleet"] = max(levels.get("fleet", 0), SUSPEND)
            acks = base.get("acks") or {}
    for r in doc.get("raises") or []:
        try:
            s, lv, seq = r["scope"], parse_level(r["level"]), int(r["seq"])
        except (KeyError, TypeError, ValueError):
            levels["fleet"] = max(levels.get("fleet", 0), SUSPEND)
            note = note or "malformed raise"
            continue
        if seq > int(acks.get(s, 0)):
            levels[s] = max(levels.get(s, 0), lv)
    return levels, note


class FleetSwitch:
    """Cached reader (and writer) of one governance file."""

    def __init__(self, path: str | Path | None = None, *, poll_s: float = 0.05,
                 key: Optional[bytes] = None) -> None:
        self.path = Path(path) if path is not None else default_path()
        self.poll_s = poll_s
        self._key = key
        self._levels: dict[str, int] = {}
        self.note = ""
        self._sig: Optional[tuple] = None
        self._next = 0.0

    # ── read ────────────────────────────────────────────────────────

    def level(self, *scopes: str) -> int:
        now = time.monotonic()
        if now >= self._next:
            self._next = now + self.poll_s
            self._refresh()
        lv = self._levels
        if not lv:
            return NORMAL
        out = lv.get("fleet", 0)
        for s in scopes:
            v = lv.get(s, 0)
            if v > out:
                out = v
        return out

    def refresh(self) -> None:
        """Re-read now, ignoring the poll interval."""
        self._next = 0.0
        self._sig = None

    def _refresh(self) -> None:
        try:
            st = os.stat(self.path)
        except FileNotFoundError:
            self._sig, self._levels, self.note = None, {}, ""
            return
        except OSError:
            self._levels, self.note = {"fleet": SUSPEND}, "governance file not readable"
            return
        sig = (st.st_mtime_ns, st.st_size, st.st_ino)
        if sig == self._sig:
            return
        doc = self._read()
        key = self._key if self._key is not None else release_key()
        self._levels, self.note = evaluate(doc, key)
        self._sig = sig

    def _read(self):
        for _ in range(3):   # Windows: a writer's os.replace can briefly deny reads
            try:
                return json.loads(self.path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                return None
            except (OSError, ValueError):
                time.sleep(0.002)
        return "unreadable"

    def snapshot(self) -> dict:
        self.refresh()
        self.level()
        return {"file": str(self.path), "levels": dict(self._levels),
                "named": {s: LEVELS[v] for s, v in self._levels.items()},
                "note": self.note}

    # ── write ───────────────────────────────────────────────────────

    def raise_level(self, scope: str, level, *, by: str, reason: str = "") -> int:
        """Raise ``scope`` to ``level``.  No key needed.  Returns the new
        effective level; refuses (ValueError) anything that is not a raise."""
        check_scope(scope)
        lv = parse_level(level)
        with _Lock(self.path):
            doc = self._read_doc()
            cur = evaluate(doc, self._key if self._key is not None else release_key())[0]
            if lv <= cur.get(scope, 0):
                raise ValueError(f"{scope} is already at {LEVELS[cur.get(scope, 0)]}; "
                                 f"a raise only goes up — lowering needs a release")
            raises = doc.setdefault("raises", [])
            base = doc.get("base") or {}
            seq = 1 + max([int(r.get("seq", 0)) for r in raises]
                          + [int(v) for v in (base.get("acks") or {}).values()] + [0])
            raises.append({"seq": seq, "scope": scope, "level": lv, "by": by,
                           "reason": reason, "at": _now()})
            self._write_doc(doc)
            self._receipt("raise", scope, cur.get(scope, 0), lv, by, reason, seq=seq)
        self.refresh()
        return self.level(scope)

    def release(self, scope: str, level, *, by: str, reason: str,
                key: Optional[bytes] = None) -> int:
        """Set ``scope``'s baseline to ``level`` (up or down) and acknowledge
        every raise on it so far.  Needs the release key."""
        check_scope(scope)
        lv = parse_level(level)
        if not reason.strip():
            raise ValueError("a release needs a reason")
        key = key or self._key or release_key(create=True)
        with _Lock(self.path):
            doc = self._read_doc()
            before = evaluate(doc, key)[0].get(scope, 0)
            base = doc.get("base") or {}
            if base and not hmac.compare_digest(str(base.get("mac", "")), _base_mac(base, key)):
                raise PermissionError("the current release does not verify with this key; "
                                      "fix or remove it before releasing")
            raises = doc.get("raises") or []
            acks = dict(base.get("acks") or {})
            acks[scope] = max([int(acks.get(scope, 0))]
                              + [int(r.get("seq", 0)) for r in raises if r.get("scope") == scope])
            levels = dict(base.get("levels") or {})
            if lv:
                levels[scope] = lv
            else:
                levels.pop(scope, None)
            base = {"levels": levels, "acks": acks, "by": by, "reason": reason, "at": _now()}
            base["mac"] = _base_mac(base, key)
            doc["base"] = base
            doc["raises"] = [r for r in raises if int(r.get("seq", 0)) > int(acks.get(r.get("scope"), 0))]
            self._write_doc(doc)
            self._receipt("release", scope, before, lv, by, reason, key=key)
        self.refresh()
        return self.level(scope)

    def _read_doc(self) -> dict:
        doc = self._read()
        if doc is None:
            return {"v": 1, "raises": []}
        if not isinstance(doc, dict):
            # Unreadable: rebuild rather than refuse, so a raise still lands.
            # The old (unverifiable) base is dropped; a release re-signs.
            return {"v": 1, "raises": [], "note": "rebuilt from an unreadable file"}
        return doc

    def _write_doc(self, doc: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(doc, indent=1, ensure_ascii=False), encoding="utf-8")
        for i in range(20):
            try:
                os.replace(tmp, self.path)
                return
            except PermissionError:   # Windows: a reader holds it open
                time.sleep(0.005 * (i + 1))
        os.replace(tmp, self.path)

    def _receipt(self, op, scope, frm, to, by, reason, *, seq=None, key=None) -> None:
        """Append to the receipt log; each line carries the previous line's
        sha256, so an edited or dropped line breaks the chain."""
        p = self.path.with_name(RECEIPTS_NAME)
        prev = ""
        try:
            with open(p, "rb") as f:
                f.seek(0, 2)
                f.seek(max(0, f.tell() - 4096))
                lines = f.read().splitlines()
                if lines:
                    prev = hashlib.sha256(lines[-1]).hexdigest()
        except FileNotFoundError:
            pass
        rec = {"at": _now(), "op": op, "scope": scope, "from": LEVELS[frm], "to": LEVELS[to],
               "by": by, "reason": reason, "prev": prev}
        if seq is not None:
            rec["seq"] = seq
        if key is not None:
            rec["mac"] = hmac.new(key, canonical(rec), hashlib.sha256).hexdigest()
        with open(p, "ab") as f:
            f.write(canonical(rec) + b"\n")


class _Lock:
    """Exclusive create of <file>.lock; a lock older than 5 s is stale."""

    def __init__(self, path: Path) -> None:
        self.lock = path.with_name(path.name + ".lock")

    def __enter__(self):
        self.lock.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + 3.0
        while True:
            try:
                fd = os.open(self.lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
                os.write(fd, str(os.getpid()).encode())
                os.close(fd)
                return self
            except FileExistsError:
                try:
                    if time.time() - os.stat(self.lock).st_mtime > _LOCK_STALE_S:
                        os.unlink(self.lock)
                        continue
                except FileNotFoundError:
                    continue
                if time.monotonic() > deadline:
                    raise TimeoutError(f"governance lock busy: {self.lock}")
                time.sleep(0.002)

    def __exit__(self, *exc):
        try:
            os.unlink(self.lock)
        except FileNotFoundError:
            pass


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def scopes_for(*, loop: str = "", run: str = "", fabric: str = "") -> tuple[str, ...]:
    out = []
    if loop:
        out.append("loop:" + re.sub(r"\s+", "_", loop.strip()))
    if run:
        out.append("run:" + run)
    if fabric:
        out.append("fabric:" + fabric)
    return tuple(out)


# ── CLI ──────────────────────────────────────────────────────────────────────

def main(argv: Optional[Iterable[str]] = None) -> int:
    import argparse
    import getpass

    ap = argparse.ArgumentParser(prog="python -m axlab.governance.fleet_level",
                                 description="Fleet kill switch: status | raise | release")
    ap.add_argument("--file", default=None, help="governance file (default .nodexloop/governance.json)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    for name in ("raise", "release"):
        p = sub.add_parser(name)
        p.add_argument("scope", help="fleet | loop:<name> | run:<id> | fabric:<name>")
        p.add_argument("level", help="0-4, L4, TERMINATE ...")
        p.add_argument("--reason", default="")
        p.add_argument("--by", default=None)
    a = ap.parse_args(list(argv) if argv is not None else None)
    sw = FleetSwitch(a.file)
    try:
        if a.cmd == "raise":
            sw.raise_level(a.scope, a.level, by=a.by or getpass.getuser(), reason=a.reason)
        elif a.cmd == "release":
            sw.release(a.scope, a.level, by=a.by or getpass.getuser(), reason=a.reason)
    except (ValueError, PermissionError, TimeoutError) as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(sw.snapshot(), indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
