"""File-based memory: lessons, dedupe, recall, redaction, containment."""

import pytest

from fable.memory import Checkpoint, ContainmentError, Memory, redact


@pytest.fixture
def memory(tmp_path):
    return Memory(root=tmp_path / "mem")


class TestAddLesson:
    def test_lesson_file_starts_with_trigger_line(self, memory):
        path = memory.add_lesson(
            "when pytest passes locally but the gate fails, check cwd",
            "The gate runs from the repo root; pytest ran from tests/.",
        )
        first = path.read_text().splitlines()[0]
        assert first.startswith("# when pytest passes locally")

    def test_duplicate_body_dedupes_to_existing_file(self, memory):
        first = memory.add_lesson("trigger one", "same body text")
        second = memory.add_lesson("trigger two", "  SAME   body\ntext  ")
        assert second == first, "normalized-identical bodies must dedupe"

    def test_duplicate_trigger_dedupes_to_existing_file(self, memory):
        first = memory.add_lesson("same trigger", "body a")
        second = memory.add_lesson("same  trigger", "body b entirely different")
        assert second == first

    def test_multiline_trigger_is_forced_to_one_line(self, memory):
        path = memory.add_lesson("line one\nline two", "body")
        assert "\n" not in path.read_text().splitlines()[0].lstrip("# ")

    def test_body_capped_at_thirty_lines(self, memory):
        body = "\n".join(f"line {i}" for i in range(100))
        path = memory.add_lesson("long lesson", body)
        lines = path.read_text().splitlines()
        assert len(lines) <= 31  # trigger line + 30
        assert "truncated" in lines[-1]

    def test_add_lesson_persists_redacted_body(self, memory):
        # Integration: add_lesson -> _write -> redact -> disk. A secret in a
        # lesson body must never survive to the file (memory files get
        # committed and pasted into prompts).
        path = memory.add_lesson(
            "when a leaked credential shows up in a traceback",
            "The bug was a hard-coded api_key = sk_live_abcdefghijklmnop line.",
        )
        written = path.read_text()
        assert "sk_live_abcdefghijklmnop" not in written
        assert "[REDACTED]" in written

    def test_slug_colliding_triggers_do_not_overwrite(self, memory):
        # Two DIFFERENT triggers whose first 40 chars are identical must land
        # in separate files instead of silently overwriting each other.
        base = "when the very same long shared prefix appears here"
        first = memory.add_lesson(base + " in case ALPHA", "body alpha")
        second = memory.add_lesson(base + " in case BRAVO", "body bravo entirely")
        assert first != second
        assert first.exists() and second.exists()
        assert "alpha" in first.read_text()
        assert "bravo" in second.read_text()


class TestRecall:
    def test_recall_hits_by_regex_with_path_and_line(self, memory):
        memory.add_lesson("cache misses", "check for timestamps in the prefix")
        hits = memory.recall(r"timestamps?")
        assert "lessons/" in hits
        assert "timestamps" in hits

    def test_recall_miss_says_so(self, memory):
        assert "no memory matches" in memory.recall("zzz_nothing")

    def test_bad_pattern_raises_value_error(self, memory):
        with pytest.raises(ValueError, match="Bad recall pattern"):
            memory.recall("([unclosed")


class TestRedact:
    def test_kv_secrets_are_masked(self):
        out = redact("api_key = sk_live_abcdefghijklmnop")
        assert "sk_live" not in out
        assert "[REDACTED]" in out

    def test_pem_blocks_are_masked(self):
        pem = (
            "-----BEGIN PRIVATE KEY-----\nMIIEvQIBADANBg\n-----END PRIVATE KEY-----"
        )
        assert "MIIEvQIBADANBg" not in redact(pem)

    def test_plain_text_is_untouched(self):
        text = "the parser splits on commas"
        assert redact(text) == text


class TestCheckpoint:
    def test_to_markdown_carries_every_section(self):
        cp = Checkpoint(
            goal="ship the widget",
            decisions=("chose sqlite because zero-ops",),
            files_touched=("src/widget.py",),
            verified_done=("tests pass [toolu_1]",),
            open_issues=("flaky test_io",),
            next_steps=("wire CLI", "write docs", "tag release"),
            lessons=("check cwd before pytest",),
        )
        md = cp.to_markdown()
        for needle in (
            "ship the widget", "sqlite", "src/widget.py", "toolu_1",
            "flaky test_io", "wire CLI", "check cwd",
        ):
            assert needle in md

    def test_checkpoint_roundtrip_via_memory(self, memory):
        cp = Checkpoint(
            goal="g", decisions=(), files_touched=(), verified_done=(),
            open_issues=(), next_steps=("a", "b", "c"), lessons=(),
        )
        path = memory.checkpoint(cp)
        assert path.exists()
        assert "## Goal" in path.read_text()


class TestMarkPassed:
    def test_secret_shaped_blueprint_persists_valid_json_and_marks_by_index(
        self, memory
    ):
        # Regression: a secret-shaped step action must (1) not corrupt
        # feature_list.json when redacted, and (2) still be markable passed.
        # Redacting the serialized blob ate the closing quote -> invalid JSON;
        # marking by step.action (redacted on disk) raised KeyError. The fix
        # redacts per field and marks by index. This drives the real code path.
        import json

        from fable.config import FableConfig
        from fable.loop import Agent, Blueprint, Step

        agent = Agent(memory=memory)
        blueprint = Blueprint(steps=[
            Step(id="s1",
                 action="Deploy after setting api_key = sk_live_abcdefghijklmnop",
                 verifier="pytest -q"),
            Step(id="s2", action="Write the summary", verifier="judgment"),
        ])
        agent._persist_blueprint(blueprint, FableConfig())

        path = memory.state_dir / "feature_list.json"
        raw = path.read_text(encoding="utf-8")
        stored = json.loads(raw)                      # must NOT raise (valid JSON)
        assert "sk_live_abcdefghijklmnop" not in raw  # secret redacted
        assert [f["passes"] for f in stored] == [False, False]

        # Marking flips every step's passes to true, keyed by index.
        agent._mark_blueprint_passed(blueprint, ())
        stored = json.loads(path.read_text(encoding="utf-8"))
        assert all(f["passes"] is True for f in stored)

    def test_mark_passed_unknown_feature_raises(self, memory):
        import json

        (memory.state_dir / "feature_list.json").write_text(
            json.dumps([]), encoding="utf-8"
        )
        with pytest.raises(KeyError):
            memory.mark_passed("nope", ["ev1"])


class TestContainmentAndScratch:
    def test_escaping_the_root_raises(self, memory):
        with pytest.raises(ContainmentError):
            memory._contained("../outside.md")

    def test_spill_writes_under_scratch_and_slugs_the_hint(self, memory):
        path = memory.spill("big content", "Firehose OUTPUT!!")
        assert path.exists()
        assert "firehose-output" in path.name
        assert path.read_text() == "big content"
