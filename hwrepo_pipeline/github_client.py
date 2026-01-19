from __future__ import annotations

import base64
import logging
import time
from typing import Any, Dict, Iterable, List, Optional, Tuple

import requests

LOGGER = logging.getLogger(__name__)


class GitHubClient:
    def __init__(self, token: Optional[str] = None, base_url: str = "https://api.github.com"):
        self.base_url = base_url.rstrip("/")
        self.session = requests.Session()
        self.session.headers.update({"Accept": "application/vnd.github+json"})
        if token:
            self.session.headers.update({"Authorization": f"Bearer {token}"})

    def request(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        resp = self.session.request(method, url, **kwargs)
        if resp.status_code == 403 and resp.headers.get("X-RateLimit-Remaining") == "0":
            reset = int(resp.headers.get("X-RateLimit-Reset", "0"))
            sleep_for = max(0, reset - int(time.time()))
            LOGGER.warning("rate limit hit; sleeping for %ss", sleep_for)
            time.sleep(sleep_for)
            resp = self.session.request(method, url, **kwargs)
        return resp

    def get_json(self, path: str, params: Optional[Dict[str, Any]] = None) -> Tuple[Any, Dict[str, str]]:
        url = f"{self.base_url}{path}"
        resp = self.request("GET", url, params=params)
        if resp.status_code >= 400:
            raise requests.HTTPError(f"{resp.status_code}: {resp.text}")
        return resp.json(), resp.headers

    def get_json_or_none(
        self, path: str, params: Optional[Dict[str, Any]] = None
    ) -> Tuple[Optional[Any], Dict[str, str]]:
        url = f"{self.base_url}{path}"
        resp = self.request("GET", url, params=params)
        if resp.status_code == 404:
            return None, resp.headers
        if resp.status_code >= 400:
            raise requests.HTTPError(f"{resp.status_code}: {resp.text}")
        return resp.json(), resp.headers

    def post_graphql(self, query: str, variables: Dict[str, Any]) -> Dict[str, Any]:
        url = f"{self.base_url}/graphql"
        resp = self.request("POST", url, json={"query": query, "variables": variables})
        if resp.status_code >= 400:
            raise requests.HTTPError(f"{resp.status_code}: {resp.text}")
        payload = resp.json()
        if "errors" in payload:
            raise requests.HTTPError(f"GraphQL error: {payload['errors']}")
        return payload["data"]

    def search_repositories(
        self,
        query: str,
        sort: str,
        order: str,
        max_results: int,
        per_page: int = 100,
    ) -> Iterable[Dict[str, Any]]:
        collected = 0
        page = 1
        while collected < max_results:
            params = {
                "q": query,
                "sort": sort,
                "order": order,
                "per_page": per_page,
                "page": page,
            }
            data, _ = self.get_json("/search/repositories", params=params)
            items = data.get("items", [])
            if not items:
                break
            for item in items:
                yield item
                collected += 1
                if collected >= max_results:
                    break
            if len(items) < per_page:
                break
            page += 1

    def list_contents(
        self, owner: str, repo: str, path: str, ref: Optional[str] = None
    ) -> Optional[Any]:
        params = {"ref": ref} if ref else None
        data, _ = self.get_json_or_none(
            f"/repos/{owner}/{repo}/contents/{path}", params=params
        )
        return data

    def get_file_text(
        self, owner: str, repo: str, path: str, ref: Optional[str] = None
    ) -> Optional[str]:
        data = self.list_contents(owner, repo, path, ref=ref)
        if not data or data.get("type") != "file":
            return None
        if "content" in data and data.get("encoding") == "base64":
            decoded = base64.b64decode(data["content"])
            return decoded.decode("utf-8", errors="replace")
        if data.get("download_url"):
            resp = self.request("GET", data["download_url"])
            if resp.status_code >= 400:
                return None
            return resp.text
        return None

    def get_languages(self, owner: str, repo: str) -> Dict[str, int]:
        data, _ = self.get_json(f"/repos/{owner}/{repo}/languages")
        return data

    def get_tree(self, owner: str, repo: str, ref: str) -> Optional[List[Dict[str, Any]]]:
        data, _ = self.get_json_or_none(
            f"/repos/{owner}/{repo}/git/trees/{ref}", params={"recursive": 1}
        )
        if not data:
            return None
        return data.get("tree", [])

    def search_issues_total(self, query: str) -> int:
        data, _ = self.get_json("/search/issues", params={"q": query, "per_page": 1})
        return int(data.get("total_count", 0))

    def get_releases(self, owner: str, repo: str, per_page: int = 1) -> Tuple[List[Any], Dict[str, str]]:
        data, headers = self.get_json(
            f"/repos/{owner}/{repo}/releases", params={"per_page": per_page}
        )
        return data, headers

    def get_tags(self, owner: str, repo: str, per_page: int = 1) -> Tuple[List[Any], Dict[str, str]]:
        data, headers = self.get_json(
            f"/repos/{owner}/{repo}/tags", params={"per_page": per_page}
        )
        return data, headers
