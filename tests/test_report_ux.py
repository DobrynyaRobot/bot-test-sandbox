import unittest

from github_bot.report import check_summary, review_markdown, status_label
from test_report import BASE, HEAD, ROOT, sample


class ReportUXTests(unittest.TestCase):
    def test_numeric_order_and_preview_limits(self):
        result = sample()
        template = result["findings"][0]
        result["findings"] = [dict(template, line=line) for line in range(211, 0, -1)]
        body = review_markdown(result, comment=True, run_id=3)
        self.assertLess(body.index(":2`"), body.index(":10`"))
        self.assertLess(body.index(":3`"), body.index("<details>"))
        self.assertGreater(body.index(":4`"), body.index("<details>"))
        self.assertIn("Другие замечания · 17", body)
        self.assertIn("20 из 211", body)
        self.assertLess(body.index("</details>"), body.index("Отчёт проверки"))
        self.assertIn("200 из 211", review_markdown(result))
        self.assertIn("3 из 211", check_summary(result))

    def test_partial_files_use_correct_revisions(self):
        result = sample("unsupported", coverage="partial", head_repository="other/fork", coverage_gaps=[
            {"file": "plugins/a/package.json", "phase": "base", "code": "peer_dependencies", "section": "peerDependencies"},
            {"file": "plugins/b/package.json", "phase": "head", "code": "npm_alias"},
            {"file": "../bad", "phase": "comparison", "code": "baseline_edge_unresolved"},
        ])
        body = review_markdown(result)
        self.assertIn(f"{ROOT}/blob/{BASE}/plugins/a/package.json", body)
        self.assertIn(f"https://github.com/other/fork/blob/{HEAD}/plugins/b/package.json", body)
        self.assertNotIn("/../bad", body)
        self.assertIn("peerDependencies", body)
        self.assertIn("только часть", body)

    def test_confirmed_and_unverified_are_separate(self):
        result = sample("clean", coverage="partial", confirmations=[
            {"file": "plugins/a/package.json", "dependency": "some-package", "rule_id": "package-app-core"},
        ], unverified_previous=2)
        self.assertIn("частичный охват", status_label(result))
        body = review_markdown(result, comment=True)
        self.assertIn("Исправление подтверждено · 1", body)
        self.assertIn("без подтверждения исправления · 2", body)
        self.assertIn("`some-package`", body)

    def test_single_finding_immediately_actionable(self):
        body = review_markdown(sample(), run_id=1, comment=True)
        self.assertNotIn("<details>", body)
        self.assertLess(body.index("Как исправить"), body.index("Изменения PR"))
        for term in ("публичный API", "пакет `app`", "`devDependencies`", "ошибочным?", "проверенный коммит"):
            self.assertIn(term, body)
