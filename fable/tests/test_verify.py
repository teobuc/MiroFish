"""Regression tests for the verify.py security/contract fixes.

Covered:
  #1 _read_artifacts containment  -- model-supplied paths that escape the
     workspace are REJECTED unread (host-file exfiltration guard).
  #2 refute_vote strict majority  -- an even-k tie does NOT survive.
  #3 assert_red infra rejection   -- a timeout/infra exit is not counted red.
  #4 audit_claims decisiveness     -- a claim citing only non-decisive tool
     calls (no exit code) is UNGROUNDED.

Fully offline: no network, no real FableClient. The refuter path is driven
against a scripted fake exposing only ``.structured``.
"""

import threading

import pytest

from fable.verify import (
    _read_artifacts,
    assert_red,
    audit_claims,
    refute_vote,
)


# --------------------------------------------------------------------------- #
# Fakes


class _ScriptedClient:
    """Fake FableClient exposing only ``structured``; pops scripted dicts.

    Thread-safe because refute_vote fans out across a ThreadPoolExecutor.
    """

    def __init__(self, responses):
        self._responses = list(responses)
        self._lock = threading.Lock()

    def structured(self, *, prefix, messages, role, schema):
        with self._lock:
            return self._responses.pop(0)


class _FakeLedger:
    """Minimal stand-in for UsageLedger exposing only evidence_for."""

    def __init__(self, records):
        self._records = records

    def evidence_for(self, tool_use_id):
        return self._records.get(tool_use_id)


def _record(exit_code=None, is_error=False):
    return {"tool_use_id": "t", "name": "x", "args_hash": "",
            "result_hash": "", "exit_code": exit_code, "is_error": is_error,
            "raw_output_path": None}


def _refutation(found):
    return {"refutation_found": found, "counterexample": "c",
            "expected_vs_actual": "e"}


# --------------------------------------------------------------------------- #
# #2 refute_vote: strict majority, even-k tie must NOT survive


def test_refute_vote_even_tie_does_not_survive():
    # k=4, exactly 2 refuters find a counterexample: a tie is not a majority
    # finding no counterexample, so the artifact must NOT survive.
    client = _ScriptedClient(
        [_refutation(True), _refutation(True),
         _refutation(False), _refutation(False)]
    )
    survived, refutations = refute_vote([], "claim", client=client, k=4)
    assert survived is False
    assert len(refutations) == 2


def test_refute_vote_strict_majority_survives():
    # k=4, only 1 refuter finds a counterexample: 3/4 clean is a strict
    # majority, so the artifact survives (every counterexample still returned).
    client = _ScriptedClient(
        [_refutation(True), _refutation(False),
         _refutation(False), _refutation(False)]
    )
    survived, refutations = refute_vote([], "claim", client=client, k=4)
    assert survived is True
    assert len(refutations) == 1


def test_refute_vote_odd_k_majority():
    # k=3, 2 refuters find a counterexample -> minority survive -> not survived.
    client = _ScriptedClient(
        [_refutation(True), _refutation(True), _refutation(False)]
    )
    survived, _ = refute_vote([], "claim", client=client, k=3)
    assert survived is False


# --------------------------------------------------------------------------- #
# #3 assert_red: only exit 1 is red; timeouts/infra are rejected


def test_assert_red_timeout_is_not_red(monkeypatch):
    import fable.verify as verify

    monkeypatch.setattr(verify, "_run_fresh",
                        lambda cmd, cwd: (124, "TIMEOUT after 600s"))
    ev = assert_red("pytest -q tests/test_new.py", "/nonexistent")
    assert ev.passed is False
    assert ev.exit_code == 124
    assert "INFRA FAILURE" in ev.detail


def test_assert_red_genuine_failure_is_red(monkeypatch):
    import fable.verify as verify

    monkeypatch.setattr(verify, "_run_fresh",
                        lambda cmd, cwd: (1, "1 failed"))
    ev = assert_red("pytest -q", "/w")
    assert ev.passed is True
    assert "RED" in ev.detail


def test_assert_red_pass_is_vacuous(monkeypatch):
    import fable.verify as verify

    monkeypatch.setattr(verify, "_run_fresh", lambda cmd, cwd: (0, "passed"))
    ev = assert_red("pytest -q", "/w")
    assert ev.passed is False
    assert "VACUOUS" in ev.detail


def test_assert_red_no_tests_collected_is_vacuous(monkeypatch):
    import fable.verify as verify

    monkeypatch.setattr(verify, "_run_fresh",
                        lambda cmd, cwd: (5, "no tests ran"))
    ev = assert_red("pytest -q", "/w")
    assert ev.passed is False
    assert "VACUOUS" in ev.detail


def test_assert_red_usage_error_is_infra(monkeypatch):
    import fable.verify as verify

    # pytest exit 2/3/4 (interrupt/internal/usage) is infra, not a red test.
    monkeypatch.setattr(verify, "_run_fresh",
                        lambda cmd, cwd: (4, "usage error"))
    ev = assert_red("pytest -q", "/w")
    assert ev.passed is False
    assert "INFRA FAILURE" in ev.detail


# --------------------------------------------------------------------------- #
# #1 _read_artifacts containment


def test_read_artifacts_contained_file_is_read(tmp_path):
    (tmp_path / "report.txt").write_text("hello contained", encoding="utf-8")
    out = _read_artifacts(["report.txt"], workspace=tmp_path)
    assert "hello contained" in out
    assert "REJECTED" not in out


def test_read_artifacts_rejects_absolute_escape(tmp_path):
    secret = tmp_path.parent / "secret.txt"
    secret.write_text("TOP SECRET", encoding="utf-8")
    out = _read_artifacts([str(secret)], workspace=tmp_path)
    assert "REJECTED" in out
    assert "TOP SECRET" not in out


def test_read_artifacts_rejects_dotdot_escape(tmp_path):
    secret = tmp_path.parent / "secret.txt"
    secret.write_text("TOP SECRET", encoding="utf-8")
    out = _read_artifacts(["../secret.txt"], workspace=tmp_path)
    assert "REJECTED" in out
    assert "TOP SECRET" not in out


def test_read_artifacts_rejects_symlink_escape(tmp_path):
    secret = tmp_path.parent / "secret.txt"
    secret.write_text("TOP SECRET", encoding="utf-8")
    link = tmp_path / "link.txt"
    link.symlink_to(secret)
    out = _read_artifacts(["link.txt"], workspace=tmp_path)
    assert "REJECTED" in out
    assert "TOP SECRET" not in out


def test_read_artifacts_no_workspace_reads_absolute(tmp_path):
    # With no workspace supplied, containment is not applied (unchanged
    # behavior for internal callers that pass trusted paths).
    f = tmp_path / "a.txt"
    f.write_text("trusted", encoding="utf-8")
    out = _read_artifacts([f])
    assert "trusted" in out


# --------------------------------------------------------------------------- #
# #4 audit_claims decisiveness


def test_audit_claims_nondecisive_only_is_ungrounded():
    # A "tests pass" claim citing only a read/think (exit_code None) is not
    # certified by a decisive check -> UNGROUNDED failure.
    ledger = _FakeLedger({"tool-1": _record(exit_code=None, is_error=False)})
    report = {"claims": [{"text": "tests passing", "tool_use_ids": ["tool-1"]}]}
    result = audit_claims(report, ledger)
    assert result.passed is False
    assert "UNGROUNDED" in result.failures[0].detail


def test_audit_claims_decisive_success_is_grounded():
    ledger = _FakeLedger({"tool-1": _record(exit_code=0, is_error=False)})
    report = {"claims": [{"text": "tests passing", "tool_use_ids": ["tool-1"]}]}
    result = audit_claims(report, ledger)
    assert result.passed is True


def test_audit_claims_nonzero_exit_still_fails():
    ledger = _FakeLedger({"tool-1": _record(exit_code=1, is_error=False)})
    report = {"claims": [{"text": "tests passing", "tool_use_ids": ["tool-1"]}]}
    result = audit_claims(report, ledger)
    assert result.passed is False


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
