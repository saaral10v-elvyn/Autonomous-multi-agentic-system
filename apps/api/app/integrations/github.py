"""GitHub REST adapter."""

import re
from collections.abc import Mapping
from urllib.parse import quote

from app.integrations.http import request_json


class GitHubAdapter:
    kind = "github"
    api = "https://api.github.com"

    def __init__(self, token: str) -> None:
        self._token = token

    async def execute(self, action: str, payload: Mapping[str, object]) -> Mapping[str, object]:
        if action == "create_repo":
            name = _required(payload, "name")
            if not _REPO_PART.fullmatch(name):
                raise ValueError("Invalid GitHub repository name.")
            result = await request_json(
                f"{self.api}/user/repos",
                self._token,
                "POST",
                {"name": name, "private": payload.get("private", True)},
            )
            return _safe_fields(
                result, "id", "full_name", "html_url", "default_branch", "clone_url"
            )
        owner, repo = _required(payload, "owner"), _required(payload, "repo")
        if not _REPO_PART.fullmatch(owner) or not _REPO_PART.fullmatch(repo):
            raise ValueError("Invalid GitHub repository target.")
        if action == "repo_info":
            result = await request_json(f"{self.api}/repos/{owner}/{repo}", self._token, "GET")
            return _safe_fields(result, "id", "full_name", "html_url", "default_branch")
        if action == "branch_head":
            branch = quote(_required(payload, "branch"), safe="")
            result = await request_json(
                f"{self.api}/repos/{owner}/{repo}/git/ref/heads/{branch}", self._token, "GET"
            )
            value = result.get("object", {}) if isinstance(result, dict) else {}
            return _safe_fields(value, "sha")
        if action == "create_branch":
            branch = _required(payload, "branch")
            if not branch.startswith("forge/") or ".." in branch:
                raise ValueError("Feature branch names must use the forge/ prefix.")
            base = _required(payload, "sha")
            await request_json(
                f"{self.api}/repos/{owner}/{repo}/git/refs",
                self._token,
                "POST",
                {"ref": f"refs/heads/{branch}", "sha": base},
            )
            return {"branch": branch}
        if action == "commit_file":
            path = _required(payload, "path")
            branch = _required(payload, "branch")
            if not branch.startswith("forge/") or ".." in branch:
                raise ValueError("Commits must target a forge/ feature branch.")
            if path.startswith("/") or ".." in path.split("/"):
                raise ValueError("File path must stay inside the repository.")
            result = await request_json(
                f"{self.api}/repos/{owner}/{repo}/contents/{quote(path, safe='/')}",
                self._token,
                "PUT",
                {
                    "message": _required(payload, "message"),
                    "content": _required(payload, "content"),
                    "branch": branch,
                },
            )
            commit = result.get("commit", {}) if isinstance(result, dict) else {}
            return _safe_fields(commit, "sha", "html_url")
        if action == "create_pr":
            result = await request_json(
                f"{self.api}/repos/{owner}/{repo}/pulls",
                self._token,
                "POST",
                {
                    "title": _required(payload, "title"),
                    "head": _required(payload, "head"),
                    "base": _required(payload, "base"),
                    "body": str(payload.get("body", "")),
                },
            )
            return _safe_fields(result, "number", "html_url", "state")
        if action == "push_default":
            branch, sha = _required(payload, "branch"), _required(payload, "sha")
            current = await request_json(f"{self.api}/repos/{owner}/{repo}", self._token, "GET")
            if not isinstance(current, dict) or current.get("default_branch") != branch:
                raise ValueError("Only the repository default branch can be updated.")
            result = await request_json(
                f"{self.api}/repos/{owner}/{repo}/git/refs/heads/{quote(branch, safe='/')}",
                self._token,
                "PATCH",
                {"sha": sha, "force": False},
            )
            return {"updated": bool(isinstance(result, dict) and result.get("ref"))}
        raise ValueError("Unsupported GitHub action.")


def _required(payload: Mapping[str, object], field: str) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value.strip() or value.startswith("-"):
        raise ValueError(f"{field} is required.")
    return value.strip()


def _safe_fields(value: object, *fields: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise RuntimeError("External integration returned an invalid response.")
    return {field: value[field] for field in fields if field in value}


_REPO_PART = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")
