"""Render the same scoped review for the PR and the GitHub Actions summary."""

import hashlib
import re
from urllib.parse import unquote, urlsplit

from .github import repo_segment, revision_sha, url_path

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
    "engine_setup_failed": "AACT не запустился. Попросите сопровождающего открыть этап установки в Actions и повторить проверку.",
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
            if pr_url and line and finding.get("in_diff"):
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
        fix = "Для доступа к общим сервисам используйте публичный API плагина. Код сборки приложения, которому нужен App Core, перенесите в пакет `app`. Если пакет нужен только для тестов или разработки, перенесите зависимость в `devDependencies`."
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


def status_label(result: dict) -> str:
    label = _status_label(result, _display_status(result))
    if result.get("coverage") == "partial" and _display_status(result) in {"clean", "violation"}:
        return label + " · частичный охват"
    return label


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


def _links(result: dict, run_id: int | None, comment: bool) -> tuple[str, list[str]]:
    links = []
    pr_url = ""
    if repository := result.get("repository"):
        root = ORIGIN + repo_segment(repository).removeprefix("/repos")
        if number := result.get("number"):
            pr_url = f"{root}/pull/{number}"
            links.append(f"[Изменения PR № {number}]({pr_url}/files)")
            if not comment and (comment_id := result.get("comment_id")):
                links.append(f"[Комментарий бота]({pr_url}#issuecomment-{comment_id})")
        if run_id:
            links.append(f"[Отчёт проверки]({root}/actions/runs/{run_id})")
        if head_sha := result.get("head_sha"):
            head_repo = result.get("head_repository", repository)
            links.append(f"[Коммит `{head_sha[:8]}`]({ORIGIN}/{head_repo}/commit/{head_sha})")
    return pr_url, links


def _gap_line(gap: dict, result: dict) -> str:
    path = gap["file"]
    phase = gap["phase"]
    label = _code(path, 570)
    repo = result.get("repository")
    sha = result.get("effective_base_sha") or result.get("base_sha")
    if phase in {"head", "comparison"}:
        repo = result.get("head_repository") or repo
        sha = result.get("head_sha")
    try:
        root = repo_segment(repo).removeprefix("/repos")
        href = f"{ORIGIN}{root}/blob/{revision_sha(sha)}/{url_path(path)}"
    except ValueError:
        pass
    else:
        if phase in {"base", "head", "comparison"}:
            label = f"[{label}]({href})"
    stage = {"base": "базовый коммит", "head": "коммит PR", "comparison": "сравнение с базой"}.get(phase, "анализ")
    section = " · " + _code(gap["section"], 100) if gap.get("section") else ""
    why = DIAGNOSTICS.get(gap["code"]) or _code(gap["code"], 80)
    return f"- {label} ({stage}){section} — {why}"


def _bounded_list(items, total: int, budget: int) -> list[str]:
    lines = []
    for item in items:
        size = len(item.encode("utf-8")) + 1
        if size > budget:
            break
        budget -= size
        lines.append(item)
    if len(lines) < total:
        lines.append(f"Показано **{len(lines)} из {total}**.")
    return lines


def _review(result: dict, run_id: int | None, comment: bool, limit: int, byte_limit: int) -> str:
    status = _display_status(result)
    diagnostics = result.get("diagnostic_codes", [])
    delivery = result.get("delivery_status")
    prefix = [MARKER] if comment else []
    prefix.append(f"### Архитектурная проверка · {status_label(result)}")
    message = _status_message(status, diagnostics)
    if message:
        prefix.append(message)
    if result.get("coverage") == "partial" and status in {"violation", "clean", "unsupported"}:
        prefix.append("**Проверка охватила только часть зависимостей.**")
    pr_url, links = _links(result, run_id, comment)
    footer = []
    findings = result.get("findings", []) if status == "violation" else []
    app_rule = next((f for f in findings if f.get("rule_id") == "package-app-core"), None)
    if app_rule:
        source = _rule_source(app_rule["source"], result["repository"], result.get("effective_base_sha") or result.get("base_sha"))
        if source:
            footer.append(f"**Считаете замечание ошибочным?** Попросите сопровождающего проверить его в обсуждении PR. Укажите [пункт правила]({source}), зависимость и проверенный коммит.")

    if status != "superseded":
        confirmations = result.get("confirmations", [])
        if confirmations:
            rows = (f"- {_code(item['file'], 570)} · {_code(item['dependency'], 214)} · {_code(item['rule_id'], 100)}" for item in confirmations)
            footer.append(f"**Исправление подтверждено · {len(confirmations)}**\n\n" +
                          "\n".join(_bounded_list(rows, len(confirmations), byte_limit // 8)))
        if pending := result.get("unverified_previous"):
            footer.append(f"**Прежние замечания без подтверждения исправления · {pending}.**")

    gaps = result.get("coverage_gaps", [])
    if gaps and status != "superseded":
        rows = (_gap_line(gap, result) for gap in gaps)
        footer.append("**Что осталось проверить вручную**\n\n" +
                      "\n".join(_bounded_list(rows, len(gaps), byte_limit // 8)))
    if status in {"clean", "violation", "unsupported", "error"}:
        gap_codes = {gap["code"] for gap in gaps}
        messages = [DIAGNOSTICS[key] for key in dict.fromkeys([*diagnostics, result.get("reason")])
                    if key in DIAGNOSTICS and key not in gap_codes]
        if messages:
            footer.append("\n".join(f"- {message}" for message in messages))
    if result.get("scope_code") == "direct_package_declarations" and status in {"clean", "violation", "unsupported"}:
        footer.append("Область проверки · прямые зависимости в изменённых `package.json` по настроенным путям.")
        counts = result.get("coverage_counts", {})
        base_count, head_count = counts.get("base_manifests"), counts.get("head_manifests")
        if type(base_count) is int and type(head_count) is int:
            footer.append(f"Прочитано манифестов · база — {base_count}, коммит PR — {head_count}.")
    if delivery in {"failed", "unknown"}:
        detail = "GitHub не принял обновление комментария" if delivery == "failed" else "GitHub не подтвердил обновление комментария"
        footer.append(f"**Доставка комментария.** {detail}.")
    if result.get("check_action") == "unconfirmed":
        footer.append("**Публикация в Checks.** GitHub не подтвердил обновление. Итог доступен в этом запуске.")
    if not comment and (status == "error" or delivery in {"failed", "unknown"}) and result.get("reason"):
        footer.append(f"Код для диагностики · {_code(result['reason'], 80)}.")
    if links:
        footer.append(" · ".join(links))

    sections = [*prefix]
    if findings:
        budget = byte_limit - len("\n\n".join(prefix + footer).encode("utf-8")) - 512
        blocks = []
        def order(item):
            line = item.get("line")
            return item["file"], line if type(line) is int else 1_000_001, item["rule"], item["problem"]
        for finding in sorted(findings, key=order)[:limit]:
            block = _finding_markdown(finding, result, pr_url).rstrip()
            size = len(block.encode("utf-8")) + 2
            if size > budget:
                break
            budget -= size
            blocks.append(block)
        sections.extend(blocks[:3] if comment else blocks)
        if comment and len(blocks) > 3:
            sections.append(f"<details>\n<summary>Другие замечания · {len(blocks) - 3}</summary>\n\n" +
                            "\n\n".join(blocks[3:]) + "\n\n</details>")
        if len(blocks) < len(findings):
            sections.append(f"Показано **{len(blocks)} из {len(findings)}** замечаний.")
    return "\n\n".join(sections + footer) + "\n"


def review_markdown(result: dict, *, run_id: int | None = None, comment: bool = False) -> str:
    limit, budget = (20, 50_000) if comment else (200, 300_000)
    return _review(result, run_id, comment, limit, budget)


def check_summary(result: dict, *, run_id: int | None = None) -> str:
    return _review(result, run_id, False, 3, 45_000)
