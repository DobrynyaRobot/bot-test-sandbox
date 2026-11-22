# Архитектурное ревью pull request

Здесь можно посмотреть, как бот находит нарушение архитектурного правила в обычном PR. Пример взят из Backstage. Плагину запрещено добавлять `@backstage/core-app-api` и `@backstage/frontend-app-api` в `dependencies`. Эти пакеты нужны самому приложению. Для тестов плагин может использовать их через `devDependencies`. [Текст правила](docs/architecture-policy.md)

## Посмотреть на примере

| Что поменяли | Что сделал бот |
|---|---|
| В [PR № 2](https://github.com/DobrynyaRobot/bot-test-sandbox/pull/2/files) добавили `@backstage/core-app-api` в `dependencies` плагина. | Оставил [комментарий](https://github.com/DobrynyaRobot/bot-test-sandbox/pull/2#issuecomment-7077172576) с файлом, строкой и ссылкой на правило. При следующем коммите обновил тот же комментарий. |
| В [PR № 3](https://github.com/DobrynyaRobot/bot-test-sandbox/pull/3/files) тот же пакет добавили в `devDependencies`. | [Проверка](https://github.com/DobrynyaRobot/bot-test-sandbox/actions/runs/45007725977) вернула `clean`. Комментариев нет. |

Чтобы повторить, создайте ветку от `main`, добавьте один из этих пакетов в раздел `dependencies` файла [plugins/demo/package.json](plugins/demo/package.json) и откройте PR в `main`. Ожидайте одно замечание. В GitHub Actions откройте **Architecture review** и шаг **Review immutable PR data**. Поле `status` показывает результат, а `findings_count` — число находок. Зелёная галочка означает, что проверка выполнилась. PR с нарушением тоже может получить зелёную галочку.

## Что сравниваем в проекте

В исследовании сравниваем три способа ревью одного и того же PR. Агент читает изменение и архитектурные решения. AACT проверяет запрограммированные правила. В третьем варианте агент получает ещё и результат AACT. Сравниваем замечания с разметкой по исходникам и документам. Считаем найденные нарушения, пропуски и ложные замечания. Повторы показывают, насколько устойчив ответ.

В этой песочнице работает вариант с AACT 3.0.1. Он сравнивает прямые зависимости с базовой ревизией PR. Проверка охватывает изменённые манифесты по шаблонам `plugins/**/package.json` и `packages/**/package.json`. Результат `clean` относится только к этому правилу. [Настройка](.github/architecture-review.json) · [Workflow](.github/workflows/architecture-review.yml) · [Статья об архитектурных тестах](https://habr.com/ru/articles/800205/)
