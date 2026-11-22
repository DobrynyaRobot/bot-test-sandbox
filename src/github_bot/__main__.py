"""Review the PR event and write a structured receipt and an Actions summary."""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

from . import GitHubClient, PullRequestContext, _current, process_event
from .agent import select_reviewer
from .checks import publish_check
from .github import GitHubError, InputLimit, positive_id, revision_sha
from .report import review_markdown, workflow_annotation
from .routing import manual_inputs, resolve_manual, resolve_signal

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


def _write_summary(result: dict, context: PullRequestContext | None) -> bool:
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if not summary:
        return False
    if context:
        result = {"repository": context.repository, "number": context.number,
                  "head_sha": context.head_sha, **result}
    try:
        with Path(summary).open("a", encoding="utf-8") as file:
            file.write(review_markdown(result))
    except OSError:
        print("Could not write the GitHub Actions summary.", file=sys.stderr)
        return False
    return True


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", default=os.environ.get("GITHUB_REPOSITORY"))
    parser.add_argument("--event", type=Path, default=os.environ.get("GITHUB_EVENT_PATH"))
    parser.add_argument("--policy", type=Path, default=os.environ.get("ARCH_REVIEW_POLICY") or ".github/architecture-review.json")
    parser.add_argument("--verify-checkout", action="store_true")
    trigger = parser.add_mutually_exclusive_group()
    trigger.add_argument("--workflow-run", action="store_true", help="Resolve a trusted workflow_run signal")
    trigger.add_argument("--workflow-dispatch", action="store_true", help="Review a PR in the selected manual mode")
    parser.add_argument("--api-url", default=os.environ.get("GITHUB_API_URL", "https://api.github.com"))
    args = parser.parse_args(argv)
    # Keep API credentials in their Python callers and out of subprocesses.
    token = os.environ.pop("GITHUB_TOKEN", "")
    openai_key = os.environ.pop("OPENAI_API_KEY", "")
    actions_event = args.workflow_run or args.workflow_dispatch
    mode = "aact"
    try:
        run_id = positive_id(int(os.environ.get("GITHUB_RUN_ID", "")))
    except ValueError:
        run_id = None
    context = None
    try:
        if not args.repository or not args.event:
            raise ValueError("missing_github_context")
        event = _event(args.event)
        checkout = Path.cwd().resolve()
        policy = (checkout / args.policy).resolve()
        if not policy.is_relative_to(checkout):
            raise ValueError("policy_outside_checkout")
        api = GitHubClient(token, base_url=args.api_url)
        if actions_event:
            if not args.verify_checkout or not run_id:
                raise ValueError("workflow_run_requires_verified_checkout")
            workflow_sha = revision_sha(os.environ.get("GITHUB_WORKFLOW_SHA"))
            _verify_checkout(checkout, workflow_sha)
            if args.workflow_dispatch:
                number, mode = manual_inputs(event, os.environ.get("GITHUB_REF", ""))
                event = resolve_manual(api, event, args.repository, number)
            else:
                event = resolve_signal(api, event, args.repository)
        if event is None:
            status, reason = (("error", "manual_pr_not_open") if args.workflow_dispatch else
                              ("superseded", "signal_without_current_pr"))
            result = {"status": status, "action": "none", "findings": [], "reason": reason}
        else:
            context = PullRequestContext.from_event(event, args.repository)
            base_policy_path = None
            if args.verify_checkout:
                workflow_sha = revision_sha(os.environ.get("GITHUB_WORKFLOW_SHA") or context.base_sha)
                if not actions_event:
                    _verify_checkout(checkout, workflow_sha)
                if workflow_sha != context.base_sha:
                    base_policy_path = policy.relative_to(checkout).as_posix()
            setup_failed = actions_event and os.environ.get("ARCH_REVIEW_SETUP_FAILED") == "true"
            if setup_failed and mode == "aact":
                result = {"status": "error", "action": "none", "findings": [], "reason": "engine_setup_failed"}
            else:
                reviewer = select_reviewer(mode, openai_key, formal_setup_failed=setup_failed)
                result = process_event(api, event, args.repository, policy, run_id=run_id, base_policy_path=base_policy_path,
                                       reviewer=reviewer, publish_comments=not args.workflow_dispatch)
            result.update(mode=mode, manual=args.workflow_dispatch)
            if actions_event and result["status"] != "superseded":
                try:
                    result = {"repository": context.repository, "head_sha": context.head_sha,
                              "number": context.number, **result}
                    if _current(api, context) is None:
                        result["check_action"] = "superseded"
                    else:
                        result["check_id"], result["check_action"] = publish_check(api, result, run_id)
                except (GitHubError, InputLimit, TypeError, ValueError):
                    result["check_action"] = "unconfirmed"
    except (GitHubError, InputLimit, OSError, ValueError, TypeError, KeyError, AttributeError, subprocess.SubprocessError) as exc:
        reason = exc.code if isinstance(exc, GitHubError) else "invalid_checkout_or_github_context"
        result = {"status": "error", "action": "none", "findings": [], "reason": reason}

    result.update(mode=mode, manual=args.workflow_dispatch)
    summary_written = _write_summary(result, context)
    allowed = {"repository", "number", "base_sha", "head_sha", "effective_base_sha", "status",
               "action", "reason", "review_status", "delivery_status", "coverage", "scope_code",
               "diagnostic_codes", "comment_id", "check_id", "check_action", "unverified_previous",
               "mode", "manual", "agent_model", "agent_usage", "arm_status"}
    receipt = {k: v for k, v in result.items() if k in allowed}
    receipt["findings_count"] = len(result.get("findings", []))
    receipt["resolved_count"] = len(result.get("confirmations", []))
    if os.environ.get("GITHUB_ACTIONS") == "true":
        print(workflow_annotation(result), flush=True)
        if result.get("check_action") == "unconfirmed":
            print("::warning title=Checks::GitHub не подтвердил публикацию в Checks. Итог есть в отчёте запуска.", flush=True)
        receipt["summary_written"] = summary_written
    print(json.dumps(receipt, sort_keys=True), flush=True)
    return 2 if result["status"] == "error" else 0


if __name__ == "__main__":
    sys.exit(main())
