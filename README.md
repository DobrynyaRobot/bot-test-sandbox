# Архитектурное ревью pull request

Бот проверяет, не добавил ли плагин App Core в рабочие зависимости. Плагин может использовать этот пакет для тестов через `devDependencies`. [Правило](docs/architecture-policy.md#frontend-app-core)

## Куда нажать

| Пример | Где смотреть |
|---|---|
| Плагин добавил `@backstage/core-app-api` в `dependencies`. | [Открыть замечание к PR № 2](https://github.com/DobrynyaRobot/bot-test-sandbox/pull/2#issuecomment-7077172576). Нажмите на имя файла, чтобы перейти к строке. Под ней есть исправление, ссылка на правило и ссылка на точный коммит. |
| Пакет добавили только в `devDependencies`. | [Посмотреть изменения в PR № 3](https://github.com/DobrynyaRobot/bot-test-sandbox/pull/3/files). [Открыть чистый результат в Actions](https://github.com/DobrynyaRobot/bot-test-sandbox/actions/runs/45015668745). |

Для любого PR откройте [Actions → Architecture review](https://github.com/DobrynyaRobot/bot-test-sandbox/actions/workflows/architecture-review.yml) и выберите запуск по номеру PR. Короткий итог виден на странице **Summary** в блоке **Annotations**. Зелёная галочка показывает, что проверка выполнилась. О нарушениях говорит итог проверки.

На новых коммитах бот обновляет один комментарий. Если вы исправили нарушение, в нём появится новый результат. В PR, где замечаний ещё не было, чистый результат можно посмотреть в Actions.

Чтобы попробовать на своём PR, добавьте `@backstage/core-app-api` в `dependencies` файла [plugins/demo/package.json](plugins/demo/package.json) и откройте PR в `main`. Бот укажет добавленную строку.

## Что сравниваем в проекте

В исследовании сравниваем три способа ревью одного и того же PR. Агент читает изменение и архитектурные решения. AACT проверяет запрограммированные правила. В третьем варианте агент получает ещё и результат AACT. Сравниваем замечания с разметкой по исходникам и документам. Считаем найденные нарушения, пропуски и ложные замечания. Повторы показывают, насколько устойчив ответ.

В этой песочнице работает AACT 3.0.1. Он сравнивает прямые зависимости в изменённых `plugins/**/package.json` и `packages/**/package.json` с базовой ревизией PR. [Настройка](.github/architecture-review.json) · [Workflow](.github/workflows/architecture-review.yml) · [Статья об архитектурных тестах](https://habr.com/ru/articles/800205/)
