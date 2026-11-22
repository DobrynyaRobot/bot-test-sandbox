"""Routing and delivery of the review to the exact PR head."""

import copy
import unittest
from unittest.mock import Mock

from github_bot.checks import NAME, publish_check
from github_bot.github import GitHubError
from github_bot.routing import resolve_signal

REPO = {"id": 19, "full_name": "example/base"}
FORK = {"id": 27, "full_name": "someone/fork"}
BASE = "a" * 40
HEAD = "b" * 40


def pr(repo=FORK, sha=HEAD, number=9, created="2026-11-05T10:01:01Z"):
    return {"number": number, "state": "open", "created_at": created,
            "base": {"ref": "main", "sha": BASE, "repo": REPO},
            "head": {"ref": "fix/deps", "sha": sha, "repo": repo}}


def signal(repo=FORK, hints=()):
    return {"action": "completed", "repository": REPO, "workflow_run": {
        "id": 10, "path": ".github/workflows/architecture-review-request.yml",
        "event": "pull_request", "status": "completed", "conclusion": "success", "repository": REPO,
        "head_repository": repo, "head_branch": "fix/deps", "head_sha": HEAD,
        "created_at": "2026-11-05T10:01:02Z", "pull_requests": list(hints),
    }}


class SignalTests(unittest.TestCase):
    def test_use_githubs_pr_hint(self):
        api = Mock()
        api.pr.return_value = pr(repo=REPO)
        result = resolve_signal(api, signal(repo=REPO, hints=({"number": 9},)), REPO["full_name"])
        self.assertEqual(result["pull_request"]["head"]["sha"], HEAD)
        api.pr.assert_called_once_with(REPO["full_name"], 9)
        api.open_pulls.assert_not_called()

    def test_fork_without_hint_uses_exact_identity(self):
        api = Mock()
        api.open_pulls.return_value = [pr(sha="c" * 40, number=1), pr(repo=REPO, number=2), pr()]
        result = resolve_signal(api, signal(), REPO["full_name"])
        self.assertEqual(result["number"], 9)
        api.open_pulls.assert_called_once_with(REPO["full_name"], "someone:fix/deps", "main")

    def test_ambiguous_or_stale_fork_never_selects_a_pr(self):
        api = Mock()
        for candidates in ([pr(), pr(number=10)], [pr(sha="c" * 40)],
                           [pr(created="2026-11-05T10:02:00Z")]):
            api.open_pulls.return_value = candidates
            self.assertIsNone(resolve_signal(api, signal(), REPO["full_name"]))

    def test_other_workflow_or_incomplete_signal_never_looks_up_pr(self):
        api = Mock()
        for field, value in (("path", ".github/workflows/other.yml"), ("conclusion", "cancelled"),
                             ("event", "workflow_dispatch"), ("head_repository", None)):
            event = copy.deepcopy(signal())
            event["workflow_run"][field] = value
            self.assertIsNone(resolve_signal(api, event, REPO["full_name"]))
        api.open_pulls.assert_not_called()
        api.pr.assert_not_called()


class ChecksTests(unittest.TestCase):
    def setUp(self):
        self.api = Mock()
        self.api.checks.return_value = []
        self.api.create_check.return_value = {"id": 33, "head_sha": HEAD}
        self.result = {"repository": REPO["full_name"], "number": 9, "head_sha": HEAD,
                       "base_sha": BASE, "status": "clean", "coverage": "complete_scoped", "findings": []}

    def payload(self):
        self.assertEqual(publish_check(self.api, self.result, 511), (33, "created"))
        repository, sha, data = self.api.create_check.call_args.args
        self.assertEqual((repository, sha), (REPO["full_name"], HEAD))
        self.assertEqual(data["details_url"], "https://github.com/example/base/actions/runs/511")
        self.assertEqual(data["external_id"], "architecture-review/pr/9")
        return data

    def test_clean_scope_is_success(self):
        self.assertEqual(self.payload()["conclusion"], "success")

    def test_no_scope_skips_and_partial_is_neutral(self):
        self.result.update(status="unsupported", coverage="partial", diagnostic_codes=["peer_dependencies"])
        data = self.payload()
        self.assertEqual(data["conclusion"], "neutral")
        self.assertIn("проверка неполна", data["output"]["title"].lower())
        self.result.update(coverage="unsupported", diagnostic_codes=["no_supported_manifests"])
        self.assertEqual(self.payload()["conclusion"], "skipped")

    def test_rerun_edits_only_own_check_for_this_pr(self):
        owned = {"name": f"{NAME} · PR № 9", "id": 77, "head_sha": HEAD,
                 "app": {"slug": "github-actions"}, "external_id": "architecture-review/pr/9"}
        self.api.checks.return_value = [owned, {**owned, "id": 78, "external_id": "architecture-review/pr/10"},
                                       {**owned, "id": 79, "app": {"slug": "someone-else"}}]
        self.api.edit_check.return_value = {"id": 77, "head_sha": HEAD}
        self.assertEqual(publish_check(self.api, self.result, 511), (77, "updated"))
        self.assertEqual(self.api.edit_check.call_args.args[1], 77)
        self.api.create_check.assert_not_called()

    def test_failure_is_visible_and_mismatched_response_is_unconfirmed(self):
        self.result["status"] = "error"
        self.assertEqual(self.payload()["conclusion"], "failure")
        self.api.create_check.return_value["head_sha"] = BASE
        with self.assertRaisesRegex(GitHubError, "unconfirmed_check_delivery"):
            publish_check(self.api, self.result, 511)


if __name__ == "__main__":
    unittest.main()
