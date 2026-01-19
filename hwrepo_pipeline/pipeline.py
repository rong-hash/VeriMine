from __future__ import annotations

import json
import logging
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from .config import PipelineConfig
from .github_client import GitHubClient
from .models import MatchEvidence, RejectRecord, RepoCard
from .scanner import compile_allowlist, compile_denylist, extract_candidate_cmds, scan_text

LOGGER = logging.getLogger(__name__)


def _parse_dt(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _within_days(pushed_at: str, days: int) -> bool:
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    return _parse_dt(pushed_at) >= cutoff


def _estimate_total_from_link(link_header: Optional[str], per_page: int) -> Optional[int]:
    if not link_header:
        return None
    for part in link_header.split(","):
        if 'rel="last"' in part:
            start = part.find("page=")
            if start == -1:
                return None
            page_str = part[start + 5 :].split(">", 1)[0]
            try:
                last_page = int(page_str)
                return last_page * per_page
            except ValueError:
                return None
    return None


def discover_candidates(client: GitHubClient, config: PipelineConfig) -> List[Dict[str, Any]]:
    repos: Dict[str, Dict[str, Any]] = {}
    for language in config.search_languages:
        query = f"language:{language} {config.search_qualifiers} stars:>={config.min_stars}"
        for item in client.search_repositories(
            query=query,
            sort=config.search_sort,
            order=config.search_order,
            max_results=config.max_repos_per_language,
        ):
            repos[item["full_name"]] = item
    return list(repos.values())


def _language_ratio(languages: Dict[str, int]) -> float:
    total = sum(languages.values())
    if total == 0:
        return 0.0
    verilog = languages.get("Verilog", 0) + languages.get("SystemVerilog", 0)
    return verilog / total


def _count_sv_files(tree: Iterable[Dict[str, Any]], extensions: List[str]) -> int:
    count = 0
    for item in tree:
        if item.get("type") != "blob":
            continue
        path = item.get("path", "")
        if any(path.endswith(ext) for ext in extensions):
            count += 1
    return count


def _count_sv_lines(
    client: GitHubClient,
    owner: str,
    repo: str,
    paths: Iterable[str],
    min_lines: int,
    ref: Optional[str] = None,
) -> int:
    total = 0
    for path in paths:
        text = client.get_file_text(owner, repo, path, ref=ref)
        if text is None:
            continue
        total += len(text.splitlines())
        if total >= min_lines:
            break
    return total


def _get_ci_files(client: GitHubClient, owner: str, repo: str) -> Tuple[bool, List[str]]:
    ci_files: List[str] = []
    data = client.list_contents(owner, repo, ".github/workflows")
    if isinstance(data, list):
        for item in data:
            if item.get("type") == "file":
                ci_files.append(item.get("name", ""))
    if ci_files:
        return True, ci_files

    gitlab_ci = client.get_file_text(owner, repo, ".gitlab-ci.yml")
    if gitlab_ci:
        return True, [".gitlab-ci.yml"]

    return False, []


def _collect_scan_paths(
    client: GitHubClient,
    owner: str,
    repo: str,
    config: PipelineConfig,
    ci_files: List[str],
) -> List[str]:
    paths: Set[str] = set(config.scan_paths)

    if config.scan_workflows and ci_files:
        for name in ci_files:
            if name == ".gitlab-ci.yml":
                paths.add(name)
            else:
                paths.add(f".github/workflows/{name}")

    if config.scan_scripts_dir:
        data = client.list_contents(owner, repo, "scripts")
        if isinstance(data, list):
            for item in data[: config.max_script_files]:
                if item.get("type") == "file":
                    paths.add(item.get("path", ""))

    return sorted(p for p in paths if p)


def _scan_repo_for_tools(
    client: GitHubClient,
    owner: str,
    repo: str,
    paths: Iterable[str],
    allow_re,
    deny_re,
) -> Tuple[List[MatchEvidence], List[MatchEvidence], List[str], List[str]]:
    allow_hits: List[MatchEvidence] = []
    deny_hits: List[MatchEvidence] = []
    build_cmds: List[str] = []
    test_cmds: List[str] = []

    for path in paths:
        text = client.get_file_text(owner, repo, path)
        if text is None:
            continue
        allow, deny = scan_text(path, text, allow_re, deny_re)
        allow_hits.extend(allow)
        deny_hits.extend(deny)
        build, test = extract_candidate_cmds(path, text)
        build_cmds.extend(build)
        test_cmds.extend(test)

    return allow_hits, deny_hits, build_cmds, test_cmds


def _commit_count_graphql(
    client: GitHubClient,
    owner: str,
    repo: str,
    since: datetime,
) -> Optional[int]:
    query = """
    query($owner: String!, $name: String!, $since: GitTimestamp!) {
      repository(owner: $owner, name: $name) {
        defaultBranchRef {
          target {
            ... on Commit {
              history(since: $since) {
                totalCount
              }
            }
          }
        }
      }
    }
    """
    data = client.post_graphql(
        query,
        {
            "owner": owner,
            "name": repo,
            "since": since.isoformat(),
        },
    )
    ref = data.get("repository", {}).get("defaultBranchRef")
    if not ref:
        return None
    history = ref.get("target", {}).get("history")
    if not history:
        return None
    return int(history.get("totalCount", 0))


def _commit_count_rest(
    client: GitHubClient,
    owner: str,
    repo: str,
    since: datetime,
) -> Optional[int]:
    data, headers = client.get_json(
        f"/repos/{owner}/{repo}/commits",
        params={"since": since.isoformat(), "per_page": 1},
    )
    if not data:
        return 0
    estimated = _estimate_total_from_link(headers.get("Link"), per_page=1)
    if estimated is not None:
        return estimated
    return len(data)


def _commit_count(
    client: GitHubClient,
    owner: str,
    repo: str,
    since: datetime,
    use_graphql: bool,
) -> Optional[int]:
    if use_graphql:
        try:
            return _commit_count_graphql(client, owner, repo, since)
        except Exception:
            LOGGER.exception("GraphQL commit count failed; falling back to REST")
    try:
        return _commit_count_rest(client, owner, repo, since)
    except Exception:
        LOGGER.exception("REST commit count failed")
        return None


def evaluate_repo(
    client: GitHubClient, item: Dict[str, Any], config: PipelineConfig
) -> Tuple[Optional[RepoCard], Optional[RejectRecord]]:
    full_name = item["full_name"]
    owner = item["owner"]["login"]
    repo = item["name"]
    default_branch = item.get("default_branch", "")
    reasons: List[str] = []

    if item.get("archived") or item.get("fork"):
        reasons.append("archived_or_fork")

    if item.get("stargazers_count", 0) < config.min_stars:
        reasons.append("min_stars")

    pushed_at = item.get("pushed_at")
    if not pushed_at or not _within_days(pushed_at, config.pushed_within_days):
        reasons.append("pushed_at")

    try:
        languages = client.get_languages(owner, repo)
    except Exception:
        reasons.append("languages_api")
        languages = None

    sv_ratio = _language_ratio(languages) if languages else 0.0
    if languages and sv_ratio < config.min_sv_ratio:
        reasons.append("sv_ratio")

    sv_file_count = 0
    sv_line_count = -1
    try:
        tree = client.get_tree(owner, repo, default_branch)
    except Exception:
        tree = None
        reasons.append("tree_api")

    if tree is not None:
        sv_file_count = _count_sv_files(tree, config.verilog_extensions)
        file_pass = config.min_sv_files == 0 or sv_file_count >= config.min_sv_files

        line_pass = True
        if config.min_sv_lines and not file_pass:
            sv_paths = [
                entry.get("path", "")
                for entry in tree
                if entry.get("type") == "blob"
                and any(entry.get("path", "").endswith(ext) for ext in config.verilog_extensions)
            ]
            sv_line_count = _count_sv_lines(
                client,
                owner,
                repo,
                sv_paths,
                config.min_sv_lines,
                ref=default_branch,
            )
            line_pass = sv_line_count >= config.min_sv_lines

        if not (file_pass or line_pass):
            reasons.append("sv_size")
    else:
        if "tree_api" not in reasons:
            reasons.append("tree_api")

    has_ci, ci_files = _get_ci_files(client, owner, repo)
    scan_paths = _collect_scan_paths(client, owner, repo, config, ci_files)
    allow_re = compile_allowlist(config.allowlist_terms)
    deny_re = compile_denylist(config.denylist_terms)
    allow_hits, deny_hits, build_cmds, test_cmds = _scan_repo_for_tools(
        client, owner, repo, scan_paths, allow_re, deny_re
    )

    if deny_hits:
        reasons.append("denylist_tools")

    if not allow_hits:
        reasons.append("allowlist_missing")

    pr_total = 0
    if config.min_pr_total:
        try:
            pr_total = client.search_issues_total(f"repo:{full_name} is:pr")
            if pr_total < config.min_pr_total:
                reasons.append("pr_total")
        except Exception:
            reasons.append("pr_total_api")
    else:
        try:
            pr_total = client.search_issues_total(f"repo:{full_name} is:pr")
        except Exception:
            pr_total = 0

    issue_total = 0
    if config.min_issue_total:
        try:
            issue_total = client.search_issues_total(f"repo:{full_name} is:issue")
            if issue_total < config.min_issue_total:
                reasons.append("issue_total")
        except Exception:
            reasons.append("issue_total_api")
    else:
        try:
            issue_total = client.search_issues_total(f"repo:{full_name} is:issue")
        except Exception:
            issue_total = 0

    commit_12m = None
    commit_6m = None
    if config.min_commit_last_12m or config.min_commit_last_6m:
        now = datetime.now(timezone.utc)
        commit_12m = _commit_count(
            client, owner, repo, now - timedelta(days=365), config.use_graphql
        )
        commit_6m = _commit_count(
            client, owner, repo, now - timedelta(days=182), config.use_graphql
        )
        if commit_12m is None or commit_6m is None:
            reasons.append("commit_count")
        elif (
            commit_12m < config.min_commit_last_12m
            and commit_6m < config.min_commit_last_6m
        ):
            reasons.append("commit_activity")

    has_release_or_tags = True
    if config.min_releases or config.min_tags:
        try:
            releases, _ = client.get_releases(owner, repo, per_page=1)
            has_release = (
                len(releases) >= config.min_releases if config.min_releases else False
            )

            tags, headers = client.get_tags(owner, repo, per_page=1)
            tags_est = _estimate_total_from_link(headers.get("Link"), per_page=1)
            tags_count = tags_est if tags_est is not None else len(tags)

            has_release_or_tags = has_release or tags_count >= config.min_tags
            if not has_release_or_tags:
                reasons.append("release_or_tags")
        except Exception:
            reasons.append("release_or_tags_api")
            has_release_or_tags = False

    if reasons:
        return None, RejectRecord(repo=full_name, reasons=sorted(set(reasons)))

    card = RepoCard(
        repo=full_name,
        default_branch=default_branch,
        stars=item.get("stargazers_count", 0),
        pushed_at=pushed_at,
        sv_ratio=sv_ratio,
        sv_file_count=sv_file_count,
        sv_line_count=sv_line_count,
        has_ci=has_ci,
        ci_files=ci_files,
        commit_count_last_12m=commit_12m,
        commit_count_last_6m=commit_6m,
        pr_total=pr_total,
        issue_total=issue_total,
        has_release_or_tags=has_release_or_tags,
        open_eda_evidence=allow_hits,
        deny_evidence=deny_hits,
        candidate_build_cmds=sorted(set(build_cmds)),
        candidate_test_cmds=sorted(set(test_cmds)),
    )
    return card, None


def run_pipeline(
    client: GitHubClient,
    config: PipelineConfig,
    output_path: Path,
    reject_path: Path,
) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    reject_path.parent.mkdir(parents=True, exist_ok=True)

    with output_path.open("w", encoding="utf-8") as out_f, reject_path.open(
        "w", encoding="utf-8"
    ) as rej_f:
        for item in discover_candidates(client, config):
            card, reject = evaluate_repo(client, item, config)
            if card:
                out_f.write(json.dumps(asdict(card), ensure_ascii=False) + "\n")
                out_f.flush()
            elif reject:
                rej_f.write(json.dumps(asdict(reject), ensure_ascii=False) + "\n")
                rej_f.flush()
