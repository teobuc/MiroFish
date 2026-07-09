"""End-to-end loop tests against a scripted fake client — zero network.

``fable.loop`` imports ``FableClient`` by name, so monkeypatching
``fable.loop.FableClient`` swaps the model for a script of ModelTurns while
every other part of the machine (registry, execute, ledger, trace, rails)
runs for real.
"""

import json

import pytest

import fable.loop as loop_module
from fable import run
from fable.client import ModelTurn, UsageLedger
from fable.config import Budget, FableConfig
from fable.tools import tool
from fable.trace import TraceReader

USAGE = {
    "input_tokens": 1_000,
    "output_tokens": 100,
    "cache_creation_input_tokens": 0,
    "cache_read_input_tokens": 0,
}


def text_turn(text, usage=USAGE):
    return ModelTurn(
        stop_reason="end_turn",
        content=({"type": "text", "text": text},),
        usage=dict(usage),
        cost_usd=0.01,
    )


def tool_turn(name, args, tool_use_id="toolu_1", usage=USAGE):
    return ModelTurn(
        stop_reason="tool_use",
        content=(
            {"type": "text", "text": f"calling {name}"},
            {"type": "tool_use", "id": tool_use_id, "name": name, "input": args},
        ),
        usage=dict(usage),
        cost_usd=0.01,
    )


class FakeClient:
    """Pops scripted turns; records what the loop sent it."""

    script: list = []
    calls: list = []

    def __init__(self, config: FableConfig, ledger: UsageLedger | None = None):
        self._config = config
        self._ledger = ledger if ledger is not None else UsageLedger()

    @property
    def ledger(self):
        return self._ledger

    @property
    def config(self):
        return self._config

    def call(self, *, prefix, messages, role="executor", policy=None,
             output_schema=None):
        FakeClient.calls.append(
            {"messages": [dict(m) for m in messages], "role": role,
             "output_schema": output_schema}
        )
        turn = (
            FakeClient.script.pop(0) if FakeClient.script else text_turn("done")
        )
        tier = self._config.tiers["strong"]
        self._ledger.record(turn.usage, tier, role)
        return turn


@pytest.fixture(autouse=True)
def fake_client(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)  # .fable/{scratch,traces} land in tmp
    monkeypatch.setattr(loop_module, "FableClient", FakeClient)
    FakeClient.script = []
    FakeClient.calls = []
    yield FakeClient


@tool
def write_note(content: str) -> str:
    """Write a note file. Call to persist a short note to disk."""
    from pathlib import Path
    Path("note.txt").write_text(content)
    return "written"


class TestHappyPaths:
    def test_immediate_answer_is_ok_unverified(self, tmp_path):
        FakeClient.script = [text_turn("the answer is 42")]
        result = run("what is the answer?")
        assert result.status == "ok_unverified"
        assert result.output == "the answer is 42"
        assert result.turns == 0
        assert result.cost_usd > 0
        assert result.trace_path.exists()
        events = [e.event for e in TraceReader(result.trace_path).events()]
        assert events[0] == "run_start"
        assert events[-1] == "run_end"

    def test_tool_turn_executes_and_results_ride_one_user_message(self, tmp_path):
        FakeClient.script = [
            tool_turn("write_note", {"content": "hello"}),
            text_turn("noted"),
        ]
        result = run("write a note", tools=[write_note])
        assert result.status == "ok_unverified"
        assert result.turns == 1
        assert (tmp_path / "note.txt").read_text() == "hello"
        # The second model call must see: assistant tool_use turn, then
        # exactly ONE user message whose content is the tool_result blocks.
        second_call = FakeClient.calls[1]["messages"]
        assert second_call[-2]["role"] == "assistant"
        last = second_call[-1]
        assert last["role"] == "user"
        blocks = last["content"]
        assert all(b["type"] == "tool_result" for b in blocks)
        assert blocks[0]["tool_use_id"] == "toolu_1"
        assert blocks[0]["is_error"] is False


class TestFailureContainment:
    def test_refusal_never_touches_content(self):
        FakeClient.script = [
            ModelTurn(stop_reason="refusal", content=(), usage=dict(USAGE),
                      cost_usd=0.0, stop_details={"category": "cyber"}),
        ]
        result = run("anything")
        assert result.status == "refusal"
        assert "cyber" in result.output

    def test_max_turns_rail_stops_a_tool_happy_model(self):
        # A model that would call tools forever must hit the turn rail.
        FakeClient.script = [
            tool_turn("write_note", {"content": f"n{i}"}, tool_use_id=f"t{i}")
            for i in range(10)
        ]
        result = run("loop forever", tools=[write_note], max_turns=2)
        assert result.status == "max_turns"

    def test_stall_detector_catches_identical_repeated_calls(self):
        # The detector needs a FULL window (stall_window=8) before it
        # judges; 10 identical calls guarantees a verdict.
        FakeClient.script = [
            tool_turn("write_note", {"content": "same"}, tool_use_id=f"t{i}")
            for i in range(10)
        ] + [text_turn("never reached")]
        result = run("busy loop", tools=[write_note])
        assert result.status == "stalled"

    def test_max_tokens_truncation_recovers_then_gives_up(self):
        truncated = ModelTurn(
            stop_reason="max_tokens",
            content=({"type": "text", "text": "partial..."},),
            usage=dict(USAGE), cost_usd=0.01,
        )
        FakeClient.script = [truncated, text_turn("recovered")]
        result = run("long output")
        assert result.status == "ok_unverified"
        assert result.output == "recovered"
        # The recovery message must be an operator continuation, and the
        # partial assistant content must be preserved in the transcript.
        second = FakeClient.calls[1]["messages"]
        assert second[-1]["role"] == "user"
        assert "truncated" in second[-1]["content"].lower()
        assert second[-2]["role"] == "assistant"


class TestContextPressure:
    def test_warn_threshold_injects_mid_conversation_system_message(self):
        # First turn reports usage past warn_pct (60% of the 1M window);
        # the next assembled request must carry a {"role": "system"}
        # operator message that is NOT messages[0].
        big_usage = dict(USAGE, input_tokens=700_000)
        FakeClient.script = [
            tool_turn("write_note", {"content": "x"}, usage=big_usage),
            text_turn("done"),
        ]
        # budget high enough that the pressure ladder, not the budget rail,
        # is what reacts to the huge context.
        result = run("heavy context", tools=[write_note], budget_usd=50.0)
        assert result.status == "ok_unverified"
        second = FakeClient.calls[1]["messages"]
        system_positions = [
            i for i, m in enumerate(second) if m["role"] == "system"
        ]
        assert system_positions, "expected an operator pressure warning"
        assert 0 not in system_positions, "system message must never lead"

    def test_budget_rail_projects_before_spending(self):
        # Tiny budget: the projected next call exceeds it immediately after
        # the first (already expensive) turn.
        expensive = dict(USAGE, input_tokens=900_000, output_tokens=8_000)
        FakeClient.script = [
            tool_turn("write_note", {"content": "x"}, usage=expensive),
            text_turn("never reached"),
        ]
        result = run("spendy", tools=[write_note], budget_usd=0.05)
        assert result.status == "budget_exceeded"
        assert "$" in result.output


class TestTraceIntegration:
    def test_trace_carries_tool_evidence_for_the_audit(self):
        FakeClient.script = [
            tool_turn("write_note", {"content": "hello"}),
            text_turn("noted"),
        ]
        result = run("write a note", tools=[write_note])
        reader = TraceReader(result.trace_path)
        tool_results = [e for e in reader.events() if e.event == "tool_result"]
        assert tool_results, "tool executions must be traced"
        detail = tool_results[0].detail
        assert detail["tool"] == "write_note"
        assert detail["is_error"] is False
        assert detail["args_hash"]

    def test_on_event_hook_sees_the_run_live(self):
        seen = []
        FakeClient.script = [text_turn("done")]
        run("observe me", on_event=seen.append)
        assert [e.event for e in seen][0] == "run_start"
        assert [e.event for e in seen][-1] == "run_end"
