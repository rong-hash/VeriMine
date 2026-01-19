"""CLI entry point for the commit miner."""
from __future__ import annotations

import argparse
import logging
import os
from pathlib import Path

from .commit_miner import run_miner
from .config import load_miner_config
from .github_client import GitHubClient


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Mine commit pairs from repositories"
    )
    parser.add_argument(
        "--input",
        default="output/repo_cards.jsonl",
        help="Input JSONL path with repo cards",
    )
    parser.add_argument(
        "--output",
        default="output/commit_pairs.jsonl",
        help="Output JSONL path for commit pairs",
    )
    parser.add_argument(
        "--rejects",
        default="output/miner_rejects.jsonl",
        help="Output JSONL path for rejected candidates",
    )
    parser.add_argument(
        "--progress",
        default=None,
        help="Progress file for resumption (optional)",
    )
    parser.add_argument(
        "--config",
        default=None,
        help="Path to miner config JSON (optional)",
    )
    parser.add_argument(
        "--token",
        default=None,
        help="GitHub token (defaults to GITHUB_TOKEN env)",
    )
    parser.add_argument(
        "--lookback-days",
        type=int,
        default=None,
        help="Number of days to look back (overrides config)",
    )
    parser.add_argument(
        "--max-prs",
        type=int,
        default=None,
        help="Max PRs per repo (overrides config)",
    )
    parser.add_argument(
        "--max-commits",
        type=int,
        default=None,
        help="Max commits per repo (overrides config)",
    )
    parser.add_argument(
        "--no-clusters",
        action="store_true",
        help="Disable commit cluster mining (PR only)",
    )
    parser.add_argument(
        "--no-graphql",
        action="store_true",
        help="Use REST API instead of GraphQL",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        help="Logging level",
    )

    args = parser.parse_args()

    # Setup logging
    logging.basicConfig(
        level=args.log_level,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    # Load config
    config = load_miner_config(args.config)

    # Apply CLI overrides
    if args.lookback_days is not None:
        config.lookback_days = args.lookback_days
    if args.max_prs is not None:
        config.max_prs_per_repo = args.max_prs
    if args.max_commits is not None:
        config.max_commits_per_repo = args.max_commits
    if args.no_clusters:
        config.enable_cluster_mining = False
    if args.no_graphql:
        config.use_graphql = False

    # Setup GitHub client
    env_token = os.environ.get("GITHUB_TOKEN")
    if args.token and env_token and args.token != env_token:
        logging.error("Token mismatch: --token differs from GITHUB_TOKEN")
        raise SystemExit(2)

    token = args.token or env_token
    if not token:
        logging.warning("GITHUB_TOKEN not set; rate limits will be low")

    client = GitHubClient(token=token)

    # Ensure output directory exists
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    rejects_path = Path(args.rejects)
    rejects_path.parent.mkdir(parents=True, exist_ok=True)

    progress_path = Path(args.progress) if args.progress else None

    # Run the miner
    run_miner(
        client=client,
        config=config,
        input_path=Path(args.input),
        output_path=output_path,
        rejects_path=rejects_path,
        progress_path=progress_path,
    )


if __name__ == "__main__":
    main()
