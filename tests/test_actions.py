"""Unit tests for Iteration 4: ActionPlan generation, Jira client, Teams notifier."""
import json
import os
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_boto3_stub = types.ModuleType("boto3")
_boto3_stub.client = MagicMock()
_boto3_stub.resource = MagicMock()
sys.modules.setdefault("boto3", _boto3_stub)

FIXTURES = Path(__file__).resolve().parent / "fixtures"
HANDLER_DIR = Path(__file__).resolve().parent.parent / "infra" / "terraform" / "modules" / "actions_runner" / "src"

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "packages" / "contracts" / "src"))
from contracts.action_plan_v1 import ActionPlanV1, ActionResultV1, PlannedAction


def _load_fixture(name: str) -> dict:
    with open(FIXTURES / name) as f:
        return json.load(f)


_HANDLER_MODULES = ("handler", "plan_generator", "jira_client", "teams_notifier", "github_client", "repo_resolver", "trace_parser")


@pytest.fixture(autouse=True)
def _handler_path(monkeypatch):
    monkeypatch.setenv("INCIDENTS_TABLE", "test-incidents")
    monkeypatch.setenv("EVENT_BUS_NAME", "")
    monkeypatch.setenv("ACTIONS_DRY_RUN", "true")
    monkeypatch.setenv("ENABLE_GITHUB_PR_ACTION", "true")
    monkeypatch.setenv("GITHUB_OWNER", "test-owner")
    monkeypatch.setenv("GITHUB_DEFAULT_BRANCH", "main")
    monkeypatch.setenv("AUTOMATION_ENABLED", "true")
    monkeypatch.setenv("PR_CONFIDENCE_THRESHOLD", "0.7")
    sys.path.insert(0, str(HANDLER_DIR))
    for mod in _HANDLER_MODULES:
        if mod in sys.modules:
            del sys.modules[mod]
    yield
    sys.path.pop(0)
    for mod in _HANDLER_MODULES:
        if mod in sys.modules:
            del sys.modules[mod]


# ── Schema tests ──────────────────────────────────────────────────

class TestActionSchemas:
    def test_action_plan_v1(self):
        plan = ActionPlanV1(
            incident_id="inc-1",
            service="svc",
            actions=[PlannedAction(action_type="create_jira_ticket", title="Test", priority="P2")],
        )
        assert plan.schema_version == "incident_action_plan.v1"
        assert len(plan.actions) == 1

    def test_action_result_v1(self):
        result = ActionResultV1(
            incident_id="inc-1",
            action_id="act-1",
            action_type="create_jira_ticket",
            status="success",
            external_refs={"jira_issue_key": "OPS-1"},
        )
        assert result.status == "success"

    def test_action_result_skipped(self):
        result = ActionResultV1(
            incident_id="inc-1",
            action_id="act-2",
            action_type="notify_teams",
            status="skipped",
            error="teams_not_configured",
        )
        assert result.error == "teams_not_configured"


# ── Plan generator tests ──────────────────────────────────────────

class TestPlanGenerator:
    def test_generate_plan_from_packet(self):
        from plan_generator import generate_action_plan
        packet = _load_fixture("sample_packet.json")
        plan = generate_action_plan(packet, dry_run=True)
        assert plan["schema_version"] == "incident_action_plan.v1"
        assert plan["incident_id"] == "inc-test456"
        assert len(plan["actions"]) == 3
        types_ = [a["action_type"] for a in plan["actions"]]
        assert "create_jira_ticket" in types_
        assert "notify_teams" in types_
        assert "create_github_pr" in types_

    def test_priority_p1_for_high_confidence(self):
        from plan_generator import generate_action_plan
        packet = _load_fixture("sample_packet.json")
        packet["findings"][0]["confidence"] = 0.95
        plan = generate_action_plan(packet, dry_run=True)
        assert plan["actions"][0]["priority"] == "P1"

    def test_priority_p2_for_low_confidence(self):
        from plan_generator import generate_action_plan
        packet = _load_fixture("sample_packet.json")
        for f in packet["findings"]:
            f["confidence"] = 0.3
        plan = generate_action_plan(packet, dry_run=True)
        assert plan["actions"][0]["priority"] == "P2"

    def test_jira_description_contains_findings(self):
        from plan_generator import generate_action_plan
        packet = _load_fixture("sample_packet.json")
        plan = generate_action_plan(packet, dry_run=True)
        jira = next(a for a in plan["actions"] if a["action_type"] == "create_jira_ticket")
        assert "Findings" in jira["description_md"]
        assert "RuntimeError" in jira["description_md"]

    def test_teams_body(self):
        from plan_generator import build_teams_body
        packet = _load_fixture("sample_packet.json")
        body = build_teams_body(packet, {"jira_issue_key": "OPS-1", "url": "http://jira/OPS-1"})
        assert "inc-test456" in body
        assert "OPS-1" in body


# ── Jira client tests ─────────────────────────────────────────────

class TestJiraClient:
    def test_dry_run_returns_dryrun_key(self):
        from jira_client import DryRunJiraClient
        client = DryRunJiraClient()
        result = client.create_issue("test summary", "test desc")
        assert result["issue_key"].startswith("DRYRUN-")
        assert "dryrun" in result["url"].lower()

    def test_dry_run_increments(self):
        from jira_client import DryRunJiraClient
        client = DryRunJiraClient()
        r1 = client.create_issue("s1", "d1")
        r2 = client.create_issue("s2", "d2")
        assert r1["issue_key"] != r2["issue_key"]


# ── Teams notifier tests ──────────────────────────────────────────

class TestTeamsNotifier:
    def test_dry_run_returns_ok(self):
        from teams_notifier import DryRunTeamsNotifier
        notifier = DryRunTeamsNotifier()
        result = notifier.send_message("title", "body")
        assert result["status_code"] == 200
        assert "DRYRUN" in result["response"]

    def test_dry_run_increments(self):
        from teams_notifier import DryRunTeamsNotifier
        notifier = DryRunTeamsNotifier()
        r1 = notifier.send_message("t1", "b1")
        r2 = notifier.send_message("t2", "b2")
        assert r1["message_id"] != r2["message_id"]


# ── Handler DRY_RUN test ──────────────────────────────────────────

class TestHandlerDryRun:
    def test_dry_run_produces_success_results(self):
        from handler import _execute_jira, _execute_teams
        from plan_generator import generate_action_plan, build_teams_body
        packet = _load_fixture("sample_packet.json")
        plan = generate_action_plan(packet, dry_run=True)

        jira_result = _execute_jira(plan, packet, "inc-test", "corr-1")
        assert jira_result["status"] == "success"
        assert "DRYRUN" in jira_result["external_refs"]["jira_issue_key"]

        teams_result = _execute_teams(plan, packet, jira_result.get("external_refs", {}), "inc-test", "corr-1")
        assert teams_result["status"] == "success"


# ── GitHub client tests ──────────────────────────────────────────

class TestGitHubClient:
    def test_dry_run_returns_pr_url(self):
        from github_client import DryRunGitHubClient
        client = DryRunGitHubClient("test-owner")
        refs = client.create_pr_with_notes(
            repo="my-repo",
            branch_name="KAN-1",
            pr_title="Test PR",
            pr_body="body",
            file_path=".opsrunbook/pr-notes/KAN-1.md",
            file_content="# notes",
            commit_message="add notes",
        )
        assert "github.com" in refs["pr_url"]
        assert refs["pr_number"] == 1
        assert refs["github_owner"] == "test-owner"
        assert refs["github_repo"] == "my-repo"
        assert refs["branch"] == "KAN-1"
        assert refs["default_branch"] == "main"
        assert refs["commit_sha"].startswith("dryrun-sha-")

    def test_dry_run_increments(self):
        from github_client import DryRunGitHubClient
        client = DryRunGitHubClient()
        r1 = client.create_pr_with_notes(
            repo="r", branch_name="B-1", pr_title="t", pr_body="b",
            file_path="f", file_content="c", commit_message="m",
        )
        r2 = client.create_pr_with_notes(
            repo="r", branch_name="B-2", pr_title="t", pr_body="b",
            file_path="f", file_content="c", commit_message="m",
        )
        assert r1["pr_number"] != r2["pr_number"]

    def test_dry_run_default_branch(self):
        from github_client import DryRunGitHubClient
        client = DryRunGitHubClient()
        assert client.get_default_branch("any-repo") == "main"


class TestGitHubPRExecution:
    def test_dry_run_github_pr_success(self):
        from handler import _execute_github_pr
        from plan_generator import generate_action_plan
        packet = _load_fixture("sample_packet.json")
        plan = generate_action_plan(packet, dry_run=True)

        jira_refs = {"jira_issue_key": "KAN-42", "jira_url": "https://jira.example.com/browse/KAN-42"}
        result = _execute_github_pr(plan, packet, jira_refs, "inc-test456", "corr-1")
        assert result["status"] == "success"
        assert result["action_type"] == "create_github_pr"
        assert "github.com" in result["external_refs"]["pr_url"]
        assert result["external_refs"]["branch"] == "opsrunbook/KAN-42"
        assert "repo_resolution" in result["external_refs"]

    def test_missing_jira_key_fails(self):
        from handler import _execute_github_pr
        from plan_generator import generate_action_plan
        packet = _load_fixture("sample_packet.json")
        plan = generate_action_plan(packet, dry_run=True)

        result = _execute_github_pr(plan, packet, {}, "inc-test456", "corr-1")
        assert result["status"] == "failed"
        assert "missing jira_issue_key" in result["error"]

    def test_repo_from_suspected_owners_below_threshold_skips(self):
        """Heuristic-only repos (0.5 confidence) are below the 0.7 gate and get skipped."""
        from handler import _execute_github_pr
        from plan_generator import generate_action_plan
        packet = _load_fixture("sample_packet.json")
        packet["service"] = "unknown-svc"
        packet["suspected_owners"] = [
            {"repo": "custom-repo", "confidence": 0.9, "reasons": ["test"]},
            {"repo": "other-repo", "confidence": 0.3, "reasons": ["test"]},
        ]
        plan = generate_action_plan(packet, dry_run=True)

        jira_refs = {"jira_issue_key": "KAN-99", "jira_url": "https://jira.example.com/browse/KAN-99"}
        result = _execute_github_pr(plan, packet, jira_refs, "inc-test", "corr-1")
        assert result["status"] == "skipped"
        assert "confidence" in result["error"]

    def test_plan_includes_github_pr_context(self):
        from plan_generator import generate_action_plan
        packet = _load_fixture("sample_packet.json")
        plan = generate_action_plan(packet, dry_run=True)
        gh_action = next(a for a in plan["actions"] if a["action_type"] == "create_github_pr")
        assert gh_action["context"]["incident_id"] == "inc-test456"
        assert gh_action["context"]["service"] == "loggen"

    def test_pr_notes_content(self):
        from plan_generator import build_pr_notes
        packet = _load_fixture("sample_packet.json")
        jira_ref = {"jira_issue_key": "KAN-5", "jira_url": "https://jira.example.com/browse/KAN-5"}
        notes = build_pr_notes(packet, jira_ref)
        assert "inc-test456" in notes
        assert "KAN-5" in notes
        assert "Evidence" in notes
        assert "Findings" in notes

    def test_pr_body_content(self):
        from plan_generator import build_pr_body
        packet = _load_fixture("sample_packet.json")
        jira_ref = {"jira_issue_key": "KAN-5", "jira_url": "https://jira.example.com/browse/KAN-5"}
        body = build_pr_body(packet, jira_ref)
        assert "inc-test456" in body
        assert "KAN-5" in body
        assert "Finding" in body


# ── Sanitizer tests ───────────────────────────────────────────────

_API_SRC = str(Path(__file__).resolve().parent.parent / "services" / "api" / "src")
if _API_SRC not in sys.path:
    sys.path.insert(0, _API_SRC)
from sanitize import sanitize as _sanitize


class TestSanitizer:
    def test_strips_control_chars_from_strings(self):
        from decimal import Decimal

        dirty = {
            "clean": "hello world",
            "with_null": "hello\x00world",
            "with_bel": "hello\x07world",
            "with_tab": "hello\tworld",
            "with_newline": "hello\nworld",
            "with_cr": "hello\rworld",
            "nested": {"deep": "a\x01b\x02c"},
            "list_val": ["ok\x03val", "fine"],
            "number": 42,
            "decimal": Decimal("3.14"),
            "decimal_int": Decimal("7"),
            "none_val": None,
        }
        clean = _sanitize(dirty)

        assert clean["clean"] == "hello world"
        assert clean["with_null"] == "helloworld"
        assert clean["with_bel"] == "helloworld"
        assert clean["with_tab"] == "hello\tworld"  # \t (0x09) is allowed
        assert clean["with_newline"] == "hello\nworld"  # \n (0x0a) is allowed
        assert clean["with_cr"] == "hello\rworld"  # \r (0x0d) is allowed
        assert clean["nested"]["deep"] == "abc"
        assert clean["list_val"][0] == "okval"
        assert clean["number"] == 42
        assert isinstance(clean["decimal"], float)
        assert isinstance(clean["decimal_int"], int)
        assert clean["none_val"] is None

    def test_sanitized_output_is_valid_json(self):
        nasty = {
            "description_md": "line1\x00\x01\x02\nline2\x1f\ttabbed",
            "nested": {"val": "ok\x03\x04stuff"},
            "list": ["\x05hidden", "normal"],
        }
        clean = _sanitize(nasty)
        serialized = json.dumps(clean)
        parsed = json.loads(serialized)
        assert parsed["description_md"] == "line1\nline2\ttabbed"
        assert parsed["nested"]["val"] == "okstuff"

    def test_full_action_plan_roundtrip(self):
        """Generate a plan with control chars injected, sanitize, and verify json.loads."""
        from plan_generator import generate_action_plan

        packet = _load_fixture("sample_packet.json")
        packet["findings"][0]["summary"] = "Error:\x00NullPointer\x07 in\x01handler"
        plan = generate_action_plan(packet, dry_run=True)

        clean_plan = _sanitize(plan)
        serialized = json.dumps({"ok": True, "action_plan": clean_plan})
        parsed = json.loads(serialized)
        assert parsed["ok"] is True
        desc = parsed["action_plan"]["actions"][0]["description_md"]
        assert "\x00" not in desc
        assert "\x07" not in desc


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# v1 Hardening Tests
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestIdempotency:
    """Test that actions are skipped when already executed for an incident."""

    def test_find_existing_action_returns_stored_result(self):
        from handler import _find_existing_action
        mock_table = MagicMock()
        mock_table.query.return_value = {
            "Items": [{
                "action_type": "create_jira_ticket",
                "status": "success",
                "external_refs": json.dumps({"jira_issue_key": "KAN-1"}),
                "evidence_refs": json.dumps([]),
            }]
        }
        result = _find_existing_action(mock_table, "inc-123", "create_jira_ticket")
        assert result is not None
        assert result["status"] == "success"
        assert result["external_refs"]["jira_issue_key"] == "KAN-1"

    def test_find_existing_action_returns_none_when_not_found(self):
        from handler import _find_existing_action
        mock_table = MagicMock()
        mock_table.query.return_value = {"Items": []}
        result = _find_existing_action(mock_table, "inc-123", "create_jira_ticket")
        assert result is None

    def test_find_existing_action_ignores_failed(self):
        from handler import _find_existing_action
        mock_table = MagicMock()
        mock_table.query.return_value = {
            "Items": [{
                "action_type": "create_jira_ticket",
                "status": "failed",
                "external_refs": "{}",
                "evidence_refs": "[]",
            }]
        }
        result = _find_existing_action(mock_table, "inc-123", "create_jira_ticket")
        assert result is None

    def test_find_existing_action_ignores_other_types(self):
        from handler import _find_existing_action
        mock_table = MagicMock()
        mock_table.query.return_value = {
            "Items": [{
                "action_type": "notify_teams",
                "status": "success",
                "external_refs": "{}",
                "evidence_refs": "[]",
            }]
        }
        result = _find_existing_action(mock_table, "inc-123", "create_jira_ticket")
        assert result is None

    def test_idempotency_key_deterministic(self):
        from handler import _idempotency_key
        k1 = _idempotency_key("inc-1", "create_jira_ticket", "PROJ")
        k2 = _idempotency_key("inc-1", "create_jira_ticket", "PROJ")
        k3 = _idempotency_key("inc-2", "create_jira_ticket", "PROJ")
        assert k1 == k2
        assert k1 != k3

    def test_idempotency_key_differs_by_action_type(self):
        from handler import _idempotency_key
        k1 = _idempotency_key("inc-1", "create_jira_ticket", "PROJ")
        k2 = _idempotency_key("inc-1", "notify_teams", "PROJ")
        assert k1 != k2


class TestConfidenceGate:
    """Test that PRs are skipped when repo confidence is below threshold."""

    def test_low_confidence_skips_pr(self):
        from handler import _execute_github_pr
        from plan_generator import generate_action_plan
        packet = _load_fixture("sample_packet.json")
        packet["service"] = "unknown-service-xyz"
        packet["suspected_owners"] = [
            {"repo": "maybe-repo", "confidence": 0.3, "reasons": ["guess"]}
        ]
        plan = generate_action_plan(packet, dry_run=True)
        jira_refs = {"jira_issue_key": "KAN-1", "jira_url": "https://jira.example.com/browse/KAN-1"}

        result = _execute_github_pr(plan, packet, jira_refs, "inc-test", "corr-1")
        assert result["status"] == "skipped"
        assert "confidence" in result["error"]

    def test_high_confidence_mapping_creates_pr(self):
        from handler import _execute_github_pr
        from plan_generator import generate_action_plan
        packet = _load_fixture("sample_packet.json")
        plan = generate_action_plan(packet, dry_run=True)
        jira_refs = {"jira_issue_key": "KAN-99", "jira_url": "https://jira.example.com/browse/KAN-99"}

        result = _execute_github_pr(plan, packet, jira_refs, "inc-test", "corr-1")
        assert result["status"] == "success"

    def test_unverified_no_repo_skips_pr(self):
        from handler import _execute_github_pr
        from plan_generator import generate_action_plan
        packet = _load_fixture("sample_packet.json")
        packet["service"] = ""
        packet["suspected_owners"] = []
        packet["findings"] = []
        plan = generate_action_plan(packet, dry_run=True)
        jira_refs = {"jira_issue_key": "KAN-1", "jira_url": "https://jira.example.com/browse/KAN-1"}

        result = _execute_github_pr(plan, packet, jira_refs, "inc-test", "corr-1")
        assert result["status"] == "skipped"


class TestKillSwitch:
    """Test AUTOMATION_ENABLED kill switch."""

    def test_automation_disabled_returns_early(self, monkeypatch):
        monkeypatch.setenv("AUTOMATION_ENABLED", "false")
        for mod in _HANDLER_MODULES:
            if mod in sys.modules:
                del sys.modules[mod]

        from handler import lambda_handler
        event = {
            "detail": {
                "incident_id": "inc-kill",
                "packet_ref": {"s3_bucket": "b", "s3_key": "k"},
            }
        }
        result = lambda_handler(event, None)
        assert result["ok"] is True
        assert result["status"] == "automation_disabled"


class TestDeterministicPRBody:
    """Test that PR body is fixed-template, contains required fields."""

    def test_pr_body_has_marker(self):
        from handler import _build_deterministic_pr_body
        from repo_resolver import RepoResolution
        packet = _load_fixture("sample_packet.json")
        resolution = RepoResolution(
            repo_full_name="org/repo", confidence=0.95,
            reasons=["mapping match"], verification="mapping",
        )
        body = _build_deterministic_pr_body(packet, "KAN-1", "https://jira/KAN-1", resolution)
        assert "opsrunbook_copilot: true" in body

    def test_pr_body_has_incident_id(self):
        from handler import _build_deterministic_pr_body
        from repo_resolver import RepoResolution
        packet = _load_fixture("sample_packet.json")
        resolution = RepoResolution(
            repo_full_name="org/repo", confidence=0.95,
            reasons=[], verification="mapping",
        )
        body = _build_deterministic_pr_body(packet, "KAN-1", "https://jira/KAN-1", resolution)
        assert "inc-test456" in body

    def test_pr_body_has_confidence(self):
        from handler import _build_deterministic_pr_body
        from repo_resolver import RepoResolution
        packet = _load_fixture("sample_packet.json")
        resolution = RepoResolution(
            repo_full_name="org/repo", confidence=0.95,
            reasons=[], verification="mapping",
        )
        body = _build_deterministic_pr_body(packet, "KAN-1", "https://jira/KAN-1", resolution)
        assert "Confidence" in body
        assert "95%" in body

    def test_pr_body_has_evidence_summary(self):
        from handler import _build_deterministic_pr_body
        from repo_resolver import RepoResolution
        packet = _load_fixture("sample_packet.json")
        resolution = RepoResolution(
            repo_full_name="org/repo", confidence=0.95,
            reasons=[], verification="mapping",
        )
        body = _build_deterministic_pr_body(packet, "KAN-1", "https://jira/KAN-1", resolution)
        assert "Evidence Summary" in body
        assert "evidence object(s)" in body

    def test_pr_body_has_findings(self):
        from handler import _build_deterministic_pr_body
        from repo_resolver import RepoResolution
        packet = _load_fixture("sample_packet.json")
        resolution = RepoResolution(
            repo_full_name="org/repo", confidence=0.95,
            reasons=[], verification="mapping",
        )
        body = _build_deterministic_pr_body(packet, "KAN-1", "https://jira/KAN-1", resolution)
        assert "Finding(s)" in body
        assert "evidence ref(s)" in body

    def test_pr_body_has_jira_link(self):
        from handler import _build_deterministic_pr_body
        from repo_resolver import RepoResolution
        packet = _load_fixture("sample_packet.json")
        resolution = RepoResolution(
            repo_full_name="org/repo", confidence=0.95,
            reasons=[], verification="mapping",
        )
        body = _build_deterministic_pr_body(packet, "KAN-1", "https://jira/KAN-1", resolution)
        assert "[KAN-1]" in body
        assert "https://jira/KAN-1" in body


class TestStructuredLogging:
    """Test that _log always includes incident_id and correlation_id."""

    def test_log_contains_required_fields(self, capsys):
        from handler import _log
        _log("test_msg", "inc-999", "corr-abc", extra_field="value")
        captured = capsys.readouterr()
        parsed = json.loads(captured.out.strip())
        assert parsed["msg"] == "test_msg"
        assert parsed["incident_id"] == "inc-999"
        assert parsed["correlation_id"] == "corr-abc"
        assert parsed["extra_field"] == "value"

    def test_log_handles_empty_ids(self, capsys):
        from handler import _log
        _log("test_msg")
        captured = capsys.readouterr()
        parsed = json.loads(captured.out.strip())
        assert parsed["incident_id"] == ""
        assert parsed["correlation_id"] == ""


class TestEvidenceRefsInPlan:
    """Ensure action plans always include evidence_refs."""

    def test_plan_actions_have_evidence_refs(self):
        from plan_generator import generate_action_plan
        packet = _load_fixture("sample_packet.json")
        plan = generate_action_plan(packet, dry_run=True)
        for action in plan["actions"]:
            assert "evidence_refs" in action
            assert isinstance(action["evidence_refs"], list)
            # For a packet with evidence, refs should be present
            if packet.get("all_evidence_refs"):
                assert len(action["evidence_refs"]) > 0

    def test_plan_evidence_refs_empty_when_no_evidence(self):
        from plan_generator import generate_action_plan
        packet = _load_fixture("sample_packet.json")
        packet["all_evidence_refs"] = []
        plan = generate_action_plan(packet, dry_run=True)
        for action in plan["actions"]:
            assert "evidence_refs" in action
            assert action["evidence_refs"] == []
