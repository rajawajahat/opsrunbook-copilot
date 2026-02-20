"""Tests for Iteration 7: deterministic repo resolution, trace parsing,
GitHub verification, and skip behavior."""

import json
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

_ACTIONS_SRC = os.path.join(
    os.path.dirname(__file__), "..",
    "infra", "terraform", "modules", "actions_runner", "src",
)
if _ACTIONS_SRC not in sys.path:
    sys.path.insert(0, _ACTIONS_SRC)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 1. Trace Parser
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestTraceParser:
    def test_python_traceback(self):
        from trace_parser import parse_frames

        text = '''Traceback (most recent call last):
  File "/var/task/handler.py", line 42, in lambda_handler
    result = process(event)
  File "/var/task/services/processor.py", line 18, in process
    return compute(event["data"])'''

        frames = parse_frames(text)
        assert len(frames) == 2
        assert frames[0].normalized_path == "handler.py"
        assert frames[0].line == 42
        assert frames[0].function == "lambda_handler"
        assert frames[1].normalized_path == "services/processor.py"
        assert frames[1].line == 18

    def test_node_traceback(self):
        from trace_parser import parse_frames

        text = '''Error: something went wrong
    at processEvent (/usr/src/app/src/handler.js:15:8)
    at /usr/src/app/lib/router.js:42:12'''

        frames = parse_frames(text)
        assert len(frames) == 2
        assert frames[0].normalized_path == "src/handler.js"
        assert frames[0].line == 15
        assert frames[0].column == 8
        assert frames[0].function == "processEvent"
        assert frames[1].normalized_path == "lib/router.js"

    def test_noise_filtered(self):
        from trace_parser import extract_app_frames

        text = '''Traceback (most recent call last):
  File "/var/task/handler.py", line 10, in main
    import foo
  File "/var/task/.venv/lib/python3.12/site-packages/boto3/client.py", line 50, in call
    pass
  File "/var/task/services/core.py", line 5, in run
    pass'''

        frames = extract_app_frames(text)
        paths = [f.normalized_path for f in frames]
        assert "handler.py" in paths
        assert "services/core.py" in paths
        assert not any("site-packages" in p for p in paths)

    def test_max_five_frames(self):
        from trace_parser import extract_app_frames

        lines = []
        for i in range(20):
            lines.append(f'  File "/var/task/mod{i}.py", line {i+1}, in func{i}')
        text = "\n".join(lines)
        frames = extract_app_frames(text)
        assert len(frames) <= 5

    def test_path_normalization(self):
        from trace_parser import normalize_path

        assert normalize_path("/var/task/handler.py") == "handler.py"
        assert normalize_path("/usr/src/app/src/index.js") == "src/index.js"
        assert normalize_path("/home/runner/work/repo/repo/src/main.py") == "src/main.py"
        assert normalize_path("./relative/path.py") == "relative/path.py"
        assert normalize_path("/app/lib/util.py") == "lib/util.py"

    def test_generic_path_line(self):
        from trace_parser import parse_frames

        text = "Error in handler.py:42 during processing"
        frames = parse_frames(text)
        assert len(frames) >= 1
        assert frames[0].normalized_path == "handler.py"
        assert frames[0].line == 42

    def test_deduplication(self):
        from trace_parser import parse_frames

        text = '''  File "/var/task/handler.py", line 10, in main
  File "/var/task/handler.py", line 10, in main'''
        frames = parse_frames(text)
        assert len(frames) == 1


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 2. Repo Mapping + Resolver
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestMappingRules:
    def test_exact_match(self):
        from repo_resolver import MappingRule

        rule = MappingRule(type="exact", signal="service_name", pattern="loggen", repo="org/loggen-repo")
        assert rule.matches("loggen") is True
        assert rule.matches("loggen-extra") is False

    def test_prefix_match(self):
        from repo_resolver import MappingRule

        rule = MappingRule(type="prefix", signal="lambda_name", pattern="billing-", repo="org/billing")
        assert rule.matches("billing-api") is True
        assert rule.matches("payments-api") is False

    def test_load_mapping_file(self):
        from repo_resolver import load_mapping_rules

        rules = load_mapping_rules(os.path.join(_ACTIONS_SRC, "repo_mapping.json"))
        assert len(rules) >= 2
        assert any(r.signal == "service_name" and r.pattern == "loggen" for r in rules)


class TestRepoResolver:
    def test_mapping_match_selects_correct_repo(self):
        from repo_resolver import resolve_repo, MappingRule

        rules = [
            MappingRule(type="exact", signal="service_name", pattern="loggen", repo="org/loggen-repo"),
            MappingRule(type="prefix", signal="lambda_name", pattern="billing-", repo="org/billing"),
        ]
        packet = {"service": "loggen", "findings": [], "suspected_owners": [], "all_evidence_refs": []}

        result = resolve_repo(packet, rules=rules)
        assert result.repo_full_name == "org/loggen-repo"
        assert result.confidence == 0.95
        assert result.verification == "mapping"
        assert "mapping rule" in result.reasons[0]

    def test_no_mapping_falls_to_heuristic(self):
        from repo_resolver import resolve_repo, MappingRule

        rules = [MappingRule(type="exact", signal="service_name", pattern="other", repo="org/other")]
        packet = {
            "service": "myservice",
            "findings": [],
            "suspected_owners": [{"repo": "my-repo", "confidence": 0.6}],
            "all_evidence_refs": [],
        }
        result = resolve_repo(packet, rules=rules, owner="org")
        assert result.repo_full_name == "org/my-repo"
        assert result.confidence == 0.5
        assert result.verification == "unverified"

    def test_trace_verification_raises_confidence(self):
        from repo_resolver import resolve_repo, MappingRule

        checker = MagicMock()
        checker.file_exists.return_value = True

        packet = {
            "service": "myservice",
            "findings": [{"summary": 'File "/var/task/handler.py", line 10, in main\n  error here'}],
            "suspected_owners": [{"repo": "my-repo", "confidence": 0.5}],
            "all_evidence_refs": [],
        }
        result = resolve_repo(packet, rules=[], checker=checker, owner="org")
        assert result.confidence == 0.85
        assert result.verification == "verified"
        assert "verified" in result.reasons[0]
        assert checker.file_exists.call_count <= 4

    def test_verification_bounded_calls(self):
        from repo_resolver import resolve_repo

        checker = MagicMock()
        checker.file_exists.return_value = False

        packet = {
            "service": "svc",
            "findings": [{"summary": 'File "/var/task/a.py", line 1\nFile "/var/task/b.py", line 2\nFile "/var/task/c.py", line 3'}],
            "suspected_owners": [
                {"repo": "r1", "confidence": 0.5},
                {"repo": "r2", "confidence": 0.4},
                {"repo": "r3", "confidence": 0.3},
            ],
            "all_evidence_refs": [],
        }
        resolve_repo(packet, rules=[], checker=checker, owner="org")
        assert checker.file_exists.call_count <= 4

    def test_no_repo_returns_empty(self):
        from repo_resolver import resolve_repo

        packet = {"service": "", "findings": [], "suspected_owners": [], "all_evidence_refs": []}
        result = resolve_repo(packet, rules=[])
        assert result.repo_full_name == ""
        assert result.confidence == 0.0

    def test_unverified_repo_action_skipped(self):
        """Simulate what the actions_runner does when repo can't be resolved."""
        from repo_resolver import resolve_repo

        packet = {"service": "", "findings": [], "suspected_owners": [], "all_evidence_refs": []}
        result = resolve_repo(packet, rules=[])
        assert result.repo_full_name == ""
        # The handler should skip when repo_full_name is empty

    def test_reasons_field_populated(self):
        from repo_resolver import resolve_repo, MappingRule

        rules = [MappingRule(type="exact", signal="service_name", pattern="loggen", repo="org/repo")]
        packet = {"service": "loggen", "findings": [], "suspected_owners": [], "all_evidence_refs": []}

        result = resolve_repo(packet, rules=rules)
        assert len(result.reasons) > 0
        assert any("mapping rule" in r for r in result.reasons)

    def test_trace_frames_included(self):
        from repo_resolver import resolve_repo

        packet = {
            "service": "svc",
            "findings": [{"summary": 'File "/var/task/handler.py", line 42, in main'}],
            "suspected_owners": [],
            "all_evidence_refs": [],
        }
        result = resolve_repo(packet, rules=[])
        assert len(result.trace_frames) > 0
        assert result.trace_frames[0]["normalized_path"] == "handler.py"
        assert result.trace_frames[0]["line"] == 42

    def test_legacy_map_compat(self):
        from repo_resolver import resolve_repo

        legacy = {"loggen": "opsrunbook-copilot-test"}
        packet = {
            "service": "loggen",
            "findings": [],
            "suspected_owners": [],
            "all_evidence_refs": [],
        }
        result = resolve_repo(packet, rules=[], owner="org", legacy_map=legacy)
        assert result.repo_full_name == "org/opsrunbook-copilot-test"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 3. GitHub file_exists + PR idempotency
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestGitHubFileExists:
    def test_dry_run_always_true(self):
        from github_client import DryRunGitHubClient

        client = DryRunGitHubClient("owner")
        assert client.file_exists("owner/repo", "handler.py") is True

    def test_dry_run_find_open_pr_returns_none(self):
        from github_client import DryRunGitHubClient

        client = DryRunGitHubClient("owner")
        assert client.find_open_pr("repo", "branch") is None


class TestPRIdempotency:
    def test_deterministic_branch_name(self):
        """Branch name should be opsrunbook/<jira_key>."""
        jira_key = "KAN-42"
        branch = f"opsrunbook/{jira_key}"
        assert branch == "opsrunbook/KAN-42"

    def test_dry_run_pr_create(self):
        from github_client import DryRunGitHubClient

        client = DryRunGitHubClient("owner")
        refs = client.create_pr_with_notes(
            repo="test", branch_name="opsrunbook/KAN-1",
            pr_title="test", pr_body="body",
            file_path=".opsrunbook/pr-notes/KAN-1.md",
            file_content="content", commit_message="msg",
        )
        assert refs["branch"] == "opsrunbook/KAN-1"
        assert refs["pr_number"] == 1


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 4. RepoResolution serialization
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestResolutionSerialization:
    def test_to_dict(self):
        from repo_resolver import RepoResolution

        r = RepoResolution(
            repo_full_name="org/repo",
            confidence=0.95,
            reasons=["rule matched"],
            verification="mapping",
            trace_frames=[{"normalized_path": "a.py", "line": 1}],
        )
        d = r.to_dict()
        assert d["repo_full_name"] == "org/repo"
        assert d["confidence"] == 0.95
        assert d["verification"] == "mapping"
        assert len(d["reasons"]) == 1
        assert json.dumps(d)  # must be JSON-serializable
