"""Commit clustering algorithm for grouping related commits by author."""
from __future__ import annotations

import hashlib
import logging
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Set, Tuple

from .diff_classifier import (
    classify_files,
    compute_file_overlap,
    extract_issue_refs,
    merge_patches,
)
from .models import AuthorContribution, CommitInfo, FilePatch

LOGGER = logging.getLogger(__name__)


def parse_iso_datetime(dt_str: str) -> datetime:
    """Parse ISO 8601 datetime string."""
    dt_str = dt_str.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(dt_str)
    except ValueError:
        return datetime.strptime(dt_str[:19], "%Y-%m-%dT%H:%M:%S")


def generate_contribution_id(repo: str, author: str, commits: List[CommitInfo]) -> str:
    """Generate a unique contribution ID."""
    sha_concat = "".join(c.sha[:8] for c in commits[:5])  # Use first 5 commits
    hash_input = f"{repo}:{author}:{sha_concat}".encode()
    return hashlib.sha256(hash_input).hexdigest()[:12]


def group_commits_by_author(commits: List[CommitInfo]) -> Dict[str, List[CommitInfo]]:
    """Group commits by author name."""
    author_groups: Dict[str, List[CommitInfo]] = defaultdict(list)
    for commit in commits:
        author_groups[commit.author].append(commit)
    return dict(author_groups)


def cluster_author_commits_by_feature(
    commits: List[CommitInfo],
    time_window_days: int = 60,
    file_overlap_threshold: float = 0.2,
) -> List[List[CommitInfo]]:
    """
    Cluster an author's commits into feature groups.

    Uses file path similarity and time proximity to group related commits.
    A longer time window is used since feature development can span weeks.

    Args:
        commits: List of commits from a single author (chronologically sorted)
        time_window_days: Max days between commits in same feature cluster
        file_overlap_threshold: Min file overlap ratio to consider related

    Returns:
        List of commit groups (each group is a feature)
    """
    if not commits:
        return []

    # Sort by date
    sorted_commits = sorted(commits, key=lambda c: c.authored_date)

    clusters: List[List[CommitInfo]] = []
    current_cluster: List[CommitInfo] = [sorted_commits[0]]
    current_files: Set[str] = set(f.path for f in sorted_commits[0].files)
    last_time = parse_iso_datetime(sorted_commits[0].authored_date)

    for commit in sorted_commits[1:]:
        commit_time = parse_iso_datetime(commit.authored_date)
        commit_files = set(f.path for f in commit.files)

        # Check if this commit belongs to current cluster
        time_gap = (commit_time - last_time).days
        file_overlap = compute_file_overlap(list(current_files), list(commit_files))

        # Criteria for same feature:
        # 1. Within time window AND has file overlap, OR
        # 2. Very high file overlap (same files being modified)
        same_feature = (
            (time_gap <= time_window_days and file_overlap >= file_overlap_threshold)
            or file_overlap >= 0.5  # High overlap = definitely same feature
        )

        if same_feature:
            current_cluster.append(commit)
            current_files.update(commit_files)
            last_time = commit_time
        else:
            # Start new cluster
            if current_cluster:
                clusters.append(current_cluster)
            current_cluster = [commit]
            current_files = commit_files
            last_time = commit_time

    # Don't forget the last cluster
    if current_cluster:
        clusters.append(current_cluster)

    return clusters


def collect_author_contributions(
    repo: str,
    commits: List[CommitInfo],
    time_window_days: int = 60,
    min_commits: int = 2,
) -> List[AuthorContribution]:
    """
    Collect contributions from all authors in the repository.

    This groups commits by author, then by feature within each author,
    and collects/merges patches from each contribution.

    Args:
        repo: Repository name (owner/repo)
        commits: List of all commits
        time_window_days: Max days between commits in same feature
        min_commits: Minimum commits required for a contribution

    Returns:
        List of AuthorContribution objects
    """
    contributions: List[AuthorContribution] = []

    # Group by author
    author_groups = group_commits_by_author(commits)

    for author, author_commits in author_groups.items():
        # Cluster by feature within this author's commits
        feature_clusters = cluster_author_commits_by_feature(
            author_commits,
            time_window_days=time_window_days,
        )

        for cluster in feature_clusters:
            if len(cluster) < min_commits:
                continue

            # Sort chronologically
            cluster.sort(key=lambda c: c.authored_date)

            # Collect all patches from this cluster
            all_patches: List[List[FilePatch]] = []
            commit_shas: List[str] = []
            commit_messages: List[str] = []

            for commit in cluster:
                commit_shas.append(commit.sha)
                commit_messages.append(commit.message.split("\n")[0][:100])
                all_patches.append(commit.files)

            # Merge patches
            code_patches, test_patches = merge_patches(all_patches)

            # Only include if has both code and test patches
            if not code_patches or not test_patches:
                continue

            contribution = AuthorContribution(
                repo=repo,
                author=author,
                contribution_id=generate_contribution_id(repo, author, cluster),
                commits=commit_shas,
                first_commit_date=cluster[0].authored_date,
                last_commit_date=cluster[-1].authored_date,
                code_patches=code_patches,
                test_patches=test_patches,
                commit_messages=commit_messages,
                validation_status="pending",
            )
            contributions.append(contribution)

    return contributions


def get_pr_covered_shas(prs: List[dict]) -> Set[str]:
    """
    Get all commit SHAs that are covered by merged PRs.

    Args:
        prs: List of PR info dicts

    Returns:
        Set of covered commit SHAs
    """
    covered: Set[str] = set()

    for pr in prs:
        merge_sha = pr.get("merge_commit_sha") or pr.get("mergeCommit", {}).get("oid")
        if merge_sha:
            covered.add(merge_sha)

    return covered


# Keep old function for backward compatibility
def cluster_commits(
    repo: str,
    commits: List[CommitInfo],
    time_window_hours: int = 24,
    file_overlap_threshold: float = 0.3,
    covered_shas: Optional[Set[str]] = None,
) -> List[AuthorContribution]:
    """
    Cluster commits by author and feature.

    This is the main entry point for commit clustering.
    Now returns AuthorContribution instead of CommitCluster.
    """
    if covered_shas is None:
        covered_shas = set()

    # Filter out covered commits
    uncovered = [c for c in commits if c.sha not in covered_shas]
    if not uncovered:
        return []

    # Convert hours to days for the new algorithm
    time_window_days = max(1, time_window_hours // 24) if time_window_hours < 24 * 60 else 60

    return collect_author_contributions(
        repo=repo,
        commits=uncovered,
        time_window_days=time_window_days,
        min_commits=1,  # Allow single commits if they have code+test
    )
