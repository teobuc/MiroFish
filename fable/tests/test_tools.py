"""Tool registry: schema generation, execution semantics, containment."""

from pathlib import Path

import pytest

from fable.client import UsageLedger
from fable.tools import Tool, ToolRegistry, execute, fs_tools, shell_tool, tool


@tool(parallel_safe=True)
def add(a: int, b: int = 0) -> str:
    """Add two integers. Call when the user asks for a sum."""
    return str(a + b)


@tool
def boom(message: str) -> str:
    """Always raises. Exists to test error capture."""
    raise RuntimeError(message)


@tool
def quiet() -> str:
    """Returns nothing, to test the empty-output sentinel."""
    return ""


@tool(parallel_safe=True)
def typed(flag: bool, names: list[str], note: str | None = None) -> str:
    """Exercise bool, list, and optional hints in one signature."""
    return f"{flag}:{','.join(names)}:{note}"


class TestToolDecorator:
    def test_schema_from_type_hints(self):
        schema = add.input_schema
        assert schema["type"] == "object"
        assert schema["properties"]["a"] == {"type": "integer"}
        assert schema["required"] == ["a"]  # b has a default
        assert schema["additionalProperties"] is False

    def test_bool_list_and_optional_hints(self):
        props = typed.input_schema["properties"]
        assert props["flag"] == {"type": "boolean"}
        assert props["names"] == {"type": "array", "items": {"type": "string"}}
        assert props["note"] == {"type": "string"}  # X | None unwraps to X
        assert typed.input_schema["required"] == ["flag", "names"]

    def test_docstring_becomes_description_verbatim(self):
        assert add.description.startswith("Add two integers.")

    def test_to_api_sets_strict_on_the_tool_definition(self):
        wire = add.to_api()
        assert wire["strict"] is True
        assert set(wire) == {"name", "description", "input_schema", "strict"}

    def test_missing_docstring_is_a_hard_error(self):
        with pytest.raises(ValueError, match="docstring"):
            @tool
            def undocumented(x: str) -> str:  # pragma: no cover - never built
                return x

    def test_var_args_are_rejected(self):
        with pytest.raises(ValueError, match=r"\*args"):
            @tool
            def splat(*parts: str) -> str:  # pragma: no cover - never built
                """Docstring present, signature still illegal."""
                return "".join(parts)


class TestRegistry:
    def test_register_get_and_unknown(self):
        registry = ToolRegistry([add])
        assert registry.get("add") is add
        with pytest.raises(KeyError):
            registry.get("missing")

    def test_freeze_is_deterministic(self):
        registry = ToolRegistry([add, typed])
        assert registry.freeze() == registry.freeze()


def _execute(tool_uses, registry, tmp_path):
    return execute(
        tool_uses,
        registry,
        scratch=tmp_path / "scratch",
        ledger=UsageLedger(),
        cap_chars=200,
    )


class TestExecute:
    def test_results_come_back_in_block_order_with_matching_ids(self, tmp_path):
        registry = ToolRegistry([add, quiet])
        blocks = _execute(
            [
                {"type": "tool_use", "id": "toolu_1", "name": "quiet", "input": {}},
                {"type": "tool_use", "id": "toolu_2", "name": "add",
                 "input": {"a": 2, "b": 3}},
            ],
            registry, tmp_path,
        )
        assert [b["tool_use_id"] for b in blocks] == ["toolu_1", "toolu_2"]
        assert all(b["type"] == "tool_result" for b in blocks)
        assert blocks[1]["content"] == "5"

    def test_tool_exception_becomes_error_observation_not_crash(self, tmp_path):
        blocks = _execute(
            [{"type": "tool_use", "id": "t", "name": "boom",
              "input": {"message": "kaput"}}],
            ToolRegistry([boom]), tmp_path,
        )
        assert blocks[0]["is_error"] is True
        assert "RuntimeError" in blocks[0]["content"]
        assert "kaput" in blocks[0]["content"]

    def test_unknown_tool_reports_available_tools(self, tmp_path):
        blocks = _execute(
            [{"type": "tool_use", "id": "t", "name": "ghost", "input": {}}],
            ToolRegistry([add]), tmp_path,
        )
        assert blocks[0]["is_error"] is True
        assert "add" in blocks[0]["content"]

    def test_empty_output_gets_explicit_sentinel(self, tmp_path):
        blocks = _execute(
            [{"type": "tool_use", "id": "t", "name": "quiet", "input": {}}],
            ToolRegistry([quiet]), tmp_path,
        )
        assert blocks[0]["is_error"] is False
        assert blocks[0]["content"]  # never an empty string back to the model

    def test_oversized_output_spills_to_scratch(self, tmp_path):
        @tool
        def firehose() -> str:
            """Emit far more than the cap, to test spill shaping."""
            return "x" * 10_000

        blocks = _execute(
            [{"type": "tool_use", "id": "toolu_big", "name": "firehose",
              "input": {}}],
            ToolRegistry([firehose]), tmp_path,
        )
        content = blocks[0]["content"]
        assert len(content) < 10_000, "shaped output must be smaller than raw"
        spilled = list((tmp_path / "scratch").glob("tool-toolu_big*"))
        assert spilled, "full raw output must land in scratch"
        assert spilled[0].read_text().count("x") == 10_000

    def test_string_json_input_is_parsed_not_matched(self, tmp_path):
        blocks = _execute(
            [{"type": "tool_use", "id": "t", "name": "add",
              "input": '{"a": 40, "b": 2}'}],
            ToolRegistry([add]), tmp_path,
        )
        assert blocks[0]["content"] == "42"


class TestFsToolsContainment:
    def test_read_inside_root_works(self, tmp_path):
        (tmp_path / "hello.txt").write_text("salut\n")
        registry = ToolRegistry(fs_tools(root=tmp_path))
        blocks = _execute(
            [{"type": "tool_use", "id": "t", "name": "read_file",
              "input": {"file_path": str(tmp_path / "hello.txt")}}],
            registry, tmp_path,
        )
        assert blocks[0]["is_error"] is False
        assert "salut" in blocks[0]["content"]

    def test_path_traversal_outside_root_is_an_error_observation(self, tmp_path):
        registry = ToolRegistry(fs_tools(root=tmp_path / "jail"))
        (tmp_path / "jail").mkdir()
        blocks = _execute(
            [{"type": "tool_use", "id": "t", "name": "read_file",
              "input": {"file_path": str(tmp_path / "outside.txt")}}],
            registry, tmp_path,
        )
        assert blocks[0]["is_error"] is True

    def test_glob_absolute_base_outside_root_is_rejected(self, tmp_path):
        (tmp_path / "jail").mkdir()
        (tmp_path / "secret.txt").write_text("classified\n")
        registry = ToolRegistry(fs_tools(root=tmp_path / "jail"))
        blocks = _execute(
            [{"type": "tool_use", "id": "t", "name": "glob",
              "input": {"pattern": "*.txt", "base_dir": str(tmp_path)}}],
            registry, tmp_path,
        )
        assert blocks[0]["is_error"] is True
        assert "escapes the workspace root" in blocks[0]["content"]
        assert "classified" not in blocks[0]["content"]

    def test_grep_relative_traversal_out_of_root_is_rejected(self, tmp_path):
        (tmp_path / "jail").mkdir()
        (tmp_path / "secret.txt").write_text("password=hunter2\n")
        registry = ToolRegistry(fs_tools(root=tmp_path / "jail"))
        blocks = _execute(
            [{"type": "tool_use", "id": "t", "name": "grep_search",
              "input": {"pattern": "password", "search_path": "..",
                        "file_glob": "*.txt"}}],
            registry, tmp_path,
        )
        assert blocks[0]["is_error"] is True
        assert "escapes the workspace root" in blocks[0]["content"]
        assert "hunter2" not in blocks[0]["content"]

    def test_glob_pattern_traversal_out_of_root_is_filtered(self, tmp_path):
        (tmp_path / "jail").mkdir()
        (tmp_path / "secret.txt").write_text("classified\n")
        registry = ToolRegistry(fs_tools(root=tmp_path / "jail"))
        blocks = _execute(
            [{"type": "tool_use", "id": "t", "name": "glob",
              "input": {"pattern": "../*.txt"}}],
            registry, tmp_path,
        )
        # '..' in the pattern must not leak a file living outside the root.
        assert "secret.txt" not in blocks[0]["content"]


class TestShellAllowlist:
    def test_metacharacter_injection_is_blocked(self, tmp_path):
        marker = tmp_path / "pwned"
        registry = ToolRegistry([shell_tool(allowlist=["echo"])])
        blocks = _execute(
            [{"type": "tool_use", "id": "t", "name": "shell",
              "input": {"command": f"echo hi; touch {marker}"}}],
            registry, tmp_path,
        )
        # echo ran (its own argv), but the chained 'touch' never executed.
        assert blocks[0]["is_error"] is False
        assert not marker.exists(), "shell=False must not run the injected command"
        assert "touch" in blocks[0]["content"]  # echoed literally, not executed

    def test_allowed_command_still_runs(self, tmp_path):
        registry = ToolRegistry([shell_tool(allowlist=["echo"])])
        blocks = _execute(
            [{"type": "tool_use", "id": "t", "name": "shell",
              "input": {"command": "echo hello"}}],
            registry, tmp_path,
        )
        assert blocks[0]["is_error"] is False
        assert "hello" in blocks[0]["content"]
        assert "[exit code: 0]" in blocks[0]["content"]

    def test_disallowed_first_token_is_rejected(self, tmp_path):
        registry = ToolRegistry([shell_tool(allowlist=["echo"])])
        blocks = _execute(
            [{"type": "tool_use", "id": "t", "name": "shell",
              "input": {"command": "rm -rf /"}}],
            registry, tmp_path,
        )
        assert blocks[0]["is_error"] is True
        assert "allowlist" in blocks[0]["content"]

    def test_empty_command_in_allowlist_mode_is_rejected(self, tmp_path):
        registry = ToolRegistry([shell_tool(allowlist=["echo"])])
        blocks = _execute(
            [{"type": "tool_use", "id": "t", "name": "shell",
              "input": {"command": "   "}}],
            registry, tmp_path,
        )
        assert blocks[0]["is_error"] is True
        assert "Empty command" in blocks[0]["content"]
