"""Tests for Iteration 6: webhook ingestion, signature, dedupe, normalization,
guardrails, patcher, and loop prevention."""

import hashlib
import hmac
import json
import os
import sys
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

# Ensure API env vars exist before any webhook module import
os.environ.setdefault("EVIDENCE_BUCKET", "test-evidence-bucket")
os.environ.setdefault("INCIDENTS_TABLE", "test-incidents-table")
os.environ.setdefault("SNAPSHOTS_TABLE", "test-snapshots-table")
os.environ.setdefault("PACKETS_TABLE", "test-packets-table")
os.environ.setdefault("AWS_REGION", "us-east-1")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "services", "api"))

# ── Fixtures ──────────────────────────────────────────────────────

WEBHOOK_SECRET = "test-secret-123"

SAMPLE_PR_COMMENT_EVENT = {
    "action": "created",
    "issue": {
        "number": 7,
        "html_url": "https://github.com/owner/repo/pull/7",
        "pull_request": {
            "html_url": "https://github.com/owner/repo/pull/7",
        },
    },
    "comment": {
        "body": "Please fix spelling in opsrunbook/KAN-4.md",
        "html_url": "https://github.com/owner/repo/pull/7#issuecomment-123",
    },
    "repository": {
        "full_name": "owner/repo",
    },
    "installation": {"id": 12345},
    "sender": {"login": "human-reviewer"},
}

SAMPLE_REVIEW_COMMENT_EVENT = {
    "action": "created",
    "pull_request": {
        "number": 7,
        "html_url": "https://github.com/owner/repo/pull/7",
    },
    "comment": {
        "body": "This line has a typo",
        "html_url": "https://github.com/owner/repo/pull/7#discussion_r456",
        "path": "src/main.py",
        "position": 5,
        "original_position": 5,
        "line": 10,
        "original_line": 10,
        "side": "RIGHT",
        "diff_hunk": "@@ -8,3 +8,3 @@ def main():\n-    print('helo')\n+    print('hello')",
    },
    "repository": {
        "full_name": "owner/repo",
    },
    "installation": {"id": 12345},
    "sender": {"login": "human-reviewer"},
}

SAMPLE_REVIEW_EVENT = {
    "action": "submitted",
    "review": {
        "body": "Looks good, a few nits",
        "html_url": "https://github.com/owner/repo/pull/7#pullrequestreview-789",
        "state": "changes_requested",
    },
    "pull_request": {
        "number": 7,
        "html_url": "https://github.com/owner/repo/pull/7",
    },
    "repository": {
        "full_name": "owner/repo",
    },
    "installation": {"id": 12345},
    "sender": {"login": "human-reviewer"},
}


def _sign(body: bytes, secret: str) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


# ── 1. Signature verification ────────────────────────────────────

class TestSignatureVerification:
    def test_valid_signature(self):
        from src.routers.webhooks import _verify_signature

        payload = b'{"hello":"world"}'
        sig = _sign(payload, WEBHOOK_SECRET)
        assert _verify_signature(payload, sig, WEBHOOK_SECRET) is True

    def test_invalid_signature(self):
        from src.routers.webhooks import _verify_signature

        payload = b'{"hello":"world"}'
        assert _verify_signature(payload, "sha256=bad", WEBHOOK_SECRET) is False

    def test_missing_signature(self):
        from src.routers.webhooks import _verify_signature

        assert _verify_signature(b"data", "", WEBHOOK_SECRET) is False
        assert _verify_signature(b"data", None, WEBHOOK_SECRET) is False

    def test_missing_secret(self):
        from src.routers.webhooks import _verify_signature

        assert _verify_signature(b"data", "sha256=abc", "") is False

    def test_no_sha256_prefix(self):
        from src.routers.webhooks import _verify_signature

        assert _verify_signature(b"data", "md5=abc", WEBHOOK_SECRET) is False


# ── 2. Event normalization ────────────────────────────────────────

class TestNormalization:
    def test_issue_comment_normalization(self):
        from src.routers.webhooks import _normalize_event

        result = _normalize_event("issue_comment", "dlv-001", SAMPLE_PR_COMMENT_EVENT)

        assert result["schema_version"] == "github_pr_review_event.v1"
        assert result["delivery_id"] == "dlv-001"
        assert result["event_type"] == "issue_comment"
        assert result["pr_number"] == 7
        assert result["repo_full_name"] == "owner/repo"
        assert result["sender_login"] == "human-reviewer"
        assert "fix spelling" in result["comment_body"]
        assert result["inline_context"] is None
        assert result["review_state"] is None

    def test_review_comment_normalization(self):
        from src.routers.webhooks import _normalize_event

        result = _normalize_event(
            "pull_request_review_comment", "dlv-002", SAMPLE_REVIEW_COMMENT_EVENT
        )

        assert result["event_type"] == "pull_request_review_comment"
        assert result["pr_number"] == 7
        assert result["comment_body"] == "This line has a typo"
        assert result["inline_context"] is not None
        assert result["inline_context"]["path"] == "src/main.py"
        assert result["inline_context"]["line"] == 10

    def test_review_normalization(self):
        from src.routers.webhooks import _normalize_event

        result = _normalize_event(
            "pull_request_review", "dlv-003", SAMPLE_REVIEW_EVENT
        )

        assert result["event_type"] == "pull_request_review"
        assert result["review_state"] == "changes_requested"
        assert "nits" in result["comment_body"]

    def test_pydantic_schema_validates(self):
        contracts_path = os.path.join(os.path.dirname(__file__), "..", "packages", "contracts", "src")
        if contracts_path not in sys.path:
            sys.path.insert(0, contracts_path)
        from contracts.github_pr_review_event_v1 import GitHubPRReviewEventV1
        from src.routers.webhooks import _normalize_event

        normalized = _normalize_event("issue_comment", "dlv-004", SAMPLE_PR_COMMENT_EVENT)
        obj = GitHubPRReviewEventV1(**normalized)
        assert obj.delivery_id == "dlv-004"
        assert obj.pr_number == 7


# ── 3. Dedupe behavior ───────────────────────────────────────────

class TestDedupe:
    def test_not_processed_returns_false(self):
        store = MagicMock()
        store.get_item.return_value = {}

        from src.stores.webhook_dedupe_store import WebhookDedupeStore

        with patch.object(WebhookDedupeStore, "__init__", lambda self, *a, **kw: None):
            ds = WebhookDedupeStore.__new__(WebhookDedupeStore)
            ds._table = store
            assert ds.already_processed("new-delivery") is False

    def test_already_processed_returns_true(self):
        store = MagicMock()
        store.get_item.return_value = {"Item": {"pk": "WEBHOOK#DELIVERY"}}

        from src.stores.webhook_dedupe_store import WebhookDedupeStore

        with patch.object(WebhookDedupeStore, "__init__", lambda self, *a, **kw: None):
            ds = WebhookDedupeStore.__new__(WebhookDedupeStore)
            ds._table = store
            assert ds.already_processed("existing-delivery") is True


# ── 4. PR pause/resume ───────────────────────────────────────────

class TestPRPauseResume:
    def test_pause_and_check(self):
        from src.stores.webhook_dedupe_store import WebhookDedupeStore

        store = MagicMock()
        with patch.object(WebhookDedupeStore, "__init__", lambda self, *a, **kw: None):
            ds = WebhookDedupeStore.__new__(WebhookDedupeStore)
            ds._table = store

            ds.set_pr_paused("owner/repo", 7, True)
            store.put_item.assert_called_once()
            call_item = store.put_item.call_args[1]["Item"]
            assert call_item["paused"] is True

    def test_is_paused_false_when_no_item(self):
        from src.stores.webhook_dedupe_store import WebhookDedupeStore

        store = MagicMock()
        store.get_item.return_value = {}
        with patch.object(WebhookDedupeStore, "__init__", lambda self, *a, **kw: None):
            ds = WebhookDedupeStore.__new__(WebhookDedupeStore)
            ds._table = store
            assert ds.is_pr_paused("owner/repo", 7) is False

    def test_none_pr_number_not_paused(self):
        from src.stores.webhook_dedupe_store import WebhookDedupeStore

        with patch.object(WebhookDedupeStore, "__init__", lambda self, *a, **kw: None):
            ds = WebhookDedupeStore.__new__(WebhookDedupeStore)
            assert ds.is_pr_paused("owner/repo", None) is False


# ── 5. Self-event detection ──────────────────────────────────────

class TestSelfEventDetection:
    def test_bot_suffix_detected(self):
        from src.routers.webhooks import _normalize_event

        event = dict(SAMPLE_PR_COMMENT_EVENT)
        event["sender"] = {"login": "opsrunbook-copilot-bot[bot]"}
        result = _normalize_event("issue_comment", "dlv-bot", event)
        assert result["sender_login"] == "opsrunbook-copilot-bot[bot]"
        assert result["sender_login"].lower().endswith("[bot]")


# ── 6. Patcher tests ─────────────────────────────────────────────

class TestPatcher:
    @pytest.fixture(autouse=True)
    def _add_patcher_path(self):
        sys.path.insert(0, os.path.join(
            os.path.dirname(__file__), "..",
            "infra", "terraform", "modules", "pr_review_cycle", "src",
        ))

    def test_allowlist_enforcement(self):
        from patcher import _is_path_allowed

        assert _is_path_allowed("src/main.py", ["src/"]) is True
        assert _is_path_allowed("tests/test.py", ["src/"]) is False
        assert _is_path_allowed(".opsrunbook/notes.md", [".opsrunbook/", "src/"]) is True

    def test_blocked_ci_paths(self):
        from patcher import _is_path_allowed

        assert _is_path_allowed(".github/workflows/ci.yml", ["src/", ".github/"]) is False
        assert _is_path_allowed(".circleci/config.yml", [".circleci/"]) is False

    def test_size_limit_too_many_files(self):
        from patcher import apply_patch_plan, PatchResult

        plan = {
            "proposed_edits": [
                {"file_path": f"src/file{i}.py", "change_type": "edit"} for i in range(10)
            ]
        }
        gh = MagicMock()
        result = apply_patch_plan(
            gh=gh, owner="o", repo="r", branch="b", plan=plan,
            delivery_id="d", allowed_paths=["src/"], max_files=5,
        )
        assert result.status == "failed"
        assert "too many files" in result.reason

    def test_empty_edits_deferred(self):
        from patcher import apply_patch_plan

        result = apply_patch_plan(
            gh=MagicMock(), owner="o", repo="r", branch="b",
            plan={"proposed_edits": []}, delivery_id="d",
        )
        assert result.status == "deferred"

    def test_apply_instructions_replace(self):
        from patcher import _apply_instructions

        original = "Hello world, this is a test."
        instructions = 'replace "Hello" with "Hi"'
        result = _apply_instructions(original, instructions)
        assert result == "Hi world, this is a test."

    def test_apply_instructions_no_match(self):
        from patcher import _apply_instructions

        result = _apply_instructions("Hello world", 'replace "Goodbye" with "Hi"')
        assert result is None

    def test_all_or_nothing_on_commit_failure(self):
        from patcher import apply_patch_plan

        gh = MagicMock()
        gh.get_file_content.return_value = ('print("hello")', "sha1")
        gh.create_or_update_file.side_effect = RuntimeError("API error")

        plan = {
            "proposed_edits": [{
                "file_path": "src/main.py",
                "change_type": "edit",
                "patch": "",
                "instructions": 'replace "hello" with "world"',
            }]
        }
        result = apply_patch_plan(
            gh=gh, owner="o", repo="r", branch="b", plan=plan,
            delivery_id="d", allowed_paths=["src/"],
        )
        assert result.status == "failed"
        assert "commit failed" in result.reason


# ── 7. Guardrails tests ──────────────────────────────────────────

class TestGuardrails:
    @pytest.fixture(autouse=True)
    def _add_handler_path(self):
        sys.path.insert(0, os.path.join(
            os.path.dirname(__file__), "..",
            "infra", "terraform", "modules", "pr_review_cycle", "src",
        ))
        os.environ.setdefault("EVIDENCE_BUCKET", "test-bucket")
        os.environ.setdefault("INCIDENTS_TABLE", "test-table")

    def test_non_copilot_pr_rejected(self):
        from handler import _step_guardrails_check

        result = _step_guardrails_check({
            "event": {"sender_login": "human", "comment_body": "fix this"},
            "pr_context": {
                "body": "Normal PR without marker",
                "labels": [],
                "user_login": "human",
            },
        })
        assert result["guardrails"]["proceed"] is False
        assert "not created by opsrunbook" in result["guardrails"]["reason"]

    def test_copilot_pr_accepted(self):
        from handler import _step_guardrails_check

        result = _step_guardrails_check({
            "event": {"sender_login": "human", "comment_body": "fix this"},
            "pr_context": {
                "body": "Auto-generated by opsrunbook_copilot: true",
                "labels": [],
                "user_login": "opsrunbook-copilot-bot[bot]",
            },
        })
        assert result["guardrails"]["proceed"] is True

    def test_copilot_label_accepted(self):
        from handler import _step_guardrails_check

        result = _step_guardrails_check({
            "event": {"sender_login": "human", "comment_body": "fix this"},
            "pr_context": {
                "body": "",
                "labels": ["opsrunbook-copilot"],
                "user_login": "anyone",
            },
        })
        assert result["guardrails"]["proceed"] is True

    def test_stop_command_blocks(self):
        from handler import _step_guardrails_check

        result = _step_guardrails_check({
            "event": {"sender_login": "human", "comment_body": "/copilot stop"},
            "pr_context": {
                "body": "opsrunbook_copilot: true",
                "labels": [],
                "user_login": "opsrunbook-copilot-bot[bot]",
            },
        })
        assert result["guardrails"]["proceed"] is False
        assert "stop" in result["guardrails"]["reason"]

    def test_bot_sender_blocked(self):
        from handler import _step_guardrails_check

        result = _step_guardrails_check({
            "event": {"sender_login": "opsrunbook-copilot-bot[bot]", "comment_body": "done"},
            "pr_context": {
                "body": "opsrunbook_copilot: true",
                "labels": [],
                "user_login": "opsrunbook-copilot-bot[bot]",
            },
        })
        assert result["guardrails"]["proceed"] is False
        assert "bot itself" in result["guardrails"]["reason"]


# ── 8. Code context builder ───────────────────────────────────────

SAMPLE_FILE_TEXT = """\
import os
import sys

def main():
    print("Hello world")
    x = 1 + 2
    y = x * 3
    if y > 10:
        print("big number")
    else:
        print("small number")
    return y

def helper():
    pass

class Foo:
    def bar(self):
        return "baz"

# end of file
"""

_PR_REVIEW_SRC = os.path.join(
    os.path.dirname(__file__), "..",
    "infra", "terraform", "modules", "pr_review_cycle", "src",
)


class TestCodeContext:
    @pytest.fixture(autouse=True)
    def _add_path(self):
        if _PR_REVIEW_SRC not in sys.path:
            sys.path.insert(0, _PR_REVIEW_SRC)

    def test_build_context_middle_of_file(self):
        from code_context import build_code_context_from_text

        ctx = build_code_context_from_text(
            text=SAMPLE_FILE_TEXT, path="src/main.py", ref="abc123",
            file_sha="sha-xyz", line=7, window=3,
        )
        assert ctx.path == "src/main.py"
        assert ctx.target_line == 7
        assert ctx.start_line == 4
        assert ctx.end_line == 10
        assert "x = 1 + 2" in ctx.snippet
        assert "y = x * 3" in ctx.snippet
        # Line numbers are present
        assert " 7 | " in ctx.snippet or "7 |" in ctx.snippet

    def test_build_context_start_of_file(self):
        from code_context import build_code_context_from_text

        ctx = build_code_context_from_text(
            text=SAMPLE_FILE_TEXT, path="f.py", line=1, window=5,
        )
        assert ctx.start_line == 1
        assert ctx.target_line == 1
        assert ctx.end_line == 6
        assert "import os" in ctx.snippet

    def test_build_context_end_of_file(self):
        from code_context import build_code_context_from_text

        lines = SAMPLE_FILE_TEXT.split("\n")
        total = len(lines)
        ctx = build_code_context_from_text(
            text=SAMPLE_FILE_TEXT, path="f.py", line=total, window=3,
        )
        assert ctx.end_line == total
        assert ctx.start_line >= total - 3

    def test_build_context_line_beyond_file_clamped(self):
        from code_context import build_code_context_from_text

        ctx = build_code_context_from_text(
            text=SAMPLE_FILE_TEXT, path="f.py", line=9999, window=2,
        )
        assert ctx.target_line == ctx.total_lines

    def test_build_context_line_zero_clamped(self):
        from code_context import build_code_context_from_text

        ctx = build_code_context_from_text(
            text=SAMPLE_FILE_TEXT, path="f.py", line=0, window=2,
        )
        assert ctx.target_line == 1
        assert ctx.start_line == 1

    def test_to_dict_fields(self):
        from code_context import build_code_context_from_text

        ctx = build_code_context_from_text(
            text="line1\nline2\nline3", path="a.txt",
            ref="ref1", file_sha="sha1", line=2, window=1,
        )
        d = ctx.to_dict()
        assert d["path"] == "a.txt"
        assert d["ref"] == "ref1"
        assert d["file_sha"] == "sha1"
        assert d["target_line"] == 2
        assert d["start_line"] == 1
        assert d["end_line"] == 3
        assert d["total_lines"] == 3
        assert isinstance(d["snippet"], str)
        assert isinstance(d["byte_size"], int)


class TestFormatSnippet:
    @pytest.fixture(autouse=True)
    def _add_path(self):
        if _PR_REVIEW_SRC not in sys.path:
            sys.path.insert(0, _PR_REVIEW_SRC)

    def test_basic_formatting(self):
        from code_context import format_snippet

        result = format_snippet(["def foo():", "    pass"], 10)
        assert "10 | def foo():" in result
        assert "11 |     pass" in result

    def test_wide_line_numbers(self):
        from code_context import format_snippet

        result = format_snippet(["a", "b", "c"], 998)
        assert " 998 | a" in result
        assert "1000 | c" in result

    def test_empty_lines(self):
        from code_context import format_snippet

        assert format_snippet([], 1) == ""

    def test_single_line(self):
        from code_context import format_snippet

        result = format_snippet(["only line"], 42)
        assert "42 | only line" in result


class TestExtractFileLineFromEvent:
    @pytest.fixture(autouse=True)
    def _add_path(self):
        if _PR_REVIEW_SRC not in sys.path:
            sys.path.insert(0, _PR_REVIEW_SRC)

    def test_inline_context_preferred(self):
        from code_context import extract_file_line_from_event

        event = {
            "inline_context": {"path": "src/main.py", "line": 42},
            "comment_body": "also see config/app.json:10",
        }
        result = extract_file_line_from_event(event)
        assert result == [("src/main.py", 42)]

    def test_original_line_fallback(self):
        from code_context import extract_file_line_from_event

        event = {
            "inline_context": {"path": "src/x.py", "line": None, "original_line": 7},
            "comment_body": "",
        }
        result = extract_file_line_from_event(event)
        assert result == [("src/x.py", 7)]

    def test_colon_pattern_in_comment(self):
        from code_context import extract_file_line_from_event

        event = {
            "inline_context": None,
            "comment_body": "Fix src/utils.py:25 and config/app.json:3",
        }
        result = extract_file_line_from_event(event)
        assert ("src/utils.py", 25) in result
        assert ("config/app.json", 3) in result

    def test_line_keyword_in_comment(self):
        from code_context import extract_file_line_from_event

        event = {
            "inline_context": None,
            "comment_body": "Error in handler.py line 100",
        }
        result = extract_file_line_from_event(event)
        assert ("handler.py", 100) in result

    def test_bare_file_fallback(self):
        from code_context import extract_file_line_from_event

        event = {
            "inline_context": None,
            "comment_body": "Please fix main.py",
        }
        result = extract_file_line_from_event(event)
        assert result == [("main.py", 1)]

    def test_max_five_results(self):
        from code_context import extract_file_line_from_event

        many_files = " ".join(f"f{i}.py" for i in range(20))
        result = extract_file_line_from_event({
            "inline_context": None,
            "comment_body": many_files,
        })
        assert len(result) <= 5


# ── 9. Stub plan fix with code context ───────────────────────────

class TestStubPlanFix:
    @pytest.fixture(autouse=True)
    def _add_path(self):
        if _PR_REVIEW_SRC not in sys.path:
            sys.path.insert(0, _PR_REVIEW_SRC)
        os.environ.setdefault("EVIDENCE_BUCKET", "test-bucket")
        os.environ.setdefault("INCIDENTS_TABLE", "test-table")

    def test_stub_with_inline_context_no_code_ctx(self):
        from handler import _stub_plan_fix

        plan = _stub_plan_fix(
            event={"delivery_id": "d1", "inline_context": {"path": "src/foo.py", "line": 10}},
            pr_ctx={"pr_number": 5, "owner": "o", "repo": "r"},
            comment="fix the typo",
        )
        assert plan["schema_version"] == "pr_fix_plan.v1"
        assert len(plan["proposed_edits"]) == 1
        assert plan["proposed_edits"][0]["file_path"] == "src/foo.py"
        assert plan["proposed_edits"][0]["target_line"] == 10

    def test_stub_with_code_context(self):
        from handler import _stub_plan_fix

        code_ctx = [{
            "path": "src/main.py",
            "ref": "branch-1",
            "file_sha": "abc",
            "target_line": 5,
            "start_line": 1,
            "end_line": 10,
            "snippet": ' 5 | print("helo world")',
            "total_lines": 20,
            "byte_size": 200,
        }]
        plan = _stub_plan_fix(
            event={"delivery_id": "d3", "inline_context": {"path": "src/main.py", "line": 5}},
            pr_ctx={"pr_number": 5, "owner": "o", "repo": "r"},
            comment='replace "helo" with "hello"',
            code_contexts=code_ctx,
        )
        assert plan["schema_version"] == "pr_fix_plan.v1"
        assert len(plan["proposed_edits"]) == 1
        edit = plan["proposed_edits"][0]
        assert edit["file_path"] == "src/main.py"
        assert edit["target_line"] == 5
        assert edit["line_range"] == [1, 10]
        # Should produce a patch since we have context + replace pattern
        assert edit["patch"] != ""
        assert "helo" in edit["rationale"]
        assert plan["requires_human"] is False
        assert plan["risk_level"] == "low"

    def test_stub_replace_generates_unified_diff(self):
        from handler import _stub_plan_fix

        code_ctx = [{
            "path": ".opsrunbook/pr-notes/KAN-4.md",
            "ref": "KAN-4",
            "file_sha": "sha1",
            "target_line": 3,
            "start_line": 1,
            "end_line": 5,
            "snippet": (
                "1 | # Incident Report\n"
                "2 | \n"
                "3 | Service: loggen\n"
                "4 | Environemnt: dev\n"
                "5 | Status: open"
            ),
            "total_lines": 5,
            "byte_size": 80,
        }]
        plan = _stub_plan_fix(
            event={"delivery_id": "d4", "inline_context": {"path": ".opsrunbook/pr-notes/KAN-4.md", "line": 4}},
            pr_ctx={"pr_number": 7, "owner": "o", "repo": "r"},
            comment='fix spelling: replace "Environemnt" with "Environment"',
            code_contexts=code_ctx,
        )
        edit = plan["proposed_edits"][0]
        assert "@@" in edit["patch"]
        assert "-Environemnt" in edit["patch"] or "-" in edit["patch"]
        assert "+Environment" in edit["patch"] or "+" in edit["patch"]
        assert plan["requires_human"] is False

    def test_stub_no_context_requires_human(self):
        from handler import _stub_plan_fix

        plan = _stub_plan_fix(
            event={"delivery_id": "d5", "inline_context": None, "comment_body": "fix main.py"},
            pr_ctx={"pr_number": 5, "owner": "o", "repo": "r"},
            comment="fix main.py",
        )
        assert plan["requires_human"] is True

    def test_stub_with_file_refs_in_comment(self):
        from handler import _stub_plan_fix

        plan = _stub_plan_fix(
            event={"delivery_id": "d2", "inline_context": None, "comment_body": "Please fix config/settings.json and src/app.py"},
            pr_ctx={"pr_number": 5, "owner": "o", "repo": "r"},
            comment="Please fix config/settings.json and src/app.py",
        )
        assert len(plan["proposed_edits"]) >= 2
        files = [e["file_path"] for e in plan["proposed_edits"]]
        assert "config/settings.json" in files
        assert "src/app.py" in files

    def test_model_trace_includes_context_count(self):
        from handler import _stub_plan_fix

        plan = _stub_plan_fix(
            event={"delivery_id": "d6", "inline_context": None},
            pr_ctx={"pr_number": 1, "owner": "o", "repo": "r"},
            comment="test",
            code_contexts=[{"path": "a.py", "target_line": 1, "start_line": 1,
                           "end_line": 1, "snippet": "1 | x", "file_sha": "s",
                           "total_lines": 1, "byte_size": 1}],
        )
        assert plan["model_trace"]["code_contexts_used"] == 1


# ── 10. Plan validation (pr_fix_plan.v1 schema) ──────────────────

class TestPlanValidation:
    @pytest.fixture(autouse=True)
    def _add_paths(self):
        if _PR_REVIEW_SRC not in sys.path:
            sys.path.insert(0, _PR_REVIEW_SRC)
        contracts_path = os.path.join(
            os.path.dirname(__file__), "..", "packages", "contracts", "src",
        )
        if contracts_path not in sys.path:
            sys.path.insert(0, contracts_path)
        os.environ.setdefault("EVIDENCE_BUCKET", "test-bucket")
        os.environ.setdefault("INCIDENTS_TABLE", "test-table")

    def test_stub_plan_validates_against_schema(self):
        from contracts.pr_fix_plan_v1 import PRFixPlanV1
        from handler import _stub_plan_fix

        plan_dict = _stub_plan_fix(
            event={"delivery_id": "v1", "inline_context": {"path": "src/a.py", "line": 5}},
            pr_ctx={"pr_number": 1, "owner": "o", "repo": "r"},
            comment="fix this",
        )
        plan = PRFixPlanV1(**plan_dict)
        assert plan.schema_version == "pr_fix_plan.v1"
        assert plan.delivery_id == "v1"
        assert plan.pr_number == 1

    def test_context_grounded_plan_validates(self):
        from contracts.pr_fix_plan_v1 import PRFixPlanV1
        from handler import _stub_plan_fix

        code_ctx = [{
            "path": "src/x.py", "ref": "b", "file_sha": "s",
            "target_line": 3, "start_line": 1, "end_line": 5,
            "snippet": '3 | print("helo")', "total_lines": 5, "byte_size": 50,
        }]
        plan_dict = _stub_plan_fix(
            event={"delivery_id": "v2", "inline_context": {"path": "src/x.py", "line": 3}},
            pr_ctx={"pr_number": 2, "owner": "o", "repo": "r"},
            comment='replace "helo" with "hello"',
            code_contexts=code_ctx,
        )
        plan = PRFixPlanV1(**plan_dict)
        assert len(plan.proposed_edits) == 1
        assert plan.risk_level == "low"
        assert plan.requires_human is False


# ── 11. _infer_fix_from_comment ───────────────────────────────────

class TestInferFix:
    @pytest.fixture(autouse=True)
    def _add_path(self):
        if _PR_REVIEW_SRC not in sys.path:
            sys.path.insert(0, _PR_REVIEW_SRC)
        os.environ.setdefault("EVIDENCE_BUCKET", "test-bucket")
        os.environ.setdefault("INCIDENTS_TABLE", "test-table")

    def test_replace_with_pattern(self):
        from handler import _infer_fix_from_comment

        snippet = '5 | print("helo world")'
        patch, instructions = _infer_fix_from_comment(
            'replace "helo" with "hello"', snippet, 5,
        )
        assert patch != ""
        assert "@@" in patch
        assert 'replace "helo" with "hello"' == instructions

    def test_change_to_pattern(self):
        from handler import _infer_fix_from_comment

        snippet = '10 | name = "Jonh"'
        patch, instructions = _infer_fix_from_comment(
            'change "Jonh" to "John"', snippet, 10,
        )
        assert patch != ""
        assert 'replace "Jonh" with "John"' == instructions

    def test_no_match_returns_empty_patch(self):
        from handler import _infer_fix_from_comment

        snippet = '1 | x = 1'
        patch, instructions = _infer_fix_from_comment(
            "this looks wrong", snippet, 1,
        )
        assert patch == ""
        assert "Address review feedback" in instructions

    def test_replace_text_not_in_snippet(self):
        from handler import _infer_fix_from_comment

        snippet = '1 | x = 1'
        patch, instructions = _infer_fix_from_comment(
            'replace "missing" with "found"', snippet, 1,
        )
        assert patch == ""
        assert 'replace "missing" with "found"' == instructions
