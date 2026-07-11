"""Trace layer: JSONL round-trip, writer resilience, failure detectors."""

from pathlib import Path

from fable.trace import TraceEvent, TraceReader, TraceWriter, detect_failures


def event(turn, name, role="executor", **detail):
    return TraceEvent(
        ts="2026-07-08T00:00:00+00:00",
        elapsed_seconds=float(turn),
        turn=turn,
        event=name,
        role=role,
        detail=detail,
    )


def write_trace(tmp_path, events) -> TraceReader:
    path = tmp_path / "trace.jsonl"
    writer = TraceWriter(path)
    for e in events:
        writer.emit(e)
    return TraceReader(path)


class TestRoundTrip:
    def test_event_survives_serialization(self):
        original = event(3, "tool_result", tool="grep", exit_code=0)
        restored = TraceEvent.from_json_line(original.to_json_line())
        assert restored == original

    def test_jsonl_keys_match_the_documented_wire_names(self):
        import json
        line = json.loads(event(1, "run_end", status="ok").to_json_line())
        assert set(line) == {
            "timestamp", "elapsed_seconds", "turn", "action", "role", "details",
        }

    def test_writer_appends_and_reader_streams(self, tmp_path):
        reader = write_trace(tmp_path, [event(1, "a"), event(2, "b")])
        assert [e.event for e in reader.events()] == ["a", "b"]

    def test_failing_on_event_hook_never_kills_the_writer(self, tmp_path):
        def bad_hook(_):
            raise RuntimeError("dashboard crashed")

        writer = TraceWriter(tmp_path / "t.jsonl", bad_hook)
        writer.emit(event(1, "a"))  # must not raise
        assert (tmp_path / "t.jsonl").read_text().strip()


def ok_run(*middle):
    """A minimal healthy run: one decisive command, clean end."""
    return [
        event(1, "tool_result", tool="shell", args_hash="h1",
              exit_code=0, is_error=False, tool_use_id="t1"),
        *middle,
        event(9, "run_end", status="ok"),
    ]


class TestDetectors:
    def test_healthy_run_raises_no_flags(self, tmp_path):
        assert detect_failures(write_trace(tmp_path, ok_run())) == []

    def test_loop_stall_on_three_identical_calls(self, tmp_path):
        stall = [
            event(i, "tool_result", tool="grep", args_hash="same",
                  exit_code=0, is_error=False, tool_use_id=f"t{i}")
            for i in range(1, 4)
        ]
        findings = detect_failures(write_trace(tmp_path, ok_run(*stall)))
        assert any(f["mode"] == "loop_stall" for f in findings)

    def test_loop_stall_on_stall_rail_trip(self, tmp_path):
        events = ok_run(event(5, "rail_trip", rail="stall"))
        findings = detect_failures(write_trace(tmp_path, events))
        assert any(f["mode"] == "loop_stall" for f in findings)

    def test_error_laundering_when_failure_never_retried_to_success(self, tmp_path):
        events = ok_run(
            event(5, "tool_result", tool="shell", args_hash="deploy",
                  exit_code=1, is_error=True, tool_use_id="t5"),
        )
        findings = detect_failures(write_trace(tmp_path, events))
        assert any(f["mode"] == "error_laundering" for f in findings)

    def test_no_error_laundering_when_retry_succeeded(self, tmp_path):
        events = ok_run(
            event(5, "tool_result", tool="shell", args_hash="deploy",
                  exit_code=1, is_error=True, tool_use_id="t5"),
            event(6, "tool_result", tool="shell", args_hash="deploy",
                  exit_code=0, is_error=False, tool_use_id="t6"),
        )
        findings = detect_failures(write_trace(tmp_path, events))
        assert not any(f["mode"] == "error_laundering" for f in findings)

    def test_verification_skipping_when_no_exit_code_ever_seen(self, tmp_path):
        events = [
            event(1, "tool_result", tool="read_file", args_hash="r1",
                  exit_code=None, is_error=False, tool_use_id="t1"),
            event(2, "run_end", status="ok_unverified"),
        ]
        findings = detect_failures(write_trace(tmp_path, events))
        assert any(f["mode"] == "verification_skipping" for f in findings)

    def test_verification_skipping_when_ok_run_ran_no_tools_at_all(self, tmp_path):
        # An ok run with ZERO tool_results is the strongest verification
        # skipping: nothing was exercised. It must flag even with no
        # tool_result events present (the old 'and tool_results' clause
        # wrongly suppressed this case).
        events = [event(1, "run_end", status="ok")]
        findings = detect_failures(write_trace(tmp_path, events))
        assert any(f["mode"] == "verification_skipping" for f in findings)

    def test_fabricated_status_on_failed_claim_audit(self, tmp_path):
        events = ok_run(
            event(5, "gate_check", checks=["claims"],
                  failures=["claim:tests-pass"], failure_details=[]),
        )
        findings = detect_failures(write_trace(tmp_path, events))
        assert any(f["mode"] == "fabricated_status" for f in findings)

    def test_premature_completion_after_plan_gate_only(self, tmp_path):
        events = [
            event(1, "gate_check", checks=["plan_gate"], failures=[]),
            event(2, "tool_result", tool="shell", args_hash="h",
                  exit_code=0, is_error=False, tool_use_id="t1"),
            event(3, "run_end", status="ok_unverified"),
        ]
        findings = detect_failures(write_trace(tmp_path, events))
        assert any(f["mode"] == "premature_completion" for f in findings)

    def test_scope_drift_and_test_tampering_from_diff_scope_details(self, tmp_path):
        events = ok_run(
            event(5, "gate_check", checks=["diff_scope"], failures=["diff_scope"],
                  failure_details=[
                      {"name": "diff_scope",
                       "detail": "out-of-scope file touches: src/unrelated.py; "
                                 "test/CI files modified: tests/test_gate.py"},
                  ]),
        )
        findings = detect_failures(write_trace(tmp_path, events))
        modes = {f["mode"] for f in findings}
        assert "scope_drift" in modes
        assert "test_tampering" in modes
