import fs from 'node:fs';
import { isMap, parseDocument } from 'yaml';

const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const packageName = /^(?:@[a-z0-9][a-z0-9._-]*\/)?[a-z0-9][a-z0-9._-]*$/;
const frontendRoles = new Set(['frontend', 'frontend-plugin', 'web-library', 'frontend-plugin-module']);
const otherRoles = new Set([
  'backend', 'backend-plugin', 'backend-plugin-module', 'node-library', 'common-library', 'cli', 'web-library',
]);
const knownRoles = new Set([...frontendRoles, ...otherRoles]);
const modeled = ['dependencies', 'devDependencies'];

function validName(value) {
  return typeof value === 'string' && value.length <= 214 && packageName.test(value);
}

function manifestedTable(doc, section, filename) {
  if (!Object.hasOwn(doc, section)) return {};
  const value = doc[section];
  if (!value || Array.isArray(value) || typeof value !== 'object') {
    throw new Error(`Expected ${section} object in ${filename}`);
  }
  for (const [name, version] of Object.entries(value)) {
    if (!validName(name) || typeof version !== 'string') {
      throw new Error(`Invalid ${section} declaration in ${filename}`);
    }
  }
  return value;
}

function tagsFor(manifest, plugin, useNamingConvention) {
  let role = manifest.backstage?.role;
  if (!role && useNamingConvention && plugin) {
    const name = manifest.name.split('/').at(-1);
    role = name.endsWith('-backend') ? 'backend-plugin'
      : name.endsWith('-node') ? 'node-library'
      : name.endsWith('-common') ? 'common-library'
      : name.endsWith('-react') ? 'web-library' : 'frontend-plugin';
  }
  const tags = ['package'];
  if (plugin) tags.push('plugin');
  if (frontendRoles.has(role)) tags.push('frontend');
  return { tags, role: knownRoles.has(role) ? role : 'unknown' };
}

function edgeLocation(text, filename, map, name) {
  const declaration = map.items.find(item => item.key?.value === name);
  if (!declaration?.key.range) throw new Error(`Missing dependency token in ${filename}`);
  const [start, end] = declaration.key.range;
  const location = offset => ({
    line: text.slice(0, offset).split('\n').length,
    col: offset - text.lastIndexOf('\n', offset - 1),
    offset,
  });
  return { file: filename, start: location(start), end: location(end) };
}

function aliasTarget(version) {
  return version.startsWith('npm:') ? version.slice(4).split(/(?<=.)@/)[0] : null;
}

function relevantPackages(dependencies, targets) {
  const names = new Set();
  for (const [name, version] of Object.entries(dependencies)) {
    const alias = aliasTarget(version);
    if (targets.has(name) || targets.has(alias)) {
      names.add(name);
      if (alias) names.add(alias);
    }
  }
  return [...names];
}

function detectGaps(sources, filename, manifest, tags, role, optional) {
  const applicable = { 'development-only': sources['development-only'] || [] };
  if (tags.includes('plugin')) {
    applicable['package-app-core'] = sources['package-app-core'] || [];
  }
  if (tags.includes('frontend') || role === 'unknown') {
    applicable['frontend-backend'] = sources['frontend-backend'] || [];
  }
  const relevant = new Set(Object.values(applicable).flat());
  // npm uses the optional spec in preference to a same-name runtime entry.
  const runtime = Object.fromEntries(Object.entries(manifestedTable(manifest, 'dependencies', filename))
    .filter(([name]) => !Object.hasOwn(optional, name)));
  const effective = { ...runtime, ...optional };
  const gaps = [];
  const possibleEdges = [];

  function record(reason, packages, declarations, policies = applicable) {
    if (!packages.length) return;
    gaps.push({ file: filename, reason, packages });
    const actualTargets = new Set(Object.entries(declarations)
      .map(([name, version]) => aliasTarget(version) ?? name));
    for (const [ruleId, targets] of Object.entries(policies)) {
      for (const dependency of targets) {
        if (actualTargets.has(dependency)) {
          possibleEdges.push({ file: filename, source: manifest.name, dependency, ruleId });
        }
      }
    }
  }

  for (const [section, dependencies] of Object.entries({
    optionalDependencies: optional,
    peerDependencies: manifestedTable(manifest, 'peerDependencies', filename),
  })) {
    record(section, relevantPackages(dependencies, relevant), dependencies);
  }
  for (const section of ['bundleDependencies', 'bundledDependencies']) {
    if (!Object.hasOwn(manifest, section)) continue;
    const listing = manifest[section];
    if (typeof listing !== 'boolean' && !(Array.isArray(listing) && listing.every(validName))) {
      throw new Error(`Invalid ${section} in ${filename}`);
    }
    if (listing === true) {
      record(section, relevantPackages(effective, relevant), effective);
    }
    if (Array.isArray(listing)) {
      const packages = listing.filter(name => relevant.has(name));
      const declarations = Object.fromEntries(packages
        .filter(name => Object.hasOwn(effective, name)).map(name => [name, effective[name]]));
      record(section, packages, declarations);
    }
  }
  if (role === 'unknown') {
    const packages = (sources['frontend-backend'] || []).filter(name => Object.hasOwn(runtime, name));
    record('source-role-unknown', packages, runtime, { 'frontend-backend': packages });
  }
  for (const [name, version] of Object.entries(runtime)) {
    if (version.startsWith('npm:')) {
      const alias = aliasTarget(version);
      if (relevant.has(alias) || relevant.has(name)) record('npm-alias', [name, alias], { [name]: version });
    }
  }
  return { gaps, possibleEdges };
}

function project(files) {
  const elements = Object.create(null);
  const gaps = [];
  const possibleEdges = [];
  for (const { filename, text, plugin } of files) {
    const data = JSON.parse(text);
    if (!data || !validName(data.name)) throw new Error(`Missing or invalid package name in ${filename}`);
    if (Object.hasOwn(elements, data.name)) throw new Error(`Duplicate package identity in ${filename}`);
    const syntax = parseDocument(text, { schema: 'json', uniqueKeys: true, strict: true });
    if (syntax.errors.length) throw new Error(`Invalid or duplicate manifest keys in ${filename}`);
    const { tags, role } = tagsFor(data, plugin, input.useNamingConvention);
    const optional = manifestedTable(data, 'optionalDependencies', filename);
    const relations = [];
    const omitted = detectGaps(input.rules, filename, data, tags, role, optional);
    gaps.push(...omitted.gaps);
    possibleEdges.push(...omitted.possibleEdges);
    for (const section of modeled) {
      const entries = Object.keys(manifestedTable(data, section, filename)).sort();
      if (!entries.length) continue;
      const map = syntax.get(section, true);
      if (!isMap(map)) throw new Error(`Expected ${section} map in ${filename}`);
      for (const name of entries) {
        if (section === 'dependencies' && (Object.hasOwn(optional, name) || data[section][name].startsWith('npm:'))) continue;
        relations.push({
          to: name,
          description: section,
          technology: 'npm',
          tags: [section === 'dependencies' ? 'runtime' : 'development'],
          sourceLocation: edgeLocation(text, filename, map, name),
        });
      }
    }
    elements[data.name] = {
      name: data.name, label: data.name, kind: 'Component', external: false, tags, relations,
    };
  }
  for (const { relations } of Object.values(elements)) {
    for (const { to } of relations) {
      if (!Object.hasOwn(elements, to)) {
        elements[to] = { name: to, label: to, kind: 'Component', external: false,
          tags: ['package', 'reference-only'], relations: [] };
      }
    }
  }
  return {
    model: { elements, boundaries: {}, rootBoundaryNames: [], workspace: {
      name: 'Changed package manifests', description: 'Direct declared dependencies in an immutable PR snapshot.',
    } }, gaps, possibleEdges,
  };
}

try {
  process.stdout.write(JSON.stringify({ base: project(input.base), head: project(input.head) }));
} catch (error) {
  // The Python caller never treats projector errors as a negative review.
  process.stdout.write(JSON.stringify({ error: error.message }));
  process.exitCode = 2;
}
