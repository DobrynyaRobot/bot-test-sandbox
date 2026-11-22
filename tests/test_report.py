"""Run with PYTHONPATH=src python3 -B -m unittest discover -s tests."""

from hashlib import sha256
from pathlib import Path
import unittest
from unittest.mock import Mock

from github_bot import process_event
from github_bot.report import MARKER, line_in_patch, review_markdown


ROOT = "https://github.com/example/sandbox"
BASE = "a" * 40
HEAD = "b" * 40
PATH = "plugins/demo/package.json"
RULE = f"{ROOT}/blob/{BASE}/docs/architecture-policy.md#L3"
PATCH = "@@ -4,2 +4,3 @@\n context\n-old value\n+new value\n+other line\n@@ -11 +12 @@\n-old\n+new"


def sample(status="violation", **changes):
    finding = {
        "file": PATH, "line": 5, "rule": "App Core rule", "rule_id": "package-app-core",
        "problem": "App Core is installed in the plugin", "recommendation": "Use a public API",
        "dependency": "@backstage/core-app-api", "source": RULE, "in_diff": True,
    }
    return {"repository": "example/sandbox", "number": 42, "base_sha": BASE, "head_sha": HEAD,
            "status": status, "findings": [finding] if status == "violation" else [],
            "scope_code": "direct_package_declarations", **changes}


class ReportTests(unittest.TestCase):
    def test_line_numbers_for_actual_right_side_of_hunk(self):
        self.assertTrue(all(line_in_patch(PATCH, line) for line in (4, 5, 6, 12)))
        self.assertFalse(any(line_in_patch(PATCH, line) for line in (True, 0, None, 3, 7, 11, 13)))
        self.assertFalse(line_in_patch("+ mock line without hunk header", 1))
        self.assertFalse(line_in_patch("@@ -4 +8,20 @@\n+truncated hunk", 9))

    def test_violation_links_to_diff_immutable_file_and_evidence(self):
        comment = review_markdown(sample(), run_id=72, comment=True)
        digest = sha256(PATH.encode()).hexdigest()
        self.assertEqual(comment.splitlines()[0], MARKER)
        self.assertIn(f"[`{PATH}:5`]({ROOT}/pull/42/files#diff-{digest}R5)", comment)
        self.assertIn(f"[Правило App Core]({RULE})", comment)
        self.assertIn(f"[Строка в проверенном коммите]({ROOT}/blob/{HEAD}/{PATH}#L5)", comment)
        self.assertIn(f"[Отчёт проверки]({ROOT}/actions/runs/72)", comment)
        self.assertIn("`@backstage/core-app-api`", comment)
        self.assertIn("**Как исправить.**", comment)

    def test_missing_hunk_uses_pinned_fork_file_with_encoded_path(self):
        result = sample(head_repository="someone/fork")
        result["findings"][0].update(file="plugins/demo [copy]/package.json", in_diff=False)
        body = review_markdown(result, comment=True)
        self.assertIn(f"https://github.com/someone/fork/blob/{HEAD}/plugins/demo%20%5Bcopy%5D/package.json#L5", body)
        self.assertNotIn("#diff-", body)

    def test_summary_links_back_to_managed_comment_and_shows_more_findings(self):
        result = sample(comment_id=11)
        result["findings"] *= 22
        self.assertIn("Показано **20 из 22**", review_markdown(result, comment=True))
        summary = review_markdown(result)
        self.assertEqual(summary.count("**Как исправить.**"), 22)
        self.assertIn(f"[Комментарий бота]({ROOT}/pull/42#issuecomment-11)", summary)

    def test_clean_unsupported_and_superseded_are_distinct(self):
        clean = review_markdown(sample("clean"))
        skipped = review_markdown(sample("unsupported", diagnostic_codes=["no_supported_manifests"]))
        self.assertIn("новых нарушений не найдено", clean)
        self.assertIn("правило неприменимо", skipped)
        self.assertNotIn("не найдено", skipped)
        old = sample()
        old["status"] = "superseded"
        superseded = review_markdown(old)
        self.assertIn("результат устарел", superseded)
        self.assertNotIn(PATH, superseded)

    def test_partial_and_comment_delivery_failure_keep_review_result(self):
        result = sample(coverage="partial", diagnostic_codes=["peer_dependencies"])
        result.update(status="error", review_status="violation", delivery_status="failed", reason="github_http_403")
        summary = review_markdown(result)
        self.assertIn("новых нарушений — 1", summary)
        self.assertIn("GitHub не принял обновление комментария", summary)
        self.assertIn("peerDependencies", summary)
        self.assertIn("`github_http_403`", summary)
        self.assertIn(PATH, summary)

    def test_reviewer_text_cannot_supply_a_link(self):
        result = sample()
        result["findings"][0].update(
            source="https://external.example/rule", rule_id="custom",
            file="plugins/x]/package.json", in_diff=False,
            problem="`\n[link](https://external.example) <!-- html -->",
        )
        body = review_markdown(result, comment=True)
        self.assertNotIn("[link](", body)
        self.assertNotIn("external.example/rule", body)
        self.assertNotIn("<!-- html -->", body)
        self.assertIn("plugins/x%5D/package.json#L5", body)


class BasePolicyTests(unittest.TestCase):
    def setUp(self):
        self.repository = "example/sandbox"
        self.policy = ".github/architecture-review.json"
        self.pr = {"number": 42, "state": "open", "changed_files": 1,
                   "base": {"sha": BASE, "repo": {"full_name": self.repository}},
                   "head": {"sha": HEAD}}
        self.api = Mock()
        self.api.pr.return_value = self.pr
        self.api.files.return_value = [{"filename": PATH, "status": "modified"}]
        self.api.merge_base.return_value = BASE
        self.api.comments.return_value = []

    def test_latest_reviewer_uses_original_pr_base_policy(self):
        self.api.fetch_blob.return_value = b'"policy-on-original-base"'
        handled = []

        def reviewer(*, policy_path, base_sha, **kwargs):
            self.assertEqual(base_sha, BASE)
            self.assertEqual(policy_path.read_bytes(), b'"policy-on-original-base"')
            handled.append(policy_path)
            return {"status": "clean"}

        result = process_event(self.api, {"pull_request": self.pr}, self.repository, Path(self.policy),
                               reviewer=reviewer, base_policy_path=self.policy)
        self.assertEqual(result["status"], "clean")
        self.api.fetch_blob.assert_called_once_with(self.repository, self.policy, BASE)
        self.assertFalse(handled[0].exists())
        self.api.create_comment.assert_not_called()

    def test_base_without_settings_reports_unsupported(self):
        self.api.fetch_blob.return_value = None
        reviewer = Mock()
        result = process_event(self.api, {"pull_request": self.pr}, self.repository, Path(self.policy),
                               reviewer=reviewer, base_policy_path=self.policy)
        self.assertEqual(result["status"], "unsupported")
        self.assertIn("Обновите PR от основной ветки", review_markdown(result))
        reviewer.assert_not_called()
        self.api.create_comment.assert_not_called()


if __name__ == "__main__":
    unittest.main()
