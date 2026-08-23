"""The typed coordinator seam is injectable before it becomes authoritative."""

from __future__ import annotations

import asyncio
import inspect
import json
import threading
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.run_coordinator import (
    CommandOperation,
    CoordinatorDecision,
    MemoryRunCoordinator,
    OwnerLease,
    SQLiteRunCoordinator,
    SubmitRun,
)
from kiro_crew.subagent import SubagentInfo, SubagentManager
from kiro_crew.subagent_command_authority import AuthorityOutcomeUncertain


def test_subagent_manager_defaults_to_durable_sqlite_coordinator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "kiro_crew.subagent.SQLiteRunCoordinator",
        SQLiteRunCoordinator,
    )
    manager = SubagentManager(sessions=MagicMock(), ctx_builder=MagicMock())
    assert isinstance(manager._coordinator, SQLiteRunCoordinator)


def test_subagent_manager_preserves_injected_coordinator_identity() -> None:
    coordinator = MemoryRunCoordinator()
    manager = SubagentManager(
        sessions=MagicMock(),
        ctx_builder=MagicMock(),
        coordinator=coordinator,
    )
    assert manager._coordinator is coordinator
    assert manager.command_authority._coordinator is coordinator
    assert manager.command_authority._manager is manager


@pytest.mark.asyncio
async def test_accepted_run_is_submitted_after_legacy_admission() -> None:
    coordinator = MemoryRunCoordinator()
    manager = SubagentManager(
        sessions=MagicMock(),
        ctx_builder=MagicMock(),
        coordinator=coordinator,
    )
    info = SubagentInfo(
        id="run-1",
        task="redacted task",
        parent_session_key="parent-1",
        agent="researcher",
        model="served-model",
        reasoning_effort="high",
        allowed_tools=["Read"],
        bare=True,
        cwd="/tmp/project",
        silent=True,
        max_turns=7,
        keep=True,
        include_memory=False,
        include_lessons=True,
        include_project=False,
    )
    info._raw_task = "raw task"

    await manager._shadow_submit_accepted_run(info)

    run = await coordinator.get_run("run-1")
    assert run is not None
    assert run.task == "raw task"
    assert info._coordinator_fence is not None
    command = info._coordinator_command
    assert command is not None
    assert command.operation is CommandOperation.SPAWN
    payload = json.loads(command.payload_json)
    assert payload == {
        "agent": "researcher",
        "allowed_tools": ["Read"],
        "app": "",
        "bare": True,
        "batch_id": "",
        "batch_total": 0,
        "conversation_key": "",
        "cwd": "/tmp/project",
        "include_lessons": True,
        "include_memory": False,
        "include_project": False,
        "keep": True,
        "max_turns": 7,
        "model": "served-model",
        "operation": "spawn",
        "parent_session": "parent-1",
        "reasoning_effort": "high",
        "run_id": "run-1",
        "silent": True,
        "task": "raw task",
    }


@pytest.mark.asyncio
async def test_default_shadow_persists_accepted_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "kiro_crew.subagent.SQLiteRunCoordinator",
        SQLiteRunCoordinator,
    )
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    manager = SubagentManager(sessions=MagicMock(), ctx_builder=MagicMock())

    await manager._shadow_submit_accepted_run(SubagentInfo(id="durable-run", task="task"))

    stored = await SQLiteRunCoordinator(tmp_path / "run-coordinator/coordinator.db").get_run(
        "durable-run"
    )
    assert stored is not None
    assert stored.task == "task"


@pytest.mark.asyncio
async def test_manager_persists_process_identity_through_its_execution_fence() -> None:
    coordinator = MemoryRunCoordinator()
    manager = SubagentManager(
        sessions=MagicMock(),
        ctx_builder=MagicMock(),
        coordinator=coordinator,
    )
    await coordinator.submit(
        SubmitRun(
            run_id="process-run",
            command_id="process-command",
            idempotency_key="process-key",
            payload_hash="process-hash",
            parent_session="parent-1",
            agent="researcher",
            task="task",
            conversation_key="",
            operation=CommandOperation.SPAWN,
        )
    )
    claim = await coordinator.claim_command(
        "process-command",
        OwnerLease("executor", 9_999_999_999.0),
    )
    assert claim is not None
    info = SubagentInfo(id="process-run", task="task")
    info._coordinator_command = claim.command
    info._coordinator_fence = claim.fence
    info._coordinator_version = claim.run.version

    await manager._coordinator_mark_starting(info)
    await manager._coordinator_mark_running(info)
    await manager._coordinator_record_process(info, 4321, "start-1", True)

    stored = await coordinator.get_run("process-run")
    assert stored is not None
    assert stored.process_id == 4321
    assert stored.process_start_id == "start-1"
    assert stored.process_owned is True
    assert info._coordinator_version == stored.version


@pytest.mark.asyncio
async def test_continuation_submission_is_stable_and_idempotent() -> None:
    coordinator = MemoryRunCoordinator()
    manager = SubagentManager(
        sessions=MagicMock(),
        ctx_builder=MagicMock(),
        coordinator=coordinator,
    )
    info = SubagentInfo(
        id="run-2",
        task="follow up",
        parent_session_key="parent-1",
        conversation_key="subagent:original",
    )
    info._raw_task = "follow up"

    await manager._shadow_submit_accepted_run(info)
    await manager._shadow_submit_accepted_run(info)

    receipt = await coordinator.get_command_by_key("continue:run-2")
    assert receipt is not None
    assert receipt.command.operation is CommandOperation.CONTINUE
    assert receipt.command.command_id == "continue:run-2"
    assert info._coordinator_fence is not None


@pytest.mark.asyncio
async def test_shadow_submission_failure_preserves_legacy_execution() -> None:
    coordinator = AsyncMock()
    coordinator.submit.side_effect = RuntimeError("database unavailable")
    manager = SubagentManager(
        sessions=MagicMock(),
        ctx_builder=MagicMock(),
        coordinator=coordinator,
    )
    info = SubagentInfo(id="run-3", task="task")

    await manager._shadow_submit_accepted_run(info)

    coordinator.submit.assert_awaited_once()


@pytest.mark.asyncio
async def test_shadow_request_construction_failure_is_contained() -> None:
    coordinator = AsyncMock()
    manager = SubagentManager(
        sessions=MagicMock(),
        ctx_builder=MagicMock(),
        coordinator=coordinator,
    )
    info = SubagentInfo(id="run-invalid", task="task")
    info.allowed_tools = [{"not-json-serializable"}]  # type: ignore[list-item]

    await manager._shadow_submit_accepted_run(info)

    coordinator.submit.assert_not_awaited()


@pytest.mark.asyncio
async def test_starting_transition_retries_a_lost_commit_response() -> None:
    coordinator = AsyncMock()
    committed = MagicMock()
    committed.decision = CoordinatorDecision.UNCHANGED
    committed.value.version = 4
    coordinator.mark_starting.side_effect = [OSError("response lost"), committed]
    manager = SubagentManager(
        sessions=MagicMock(),
        ctx_builder=MagicMock(),
        coordinator=coordinator,
    )
    info = SubagentInfo(id="starting-response-lost", task="task")
    info._coordinator_command = MagicMock()
    info._coordinator_fence = MagicMock()
    info._coordinator_version = 3

    await manager._coordinator_mark_starting(info)

    assert coordinator.mark_starting.await_count == 2
    assert info._coordinator_version == 4
    assert info._coordinator_started is True


@pytest.mark.asyncio
async def test_running_transition_retries_a_lost_commit_response() -> None:
    coordinator = AsyncMock()
    committed = MagicMock()
    committed.decision = CoordinatorDecision.UNCHANGED
    committed.value.version = 5
    coordinator.mark_running.side_effect = [OSError("response lost"), committed]
    manager = SubagentManager(
        sessions=MagicMock(),
        ctx_builder=MagicMock(),
        coordinator=coordinator,
    )
    info = SubagentInfo(id="running-response-lost", task="task")
    info._coordinator_fence = MagicMock()
    info._coordinator_version = 4

    await manager._coordinator_mark_running(info)

    assert coordinator.mark_running.await_count == 2
    assert info._coordinator_version == 5
    assert info._coordinator_running is True


@pytest.mark.asyncio
async def test_unfenced_shadow_submission_aborts_before_execution() -> None:
    order: list[str] = []
    coordinator = AsyncMock()

    async def submit(_request: object) -> object:
        order.append("submit")
        return None

    coordinator.submit.side_effect = submit
    manager = SubagentManager(
        sessions=MagicMock(),
        ctx_builder=MagicMock(),
        coordinator=coordinator,
    )

    async def run_inner(_info: SubagentInfo, _session_key: str) -> None:
        order.append("execute")

    manager._run_inner = AsyncMock(side_effect=run_inner)
    manager._teardown_run_session = AsyncMock()
    manager._claim_finalize = MagicMock(return_value=False)

    info = SubagentInfo(id="run-4", task="task")
    await manager._run(info)

    assert order == ["submit"]
    assert info.done is True
    assert "coordinator execution fence is missing" in info.error
    manager._run_inner.assert_not_awaited()


@pytest.mark.asyncio
async def test_slow_shadow_submit_waits_without_cancelling_accepted_run() -> None:
    class DelayedSubmitCoordinator(MemoryRunCoordinator):
        def __init__(self) -> None:
            super().__init__()
            self.release_submit = asyncio.Event()
            self.submit_started = asyncio.Event()
            self.submit_cancelled = False

        async def submit(self, request):
            self.submit_started.set()
            try:
                await self.release_submit.wait()
            except asyncio.CancelledError:
                self.submit_cancelled = True
                raise
            return await super().submit(request)

    coordinator = DelayedSubmitCoordinator()
    on_done = AsyncMock()
    manager = SubagentManager(
        sessions=MagicMock(),
        ctx_builder=MagicMock(),
        coordinator=coordinator,
        on_done=on_done,
    )
    manager._run_inner = AsyncMock()
    manager._teardown_run_session = AsyncMock()
    manager._fire_event = AsyncMock()
    info = SubagentInfo(id="late-submit", task="task")
    manager._agents[info.id] = info

    run_task = asyncio.create_task(manager._run(info))
    await coordinator.submit_started.wait()
    await asyncio.sleep(0)

    assert run_task.done() is False
    assert coordinator.submit_cancelled is False
    manager._run_inner.assert_not_awaited()
    manager._fire_event.assert_not_awaited()
    on_done.assert_not_awaited()

    coordinator.release_submit.set()
    await run_task

    assert info._coordinator_claim_uncertain is False
    assert info._coordinator_fence is not None
    manager._run_inner.assert_awaited_once()


@pytest.mark.asyncio
async def test_slow_shadow_submit_failure_reports_one_terminal_failure() -> None:
    class FailingDelayedSubmitCoordinator(MemoryRunCoordinator):
        def __init__(self) -> None:
            super().__init__()
            self.release_submit = asyncio.Event()
            self.submit_started = asyncio.Event()

        async def submit(self, request):
            self.submit_started.set()
            await self.release_submit.wait()
            raise RuntimeError("sqlite write failed")

    coordinator = FailingDelayedSubmitCoordinator()
    on_done = AsyncMock()
    manager = SubagentManager(
        sessions=MagicMock(),
        ctx_builder=MagicMock(),
        coordinator=coordinator,
        on_done=on_done,
    )
    manager._run_inner = AsyncMock()
    manager._teardown_run_session = AsyncMock()
    manager._fire_event = AsyncMock()
    info = SubagentInfo(id="failed-late-submit", task="task")
    manager._agents[info.id] = info

    run_task = asyncio.create_task(manager._run(info))
    await coordinator.submit_started.wait()
    await asyncio.sleep(0)

    assert run_task.done() is False
    on_done.assert_not_awaited()

    coordinator.release_submit.set()
    await run_task
    if manager._report_tasks:
        await asyncio.gather(*manager._report_tasks)

    assert info._coordinator_claim_uncertain is False
    assert info._finalized is True
    assert "coordinator execution fence is missing" in info.error
    manager._fire_event.assert_awaited_once()
    on_done.assert_awaited_once()


@pytest.mark.asyncio
async def test_legacy_admission_awaits_coordinator_submit_and_claim() -> None:
    coordinator = MemoryRunCoordinator()
    manager = SubagentManager(
        sessions=MagicMock(),
        ctx_builder=MagicMock(),
        coordinator=coordinator,
    )
    info = SubagentInfo(id="durable-admission-fence", task="task")

    with patch(
        "kiro_crew.subagent.asyncio.wait_for",
        side_effect=AssertionError("pre-execution coordinator calls must not time out"),
    ):
        await manager._shadow_submit_accepted_run(info)

    assert info._coordinator_claim_uncertain is False
    assert info._coordinator_fence is not None
    assert info._coordinator_command is not None


@pytest.mark.asyncio
async def test_late_shadow_submit_failure_reopens_recovery_off_loop() -> None:
    manager = SubagentManager(sessions=MagicMock(), ctx_builder=MagicMock())
    info = SubagentInfo(id="late-shadow-failure", task="task")
    info._coordinator_claim_uncertain = True
    manager._claim_finalize = MagicMock(return_value=True)
    manager._record_cost = MagicMock()
    manager._spawn_terminal_report = MagicMock()
    clear_threads: list[int] = []

    with patch(
        "kiro_crew.subagent.clear_tombstone",
        side_effect=lambda _agent_id: clear_threads.append(threading.get_ident()),
    ):
        settlement = manager._resume_legacy_terminal_after_failed_shadow_submit(info)
        if inspect.isawaitable(settlement):
            await settlement

    assert clear_threads
    assert clear_threads != [threading.get_ident()]
    assert info._legacy_delivery_tombstone is True
    manager._spawn_terminal_report.assert_called_once()
    report_call = manager._spawn_terminal_report.call_args
    assert report_call.args == (info,)
    assert report_call.kwargs["tombstone_error_on_success"] is True


@pytest.mark.asyncio
async def test_shadow_fallback_tombstones_only_after_parent_report() -> None:
    on_done = AsyncMock()
    manager = SubagentManager(
        sessions=MagicMock(),
        ctx_builder=MagicMock(),
        on_done=on_done,
    )
    manager._fire_event = AsyncMock()
    manager._write_tombstone = MagicMock()
    info = SubagentInfo(id="reported-shadow-fallback", task="task")
    info.done = True
    info.error = "coordinator submission failed"

    await manager._report_terminal(
        info,
        source="Subagent",
        injection_timeout_reason="timed out",
        mark_delivered_on_success=False,
        tombstone_error_on_success=True,
    )

    on_done.assert_awaited_once_with(info)
    assert info._reported_to_parent is True
    manager._write_tombstone.assert_called_once_with(info, "error")


@pytest.mark.asyncio
async def test_shadow_fallback_defers_tombstone_when_parent_queues() -> None:
    async def queue_completion(info: SubagentInfo) -> None:
        info._delivery_queued = True

    manager = SubagentManager(
        sessions=MagicMock(),
        ctx_builder=MagicMock(),
        on_done=queue_completion,
    )
    manager._fire_event = AsyncMock()
    manager._write_tombstone = MagicMock()
    info = SubagentInfo(id="queued-shadow-fallback", task="task")
    info.done = True
    info.error = "coordinator submission failed"

    await manager._report_terminal(
        info,
        source="Subagent",
        injection_timeout_reason="timed out",
        mark_delivered_on_success=False,
        tombstone_error_on_success=True,
    )

    assert info._reported_to_parent is True
    manager._write_tombstone.assert_not_called()


@pytest.mark.asyncio
async def test_shadow_fallback_route_failure_stays_recoverable() -> None:
    async def fail_delivery(info: SubagentInfo) -> None:
        info._delivery_failed = True

    manager = SubagentManager(
        sessions=MagicMock(),
        ctx_builder=MagicMock(),
        on_done=fail_delivery,
    )
    manager._fire_event = AsyncMock()
    manager._write_tombstone = MagicMock()
    manager._settle_digest_holds = AsyncMock()
    info = SubagentInfo(id="failed-shadow-fallback", task="task")
    info.done = True
    info.error = "coordinator submission failed"

    await manager._report_terminal(
        info,
        source="Subagent",
        injection_timeout_reason="timed out",
        mark_delivered_on_success=False,
        settle_digest=True,
        tombstone_error_on_success=True,
    )

    assert info._reported_to_parent is False
    manager._write_tombstone.assert_not_called()
    manager._settle_digest_holds.assert_not_awaited()


@pytest.mark.asyncio
async def test_timed_out_shadow_claim_defers_terminal_delivery_to_recovery() -> None:
    class DelayedClaimCoordinator(MemoryRunCoordinator):
        def __init__(self) -> None:
            self.now = time.time()
            super().__init__(clock=lambda: self.now)
            self.release_claim = asyncio.Event()
            self.lookup_started = asyncio.Event()
            self.claim_task: asyncio.Task | None = None

        async def claim_command(self, command_id, owner):
            async def commit_claim():
                await self.release_claim.wait()
                return await super(DelayedClaimCoordinator, self).claim_command(
                    command_id,
                    owner,
                )

            self.claim_task = asyncio.create_task(commit_claim())
            raise asyncio.TimeoutError

        async def get_command_by_key(self, idempotency_key):
            self.lookup_started.set()
            return await super().get_command_by_key(idempotency_key)

    coordinator = DelayedClaimCoordinator()
    on_done = AsyncMock()
    manager = SubagentManager(
        sessions=MagicMock(),
        ctx_builder=MagicMock(),
        coordinator=coordinator,
        on_done=on_done,
    )
    manager._run_inner = AsyncMock()
    manager._teardown_run_session = AsyncMock()
    manager._fire_event = AsyncMock()
    info = SubagentInfo(
        id="late-claim",
        task="task",
        batch_id="batch-late",
        batch_total=2,
    )
    manager._agents[info.id] = info

    run_task = asyncio.create_task(manager._run(info))
    await asyncio.wait_for(coordinator.lookup_started.wait(), timeout=1)
    await run_task
    coordinator.release_claim.set()
    assert coordinator.claim_task is not None
    await coordinator.claim_task

    receipt = await coordinator.get_command_by_key("spawn:late-claim")
    assert receipt is not None
    assert receipt.command.status.value == "claimed"
    assert info._coordinator_claim_uncertain is True
    assert info._finalized is False
    assert manager.batch_members_pending("batch-late") is True
    manager._run_inner.assert_not_awaited()
    manager._fire_event.assert_not_awaited()
    on_done.assert_not_awaited()

    coordinator.now += 1_000
    claims = await coordinator.claim_recovery(
        OwnerLease("recovery", coordinator.now + 100),
        limit=1,
    )
    assert len(claims) == 1
    completion = manager._run_recovery.completion_for(claims[0].run)
    completed = await coordinator.complete(
        completion,
        claims[0].fence,
        claims[0].run.version,
    )
    assert completed.value is not None
    await manager._outbox_delivery.drain_once()

    manager._fire_event.assert_awaited_once()
    on_done.assert_awaited_once()
    delivered = on_done.await_args.args[0]
    assert delivered.batch_id == "batch-late"
    assert delivered.batch_total == 2


@pytest.mark.asyncio
async def test_timed_out_shadow_claim_resumes_from_stable_committed_fence() -> None:
    class ClaimBeforeLookupCoordinator(MemoryRunCoordinator):
        def __init__(self) -> None:
            super().__init__()
            self.release_claim = asyncio.Event()
            self.claim_task: asyncio.Task | None = None

        async def claim_command(self, command_id, owner):
            async def commit_claim():
                await self.release_claim.wait()
                return await super(ClaimBeforeLookupCoordinator, self).claim_command(
                    command_id,
                    owner,
                )

            self.claim_task = asyncio.create_task(commit_claim())
            raise asyncio.TimeoutError

        async def get_command_by_key(self, idempotency_key):
            self.release_claim.set()
            assert self.claim_task is not None
            await self.claim_task
            return await super().get_command_by_key(idempotency_key)

    coordinator = ClaimBeforeLookupCoordinator()
    manager = SubagentManager(
        sessions=MagicMock(),
        ctx_builder=MagicMock(),
        coordinator=coordinator,
    )
    manager._run_inner = AsyncMock()
    manager._teardown_run_session = AsyncMock()
    manager._claim_finalize = MagicMock(return_value=False)
    manager._start_coordinator_heartbeat = MagicMock()
    info = SubagentInfo(id="resolved-claim", task="task")

    await manager._run(info)

    assert info._coordinator_claim_uncertain is False
    assert info._coordinator_fence is not None
    assert info._coordinator_fence.owner_id == manager._coordinator_owner_id
    manager._run_inner.assert_awaited_once()


@pytest.mark.asyncio
async def test_legacy_admission_does_not_add_a_shorter_submit_timeout() -> None:
    coordinator = MemoryRunCoordinator()
    manager = SubagentManager(
        sessions=MagicMock(),
        ctx_builder=MagicMock(),
        coordinator=coordinator,
    )
    info = SubagentInfo(id="durable-admission", task="task")

    with patch(
        "kiro_crew.subagent.asyncio.wait_for",
        side_effect=AssertionError("shadow submit must use the coordinator deadline"),
    ):
        await manager._shadow_submit_accepted_run(info)

    assert await coordinator.get_run("durable-admission") is not None


@pytest.mark.asyncio
async def test_authoritatively_admitted_run_does_not_submit_twice() -> None:
    coordinator = AsyncMock()
    manager = SubagentManager(
        sessions=MagicMock(),
        ctx_builder=MagicMock(),
        coordinator=coordinator,
    )
    manager._run_inner = AsyncMock()
    manager._teardown_run_session = AsyncMock()
    manager._claim_finalize = MagicMock(return_value=False)
    manager._coordinator_mark_starting = AsyncMock()
    manager._start_coordinator_heartbeat = MagicMock()
    manager.command_authority.execution_started = AsyncMock()
    info = SubagentInfo(id="run-5", task="task", _coordinator_admitted=True)
    info._coordinator_fence = MagicMock()

    await manager._run(info)

    coordinator.submit.assert_not_awaited()
    manager._run_inner.assert_awaited_once()
    manager.command_authority.execution_started.assert_awaited_once_with("run-5")


@pytest.mark.asyncio
async def test_failed_start_settlement_keeps_waiting_run_retryable() -> None:
    manager = SubagentManager(sessions=MagicMock(), ctx_builder=MagicMock())
    manager._run_inner = AsyncMock()
    manager._teardown_run_session = AsyncMock()
    manager._claim_finalize = MagicMock(return_value=True)
    manager._coordinator_mark_starting = AsyncMock()
    manager._start_coordinator_heartbeat = MagicMock()
    manager.command_authority.execution_started = AsyncMock(
        side_effect=AuthorityOutcomeUncertain("write failed")
    )
    info = SubagentInfo(
        id="waiting-run",
        task="task",
        batch_id="waiting-wave",
        batch_total=3,
        _coordinator_admitted=True,
        _coordinator_waiting=True,
    )
    info._coordinator_fence = MagicMock()

    await manager._run(info)

    assert info._coordinator_waiting is True
    assert info._coordinator_claim_uncertain is True
    assert info.done is False
    assert manager._outbox_live_run_batches[info.id] == ("waiting-wave", 3)
    manager._run_inner.assert_not_awaited()
    manager._claim_finalize.assert_not_called()
    assert manager.command_authority.execution_started.await_count == 2


@pytest.mark.asyncio
async def test_failed_lifecycle_start_transition_never_applies_command() -> None:
    manager = SubagentManager(sessions=MagicMock(), ctx_builder=MagicMock())
    manager._run_inner = AsyncMock()
    manager._teardown_run_session = AsyncMock()
    manager._claim_finalize = MagicMock(return_value=True)
    manager._coordinator_mark_starting = AsyncMock(side_effect=OSError("write failed"))
    manager._start_coordinator_heartbeat = MagicMock()
    manager.command_authority.execution_started = AsyncMock()
    info = SubagentInfo(
        id="uncommitted-start",
        task="task",
        batch_id="starting-wave",
        batch_total=4,
        _coordinator_admitted=True,
        _coordinator_waiting=True,
    )
    info._coordinator_fence = MagicMock()

    await manager._run(info)

    assert info._coordinator_waiting is True
    assert info._coordinator_claim_uncertain is True
    assert info.done is False
    assert manager._outbox_live_run_batches[info.id] == ("starting-wave", 4)
    manager.command_authority.execution_started.assert_not_awaited()
    manager._run_inner.assert_not_awaited()
    manager._claim_finalize.assert_not_called()


@pytest.mark.asyncio
async def test_lost_start_settlement_response_reconciles_before_execution() -> None:
    manager = SubagentManager(sessions=MagicMock(), ctx_builder=MagicMock())
    manager._run_inner = AsyncMock()
    manager._teardown_run_session = AsyncMock()
    manager._claim_finalize = MagicMock(return_value=False)
    manager._coordinator_mark_starting = AsyncMock()
    manager._start_coordinator_heartbeat = MagicMock()
    manager.command_authority.execution_started = AsyncMock(
        side_effect=[AuthorityOutcomeUncertain("response lost"), None]
    )
    info = SubagentInfo(
        id="reconciled-start",
        task="task",
        _coordinator_admitted=True,
        _coordinator_waiting=True,
    )
    info._coordinator_fence = MagicMock()

    await manager._run(info)

    assert info._coordinator_waiting is False
    assert info._coordinator_claim_uncertain is False
    assert manager.command_authority.execution_started.await_count == 2
    manager._run_inner.assert_awaited_once()


@pytest.mark.asyncio
async def test_queued_cancel_keeps_authority_lease_until_terminal_commit() -> None:
    manager = SubagentManager(sessions=MagicMock(), ctx_builder=MagicMock())
    queued = {"_preassigned_id": "queued-run"}
    manager._unqueue = MagicMock(return_value=[queued])
    manager._finalize_queued_cancel = AsyncMock()
    manager.command_authority.stop_execution_heartbeat = AsyncMock()

    assert await manager.cancel("queued-run") is True

    manager.command_authority.stop_execution_heartbeat.assert_not_awaited()
    manager._finalize_queued_cancel.assert_awaited_once_with(queued)


@pytest.mark.asyncio
async def test_manager_shutdown_closes_command_authority() -> None:
    manager = SubagentManager(sessions=MagicMock(), ctx_builder=MagicMock())
    manager.command_authority.close = AsyncMock()

    await manager.cancel_all()

    manager.command_authority.close.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_cancelled_shutdown_readmits_unreported_terminal_owner() -> None:
    manager = SubagentManager(sessions=MagicMock(), ctx_builder=MagicMock())
    manager._cancel_all_impl = AsyncMock(side_effect=asyncio.CancelledError)
    report_task = asyncio.create_task(asyncio.Event().wait())
    info = SubagentInfo(id="unreported-shadow-fallback", task="task")
    manager._lifecycle.pending_reports = MagicMock(return_value={report_task})
    manager._lifecycle.owner_for = MagicMock(return_value=info)

    try:
        with patch("kiro_crew.subagent.clear_tombstone", return_value=True) as clear:
            with pytest.raises(asyncio.CancelledError):
                await manager.cancel_all()
        clear.assert_called_once_with(info.id)
    finally:
        report_task.cancel()
        await asyncio.gather(report_task, return_exceptions=True)


@pytest.mark.asyncio
async def test_manager_shutdown_retains_active_shadow_submission_for_recovery() -> None:
    class DelayedSubmitCoordinator(MemoryRunCoordinator):
        def __init__(self) -> None:
            super().__init__()
            self.release_submit = asyncio.Event()
            self.submit_started = asyncio.Event()
            self.submit_cancelled = asyncio.Event()

        async def submit(self, request):
            self.submit_started.set()
            try:
                await self.release_submit.wait()
            except asyncio.CancelledError:
                self.submit_cancelled.set()
                raise
            return await super().submit(request)

    coordinator = DelayedSubmitCoordinator()
    on_done = AsyncMock()
    manager = SubagentManager(
        sessions=MagicMock(),
        ctx_builder=MagicMock(),
        coordinator=coordinator,
        on_done=on_done,
    )
    manager.command_authority.close = AsyncMock()
    manager._run_inner = AsyncMock()
    manager._teardown_run_session = AsyncMock()
    manager._fire_event = AsyncMock()
    manager._write_tombstone = MagicMock()
    info = SubagentInfo(id="shutdown-shadow-submit", task="task")
    manager._agents[info.id] = info
    run_task = asyncio.create_task(manager._run(info))
    manager._tasks[info.id] = run_task
    await coordinator.submit_started.wait()

    shutdown_task = asyncio.create_task(manager.cancel_all())
    try:
        await run_task

        assert coordinator.submit_cancelled.is_set() is False
        assert shutdown_task.done() is False
    finally:
        coordinator.release_submit.set()
        await shutdown_task

    assert info._coordinator_fence is not None
    assert info._coordinator_claim_uncertain is True
    assert manager._coordinator_shadow_submits == set()
    on_done.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancelled_shutdown_readmits_unsettled_shadow_submission() -> None:
    class DelayedSubmitCoordinator(MemoryRunCoordinator):
        def __init__(self) -> None:
            super().__init__()
            self.submit_started = asyncio.Event()
            self.submit_cancelled = asyncio.Event()

        async def submit(self, request):
            self.submit_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.submit_cancelled.set()
                raise

    coordinator = DelayedSubmitCoordinator()
    manager = SubagentManager(
        sessions=MagicMock(),
        ctx_builder=MagicMock(),
        coordinator=coordinator,
        on_done=AsyncMock(),
    )
    manager.command_authority.close = AsyncMock()
    manager._run_inner = AsyncMock()
    manager._teardown_run_session = AsyncMock()
    manager._fire_event = AsyncMock()
    manager._write_tombstone = MagicMock()
    info = SubagentInfo(id="cancelled-shutdown-submit", task="task")
    manager._agents[info.id] = info
    run_task = asyncio.create_task(manager._run(info))
    manager._tasks[info.id] = run_task
    await coordinator.submit_started.wait()

    shutdown_task = asyncio.create_task(manager.cancel_all())
    await run_task
    for _ in range(100):
        if not manager._coordinator_shadow_submits:
            break
        await asyncio.sleep(0)
    assert manager._coordinator_shadow_submits == set()
    assert shutdown_task.done() is False

    with patch("kiro_crew.subagent.clear_tombstone", return_value=True) as clear:
        shutdown_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await shutdown_task

    assert coordinator.submit_cancelled.is_set()
    clear.assert_any_call(info.id)


@pytest.mark.asyncio
async def test_cancelled_shutdown_readmits_submit_before_drain_starts() -> None:
    class DelayedSubmitCoordinator(MemoryRunCoordinator):
        def __init__(self) -> None:
            super().__init__()
            self.release_submit = asyncio.Event()
            self.submit_started = asyncio.Event()

        async def submit(self, request):
            self.submit_started.set()
            await self.release_submit.wait()
            return await super().submit(request)

    coordinator = DelayedSubmitCoordinator()
    teardown_started = asyncio.Event()

    async def delayed_teardown(*_args) -> None:
        teardown_started.set()
        await asyncio.Event().wait()

    manager = SubagentManager(
        sessions=MagicMock(),
        ctx_builder=MagicMock(),
        coordinator=coordinator,
        on_done=AsyncMock(),
    )
    manager.command_authority.close = AsyncMock()
    manager._run_inner = AsyncMock()
    manager._teardown_run_session = AsyncMock(side_effect=delayed_teardown)
    manager._fire_event = AsyncMock()
    manager._write_tombstone = MagicMock()
    info = SubagentInfo(id="pre-drain-shutdown-submit", task="task")
    manager._agents[info.id] = info
    run_task = asyncio.create_task(manager._run(info))
    manager._tasks[info.id] = run_task
    await coordinator.submit_started.wait()

    shutdown_task = asyncio.create_task(manager.cancel_all())
    await teardown_started.wait()

    with patch("kiro_crew.subagent.clear_tombstone", return_value=True) as clear:
        shutdown_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await shutdown_task

    clear.assert_called_once_with(info.id)
    assert run_task.done()

    coordinator.release_submit.set()
    retained = list(manager._coordinator_shadow_submits)
    if retained:
        await asyncio.gather(*retained, return_exceptions=True)


@pytest.mark.asyncio
async def test_manager_shutdown_drains_retained_shadow_submission() -> None:
    manager = SubagentManager(sessions=MagicMock(), ctx_builder=MagicMock())
    manager.command_authority.close = AsyncMock()
    started = asyncio.Event()
    release = asyncio.Event()
    settled = asyncio.Event()

    async def retained_submit() -> None:
        started.set()
        await release.wait()
        settled.set()

    submit_task = asyncio.create_task(retained_submit())
    manager._coordinator_shadow_submits.add(submit_task)
    await started.wait()

    shutdown_task = asyncio.create_task(manager.cancel_all())
    await asyncio.sleep(0)

    assert submit_task.cancelled() is False
    assert shutdown_task.done() is False

    release.set()
    await shutdown_task

    assert settled.is_set()
    assert manager._coordinator_shadow_submits == set()
    manager.command_authority.close.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_manager_shutdown_drains_report_created_by_late_shadow_settlement() -> None:
    manager = SubagentManager(sessions=MagicMock(), ctx_builder=MagicMock())
    manager.command_authority.close = AsyncMock()
    submit_release = asyncio.Event()
    report_started = asyncio.Event()
    report_release = asyncio.Event()
    info = SubagentInfo(id="late-shadow-report", task="task")

    async def retained_submit() -> None:
        await submit_release.wait()

    async def late_report() -> None:
        report_started.set()
        await report_release.wait()

    submit_task = asyncio.create_task(retained_submit())
    manager._coordinator_shadow_submits.add(submit_task)

    def schedule_report(_done: asyncio.Task[None]) -> None:
        manager._lifecycle.spawn_report(info, late_report)

    submit_task.add_done_callback(schedule_report)
    shutdown_task = asyncio.create_task(manager.cancel_all())
    await asyncio.sleep(0)
    submit_release.set()
    await report_started.wait()

    assert shutdown_task.done() is False

    report_release.set()
    await shutdown_task


@pytest.mark.asyncio
async def test_process_identity_persistence_failure_aborts_before_execution(monkeypatch) -> None:
    sessions = MagicMock()
    sessions.get_pid.return_value = 4321
    manager = SubagentManager(
        sessions=sessions,
        ctx_builder=MagicMock(),
        coordinator=MemoryRunCoordinator(),
    )
    info = SubagentInfo(id="run-process", task="inspect")
    manager._coordinator_record_process = AsyncMock(side_effect=OSError("write failed"))
    monkeypatch.setattr("kiro_crew.subagent.platform_compat.process_start_time", lambda _pid: "s1")
    monkeypatch.setattr("kiro_crew.subagent.update_state", MagicMock())

    with pytest.raises(OSError, match="write failed"):
        await manager._record_process_identity(info, "subagent:run-process")

    assert info._pid == 4321


@pytest.mark.asyncio
async def test_process_identity_mirrors_legacy_state_off_loop(monkeypatch) -> None:
    sessions = MagicMock()
    sessions.get_pid.return_value = 4321
    manager = SubagentManager(
        sessions=sessions,
        ctx_builder=MagicMock(),
        coordinator=MemoryRunCoordinator(),
    )
    manager._coordinator_record_process = AsyncMock()
    info = SubagentInfo(id="run-process-off-loop", task="inspect")
    event_loop_thread = threading.get_ident()
    update_threads: list[int] = []
    monkeypatch.setattr(
        "kiro_crew.subagent.platform_compat.process_start_time",
        lambda _pid: "s1",
    )
    monkeypatch.setattr(
        "kiro_crew.subagent.update_state",
        lambda *_args, **_kwargs: update_threads.append(threading.get_ident()),
    )

    await manager._record_process_identity(info, "subagent:run-process-off-loop")

    assert update_threads
    assert update_threads != [event_loop_thread]
    manager._coordinator_record_process.assert_awaited_once_with(
        info,
        4321,
        "s1",
        True,
    )


@pytest.mark.asyncio
async def test_process_identity_requires_an_execution_fence() -> None:
    manager = SubagentManager(
        sessions=MagicMock(),
        ctx_builder=MagicMock(),
        coordinator=MemoryRunCoordinator(),
    )

    with pytest.raises(RuntimeError, match="execution fence is missing"):
        await manager._coordinator_record_process(
            SubagentInfo(id="unfenced-run", task="inspect"),
            4321,
            "start-1",
            True,
        )
