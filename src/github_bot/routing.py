"""Bind a trusted workflow event to one current pull request."""

from datetime import datetime
import re

from .github import GitHubClient, GitHubError, positive_id, repo_segment, revision_sha

SIGNAL = ".github/workflows/architecture-review-request.yml"
TARGET = "main"
MODE_LABELS = {"aact": "AACT", "agent": "Агент", "hybrid": "Гибрид"}


def _time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("signal_timestamp")
    return parsed


def manual_inputs(event: dict, ref: str) -> tuple[int, str]:
    if ref != f"refs/heads/{TARGET}":
        raise ValueError("manual_review_ref")
    inputs = event["inputs"]
    number, mode = inputs["pr_number"], inputs.get("mode", "aact")
    if not re.fullmatch(r"[1-9][0-9]{0,17}", str(number)) or not isinstance(mode, str) or mode not in MODE_LABELS:
        raise ValueError("invalid_manual_inputs")
    return positive_id(int(number)), mode


def resolve_manual(api: GitHubClient, event: dict, repository: str, number: int) -> dict | None:
    repo_segment(repository)
    event_repo = event["repository"]
    if event_repo["full_name"].casefold() != repository.casefold():
        raise ValueError("manual_review_repository")
    try:
        pr = api.pr(repository, number)
    except GitHubError as exc:
        if exc.status == 404:
            return None
        raise
    base = pr["base"]
    if (pr.get("number") != number or base["repo"]["id"] != positive_id(event_repo["id"])
            or base["repo"]["full_name"].casefold() != repository.casefold()):
        raise ValueError("manual_review_repository")
    if pr.get("state") != "open" or base["ref"] != TARGET:
        return None
    return {"number": number, "pull_request": pr}


def resolve_signal(api: GitHubClient, event: dict, repository: str) -> dict | None:
    """Use only GitHub event metadata and live PR data. No artifacts are read."""
    repo_segment(repository)
    run = event["workflow_run"]
    event_repo = event["repository"]
    repo_id = positive_id(event_repo["id"])
    run_repo = run["repository"]
    if (event.get("action") != "completed" or run.get("event") != "pull_request"
            or run.get("status") != "completed" or run.get("conclusion") != "success"
            or run["path"].partition("@")[0] != SIGNAL
            or run_repo["id"] != repo_id or run_repo["full_name"].casefold() != repository.casefold()
            or event_repo["full_name"].casefold() != repository.casefold()):
        return None
    positive_id(run["id"])
    head_repo = run.get("head_repository")
    if not head_repo:
        return None
    head_repo_id = positive_id(head_repo["id"])
    head_name = head_repo["full_name"]
    repo_segment(head_name)
    branch = run["head_branch"]
    if not isinstance(branch, str) or not branch or len(branch) > 1024 or not branch.isprintable():
        raise ValueError("signal_branch")
    sha = revision_sha(run["head_sha"])
    signal_time = _time(run["created_at"])

    # Some fork events have no PR hint. Constrain the fallback to the head
    # owner and branch and require one exact open PR before doing any writes.
    hints = run.get("pull_requests", [])
    if not isinstance(hints, list) or len(hints) > 20:
        raise ValueError("signal_pr_hints")
    if hints:
        numbers = {positive_id(hint["number"]) for hint in hints}
        candidates = [api.pr(repository, number) for number in sorted(numbers)]
    else:
        owner = head_name.split("/", 1)[0]
        candidates = api.open_pulls(repository, f"{owner}:{branch}", TARGET)

    matches = []
    for pr in candidates:
        base, head = pr["base"], pr["head"]
        live_head_repo = head.get("repo") or {}
        if (pr.get("state") == "open" and base["ref"] == TARGET and base["repo"]["id"] == repo_id
                and base["repo"]["full_name"].casefold() == repository.casefold()
                and head["sha"] == sha and head["ref"] == branch
                and live_head_repo.get("id") == head_repo_id
                and live_head_repo["full_name"].casefold() == head_name.casefold()
                and _time(pr["created_at"]) <= signal_time):
            matches.append(pr)
    if len(matches) != 1:
        return None
    return {"number": positive_id(matches[0]["number"]), "pull_request": matches[0]}
