"""GitHub client: the `gh` CLI behind the `GhClient` seam (infrastructure).

Every call is one `gh` child with the network timeout; a failure is a typed `GhError`.
"""

from __future__ import annotations

import json
from typing import Any, Protocol

from .github_plan import GhComment, GhEvent, GhIssue, GhIssueRef, IssuePatch
from .subproc import NETWORK_S, run_with_timeout

ISSUES_PER_PAGE: int = 100


# ----------------------------------------------------------------- errors ---


class GhError(Exception):
    """Typed GitHub failure."""


class GhSpawnError(GhError):
    def __init__(self, program: str, source: Exception) -> None:
        super().__init__(f"cannot run '{program}': {source}")
        self.program = program
        self.source = source


class GhCommandError(GhError):
    def __init__(self, op: str, status: str, stderr: str) -> None:
        super().__init__(f"gh {op} exited unsuccessfully ({status}): {stderr}")
        self.op = op
        self.status = status
        self.stderr = stderr


class GhParseError(GhError):
    def __init__(self, op: str, reason: str) -> None:
        super().__init__(f"gh {op} produced unusable output: {reason}")
        self.op = op
        self.reason = reason


# ------------------------------------------------------------------- seam ---


class GhClient(Protocol):
    def list_issues(self, repo: str, label: str) -> list[GhIssue]: ...

    def get_issue(self, repo: str, number: int) -> GhIssue: ...

    def search_marker(self, repo: str, phrase: str) -> list[GhIssueRef]: ...

    def list_sub_issues(self, repo: str, number: int) -> list[GhIssueRef]: ...

    def list_blocked_by(self, repo: str, number: int) -> list[GhIssueRef]: ...

    def list_issue_types(self, repo: str) -> set[str]: ...

    def ensure_label(self, repo: str, label: str) -> None: ...

    def create_issue(
        self,
        repo: str,
        title: str,
        body: str,
        labels: list[str],
        issue_type: str | None,
    ) -> GhIssueRef: ...

    def update_issue(self, repo: str, number: int, patch: IssuePatch) -> GhIssue: ...

    def add_labels(self, repo: str, number: int, labels: list[str]) -> None: ...

    def remove_label(self, repo: str, number: int, label: str) -> None: ...

    def add_sub_issue(self, repo: str, parent: int, child_id: int) -> None: ...

    def remove_sub_issue(self, repo: str, parent: int, child_id: int) -> None: ...

    def add_blocked_by(self, repo: str, number: int, blocker_id: int) -> None: ...

    def viewer_login(self) -> str: ...

    def list_timeline(self, repo: str, number: int) -> list[GhEvent]: ...

    def list_comments(self, repo: str, since: str | None) -> list[GhComment]: ...

    def body_editor(self, repo: str, number: int) -> tuple[str, str] | None: ...


#: Who last edited an issue body, and when (REST does not say).
BODY_EDITOR_QUERY = (
    "query($owner: String!, $name: String!, $number: Int!) {"
    " repository(owner: $owner, name: $name) {"
    " issue(number: $number) { lastEditedAt editor { login } } } }"
)


def _login(raw: Any) -> str | None:
    return str(raw["login"]) if isinstance(raw, dict) and raw.get("login") else None


class SubprocessGh:
    def __init__(self, program: str = "gh") -> None:
        self.program = program

    def _run(self, op: str, args: list[str]) -> str:
        cmd = [self.program, *args]
        try:
            res = run_with_timeout(
                cmd, capture_output=True, text=True, timeout=NETWORK_S, check=False
            )
        except OSError as e:
            raise GhSpawnError(self.program, e) from e

        if res.returncode != 0:
            raise GhCommandError(
                op=op,
                status=f"exit status: {res.returncode}",
                stderr=res.stderr.strip(),
            )
        return res.stdout

    def _json(self, op: str, args: list[str]) -> Any:
        out = self._run(op, args)
        try:
            return json.loads(out)
        except Exception as e:
            raise GhParseError(op, str(e)) from e

    def _pages(self, op: str, path: str, *fields: str) -> list[Any]:
        """Every item of a paginated GET: `gh api --paginate --slurp` reads them all."""
        args = ["api", "--method", "GET", path, "--paginate", "--slurp"]
        for f in (*fields, f"per_page={ISSUES_PER_PAGE}"):
            args.extend(["-f", f])
        pages = self._json(op, args)
        if not isinstance(pages, list) or not all(isinstance(p, list) for p in pages):
            raise GhParseError(op, "expected JSON array of pages")
        return [item for page in pages for item in page]

    def list_issues(self, repo: str, label: str) -> list[GhIssue]:
        items = self._pages(
            "list_issues", f"repos/{repo}/issues", "state=all", f"labels={label}"
        )
        return [self._parse_raw_issue(item) for item in items]

    def get_issue(self, repo: str, number: int) -> GhIssue:
        raw = self._json("get_issue", ["api", f"repos/{repo}/issues/{number}"])
        return self._parse_raw_issue(raw)

    def search_marker(self, repo: str, phrase: str) -> list[GhIssueRef]:
        raw = self._json(
            "search_marker",
            [
                "api",
                "--method",
                "GET",
                "search/issues",
                "-f",
                f'q=repo:{repo} in:body "{phrase}"',
            ],
        )
        items = raw.get("items", []) if isinstance(raw, dict) else []
        return [GhIssueRef(number=i["number"], id=i["id"]) for i in items]

    def list_sub_issues(self, repo: str, number: int) -> list[GhIssueRef]:
        items = self._pages(
            "list_sub_issues", f"repos/{repo}/issues/{number}/sub_issues"
        )
        return [GhIssueRef(number=i["number"], id=i["id"]) for i in items]

    def list_blocked_by(self, repo: str, number: int) -> list[GhIssueRef]:
        items = self._pages(
            "list_blocked_by",
            f"repos/{repo}/issues/{number}/dependencies/blocked_by",
        )
        return [GhIssueRef(number=i["number"], id=i["id"]) for i in items]

    def list_issue_types(self, repo: str) -> set[str]:
        raw = self._json("list_issue_types", ["api", f"repos/{repo}/issue-types"])
        if not isinstance(raw, list):
            raise GhParseError("list_issue_types", "expected JSON array")
        types_set: set[str] = set()
        for item in raw:
            if item.get("is_enabled", True):
                name = item.get("name")
                if name:
                    types_set.add(str(name))
        return types_set

    def ensure_label(self, repo: str, label: str) -> None:
        try:
            self._run("ensure_label", ["api", f"repos/{repo}/labels/{label}"])
            return
        except GhCommandError:
            pass

        self._run(
            "ensure_label",
            [
                "api",
                "--method",
                "POST",
                f"repos/{repo}/labels",
                "-f",
                f"name={label}",
            ],
        )

    def create_issue(
        self,
        repo: str,
        title: str,
        body: str,
        labels: list[str],
        issue_type: str | None,
    ) -> GhIssueRef:
        args = [
            "api",
            "--method",
            "POST",
            f"repos/{repo}/issues",
            "-f",
            f"title={title}",
            "-f",
            f"body={body}",
        ]
        if issue_type:
            args.extend(["-f", f"type={issue_type}"])
        for label in labels:
            args.extend(["-f", f"labels[]={label}"])
        raw = self._json("create_issue", args)
        return GhIssueRef(number=raw["number"], id=raw["id"])

    def update_issue(self, repo: str, number: int, patch: IssuePatch) -> GhIssue:
        args = ["api", "--method", "PATCH", f"repos/{repo}/issues/{number}"]
        if patch.title is not None:
            args.extend(["-f", f"title={patch.title}"])
        if patch.body is not None:
            args.extend(["-f", f"body={patch.body}"])
        if patch.issue_type is not None:
            args.extend(["-f", f"type={patch.issue_type}"])
        if patch.state is not None:
            args.extend(["-f", f"state={patch.state.wire_state()}"])
            reason = patch.state.wire_reason()
            if reason:
                args.extend(["-f", f"state_reason={reason}"])
        raw = self._json("update_issue", args)
        return self._parse_raw_issue(raw)

    def add_labels(self, repo: str, number: int, labels: list[str]) -> None:
        args = ["api", "--method", "POST", f"repos/{repo}/issues/{number}/labels"]
        for label in labels:
            args.extend(["-f", f"labels[]={label}"])
        self._run("add_labels", args)

    def remove_label(self, repo: str, number: int, label: str) -> None:
        self._run(
            "remove_label",
            [
                "api",
                "--method",
                "DELETE",
                f"repos/{repo}/issues/{number}/labels/{label}",
            ],
        )

    def add_sub_issue(self, repo: str, parent: int, child_id: int) -> None:
        self._run(
            "add_sub_issue",
            [
                "api",
                "--method",
                "POST",
                f"repos/{repo}/issues/{parent}/sub_issues",
                "-F",
                f"sub_issue_id={child_id}",
            ],
        )

    def remove_sub_issue(self, repo: str, parent: int, child_id: int) -> None:
        self._run(
            "remove_sub_issue",
            [
                "api",
                "--method",
                "DELETE",
                f"repos/{repo}/issues/{parent}/sub_issue",
                "-F",
                f"sub_issue_id={child_id}",
            ],
        )

    def add_blocked_by(self, repo: str, number: int, blocker_id: int) -> None:
        self._run(
            "add_blocked_by",
            [
                "api",
                "--method",
                "POST",
                f"repos/{repo}/issues/{number}/dependencies/blocked_by",
                "-F",
                f"issue_id={blocker_id}",
            ],
        )

    def viewer_login(self) -> str:
        login = _login(self._json("viewer_login", ["api", "user"]))
        if login is None:
            raise GhParseError("viewer_login", "the user has no login")
        return login

    def list_timeline(self, repo: str, number: int) -> list[GhEvent]:
        items = self._pages("list_timeline", f"repos/{repo}/issues/{number}/timeline")
        try:
            return [
                GhEvent(
                    id=str(item.get("id") or item.get("node_id") or ""),
                    event=str(item.get("event") or ""),
                    actor=_login(item.get("actor")),
                    at=str(item.get("created_at") or ""),
                    label=(item.get("label") or {}).get("name"),
                    rename_from=(item.get("rename") or {}).get("from"),
                    rename_to=(item.get("rename") or {}).get("to"),
                )
                for item in items
            ]
        except AttributeError as e:
            raise GhParseError("list_timeline", str(e)) from e

    def list_comments(self, repo: str, since: str | None) -> list[GhComment]:
        fields = [f"since={since}"] if since else []
        items = self._pages("list_comments", f"repos/{repo}/issues/comments", *fields)
        try:
            return [
                GhComment(
                    id=str(item["id"]),
                    issue=int(str(item["issue_url"]).rsplit("/", 1)[1]),
                    actor=_login(item.get("user")),
                    at=str(item.get("updated_at") or item["created_at"]),
                    body=str(item.get("body") or ""),
                )
                for item in items
            ]
        except (KeyError, IndexError, ValueError, TypeError) as e:
            raise GhParseError("list_comments", str(e)) from e

    def body_editor(self, repo: str, number: int) -> tuple[str, str] | None:
        owner, name = repo.split("/", 1)
        raw = self._json(
            "body_editor",
            [
                "api",
                "graphql",
                "-f",
                f"query={BODY_EDITOR_QUERY}",
                "-f",
                f"owner={owner}",
                "-f",
                f"name={name}",
                "-F",
                f"number={number}",
            ],
        )
        try:
            issue = raw["data"]["repository"]["issue"]
        except (KeyError, TypeError) as e:
            raise GhParseError("body_editor", str(e)) from e
        if not isinstance(issue, dict):
            raise GhParseError("body_editor", "The issue is missing or malformed.")
        login = _login(issue.get("editor"))
        edited = issue.get("lastEditedAt")
        return None if login is None or not edited else (login, str(edited))

    def _parse_raw_issue(self, raw: dict[str, Any]) -> GhIssue:
        issue_type_name = None
        if isinstance(raw.get("type"), dict):
            issue_type_name = raw["type"].get("name")
        labels = [
            l.get("name", "")
            for l in raw.get("labels", [])
            if isinstance(l, dict) and l.get("name")
        ]
        sub_total = None
        if isinstance(raw.get("sub_issues_summary"), dict):
            sub_total = raw["sub_issues_summary"].get("total")
        blocked_total = None
        if isinstance(raw.get("issue_dependencies_summary"), dict):
            blocked_total = raw["issue_dependencies_summary"].get("total_blocked_by")

        return GhIssue(
            number=raw["number"],
            id=raw["id"],
            title=raw.get("title", ""),
            body=raw.get("body") or "",
            state=raw.get("state", "open"),
            state_reason=raw.get("state_reason"),
            issue_type=issue_type_name,
            labels=labels,
            is_pull_request="pull_request" in raw and raw["pull_request"] is not None,
            sub_issues_total=sub_total,
            blocked_by_total=blocked_total,
            updated_at=raw.get("updated_at"),
        )
