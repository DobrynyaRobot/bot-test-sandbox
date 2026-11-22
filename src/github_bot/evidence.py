"""Bounded, structured evidence for rendering and comparing trusted bot runs."""

import base64
import binascii
import json
import re

from aact_reviewer import PACKAGE_NAME, RULES, SHA, safe_path

MAX_KEYS = 100
MAX_MARKER = 6500
HISTORY = re.compile(r"^<!-- architecture-review-history:v1 ([A-Za-z0-9+/=]{1,6500}) -->$", re.MULTILINE)
SECTIONS = {"optionalDependencies", "peerDependencies", "bundledDependencies", "bundleDependencies"}


def _manifest(value):
    try:
        path = safe_path(value)
        return path if path.split("/")[-1] == "package.json" else None
    except ValueError:
        return None


def normalize_gaps(diagnostics, reason_codes):
    rows = []
    if not isinstance(diagnostics, list):
        return rows
    for gap in diagnostics[:100]:
        if not isinstance(gap, dict) or not (path := _manifest(gap.get("file"))):
            continue
        phase, reason = gap.get("phase"), gap.get("reason")
        if phase not in ("base", "head", "comparison") or not isinstance(reason, str):
            continue
        row = {"file": path, "phase": phase, "code": reason_codes.get(reason, "unclassified")}
        if reason in SECTIONS:
            row["section"] = reason
        if row not in rows:
            rows.append(row)
    return rows


def _key(values):
    if not isinstance(values, (list, tuple)) or len(values) != 3:
        return None
    rule, path, dependency = values
    if (not isinstance(rule, str) or rule not in RULES or not _manifest(path)
            or not isinstance(dependency, str) or len(dependency) > 214
            or not PACKAGE_NAME.fullmatch(dependency)):
        return None
    return rule, path, dependency


def fingerprint(finding):
    if isinstance(finding, dict):
        return _key((finding.get("rule_id"), finding.get("file"), finding.get("dependency")))
    return None


def _keys(items):
    if not isinstance(items, list) or len(items) > 5000:
        return None
    keys = [_key(item) for item in items]
    return set(keys) if all(keys) else None


def _identity(result):
    return {key: result.get(key) for key in ("base_sha", "effective_base_sha", "head_sha", "policy_hash")}


def _valid_identity(state):
    return (all(isinstance(state.get(k), str) and SHA.fullmatch(state[k])
                for k in ("base_sha", "effective_base_sha", "head_sha"))
            and isinstance(state.get("policy_hash"), str)
            and re.fullmatch(r"[0-9a-f]{64}", state["policy_hash"]))


def read_history(body):
    if not isinstance(body, str) or len(body) > 65000:
        return None
    matches = HISTORY.findall(body)
    if len(matches) != 1:
        return None
    try:
        state = json.loads(base64.b64decode(matches[0], validate=True))
        if not isinstance(state, dict) or not _valid_identity(state):
            return None
        for field in ("active", "revertible", "resolved"):
            if not isinstance(state.get(field), list) or len(state[field]) > MAX_KEYS or _keys(state[field]) is None:
                return None
        if not _keys(state["revertible"]) <= (_keys(state["active"]) | _keys(state["resolved"])):
            return None
        return state
    except (ValueError, UnicodeError, binascii.Error, RecursionError):
        return None


def history_marker(result):
    """Serialize bounded snapshot state only; callers authenticate the comment author."""
    keys = [fingerprint(finding) for finding in result.get("findings", [])]
    if not all(keys) or not _valid_identity(result) or result.get("status") not in ("violation", "clean", "unsupported"):
        return ""
    delta = result.get("lifecycle") or {}
    active = set(keys) | (_keys(delta.get("unverified", [])) or set())
    resolved = _keys(delta.get("resolved", [])) or set()
    reversible = (_keys((result.get("history_evidence") or {}).get("revertible", [])) or set())
    reversible.update(_keys(delta.get("revertible", [])) or set())
    state = {**_identity(result), "active": sorted(active), "resolved": sorted(resolved),
             "revertible": sorted(reversible & (active | resolved))}
    if max(len(active), len(resolved), len(state["revertible"])) > MAX_KEYS:
        return ""
    encoded = base64.b64encode(json.dumps(state, ensure_ascii=False, separators=(",", ":")).encode()).decode()
    return f"<!-- architecture-review-history:v1 {encoded} -->" if len(encoded) <= MAX_MARKER else ""


def lifecycle(result, previous, changed_paths=None):
    """Compare snapshots on identical bases and policy. changed_paths must be the full PR diff."""
    if (not previous or not _valid_identity(result) or result.get("status") not in ("clean", "violation", "unsupported")
            or any(previous.get(k) != result.get(k) for k in ("base_sha", "effective_base_sha", "policy_hash"))):
        return {}
    current = {fingerprint(finding) for finding in result.get("findings", [])}
    if None in current:
        return {}
    active = _keys(previous["active"])
    reversible = _keys(previous["revertible"])
    if active is None or reversible is None:
        return {}
    pending = active - current
    if result["head_sha"] == previous["head_sha"]:
        pending.update((_keys(previous["resolved"]) or set()) - current)
    evidence = result.get("history_evidence") or {}
    edges = _keys(evidence.get("head_edges"))
    complete = set(evidence.get("complete_paths", []))
    removed = set(evidence.get("removed_paths", []))
    resolved = set()
    may_compare_diff = edges is not None or "no_supported_manifests" in result.get("diagnostic_codes", [])
    for key in pending:
        path = key[1]
        if edges is not None and (path in removed or (path in complete and key not in edges)):
            resolved.add(key)
        elif may_compare_diff and changed_paths is not None and key in reversible and path not in changed_paths:
            resolved.add(key)
    return {"new": sorted(current - active), "remaining": sorted(active & current),
            "resolved": sorted(resolved), "unverified": sorted(pending - resolved),
            "revertible": sorted(reversible & (pending | current))}
