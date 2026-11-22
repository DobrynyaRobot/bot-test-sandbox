"""Bounded GitHub REST transport for file data and one advisory comment."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
import time
from http.client import HTTPException
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


class GitHubError(Exception):
    """An API operation failed. The printable code contains no response body."""

    def __init__(self, code: str, status: int | None = None):
        self.code = code
        self.status = status
        super().__init__(code)


class InputLimit(Exception):
    """The available data exceeds a supported bound."""


def repo_segment(repository: str) -> str:
    if not isinstance(repository, str) or not re.fullmatch(
        r"[a-zA-Z0-9_.-]{1,100}/[a-zA-Z0-9_.-]{1,100}", repository
    ) or any(part in (".", "..") for part in repository.split("/")):
        raise ValueError("invalid_repository")
    return f"/repos/{repository}"


def url_path(path: str) -> str:
    if not isinstance(path, str) or len(path) > 1024 or "\\" in path or any(
        part in ("", ".", "..") for part in path.split("/")
    ) or any(ord(char) < 32 or ord(char) == 127 for char in path):
        raise ValueError("invalid_path")
    return quote(path, safe="/")


def revision_sha(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{40}", value):
        raise ValueError("invalid_revision")
    return value


def positive_id(value: int) -> int:
    if type(value) is not int or value <= 0 or value >= 10**18:
        raise ValueError("invalid_number")
    return value


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class GitHubClient:
    def __init__(
        self,
        token: str,
        base_url: str = "https://api.github.com",
        timeout: float = 8,
        total_timeout: float = 180,
        max_response_bytes: int = 8_000_000,
        max_total_bytes: int = 32_000_000,
        max_blob_bytes: int = 262_144,
    ):
        if not token or any(ord(ch) < 32 or ord(ch) > 126 for ch in token):
            raise ValueError("missing_or_invalid_github_token")
        parsed = urlsplit(base_url)
        local = parsed.hostname in ("localhost", "127.0.0.1", "::1")
        if (
            not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or (parsed.scheme != "https" and not (local and parsed.scheme == "http"))
        ):
            raise ValueError("invalid_api_origin")
        self._origin = (parsed.scheme, parsed.netloc)
        self._prefix = parsed.path.rstrip("/")
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._timeout = timeout
        self._deadline = time.monotonic() + total_timeout
        self._max_response_bytes = max_response_bytes
        self._max_total_bytes = max_total_bytes
        self._max_blob_bytes = max_blob_bytes
        self._received = 0
        self._rate_blocked = False
        self._commit_trees: dict[tuple[str, str], str | None] = {}
        self._tree_entries: dict[tuple[str, str], dict[str, dict]] = {}
        self._opener = build_opener(_NoRedirect())

    def _request(self, method: str, path: str, body: dict | None = None):
        if not path.startswith("/") or path.startswith("//") or "#" in path:
            raise ValueError("invalid_api_path")
        values = json.dumps(body).encode("utf-8") if body is not None else None
        call = Request(
            self._base_url + path,
            data=values,
            method=method,
            headers={
                "Authorization": f"Bearer {self._token}",
                "Accept": "application/vnd.github+json",
                "Content-Type": "application/json",
                "User-Agent": "architecture-review-benchmark",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        # Retry only reads. A write may have reached GitHub even if its response was lost.
        attempts = 2 if method == "GET" else 1
        for attempt in range(attempts):
            if self._rate_blocked:
                raise GitHubError("github_rate_limited", 429)
            remaining = self._deadline - time.monotonic()
            if remaining <= 0:
                raise GitHubError("github_deadline")
            try:
                with self._opener.open(call, timeout=min(self._timeout, remaining)) as response:
                    remaining_bytes = self._max_total_bytes - self._received
                    limit = min(self._max_response_bytes, remaining_bytes)
                    raw = response.read(limit + 1)
                    if len(raw) > limit:
                        raise InputLimit("github_response_limit")
                    declared = response.headers.get("Content-Length")
                    if declared is not None and not getattr(response, "chunked", False):
                        if not re.fullmatch(r"[0-9]{1,12}", declared) or int(declared) != len(raw):
                            raise HTTPException("Incomplete API response body")
                    self._received += len(raw)
                    try:
                        return json.loads(raw) if raw else None, response.headers.get("Link", "")
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        raise GitHubError("github_invalid_json") from None
            except HTTPError as exc:
                status = exc.code
                headers = exc.headers
                raw_delay = headers.get("Retry-After", "")
                primary = headers.get("X-RateLimit-Remaining") == "0"
                rate_limited = status == 429 or (status == 403 and (primary or bool(raw_delay)))
                if status == 403 and not rate_limited:
                    # Some secondary rate-limit responses omit the corresponding headers.
                    try:
                        hint = exc.read(4096) or b""
                    except (OSError, ValueError, AttributeError, HTTPException):
                        hint = b""
                    self._received += len(hint)
                    rate_limited = b"rate limit" in hint.lower()
                exc.close()
                delay = self._retry_delay(headers, rate_limited, primary)
                retry = rate_limited or status in (502, 503, 504)
                remaining = self._deadline - time.monotonic()
                if retry and attempt + 1 < attempts and delay < remaining - 0.2:
                    time.sleep(delay)
                    continue
                if rate_limited:
                    self._rate_blocked = True
                    raise GitHubError("github_rate_limited", status) from None
                raise GitHubError(f"github_http_{status}", status) from None
            except (URLError, TimeoutError, OSError, HTTPException):
                if attempt + 1 < attempts and remaining > 0.2:
                    time.sleep(0.2)
                    continue
                raise GitHubError("github_transport_or_timeout") from None

    @staticmethod
    def _retry_delay(headers, rate_limited: bool, primary: bool) -> float:
        delays = []
        retry_after = headers.get("Retry-After", "")
        if retry_after.isdecimal() and len(retry_after) <= 8:
            delays.append(float(retry_after))
        reset = headers.get("X-RateLimit-Reset", "")
        if primary and reset.isdecimal() and len(reset) <= 12:
            delays.append(max(0, float(reset) - time.time()))
        return max(delays) if delays else 60.0 if rate_limited else 0.2

    def _paginate(self, repository: str, resource: str, limit: int, repository_id: int | None) -> list[dict]:
        path = repo_segment(repository) + resource
        permitted_paths = {self._prefix + path}
        if repository_id is not None:
            permitted_paths.add(f"{self._prefix}/repositories/{positive_id(repository_id)}{resource}")
        values = []
        for number in range(1, 33):
            # Request only the original resource. A validated Link can select
            # the next page without redirecting the request to another URL.
            items, link = self._request("GET", f"{path}?per_page=100&page={number}")
            if not isinstance(items, list) or not all(isinstance(item, dict) for item in items):
                raise GitHubError("github_unexpected_array")
            values.extend(items)
            if len(values) > limit:
                raise InputLimit("github_item_limit")
            next_links = re.findall(r'<([^<>]+)>\s*;\s*rel="next"', link)
            if not next_links:
                return values
            if len(next_links) != 1:
                raise GitHubError("github_invalid_pagination")
            try:
                next_url = urlsplit(next_links[0])
                query = parse_qs(next_url.query, strict_parsing=True, keep_blank_values=True, max_num_fields=2)
            except ValueError:
                raise GitHubError("github_invalid_pagination") from None
            if (
                (next_url.scheme, next_url.netloc) != self._origin
                or next_url.path not in permitted_paths
                or next_url.fragment
                or query.keys() - {"page", "per_page"}
                or query.get("page") != [str(number + 1)]
                or query.get("per_page", ["100"]) != ["100"]
            ):
                raise GitHubError("github_invalid_pagination")
        raise InputLimit("github_page_limit")

    def pr(self, repository: str, number: int) -> dict:
        value, _ = self._request("GET", f"{repo_segment(repository)}/pulls/{positive_id(number)}")
        if not isinstance(value, dict):
            raise GitHubError("github_unexpected_pr")
        return value

    def files(self, repository: str, number: int, limit: int, repository_id: int | None = None) -> list[dict]:
        return self._paginate(repository, f"/pulls/{positive_id(number)}/files", limit, repository_id)

    def merge_base(
        self, repository: str, target: str, head: str, fork_repository: str | None = None
    ) -> str:
        endpoint = f"{repo_segment(repository)}/compare/{revision_sha(target)}..."
        head = revision_sha(head)
        try:
            result, _ = self._request("GET", endpoint + head + "?per_page=1")
        except GitHubError as exc:
            if exc.status != 404 or not fork_repository or fork_repository.casefold() == repository.casefold():
                raise
            repo_segment(fork_repository)
            owner = fork_repository.split("/", 1)[0]
            result, _ = self._request("GET", endpoint + owner + ":" + head + "?per_page=1")
        try:
            if result["base_commit"]["sha"] != target:
                raise ValueError("github_compare_target_mismatch")
            return revision_sha(result["merge_base_commit"]["sha"])
        except (KeyError, TypeError, ValueError):
            raise GitHubError("github_invalid_comparison") from None

    def comments(self, repository: str, number: int, repository_id: int | None = None) -> list[dict]:
        return self._paginate(repository, f"/issues/{positive_id(number)}/comments", 3000, repository_id)

    def create_comment(self, repository: str, number: int, body: str) -> int | None:
        value, _ = self._request("POST", f"{repo_segment(repository)}/issues/{positive_id(number)}/comments", {"body": body})
        try:
            return positive_id(value.get("id")) if isinstance(value, dict) else None
        except ValueError:
            # A successful write without a usable ID still produced a comment.
            return None

    def edit_comment(self, repository: str, comment_id: int, body: str):
        self._request("PATCH", f"{repo_segment(repository)}/issues/comments/{positive_id(comment_id)}", {"body": body})

    def _commit_tree(self, repository: str, commit: str) -> str | None:
        key = (repository.casefold(), commit)
        if key not in self._commit_trees:
            try:
                value, _ = self._request("GET", f"{repo_segment(repository)}/git/commits/{commit}")
            except GitHubError as exc:
                if exc.status != 404:
                    raise
                self._commit_trees[key] = None
            else:
                try:
                    if value["sha"] != commit:
                        raise ValueError
                    self._commit_trees[key] = revision_sha(value["tree"]["sha"])
                except (KeyError, TypeError, ValueError):
                    raise GitHubError("github_invalid_commit_tree") from None
        return self._commit_trees[key]

    def _entries(self, repository: str, tree: str) -> dict[str, dict]:
        key = (repository.casefold(), tree)
        if key not in self._tree_entries:
            value, _ = self._request("GET", f"{repo_segment(repository)}/git/trees/{tree}")
            if not isinstance(value, dict) or value.get("sha") != tree or not isinstance(value.get("tree"), list):
                raise GitHubError("github_invalid_tree")
            if value.get("truncated") is not False:
                raise InputLimit("github_tree_coverage_limit")
            entries = {}
            for entry in value["tree"]:
                name = entry.get("path") if isinstance(entry, dict) else None
                if not isinstance(name, str) or not name or "/" in name or name in entries:
                    raise GitHubError("github_invalid_tree_entry")
                entries[name] = entry
            self._tree_entries[key] = entries
        return self._tree_entries[key]

    def _regular_blob(self, repository: str, path: str, commit: str) -> str | None:
        url_path(path)  # Validate the exact Git path before traversing its segments.
        tree = self._commit_tree(repository, revision_sha(commit))
        if tree is None:
            return None
        segments = path.split("/")
        for index, part in enumerate(segments):
            entry = self._entries(repository, tree).get(part)
            if entry is None:
                return None
            mode = entry.get("mode")
            type_ = entry.get("type")
            leaf = index == len(segments) - 1
            if leaf:
                if type_ != "blob" or mode not in ("100644", "100755"):
                    raise InputLimit("github_unsupported_blob_mode")
                size = entry.get("size")
                if type(size) is int and size > self._max_blob_bytes:
                    raise InputLimit("github_blob_limit")
            elif type_ != "tree" or mode != "040000":
                raise InputLimit("github_unsupported_tree_mode")
            try:
                tree = revision_sha(entry.get("sha"))
            except ValueError:
                raise GitHubError("github_invalid_tree_entry") from None
        return tree

    def fetch_blob(self, repository: str, path: str, commit: str) -> bytes | None:
        blob = self._regular_blob(repository, path, commit)
        if blob is None:
            return None
        value, _ = self._request("GET", f"{repo_segment(repository)}/git/blobs/{blob}")
        if (
            not isinstance(value, dict)
            or value.get("sha") != blob
            or value.get("encoding") != "base64"
            or not isinstance(value.get("content"), str)
        ):
            raise GitHubError("github_invalid_blob")
        size = value.get("size")
        if type(size) is not int or size < 0:
            raise GitHubError("github_invalid_blob_size")
        if size > self._max_blob_bytes:
            raise InputLimit("github_blob_limit")
        encoded = value["content"].replace("\n", "")
        if len(encoded) > 4 * ((self._max_blob_bytes + 2) // 3):
            raise InputLimit("github_blob_limit")
        try:
            raw = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error):
            raise GitHubError("github_invalid_blob") from None
        if len(raw) > self._max_blob_bytes:
            raise InputLimit("github_blob_limit")
        if size != len(raw):
            raise GitHubError("github_invalid_blob_size")
        git_bytes = f"blob {size}\0".encode() + raw
        if hashlib.sha1(git_bytes, usedforsecurity=False).hexdigest() != blob:
            raise GitHubError("github_blob_digest_mismatch")
        return raw
