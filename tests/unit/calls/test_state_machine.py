from __future__ import annotations

import pytest

from telecraft.client.calls.state import CallState, assert_transition, can_transition


def test_state_machine_allows_expected_mvp_paths() -> None:
    assert can_transition(CallState.IDLE, CallState.RINGING_IN)
    assert can_transition(CallState.RINGING_IN, CallState.CONNECTING)
    assert can_transition(CallState.CONNECTING, CallState.IN_CALL)
    assert can_transition(CallState.IN_CALL, CallState.DISCONNECTING)
    assert can_transition(CallState.DISCONNECTING, CallState.ENDED)


def test_state_machine_blocks_invalid_jump() -> None:
    with pytest.raises(ValueError):
        assert_transition(CallState.IDLE, CallState.IN_CALL)


def test_terminal_states_are_terminal() -> None:
    assert not can_transition(CallState.ENDED, CallState.CONNECTING)
    assert not can_transition(CallState.FAILED, CallState.CONNECTING)
