import { defineRule } from 'aact';

// No case identifiers, expected labels, supplied verdicts, or curated violating
// source edges occur here. These are repository policies from historical docs.
function checkEdges(model, options, sourceApplies, ruleName, explanation) {
  const targets = new Set(options?.targets || []);
  const violations = [];
  for (const source of Object.values(model.elements)) {
    if (!sourceApplies(source)) continue;
    for (const edge of source.relations) {
      if (!edge.tags.includes('runtime') || !targets.has(edge.to)) continue;
      violations.push({ target: source.name, targetKind: 'element',
        message: `${ruleName}: ${source.name} -> ${edge.to}. ${explanation}`,
        sourceLocation: edge.sourceLocation });
    }
  }
  return violations;
}

export const packageAppCore = defineRule({
  name: 'package-app-core',
  description: 'Plugin packages must not have runtime dependencies on Frontend App Core.',
  rationale: 'Historical Backstage architecture-overview reserves Frontend App Core for app entry packages. The strict dependency graph applies to dependencies; development dependencies may cross the graph for testing.',
  check(model, options) {
    return checkEdges(model, options, source => source.tags.includes('plugin'), 'package-app-core', 'App Core belongs to application entry packages; test-only exceptions use devDependencies.');
  },
});

export const frontendBackend = defineRule({
  name: 'frontend-backend',
  description: 'Frontend packages must not depend on documented backend-only implementation packages.',
  rationale: 'Historical ADR011 distinguishes frontend x/x-react and backend x-backend/x-node packages, and explicitly classifies the legacy techdocs-common package as techdocs-node.',
  check(model, options) {
    return checkEdges(model, options, source => source.tags.includes('frontend'), 'frontend-backend', 'The target is a backend implementation according to the pinned ADR.');
  },
});

export const developmentOnly = defineRule({
  name: 'development-only',
  description: 'Packages documented as development tooling must not be runtime dependencies.',
  rationale: 'Historical architecture-overview says the Backstage CLI must always be a development dependency. Historical test-utils README prescribes yarn add -D.',
  check(model, options) {
    return checkEdges(model, options, () => true, 'development-only', 'The documented installation scope is devDependencies.');
  },
});
