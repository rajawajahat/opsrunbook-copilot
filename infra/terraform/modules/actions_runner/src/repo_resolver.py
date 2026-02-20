"""
Iteration 7 – Deterministic Repo Resolver.

Priority:
  1. Mapping rules (prefix/exact match on signals) → high confidence
  2. Trace-driven verification (file_exists on GitHub) → strong confidence
  3. Heuristic fallback (existing suspected_owners) → low confidence

Returns a RepoResolution with repo_full_name, confidence, reasons list,
and a verification status.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Any, Protocol

from trace_parser import extract_app_frames, TraceFrame

MAPPING_PATH = os.path.join(os.path.dirname(__file__), "repo_mapping.json")
MAX_VERIFY_CALLS = 4


@dataclass
class RepoResolution:
    repo_full_name: str
    confidence: float
    reasons: list[str] = field(default_factory=list)
    verification: str = "none"  # none | mapping | verified | unverified
    trace_frames: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "repo_full_name": self.repo_full_name,
            "confidence": self.confidence,
            "reasons": self.reasons,
            "verification": self.verification,
            "trace_frames": self.trace_frames,
        }


@dataclass
class MappingRule:
    type: str       # "prefix" | "exact"
    signal: str     # "lambda_name" | "log_group" | "service_name" | "state_machine"
    pattern: str
    repo: str

    def matches(self, value: str) -> bool:
        if self.type == "exact":
            return value == self.pattern
        if self.type == "prefix":
            return value.startswith(self.pattern)
        return False


class FileChecker(Protocol):
    def file_exists(self, repo_full_name: str, path: str) -> bool: ...


def load_mapping_rules(path: str | None = None) -> list[MappingRule]:
    path = path or MAPPING_PATH
    if not os.path.exists(path):
        return []
    with open(path) as f:
        data = json.load(f)
    rules_raw = data.get("rules", [])
    return [
        MappingRule(
            type=r.get("type", "prefix"),
            signal=r.get("signal", ""),
            pattern=r.get("pattern", ""),
            repo=r.get("repo", ""),
        )
        for r in rules_raw
        if r.get("repo")
    ]


def _extract_signals(packet: dict) -> dict[str, list[str]]:
    """Pull matchable signal values from a packet / evidence."""
    signals: dict[str, list[str]] = {
        "service_name": [],
        "lambda_name": [],
        "log_group": [],
        "state_machine": [],
    }

    service = packet.get("service", "")
    if service:
        signals["service_name"].append(service)

    for eref in packet.get("all_evidence_refs", []):
        sk = eref.get("s3_key", "")
        # Lambda names from evidence keys
        if "lambda" in sk.lower():
            parts = sk.split("/")
            for p in parts:
                if p.startswith("opsrunbook") or p.startswith("billing") or p.startswith("spaces"):
                    signals["lambda_name"].append(p)

    # From findings text
    for finding in packet.get("findings", []):
        summary = finding.get("summary", "")
        # Extract log group references
        for m in re.finditer(r'/aws/lambda/([\w-]+)', summary):
            signals["lambda_name"].append(m.group(1))
            signals["log_group"].append(f"/aws/lambda/{m.group(1)}")
        for m in re.finditer(r'arn:aws:states:[^:]+:\d+:stateMachine:([\w-]+)', summary):
            signals["state_machine"].append(m.group(1))

    # From suspected_owners reasons
    for owner in packet.get("suspected_owners", []):
        for reason in owner.get("reasons", []):
            for m in re.finditer(r'/aws/lambda/([\w-]+)', reason):
                signals["log_group"].append(f"/aws/lambda/{m.group(1)}")
                signals["lambda_name"].append(m.group(1))

    return signals


def _match_rules(
    rules: list[MappingRule], signals: dict[str, list[str]],
) -> RepoResolution | None:
    """Check mapping rules against signals. First match wins."""
    for rule in rules:
        for value in signals.get(rule.signal, []):
            if rule.matches(value):
                return RepoResolution(
                    repo_full_name=rule.repo,
                    confidence=0.95,
                    reasons=[f"mapping rule: {rule.type} {rule.signal}='{rule.pattern}' → {rule.repo}"],
                    verification="mapping",
                )
    return None


def _verify_with_github(
    checker: FileChecker,
    candidates: list[str],
    trace_paths: list[str],
) -> tuple[str, list[str]] | None:
    """Try file_exists for top candidates × top paths (bounded).

    Returns (repo_full_name, reasons) or None.
    """
    calls = 0
    for repo in candidates[:2]:
        for path in trace_paths[:2]:
            if calls >= MAX_VERIFY_CALLS:
                return None
            calls += 1
            if checker.file_exists(repo, path):
                return repo, [f"verified: {path} exists in {repo}"]
    return None


def resolve_repo(
    packet: dict,
    rules: list[MappingRule] | None = None,
    checker: FileChecker | None = None,
    owner: str = "",
    legacy_map: dict | None = None,
) -> RepoResolution:
    """Main entry point for repo resolution.

    Parameters
    ----------
    packet : dict
        The IncidentPacket.
    rules : list[MappingRule] | None
        Loaded mapping rules. Will load from default file if None.
    checker : FileChecker | None
        GitHub file existence checker. Skips verification if None.
    owner : str
        GitHub owner prefix (e.g. "rajawajahat").
    legacy_map : dict | None
        The old config/resource_repo_map.json dict for backward compat.
    """
    if rules is None:
        rules = load_mapping_rules()

    signals = _extract_signals(packet)

    # Extract trace frames from findings
    all_frames: list[TraceFrame] = []
    for finding in packet.get("findings", []):
        text = finding.get("summary", "") + "\n" + finding.get("notes", "")
        all_frames.extend(extract_app_frames(text))

    frame_dicts = [f.to_dict() for f in all_frames[:5]]

    # ── Priority 1: mapping rules ────────────────────────────────
    mapping_result = _match_rules(rules, signals)
    if mapping_result:
        mapping_result.trace_frames = frame_dicts
        return mapping_result

    # ── Priority 2: trace-driven verification ────────────────────
    trace_paths = [f.normalized_path for f in all_frames if f.normalized_path]

    heuristic_repos: list[str] = []
    # From suspected_owners
    for owner_entry in packet.get("suspected_owners", []):
        repo = owner_entry.get("repo", "")
        if repo and owner:
            full = f"{owner}/{repo}" if "/" not in repo else repo
            if full not in heuristic_repos:
                heuristic_repos.append(full)

    # From legacy map
    if legacy_map:
        service = packet.get("service", "")
        mapped = legacy_map.get(service, "")
        if mapped and owner:
            full = f"{owner}/{mapped}" if "/" not in mapped else mapped
            if full not in heuristic_repos:
                heuristic_repos.insert(0, full)

    if checker and trace_paths and heuristic_repos:
        verified = _verify_with_github(checker, heuristic_repos, trace_paths)
        if verified:
            repo_name, reasons = verified
            return RepoResolution(
                repo_full_name=repo_name,
                confidence=0.85,
                reasons=reasons,
                verification="verified",
                trace_frames=frame_dicts,
            )

    # ── Priority 3: heuristic fallback ───────────────────────────
    if heuristic_repos:
        best = heuristic_repos[0]
        return RepoResolution(
            repo_full_name=best,
            confidence=0.5,
            reasons=[f"heuristic: best candidate from suspected_owners / legacy map"],
            verification="unverified",
            trace_frames=frame_dicts,
        )

    return RepoResolution(
        repo_full_name="",
        confidence=0.0,
        reasons=["no repo could be determined"],
        verification="unverified",
        trace_frames=frame_dicts,
    )
