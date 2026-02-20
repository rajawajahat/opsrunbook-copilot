"""Unit tests for IncidentPacketV1 schema validation and stub analyzer."""
import json
import os
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# Stub boto3 before importing handler (Lambda handler imports boto3 at module level)
_boto3_stub = types.ModuleType("boto3")
_boto3_stub.client = MagicMock()
_boto3_stub.resource = MagicMock()
sys.modules.setdefault("boto3", _boto3_stub)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "packages" / "contracts" / "src"))
from contracts.incident_packet_v1 import (
    Finding,
    Hypothesis,
    IncidentPacketV1,
    ModelTrace,
    NextAction,
    PacketEvidenceRef,
    PacketHashes,
    SnapshotRef,
    SuspectedOwner,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _load_fixture(name: str) -> dict:
    with open(FIXTURES / name) as f:
        return json.load(f)


def _make_ref(**overrides) -> PacketEvidenceRef:
    base = {"collector_type": "logs", "s3_bucket": "b", "s3_key": "k", "sha256": "h", "byte_size": 1}
    base.update(overrides)
    return PacketEvidenceRef(**base)


class TestFindingValidation:
    def test_high_confidence_requires_evidence(self):
        with pytest.raises(ValueError, match="evidence_refs"):
            Finding(id="f1", summary="high confidence", confidence=0.8, evidence_refs=[])

    def test_high_confidence_with_evidence_ok(self):
        f = Finding(id="f1", summary="ok", confidence=0.8, evidence_refs=[_make_ref()])
        assert f.confidence == 0.8

    def test_low_confidence_no_evidence_ok(self):
        f = Finding(id="f2", summary="low", confidence=0.3, evidence_refs=[])
        assert len(f.evidence_refs) == 0

    def test_boundary_0_6_no_evidence_ok(self):
        f = Finding(id="f3", summary="boundary", confidence=0.6, evidence_refs=[])
        assert f.confidence == 0.6

    def test_boundary_0_61_requires_evidence(self):
        with pytest.raises(ValueError, match="evidence_refs"):
            Finding(id="f4", summary="just above", confidence=0.61, evidence_refs=[])


class TestIncidentPacketV1:
    def test_minimal_packet(self):
        packet = IncidentPacketV1(
            incident_id="inc-1",
            collector_run_id="run-1",
            service="loggen",
            snapshot_ref=SnapshotRef(s3_bucket="b", s3_key="k"),
        )
        assert packet.schema_version == "incident_packet.v1"
        assert packet.limits == []

    def test_packet_with_findings(self):
        packet = IncidentPacketV1(
            incident_id="inc-2",
            collector_run_id="run-2",
            service="loggen",
            snapshot_ref=SnapshotRef(s3_bucket="b", s3_key="k"),
            findings=[Finding(id="f1", summary="test", confidence=0.9, evidence_refs=[_make_ref()])],
            limits=["No metrics available"],
        )
        assert len(packet.findings) == 1
        assert len(packet.limits) == 1

    def test_packet_serialization_stable(self):
        packet = IncidentPacketV1(
            incident_id="inc-3",
            collector_run_id="run-3",
            service="svc",
            snapshot_ref=SnapshotRef(s3_bucket="b", s3_key="k", sha256="abc"),
            model_trace=ModelTrace(provider="stub", prompt_version="v1"),
        )
        j1 = json.dumps(packet.model_dump(), sort_keys=True, default=str)
        j2 = json.dumps(packet.model_dump(), sort_keys=True, default=str)
        assert j1 == j2


class TestStubAnalyzer:
    """Tests the analyzer handler's analysis functions directly."""

    @pytest.fixture(autouse=True)
    def _add_handler_to_path(self, monkeypatch):
        monkeypatch.setenv("PACKETS_TABLE", "test-packets")
        monkeypatch.setenv("EVENT_BUS_NAME", "")
        handler_dir = Path(__file__).resolve().parent.parent / "infra" / "terraform" / "modules" / "analyzer" / "src"
        sys.path.insert(0, str(handler_dir))
        # Force reimport to pick up env vars
        if "handler" in sys.modules:
            del sys.modules["handler"]
        yield
        sys.path.pop(0)
        if "handler" in sys.modules:
            del sys.modules["handler"]

    def test_analyze_logs_with_errors(self):
        from handler import _analyze_logs
        evidence = _load_fixture("sample_logs_evidence.json")
        eref = {"collector_type": "logs", "s3_bucket": "b", "s3_key": "k", "sha256": "h", "byte_size": 1}
        findings, hypos, actions, limits = _analyze_logs(evidence, eref)
        assert len(findings) >= 1
        assert findings[0]["id"] == "logs-errors-found"
        assert findings[0]["confidence"] == 0.8
        assert len(findings[0]["evidence_refs"]) == 1

    def test_analyze_logs_no_errors(self):
        from handler import _analyze_logs
        evidence = {"sections": [{"name": "recent_errors", "rows": []}]}
        eref = {"collector_type": "logs", "s3_bucket": "b", "s3_key": "k", "sha256": "h", "byte_size": 1}
        findings, hypos, actions, limits = _analyze_logs(evidence, eref)
        assert len(findings) == 0
        assert "No errors" in limits[0]

    def test_analyze_metrics(self):
        from handler import _analyze_metrics
        evidence = _load_fixture("sample_metrics_evidence.json")
        eref = {"collector_type": "metrics", "s3_bucket": "b", "s3_key": "k", "sha256": "h", "byte_size": 1}
        findings, hypos, actions, limits = _analyze_metrics(evidence, eref)
        assert len(findings) >= 1
        assert findings[0]["id"] == "metrics-collected"

    def test_analyze_stepfn_running_not_flagged(self):
        from handler import _analyze_stepfn
        evidence = {"sections": [{"name": "orchestrator_execution", "status": "RUNNING"}]}
        eref = {"collector_type": "stepfn", "s3_bucket": "b", "s3_key": "k", "sha256": "h", "byte_size": 1}
        findings, hypos, actions, limits = _analyze_stepfn(evidence, eref)
        assert all(f["id"] != "stepfn-orchestrator-failed" for f in findings)

    def test_analyze_stepfn_failed_is_flagged(self):
        from handler import _analyze_stepfn
        evidence = {"sections": [{"name": "orchestrator_execution", "status": "FAILED", "error": "Lambda timeout", "last_failed_state": "CollectLogs"}]}
        eref = {"collector_type": "stepfn", "s3_bucket": "b", "s3_key": "k", "sha256": "h", "byte_size": 1}
        findings, hypos, actions, limits = _analyze_stepfn(evidence, eref)
        assert any(f["id"] == "stepfn-orchestrator-failed" for f in findings)
        assert any("CollectLogs" in h["summary"] for h in hypos)

    def test_resolve_repo_candidates(self):
        from handler import _resolve_repo_candidates, RESOURCE_REPO_MAP
        manifest = {"service": "loggen"}
        evidence = {"logs": {"log_groups": ["/aws/lambda/opsrunbook-copilot-dev-loggen"]}}
        owners = _resolve_repo_candidates(manifest, evidence)
        repo_names = [o["repo"] for o in owners]
        assert "opsrunbook-copilot" in repo_names or "opsrunbook-copilot-test" in repo_names or "unknown" in repo_names
