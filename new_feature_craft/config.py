"""
Configuration for the new_feature_craft pipeline.

CraftConfig: All tunable parameters with sensible defaults.
Supports loading from JSON files.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional


@dataclass
class CraftConfig:
    """Configuration for the new_feature_craft pipeline."""

    # Input / output
    repo_list_path: str = "output/repo_list_chip.jsonl"
    output_dir: str = "output/craft"

    # Models
    model: str = ""                         # Main model (analysis / generation)
    flash_model: str = ""                   # Fast model (evaluation)
    actor_models: List[str] = field(default_factory=list)

    # Mining parameters
    modules_per_repo: int = 3               # Max modules to mine per repo
    actor_validation_runs: int = 4          # Runs per actor
    quality_threshold: float = 6.5          # Minimum query quality score

    # Timeouts (seconds)
    test_timeout: int = 10800               # Test execution timeout (3h)
    module_timeout: int = 25200             # Single module processing (7h)
    repo_timeout: int = 126000              # Single repo timeout (35h)

    # Parallelism
    max_concurrent_repos: int = 5

    # Runtime
    run_mode: str = "local"                 # "local" or "remote"
    copy_to_final: str = ""                 # Optional final output dir


def load_config(path: str | Path | None) -> CraftConfig:
    """Load CraftConfig from a JSON file, falling back to defaults."""
    if path is None:
        return CraftConfig()
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    config = CraftConfig()
    for key, value in data.items():
        if hasattr(config, key):
            setattr(config, key, value)
    return config
