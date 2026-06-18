"""Property-based test for Property 12: include_meaningful_output Flag.

# Feature: conversation-logger, Property 12: include_meaningful_output Flag

**Validates: Requirements 8.4, 8.5**

Property: *For any* tool action, when `include_meaningful_output` is false, the generated
Tool_Summary SHALL contain only the tool name, description, and outcome — no output excerpts
SHALL appear.

Test strategy:
- Generate random actions with meaningful output content (error messages, stderr, etc.)
- Test with include_meaningful_output=False: verify the summary contains ONLY
  `[{actionType}] {description} → {outcome}` on a single line (no newline, no appended excerpts)
- Compare with include_meaningful_output=True for the same action to show the difference
  when meaningful output exists
"""

import json
import re

from hypothesis import given, settings
from hypothesis import strategies as st

from kiro_ception.tool_summaries import ToolSummaryConfig, generate_tool_summary


# --- Strategies ---

# Action types that are NOT excluded and NOT "say"
_ACTION_TYPES = st.sampled_from(
    ["readFile", "writeFile", "fs_write", "grep_search", "file_search",
     "execute_pwsh", "invoke_sub_agent", "unknown_tool", "custom_action"]
)

_ACTION_STATES = st.sampled_from(["completed", "failed", "error"])

# Meaningful output indicators that will be recognized by the heuristic
_MEANINGFUL_INDICATORS = [
    "error", "Error", "ERROR", "fail", "FAIL", "warning", "Warning",
    "WARN", "test", "assert", "exception", "Exception", "traceback",
    "Traceback", "npm ERR", "PASSED", "FAILED", "passed", "failed",
]


@st.composite
def meaningful_text(draw: st.DrawFn) -> str:
    """Generate text that contains at least one meaningful indicator."""
    indicator = draw(st.sampled_from(_MEANINGFUL_INDICATORS))
    prefix = draw(st.text(min_size=5, max_size=50, alphabet=st.characters(
        categories=("L", "N", "P", "Z"), exclude_characters="\x00"
    )))
    suffix = draw(st.text(min_size=5, max_size=200, alphabet=st.characters(
        categories=("L", "N", "P", "Z"), exclude_characters="\x00"
    )))
    return f"{prefix}{indicator}{suffix}"


@st.composite
def action_with_meaningful_output(draw: st.DrawFn) -> dict:
    """Generate an action dict that has meaningful output content.

    Ensures the action has error/stderr/stdout with meaningful indicators so that
    include_meaningful_output=True would actually append content.
    """
    action_type = draw(_ACTION_TYPES)
    action_state = draw(_ACTION_STATES)

    # Build input based on action type
    if action_type == "readFile":
        input_data = {"path": f"/src/{draw(st.text(min_size=1, max_size=30, alphabet='abcdefghijklmnopqrstuvwxyz_'))}.ts"}
    elif action_type in ("writeFile", "fs_write"):
        input_data = {"path": f"/src/{draw(st.text(min_size=1, max_size=30, alphabet='abcdefghijklmnopqrstuvwxyz_'))}.ts"}
    elif action_type in ("grep_search", "file_search"):
        input_data = {"query": draw(st.text(min_size=1, max_size=20, alphabet="abcdefghijklmnopqrstuvwxyz"))}
    elif action_type == "execute_pwsh":
        input_data = {"command": draw(st.text(min_size=1, max_size=80, alphabet="abcdefghijklmnopqrstuvwxyz -_./"))}
    elif action_type == "invoke_sub_agent":
        input_data = {
            "name": draw(st.text(min_size=1, max_size=20, alphabet="abcdefghijklmnopqrstuvwxyz-")),
            "prompt": draw(st.text(min_size=1, max_size=80, alphabet="abcdefghijklmnopqrstuvwxyz ")),
        }
    else:
        input_data = {"param": draw(st.text(min_size=1, max_size=30, alphabet="abcdefghijklmnopqrstuvwxyz"))}

    # Build output with meaningful content in one of the recognized fields
    output_field = draw(st.sampled_from(["error", "stderr", "stdout"]))
    meaningful_content = draw(meaningful_text())

    output_data: dict = {}
    output_data[output_field] = meaningful_content

    # For grep/file_search, add results count
    if action_type in ("grep_search", "file_search"):
        output_data["results"] = list(range(draw(st.integers(min_value=0, max_value=10))))

    # For execute_pwsh, add exit code
    if action_type == "execute_pwsh":
        output_data["exitCode"] = draw(st.integers(min_value=0, max_value=127))

    return {
        "actionType": action_type,
        "actionState": action_state,
        "input": input_data,
        "output": output_data,
    }


# --- Property Test ---

@settings(max_examples=100)
@given(action=action_with_meaningful_output())
def test_prop12_no_output_excerpts_when_flag_disabled(action: dict):
    """Property 12: When include_meaningful_output is False, the summary contains
    ONLY [actionType] description → outcome on a single line with no appended excerpts.

    # Feature: conversation-logger, Property 12: include_meaningful_output Flag
    **Validates: Requirements 8.4, 8.5**
    """
    config_disabled = ToolSummaryConfig(include_meaningful_output=False)
    summary = generate_tool_summary(action, config_disabled)

    # Should not be None (not a say action, not excluded)
    assert summary is not None, "Summary should not be None for a valid non-say, non-excluded action"

    # Key verification 1: No newlines — meaningful output is appended after a newline
    assert "\n" not in summary, (
        f"Summary should not contain newlines when include_meaningful_output=False.\n"
        f"Got: {summary!r}"
    )

    # Key verification 2: Matches exactly the pattern [type] desc → outcome
    # with no trailing content beyond the outcome
    pattern = re.compile(r"^\[.+\] .+ → .+$")
    assert pattern.match(summary), (
        f"Summary should match pattern '[{{type}}] {{desc}} → {{outcome}}' exactly.\n"
        f"Got: {summary!r}"
    )

    # Key verification 3: No meaningful output excerpts should appear after the base line.
    # The outcome portion may legitimately contain truncated error text for failed/error states
    # (per Req 2.9), but _extract_meaningful_output content should NOT be appended.
    # Since we already verified no newlines exist (verification 1), the meaningful output
    # which is always appended after a newline cannot be present. Additionally verify that
    # stderr/stdout content (not used in the outcome) does not leak through.
    output_data = action.get("output", {})
    action_state = action.get("actionState", "")

    # For stderr/stdout fields: when the action state is "completed", these should
    # never appear in the summary since they are output excerpts.
    # However, for failed/error actions, stderr IS legitimately used as the error
    # message in the outcome portion (per Req 2.9), so we only check stdout leakage
    # for failed/error actions.
    fields_to_check = ["stderr", "stdout"] if action_state == "completed" else ["stdout"]
    for field in fields_to_check:
        field_value = output_data.get(field, "")
        if field_value and isinstance(field_value, str) and len(field_value) > 30:
            # Use a distinctive middle chunk to avoid false positives
            chunk = field_value[15:60]
            if chunk and len(chunk) > 5:
                # Avoid false positives: if the chunk also appears in the input data
                # (e.g., file path or query), it's not a leak from output
                input_str = json.dumps(action.get("input", {}))
                if chunk in input_str:
                    continue
                assert chunk not in summary, (
                    f"Meaningful {field} content should not leak into summary when disabled.\n"
                    f"Leaked chunk: {chunk!r}\n"
                    f"Summary: {summary!r}"
                )


@settings(max_examples=100)
@given(action=action_with_meaningful_output())
def test_prop12_compare_with_flag_enabled(action: dict):
    """Property 12 (comparative): When include_meaningful_output is True, the summary
    for the same action should differ (contain additional content) compared to when
    it is False, demonstrating the flag actually controls output inclusion.

    # Feature: conversation-logger, Property 12: include_meaningful_output Flag
    **Validates: Requirements 8.4, 8.5**
    """
    config_disabled = ToolSummaryConfig(include_meaningful_output=False)
    config_enabled = ToolSummaryConfig(include_meaningful_output=True)

    summary_disabled = generate_tool_summary(action, config_disabled)
    summary_enabled = generate_tool_summary(action, config_enabled)

    assert summary_disabled is not None
    assert summary_enabled is not None

    # When meaningful output exists, the enabled version should be longer
    # (it appends a newline + meaningful content or "(no meaningful output)")
    assert len(summary_enabled) > len(summary_disabled), (
        f"With include_meaningful_output=True, summary should be longer than with False.\n"
        f"Enabled ({len(summary_enabled)} chars): {summary_enabled!r}\n"
        f"Disabled ({len(summary_disabled)} chars): {summary_disabled!r}"
    )

    # The disabled version should be a prefix of the enabled version
    # (before the newline that separates the base summary from output)
    assert summary_disabled in summary_enabled, (
        f"The disabled summary should be a substring of the enabled summary.\n"
        f"Disabled: {summary_disabled!r}\n"
        f"Enabled: {summary_enabled!r}"
    )
