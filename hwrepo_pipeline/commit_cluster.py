"""Commit clustering algorithm for grouping related commits."""
from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Set

from .diff_classifier import compute_file_overlap, extract_issue_refs
from .models import CommitCluster, CommitInfo

LOGGER = logging.getLogger(__name__)


def parse_iso_datetime(dt_str: str) -> datetime:
    """Parse ISO 8601 datetime string."""
    # Handle various ISO formats
    dt_str = dt_str.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(dt_str)
    except ValueError:
        # Fallback for some edge cases
        return datetime.strptime(dt_str[:19], "%Y-%m-%dT%H:%M:%S")


def generate_cluster_id(repo: str, commits: List[CommitInfo]) -> str:
    """Generate a unique cluster ID based on repo and commit SHAs."""
    sha_concat = "".join(c.sha[:8] for c in commits)
    hash_input = f"{repo}:{sha_concat}".encode()
    return hashlib.sha256(hash_input).hexdigest()[:12]


def is_merge_commit(commit: CommitInfo) -> bool:
    """Check if a commit is a merge commit (has multiple parents)."""
    return len(commit.parents) > 1


def cluster_commits(
    repo: str,
    commits: List[CommitInfo],
    time_window_hours: int = 24,
    file_overlap_threshold: float = 0.3,
    covered_shas: Optional[Set[str]] = None,
) -> List[CommitCluster]:
    """
    Cluster commits using multiple strategies:
    1. Merge boundary: merge commits act as cluster separators
    2. Issue link: commits referencing the same issue are grouped
    3. Time + Author + File: same author within time window with file overlap

    Args:
        repo: Repository name (owner/repo)
        commits: List of commits in chronological order (oldest first)
        time_window_hours: Max hours between commits in same cluster
        file_overlap_threshold: Min file overlap ratio (0-1)
        covered_shas: Set of commit SHAs already covered by PRs (to exclude)

    Returns:
        List of CommitCluster objects
    """
    if covered_shas is None:
        covered_shas = set()

    # Filter out covered commits
    uncovered = [c for c in commits if c.sha not in covered_shas]
    if not uncovered:
        return []

    clusters: List[CommitCluster] = []

    # Group by issue reference first
    issue_groups: Dict[str, List[CommitInfo]] = {}
    no_issue_commits: List[CommitInfo] = []

    for commit in uncovered:
        refs = extract_issue_refs(commit.message)
        if refs:
            # Use the first issue ref as the group key
            key = refs[0]
            if key not in issue_groups:
                issue_groups[key] = []
            issue_groups[key].append(commit)
        else:
            no_issue_commits.append(commit)

    # Create clusters from issue groups
    for issue_ref, issue_commits in issue_groups.items():
        if len(issue_commits) < 1:
            continue

        # Sort by date
        issue_commits.sort(key=lambda c: c.authored_date)

        # Find base_sha (parent of first commit)
        base_sha = ""
        if issue_commits[0].parents:
            base_sha = issue_commits[0].parents[0]

        cluster = CommitCluster(
            cluster_id=generate_cluster_id(repo, issue_commits),
            repo=repo,
            commits=issue_commits,
            base_sha=base_sha,
            target_sha=issue_commits[-1].sha,
            issue_refs=[issue_ref],
        )
        clusters.append(cluster)

    # Cluster remaining commits by time/author/file overlap
    if no_issue_commits:
        # Sort by date
        no_issue_commits.sort(key=lambda c: c.authored_date)

        current_cluster: List[CommitInfo] = []
        current_author: Optional[str] = None
        current_files: List[str] = []
        last_time: Optional[datetime] = None

        for commit in no_issue_commits:
            # Merge commits act as cluster boundaries
            if is_merge_commit(commit):
                if current_cluster:
                    clusters.append(_make_cluster(repo, current_cluster))
                current_cluster = []
                current_author = None
                current_files = []
                last_time = None
                continue

            commit_time = parse_iso_datetime(commit.authored_date)
            commit_files = [f.path for f in commit.files]

            should_start_new = False

            if not current_cluster:
                should_start_new = False  # First commit, start cluster
            elif current_author != commit.author:
                should_start_new = True
            elif last_time and (commit_time - last_time) > timedelta(hours=time_window_hours):
                should_start_new = True
            elif current_files and commit_files:
                overlap = compute_file_overlap(current_files, commit_files)
                if overlap < file_overlap_threshold:
                    should_start_new = True

            if should_start_new and current_cluster:
                clusters.append(_make_cluster(repo, current_cluster))
                current_cluster = []
                current_files = []

            current_cluster.append(commit)
            current_author = commit.author
            current_files.extend(commit_files)
            last_time = commit_time

        # Don't forget the last cluster
        if current_cluster:
            clusters.append(_make_cluster(repo, current_cluster))

    return clusters


def _make_cluster(repo: str, commits: List[CommitInfo]) -> CommitCluster:
    """Create a CommitCluster from a list of commits."""
    # Find base_sha (parent of first commit)
    base_sha = ""
    if commits and commits[0].parents:
        base_sha = commits[0].parents[0]

    # Collect all issue refs
    all_refs: List[str] = []
    for c in commits:
        all_refs.extend(extract_issue_refs(c.message))

    return CommitCluster(
        cluster_id=generate_cluster_id(repo, commits),
        repo=repo,
        commits=commits,
        base_sha=base_sha,
        target_sha=commits[-1].sha if commits else "",
        issue_refs=list(set(all_refs)),
    )


def get_pr_covered_shas(
    prs: List[dict],
    get_commits_between: callable = None,
) -> Set[str]:
    """
    Get all commit SHAs that are covered by merged PRs.

    This is used to exclude commits that are already part of PRs
    from the clustering process.

    Args:
        prs: List of PR info dicts with 'base_sha' and 'merge_commit_sha'
        get_commits_between: Optional function to get commits between two SHAs

    Returns:
        Set of covered commit SHAs
    """
    covered: Set[str] = set()

    for pr in prs:
        # At minimum, add the merge commit
        merge_sha = pr.get("merge_commit_sha") or pr.get("mergeCommit", {}).get("oid")
        if merge_sha:
            covered.add(merge_sha)

        # If we have a way to get commits between base and merge,
        # add all of those too
        base_sha = pr.get("base_sha") or pr.get("baseRefOid")
        if base_sha and merge_sha and get_commits_between:
            try:
                between = get_commits_between(base_sha, merge_sha)
                covered.update(between)
            except Exception:
                pass

    return covered
