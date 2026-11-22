"""Publish one advisory check per PR head with a link to the Actions run."""

from .github import GitHubClient, GitHubError, positive_id, revision_sha
from .report import check_summary, status_label
from .routing import MODE_LABELS

NAME = "Архитектура"


def _conclusion(result: dict) -> str:
    status = result.get("review_status", result["status"])
    if status == "error":
        return "failure"
    if status == "unsupported" and "no_supported_manifests" in result.get("diagnostic_codes", []):
        return "skipped"
    if status == "clean" and result.get("coverage") == "complete_scoped" and result.get("mode") != "agent":
        return "success"
    return "neutral"


def publish_check(api: GitHubClient, result: dict, run_id: int) -> tuple[int, str]:
    repository, sha = result["repository"], revision_sha(result["head_sha"])
    number = positive_id(result["number"])
    name = f"{NAME} · PR № {number}"
    external_id = f"architecture-review/pr/{number}"
    if result.get("manual"):
        mode = result["mode"]
        name += f" · {MODE_LABELS[mode]} (вручную)"
        external_id += f"/manual/{mode}"
    details = f"https://github.com/{repository}/actions/runs/{positive_id(run_id)}"
    existing = [check for check in api.checks(repository, sha, name)
                if check.get("head_sha") == sha and check.get("name") == name
                and check.get("external_id") == external_id
                and (check.get("app") or {}).get("slug") == "github-actions"]
    title = status_label(result).capitalize()
    summary = check_summary(result, run_id=run_id)
    payload = {"name": name, "external_id": external_id, "details_url": details,
               "status": "completed", "conclusion": _conclusion(result),
               "output": {"title": title[:255], "summary": summary}}
    if existing:
        check_id = max(positive_id(record["id"]) for record in existing)
        record = api.edit_check(repository, check_id, payload)
        action = "updated"
    else:
        record = api.create_check(repository, sha, payload)
        action = "created"
    if not isinstance(record, dict) or record.get("head_sha") != sha:
        raise GitHubError("unconfirmed_check_delivery")
    return positive_id(record.get("id")), action
