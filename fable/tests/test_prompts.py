"""Prompt loader: frontmatter stripping, slot resolution, failure modes."""

import pytest

from fable import prompts


class TestStripFrontmatter:
    def test_removes_leading_yaml_block(self):
        text = "---\nid: x\nversion: 1\n---\nBody line.\n"
        assert prompts.strip_frontmatter(text) == "Body line.\n"

    def test_leaves_text_without_frontmatter_alone(self):
        assert prompts.strip_frontmatter("Just a body.\n") == "Just a body.\n"

    def test_only_first_block_is_stripped(self):
        text = "---\na: 1\n---\nbody\n---\nnot frontmatter\n---\n"
        out = prompts.strip_frontmatter(text)
        assert out.startswith("body")
        assert "not frontmatter" in out


class TestResolveSlots:
    def test_caller_values_win_over_defaults(self):
        out = prompts.resolve_slots("root={{workspace_root}}", {"workspace_root": "/x"})
        assert out == "root=/x"

    def test_defaults_fill_unset_slots(self):
        out = prompts.resolve_slots("limit={{max_steps}}", {})
        assert out == "limit=7"

    def test_missing_slot_raises_loudly(self):
        with pytest.raises(ValueError, match="no_such_slot"):
            prompts.resolve_slots("{{no_such_slot}}", {})

    def test_whitespace_inside_braces_is_tolerated(self):
        assert prompts.resolve_slots("{{ max_steps }}", {}) == "7"


class TestLoad:
    def test_unknown_prompt_lists_available(self):
        with pytest.raises(FileNotFoundError, match="executor"):
            prompts.load("definitely_not_a_prompt")

    def test_executor_loads_with_static_override(self):
        text = prompts.load("executor", workspace_root="/srv/job-42")
        assert "/srv/job-42" in text
        assert "{{" not in text, "no placeholder may reach the model"

    @pytest.mark.parametrize(
        "name", ["orchestrator", "planner", "executor", "verifier", "researcher"]
    )
    def test_core_prompts_load_with_defaults_only(self, name):
        text = prompts.load(name)
        assert text.strip(), f"{name} loaded empty"
        assert "{{" not in text, f"{name} leaked a placeholder"
        assert not text.startswith("---"), f"{name} leaked frontmatter"

    def test_every_shipped_prompt_either_loads_or_fails_loudly(self):
        # The invariant: a {{placeholder}} must never silently reach the
        # model. Every prompt either resolves fully or raises ValueError.
        for path in prompts.prompts_dir().glob("*.md"):
            try:
                text = prompts.load(path.stem)
            except ValueError:
                continue  # loud failure is the contract for default-less slots
            assert "{{" not in text, f"{path.stem} leaked a placeholder silently"

    def test_loaded_prompt_is_byte_stable_across_calls(self):
        # Load-time resolution must be deterministic within a process --
        # a volatile byte here would zero the prompt-cache hit ratio.
        assert prompts.load("executor") == prompts.load("executor")
