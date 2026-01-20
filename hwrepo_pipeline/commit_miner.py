"""Core commit mining logic for extracting commit pairs and author contributions."""
from __future__ import annotations

import json
import logging
from dataclasses import asdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple, Union

from .commit_cluster import collect_author_contributions, get_pr_covered_shas
from .config import MinerConfig
from .diff_classifier import classify_files, has_valid_patches
from .github_client import GitHubClient
from .models import (
    AuthorContribution,
    CommitInfo,
    CommitPair,
    FilePatch,
    MinerRejectRecord,
)

LOGGER = logging.getLogger(__name__)


class CommitMiner:
    """Mines commit pairs and author contributions from repositories."""

    def __init__(self, client: GitHubClient, config: MinerConfig):
        self.client = client
        self.config = config

    def mine_repo(
        self, repo: str
    ) -> Tuple[List[CommitPair], List[AuthorContribution], List[MinerRejectRecord]]:
        """
        Mine commit pairs and author contributions from a single repository.

        Args:
            repo: Repository name in 'owner/repo' format

        Returns:
            Tuple of (commit_pairs, author_contributions, rejected_records)
        """
        owner, repo_name = repo.split("/")
        pairs: List[CommitPair] = []
        contributions: List[AuthorContribution] = []
        rejects: List[MinerRejectRecord] = []

        # Calculate lookback date
        since_date = (
            datetime.utcnow() - timedelta(days=self.config.lookback_days)
        ).isoformat() + "Z"

        LOGGER.info("Mining repo %s (since %s)", repo, since_date[:10])

        # Phase 1: Mine from PRs (produces CommitPair)
        pr_pairs, pr_rejects, covered_shas = self._mine_prs(
            owner, repo_name, since_date
        )
        pairs.extend(pr_pairs)
        rejects.extend(pr_rejects)

        LOGGER.info(
            "PR mining: %d pairs, %d rejects, %d covered SHAs",
            len(pr_pairs), len(pr_rejects), len(covered_shas)
        )

        # Phase 2: Mine from author contributions (produces AuthorContribution)
        if self.config.enable_cluster_mining:
            author_contribs, contrib_rejects = self._mine_author_contributions(
                owner, repo_name, since_date, covered_shas
            )
            contributions.extend(author_contribs)
            rejects.extend(contrib_rejects)

            LOGGER.info(
                "Author contribution mining: %d contributions, %d rejects",
                len(author_contribs), len(contrib_rejects)
            )

        return pairs, contributions, rejects

    def _mine_prs(
        self, owner: str, repo_name: str, since: str
    ) -> Tuple[List[CommitPair], List[MinerRejectRecord], Set[str]]:
        """Mine commit pairs from merged PRs."""
        pairs: List[CommitPair] = []
        rejects: List[MinerRejectRecord] = []
        covered_shas: Set[str] = set()

        repo = f"{owner}/{repo_name}"

        # Fetch merged PRs
        if self.config.use_graphql:
            prs = self.client.list_merged_prs_graphql(
                owner, repo_name,
                max_prs=self.config.max_prs_per_repo,
                since=since,
            )
        else:
            prs = list(self.client.list_merged_prs_rest(
                owner, repo_name,
                max_prs=self.config.max_prs_per_repo,
                since=since,
            ))

        LOGGER.debug("Fetched %d merged PRs", len(prs))

        for pr in prs:
            pr_number = pr.get("number")
            merge_sha = pr.get("mergeCommit", {}).get("oid") if self.config.use_graphql else pr.get("merge_commit_sha")
            base_sha = pr.get("baseRefOid") if self.config.use_graphql else pr.get("base", {}).get("sha")

            if merge_sha:
                covered_shas.add(merge_sha)

            if not base_sha or not merge_sha:
                rejects.append(MinerRejectRecord(
                    repo=repo,
                    source_type="pr",
                    source_id=str(pr_number),
                    reasons=["missing base_sha or merge_sha"],
                ))
                continue

            # Get files for this PR
            if self.config.use_graphql:
                files = pr.get("files", {}).get("nodes", [])
                files = [
                    {"filename": f["path"], "additions": f["additions"], "deletions": f["deletions"]}
                    for f in files if f
                ]
            else:
                files = self.client.get_pr_files(owner, repo_name, pr_number)

            # Quick filter
            if not has_valid_patches(
                files,
                min_code=self.config.min_code_changes,
                min_test=self.config.min_test_changes,
            ):
                rejects.append(MinerRejectRecord(
                    repo=repo,
                    source_type="pr",
                    source_id=str(pr_number),
                    reasons=["insufficient code or test changes"],
                ))
                continue

            # Classify files
            code_patches, test_patches, _ = classify_files(files)

            if not code_patches:
                rejects.append(MinerRejectRecord(
                    repo=repo,
                    source_type="pr",
                    source_id=str(pr_number),
                    reasons=["no Verilog/SV code changes"],
                ))
                continue

            if not test_patches:
                rejects.append(MinerRejectRecord(
                    repo=repo,
                    source_type="pr",
                    source_id=str(pr_number),
                    reasons=["no test file changes"],
                ))
                continue

            # Create commit pair
            pair = CommitPair(
                repo=repo,
                base_sha=base_sha,
                target_sha=merge_sha,
                source_type="pr",
                source_id=str(pr_number),
                code_patches=code_patches,
                test_patches=test_patches,
                validation_status="pending",
            )
            pairs.append(pair)

        return pairs, rejects, covered_shas

    def _mine_author_contributions(
        self,
        owner: str,
        repo_name: str,
        since: str,
        covered_shas: Set[str],
    ) -> Tuple[List[AuthorContribution], List[MinerRejectRecord]]:
        """Mine author contributions by collecting patches from each author."""
        contributions: List[AuthorContribution] = []
        rejects: List[MinerRejectRecord] = []
        repo = f"{owner}/{repo_name}"

        # Fetch commits
        raw_commits = list(self.client.list_commits(
            owner, repo_name,
            since=since,
            max_commits=self.config.max_commits_per_repo,
        ))

        LOGGER.debug("Fetched %d commits", len(raw_commits))

        # Convert to CommitInfo objects and fetch files for each
        commits: List[CommitInfo] = []
        for c in raw_commits:
            sha = c.get("sha", "")
            if sha in covered_shas:
                continue

            commit_data = c.get("commit", {})
            author = commit_data.get("author", {})

            # Fetch files for this commit
            commit_files = self.client.get_commit_files(owner, repo_name, sha)
            code_patches, test_patches, _ = classify_files(commit_files)
            all_patches = code_patches + test_patches

            commits.append(CommitInfo(
                sha=sha,
                message=commit_data.get("message", ""),
                author=author.get("name", ""),
                authored_date=author.get("date", ""),
                parents=[p.get("sha", "") for p in c.get("parents", [])],
                files=all_patches,
            ))

        if not commits:
            return contributions, rejects

        # Collect author contributions
        author_contribs = collect_author_contributions(
            repo=repo,
            commits=commits,
            time_window_days=self.config.author_time_window_days,
            min_commits=self.config.min_commits_per_contribution,
        )

        LOGGER.debug("Found %d author contributions", len(author_contribs))

        # Filter contributions that meet minimum thresholds
        for contrib in author_contribs:
            code_changes = sum(p.additions + p.deletions for p in contrib.code_patches)
            test_changes = sum(p.additions + p.deletions for p in contrib.test_patches)

            if code_changes < self.config.min_code_changes:
                rejects.append(MinerRejectRecord(
                    repo=repo,
                    source_type="author",
                    source_id=f"{contrib.author}:{contrib.contribution_id}",
                    reasons=[f"insufficient code changes ({code_changes} < {self.config.min_code_changes})"],
                ))
                continue

            if test_changes < self.config.min_test_changes:
                rejects.append(MinerRejectRecord(
                    repo=repo,
                    source_type="author",
                    source_id=f"{contrib.author}:{contrib.contribution_id}",
                    reasons=[f"insufficient test changes ({test_changes} < {self.config.min_test_changes})"],
                ))
                continue

            contributions.append(contrib)

        return contributions, rejects


def run_miner(
    client: GitHubClient,
    config: MinerConfig,
    input_path: Path,
    output_path: Path,
    rejects_path: Path,
    contributions_path: Optional[Path] = None,
    progress_path: Optional[Path] = None,
) -> None:
    """
    Run the commit miner on a list of repositories.

    Args:
        client: GitHub API client
        config: Miner configuration
        input_path: Path to repo_cards.jsonl
        output_path: Path to write commit_pairs.jsonl (from PRs)
        rejects_path: Path to write miner_rejects.jsonl
        contributions_path: Path to write author_contributions.jsonl (optional)
        progress_path: Optional path to track progress for resumption
    """
    miner = CommitMiner(client, config)

    # Default contributions path
    if contributions_path is None:
        contributions_path = output_path.parent / "author_contributions.jsonl"

    # Load progress if resuming
    processed_repos: Set[str] = set()
    if progress_path and progress_path.exists():
        with open(progress_path, "r") as f:
            for line in f:
                processed_repos.add(line.strip())
        LOGGER.info("Resuming: %d repos already processed", len(processed_repos))

    # Open output files in append mode if resuming
    mode = "a" if processed_repos else "w"

    with open(output_path, mode) as pairs_f, \
         open(contributions_path, mode) as contribs_f, \
         open(rejects_path, mode) as rej_f, \
         open(input_path, "r") as in_f:

        for line in in_f:
            if not line.strip():
                continue

            try:
                repo_data = json.loads(line)
            except json.JSONDecodeError:
                LOGGER.warning("Invalid JSON line: %s", line[:50])
                continue

            repo = repo_data.get("repo", "")
            if not repo:
                continue

            if repo in processed_repos:
                continue

            try:
                pairs, contributions, rejects = miner.mine_repo(repo)

                # Write commit pairs (from PRs)
                for pair in pairs:
                    pairs_f.write(json.dumps(asdict(pair)) + "\n")

                # Write author contributions
                for contrib in contributions:
                    contribs_f.write(json.dumps(asdict(contrib)) + "\n")

                # Write rejects
                for reject in rejects:
                    rej_f.write(json.dumps(asdict(reject)) + "\n")

                # Flush to ensure data is written
                pairs_f.flush()
                contribs_f.flush()
                rej_f.flush()

                LOGGER.info(
                    "Processed %s: %d pairs, %d contributions, %d rejects",
                    repo, len(pairs), len(contributions), len(rejects)
                )

                # Update progress
                if progress_path:
                    with open(progress_path, "a") as prog_f:
                        prog_f.write(repo + "\n")

            except Exception as e:
                LOGGER.error("Error processing %s: %s", repo, e)
                error_reject = MinerRejectRecord(
                    repo=repo,
                    source_type="repo",
                    source_id="",
                    reasons=[f"processing error: {str(e)}"],
                )
                rej_f.write(json.dumps(asdict(error_reject)) + "\n")
                rej_f.flush()

    LOGGER.info("Mining complete.")
    LOGGER.info("  Commit pairs: %s", output_path)
    LOGGER.info("  Author contributions: %s", contributions_path)
