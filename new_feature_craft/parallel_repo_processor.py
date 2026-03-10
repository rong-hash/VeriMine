"""
Parallel Repo Processor for new_feature_craft.

Manages concurrent processing of multiple repositories with semaphore
control and timeout handling.

Adapted from verilog_mining/parallel_pr_processor.py
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional

from agents.claude_code_executor import setup_logger
from config import CraftConfig
from craft_orchestrator import CraftOrchestrator
from models import RepoResult

logger = logging.getLogger(__name__)


class RepoStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    TIMEOUT = "timeout"
    SKIPPED = "skipped"


@dataclass
class RepoTask:
    """Tracks a single repo processing task."""
    repo_name: str
    status: RepoStatus = RepoStatus.PENDING
    result: Optional[RepoResult] = None
    error: Optional[str] = None
    start_time: float = 0
    end_time: float = 0
    elapsed: float = 0


class ParallelRepoProcessor:
    """
    Processes multiple repositories in parallel with concurrency control.

    Usage:
        processor = ParallelRepoProcessor(config)
        results = await processor.process_repo_batch(repo_list)
    """

    def __init__(self, config: CraftConfig):
        self.config = config
        self.semaphore = asyncio.Semaphore(config.max_concurrent_repos)
        self.tasks: List[RepoTask] = []
        output_dir = Path(config.output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        self.logger = setup_logger(
            output_dir / "parallel_processor.log", "parallel"
        )

    async def process_repo_batch(self, repo_list: List[Dict]) -> List[RepoResult]:
        """
        Process a batch of repos concurrently.

        Args:
            repo_list: List of dicts with at least {"repo_name": "owner/repo"}

        Returns:
            List of RepoResult
        """
        self.logger.info(f"Processing {len(repo_list)} repos "
                         f"(max concurrent: {self.config.max_concurrent_repos})")

        # Create repo tasks
        self.tasks = [
            RepoTask(repo_name=r.get("repo_name", r.get("name", "")))
            for r in repo_list
        ]

        # Launch all tasks with semaphore
        async_tasks = [
            self._process_single_repo(task) for task in self.tasks
        ]
        await asyncio.gather(*async_tasks, return_exceptions=True)

        # Collect results
        results = []
        for task in self.tasks:
            if task.result:
                results.append(task.result)
            else:
                results.append(RepoResult(
                    repo_name=task.repo_name,
                    status=task.status.value,
                    reject_reason=task.error or "Unknown error",
                ))

        self._log_summary()
        return results

    async def _process_single_repo(self, task: RepoTask):
        """Process a single repo with semaphore and timeout."""
        async with self.semaphore:
            task.status = RepoStatus.RUNNING
            task.start_time = time.time()
            self.logger.info(f"Starting: {task.repo_name}")

            try:
                orchestrator = CraftOrchestrator(
                    repo_name=task.repo_name,
                    config=self.config,
                )
                result = await asyncio.wait_for(
                    orchestrator.run(),
                    timeout=self.config.repo_timeout,
                )
                task.result = result

                if result.status == "skipped":
                    task.status = RepoStatus.SKIPPED
                elif result.status == "completed":
                    task.status = RepoStatus.COMPLETED
                else:
                    task.status = RepoStatus.FAILED

                self.logger.info(
                    f"{task.repo_name}: {task.status.value} "
                    f"(tasks={len(result.tasks)})"
                )

            except asyncio.TimeoutError:
                task.status = RepoStatus.TIMEOUT
                task.error = f"Timed out after {self.config.repo_timeout}s"
                self.logger.error(f"{task.repo_name}: TIMEOUT")

            except Exception as e:
                task.status = RepoStatus.FAILED
                task.error = str(e)
                self.logger.error(f"{task.repo_name}: FAILED - {e}")

            finally:
                task.end_time = time.time()
                task.elapsed = task.end_time - task.start_time

    def _log_summary(self):
        """Log processing summary."""
        completed = sum(1 for t in self.tasks if t.status == RepoStatus.COMPLETED)
        failed = sum(1 for t in self.tasks if t.status == RepoStatus.FAILED)
        timeout = sum(1 for t in self.tasks if t.status == RepoStatus.TIMEOUT)
        skipped = sum(1 for t in self.tasks if t.status == RepoStatus.SKIPPED)
        total_elapsed = sum(t.elapsed for t in self.tasks)

        self.logger.info("\n=== Processing Summary ===")
        self.logger.info(f"Total repos: {len(self.tasks)}")
        self.logger.info(f"Completed: {completed}")
        self.logger.info(f"Failed: {failed}")
        self.logger.info(f"Timeout: {timeout}")
        self.logger.info(f"Skipped: {skipped}")
        self.logger.info(f"Total elapsed: {total_elapsed:.0f}s")

    def generate_summary(self) -> Dict:
        """Generate processing statistics."""
        stats: Dict[str, Any] = {
            "total": len(self.tasks),
            "completed": 0,
            "failed": 0,
            "timeout": 0,
            "skipped": 0,
            "repos": [],
        }

        for task in self.tasks:
            repo_info: Dict[str, Any] = {
                "repo_name": task.repo_name,
                "status": task.status.value,
                "elapsed": round(task.elapsed, 1),
            }
            if task.result:
                repo_info["tasks_count"] = len(task.result.tasks)
                repo_info["reject_reason"] = task.result.reject_reason
            if task.error:
                repo_info["error"] = task.error

            stats["repos"].append(repo_info)

            if task.status == RepoStatus.COMPLETED:
                stats["completed"] += 1
            elif task.status == RepoStatus.FAILED:
                stats["failed"] += 1
            elif task.status == RepoStatus.TIMEOUT:
                stats["timeout"] += 1
            elif task.status == RepoStatus.SKIPPED:
                stats["skipped"] += 1

        return stats
