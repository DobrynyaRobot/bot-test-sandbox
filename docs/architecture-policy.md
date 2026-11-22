# Architecture policy for the demonstration repository

## Frontend App Core

The packages `@backstage/core-app-api` and `@backstage/frontend-app-api` are only used by the `app` package in production dependencies.

A frontend plugin takes shared services from its public plugin API. Move composition code requiring App Core to the application package. This policy covers the direct `dependencies` edges of manifests in `plugins/` and `packages/`.
