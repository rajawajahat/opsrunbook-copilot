"""CLI entrypoint for the coding agent.

Usage:
    # From a local JSON packet file:
    python -m packages.agent.runner --packet-file tests/fixtures/sample_packet.json --repo owner/repo

    # From an S3 packet (requires AWS credentials):
    python -m packages.agent.runner --s3-bucket my-bucket --s3-key path/to/packet.json --repo owner/repo

Environment variables:
    GROQ_API_KEY       — Groq API key (or reads from SSM)
    GITHUB_TOKEN       — GitHub PAT with repo scope
    LLM_PROVIDER       — "groq" (default) or "stub"
    LLM_MODEL          — Override the default model
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

from .agent import run_agent
from .github_tools import GitHubAPI


def _load_packet_from_file(path: str) -> dict:
    with open(path) as f:
        return json.load(f)


def _load_packet_from_s3(bucket: str, key: str) -> dict:
    import boto3
    s3 = boto3.client("s3")
    resp = s3.get_object(Bucket=bucket, Key=key)
    return json.loads(resp["Body"].read().decode("utf-8"))


def _get_llm():
    provider = os.environ.get("LLM_PROVIDER", "groq")
    model = os.environ.get("LLM_MODEL", "")

    if provider == "stub":
        print("[runner] LLM provider is 'stub' — agent will not work without an LLM")
        sys.exit(1)

    raw_key = os.environ.get("GROQ_API_KEY", "").strip()
    try:
        api_key = json.loads(raw_key)
    except (json.JSONDecodeError, TypeError):
        api_key = raw_key
    if provider == "groq":
        if not api_key:
            print("[runner] GROQ_API_KEY not set. Set it or use SSM via Lambda.")
            sys.exit(1)
        print(f"[runner] GROQ_API_KEY: length={len(api_key)}, prefix={api_key[:4]}...")
        from langchain_groq import ChatGroq
        return ChatGroq(
            model=model or "meta-llama/llama-4-scout-17b-16e-instruct",
            api_key=api_key,
            temperature=0.2,
            max_retries=2,
        )

    print(f"[runner] Unknown LLM provider: {provider}")
    sys.exit(1)


def _get_github(repo_str: str) -> GitHubAPI:
    raw_token = os.environ.get("GITHUB_TOKEN", "").strip()
    try:
        token = json.loads(raw_token)
    except (json.JSONDecodeError, TypeError):
        token = raw_token
    if not token:
        import subprocess
        try:
            result = subprocess.run(
                ["gh", "auth", "token"], capture_output=True, text=True, timeout=10,
            )
            token = result.stdout.strip()
        except Exception:
            pass
    if not token:
        print("[runner] GITHUB_TOKEN not set and 'gh auth token' failed.")
        print("[runner] Run: export GITHUB_TOKEN=$(gh auth token)")
        sys.exit(1)

    print(f"[runner] GITHUB_TOKEN: length={len(token)}, prefix={token[:4]}...")

    parts = repo_str.split("/")
    if len(parts) != 2:
        print(f"[runner] --repo must be owner/repo, got: {repo_str}")
        sys.exit(1)

    owner, repo = parts
    gh = GitHubAPI(token=token, owner=owner, repo=repo)
    try:
        default_branch = gh.get_default_branch()
    except Exception as e:
        print(f"[runner] Failed to access {owner}/{repo}: {e}")
        print("[runner] Check that GITHUB_TOKEN has repo access. Try: curl -H 'Authorization: Bearer $GITHUB_TOKEN' https://api.github.com/user")
        sys.exit(1)
    print(f"[runner] Target repo: {owner}/{repo} (default branch: {default_branch})")
    return gh


def _validate_edits(gh: GitHubAPI, edits) -> list:
    """Pre-validate that proposed edits target existing files with matching content."""
    valid = []
    for edit in edits:
        content = gh.read_file(edit.file_path)
        if content is None:
            print(f"  [SKIP] {edit.file_path}: file does not exist in repo")
            continue
        if content.startswith("["):
            print(f"  [SKIP] {edit.file_path}: {content}")
            continue
        if edit.old_code not in content:
            print(f"  [SKIP] {edit.file_path}: old_code not found (possible hallucination)")
            continue
        valid.append(edit)
    return valid


def _create_pr(gh: GitHubAPI, result, packet: dict, dry_run: bool):
    """Apply edits and create a PR."""
    if not result.proposed_edits:
        print("\n[runner] No edits proposed. Skipping PR creation.")
        return

    print(f"\n[runner] Validating {len(result.proposed_edits)} proposed edit(s)...")
    valid_edits = _validate_edits(gh, result.proposed_edits)

    if not valid_edits:
        print("[runner] No valid edits after validation. Skipping PR creation.")
        return

    print(f"[runner] {len(valid_edits)}/{len(result.proposed_edits)} edits validated.")

    if dry_run:
        print(f"\n[runner] DRY RUN — would create PR with {len(valid_edits)} edit(s):")
        for i, edit in enumerate(valid_edits, 1):
            print(f"  {i}. {edit.file_path}: {edit.rationale}")
        return

    incident_id = packet.get("incident_id", "unknown")
    branch_name = f"opsrunbook/fix-{incident_id}-{int(time.time())}"
    print(f"\n[runner] Creating branch: {branch_name}")
    gh.create_branch(branch_name)

    applied = 0
    failed = 0
    for i, edit in enumerate(valid_edits, 1):
        try:
            gh.apply_edit(
                branch=branch_name,
                file_path=edit.file_path,
                old_code=edit.old_code,
                new_code=edit.new_code,
                message=f"fix({incident_id}): {edit.rationale[:72]}",
            )
            print(f"  [{i}] Applied: {edit.file_path}")
            applied += 1
        except Exception as e:
            print(f"  [{i}] FAILED: {edit.file_path} — {e}")
            failed += 1

    if applied == 0:
        print("[runner] No edits applied successfully. Skipping PR.")
        return

    pr_body = _build_pr_body(result, packet, applied, failed)
    pr = gh.create_pull_request(
        title=f"[OpsRunbook Agent] Fix for {incident_id}",
        body=pr_body,
        head=branch_name,
    )

    print(f"\n[runner] PR created: {pr.get('html_url', '?')}")
    print(f"  Edits applied: {applied}, failed: {failed}")


def _build_pr_body(result, packet: dict, applied: int, failed: int) -> str:
    incident_id = packet.get("incident_id", "unknown")
    service = packet.get("service", "unknown")

    lines = [
        "## Automated Fix by OpsRunbook Agent",
        "",
        f"**Incident**: `{incident_id}`",
        f"**Service**: `{service}`",
        f"**Edits applied**: {applied} | **Failed**: {failed}",
        "",
        "---",
        "",
        "### Agent Summary",
        "",
        result.summary,
        "",
        "---",
        "",
        "### Proposed Changes",
        "",
    ]

    for i, edit in enumerate(result.proposed_edits, 1):
        lines.append(f"**{i}. `{edit.file_path}`**")
        lines.append(f"- {edit.rationale}")
        lines.append("")

    lines.extend([
        "---",
        "",
        "> **This PR was generated automatically and requires human review before merging.**",
        f"> Agent used {result.iterations} iterations and {len(result.tool_calls)} tool calls.",
    ])

    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="OpsRunbook Coding Agent")
    parser.add_argument("--packet-file", help="Path to local incident packet JSON")
    parser.add_argument("--s3-bucket", help="S3 bucket containing the packet")
    parser.add_argument("--s3-key", help="S3 key for the packet")
    parser.add_argument("--repo", required=True, help="Target GitHub repo (owner/repo)")
    parser.add_argument("--dry-run", action="store_true", help="Don't create branch/PR")
    parser.add_argument("--quiet", action="store_true", help="Minimal output")

    args = parser.parse_args()

    if args.packet_file:
        packet = _load_packet_from_file(args.packet_file)
    elif args.s3_bucket and args.s3_key:
        packet = _load_packet_from_s3(args.s3_bucket, args.s3_key)
    else:
        print("Error: provide --packet-file or --s3-bucket + --s3-key")
        sys.exit(1)

    print(f"[runner] Loaded packet for incident: {packet.get('incident_id', '?')}")

    llm = _get_llm()
    gh = _get_github(args.repo)
    result = run_agent(llm=llm, packet=packet, gh=gh, verbose=not args.quiet)

    print(f"\n[runner] Agent Summary:\n{result.summary}")

    _create_pr(gh, result, packet, dry_run=args.dry_run)

    report_path = f"agent_report_{packet.get('incident_id', 'unknown')}.json"
    with open(report_path, "w") as f:
        json.dump(result.model_dump(), f, indent=2, default=str)
    print(f"[runner] Report saved to: {report_path}")


if __name__ == "__main__":
    main()
