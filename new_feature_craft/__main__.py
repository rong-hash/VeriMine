"""
CLI entry point for new_feature_craft.

Usage:
    # Single repo
    python -m new_feature_craft --repo YosysHQ/picorv32 --modules-per-repo 1

    # Batch from repo list
    python -m new_feature_craft --repo-list output/repo_list_chip.jsonl

    # With actor validation
    python -m new_feature_craft --repo YosysHQ/picorv32 \
        --model my-model --actor-models model-a model-b
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from pathlib import Path

# Ensure package directory is on sys.path for bare imports
# (matches verilog_mining convention: scripts run from package dir)
_pkg_dir = str(Path(__file__).resolve().parent)
if _pkg_dir not in sys.path:
    sys.path.insert(0, _pkg_dir)

from config import CraftConfig, load_config
from craft_orchestrator import CraftOrchestrator
from parallel_repo_processor import ParallelRepoProcessor

logger = logging.getLogger(__name__)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="new_feature_craft: Mine core modules from chip repos"
    )

    # Input
    parser.add_argument("--repo", type=str, help="Single repo (owner/repo)")
    parser.add_argument("--repo-list", type=str, help="JSONL file with repo list")
    parser.add_argument("--repo-path", type=str, help="Path to pre-cloned repo")
    parser.add_argument("--config", type=str, help="Config JSON file")

    # Model
    parser.add_argument("--model", type=str, default="", help="Main model")
    parser.add_argument("--flash-model", type=str, default="", help="Fast model")
    parser.add_argument("--actor-models", nargs="+", default=[], help="Actor models")

    # Parameters
    parser.add_argument("--output", type=str, default="output/craft", help="Output dir")
    parser.add_argument("--modules-per-repo", type=int, default=3)
    parser.add_argument("--validation-runs", type=int, default=4)
    parser.add_argument("--quality-threshold", type=float, default=6.5)
    parser.add_argument("--test-timeout", type=int, default=300)
    parser.add_argument("--module-timeout", type=int, default=5400)
    parser.add_argument("--repo-timeout", type=int, default=14400)
    parser.add_argument("--max-concurrent", type=int, default=5)

    # Runtime
    parser.add_argument("--run-mode", choices=["local", "remote"], default="local")
    parser.add_argument("--copy-to-final", type=str, default="")

    return parser.parse_args()


def build_config(args: argparse.Namespace) -> CraftConfig:
    """Build CraftConfig from args, optionally loading from JSON."""
    if args.config:
        config = load_config(args.config)
    else:
        config = CraftConfig()

    # CLI args override config file
    if args.model:
        config.model = args.model
    if args.flash_model:
        config.flash_model = args.flash_model
    if args.actor_models:
        config.actor_models = args.actor_models
    if args.output:
        config.output_dir = args.output
    if args.modules_per_repo:
        config.modules_per_repo = args.modules_per_repo
    if args.validation_runs:
        config.actor_validation_runs = args.validation_runs
    if args.quality_threshold:
        config.quality_threshold = args.quality_threshold
    if args.test_timeout:
        config.test_timeout = args.test_timeout
    if args.module_timeout:
        config.module_timeout = args.module_timeout
    if args.repo_timeout:
        config.repo_timeout = args.repo_timeout
    if args.max_concurrent:
        config.max_concurrent_repos = args.max_concurrent
    if args.run_mode:
        config.run_mode = args.run_mode
    if args.copy_to_final:
        config.copy_to_final = args.copy_to_final

    # Fallback to environment
    if not config.model:
        config.model = os.environ.get("ANTHROPIC_MODEL", "")
    if not config.flash_model:
        config.flash_model = config.model

    return config


async def run_single_repo(repo_name: str, config: CraftConfig, repo_path: str = ""):
    """Run pipeline for a single repo."""
    orchestrator = CraftOrchestrator(
        repo_name=repo_name,
        config=config,
        repo_path=Path(repo_path) if repo_path else None,
    )
    result = await orchestrator.run()

    print(f"\n{'='*60}")
    print(f"Repo: {result.repo_name}")
    print(f"Status: {result.status}")
    if result.reject_reason:
        print(f"Reject reason: {result.reject_reason}")
    print(f"Tasks: {len(result.tasks)}")
    for task in result.tasks:
        print(f"  - {task.task_id}: {task.status}")
        if task.query_result:
            print(f"    Query score: {task.query_result.score:.1f}")
    print(f"{'='*60}\n")

    return result


async def run_batch(config: CraftConfig):
    """Run pipeline for a batch of repos from JSONL file."""
    repo_list_path = Path(config.repo_list_path)
    if not repo_list_path.exists():
        print(f"Error: repo list not found: {repo_list_path}")
        sys.exit(1)

    repo_list = []
    with open(repo_list_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                repo_list.append(json.loads(line))

    print(f"Loaded {len(repo_list)} repos from {repo_list_path}")

    processor = ParallelRepoProcessor(config)
    results = await processor.process_repo_batch(repo_list)

    # Print summary
    summary = processor.generate_summary()
    print(f"\n{'='*60}")
    print(f"Batch Processing Summary")
    print(f"Total: {summary['total']}")
    print(f"Completed: {summary['completed']}")
    print(f"Failed: {summary['failed']}")
    print(f"Timeout: {summary['timeout']}")
    print(f"Skipped: {summary['skipped']}")
    print(f"{'='*60}\n")

    # Save summary
    summary_file = Path(config.output_dir) / "batch_summary.json"
    summary_file.write_text(
        json.dumps(summary, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    return results


def main():
    args = parse_args()
    config = build_config(args)

    if not config.model:
        print("Error: --model or ANTHROPIC_MODEL required")
        sys.exit(1)

    if args.repo:
        asyncio.run(run_single_repo(args.repo, config, args.repo_path or ""))
    elif args.repo_list:
        config.repo_list_path = args.repo_list
        asyncio.run(run_batch(config))
    else:
        print("Error: --repo or --repo-list required")
        sys.exit(1)


if __name__ == "__main__":
    main()
