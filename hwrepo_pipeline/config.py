from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import List


@dataclass
class PipelineConfig:
    search_languages: List[str] = field(
        default_factory=lambda: ["Verilog", "SystemVerilog"]
    )
    search_qualifiers: str = "fork:false archived:false"
    search_sort: str = "stars"
    search_order: str = "desc"
    max_repos_per_language: int = 500

    pushed_within_days: int = 180
    min_stars: int = 100

    min_sv_ratio: float = 0.30
    min_sv_files: int = 20
    min_sv_lines: int = 3000

    min_pr_total: int = 0
    min_issue_total: int = 50

    min_commit_last_12m: int = 100
    min_commit_last_6m: int = 30

    min_tags: int = 5
    min_releases: int = 1

    allowlist_terms: List[str] = field(
        default_factory=lambda: [
            "iverilog",
            "verilator",
            "yosys",
            "symbiyosys",
            "sby",
            "sv2v",
            "surelog",
            "uhdm",
            "cocotb",
        ]
    )
    denylist_terms: List[str] = field(
        default_factory=lambda: [
            "Synopsys VCS",
            "VCS",
            "xrun",
            "xcelium",
            "questa",
            "modelsim",
            "dc_shell",
            "genus",
            "innovus",
            "primetime",
        ]
    )

    scan_paths: List[str] = field(
        default_factory=lambda: [
            "README.md",
            "README.rst",
            "README.txt",
            "README",
            "CONTRIBUTING.md",
            "CONTRIBUTING",
            "Makefile",
            "Dockerfile",
        ]
    )
    scan_workflows: bool = True
    scan_scripts_dir: bool = True
    max_script_files: int = 20

    use_graphql: bool = True

    verilog_extensions: List[str] = field(
        default_factory=lambda: [".v", ".vh", ".sv", ".svh"]
    )


def load_config(path: str | Path | None) -> PipelineConfig:
    if path is None:
        return PipelineConfig()

    data = json.loads(Path(path).read_text(encoding="utf-8"))
    config = PipelineConfig()

    for key, value in data.items():
        if hasattr(config, key):
            setattr(config, key, value)

    return config
