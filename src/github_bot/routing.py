"""Bind a completed read-only workflow signal to one current pull request."""

from datetime import datetime

from .github import GitHubClient, positive_id, repo_segment, revision_sha

SIGNAL = ".github/workflows/architecture-review-request.yml"
TARGET = "main"


def _time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("signal_timestamp")
    return parsed


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
