"""AACT 3.0.1 reviewer for direct Backstage package-manifest boundaries."""

from fnmatch import fnmatchcase
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from urllib.parse import quote

ENGINE = Path(__file__).resolve().parent / 'engine'
CLI = ENGINE / 'node_modules' / 'aact' / 'dist' / 'cli' / 'index.mjs'
RULES = {
    'package-app-core': (
        'Plugins must not have runtime dependencies on Frontend App Core.',
        'Use an API permitted for plugins. Use devDependencies only for development-only usage.',
    ),
    'frontend-backend': (
        'Frontend packages must not use a documented backend-only implementation.',
        'Use the frontend or shared contract described by the architecture document.',
    ),
    'development-only': (
        'Documented development tooling must not be a runtime dependency.',
        'Use devDependencies for development-only usage or remove the runtime dependency.',
    ),
}
BOUNDARY = 'Direct dependency declarations in changed package.json files under configured repository paths.'
SHA = re.compile(r'[0-9a-f]{40}')
REPOSITORY = re.compile(r'[a-zA-Z0-9_.-]+/[a-zA-Z0-9_.-]+')
PACKAGE_NAME = re.compile(r'(?:@[a-z0-9][a-z0-9._-]*/)?[a-z0-9][a-z0-9._-]*')
MAX_FILES = 60
MAX_FILE_BYTES = 256_000
MAX_INPUT_BYTES = 2_000_000


class CoverageGap(ValueError):
    """Required evidence is absent or outside the configured formal model."""


def result(status, findings=None, **kwargs):
    return {'status': status, 'findings': findings or [], 'scope': BOUNDARY, **kwargs}


def safe_path(value):
    if not isinstance(value, str) or not value:
        raise ValueError('Expected repository-relative file path')
    if len(value) > 500 or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError('Invalid control character or length in file path')
    if '\\' in value or any(part in ('', '.', '..') for part in value.split('/')):
        raise ValueError('Invalid repository-relative file path')
    return value


def match_manifest_path(path, pattern):
    """Match repository-relative POSIX path segments, including a recursive **."""
    segments = path.split('/')
    positions = {0}
    for selector in pattern.split('/'):
        if selector == '**':
            positions.update(range(min(positions), len(segments) + 1))
        else:
            positions = {
                i + 1 for i in positions
                if i < len(segments) and fnmatchcase(segments[i], selector)
            }
        if not positions:
            return False
    return len(segments) in positions


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate JSON key')
        result[key] = value
    return result


def read_policy(path):
    raw = Path(path).read_bytes()
    if len(raw) > 65536:
        raise ValueError('Architecture review policy exceeds the configured limit')
    data = json.loads(raw, object_pairs_hook=unique_object)
    if not isinstance(data, dict) or data.get('schema_version') != 1:
        raise ValueError('Unsupported architecture review policy schema')
    repos = data.get('repositories')
    globs = data.get('manifest_globs')
    rules = data.get('rules')
    if not isinstance(repos, list) or not repos or any(not isinstance(x, str) or not REPOSITORY.fullmatch(x) for x in repos):
        raise ValueError('Expected a repository allowlist')
    if not isinstance(globs, list) or not globs:
        raise ValueError('Expected manifest_globs')
    plugin_globs = data.get('plugin_manifest_globs', ['plugins/**/package.json'])
    if not isinstance(plugin_globs, list):
        raise ValueError('Expected plugin_manifest_globs list')
    for pattern in globs + plugin_globs:
        safe_path(pattern)
        if not pattern.endswith('package.json'):
            raise ValueError('Only package.json paths are supported by this adapter')
    if not isinstance(rules, dict) or not rules or set(rules) - set(RULES):
        raise ValueError('Unknown or empty AACT policy rules')
    for entry in rules.values():
        if not isinstance(entry, dict):
            raise ValueError('Expected rule object')
        targets = entry.get('targets')
        anchors = entry.get('contains')
        if not isinstance(targets, list) or not targets or any(
            not isinstance(x, str) or len(x) > 214 or not PACKAGE_NAME.fullmatch(x) for x in targets
        ):
            raise ValueError('Expected non-empty npm package targets')
        if not isinstance(anchors, list) or not anchors or any(not isinstance(x, str) or not x.strip() for x in anchors):
            raise ValueError('Expected exact rule anchors in the architectural document')
        safe_path(entry.get('document'))
    fallback = data.get('historical_role_fallback', False)
    if not isinstance(fallback, bool):
        raise ValueError('Expected historical_role_fallback boolean')
    return data


def read_text(fetch_blob, revision, path):
    data = fetch_blob(revision, path)
    if data is None:
        raise CoverageGap(f'Required immutable blob is unavailable: {path}')
    if isinstance(data, str):
        data = data.encode('utf-8')
    if not isinstance(data, bytes):
        raise ValueError('fetch_blob must return bytes, str, or None')
    if len(data) > MAX_FILE_BYTES:
        raise CoverageGap(f'Immutable blob exceeds the reviewer size limit: {path}')
    return data.decode('utf-8-sig')


def pin_sources(policy, repo, base_sha, fetch_blob):
    evidence = {}
    docs = {}
    for name, entry in policy['rules'].items():
        path = entry['document']
        if path not in docs:
            docs[path] = read_text(fetch_blob, base_sha, path)
        text = docs[path]
        flattened = ' '.join(text.split())
        if not all(' '.join(anchor.split()) in flattened for anchor in entry['contains']):
            raise CoverageGap(f'Base architecture document does not contain the configured anchors for {name}: {path}')
        first_anchor = entry['contains'][0]
        pattern = r'\s+'.join(re.escape(word) for word in first_anchor.split())
        match = re.search(pattern, text)
        line = text.count('\n', 0, match.start()) + 1 if match else 1
        evidence[name] = f'https://github.com/{repo}/blob/{base_sha}/{quote(path)}#L{line}'
    return evidence


def select_paths(files, globs):
    phase_paths = {'base': set(), 'head': set()}
    renames = {}

    def add(side, value):
        name = safe_path(value)
        if name.split('/')[-1] == 'package.json' and any(match_manifest_path(name, g) for g in globs):
            phase_paths[side].add(name)

    for file in files:
        if isinstance(file, str):
            file = {'filename': file, 'status': 'modified'}
        if not isinstance(file, dict):
            raise ValueError('Expected GitHub file object')
        path = safe_path(file.get('filename'))
        status = file.get('status', 'modified')
        if status not in {'added', 'removed', 'renamed', 'modified', 'changed', 'copied', 'unchanged'}:
            raise ValueError('Unrecognized GitHub changed-file status')
        if status not in {'added', 'copied'}:
            previous = file.get('previous_filename') if status == 'renamed' else path
            add('base', previous)
            if status == 'renamed':
                renames[path] = previous
        if status != 'removed':
            add('head', path)
    count = sum(len(paths) for paths in phase_paths.values())
    if not count:
        raise CoverageGap('No changed package manifests are covered by the configured formal policy')
    if count > MAX_FILES:
        raise CoverageGap('Too many package snapshots for this reviewer')
    return phase_paths, renames


def project_inputs(policy, phase_paths, base_sha, head_sha, fetch_blob):
    payload = {
        'rules': {name: entry['targets'] for name, entry in policy['rules'].items()},
        'useNamingConvention': policy.get('historical_role_fallback', False),
        'base': [],
        'head': [],
    }
    size = 0
    plugin_globs = policy.get('plugin_manifest_globs', ['plugins/**/package.json'])
    for side, pinned in (('base', base_sha), ('head', head_sha)):
        for path in sorted(phase_paths[side]):
            text = read_text(fetch_blob, pinned, path)
            size += len(text.encode('utf-8'))
            if size > MAX_INPUT_BYTES:
                raise CoverageGap('Combined package data exceed the reviewer input limit')
            payload[side].append({'filename': path, 'text': text,
                                  'plugin': any(match_manifest_path(path, g) for g in plugin_globs)})
    return payload


def node_command(args, directory, input_payload=None):
    node = shutil.which('node')
    if not node or not CLI.is_file():
        raise RuntimeError('AACT engine is missing. Install from src/aact_reviewer/install_engine.py.')
    argv = [node, *map(str, args)]
    environment = {
        'PATH': str(Path(node).parent) + os.pathsep + os.defpath,
        'XDG_CACHE_HOME': str(directory / 'cache'),
        'XDG_CONFIG_HOME': str(directory / 'config'),
        'LANG': 'C.UTF-8',
        'TZ': 'UTC',
    }
    return subprocess.run(
        argv, cwd=directory, env=environment, input=input_payload, timeout=25, capture_output=True, check=False,
    )


def project_model(payload, directory):
    proc = node_command([ENGINE / 'project.mjs'], directory, json.dumps(payload).encode())
    try:
        envelope = json.loads(proc.stdout)
    except (UnicodeError, ValueError) as exc:
        raise RuntimeError('AACT model projection produced an invalid result') from exc
    if proc.returncode or not isinstance(envelope, dict):
        reason = envelope.get('error', 'unknown projection failure') if isinstance(envelope, dict) else 'unknown projection failure'
        raise RuntimeError(f'AACT model projection failed: {reason[:300]}')
    return envelope


def engine_check(model, rules, directory, side):
    model_path = directory / f'{side}.json'
    config_path = directory / f'{side}.config.mjs'
    model_path.write_text(json.dumps(model), encoding='utf-8')
    options = {name: False for name in (
        'acl', 'acyclic', 'apiGateway', 'crud', 'dbPerService', 'cohesion', 'stableDependencies', 'commonReuse',
    )}
    options.update({name: {'targets': rules[name]['targets']} if name in rules else False for name in RULES})
    config = {
        'source': {'type': 'model-json', 'path': str(model_path)},
        'rules': options,
    }
    config_path.write_text(
        'import { packageAppCore, frontendBackend, developmentOnly } from '
        + json.dumps((ENGINE / 'rules.mjs').as_uri())
        + ';\nexport default {...' + json.dumps(config)
        + ', customRules:[packageAppCore,frontendBackend,developmentOnly]};\n',
        encoding='utf-8',
    )
    proc = node_command([CLI, 'check', f'--config={config_path}', '--json'], directory)
    try:
        out = json.loads(proc.stdout)
        findings = out['data']['violations']
        if (out['meta']['aactVersion'] != '3.0.1' or out['exitCode'] != proc.returncode
                or proc.returncode not in (0, 1) or not isinstance(findings, list)
                or proc.returncode != int(bool(findings))):
            raise ValueError('unexpected AACT check envelope')
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError(f'AACT check failed for {side} (exit {proc.returncode})') from exc
    return out


def finding_edge(item, model):
    source = item['target']
    location = item['sourceLocation']
    matches = [
        edge['to'] for edge in model['elements'][source]['relations']
        if 'runtime' in edge['tags'] and edge['sourceLocation'] == location
    ]
    if len(matches) != 1:
        raise RuntimeError('AACT finding does not identify exactly one projected runtime edge')
    return {'source': source, 'dependency': matches[0], 'ruleId': item['ruleId'], 'file': location['file']}


def edge_index(edges):
    return {
        dimension: {
            (edge[dimension], edge['dependency'], edge['ruleId']): edge for edge in edges
        }
        for dimension in ('source', 'file')
    }


def previous_edge(edge, index, renames):
    suffix = (edge['dependency'], edge['ruleId'])
    base_path = renames.get(edge['file'])
    if base_path is not None:
        return index['file'].get((base_path, *suffix))
    return index['source'].get((edge['source'], *suffix))


def _review(*, repo, base_sha, head_sha, files, policy_path, fetch_blob):
    """Review pinned package manifests against explicit, documented base policies.

    fetch_blob is a trusted immutable-data reader. No repository code is executed.
    """
    policy_hash = None
    try:
        if not all(isinstance(x, str) and SHA.fullmatch(x) for x in (base_sha, head_sha)):
            raise ValueError('Expected immutable 40-character Git revision identifiers')
        if not isinstance(repo, str) or not REPOSITORY.fullmatch(repo):
            raise ValueError('Invalid repository identifier')
        policy = read_policy(policy_path)
        if repo.lower() not in {r.lower() for r in policy['repositories']}:
            raise CoverageGap('No formal policy is configured for this repository')
        policy_digest = hashlib.sha256(json.dumps(policy, sort_keys=True).encode())
        for artifact in ('project.mjs', 'rules.mjs', 'package-lock.json'):
            policy_digest.update(hashlib.sha256((ENGINE / artifact).read_bytes()).digest())
        policy_hash = policy_digest.hexdigest()
        phase_paths, renames = select_paths(files, policy['manifest_globs'])
        sources = pin_sources(policy, repo, base_sha, fetch_blob)
        inputs = project_inputs(policy, phase_paths, base_sha, head_sha, fetch_blob)
        with tempfile.TemporaryDirectory(prefix='architecture-aact-') as scratch:
            work = Path(scratch)
            phases = project_model(inputs, work)
            old = engine_check(phases['base']['model'], policy['rules'], work, 'base')
            new = engine_check(phases['head']['model'], policy['rules'], work, 'head')
        before = edge_index([finding_edge(item, phases['base']['model']) for item in old['data']['violations']])
        uncertain_before = edge_index(phases['base']['possibleEdges'])
        gaps = [{'phase': side, **item} for side in ('base', 'head') for item in phases[side]['gaps']]
        findings = []
        head_edges = []
        revertible = []
        base_gaps = {gap['file'] for gap in phases['base']['gaps']}
        head_gaps = {gap['file'] for gap in phases['head']['gaps']}
        for item in new['data']['violations']:
            edge = finding_edge(item, phases['head']['model'])
            key = [edge['ruleId'], edge['file'], edge['dependency']]
            head_edges.append(key)
            if previous_edge(edge, before, renames):
                continue
            if ambiguous := previous_edge(edge, uncertain_before, renames):
                gaps.append({
                    'phase': 'comparison', 'reason': 'baseline_edge_unresolved', 'file': edge['file'],
                    'base_file': ambiguous['file'], 'rule_id': edge['ruleId'],
                    'source_package': edge['source'], 'dependency': edge['dependency'],
                })
                continue
            same_path_key = (edge['file'], edge['dependency'], edge['ruleId'])
            if (edge['file'] not in renames and edge['file'] not in base_gaps
                    and same_path_key not in before['file']):
                revertible.append(key)
            rule_id = item['ruleId']
            name, recommendation = RULES[rule_id]
            location = item['sourceLocation']
            findings.append({
                'rule_id': rule_id, 'rule': name, 'file': location['file'],
                'line': location['start']['line'], 'problem': item['message'],
                'recommendation': recommendation, 'source': sources[rule_id], 'target': item['target'],
                'dependency': edge['dependency'],
            })
        return result(
            'violation' if findings else 'unsupported' if gaps else 'clean',
            findings, coverage='partial' if gaps else 'complete_scoped',
            diagnostics=gaps, engine_version='3.0.1',
            policy_hash=policy_hash,
            coverage_counts={
                'base_manifests': len(phase_paths['base']), 'head_manifests': len(phase_paths['head']),
                'head_runtime_edges': sum(
                    'runtime' in relation['tags'] for element in phases['head']['model']['elements'].values()
                    for relation in element['relations']
                ),
            },
            history_evidence={
                'complete_paths': sorted(phase_paths['head'] - head_gaps),
                'removed_paths': sorted(phase_paths['base'] - phase_paths['head'] - set(renames.values())),
                'head_edges': head_edges, 'revertible': revertible,
            } if len(head_edges) <= 5000 else {},
            base_violation_count=len(old['data']['violations']), head_violation_count=len(new['data']['violations']),
            base_exit_code=old['exitCode'], head_exit_code=new['exitCode'],
            duration_seconds=(old['meta']['durationMs'] + new['meta']['durationMs']) / 1000,
        )
    except CoverageGap as exc:
        return result('unsupported', coverage='unsupported', diagnostics=[str(exc)], policy_hash=policy_hash)
    except subprocess.TimeoutExpired:
        return result('error', coverage='unknown', diagnostics=['AACT processing timed out'])
    except (KeyError, OSError, ValueError, RuntimeError, TypeError) as exc:
        return result('error', coverage='unknown', diagnostics=[str(exc)[:360]])


def review(*, repo, base_sha, head_sha, files, policy_path, fetch_blob):
    reviewed = _review(repo=repo, base_sha=base_sha, head_sha=head_sha, files=files,
                       policy_path=policy_path, fetch_blob=fetch_blob)
    return {'repo': repo, 'base_sha': base_sha, 'head_sha': head_sha, **reviewed}


__all__ = ['review']
