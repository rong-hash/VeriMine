from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class MatchEvidence:
    path: str
    line_number: int
    line: str
    pattern: str


@dataclass
class RepoCard:
    repo: str
    default_branch: str
    stars: int
    pushed_at: str
    sv_ratio: float
    sv_file_count: int
    sv_line_count: int
    has_ci: bool
    ci_files: List[str]
    commit_count_last_12m: Optional[int]
    commit_count_last_6m: Optional[int]
    pr_total: int
    issue_total: int
    has_release_or_tags: bool
    open_eda_evidence: List[MatchEvidence] = field(default_factory=list)
    deny_evidence: List[MatchEvidence] = field(default_factory=list)
    candidate_build_cmds: List[str] = field(default_factory=list)
    candidate_test_cmds: List[str] = field(default_factory=list)


@dataclass
class RejectRecord:
    repo: str
    reasons: List[str]


# --- Commit Miner Models ---


@dataclass
class FilePatch:
    """Represents a file change in a commit/PR."""
    path: str
    patch_type: str  # "code" | "test" | "other"
    additions: int
    deletions: int
    patch: Optional[str] = None  # unified diff content


@dataclass
class CommitPair:
    """A pair of commits representing a code+test change."""
    repo: str
    base_sha: str  # base commit (before the change)
    target_sha: str  # target commit (after the change)
    source_type: str  # "pr" | "cluster"
    source_id: str  # PR number or cluster id
    code_patches: List[FilePatch] = field(default_factory=list)
    test_patches: List[FilePatch] = field(default_factory=list)
    validation_status: str = "pending"  # "pending" | "valid" | "invalid"


@dataclass
class PRInfo:
    """Information about a merged pull request."""
    number: int
    title: str
    base_sha: str
    merge_commit_sha: str
    merged_at: str
    author: str
    files: List[FilePatch] = field(default_factory=list)


@dataclass
class CommitInfo:
    """Information about a single commit."""
    sha: str
    message: str
    author: str
    authored_date: str
    parents: List[str] = field(default_factory=list)
    files: List[FilePatch] = field(default_factory=list)


@dataclass
class CommitCluster:
    """A cluster of related commits."""
    cluster_id: str
    repo: str
    commits: List[CommitInfo] = field(default_factory=list)
    base_sha: str = ""  # commit before the cluster
    target_sha: str = ""  # last commit in the cluster
    issue_refs: List[str] = field(default_factory=list)


@dataclass
class MinerRejectRecord:
    """Record of a rejected commit pair candidate."""
    repo: str
    source_type: str
    source_id: str
    reasons: List[str]
