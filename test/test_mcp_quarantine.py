"""Tests for the consecutive-probe-failure quarantine.

The defect these pin: a probe verdict was display-only, so a server that failed
its handshake eight times running was still emitted into every agent's ``tools``
and re-spawned by every new session. These cover the counter's arithmetic, the
statuses that deliberately carry NO verdict, the fail-open behaviour of an
unreadable store, and the wire contract of the release endpoint.

The mount decision itself is pinned in ``test_agent.py`` beside the ``disabled``
gate it sits next to, because it needs that module's install harness.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew import mcp_quarantine


@pytest.fixture
def store(tmp_path, monkeypatch):
    """Point the quarantine store at tmp_path and pin the threshold to 3."""
    path = tmp_path / "mcp-quarantine.json"
    monkeypatch.setattr(mcp_quarantine, "_STORE_PATH", path)
    monkeypatch.setattr(mcp_quarantine, "threshold", lambda: 3)
    return path


def _fail(name: str, status: str = "error", error: str = "boom"):
    return [(name, status, error)]


# ---------------------------------------------------------------------------
# the counter
# ---------------------------------------------------------------------------


class TestCounter:
    def test_failures_accumulate_and_cross_the_threshold_once(self, store):
        assert mcp_quarantine.record_verdicts(_fail("airbnb")) == set()
        assert mcp_quarantine.record_verdicts(_fail("airbnb")) == set()
        assert mcp_quarantine.quarantined_names() == set()

        # Third consecutive failure is the one that quarantines.
        assert mcp_quarantine.record_verdicts(_fail("airbnb")) == {"airbnb"}
        assert mcp_quarantine.quarantined_names() == {"airbnb"}

        # And it reports newly-crossed ONCE. The caller rebuilds the agent
        # config off this return value, so a sticky "newly" would rebuild on
        # every probe round for as long as the server stayed broken.
        assert mcp_quarantine.record_verdicts(_fail("airbnb")) == set()
        assert mcp_quarantine.quarantined_names() == {"airbnb"}

    def test_a_timeout_counts_the_same_as_an_error(self, store):
        mcp_quarantine.record_verdicts(_fail("bazi", status="timeout", error="timeout after 15s"))
        mcp_quarantine.record_verdicts(_fail("bazi", status="error"))
        assert mcp_quarantine.record_verdicts(_fail("bazi", status="timeout")) == {"bazi"}

    def test_one_success_clears_the_counter_outright(self, store):
        mcp_quarantine.record_verdicts(_fail("airbnb"))
        mcp_quarantine.record_verdicts(_fail("airbnb"))
        mcp_quarantine.record_verdicts([("airbnb", "ok", "")])
        # Not decremented to 1 -- gone. The claim is "consistently
        # unreachable", and one good handshake disproves it, so the next two
        # failures must not be enough to quarantine.
        assert mcp_quarantine.state_for("airbnb") is None
        mcp_quarantine.record_verdicts(_fail("airbnb"))
        mcp_quarantine.record_verdicts(_fail("airbnb"))
        assert mcp_quarantine.quarantined_names() == set()

    def test_a_success_releases_an_already_quarantined_server(self, store):
        for _ in range(3):
            mcp_quarantine.record_verdicts(_fail("airbnb"))
        assert mcp_quarantine.quarantined_names() == {"airbnb"}
        # Reported as a mount change: the record is gone so nothing calls the
        # server quarantined any more, but its @ref and mcpServers entry do not
        # come back until the caller rebuilds off this return value.
        assert mcp_quarantine.record_verdicts([("airbnb", "ok", "")]) == {"airbnb"}
        assert mcp_quarantine.quarantined_names() == set()

    def test_a_recovery_below_the_threshold_is_not_a_mount_change(self, store):
        """It never stopped being mounted, so nothing needs rebuilding.

        Reporting it would rebuild the agent config on every probe round in which
        any server happened to carry a single stale failure.
        """
        mcp_quarantine.record_verdicts(_fail("airbnb"))
        assert mcp_quarantine.record_verdicts([("airbnb", "ok", "")]) == set()

    def test_servers_are_counted_independently(self, store):
        mcp_quarantine.record_verdicts([("a", "error", ""), ("b", "ok", "")])
        mcp_quarantine.record_verdicts([("a", "error", ""), ("b", "error", "")])
        assert mcp_quarantine.record_verdicts([("a", "error", ""), ("b", "error", "")]) == {"a"}
        assert mcp_quarantine.quarantined_names() == {"a"}

    def test_the_stored_error_is_bounded(self, store):
        mcp_quarantine.record_verdicts(_fail("noisy", error="x" * 5000))
        assert len(mcp_quarantine.state_for("noisy")["lastError"]) == 400


# ---------------------------------------------------------------------------
# statuses that carry no verdict
# ---------------------------------------------------------------------------


class TestNonVerdictStatuses:
    @pytest.mark.parametrize("status", ["needs_auth", "unknown", "outdated", "disabled", ""])
    def test_status_is_neither_a_failure_nor_a_success(self, store, status):
        """These are "no result", not "a bad result".

        ``needs_auth`` is the load-bearing one: a server asking for OAuth
        sign-in is working correctly and saying so. Counting it would
        quarantine every connection the user has not signed into yet -- and
        then a quarantined server can never be signed into, so the state would
        be self-sealing.
        """
        mcp_quarantine.record_verdicts(_fail("srv"))
        before = mcp_quarantine.state_for("srv")
        assert mcp_quarantine.record_verdicts([("srv", status, "")]) == set()
        assert mcp_quarantine.state_for("srv") == before

    def test_a_nameless_row_is_skipped(self, store):
        assert mcp_quarantine.record_verdicts([("", "error", "")]) == set()
        assert mcp_quarantine.snapshot() == {}


# ---------------------------------------------------------------------------
# the off switch
# ---------------------------------------------------------------------------


class TestDisabled:
    def test_threshold_zero_never_quarantines(self, tmp_path, monkeypatch):
        monkeypatch.setattr(mcp_quarantine, "_STORE_PATH", tmp_path / "q.json")
        monkeypatch.setattr(mcp_quarantine, "threshold", lambda: 0)
        for _ in range(10):
            assert mcp_quarantine.record_verdicts(_fail("airbnb")) == set()
        assert mcp_quarantine.quarantined_names() == set()

    def test_threshold_zero_releases_records_written_earlier(self, store, monkeypatch):
        """Turning the feature off has to release what it already caught.

        Otherwise the switch is only half an off switch: new failures stop
        counting but the servers already quarantined stay unmounted, with no
        surface left that explains why.
        """
        for _ in range(3):
            mcp_quarantine.record_verdicts(_fail("airbnb"))
        assert mcp_quarantine.quarantined_names() == {"airbnb"}
        monkeypatch.setattr(mcp_quarantine, "threshold", lambda: 0)
        assert mcp_quarantine.quarantined_names() == set()

    def test_raising_the_threshold_releases_a_server_below_it(self, store, monkeypatch):
        for _ in range(3):
            mcp_quarantine.record_verdicts(_fail("airbnb"))
        monkeypatch.setattr(mcp_quarantine, "threshold", lambda: 5)
        assert mcp_quarantine.quarantined_names() == set()


# ---------------------------------------------------------------------------
# the store itself
# ---------------------------------------------------------------------------


class TestStore:
    def test_absent_store_reads_as_empty(self, store):
        assert mcp_quarantine.quarantined_names() == set()
        assert mcp_quarantine.snapshot() == {}
        assert mcp_quarantine.state_for("anything") is None

    @pytest.mark.parametrize("body", ["{ not json", "[]", '{"servers": 5}', '"text"'])
    def test_a_corrupt_store_fails_open(self, store, body):
        """A store we cannot parse must quarantine NOTHING.

        This module can only ever remove a server from the agent config, so
        failing closed would let one bad byte on disk unmount the user's whole
        MCP fleet with no way to see why.
        """
        store.write_text(body, encoding="utf-8")
        assert mcp_quarantine.quarantined_names() == set()
        assert mcp_quarantine.snapshot() == {}

    def test_invalid_utf8_reads_as_corrupt_rather_than_raising(self, store):
        """``read_text`` decodes strictly, and ``UnicodeDecodeError`` is neither an
        ``OSError`` nor a ``JSONDecodeError`` -- it is a ``ValueError`` raised
        before json sees the bytes. It escaped both other arms and surfaced as a
        500 from whichever handler happened to read the store.
        """
        store.write_bytes(b'{"servers": {"a\xff\xfe": {}}}')
        assert mcp_quarantine.quarantined_names() == set()
        assert mcp_quarantine.snapshot() == {}
        assert mcp_quarantine.applied_aliases() == set()
        assert mcp_quarantine.reset_unreadable_store() is True

    @pytest.mark.parametrize("body", ["{ not json", "[]", '{"servers": 5}', '"text"'])
    def test_a_corrupt_store_is_repaired_so_the_config_can_reconcile(self, store, body):
        """Failing open silently changes the quarantined set; something must say so.

        Nothing else would reconcile it: ``record_verdicts`` reads the same empty
        view, so a failing server counts up from zero, crosses no threshold, and
        triggers no rebuild -- leaving a server unmounted by a config written when
        the store was readable, with its badge and release control both gone.
        """
        store.write_text(body, encoding="utf-8")
        assert mcp_quarantine.reset_unreadable_store() is True
        # Now readable and empty, so a later probe behaves normally.
        assert json.loads(store.read_text(encoding="utf-8"))["servers"] == {}
        # And idempotent: a healthy store is not rewritten and reports no change.
        assert mcp_quarantine.reset_unreadable_store() is False

    def test_an_unwritable_reset_still_reports_the_corruption(self, store, monkeypatch):
        """The observation is what the caller needs, not the write's success.

        The applied marker lives in this file, so once it has been seen unreadable
        the marker is unknown whether or not the reset landed. Reporting False here
        made the reconcile compare two empty sets -- the fail-open read and the
        unknown marker -- decide they agreed, and leave a server unmounted with
        nothing on screen to say why.
        """
        store.write_text("{ not json", encoding="utf-8")

        def boom(*_a, **_k):
            raise OSError("read-only file system")

        monkeypatch.setattr(mcp_quarantine, "atomic_write", boom)
        assert mcp_quarantine.reset_unreadable_store() is True

    def test_an_absent_store_is_not_corrupt(self, store):
        """The normal state on a machine where nothing has failed a probe.

        Reporting it as a repair would force a rebuild on every probe round of
        every healthy installation.
        """
        assert not store.exists()
        assert mcp_quarantine.reset_unreadable_store() is False
        assert not store.exists(), "a missing store must not be created just to read it"

    def test_a_malformed_record_is_dropped_not_trusted(self, store):
        store.write_text(
            json.dumps({"version": 1, "servers": {"a": "not-a-dict", "b": {"fails": 9}}}),
            encoding="utf-8",
        )
        # ``b`` has no quarantined_at, so it is a counter with no verdict yet.
        assert mcp_quarantine.quarantined_names() == set()
        assert "a" not in mcp_quarantine.snapshot()

    def test_an_unwritable_store_does_not_raise(self, store, monkeypatch):
        def boom(*_a, **_k):
            raise OSError("read-only file system")

        monkeypatch.setattr(mcp_quarantine, "atomic_write", boom)
        assert mcp_quarantine.record_verdicts(_fail("airbnb")) == set()

    def test_an_unpersisted_crossing_is_not_reported(self, store, monkeypatch):
        """A crossing the store did not accept must not be announced.

        The caller rebuilds the agent config off this return value, so reporting
        a quarantine that is not on disk would unmount a server on a decision
        that vanishes at the next read.
        """
        mcp_quarantine.record_verdicts(_fail("airbnb"))
        mcp_quarantine.record_verdicts(_fail("airbnb"))

        def boom(*_a, **_k):
            raise OSError("disk full")

        monkeypatch.setattr(mcp_quarantine, "atomic_write", boom)
        assert mcp_quarantine.record_verdicts(_fail("airbnb")) == set()
        assert mcp_quarantine.quarantined_names() == set()

    def test_clear_propagates_a_write_failure(self, store, monkeypatch):
        """Unlike the counter, a release must NOT degrade quietly.

        Its caller tells the user the server is back and rebuilds the agent
        config; a swallowed failure would make that claim false.
        """
        for _ in range(3):
            mcp_quarantine.record_verdicts(_fail("airbnb"))

        def boom(*_a, **_k):
            raise OSError("disk full")

        monkeypatch.setattr(mcp_quarantine, "atomic_write", boom)
        with pytest.raises(OSError):
            mcp_quarantine.clear("airbnb")

    def test_every_mutation_holds_the_write_lock_across_load_and_save(self, store, monkeypatch):
        """The read-modify-write must be one transaction.

        Without it a probe round and a release race on the same file: both read
        the same records and whichever saves last discards the other's decision,
        so a release can report success while the record it deleted is written
        straight back and the server stays quarantined.

        Asserted at the write, which is the far end of the window -- the lock has
        to still be held there, not merely taken somewhere earlier.
        """
        held: list[bool] = []
        real = mcp_quarantine.atomic_write

        def watching(*a, **k):
            held.append(mcp_quarantine._WRITE_LOCK.locked())
            return real(*a, **k)

        monkeypatch.setattr(mcp_quarantine, "atomic_write", watching)
        mcp_quarantine.record_verdicts(_fail("airbnb"))
        mcp_quarantine.clear("airbnb")
        assert held == [True, True], "a mutation reached its write without the lock"

    def test_snapshot_reads_the_store_once_regardless_of_size(self, store, monkeypatch):
        """Pins the fix for a quadratic read on the event loop.

        ``snapshot`` used to call ``state_for`` per name, and each of those
        re-read the store AND the config -- so annotating an N-server probe
        response cost N file reads, in a handler that runs on every dashboard
        poll.
        """
        mcp_quarantine.record_verdicts([(f"srv{i}", "error", "") for i in range(25)])
        reads = {"n": 0}
        real = mcp_quarantine._load

        def counting():
            reads["n"] += 1
            return real()

        monkeypatch.setattr(mcp_quarantine, "_load", counting)
        snap = mcp_quarantine.snapshot()
        assert len(snap) == 25
        assert reads["n"] == 1, f"snapshot read the store {reads['n']} times for 25 servers"

    def test_the_store_is_valid_json_with_a_version(self, store):
        mcp_quarantine.record_verdicts(_fail("airbnb"))
        payload = json.loads(store.read_text(encoding="utf-8"))
        assert payload["version"] == mcp_quarantine.STORE_VERSION
        assert payload["servers"]["airbnb"]["fails"] == 1

    def test_store_path_follows_the_data_home(self, tmp_path, monkeypatch):
        monkeypatch.setattr(mcp_quarantine, "_STORE_PATH", None)
        monkeypatch.setattr(mcp_quarantine, "data_home", lambda: tmp_path / "crew")
        assert mcp_quarantine.store_path() == tmp_path / "crew" / "mcp-quarantine.json"


# ---------------------------------------------------------------------------
# release
# ---------------------------------------------------------------------------


class TestClear:
    def test_clear_resets_the_counter_as_well_as_the_flag(self, store):
        for _ in range(3):
            mcp_quarantine.record_verdicts(_fail("airbnb"))
        removed = mcp_quarantine.clear("airbnb")
        # The removed record is handed back so a caller whose accompanying work
        # fails can put it back rather than leave a half-done release.
        assert removed is not None and removed["fails"] == 3
        assert mcp_quarantine.quarantined_names() == set()
        # The counter too: releasing a server one failure short of re-quarantine
        # would make the button look broken.
        assert mcp_quarantine.state_for("airbnb") is None
        assert mcp_quarantine.record_verdicts(_fail("airbnb")) == set()

    def test_clear_is_idempotent_and_reports_nothing_to_do(self, store):
        assert mcp_quarantine.clear("never-seen") is None

    def test_restore_puts_a_released_record_back(self, store):
        for _ in range(3):
            mcp_quarantine.record_verdicts(_fail("airbnb"))
        removed = mcp_quarantine.clear("airbnb")
        assert mcp_quarantine.quarantined_names() == set()
        mcp_quarantine.restore("airbnb", removed)
        # Byte-for-byte the prior state, so the row renders exactly as before
        # rather than as a fresh single failure.
        assert mcp_quarantine.quarantined_names() == {"airbnb"}
        assert mcp_quarantine.state_for("airbnb")["fails"] == 3


# ---------------------------------------------------------------------------
# POST /api/mcp/quarantine/clear
# ---------------------------------------------------------------------------


@pytest.fixture
def store_path_of(tmp_path, monkeypatch):
    """The store path used by the endpoint fixture, for corrupting it directly."""
    return tmp_path / "q.json"


@pytest.fixture
def endpoint(tmp_path, monkeypatch):
    from kiro_crew.dashboard.handlers import mcp as mcp_mod

    monkeypatch.setattr(mcp_quarantine, "_STORE_PATH", tmp_path / "q.json")
    monkeypatch.setattr(mcp_quarantine, "threshold", lambda: 3)
    rebuild = MagicMock()
    # Patched on the HANDLER module: it imports the symbol at module scope, so
    # patching kiro_crew.agent would leave this call site bound to the original.
    monkeypatch.setattr(mcp_mod, "rebuild_agent_config", rebuild)
    # Eligibility reads the real scope files, which a tmp_path store knows nothing
    # about. Make every recorded server eligible so these tests exercise the
    # reconcile (desired vs applied) rather than scope discovery.
    monkeypatch.setattr(
        mcp_mod, "quarantine_effective_aliases", lambda: mcp_quarantine.quarantined_names()
    )
    monkeypatch.setattr(mcp_mod, "quarantine_eligible_aliases", lambda: {"airbnb", "healthy"})
    monkeypatch.setattr(mcp_mod, "sel", lambda: MagicMock())
    monkeypatch.setattr(mcp_mod, "_mcp_probe_cache", [])
    return SimpleNamespace(mod=mcp_mod, rebuild=rebuild)


async def _client(mod) -> TestClient:
    app = web.Application()
    app["state"] = MagicMock()
    app.router.add_post("/api/mcp/quarantine/clear", mod.api_mcp_quarantine_clear)
    client = TestClient(TestServer(app))
    await client.start_server()
    return client


def _quarantine_and_apply(name: str = "airbnb") -> None:
    """Drive ``name`` into quarantine AND mark it as emitted, like a probe round.

    The applied marker is what the reconcile compares against, and only a probe
    round's reconcile writes it -- so a test that merely records failures leaves
    the store in a state no running gateway produces, where the server is
    quarantined but the emitted config is believed to reflect nothing.
    """
    for _ in range(3):
        mcp_quarantine.record_verdicts(_fail(name))
    mcp_quarantine.mark_applied({name})


@pytest.mark.asyncio
class TestClearEndpoint:
    async def test_release_rebuilds_the_agent_config_in_the_same_request(self, endpoint):
        _quarantine_and_apply()
        client = await _client(endpoint.mod)
        try:
            resp = await client.post("/api/mcp/quarantine/clear", json={"name": "airbnb"})
            assert resp.status == 200
            assert await resp.json() == {"ok": True, "name": "airbnb", "released": True}
        finally:
            await client.close()
        assert mcp_quarantine.quarantined_names() == set()
        # Without the rebuild the ref stays absent from the agent config, so the
        # next session still would not mount the server and the release would
        # read as having done nothing.
        assert endpoint.rebuild.called

    async def test_releasing_an_unquarantined_server_costs_no_rebuild(self, endpoint):
        """Reconcile rebuilds only when the emitted config is actually out of date.

        Round 2 rebuilt on every clear request to make a retry self-healing. The
        reconcile supersedes that: it compares what a rebuild would emit against
        what the last one did, so it heals a stale config (see the test below)
        without paying for a rebuild when nothing is wrong.
        """
        client = await _client(endpoint.mod)
        try:
            resp = await client.post("/api/mcp/quarantine/clear", json={"name": "healthy"})
            assert resp.status == 200
            assert (await resp.json())["released"] is False
        finally:
            await client.close()
        assert not endpoint.rebuild.called

    async def test_a_retry_heals_a_config_left_stale_by_an_earlier_failure(self, endpoint):
        """The self-healing half, without the blind rebuild.

        Precondition is the aftermath of a partial failure: the store says nothing
        is quarantined, but the last emitted config unmounted a server. A retry
        must reconcile that even though there is no record left to release.
        """
        mcp_quarantine.mark_applied({"airbnb"})
        assert mcp_quarantine.quarantined_names() == set()
        client = await _client(endpoint.mod)
        try:
            resp = await client.post("/api/mcp/quarantine/clear", json={"name": "airbnb"})
            assert resp.status == 200
            assert (await resp.json())["released"] is False
        finally:
            await client.close()
        assert endpoint.rebuild.called, "a stale config must be reconciled on retry"
        assert mcp_quarantine.applied_aliases() == set()

    async def test_release_drops_the_stale_annotation_from_the_probe_cache(self, endpoint):
        _quarantine_and_apply()
        endpoint.mod._mcp_probe_cache[:] = [
            {"name": "airbnb", "status": "error", "quarantined": True, "probeFailures": 3}
        ]
        client = await _client(endpoint.mod)
        try:
            await client.post("/api/mcp/quarantine/clear", json={"name": "airbnb"})
        finally:
            await client.close()
        row = endpoint.mod._mcp_probe_cache[0]
        assert "quarantined" not in row and "probeFailures" not in row

    async def test_a_missing_name_is_rejected(self, endpoint):
        client = await _client(endpoint.mod)
        try:
            resp = await client.post("/api/mcp/quarantine/clear", json={})
            assert resp.status == 400
        finally:
            await client.close()

    async def test_invalid_json_is_rejected(self, endpoint):
        client = await _client(endpoint.mod)
        try:
            resp = await client.post(
                "/api/mcp/quarantine/clear",
                data="not json",
                headers={"Content-Type": "application/json"},
            )
            assert resp.status == 400
        finally:
            await client.close()

    async def test_a_failed_rebuild_reports_500_rather_than_claiming_success(self, endpoint):
        _quarantine_and_apply()
        endpoint.rebuild.side_effect = OSError("disk full")
        client = await _client(endpoint.mod)
        try:
            resp = await client.post("/api/mcp/quarantine/clear", json={"name": "airbnb"})
            # Reporting 200 would tell the user the server is back when the
            # emitted config still omits it.
            assert resp.status == 500
        finally:
            await client.close()

    async def test_a_failed_rebuild_rolls_the_release_back(self, endpoint):
        """A half-done release must not strand the user without a retry control.

        Store write lands, rebuild fails: if the record stayed deleted the store
        would say released, so the badge AND the Remount control disappear on the
        next poll while the emitted config still omits the server -- and the
        control the user would retry with is the one that just vanished. Putting
        the record back keeps the server visibly quarantined, which is also the
        truth, since it is still not mounted.
        """
        _quarantine_and_apply()
        endpoint.rebuild.side_effect = OSError("disk full")
        client = await _client(endpoint.mod)
        try:
            resp = await client.post("/api/mcp/quarantine/clear", json={"name": "airbnb"})
            assert resp.status == 500
            assert (await resp.json())["code"] == "agent_config_rebuild_failed"
        finally:
            await client.close()
        assert mcp_quarantine.quarantined_names() == {"airbnb"}, "the release must be rolled back"
        # And the counter comes back with it, so the row renders exactly as before.
        assert mcp_quarantine.state_for("airbnb")["fails"] == 3

    async def test_a_failed_rollback_is_reported_distinctly(self, endpoint, monkeypatch):
        """Both writes failing is a different state and says so.

        Implying the state is intact would be a lie: the store and the emitted
        config now disagree, and only a later probe or rebuild reconciles them.
        """
        _quarantine_and_apply()
        endpoint.rebuild.side_effect = OSError("disk full")

        def boom(*_a, **_k):
            raise OSError("read-only")

        real_restore = mcp_quarantine.restore

        def failing_restore(name, rec):
            monkeypatch.setattr(mcp_quarantine, "atomic_write", boom)
            return real_restore(name, rec)

        monkeypatch.setattr(mcp_quarantine, "restore", failing_restore)
        client = await _client(endpoint.mod)
        try:
            resp = await client.post("/api/mcp/quarantine/clear", json={"name": "airbnb"})
            assert resp.status == 500
            assert (await resp.json())["code"] == "agent_config_rebuild_failed_rollback_failed"
        finally:
            await client.close()

    async def test_a_marker_failure_does_not_undo_a_successful_release(self, endpoint, monkeypatch):
        """The rebuild worked; only its bookkeeping did not. Do NOT roll back.

        Rolling back here would leave the emitted config with the server mounted
        while the store said it was quarantined -- an inconsistency created by the
        error handling rather than by the error. The endpoint therefore branches on
        the reconcile's own verdict, not on whether the marker moved.
        """
        _quarantine_and_apply()
        monkeypatch.setattr(mcp_quarantine, "mark_applied", MagicMock(side_effect=OSError("ro")))
        client = await _client(endpoint.mod)
        try:
            resp = await client.post("/api/mcp/quarantine/clear", json={"name": "airbnb"})
            assert resp.status == 200
            assert (await resp.json())["released"] is True
        finally:
            await client.close()
        assert endpoint.rebuild.called
        assert mcp_quarantine.quarantined_names() == set(), "the release must stand"

    async def test_the_release_and_its_reconcile_are_one_critical_section(self, endpoint):
        """A probe reconcile must not interleave between the clear and its rebuild.

        Both take the agent-config lock -- the one the toggle path and the agents
        handlers already use, because ``rebuild_agent_config`` is a
        read-modify-write of that whole file and two concurrent ones can lose each
        other's work. Asserted structurally: the endpoint holds the lock across the
        pair and therefore calls the ALREADY-LOCKED reconcile, since calling the
        wrapper would deadlock on a non-reentrant lock.
        """
        import inspect

        src = inspect.getsource(endpoint.mod.api_mcp_quarantine_clear)
        assert "async with _get_config_lock():" in src
        assert "_reconcile_locked()" in src
        assert (
            "await _reconcile_quarantine_mounts()" not in src
        ), "the wrapper re-takes the lock the endpoint already holds"

    async def test_a_failed_store_write_reports_500_and_a_code(self, endpoint, monkeypatch):
        _quarantine_and_apply()

        def boom(*_a, **_k):
            raise OSError("disk full")

        monkeypatch.setattr(mcp_quarantine, "atomic_write", boom)
        client = await _client(endpoint.mod)
        try:
            resp = await client.post("/api/mcp/quarantine/clear", json={"name": "airbnb"})
            assert resp.status == 500
            assert (await resp.json())["code"] == "quarantine_store_write_failed"
        finally:
            await client.close()
        # Nothing was released, and nothing pretended otherwise.
        assert mcp_quarantine.quarantined_names() == {"airbnb"}
        assert not endpoint.rebuild.called


# ---------------------------------------------------------------------------
# row annotation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestReconcile:
    """The rebuild trigger is idempotent, not transition-driven.

    Three separate blocking findings across two review rounds were all the same
    defect: a state transition persisted while the rebuild that had to accompany
    it did not, and nothing afterwards noticed. These pin the property that
    replaced the bookkeeping.
    """

    async def test_the_probe_path_reconciles_under_the_lock(self, endpoint):
        """The probe paths use the lock-taking wrapper, not the bare inner form."""
        import inspect

        for fn in (endpoint.mod.api_mcp_probe, endpoint.mod._run_mcp_probe):
            src = inspect.getsource(fn)
            assert (
                "_reconcile_quarantine_mounts(applied_unknown=unreadable)" in src
            ), f"{fn.__name__} must reconcile through the lock-taking wrapper"

    async def test_a_failed_rebuild_is_retried_rather_than_consumed(self, endpoint):
        """The transition is not spent by a rebuild that failed.

        Earlier revisions triggered off "what changed on this call", so a failed
        rebuild consumed the crossing forever -- later probes reported no change
        and the emitted config stayed inconsistent for good.
        """
        for _ in range(3):
            mcp_quarantine.record_verdicts(_fail("airbnb"))
        endpoint.rebuild.side_effect = OSError("disk full")
        await endpoint.mod._reconcile_locked()
        assert endpoint.rebuild.call_count == 1
        # Not advanced, so the next round tries again.
        assert mcp_quarantine.applied_aliases() == set()

        endpoint.rebuild.side_effect = None
        await endpoint.mod._reconcile_locked()
        assert endpoint.rebuild.call_count == 2
        assert mcp_quarantine.applied_aliases() == {"airbnb"}

    async def test_a_matching_config_costs_no_rebuild(self, endpoint):
        """The timed background probe must not regenerate the config every round."""
        _quarantine_and_apply()
        await endpoint.mod._reconcile_locked()
        assert not endpoint.rebuild.called

    async def test_turning_the_threshold_off_reconciles(self, endpoint, monkeypatch):
        """A threshold change moves the decision with no verdict to report it.

        Nothing is probed and nothing is released, yet the server must come back --
        which a transition-driven trigger could not see.
        """
        _quarantine_and_apply()
        monkeypatch.setattr(mcp_quarantine, "threshold", lambda: 0)
        await endpoint.mod._reconcile_locked()
        assert endpoint.rebuild.called
        assert mcp_quarantine.applied_aliases() == set()

    async def test_a_reset_store_reconciles(self, endpoint, store_path_of):
        """A reset store leaves the applied marker UNKNOWN, not empty.

        The marker lived in the file that became unreadable, so after the reset we
        cannot know what the emitted config unmounted -- and "unknown" must not
        compare equal to "nothing was unmounted", which would leave a server
        unmounted with its badge and release control both gone.
        """
        _quarantine_and_apply()
        store_path_of.write_text("{ not json", encoding="utf-8")
        assert mcp_quarantine.reset_unreadable_store() is True
        await endpoint.mod._reconcile_locked(applied_unknown=True)
        assert endpoint.rebuild.called

    async def test_a_marker_write_failure_only_costs_a_repeat(self, endpoint, monkeypatch):
        """The config is correct; only the bookkeeping did not land.

        Wasteful, never wrong -- the next round rebuilds once more rather than the
        release being lost. And it must report a MATCH, because the config matches:
        callers that read a marker-write failure as a failed rebuild go on to undo
        work that actually succeeded.
        """
        for _ in range(3):
            mcp_quarantine.record_verdicts(_fail("airbnb"))
        monkeypatch.setattr(mcp_quarantine, "mark_applied", MagicMock(side_effect=OSError("ro")))
        assert await endpoint.mod._reconcile_locked() is True
        assert endpoint.rebuild.called

    async def test_a_failed_rebuild_reports_no_match(self, endpoint):
        for _ in range(3):
            mcp_quarantine.record_verdicts(_fail("airbnb"))
        endpoint.rebuild.side_effect = OSError("disk full")
        assert await endpoint.mod._reconcile_locked() is False


class TestAnnotation:
    def test_recording_is_offloaded_whole(self):
        """Both halves of the record step must be inside ONE ``to_thread``.

        ``_quarantine_verdicts`` reads up to three MCP scope files to decide
        eligibility, so passing its RESULT as an argument to ``to_thread``
        evaluated it on the event loop and made the gateway pay for those reads on
        every probe round.
        """
        import inspect

        from kiro_crew.dashboard.handlers import mcp as mcp_mod

        for fn in (mcp_mod.api_mcp_probe, mcp_mod._run_mcp_probe):
            src = inspect.getsource(fn)
            assert (
                "to_thread(_record_probe_verdicts, result)" in src
            ), f"{fn.__name__} must offload the filter with the record"
            assert (
                "_quarantine_verdicts(result)" not in src
            ), f"{fn.__name__} evaluates the scope-reading filter on the event loop"

    def test_every_row_returning_endpoint_annotates(self):
        """The badge has to be on the endpoint the table LOADS from.

        Found by driving a real pod, not by reading code: the first version
        annotated only the two probe endpoints, so a quarantined server rendered
        as a plain failing row until the user happened to press Probe -- the
        explanation and the release control both absent on the surface a user
        lands on. This pins all four call sites.
        """
        import inspect

        from kiro_crew.dashboard.handlers import mcp as mcp_mod

        for fn in (
            mcp_mod.api_mcp_servers,
            mcp_mod.api_mcp_probe,
            mcp_mod.api_mcp_probe_cached,
            mcp_mod._run_mcp_probe,
        ):
            src = inspect.getsource(fn)
            assert "_annotate_quarantine" in src, f"{fn.__name__} returns rows without the badge"
            # And off the loop: the annotation reads the store.
            assert (
                "to_thread(_annotate_quarantine" in src
            ), f"{fn.__name__} annotates on the event loop"

    def test_healthy_rows_are_left_byte_identical(self, store):
        from kiro_crew.dashboard.handlers import mcp as mcp_mod

        rows: list[dict] = [{"name": "healthy", "status": "ok"}]
        mcp_mod._annotate_quarantine(rows)
        assert rows == [{"name": "healthy", "status": "ok"}]

    def test_a_failing_row_carries_the_count_before_it_is_quarantined(self, store):
        from kiro_crew.dashboard.handlers import mcp as mcp_mod

        mcp_quarantine.record_verdicts(_fail("airbnb"))
        rows: list[dict] = [{"name": "airbnb", "status": "error"}]
        mcp_mod._annotate_quarantine(rows)
        assert rows[0]["probeFailures"] == 1
        assert rows[0]["quarantined"] is False

    def test_a_quarantined_row_says_so(self, store):
        from kiro_crew.dashboard.handlers import mcp as mcp_mod

        for _ in range(3):
            mcp_quarantine.record_verdicts(_fail("airbnb"))
        rows: list[dict] = [{"name": "airbnb", "status": "error"}]
        mcp_mod._annotate_quarantine(rows)
        assert rows[0]["quarantined"] is True
        assert rows[0]["probeFailures"] == 3

    def test_verdict_extraction_tolerates_missing_keys(self, store, monkeypatch):
        from kiro_crew.dashboard.handlers import mcp as mcp_mod

        monkeypatch.setattr(mcp_mod, "quarantine_eligible_aliases", lambda: {"a"})
        # A nameless row cannot be eligible, so it is dropped rather than counted
        # under the empty name.
        assert mcp_mod._quarantine_verdicts([{}]) == []
        assert mcp_mod._quarantine_verdicts([{"name": "a", "status": "ok", "error": None}]) == [
            ("a", "ok", "")
        ]

    def test_kirocrews_own_servers_are_never_counted(self, store, monkeypatch):
        """No record for an ineligible server, so no badge can claim an unmount.

        Recording is filtered by the same eligibility the mount decision uses, so
        the two cannot drift into disagreeing.
        """
        from kiro_crew.dashboard.handlers import mcp as mcp_mod

        monkeypatch.setattr(mcp_mod, "quarantine_eligible_aliases", lambda: {"airbnb"})
        rows = [
            {"name": "kirocrew-core", "status": "error", "error": "x"},
            {"name": "loner", "status": "error", "error": "x"},
            {"name": "airbnb", "status": "error", "error": "x"},
        ]
        assert mcp_mod._quarantine_verdicts(rows) == [("airbnb", "error", "x")]
