"""Consecutive-probe-failure quarantine for MCP servers.

A probe verdict used to be display-only. ``probe_server`` could time out on the
same server eight times in a row and ``rebuild_agent_config`` would still emit
its ``@ref`` into every agent's ``tools``, so every new session spawned it
again. The mount decision had exactly two inputs -- what the user chose, and
nothing -- and "we tried this N times and it never answered" had nowhere to go.

This module is that third input, and it is deliberately a THIRD STATE rather
than a second writer of ``disabled``:

* ``disabled`` is the USER's choice. It lives in ``~/.kiro/settings/mcp.json``
  and nothing here ever writes it. A quarantine that flipped that key would be
  indistinguishable from the user having turned the server off -- they would
  have to guess which flag they were looking at to undo it, and a later probe
  success could silently re-enable something they had switched off by hand.
* ``quarantined`` is OURS. It lives only here and in the GENERATED agent spec
  (a derived artifact regenerated from scratch on every rebuild), so revoking
  it is a matter of clearing one counter.

Why a counter and not a single failure: a probe spawns a real process and one
failure is routinely transient (a cold npm cache, a laptop that just woke, a
registry blip). The quarantine only claims to catch a server that is
*consistently* unreachable, which is the case that costs a session on every
single start.

Statuses that carry no verdict are ignored rather than counted as either
outcome -- ``disabled`` (never probed), ``unknown`` / ``outdated`` (no fresh
result), and ``needs_auth``, which is a server working correctly and telling us
it wants a token. Counting ``needs_auth`` would quarantine every OAuth
connection the user has not signed into yet.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Any, Iterable

from kiro_crew.atomic_write import atomic_write
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.config.paths import data_home

logger = logging.getLogger(__name__)

STORE_FILENAME = "mcp-quarantine.json"
STORE_VERSION = 1

# A failed probe. Anything not in here and not "ok" carries no verdict at all.
FAILING_STATUSES = frozenset({"error", "timeout"})

# Bound the stored error so a pathological server cannot grow the state file
# without limit. The UI shows the live probe error anyway; this copy exists so
# the quarantine can explain itself after the probe cache has aged out.
_ERROR_MAX_CHARS = 400

# Tests point this at a tmp_path. Resolved per call, never at import, so a
# ``KIROCREW_HOME`` change between calls is honoured (the convention every
# other small state file in this tree follows).
_STORE_PATH: Path | None = None

# Every load-modify-save runs under this. Without it a probe round and a release
# race: both read the same records, and whichever saves last silently discards
# the other's decision -- so a release could report success while the record it
# deleted was written straight back, leaving the server quarantined and the
# button looking broken. Both writers live in the gateway process (the probe
# fan-out and the release endpoint are dashboard handlers), so a process-local
# lock closes the whole window. Readers take no lock: ``atomic_write`` renames,
# so a reader sees one whole version or the previous one, never a torn file.
_WRITE_LOCK = threading.Lock()


def store_path() -> Path:
    return _STORE_PATH if _STORE_PATH is not None else data_home() / STORE_FILENAME


def threshold() -> int:
    """Consecutive failures before a server is quarantined; 0 disables entirely.

    Read live rather than cached: the off switch has to be an off switch, and a
    stale copy would keep quarantining after an operator turned it off.
    """
    try:
        return max(0, int(KiroCrewConfig.load().agent.mcp_quarantine_after_failures))
    except Exception:
        logger.debug("cannot read mcp_quarantine_after_failures; feature off", exc_info=True)
        return 0


def _read() -> tuple[dict[str, dict[str, Any]], list[str], bool]:
    """``(records, applied, corrupt)``. ``corrupt`` is True only for an unusable file.

    A MISSING store is not corrupt -- that is the normal state on a machine where
    nothing has ever failed a probe.
    """
    path = store_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}, [], False
    except (json.JSONDecodeError, UnicodeDecodeError, OSError):
        # ``UnicodeDecodeError`` is NOT an ``OSError`` and not a
        # ``JSONDecodeError`` either -- it is a ``ValueError`` raised by the
        # strict decode inside ``read_text``, before json ever sees the bytes. A
        # store holding invalid UTF-8 therefore escaped both other arms and
        # surfaced as a 500 from whichever handler happened to read it.
        return {}, [], True
    if not isinstance(raw, dict):
        return {}, [], True
    servers = raw.get("servers")
    if not isinstance(servers, dict):
        return {}, [], True
    applied = raw.get("applied")
    applied_list = [a for a in applied if isinstance(a, str)] if isinstance(applied, list) else []
    return (
        {k: v for k, v in servers.items() if isinstance(k, str) and isinstance(v, dict)},
        applied_list,
        False,
    )


def _load() -> dict[str, dict[str, Any]]:
    """Return the per-server records, or ``{}`` for any unreadable store.

    Fails OPEN on purpose. This module can only ever REMOVE a server from the
    agent config, so a corrupt state file must not be able to quarantine
    anything -- an empty record set means every server mounts exactly as it
    does without this feature.
    """
    return _read()[0]


def applied_aliases() -> set[str]:
    """The aliases the last SUCCESSFUL agent-config rebuild was built from.

    This is what makes the rebuild trigger idempotent instead of
    transition-driven. Comparing it against the set a rebuild WOULD produce right
    now answers "is the emitted config out of date" directly, so every way the two
    can diverge is covered by one check rather than by bookkeeping at each site
    that could cause a divergence:

    * a rebuild that failed -- ``applied`` is simply not advanced, so the next
      probe round tries again instead of the transition being consumed forever;
    * a threshold lowered, raised or set to 0 -- the desired set changes with no
      verdict having been recorded;
    * a store that had to be reset after becoming unreadable;
    * a server becoming eligible or ineligible because the user added or removed
      it from a shared scope.

    Absent (a fresh install, or a store written before this field existed) reads
    as empty, which matches a config that has never unmounted anything.
    """
    return set(_read()[1])


def mark_applied(aliases: set[str]) -> None:
    """Record the set a rebuild has just successfully emitted. Raises on write failure."""
    with _WRITE_LOCK:
        servers, _applied, _corrupt = _read()
        _save(servers, applied=sorted(aliases))


def reset_unreadable_store() -> bool:
    """Try to reset an unusable store. True when the store WAS unreadable.

    The return value reports the OBSERVATION, not whether the write succeeded, and
    that distinction is load-bearing. Its caller passes it as
    ``applied_unknown``: the applied marker lives in this file, so once the file
    has been seen unreadable the marker is unknown either way. Reporting False
    because the reset write failed made the reconcile compare two empty sets --
    the fail-open read and the unknown marker -- conclude they agreed, and leave a
    server unmounted with nothing on screen to say why.

    Resetting is therefore best-effort: it stops a store that has become
    unreadable from staying that way, but the reconcile does not depend on it.
    """
    with _WRITE_LOCK:
        records, _applied, corrupt = _read()
        if not corrupt:
            return False
        logger.warning("MCP quarantine store %s was unreadable; resetting it", store_path())
        try:
            _save(records, applied=[])
        except OSError as exc:
            logger.warning("cannot reset MCP quarantine store %s: %s", store_path(), exc)
        return True


def _save(servers: dict[str, dict[str, Any]], *, applied: list[str] | None = None) -> None:
    """Write the records. RAISES on failure -- each caller decides what that means.

    ``applied`` defaults to PRESERVING whatever is on disk: the counter writes and
    the applied marker move independently, and a counter write that silently
    dropped the marker would make the next reconcile believe nothing had ever been
    unmounted.

    Deliberately not swallowed here. ``clear`` must not report a release it did
    not persist (the caller reports the release to the user), whereas
    ``record_verdicts`` can safely degrade to "the counter did not advance". Those
    are different decisions and only the callers know which one applies.
    """
    if applied is None:
        applied = _read()[1]
    payload = {"version": STORE_VERSION, "servers": servers, "applied": applied}
    atomic_write(store_path(), json.dumps(payload, indent=2) + "\n")


def record_verdicts(verdicts: Iterable[tuple[str, str, str]]) -> set[str]:
    """Fold ``(name, status, error)`` triples into the store; one write.

    Returns the names whose MOUNT STATE changed on this call -- newly
    quarantined AND newly released. The caller's only use for the return value is
    deciding whether the agent config needs regenerating, and both directions
    need one: a recovered server's record is deleted so ``quarantined_names`` stops
    reporting it, but its ``@ref`` and its ``mcpServers`` entry do not come back
    until something rebuilds. Reporting only quarantines left the UI clearing its
    badge while new sessions still omitted the server.

    A server that recovers WITHOUT having been quarantined is deliberately not
    reported: it stayed mounted throughout, so nothing needs rebuilding, and
    reporting it would rebuild on every probe round in which any server happened
    to carry a single stale failure.
    """
    limit = threshold()
    if limit <= 0:
        return set()
    with _WRITE_LOCK:
        servers = _load()
        mount_changed: set[str] = set()
        changed = False
        now = time.time()
        for name, status, error in verdicts:
            if not isinstance(name, str) or not name:
                continue
            if status == "ok":
                # A success clears the counter outright rather than decrementing
                # it. The claim being made is "consistently unreachable", and one
                # good handshake disproves it.
                gone = servers.pop(name, None)
                if gone is not None:
                    changed = True
                    prior = gone.get("fails")
                    if gone.get("quarantined_at") and isinstance(prior, int) and prior >= limit:
                        mount_changed.add(name)
                continue
            if status not in FAILING_STATUSES:
                continue
            rec = servers.setdefault(name, {})
            fails = rec.get("fails")
            rec["fails"] = (fails if isinstance(fails, int) and fails > 0 else 0) + 1
            rec["last_status"] = status
            rec["last_error"] = (error or "")[:_ERROR_MAX_CHARS]
            rec["last_failed_at"] = now
            changed = True
            if rec["fails"] >= limit and not rec.get("quarantined_at"):
                rec["quarantined_at"] = now
                mount_changed.add(name)
        if not changed:
            return set()
        try:
            _save(servers)
        except OSError as exc:
            # A store we cannot write means nothing moved, which degrades to
            # today's behaviour. Reporting a change here would be worse than
            # silence: the caller would rebuild the agent config against a
            # decision that is not on disk and will not survive.
            logger.warning("cannot write MCP quarantine store %s: %s", store_path(), exc)
            return set()
        return mount_changed


def quarantined_names() -> set[str]:
    """Server names currently quarantined, by RAW name (not alias).

    Re-checks the threshold so lowering it takes effect at the next rebuild and
    setting it to 0 releases every server, without needing to rewrite the store.
    """
    limit = threshold()
    if limit <= 0:
        return set()
    return {
        name
        for name, rec in _load().items()
        if rec.get("quarantined_at") and isinstance(rec.get("fails"), int) and rec["fails"] >= limit
    }


def _state_from(rec: dict[str, Any], limit: int) -> dict[str, Any]:
    """Shape one record for the API against an already-read threshold."""
    raw_fails = rec.get("fails")
    fails = raw_fails if isinstance(raw_fails, int) else 0
    return {
        "fails": fails,
        "quarantined": bool(limit > 0 and rec.get("quarantined_at") and fails >= limit),
        "lastStatus": rec.get("last_status", ""),
        "lastError": rec.get("last_error", ""),
        "since": rec.get("quarantined_at") or 0,
    }


def state_for(name: str) -> dict[str, Any] | None:
    """One server's record, or ``None`` when it has no failures on file."""
    rec = _load().get(name)
    if not rec:
        return None
    return _state_from(rec, threshold())


def clear(name: str) -> dict[str, Any] | None:
    """Release one server; returns the record it removed, or ``None`` if absent.

    This is what the dashboard's re-enable button calls. It resets the COUNTER
    as well as the flag -- releasing a server but leaving it one failure away
    from re-quarantine would make the button look broken.

    The removed record is HANDED BACK rather than discarded so the caller can put
    it back with ``restore`` if the work that has to accompany a release (the
    agent-config rebuild) fails. Without that, a half-done release is
    unrecoverable from the UI: the store says released, so the badge and the
    Remount control both disappear, while the emitted config still omits the
    server -- and the control the user would retry with is the one that just
    vanished.

    Propagates a write failure rather than swallowing it: the caller reports
    success to the user, so a release that did not reach disk must not read as
    a release.
    """
    with _WRITE_LOCK:
        servers = _load()
        gone = servers.pop(name, None)
        if gone is None:
            return None
        _save(servers)
        return gone


def restore(name: str, record: dict[str, Any]) -> None:
    """Put a record removed by ``clear`` back, undoing a release that failed.

    Best-effort by contract: the caller is already on a failure path and reports
    the failure either way. Raises on a write error so the caller can say that
    the rollback ALSO failed, rather than implying the state is intact.
    """
    with _WRITE_LOCK:
        servers = _load()
        servers[name] = record
        _save(servers)


def snapshot() -> dict[str, dict[str, Any]]:
    """Every server with failures on file, shaped for the API.

    ONE store read and ONE config read for the whole set. Calling ``state_for``
    per name would re-read both per record, so annotating an N-server probe
    response cost N file reads -- on the event loop, in the handler that runs on
    every dashboard poll.
    """
    limit = threshold()
    return {name: _state_from(rec, limit) for name, rec in _load().items()}
