# Architecture policy for the demonstration repository

## Frontend App Core

The packages `@backstage/core-app-api` and `@backstage/frontend-app-api` are only used by the `app` package in production dependencies.

A frontend plugin takes shared services from its public plugin API. Move composition code requiring App Core to the application package. This policy covers the direct `dependencies` edges of manifests in `plugins/` and `packages/`.

## Если замечание спорное

Ответьте в обсуждении PR и попросите сопровождающего репозитория проверить замечание. Укажите пункт правила, путь к строке, проверенный коммит и почему зависимость здесь нужна. Если есть согласованное решение, приложите ссылку на него и его область действия.
