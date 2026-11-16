"""Vercel REST adapter."""

from collections.abc import Mapping

from app.integrations.http import request_json


class VercelAdapter:
    kind = "vercel"
    api = "https://api.vercel.com"

    def __init__(self, token: str) -> None:
        self._token = token

    async def execute(self, action: str, payload: Mapping[str, object]) -> Mapping[str, object]:
        if action == "create_project":
            result = await request_json(
                f"{self.api}/v10/projects",
                self._token,
                "POST",
                {
                    "name": _required(payload, "name"),
                    "framework": payload.get("framework", "nextjs"),
                    "gitRepository": {
                        "type": "github",
                        "repo": _required(payload, "repository"),
                    },
                },
            )
            return _safe_fields(result, "id", "name", "link")
        if action == "deploy":
            result = await request_json(
                f"{self.api}/v13/deployments",
                self._token,
                "POST",
                {
                    "name": _required(payload, "name"),
                    "project": _required(payload, "project_id"),
                    "gitSource": {
                        "type": "github",
                        "repoId": _required(payload, "repo_id"),
                        "ref": str(payload.get("branch", "main")),
                    },
                },
            )
            return _safe_fields(result, "id", "url", "readyState", "inspectorUrl")
        if action == "deployment_status":
            result = await request_json(
                f"{self.api}/v13/deployments/{_required(payload, 'deployment_id')}",
                self._token,
                "GET",
            )
            return _safe_fields(result, "id", "url", "readyState", "createdAt")
        if action == "deployment_url":
            result = await request_json(
                f"{self.api}/v13/deployments/{_required(payload, 'deployment_id')}",
                self._token,
                "GET",
            )
            value = _safe_fields(result, "url")
            if not isinstance(value.get("url"), str):
                raise RuntimeError("Vercel deployment URL is unavailable.")
            return {"url": "https://" + str(value["url"]).removeprefix("https://")}
        raise ValueError("Unsupported Vercel action.")


def _required(payload: Mapping[str, object], field: str) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value.strip() or value.startswith("-"):
        raise ValueError(f"{field} is required.")
    return value.strip()


def _safe_fields(value: object, *fields: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise RuntimeError("External integration returned an invalid response.")
    return {field: value[field] for field in fields if field in value}
