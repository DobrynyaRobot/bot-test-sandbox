"""Review the PR event and write a structured receipt and an Actions summary."""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

from . import GitHubClient, PullRequestContext, process_event
from .github import positive_id, revision_sha
from .report import review_markdown

MAX_EVENT_BYTES = 1_048_576


def _event(path: Path) -> dict:
    with path.open("rb") as file:
        data = file.read(MAX_EVENT_BYTES + 1)
    if len(data) > MAX_EVENT_BYTES:
        raise ValueError("large_event")
    return json.loads(data)


def _verify_checkout(path: Path, sha: str) -> None:
    result = subprocess.run(
        ("git", "-C", str(path), "rev-parse", "--verify", "HEAD"),
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        timeout=10,
        text=True,
    )
    if result.stdout.strip() != sha:
        raise ValueError("checkout_revision_mismatch")


def _write_summary(result: dict, context: PullRequestContext | None) -> None:
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if not summary:
        return
    if context:
        result = {"repository": context.repository, "number": context.number,
                  "head_sha": context.head_sha, **result}
    try:
        with Path(summary).open("a", encoding="utf-8") as file:
            file.write(review_markdown(result))
    except OSError:
        print("Could not write the GitHub Actions summary.", file=sys.stderr)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", default=os.environ.get("GITHUB_REPOSITORY"))
    parser.add_argument("--event", type=Path, default=os.environ.get("GITHUB_EVENT_PATH"))
    parser.add_argument("--policy", type=Path, default=os.environ.get("ARCH_REVIEW_POLICY") or ".github/architecture-review.json")
    parser.add_argument("--verify-checkout", action="store_true")
    parser.add_argument("--api-url", default=os.environ.get("GITHUB_API_URL", "https://api.github.com"))
    args = parser.parse_args(argv)
    # Credentials remain solely in the Python GitHub transport. Subprocesses
    # including Git and the formal reviewer do not inherit this token.
    token = os.environ.pop("GITHUB_TOKEN", "")
    try:
        run_id = positive_id(int(os.environ.get("GITHUB_RUN_ID", "")))
    except ValueError:
        run_id = None
    context = None
    try:
        if not args.repository or not args.event:
            raise ValueError("missing_github_context")
        event = _event(args.event)
        context = PullRequestContext.from_event(event, args.repository)
        checkout = Path.cwd().resolve()
        policy = (checkout / args.policy).resolve()
        if not policy.is_relative_to(checkout):
            raise ValueError("policy_outside_checkout")
        base_policy_path = None
        if args.verify_checkout:
            workflow_sha = revision_sha(os.environ.get("GITHUB_WORKFLOW_SHA") or context.base_sha)
            _verify_checkout(checkout, workflow_sha)
            if workflow_sha != context.base_sha:
                base_policy_path = policy.relative_to(checkout).as_posix()
        result = process_event(
            GitHubClient(token, base_url=args.api_url), event, args.repository, policy,
            run_id=run_id, base_policy_path=base_policy_path,
        )
    except (OSError, ValueError, TypeError, KeyError, AttributeError, subprocess.SubprocessError):
        result = {"status": "error", "action": "none", "findings": [], "reason": "invalid_checkout_or_github_context"}

    _write_summary(result, context)
    allowed = {"repository", "number", "base_sha", "head_sha", "effective_base_sha", "status",
               "action", "reason", "review_status", "delivery_status", "coverage", "scope_code",
               "diagnostic_codes", "comment_id"}
    receipt = {k: v for k, v in result.items() if k in allowed}
    receipt["findings_count"] = len(result.get("findings", []))
    print(json.dumps(receipt, sort_keys=True), flush=True)
    return 2 if result["status"] == "error" else 0


if __name__ == "__main__":
    sys.exit(main())
