"""Regression cases for cross-run comparison and diagnostics."""

import unittest

from github_bot.evidence import history_marker, lifecycle, normalize_gaps, read_history


PATH = "plugins/x/package.json"
KEY = ("package-app-core", PATH, "@backstage/core-app-api")
FINDING = dict(zip(("rule_id", "file", "dependency"), KEY))
FIRST = {"status": "violation", "base_sha": "a" * 40, "effective_base_sha": "b" * 40,
         "head_sha": "c" * 40, "policy_hash": "d" * 64, "findings": [FINDING],
         "history_evidence": {"complete_paths": [PATH], "head_edges": [KEY], "revertible": [KEY]}}
NEXT = {**FIRST, "head_sha": "e" * 40, "status": "clean", "findings": [],
        "history_evidence": {"complete_paths": [PATH], "head_edges": []}}


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        self.prior = read_history(history_marker(FIRST))

    def test_same_head_rechecks_resolved(self):
        result = {**NEXT, "lifecycle": lifecycle(NEXT, self.prior)}
        self.assertEqual(result["lifecycle"]["resolved"], [KEY])
        self.assertEqual(lifecycle(NEXT, read_history(history_marker(result)))["resolved"], [KEY])

    def test_incomplete_path_retained_until_analyzed(self):
        retry = {**NEXT, "history_evidence": {"complete_paths": [], "head_edges": []}}
        delta = lifecycle(retry, self.prior, {PATH})
        self.assertEqual(delta["unverified"], [KEY])
        remembered = read_history(history_marker({**retry, "lifecycle": delta}))
        self.assertEqual(lifecycle(NEXT, remembered)["resolved"], [KEY])

    def test_absent_from_diff_requires_complete_diff_and_revertible_finding(self):
        result = {**NEXT, "status": "unsupported", "diagnostic_codes": ["no_supported_manifests"],
                  "history_evidence": {}}
        self.assertEqual(lifecycle(result, self.prior, set())["resolved"], [KEY])
        self.assertEqual(lifecycle(result, self.prior)["resolved"], [])
        self.assertEqual(lifecycle(result, {**self.prior, "revertible": []}, set())["resolved"], [])

    def test_uncertain_and_mismatched_runs(self):
        self.assertEqual(lifecycle({**NEXT, "base_sha": "f" * 40}, self.prior), {})
        self.assertEqual(lifecycle({**NEXT, "policy_hash": "f" * 64}, self.prior), {})
        self.assertEqual(lifecycle({**NEXT, "status": "superseded"}, self.prior), {})
        self.assertEqual(lifecycle({**NEXT, "history_evidence": {}}, self.prior, set())["resolved"], [])
        self.assertEqual(lifecycle({**NEXT, "history_evidence": {"head_edges": [KEY],
                                   "complete_paths": [PATH]}}, self.prior, {PATH})["resolved"], [])

    def test_unknown_marker_and_bounded_gaps(self):
        marker = history_marker(FIRST)
        self.assertIsNone(read_history(marker + "\n" + marker))
        self.assertIsNone(read_history("<!-- architecture-review-history:v1 abcd -->"))
        items = [{"file": PATH, "phase": "head", "reason": "peerDependencies"},
                 {"file": "../package.json", "phase": "head", "reason": "npm-alias"}]
        self.assertEqual(normalize_gaps(items, {"peerDependencies": "peer_dependencies"}),
                         [{"file": PATH, "phase": "head", "section": "peerDependencies", "code": "peer_dependencies"}])
