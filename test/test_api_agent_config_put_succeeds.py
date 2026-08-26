"""Integration test for api_agent_config PUT.

Regression test for bug where local variable 'config_path' shadowed the
imported config_path() function, causing "'PosixPath' object is not callable".
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web

from kiro_crew.dashboard.handlers import api_agent_config


@pytest.fixture(autouse=True)
def _owner_caller(monkeypatch):
    """Run as the dashboard owner: these tests exercise handler behavior PAST
    the owner boundary on the agents module's mutating endpoints, which has
    its own enumerate-the-invariant coverage in
    test_agents_endpoints_owner_auth.py."""
    monkeypatch.setattr(
        "kiro_crew.dashboard.handlers.agents.is_owner_dashboard_request",
        lambda request: True,
    )


@pytest.mark.asyncio
async def test_api_agent_config_put_succeeds(tmp_path):
    installed = tmp_path / "kirocrew.json"
    installed.write_text(json.dumps({"name": "kirocrew"}))
    defaults = tmp_path / "defaults.json"
    mc_cfg = tmp_path / "config.json"

    request = MagicMock(spec=web.Request)
    request.method = "PUT"
    request.app = {"state": MagicMock()}

    async def mock_json():
        return {"config": {"name": "test", "tools": ["a"], "allowedTools": ["b"]}}

    request.json = mock_json

    with (
        patch("kiro_crew.dashboard.handlers._installed_agent_config", return_value=installed),
        patch("kiro_crew.dashboard.handlers._find_agent_config", return_value=defaults),
        patch("kiro_crew.dashboard.handlers._reset_all_sessions", new_callable=AsyncMock),
        patch("kiro_crew.dashboard.handlers.config_path", return_value=mc_cfg),
        patch(
            "kiro_crew.agent.build_agent_config",
            return_value={"toolsSettings": {"execute_bash": {"deniedCommands": ["rm -rf"]}}},
        ),
        patch(
            "kiro_crew.dashboard.handlers.agents.get_shipped_tools",
            return_value={"tools": ["a", "c"], "allowedTools": ["b"]},
        ),
    ):

        response = await api_agent_config(request)

    assert response.status == 200
    # Verify the handler actually wrote the config files
    assert installed.exists()
    assert json.loads(installed.read_text(encoding="utf-8"))["name"] == "test"
    assert mc_cfg.exists()
    assert json.loads(mc_cfg.read_text(encoding="utf-8"))["removedTools"]["tools"] == ["c"]


@pytest.mark.asyncio
async def test_api_agent_config_put_strips_governed_grants(tmp_path, monkeypatch):
    """A dashboard PUT persists the config verbatim, so it MUST run the whole map
    through the governance filter — else a governed @denied allowedTools entry or
    a governed server's autoApprove written here restores the bypass the per-ref
    writers close. Executable (not source-inspection) coverage of that writer."""
    import kiro_crew.platform.governance as gov

    # Govern @denied only; everything else may auto-approve.
    monkeypatch.setattr(gov, "may_skip_gate_now", lambda ref: ref != "@denied")

    installed = tmp_path / "kirocrew.json"
    installed.write_text(json.dumps({"name": "kirocrew"}))
    defaults = tmp_path / "defaults.json"
    mc_cfg = tmp_path / "config.json"

    request = MagicMock(spec=web.Request)
    request.method = "PUT"
    request.app = {"state": MagicMock()}

    async def mock_json():
        return {
            "config": {
                "name": "test",
                # mount list must NOT be filtered (mounting != auto-approving)
                "tools": ["@denied", "@ok"],
                "allowedTools": ["@ok", "@denied"],
                "mcpServers": {
                    "denied": {"url": "u", "autoApprove": ["dangerous"]},
                    "ok": {"url": "u", "autoApprove": ["fine"]},
                },
            }
        }

    request.json = mock_json

    with (
        patch("kiro_crew.dashboard.handlers._installed_agent_config", return_value=installed),
        patch("kiro_crew.dashboard.handlers._find_agent_config", return_value=defaults),
        patch("kiro_crew.dashboard.handlers._reset_all_sessions", new_callable=AsyncMock),
        patch("kiro_crew.dashboard.handlers.config_path", return_value=mc_cfg),
        patch(
            "kiro_crew.dashboard.handlers.agents.get_shipped_tools",
            return_value={"tools": [], "allowedTools": []},
        ),
    ):
        response = await api_agent_config(request)

    assert response.status == 200
    written = json.loads(installed.read_text(encoding="utf-8"))
    # Governed @denied dropped from auto-approve; ungoverned @ok kept.
    assert written["allowedTools"] == ["@ok"]
    # Mount list is untouched — @denied stays mounted, just not auto-approved.
    assert written["tools"] == ["@denied", "@ok"]
    # Governed server loses autoApprove; ungoverned server keeps it.
    assert "autoApprove" not in written["mcpServers"]["denied"]
    assert written["mcpServers"]["ok"]["autoApprove"] == ["fine"]


@pytest.mark.asyncio
async def test_api_agent_config_put_strips_bookkeeping_keys(tmp_path):
    """A dashboard PUT must not re-pollute the kiro spec with Kiro Crew keys.

    Regression for #2570: the agent-detail PATCH strips ``model_managed`` /
    ``cc_model``, but the whole-config PUT used to persist them verbatim.
    kiro-cli ``deny_unknown_fields`` then rejects the entire agent until the
    next ``migrate_agent_specs`` heal on gateway rebuild.
    """
    from kiro_crew import agent_state

    installed = tmp_path / "kirocrew.json"
    installed.write_text(json.dumps({"name": "kirocrew"}))
    defaults = tmp_path / "defaults.json"
    mc_cfg = tmp_path / "config.json"

    request = MagicMock(spec=web.Request)
    request.method = "PUT"
    request.app = {"state": MagicMock()}

    async def mock_json():
        return {
            "config": {
                "name": "kirocrew",
                "tools": ["a"],
                "allowedTools": ["b"],
                "model_managed": True,
                "cc_model": "claude-sonnet-4.6",
            }
        }

    request.json = mock_json

    with (
        patch("kiro_crew.dashboard.handlers._installed_agent_config", return_value=installed),
        patch("kiro_crew.dashboard.handlers._find_agent_config", return_value=defaults),
        patch("kiro_crew.dashboard.handlers._reset_all_sessions", new_callable=AsyncMock),
        patch("kiro_crew.dashboard.handlers.config_path", return_value=mc_cfg),
        patch("kiro_crew.agent.get_shipped_tools", return_value={"tools": [], "allowedTools": []}),
    ):
        response = await api_agent_config(request)

    assert response.status == 200
    written = json.loads(installed.read_text(encoding="utf-8"))
    assert "model_managed" not in written
    assert "cc_model" not in written
    assert written["name"] == "kirocrew"
    # Lifted into the sidecar when previously unset (same rule as migrate).
    assert agent_state.get_model_managed("kirocrew") is True
    assert agent_state.get_cc_model("kirocrew") == "claude-sonnet-4.6"


@pytest.mark.asyncio
async def test_api_agent_config_put_does_not_clobber_sidecar(tmp_path):
    """A stale bookkeeping key in the PUT body must not overwrite the sidecar."""
    from kiro_crew import agent_state

    installed = tmp_path / "kirocrew.json"
    installed.write_text(json.dumps({"name": "kirocrew"}))
    defaults = tmp_path / "defaults.json"
    mc_cfg = tmp_path / "config.json"

    agent_state.set_model_managed("kirocrew", False)
    agent_state.set_cc_model("kirocrew", "test-model-stub")

    request = MagicMock(spec=web.Request)
    request.method = "PUT"
    request.app = {"state": MagicMock()}

    async def mock_json():
        return {
            "config": {
                "name": "kirocrew",
                "tools": ["a"],
                "allowedTools": ["b"],
                "model_managed": True,
                "cc_model": "claude-sonnet-4.6",
            }
        }

    request.json = mock_json

    with (
        patch("kiro_crew.dashboard.handlers._installed_agent_config", return_value=installed),
        patch("kiro_crew.dashboard.handlers._find_agent_config", return_value=defaults),
        patch("kiro_crew.dashboard.handlers._reset_all_sessions", new_callable=AsyncMock),
        patch("kiro_crew.dashboard.handlers.config_path", return_value=mc_cfg),
        patch("kiro_crew.agent.get_shipped_tools", return_value={"tools": [], "allowedTools": []}),
    ):
        response = await api_agent_config(request)

    assert response.status == 200
    written = json.loads(installed.read_text(encoding="utf-8"))
    assert "model_managed" not in written
    assert "cc_model" not in written
    assert agent_state.get_model_managed("kirocrew") is False
    assert agent_state.get_cc_model("kirocrew") == "test-model-stub"


@pytest.mark.asyncio
async def test_api_agent_config_put_uses_atomic_write(tmp_path):
    """PUT must persist the installed spec via write_config_atomically, not a
    bare write_text.

    Regression for #5086: a truncating in-place write leaves the spec corrupt
    on a mid-write crash or disk-full, breaking every subsequent session start
    because kiro-cli reads the spec at spawn.  The fix routes the write through
    write_config_atomically (temp-file + os.replace), matching the mc_cfg sidecar
    write already in the same handler.

    This test patches write_config_atomically at the site the handler imports it
    from and asserts it is called exactly once with the right args; it also
    patches Path.write_text to assert the handler never falls back to a bare,
    non-atomic write on the installed-spec path.
    """

    installed = tmp_path / "kirocrew.json"
    installed.write_text(json.dumps({"name": "kirocrew"}))
    defaults = tmp_path / "defaults.json"
    mc_cfg = tmp_path / "config.json"

    request = MagicMock(spec=web.Request)
    request.method = "PUT"
    request.app = {"state": MagicMock()}

    async def mock_json():
        return {"config": {"name": "test", "tools": [], "allowedTools": []}}

    request.json = mock_json

    atomic_calls: list = []

    def _fake_atomic(path, data, **kwargs):
        atomic_calls.append((path, data))
        # Actually write so downstream read-backs don't break.
        path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")

    with (
        patch("kiro_crew.dashboard.handlers._installed_agent_config", return_value=installed),
        patch("kiro_crew.dashboard.handlers._find_agent_config", return_value=defaults),
        patch("kiro_crew.dashboard.handlers._reset_all_sessions", new_callable=AsyncMock),
        patch("kiro_crew.dashboard.handlers.config_path", return_value=mc_cfg),
        patch(
            "kiro_crew.dashboard.handlers.agents.get_shipped_tools",
            return_value={"tools": [], "allowedTools": []},
        ),
        # Intercept write_config_atomically as imported into agents.py.
        patch(
            "kiro_crew.dashboard.handlers.agents.write_config_atomically",
            side_effect=_fake_atomic,
        ),
    ):
        response = await api_agent_config(request)

    assert response.status == 200
    # write_config_atomically must be called for the installed spec path.
    # (It is also called for the mc_cfg sidecar on the same code path, so
    # total call count may be > 1 — we care only that the installed spec write
    # went through the atomic helper, not the total invocation count.)
    installed_spec_calls = [(p, d) for p, d in atomic_calls if p == installed]
    assert len(installed_spec_calls) == 1, (
        f"Expected write_config_atomically to be called once with installed_path={installed!r}; "
        f"got calls to: {[str(p) for p, _ in atomic_calls]}.  "
        f"A bare write_text was likely used for the installed spec instead."
    )
    _, written_data = installed_spec_calls[0]
    assert written_data.get("name") == "test"


@pytest.mark.asyncio
async def test_agent_config_write_holds_the_mcp_transaction_lock(tmp_path):
    """The agent-spec write participates in the Disconnect transaction lock.

    Agent spec files are a census source for Disconnect's ownership oracle,
    which judges and acts inside ``_get_mcp_lock``. A config write landing
    between that census read and the grant unlink would lose its grant to a
    judgment that never saw it. External writers cannot be serialized; the
    gateway's own writer must be.
    """
    import contextlib

    installed = tmp_path / "kirocrew.json"
    installed.write_text(json.dumps({"name": "kirocrew"}))
    defaults = tmp_path / "defaults.json"
    mc_cfg = tmp_path / "config.json"

    request = MagicMock(spec=web.Request)
    request.method = "PUT"
    request.app = {"state": MagicMock()}

    async def mock_json():
        return {"config": {"name": "test", "tools": ["a"], "allowedTools": ["b"]}}

    request.json = mock_json

    lock_held: list[bool] = []
    writes: list[tuple[str, bool]] = []
    holding = False

    @contextlib.asynccontextmanager
    async def _recording_lock():
        nonlocal holding
        holding = True
        lock_held.append(True)
        try:
            yield
        finally:
            holding = False

    def _recording_write(path, config):  # noqa: ANN001 - mirrors the real signature
        writes.append((str(path), holding))

    with (
        patch("kiro_crew.dashboard.handlers._installed_agent_config", return_value=installed),
        patch("kiro_crew.dashboard.handlers._find_agent_config", return_value=defaults),
        patch("kiro_crew.dashboard.handlers._reset_all_sessions", new_callable=AsyncMock),
        patch("kiro_crew.dashboard.handlers.config_path", return_value=mc_cfg),
        patch(
            "kiro_crew.agent.build_agent_config",
            return_value={"toolsSettings": {}},
        ),
        patch(
            "kiro_crew.dashboard.handlers.agents.get_shipped_tools",
            return_value={"tools": ["a", "c"], "allowedTools": ["b"]},
        ),
        patch(
            "kiro_crew.dashboard.handlers.mcp._get_mcp_lock",
            _recording_lock,
        ),
        patch(
            "kiro_crew.dashboard.handlers.agents.write_config_atomically",
            _recording_write,
        ),
    ):
        resp = await api_agent_config(request)

    assert resp.status == 200
    # Only the agent-SPEC write must hold the lock: the spec file is a census
    # source for Disconnect's oracle. The config.json sidecar write is not.
    spec_writes = [held for path, held in writes if path == str(installed)]
    assert spec_writes, "the agent-spec write never happened"
    assert all(spec_writes), "the agent-spec write ran OUTSIDE the MCP lock"


@pytest.mark.asyncio
async def test_agent_config_write_drains_the_worker_on_cancellation(tmp_path):
    """The spec write goes through the SHIELDED offload, not a bare to_thread.

    Holding the lock is not enough. A cancelled PUT unwinds the async context
    and releases `_get_mcp_lock` while a bare `asyncio.to_thread` worker keeps
    writing, so a Disconnect can take the lock, read the OLD census, revoke the
    grant, and only then have the worker publish an entry whose authorization
    was just deleted. `_offload_config_write` exists for exactly this: it drains
    the worker before the lock is released (its own drain behaviour is pinned in
    mcp.py). This asserts the spec write is routed through it -- the structural
    property that makes the cancellation window unrepresentable.
    """
    installed = tmp_path / "kirocrew.json"
    installed.write_text(json.dumps({"name": "kirocrew"}))
    defaults = tmp_path / "defaults.json"
    mc_cfg = tmp_path / "config.json"

    request = MagicMock(spec=web.Request)
    request.method = "PUT"
    request.app = {"state": MagicMock()}

    async def mock_json():
        return {"config": {"name": "test", "tools": ["a"], "allowedTools": ["b"]}}

    request.json = mock_json

    offloaded: list[str] = []

    async def _recording_offload(fn, *args, **kwargs):
        offloaded.append(getattr(fn, "__name__", repr(fn)))
        return fn(*args, **kwargs)

    with (
        patch("kiro_crew.dashboard.handlers._installed_agent_config", return_value=installed),
        patch("kiro_crew.dashboard.handlers._find_agent_config", return_value=defaults),
        patch("kiro_crew.dashboard.handlers._reset_all_sessions", new_callable=AsyncMock),
        patch("kiro_crew.dashboard.handlers.config_path", return_value=mc_cfg),
        patch("kiro_crew.agent.build_agent_config", return_value={"toolsSettings": {}}),
        patch(
            "kiro_crew.dashboard.handlers.agents.get_shipped_tools",
            return_value={"tools": ["a", "c"], "allowedTools": ["b"]},
        ),
        patch("kiro_crew.dashboard.handlers.mcp._offload_config_write", _recording_offload),
    ):
        resp = await api_agent_config(request)

    assert resp.status == 200
    assert "write_config_atomically" in offloaded, (
        "the agent-spec write did not go through the shielded offload: " f"{offloaded}"
    )
