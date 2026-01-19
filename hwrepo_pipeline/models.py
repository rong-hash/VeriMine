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
