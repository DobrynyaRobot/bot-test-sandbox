"""Exercise manual routing, provider responses and hybrid fallback."""

from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from aact_reviewer import result
from github_bot.__main__ import main
from github_bot.agent import MODEL, select_reviewer
from github_bot.checks import publish_check
from github_bot.github import GitHubError
from github_bot.routing import manual_inputs, resolve_manual

ROOT = Path(__file__).resolve().parents[1]
POLICY = ROOT / ".github/architecture-review.json"
REPO = {"id": 19, "full_name": "DobrynyaRobot/bot-test-sandbox"}
BASE, HEAD = "a" * 40, "b" * 40
PATH = "plugins/demo/package.json"
DOC = "docs/architecture-policy.md"
DEPENDENCY = "@backstage/core-app-api"
RULE = "package-app-core"


def manifest(runtime=None, dev=None, optional=None):
    return json.dumps({"name": "example-plugin", "dependencies": runtime or {},
                       "devDependencies": dev or {}, "optionalDependencies": optional or {}}, indent=2)


def candidate(text, *, last=False):
    matches = [(number, row) for number, row in enumerate(text.split("\n"), 1) if f'"{DEPENDENCY}"' in row]
    line, evidence = matches[-1 if last else 0]
    return {"rule_id": RULE, "file": PATH, "dependency": DEPENDENCY, "line": line, "evidence": evidence,
            "problem": "Плагин добавил App Core в рабочие зависимости.", "recommendation": "Используйте публичный API."}


def reply(*rows):
    answer = {"status": "violation" if rows else "clean", "findings": list(rows)}
    return {"model": MODEL, "status": "completed", "output": [
        {"type": "message", "content": [{"type": "output_text", "text": json.dumps(answer)}]}
    ], "usage": {"input_tokens": 250, "output_tokens": 40}}


def pr():
    return {"number": 9, "state": "open", "changed_files": 1,
            "base": {"ref": "main", "sha": BASE, "repo": REPO},
            "head": {"ref": "pr-branch", "sha": HEAD, "repo": REPO}}


class AgentModesTests(unittest.TestCase):
    def setUp(self):
        self.head = manifest({DEPENDENCY: "^1.0.0"})
        self.base = manifest()
        self.files = [{"filename": PATH, "status": "modified"}]
        self.provider = Mock(return_value=reply(candidate(self.head)))

    def blob(self, revision, path):
        if (revision, path) == (BASE, DOC):
            return (ROOT / DOC).read_bytes()
        if path == PATH:
            return {BASE: self.base, HEAD: self.head}[revision]
        if revision == BASE and path == ".github/architecture-review.json":
            return POLICY.read_bytes()
        return None

    def run_mode(self, mode="agent", key="fixture-secret", **options):
        selected = select_reviewer(mode, key, self.provider, **options)
        return selected(repo=REPO["full_name"], base_sha=BASE, head_sha=HEAD,
                        files=self.files, policy_path=POLICY, fetch_blob=self.blob)

    def formal_finding(self):
        return {**candidate(self.head), "rule": "App Core", "source": f"https://github.com/{REPO['full_name']}/blob/{BASE}/{DOC}#L3"}

    def test_aact_routes_to_original_engine_without_network_provider(self):
        self.assertIsNone(select_reviewer("aact", "fixture-secret", self.provider))
        self.provider.assert_not_called()

    def test_agent_uses_base_doc_and_comparison_without_formal_results(self):
        with patch("github_bot.agent.review_aact") as engine:
            reviewed = self.run_mode()
        engine.assert_not_called()
        self.assertEqual(reviewed["status"], "violation")
        self.assertEqual(reviewed["findings"][0]["origin"], "agent")
        key, payload = self.provider.call_args.args
        inputs = json.loads(payload["input"])
        self.assertEqual(key, "fixture-secret")
        self.assertNotIn("fixture-secret", json.dumps(payload))
        self.assertEqual(inputs["revisions"], [BASE, HEAD])
        self.assertEqual(inputs["manifests"], {"base": {PATH: self.base}, "head": {PATH: self.head}})
        self.assertEqual(inputs["documents"][DOC], (ROOT / DOC).read_text())
        self.assertIn("+    ", inputs["diff"][PATH])
        self.assertNotIn("aact", inputs)
        self.assertFalse(payload["store"])
        self.assertEqual(payload["tools"], [])
        self.assertEqual(reviewed["agent_usage"], {"input_tokens": 250, "output_tokens": 40})

    def test_agent_rejects_evidence_from_dev_section_and_preexisting_runtime(self):
        self.head = manifest({DEPENDENCY: "^2"}, {DEPENDENCY: "^2"})
        self.provider.return_value = reply(candidate(self.head, last=True))
        self.assertEqual(self.run_mode()["diagnostics"], ["agent_unverified_evidence"])
        self.provider.return_value = reply(candidate(self.head))
        self.base = manifest({DEPENDENCY: "^1"})
        self.assertEqual(self.run_mode()["diagnostics"], ["agent_unverified_evidence"])

    def test_agent_checks_exact_line_for_minified_manifest(self):
        self.head = json.dumps(json.loads(self.head), separators=(",", ":"))
        self.provider.return_value = reply(candidate(self.head))
        reviewed = self.run_mode()
        self.assertEqual(reviewed["status"], "violation")
        self.assertEqual(reviewed["findings"][0]["line"], 1)

    def test_agent_rejects_optional_override_and_wrong_evidence(self):
        self.head = manifest({DEPENDENCY: "^1"}, optional={DEPENDENCY: "^2"})
        self.provider.return_value = reply(candidate(self.head))
        self.assertEqual(self.run_mode()["diagnostics"], ["agent_unverified_evidence"])
        self.head = manifest({DEPENDENCY: "^1"})
        self.provider.return_value = reply({**candidate(self.head), "evidence": "Wrong line"})
        self.assertEqual(self.run_mode()["diagnostics"], ["agent_unverified_evidence"])

    def test_invalid_manifest_and_missing_key_never_call_provider(self):
        self.head = '{"name":"plugin","dependencies":["invalid"]}'
        self.assertEqual(self.run_mode()["diagnostics"], ["agent_manifest_invalid"])
        self.assertEqual(self.run_mode(key="")["diagnostics"], ["agent_key_missing"])
        self.provider.assert_not_called()

    def test_hybrid_deduplicates_and_keeps_aact_when_agent_disagrees(self):
        formal = result("violation", [self.formal_finding()], coverage="complete_scoped")
        with patch("github_bot.agent.review_aact", return_value=formal):
            agreement = self.run_mode("hybrid")
            self.provider.return_value = reply()
            disagreement = self.run_mode("hybrid")
        self.assertEqual([row["origin"] for row in agreement["findings"]], ["both"])
        self.assertEqual([row["origin"] for row in disagreement["findings"]], ["aact"])
        payload = json.loads(self.provider.call_args.args[1]["input"])
        self.assertEqual(payload["aact"]["findings"][0]["dependency"], DEPENDENCY)
        self.assertEqual(disagreement["arm_status"], {"aact": "violation", "agent": "clean"})

    def test_hybrid_retains_formal_finding_on_refusal_and_missing_key(self):
        self.provider.return_value["output"][0]["content"] = [{"type": "refusal", "refusal": "ignored"}]
        formal = result("violation", [self.formal_finding()], coverage="complete_scoped")
        with patch("github_bot.agent.review_aact", return_value=formal):
            refused = self.run_mode("hybrid")
            missing = self.run_mode("hybrid", key="")
        self.assertEqual((refused["status"], refused["coverage"]), ("violation", "partial"))
        self.assertEqual(refused["diagnostics"], ["agent_refusal"])
        self.assertEqual([row["origin"] for row in missing["findings"]], ["aact"])
        self.assertEqual(missing["diagnostics"], ["agent_key_missing"])
        self.provider.assert_called_once()

    def test_hybrid_runs_agent_if_aact_setup_failed(self):
        with patch("github_bot.agent.review_aact") as engine:
            partial = self.run_mode("hybrid", formal_setup_failed=True)
        engine.assert_not_called()
        self.assertEqual((partial["status"], partial["coverage"]), ("violation", "partial"))
        self.assertEqual(partial["arm_status"], {"aact": "error", "agent": "violation"})

    def test_hybrid_preserves_findings_if_agent_input_fetch_fails(self):
        def unavailable(*_):
            raise GitHubError("github_rate_limited", 403)

        self.blob = unavailable
        formal = result("violation", [self.formal_finding()], coverage="complete_scoped")
        with patch("github_bot.agent.review_aact", return_value=formal):
            partial = self.run_mode("hybrid")
        self.assertEqual((partial["status"], partial["coverage"]), ("violation", "partial"))
        self.assertEqual(partial["diagnostics"], ["agent_input_unavailable"])


class ManualRoutingTests(unittest.TestCase):
    def test_default_and_explicit_mode_inputs(self):
        for mode in ("agent", "hybrid", "aact"):
            self.assertEqual(manual_inputs({"inputs": {"pr_number": "9", "mode": mode}}, "refs/heads/main"), (9, mode))
        self.assertEqual(manual_inputs({"inputs": {"pr_number": "9"}}, "refs/heads/main"), (9, "aact"))
        for number, mode, ref in (("9", "agent", "refs/heads/topic"),
                                  ("9", "other", "refs/heads/main"),
                                  ("09", "agent", "refs/heads/main")):
            with self.assertRaises(ValueError):
                manual_inputs({"inputs": {"pr_number": number, "mode": mode}}, ref)

    def test_manual_review_requires_an_open_main_pr_in_the_event_repo(self):
        api = Mock()
        event = {"repository": REPO}
        api.pr.return_value = pr()
        self.assertEqual(resolve_manual(api, event, REPO["full_name"], 9)["pull_request"]["head"]["sha"], HEAD)
        api.pr.return_value["state"] = "closed"
        self.assertIsNone(resolve_manual(api, event, REPO["full_name"], 9))
        api.pr.return_value = pr()
        api.pr.return_value["base"]["ref"] = "topic"
        self.assertIsNone(resolve_manual(api, event, REPO["full_name"], 9))
        with self.assertRaises(ValueError):
            resolve_manual(api, {"repository": {**REPO, "id": 20}}, REPO["full_name"], 9)
        api.pr.side_effect = GitHubError("github_not_found", 404)
        self.assertIsNone(resolve_manual(api, event, REPO["full_name"], 9))

    def test_checks_are_separate_for_each_mode_and_automatic_review(self):
        api = Mock()
        api.checks.return_value = [{"name": "Архитектура · PR № 9", "id": 22, "head_sha": HEAD,
                                    "app": {"slug": "github-actions"}, "external_id": "architecture-review/pr/9"}]
        api.create_check.return_value = {"id": 33, "head_sha": HEAD}
        review = {"repository": REPO["full_name"], "number": 9, "head_sha": HEAD,
                  "status": "clean", "coverage": "complete_scoped", "findings": [], "manual": True}
        for mode in ("agent", "hybrid", "aact"):
            self.assertEqual(publish_check(api, {**review, "mode": mode}, 42), (33, "created"))
            _, sha, data = api.create_check.call_args.args
            self.assertEqual(sha, HEAD)
            self.assertEqual(data["external_id"], f"architecture-review/pr/9/manual/{mode}")
            self.assertEqual(data["conclusion"], "neutral" if mode == "agent" else "success")
        api.edit_check.assert_not_called()

    def test_manual_cli_reports_on_current_head_without_comment_or_secret_leak(self):
        api = Mock()
        api.pr.return_value = pr()
        api.files.return_value = [{"filename": PATH, "status": "modified"}]
        api.merge_base.return_value = BASE
        api.checks.return_value = []
        api.create_check.return_value = {"id": 33, "head_sha": HEAD}
        snapshots = {BASE: manifest(), HEAD: manifest({DEPENDENCY: "^1"})}
        api.fetch_blob.side_effect = lambda repo, path, sha: (
            snapshots[sha] if path == PATH else (ROOT / path).read_text()
        )
        provider = Mock(return_value=reply(candidate(snapshots[HEAD])))
        output = io.StringIO()
        with TemporaryDirectory() as directory:
            event = Path(directory) / "event.json"
            event.write_text(json.dumps({"repository": REPO, "inputs": {"pr_number": "9", "mode": "agent"}}))
            summary = Path(directory) / "summary.md"
            environment = {"GITHUB_TOKEN": "fixture-github-key", "OPENAI_API_KEY": "fixture-openai-key",
                           "GITHUB_RUN_ID": "42", "GITHUB_REF": "refs/heads/main", "GITHUB_WORKFLOW_SHA": BASE,
                           "GITHUB_ACTIONS": "true", "GITHUB_STEP_SUMMARY": str(summary)}
            with (patch.dict(os.environ, environment, clear=True),
                  patch("github_bot.__main__.GitHubClient", return_value=api),
                  patch("github_bot.__main__._verify_checkout") as verify,
                  patch("github_bot.__main__.select_reviewer",
                        side_effect=lambda mode, key, **options: select_reviewer(mode, key, provider, **options)),
                  redirect_stdout(output)):
                verify.side_effect = lambda *args: self.assertTrue(
                    {"GITHUB_TOKEN", "OPENAI_API_KEY"}.isdisjoint(os.environ))
                code = main(["--event", str(event), "--repository", REPO["full_name"],
                             "--policy", str(POLICY), "--verify-checkout", "--workflow-dispatch"])
            contents = summary.read_text()
        self.assertEqual(code, 0)
        receipt = json.loads(output.getvalue().splitlines()[-1])
        self.assertEqual((receipt["mode"], receipt["findings_count"], receipt["check_id"]), ("agent", 1, 33))
        self.assertNotIn("fixture-", output.getvalue() + contents)
        self.assertIn("Замечание агента", contents)
        self.assertIn(f"/blob/{HEAD}/{PATH}#L4", contents)
        self.assertEqual(api.create_check.call_args.args[1], HEAD)
        api.comments.assert_not_called()
        api.edit_comment.assert_not_called()
        api.create_comment.assert_not_called()


if __name__ == "__main__":
    unittest.main()
