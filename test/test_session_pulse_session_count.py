"""The survey "new user" counter increments only on genuine user chats.

``DashboardState.get_or_create_slot`` is the sole place a brand-new slot is
minted. This asserts the durable session-pulse counter goes up by one only when
the caller both opts in via ``count_user_session=True`` (the human request-layer
paths: new-chat tab, fork) AND the new slot's origin is ``SlotOrigin.USER``.
Either conjunct alone must not count: origin=USER without the flag is the
agent-driven session-control create verb (#6139), and the flag without USER
origin is an app/cron/system slot the survey must never see.
"""

from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from chat_test_helpers import _make_ready_kiro_prerequisite

from kiro_crew.dashboard import session_pulse_counter as spc
from kiro_crew.dashboard.state import DashboardState, SlotOrigin
from kiro_crew.history import ConversationLog

_SRC = Path(__file__).resolve().parents[1] / "src" / "kiro_crew" / "dashboard"


@pytest.fixture(autouse=True)
def _isolated_counter(tmp_path, monkeypatch: pytest.MonkeyPatch):
    counter_dir = tmp_path / "home"
    counter_dir.mkdir()
    # Both the counter module and state resolve config_dir(); point both at a
    # throwaway dir so the counter file and any open-slots snapshot stay local.
    monkeypatch.setattr(spc, "config_dir", lambda: counter_dir)
    import kiro_crew.dashboard.state as state_mod

    monkeypatch.setattr(state_mod, "config_dir", lambda: counter_dir, raising=False)
    return counter_dir


def _make_state(tmp_path) -> DashboardState:
    sessions = MagicMock(count=0)
    sessions.remove = AsyncMock()
    sessions.recycle_background = AsyncMock()
    sessions.get_pid = MagicMock(return_value=None)
    state = DashboardState(
        sessions=sessions,
        crons=MagicMock(list_jobs=MagicMock(return_value=[]), status=MagicMock(return_value={})),
        lessons=MagicMock(load_all=MagicMock(return_value=[])),
        start_time=0.0,
        conversation_log=ConversationLog(base_dir=tmp_path / "log"),
    )
    state.kiro_prerequisite_service = _make_ready_kiro_prerequisite()
    return state


def test_user_origin_new_chat_with_flag_increments(tmp_path) -> None:
    state = _make_state(tmp_path)
    assert spc.get_user_session_count() == 0
    state.get_or_create_slot(origin=SlotOrigin.USER, count_user_session=True)
    assert spc.get_user_session_count() == 1
    state.get_or_create_slot(origin=SlotOrigin.USER, count_user_session=True)
    assert spc.get_user_session_count() == 2


def test_user_origin_without_flag_does_not_increment(tmp_path) -> None:
    # THE regression pinned by #6139: the session-control create verb mints
    # brand-new slots with origin=SlotOrigin.USER (the tag carries slots:user
    # privacy semantics and cannot change) but does NOT opt in to the counter.
    # An agent opening sessions unattended must not satisfy the survey's
    # eligibility window on its own. This is exactly the session-control call
    # shape: unnamed slot, origin=USER, flag left at its default.
    state = _make_state(tmp_path)
    state.get_or_create_slot(None, agent="some-agent", origin=SlotOrigin.USER)
    assert spc.get_user_session_count() == 0
    # Mutation guard: dropping the ``count_user_session`` conjunct from the
    # increment condition in state.py makes this fail.
    state.get_or_create_slot(origin=SlotOrigin.USER)
    assert spc.get_user_session_count() == 0


@pytest.mark.parametrize(
    "kwargs",
    [
        {"origin": SlotOrigin.CRON},
        {"origin": SlotOrigin.SYSTEM},
        {"app": "some-app"},  # resolves to APP origin
        {},  # untagged (origin="")
    ],
)
def test_flag_without_user_origin_does_not_increment(tmp_path, kwargs) -> None:
    # The origin conjunct is the invariant floor: a caller can never count a
    # non-USER slot, even when it passes the flag. Mutation guard: dropping the
    # ``slot._origin == SlotOrigin.USER`` conjunct makes this fail.
    state = _make_state(tmp_path)
    state.get_or_create_slot(count_user_session=True, **kwargs)
    assert spc.get_user_session_count() == 0


@pytest.mark.parametrize(
    "kwargs",
    [
        {"origin": SlotOrigin.CRON},
        {"origin": SlotOrigin.SYSTEM},
        {"app": "some-app"},
        {},
    ],
)
def test_non_user_origins_do_not_increment(tmp_path, kwargs) -> None:
    state = _make_state(tmp_path)
    state.get_or_create_slot(**kwargs)
    assert spc.get_user_session_count() == 0


def test_restore_shape_named_user_slot_does_not_increment(tmp_path) -> None:
    # Restore/rehydrate calls get_or_create_slot with the persisted key as
    # `name` and origin=USER. That must NOT count -- otherwise every gateway
    # restart re-counts each restored user session. Regression for the GPT
    # blocking finding "restoring sessions corrupts the durable session count".
    # The flag does not override this: even an opted-in caller addressing a
    # named (non-minted) slot stays uncounted.
    state = _make_state(tmp_path)
    slot = state.get_or_create_slot(
        name="chat-9-1786589233", origin=SlotOrigin.USER, count_user_session=True
    )
    assert spc.get_user_session_count() == 0
    # Returning the now-existing slot also does not count.
    again = state.get_or_create_slot(
        name="chat-9-1786589233", origin=SlotOrigin.USER, count_user_session=True
    )
    assert again is slot
    assert spc.get_user_session_count() == 0


# ---------------------------------------------------------------------------
# Structural pins on the call sites. The behavioral tests above exercise
# get_or_create_slot directly; these pin WHICH callers opt in, so mutating a
# call site (e.g. passing True at the session-control create verb, or dropping
# the flag from a human path) fails a test without needing a full HTTP stack.
# ---------------------------------------------------------------------------


def _calls_with_flag(module: str) -> list[str]:
    """Return each ``get_or_create_slot(...)`` call in *module* that passes
    ``count_user_session=True`` (comments stripped so prose can name the flag)."""
    text = (_SRC / module).read_text(encoding="utf-8")
    code = "\n".join(re.sub(r"#.*$", "", ln) for ln in text.splitlines())
    calls = []
    for m in re.finditer(r"get_or_create_slot\(", code):
        # Balance parens from the call's opening paren to slice the call text.
        depth, i = 0, m.end() - 1
        while i < len(code):
            if code[i] == "(":
                depth += 1
            elif code[i] == ")":
                depth -= 1
                if depth == 0:
                    break
            i += 1
        calls.append(code[m.start() : i + 1])
    return [c for c in calls if re.search(r"count_user_session\s*=\s*True", c)]


def test_session_control_create_does_not_opt_in() -> None:
    # The fix for #6139 IS this absence: the session-control create verb mints
    # USER-origin slots (privacy semantics) but must not count toward the
    # survey. Passing count_user_session=True there re-introduces the bug.
    assert _calls_with_flag("session_control.py") == []


def test_only_human_request_paths_opt_in() -> None:
    # Exactly the three human request-layer paths carry the flag: the chat-send
    # auto-create and the new-chat tab (chat_handlers.py), and fork
    # (chat_fork.py). A flag appearing anywhere else in the dashboard package,
    # or disappearing from these, is a deliberate decision -- update this pin
    # alongside it.
    assert len(_calls_with_flag("chat_handlers.py")) == 2
    assert len(_calls_with_flag("chat_fork.py")) == 1
    for module in (
        "cron_inject.py",
        "chat_persistence.py",
        "channel_slots.py",
        "session_transfer.py",
        "workflow_inject.py",
        "openai_compat.py",
        "handlers/cron.py",
        "handlers/taskrunner.py",
    ):
        assert _calls_with_flag(module) == [], f"{module} must not opt in"
