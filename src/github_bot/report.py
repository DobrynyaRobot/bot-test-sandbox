"""Render the same scoped review for the PR and the GitHub Actions summary."""

import hashlib
import re
from urllib.parse import unquote, urlsplit

from .github import repo_segment, url_path

ORIGIN = "https://github.com"
MARKER = "<!-- architecture-review-benchmark:v1 -->"
STATUS_LABELS = {
    "clean": "новых нарушений не найдено",
    "violation": "найдены новые нарушения",
    "unsupported": "проверка неполна",
    "error": "проверка завершилась с ошибкой",
    "superseded": "результат устарел",
}
DIAGNOSTICS = {
    "base_policy_unavailable": "В базовой ревизии PR ещё нет настроек проверки. Обновите PR от основной ветки.",
    "base_rule_anchors_missing": "В базовой ревизии не найден нужный раздел документа с правилом.",
    "baseline_edge_unresolved": "Не удалось сравнить одну из зависимостей с базовой ревизией.",
    "blob_size_limit": "Один из нужных файлов превышает лимит размера.",
    "bundled_dependencies": "Список bundled dependencies вне области проверки.",
    "engine_missing": "Перезапустите установку AACT в этом workflow.",
    "github_rate_limited": "GitHub временно ограничил запросы. Повторите запуск позже.",
    "immutable_blob_missing": "GitHub не предоставил нужный файл на проверяемой ревизии.",
    "manifest_count_limit": "В PR слишком много манифестов для одной проверки.",
    "manifest_projection_failed": "AACT не смог прочитать манифесты. Проверьте синтаксис изменённых package.json и объявления зависимостей.",
    "manifest_size_limit": "Суммарный размер манифестов превышает лимит.",
    "npm_alias": "Проверка пропустила зависимость, объявленную через npm alias.",
    "optional_dependencies": "Некоторые optionalDependencies остались за пределами проверки.",
    "peer_dependencies": "Некоторые peerDependencies остались за пределами проверки.",
    "policy_repository_unsupported": "Правила для этого репозитория ещё не настроены.",
    "pr_file_limit": "Число изменённых файлов превышает лимит проверки.",
    "pr_patch_limit": "Объём изменений превышает лимит проверки.",
    "reviewer_timeout": "AACT не успел завершить проверку.",
    "source_role_unknown": "У одного из изменённых пакетов не удалось определить роль.",
}
PACKAGE = re.compile(r"(?:@[a-z0-9][a-z0-9._-]*/)?[a-z0-9][a-z0-9._-]*")
# Count only right-hand lines actually included in a hunk. A missing patch,
# omitted hunk, or line outside the displayed hunk uses an immutable file link.
HUNK = re.compile(r"@@ -[0-9]+(?:,[0-9]+)? \+([0-9]{1,8})(?:,[0-9]+)? @@(?: .*)?")


def line_in_patch(patch: str, line: int | None) -> bool:
    if type(line) is not int or not 0 < line <= 1_000_000:
        return False
    right = None
    for row in patch.split("\n"):
        if header := HUNK.fullmatch(row):
            right = int(header[1])
        elif right is not None and row.startswith((" ", "+")):
            if right == line:
                return True
            right += 1
    return False


def _text(value: str, limit: int) -> str:
    return "".join(char for char in value[:limit] if char.isprintable())


def _code(value: str, limit: int) -> str:
    # Inline code preserves exact characters in normal npm names without pings.
    # Keep an untrusted value from closing the code span or its optional link.
    value = _text(value, limit).translate(str.maketrans("`[]<>", "′［］‹›"))
    return f"`{value}`"


def _rule_source(source: str, repository: str, base_sha: str | None) -> str | None:
    if not base_sha:
        return None
    prefix = f"/{repository}/blob/{base_sha}/"
    try:
        if "://" in source:
            parsed = urlsplit(source)
            if (parsed.scheme, parsed.netloc) != ("https", "github.com") or parsed.query:
                return None
            if not parsed.path.startswith(prefix):
                return None
            path = unquote(parsed.path[len(prefix):], errors="strict")
            anchor = parsed.fragment
        else:
            path, _, anchor = source.partition("#")
        if anchor and not re.fullmatch(r"L[1-9][0-9]{0,7}(?:-L[1-9][0-9]{0,7})?", anchor):
            return None
        safe_path = url_path(path)
    except ValueError:
        return None
    return f"{ORIGIN}{prefix}{safe_path}" + (f"#{anchor}" if anchor else "")


def _finding_markdown(finding: dict, result: dict, pr_url: str) -> str:
    path, line = finding["file"], finding.get("line")
    line = line if type(line) is int and 0 < line <= 1_000_000 else None
    label = _code(f"{path}:{line}" if line else path, 570)
    location = label
    links = []
    repository = result["repository"]
    if head_sha := result.get("head_sha"):
        head_repo = result.get("head_repository", repository)
        try:
            encoded_path = url_path(path)
        except ValueError:
            pass
        else:
            blob = f"{ORIGIN}/{head_repo}/blob/{head_sha}/{encoded_path}" + (f"#L{line}" if line else "")
            if line and finding.get("in_diff"):
                digest = hashlib.sha256(path.encode("utf-8")).hexdigest()
                location = f"[{label}]({pr_url}/files#diff-{digest}R{line})"
                links.append(f"[Строка в проверенном коммите]({blob})")
            else:
                location = f"[{label}]({blob})"

    source = _rule_source(finding["source"], repository, result.get("effective_base_sha") or result.get("base_sha"))
    dependency = finding.get("dependency")
    app_core = finding.get("rule_id") == "package-app-core" and isinstance(dependency, str) and PACKAGE.fullmatch(dependency)
    if source:
        rule_link = "Правило App Core" if app_core else "Документ с правилом"
        links.insert(0, f"[{rule_link}]({source})")

    if app_core:
        problem = f"Зависимость {_code(dependency, 214)} добавлена в `dependencies` плагина. App Core предназначен для приложения."
        fix = "Используйте публичный API плагинов. Если зависимость нужна только для тестов или разработки, перенесите её в `devDependencies`."
    else:
        problem = f"{_code(finding['rule'], 250)} · {_code(finding['problem'], 750)}"
        fix = _code(finding["recommendation"], 700)

    lines = [f"#### {location}", "", problem, "", f"**Как исправить.** {fix}"]
    if links:
        lines.extend(("", " · ".join(links)))
    return "\n".join(lines) + "\n\n"


def _status_message(status: str, diagnostics: list[str]) -> str:
    if status == "clean":
        return "В проверенных `dependencies` не появилось новых нарушений."
    if status == "superseded":
        return "PR изменился или был закрыт во время запуска. Откройте последнюю проверку для актуального коммита."
    if status == "error":
        return "Откройте этап с ошибкой в Actions. Повторите проверку после устранения причины."
    if status == "unsupported" and "no_supported_manifests" not in diagnostics:
        return "Часть проверки недоступна. Сверьте затронутые зависимости с правилом вручную."
    if "no_supported_manifests" in diagnostics:
        return "В PR нет изменённых `package.json`, которые входят в область проверки."
    return ""


def _display_status(result: dict) -> str:
    status = result["status"]
    if status == "error" and result.get("delivery_status") in ("failed", "unknown"):
        return result.get("review_status", status)
    return status


def _status_label(result: dict, status: str) -> str:
    if status == "violation":
        return f"новых нарушений — {len(result['findings'])}"
    if status == "unsupported" and "no_supported_manifests" in result.get("diagnostic_codes", []):
        return "правило неприменимо к изменениям"
    return STATUS_LABELS[status]


def workflow_annotation(result: dict) -> str:
    status = _display_status(result)
    message = _status_label(result, status).capitalize() + "."
    if result.get("delivery_status") in ("failed", "unknown"):
        message += " GitHub не подтвердил обновление комментария."
    if result.get("coverage") == "partial" and status in {"clean", "violation"}:
        message += " Покрытие неполное."
    if result["status"] == "error":
        level = "error"
    elif status == "violation" or (status == "unsupported" and "no_supported_manifests" not in result.get("diagnostic_codes", [])):
        level = "warning"
    else:
        level = "notice"
    return f"::{level} title=Архитектурная проверка::{message}"


def review_markdown(result: dict, *, run_id: int | None = None, comment: bool = False) -> str:
    status = _display_status(result)
    delivery = result.get("delivery_status")
    diagnostics = result.get("diagnostic_codes", [])
    label = _status_label(result, status)
    findings = result.get("findings", []) if status == "violation" else []
    prefix = [MARKER] if comment else []
    prefix.extend([f"### Архитектурная проверка · {label}", ""])

    message = _status_message(status, diagnostics)
    if message:
        prefix.extend((message, ""))
    if delivery == "failed":
        prefix.extend(("GitHub не принял обновление комментария. Результат проверки доступен ниже.", ""))
    elif delivery == "unknown":
        prefix.extend(("GitHub не подтвердил обновление комментария. Результат проверки доступен ниже.", ""))
    if result.get("coverage") == "partial" and status in {"violation", "clean"}:
        prefix.extend(("**Проверка охватила только часть зависимостей.**", ""))
    if status in {"violation", "unsupported", "error"}:
        messages = [DIAGNOSTICS[key] for key in dict.fromkeys([*diagnostics, result.get("reason")]) if key in DIAGNOSTICS]
        if messages:
            prefix.extend([*(f"- {message}" for message in messages), ""])

    links = []
    pr_url = ""
    if repository := result.get("repository"):
        root = ORIGIN + repo_segment(repository).removeprefix("/repos")
        if number := result.get("number"):
            pr_url = f"{root}/pull/{number}"
            links.append(f"[Изменения PR № {number}]({pr_url}/files)")
            if not comment and (comment_id := result.get("comment_id")):
                links.append(f"[Комментарий бота]({pr_url}#issuecomment-{comment_id})")
        if comment and run_id:
            links.append(f"[Отчёт проверки]({root}/actions/runs/{run_id})")
        if head_sha := result.get("head_sha"):
            head_repo = result.get("head_repository", repository)
            links.append(f"[Коммит `{head_sha[:8]}`]({ORIGIN}/{head_repo}/commit/{head_sha})")
    if links:
        prefix.extend((" · ".join(links), ""))

    output = "\n".join(prefix) + "\n"
    limit, byte_limit = (20, 50_000) if comment else (200, 300_000)
    shown = 0
    if findings:
        findings = sorted(findings, key=lambda f: (f["file"], str(f.get("line")), f["rule"], f["problem"]))
        for finding in findings[:limit]:
            block = _finding_markdown(finding, result, pr_url)
            if len(output.encode("utf-8")) + len(block.encode("utf-8")) > byte_limit - 1024:
                break
            output += block
            shown += 1
        if shown < len(findings):
            output += f"Показано **{shown} из {len(findings)}** замечаний.\n\n"
    if result.get("scope_code") == "direct_package_declarations" and status in {"clean", "violation"}:
        output += "Область проверки · прямые зависимости в изменённых `package.json`.\n"
    if not comment and (status == "error" or delivery in ("failed", "unknown")) and result.get("reason"):
        output += f"\nКод для диагностики · {_code(result['reason'], 80)}.\n"
    return output
