"""
Data models for the new_feature_craft pipeline.

Defines all dataclasses used across phases:
- CraftTask: Final output per module
- ModuleInfo: Candidate module metadata
- ModuleMining: Mining result with validation
- QueryResult: Generated query with score
- ActorResult: Single actor validation run
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class ModuleInfo:
    """Metadata for a candidate module identified by the miner."""
    module_name: str
    file_path: str
    dependent_files: List[str] = field(default_factory=list)
    importance_score: float = 0.0
    reasoning: str = ""
    test_files: List[str] = field(default_factory=list)
    estimated_complexity: str = "medium"  # "low" / "medium" / "high"


@dataclass
class ModuleMining:
    """Result of mining (removing) a module and validating base/target states."""
    module_info: ModuleInfo
    removed_files: List[str] = field(default_factory=list)
    base_state_valid: bool = False       # base tests FAIL (module removed)
    target_state_valid: bool = False     # target tests PASS (module restored)
    module_content: Dict[str, str] = field(default_factory=dict)  # {path: content}


@dataclass
class QueryResult:
    """Generated query for a single module."""
    module_name: str = ""
    query: str = ""
    score: float = 0.0


@dataclass
class ActorResult:
    """Result of a single actor validation run."""
    model_name: str = ""
    run_index: int = 0
    pass_rate: float = 0.0
    tests_passed: int = 0
    tests_total: int = 0
    exit_code: int = -1


@dataclass
class CraftTask:
    """Final output for one mined module."""
    task_id: str = ""
    repo_name: str = ""
    module_mining: Optional[ModuleMining] = None
    query_result: Optional[QueryResult] = None
    actor_validations: List[Dict[str, Any]] = field(default_factory=list)
    run_tests_sh: str = ""
    status: str = "pending"  # "completed" / "failed" / "skipped"


@dataclass
class RepoResult:
    """Aggregated result for a single repository."""
    repo_name: str = ""
    tasks: List[CraftTask] = field(default_factory=list)
    reject_reason: str = ""
    status: str = "pending"  # "completed" / "failed" / "skipped"
