"""Comment-only GitHub integration for the packaged AACT reviewer."""

from __future__ import annotations

import re
from contextlib import ExitStack
from dataclasses import dataclass, replace
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Callable

from .github import GitHubClient, GitHubError, InputLimit, positive_id, repo_segment, revision_sha, url_path
from .report import MARKER, line_in_patch, review_markdown

REVIEW_STATUSES = {"violation", "clean", "unsupported", "error"}
FILE_STATUSES = {"added", "removed", "modified", "renamed", "copied", "changed", "unchanged"}
REVIEW_COVERAGE = {"complete_scoped", "partial", "unsupported", "unknown"}
FORMAL_SCOPE = "Direct dependency declarations in changed package.json files under configured repository paths."
SOURCE_DIAGNOSTICS = {
    "optionalDependencies": "optional_dependencies",
    "peerDependencies": "peer_dependencies",
    "bundleDependencies": "bundled_dependencies",
    "bundledDependencies": "bundled_dependencies",
    "source-role-unknown": "source_role_unknown",
    "npm-alias": "npm_alias",
    "baseline_edge_unresolved": "baseline_edge_unresolved",
}
KNOWN_DIAGNOSTIC_PREFIXES = {
    "No formal policy is configured": "policy_repository_unsupported",
    "No changed package manifests": "no_supported_manifests",
    "Required immutable blob is unavailable": "immutable_blob_missing",
    "Immutable blob exceeds": "blob_size_limit",
    "Too many package snapshots": "manifest_count_limit",
    "Combined package data exceed": "manifest_size_limit",
    "Base architecture document does not contain": "base_rule_anchors_missing",
    "AACT processing timed out": "reviewer_timeout",
    "AACT model projection failed": "manifest_projection_failed",
    "AACT engine is missing": "engine_missing",
}


class CommentDeliveryError(Exception):
    def __init__(self, reason: str, uncertain: bool):
        self.reason = reason
        self.uncertain = uncertain
        super().__init__(reason)


@dataclass(frozen=True)
class PullRequestContext:
    repository: str
    number: int
    base_sha: str
    head_sha: str
    effective_base_sha: str | None = None
    repository_id: int | None = None

    @classmethod
    def from_event(cls, event: dict, repository: str):
        repo_segment(repository)
        pr = event["pull_request"]
        number = positive_id(event.get("number", pr.get("number")))
        _checked_base_repository(pr, repository)
        if pr.get("number", number) != number:
            raise ValueError("event_pr_number_mismatch")
        return cls(repository, number, revision_sha(pr["base"]["sha"]), revision_sha(pr["head"]["sha"]))

    @property
    def review_base_sha(self) -> str:
        return self.effective_base_sha or self.base_sha


def _checked_base_repository(pr: dict, expected: str) -> None:
    name = pr["base"]["repo"]["full_name"]
    repo_segment(name)
    if name.casefold() != expected.casefold():
        raise ValueError("base_repository_mismatch")


def _base_repository_id(pr: dict) -> int | None:
    value = pr["base"]["repo"].get("id")
    return None if value is None else positive_id(value)


def _current(api: GitHubClient, context: PullRequestContext) -> dict | None:
    pr = api.pr(context.repository, context.number)
    _checked_base_repository(pr, context.repository)
    if pr.get("number", context.number) != context.number:
        raise GitHubError("github_pr_number_mismatch")
    if context.repository_id is not None and _base_repository_id(pr) != context.repository_id:
        raise GitHubError("github_repository_id_mismatch")
    if (
        pr.get("state") != "open"
        or pr["base"]["sha"] != context.base_sha
        or pr["head"]["sha"] != context.head_sha
    ):
        return None
    return pr


def _review_result(value: dict) -> dict:
    if not isinstance(value, dict) or value.get("status") not in REVIEW_STATUSES:
        return {"status": "error", "findings": [], "reason": "invalid_reviewer_result"}
    findings = value.get("findings", [])
    if (
        not isinstance(findings, list)
        or len(findings) > 5000
        or (value["status"] == "violation") != bool(findings)
    ):
        return {"status": "error", "findings": [], "reason": "invalid_reviewer_findings"}
    keys = ("rule", "file", "problem", "recommendation", "source")
    if any(not isinstance(f, dict) or not all(isinstance(f.get(k), str) and f[k].strip() for k in keys)
           for f in findings):
        return {"status": "error", "findings": [], "reason": "invalid_finding"}
    coverage = value.get("coverage")
    scope = "direct_package_declarations" if value.get("scope") == FORMAL_SCOPE else "unknown"
    diagnostics = value.get("diagnostics", [])
    codes = set()
    if isinstance(diagnostics, list):
        for diagnostic in diagnostics[:100]:
            if isinstance(diagnostic, dict) and isinstance(diagnostic.get("reason"), str):
                codes.add(SOURCE_DIAGNOSTICS.get(diagnostic["reason"], "unclassified"))
            elif isinstance(diagnostic, str):
                codes.add(next((code for prefix, code in KNOWN_DIAGNOSTIC_PREFIXES.items()
                                if diagnostic.startswith(prefix)), "unclassified"))
            else:
                codes.add("unclassified")
    elif diagnostics:
        codes.add("unclassified")
    return {
        "status": value["status"],
        "findings": findings,
        "reason": "reviewer_" + value["status"],
        "coverage": coverage if isinstance(coverage, str) and coverage in REVIEW_COVERAGE else "unknown",
        "scope_code": scope,
        "diagnostic_codes": sorted(codes),
    }


def _comment_write(operation, *args):
    try:
        return operation(*args)
    except GitHubError as exc:
        uncertain = exc.code in ("github_transport_or_timeout", "github_invalid_json") or (
            exc.status is not None and (exc.status >= 500 or exc.status == 408)
        )
        raise CommentDeliveryError(exc.code, uncertain=uncertain) from None
    except InputLimit:
        # A response over the byte limit can arrive after the server applied the write.
        raise CommentDeliveryError("comment_response_limit", uncertain=True) from None


def _manage_comment(
    api: GitHubClient, context: PullRequestContext, result: dict, run_id: int | None
) -> tuple[str, int | None]:
    existing = []
    for comment in api.comments(context.repository, context.number, context.repository_id):
        user = comment.get("user")
        body = comment.get("body")
        if (
            isinstance(user, dict)
            and user.get("login") == "github-actions[bot]"
            and user.get("type") == "Bot"
            and isinstance(body, str)
            and body.partition("\n")[0] == MARKER
        ):
            existing.append(comment)
    if not existing and result["status"] != "violation":
        return "none", None
    # A newer PR event may have arrived while reading the inputs or comments.
    # GitHub doesn't offer a compare-and-swap endpoint for issue comments.
    if _current(api, context) is None:
        return "superseded", None
    body = review_markdown(result, run_id=run_id, comment=True)
    if not existing:
        comment_id = _comment_write(api.create_comment, context.repository, context.number, body)
        return "created", comment_id
    managed = min(existing, key=lambda comment: positive_id(comment["id"]))
    comment_id = managed["id"]
    if managed["body"] == body:
        return "unchanged", comment_id
    _comment_write(api.edit_comment, context.repository, comment_id, body)
    return "updated", comment_id


def _files(
    api: GitHubClient, context: PullRequestContext, pr: dict, max_files: int, max_patch_bytes: int
) -> tuple[list[dict], dict[str, str]]:
    total = pr.get("changed_files")
    if type(total) is not int or total < 0:
        raise GitHubError("github_missing_file_count")
    if total > max_files:
        raise InputLimit("pr_file_limit")
    all_files = api.files(context.repository, context.number, max_files, context.repository_id)
    if len(all_files) != total:
        raise GitHubError("github_incomplete_files")
    reduced = []
    patches = {}
    seen = set()
    patch_bytes = 0
    for entry in all_files:
        name, status = entry.get("filename"), entry.get("status")
        url_path(name)
        if name in seen or status not in FILE_STATUSES:
            raise GitHubError("github_invalid_file")
        seen.add(name)
        patch = entry.get("patch")
        if patch is not None:
            if not isinstance(patch, str):
                raise GitHubError("github_invalid_patch")
            patch_bytes += len(patch.encode("utf-8", errors="replace"))
            patches[name] = patch
        if patch_bytes > max_patch_bytes:
            raise InputLimit("pr_patch_limit")
        value = {"filename": name, "status": status}
        if status == "renamed":
            previous = entry.get("previous_filename")
            url_path(previous)
            value["previous_filename"] = previous
        reduced.append(value)
    return reduced, patches


def process_event(
    api: GitHubClient,
    event: dict,
    repository: str,
    policy_path: Path,
    reviewer: Callable | None = None,
    max_files: int = 300,
    max_patch_bytes: int = 512_000,
    run_id: int | None = None,
    base_policy_path: str | None = None,
) -> dict:
    try:
        context = PullRequestContext.from_event(event, repository)
        live = _current(api, context)
        if live is not None:
            context = replace(context, repository_id=_base_repository_id(live))
    except (GitHubError, InputLimit, ValueError, KeyError, TypeError, AttributeError) as exc:
        reason = exc.code if isinstance(exc, GitHubError) else "invalid_event_or_unavailable_pr"
        return {"status": "error", "action": "none", "findings": [], "reason": reason}
    identity = {
        "repository": context.repository,
        "number": context.number,
        "base_sha": context.base_sha,
        "head_sha": context.head_sha,
    }
    superseded = {**identity, "status": "superseded", "action": "none", "findings": []}
    if live is None:
        return superseded

    fork = live["head"].get("repo")
    fork_name = fork.get("full_name") if isinstance(fork, dict) else None

    def fetch_blob(commit: str, path: str) -> bytes | None:
        if commit not in {context.review_base_sha, context.head_sha}:
            raise ValueError("unexpected_blob_revision")
        blob = api.fetch_blob(context.repository, path, commit)
        if blob is None and commit == context.head_sha and fork_name:
            repo_segment(fork_name)
            if fork_name.casefold() != context.repository.casefold():
                return api.fetch_blob(fork_name, path, commit)
        return blob

    try:
        files, patches = _files(api, context, live, max_files, max_patch_bytes)
        effective = api.merge_base(context.repository, context.base_sha, context.head_sha, fork_name)
        context = replace(context, effective_base_sha=effective)
        identity["effective_base_sha"] = effective
        if _current(api, context) is None:
            return superseded
        if reviewer is None:
            from aact_reviewer import review
            reviewer = review
        with ExitStack() as temporary:
            if base_policy_path is not None:
                # Execute the current trusted reviewer, while using the same
                # policy bytes as a checkout of the PR's original base.
                policy_blob = api.fetch_blob(context.repository, base_policy_path, context.base_sha)
                if policy_blob is None:
                    raise InputLimit("base_policy_unavailable")
                directory = temporary.enter_context(TemporaryDirectory(prefix="architecture-policy-"))
                policy_path = Path(directory) / "policy.json"
                policy_path.write_bytes(policy_blob)
            result = _review_result(
                reviewer(
                    repo=context.repository,
                    base_sha=context.review_base_sha,
                    head_sha=context.head_sha,
                    files=files,
                    policy_path=Path(policy_path),
                    fetch_blob=fetch_blob,
                )
            )
        result["findings"] = [
            {**finding, "in_diff": line_in_patch(patches.get(finding["file"], ""), finding.get("line"))}
            for finding in result["findings"]
        ]
    except InputLimit as exc:
        # InputLimit only contains internal, non-sensitive reason codes.
        reason = exc.args[0] if exc.args and isinstance(exc.args[0], str) and re.fullmatch(r"[a-z_]{1,64}", exc.args[0]) else "input_limit"
        result = {"status": "unsupported", "findings": [], "reason": reason}
    except GitHubError as exc:
        result = {"status": "error", "findings": [], "reason": exc.code}
    except Exception:
        # Reviewer exceptions may contain a PR fragment, request body or path.
        # Do not propagate their text to workflows or comments.
        result = {"status": "error", "findings": [], "reason": "reviewer_or_input_error"}

    if fork_name:
        try:
            repo_segment(fork_name)
        except ValueError:
            pass
        else:
            identity["head_repository"] = fork_name
    try:
        if _current(api, context) is None:
            return {**identity, **result, "status": "superseded", "action": "none",
                    "review_status": result["status"], "delivery_status": "superseded"}
        action, comment_id = _manage_comment(api, context, {**identity, **result}, run_id)
        if action == "superseded":
            return {**identity, **result, "status": "superseded", "action": "none",
                    "review_status": result["status"], "delivery_status": "superseded"}
    except CommentDeliveryError as exc:
        action, delivery = ("unknown", "unknown") if exc.uncertain else ("none", "failed")
        return {**identity, **result, "status": "error", "action": action, "reason": exc.reason,
                "review_status": result["status"], "delivery_status": delivery}
    except (GitHubError, InputLimit, ValueError, KeyError, TypeError, AttributeError) as exc:
        reason = exc.code if isinstance(exc, GitHubError) else "comment_unavailable"
        return {**identity, **result, "status": "error", "action": "none", "reason": reason,
                "review_status": result["status"], "delivery_status": "failed"}
    if comment_id is not None:
        result["comment_id"] = comment_id
    return {**identity, **result, "action": action, "review_status": result["status"],
            "delivery_status": "not_needed" if action == "none" else "completed"}
