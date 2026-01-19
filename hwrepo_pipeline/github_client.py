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

    # --- Commit Miner API Methods ---

    def list_merged_prs_graphql(
        self,
        owner: str,
        repo: str,
        max_prs: int = 100,
        since: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Fetch merged PRs using GraphQL for efficiency."""
        query = """
        query($owner: String!, $repo: String!, $cursor: String) {
            repository(owner: $owner, name: $repo) {
                pullRequests(
                    first: 50,
                    after: $cursor,
                    states: [MERGED],
                    orderBy: {field: UPDATED_AT, direction: DESC}
                ) {
                    pageInfo {
                        hasNextPage
                        endCursor
                    }
                    nodes {
                        number
                        title
                        mergedAt
                        baseRefOid
                        mergeCommit {
                            oid
                        }
                        author {
                            login
                        }
                        files(first: 100) {
                            nodes {
                                path
                                additions
                                deletions
                            }
                        }
                    }
                }
            }
        }
        """
        prs: List[Dict[str, Any]] = []
        cursor = None

        while len(prs) < max_prs:
            variables = {"owner": owner, "repo": repo, "cursor": cursor}
            try:
                data = self.post_graphql(query, variables)
            except requests.HTTPError as e:
                LOGGER.warning("GraphQL error fetching PRs: %s", e)
                break

            repo_data = data.get("repository")
            if not repo_data:
                break

            pr_data = repo_data.get("pullRequests", {})
            nodes = pr_data.get("nodes", [])

            for node in nodes:
                if node is None:
                    continue
                merged_at = node.get("mergedAt")
                if since and merged_at and merged_at < since:
                    return prs
                prs.append(node)
                if len(prs) >= max_prs:
                    break

            page_info = pr_data.get("pageInfo", {})
            if not page_info.get("hasNextPage"):
                break
            cursor = page_info.get("endCursor")

        return prs

    def list_merged_prs_rest(
        self,
        owner: str,
        repo: str,
        max_prs: int = 100,
        since: Optional[str] = None,
    ) -> Iterable[Dict[str, Any]]:
        """Fetch merged PRs using REST API (fallback)."""
        collected = 0
        page = 1
        per_page = 100

        while collected < max_prs:
            params = {
                "state": "closed",
                "sort": "updated",
                "direction": "desc",
                "per_page": per_page,
                "page": page,
            }
            data, _ = self.get_json(f"/repos/{owner}/{repo}/pulls", params=params)

            if not data:
                break

            for pr in data:
                if pr.get("merged_at") is None:
                    continue
                if since and pr.get("merged_at") < since:
                    return
                yield pr
                collected += 1
                if collected >= max_prs:
                    break

            if len(data) < per_page:
                break
            page += 1

    def get_pr_files(
        self, owner: str, repo: str, pr_number: int
    ) -> List[Dict[str, Any]]:
        """Get files changed in a PR."""
        files: List[Dict[str, Any]] = []
        page = 1
        per_page = 100

        while True:
            params = {"per_page": per_page, "page": page}
            data, _ = self.get_json(
                f"/repos/{owner}/{repo}/pulls/{pr_number}/files", params=params
            )
            if not data:
                break
            files.extend(data)
            if len(data) < per_page:
                break
            page += 1

        return files

    def compare_commits(
        self, owner: str, repo: str, base: str, head: str
    ) -> Optional[Dict[str, Any]]:
        """Compare two commits and get the diff."""
        data, _ = self.get_json_or_none(
            f"/repos/{owner}/{repo}/compare/{base}...{head}"
        )
        return data

    def list_commits(
        self,
        owner: str,
        repo: str,
        sha: Optional[str] = None,
        since: Optional[str] = None,
        until: Optional[str] = None,
        max_commits: int = 100,
    ) -> Iterable[Dict[str, Any]]:
        """List commits on a repository."""
        collected = 0
        page = 1
        per_page = 100

        while collected < max_commits:
            params: Dict[str, Any] = {"per_page": per_page, "page": page}
            if sha:
                params["sha"] = sha
            if since:
                params["since"] = since
            if until:
                params["until"] = until

            data, _ = self.get_json(f"/repos/{owner}/{repo}/commits", params=params)

            if not data:
                break

            for commit in data:
                yield commit
                collected += 1
                if collected >= max_commits:
                    break

            if len(data) < per_page:
                break
            page += 1

    def get_commit(self, owner: str, repo: str, sha: str) -> Optional[Dict[str, Any]]:
        """Get details of a single commit including files."""
        data, _ = self.get_json_or_none(f"/repos/{owner}/{repo}/commits/{sha}")
        return data

    def get_commit_files(
        self, owner: str, repo: str, sha: str
    ) -> List[Dict[str, Any]]:
        """Get files changed in a specific commit."""
        commit = self.get_commit(owner, repo, sha)
        if not commit:
            return []
        return commit.get("files", [])
