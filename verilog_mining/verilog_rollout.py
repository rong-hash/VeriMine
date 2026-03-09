"""
Verilog Rollout Orchestration.

Manages sandbox deployment for Verilog task mining.
Sends repos to Agent Office sandbox (eda-sandbox:agent image)
for parallel processing.

Adapted from agent-task-craft/cc_env/cc_rollout.py
Key difference: uses a single shared Docker image (eda-sandbox:agent)
instead of per-repo images like the C++ pipeline.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Environment configurations for Verilog sandbox
ENV_CONFIGS = {
    "eda-sandbox": {
        "image": "eda-sandbox:agent",
        "workdir": "/workspace",
        "registry": "eda-sandbox",
    },
}

# Default input file
DEFAULT_INPUT_FILE = "data/repo_list.jsonl"


def prepare_upload_dir(
    upload_paths: List[str],
    selective_task_ids: Optional[List[str]] = None,
) -> Optional[str]:
    """
    Create a temporary directory with files to upload to workspace.

    Args:
        upload_paths: List of file/directory paths to include
        selective_task_ids: Optional list of task IDs for selective upload

    Returns:
        Path to temporary directory, or None on failure
    """
    if not upload_paths:
        return None

    tmpdir = tempfile.mkdtemp(prefix="verilog_upload_")
    logger.info(f"Creating upload directory: {tmpdir}")

    for path_pattern in upload_paths:
        import glob
        matches = glob.glob(path_pattern)
        if not matches:
            logger.warning(f"No matches for pattern: {path_pattern}")
            continue

        for src in matches:
            src_path = Path(src)
            if src_path.is_dir():
                dst = Path(tmpdir) / src_path.name
                shutil.copytree(src_path, dst, dirs_exist_ok=True)
            else:
                dst = Path(tmpdir) / src_path.name
                shutil.copy2(src_path, dst)
            logger.info(f"  Added: {src}")

    return tmpdir


async def start_rollout(args):
    """
    Launch Verilog task mining rollout.

    Reads repos from input JSONL, creates sandbox tasks,
    and monitors execution.
    """
    from moonpack import RolloutBatch, RolloutBatchConfigs, RolloutTaskConfigs, RolloutTaskInput

    logger.info("=" * 80)
    logger.info("Starting Verilog Task Mining Rollout")
    logger.info("=" * 80)

    env_config = ENV_CONFIGS.get(args.env)
    if not env_config:
        raise ValueError(f"Unknown environment: {args.env}")

    logger.info(f"Environment: {args.env}")
    logger.info(f"Image: {env_config['image']}")
    logger.info(f"Model: {args.model}")
    logger.info(f"Concurrent: {args.concurrent_tasks}")

    # Read input file
    input_file = args.input_file or DEFAULT_INPUT_FILE
    if not os.path.exists(input_file):
        raise RuntimeError(f"Input file not found: {input_file}")

    tasks = []
    with open(input_file) as f:
        for i, line in enumerate(f):
            if line.strip():
                item = json.loads(line)
                if "task_id" not in item:
                    item["task_id"] = f"verilog-{i}"
                tasks.append(item)

    # Apply index slicing
    start = args.start_index or 0
    end = args.end_index or len(tasks)
    tasks = tasks[start:end]

    # Deduplicate by repo
    if args.dedupe_by_repo:
        seen = set()
        deduped = []
        for t in tasks:
            repo = t.get("repo", t.get("full_name", ""))
            if repo not in seen:
                seen.add(repo)
                deduped.append(t)
        logger.info(f"Deduplicated: {len(tasks)} -> {len(deduped)}")
        tasks = deduped

    logger.info(f"Processing {len(tasks)} repos [{start}:{end}]")

    # Upload code
    upload_dir = None
    if not args.skip_upload and args.upload:
        upload_dir = prepare_upload_dir(args.upload)
        if upload_dir:
            logger.info(f"Uploading to workspace...")
            subprocess.run(
                f"eval $(curl -fsSL proxy.msh.work:3128/env --noproxy proxy.msh.work) && "
                f"megfile sync -g -w 8 {upload_dir} "
                f"s3+m2://msh-agent-xgp/workspaces/{args.workspace_id}/{args.task}/code",
                shell=True, check=True,
            )

    # Set up output directory
    if args.output_dir:
        Path(args.output_dir).mkdir(parents=True, exist_ok=True)

    code_dir = f"/mnt/workspace/{args.task}/code"

    # Create batch
    try:
        rollout_batch = await RolloutBatch.create(
            image="fake-image",
            configs=RolloutBatchConfigs(
                env={
                    "ANTHROPIC_MODEL": args.model,
                    "FLASH_MODEL": args.flash_model or "",
                    "MAX_THINKING_TOKENS": str(args.max_thinking_tokens),
                },
                command=["echo"],
                memory="64Gi",
                cpu=None,
                workspace_id=args.workspace_id,
                workspace_mount_path="/mnt/workspace",
            ),
            secrets={
                "ANTHROPIC_API_KEY": os.getenv("QIANXUN_API_KEY"),
                "QIANXUN_API_KEY": os.getenv("QIANXUN_API_KEY"),
                "GITHUB_TOKEN": os.getenv("GITHUB_TOKEN"),
            },
            concurrent_tasks=min(len(tasks), args.concurrent_tasks),
            description=f"{args.task}-verilog",
        )

        # Add tasks
        task_inputs = []
        for task in tasks:
            repo_name = task.get("repo", task.get("full_name", ""))
            task_id = task.get("task_id", "unknown")
            task_output_dir = f"/mnt/workspace/{args.task}/output/{task_id}"

            task_env = {
                "TASK_IMAGE": env_config["image"],
                "TASK_NAME": args.task,
                "TASK_ID": task_id,
                "REPO_NAME": repo_name,
                "REPO_PATH": env_config["workdir"],
                "CODE_DIR": code_dir,
                "OUTPUT_DIR": task_output_dir,
                "USER_CMD": args.cmd,
            }

            task_input = RolloutTaskInput(
                tag=["cc", "verilog"],
                description=f"{args.task}-{task_id}",
                configs=RolloutTaskConfigs(
                    env=task_env,
                    image=env_config["image"],
                    cpu=None,
                    command=[
                        "bash", "-c",
                        f"ls -la /mnt/workspace/ && ls -la {code_dir}/ && "
                        f"bash {code_dir}/entrypoint.sh",
                    ],
                ),
            )
            task_inputs.append(task_input)

        await rollout_batch.add_tasks(tasks=task_inputs)
        await rollout_batch.start()
        logger.info(f"Batch started with {len(task_inputs)} tasks")

        # Wait for completion
        await rollout_batch.wait_finish(timeout=3600 * 24, poll_interval=60)
        logger.info("All tasks completed")

    finally:
        # Cleanup
        if upload_dir and Path(upload_dir).exists():
            shutil.rmtree(upload_dir)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Verilog Rollout Orchestration")
    parser.add_argument("--cmd", required=True)
    parser.add_argument("--task", required=True)
    parser.add_argument("--env", default="eda-sandbox", choices=list(ENV_CONFIGS.keys()))
    parser.add_argument("--input-file", default=None)
    parser.add_argument("--output-dir", default=None)
    parser.add_argument("--workspace-id", default=None)
    parser.add_argument("-c", "--concurrent_tasks", type=int, default=50)
    parser.add_argument("--start-index", type=int, default=None)
    parser.add_argument("--end-index", type=int, default=None)
    parser.add_argument("-m", "--model", default="my-cc-xyz")
    parser.add_argument("--flash-model", default=None)
    parser.add_argument("--max-thinking-tokens", type=int, default=32768)
    parser.add_argument("-u", "--upload", nargs="+", default=None)
    parser.add_argument("--skip-upload", action="store_true")
    parser.add_argument("--dedupe-by-repo", action="store_true")
    parser.add_argument("--early-start-threshold", type=float, default=0.8)
    parser.add_argument("--max-concurrent-batches", type=int, default=3)
    parser.add_argument("--task-id", default=None)

    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(message)s")
    asyncio.run(start_rollout(args))
