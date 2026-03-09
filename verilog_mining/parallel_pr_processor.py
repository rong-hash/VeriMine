"""
Parallel PR Processor for Verilog task mining.

Manages concurrent processing of multiple PRs with semaphore control
and timeout handling.

Adapted from agent-task-craft/new_feature/parallel_pr_processor.py
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

logger = logging.getLogger(__name__)


class PRStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    TIMEOUT = "timeout"


@dataclass
class PRTask:
    """Tracks a single PR processing task."""
    pr_number: int
    pr_title: str
    pr_info: Dict[str, Any]
    status: PRStatus = PRStatus.PENDING
    result: Optional[Dict] = None
    error: Optional[str] = None
    start_time: float = 0
    end_time: float = 0
    elapsed: float = 0


@dataclass
class ProcessorConfig:
    """Configuration for parallel PR processing."""
    max_concurrent: int = 5
    pr_timeout: int = 5400  # 90 minutes
    output_dir: Path = field(default_factory=lambda: Path("./output"))


class ParallelPRProcessor:
    """
    Processes multiple PRs in parallel with concurrency control.

    Usage:
        processor = ParallelPRProcessor(generator, config)
        results = await processor.process_pr_batch(pr_list)
    """

    def __init__(self, generator, config: ProcessorConfig):
        """
        Args:
            generator: VerilogTaskGenerator instance
            config: Processing configuration
        """
        self.generator = generator
        self.config = config
        self.semaphore = asyncio.Semaphore(config.max_concurrent)
        self.tasks: List[PRTask] = []
        self.logger = setup_logger(
            config.output_dir / "parallel_processor.log", "parallel"
        )

    async def process_pr_batch(self, pr_list: List[Dict]) -> List[Dict]:
        """
        Process a batch of PRs concurrently.

        Args:
            pr_list: List of PR info dicts

        Returns:
            List of result dicts
        """
        self.logger.info(f"Processing {len(pr_list)} PRs (max concurrent: {self.config.max_concurrent})")

        # Create PR tasks
        self.tasks = [
            PRTask(
                pr_number=pr.get("pr_number", pr.get("number", 0)),
                pr_title=pr.get("pr_title", pr.get("title", "")),
                pr_info=pr,
            )
            for pr in pr_list
        ]

        # Launch all tasks concurrently with semaphore
        async_tasks = [
            self._process_single_pr(task) for task in self.tasks
        ]
        await asyncio.gather(*async_tasks, return_exceptions=True)

        # Collect results
        results = []
        for task in self.tasks:
            if task.result:
                results.append(task.result)
            else:
                results.append({
                    "pr_number": task.pr_number,
                    "status": task.status.value,
                    "error": task.error,
                    "final_score": 0,
                })

        # Generate summary
        self._log_summary()

        return results

    async def _process_single_pr(self, task: PRTask):
        """Process a single PR with semaphore and timeout."""
        async with self.semaphore:
            task.status = PRStatus.RUNNING
            task.start_time = time.time()
            self.logger.info(f"Starting PR #{task.pr_number}: {task.pr_title}")

            try:
                result = await asyncio.wait_for(
                    self.generator.generate_task_from_pr(task.pr_info),
                    timeout=self.config.pr_timeout,
                )
                task.result = result
                task.status = PRStatus.COMPLETED
                self.logger.info(
                    f"PR #{task.pr_number} completed: "
                    f"score={result.get('final_score', 0)}"
                )

            except asyncio.TimeoutError:
                task.status = PRStatus.TIMEOUT
                task.error = f"Timed out after {self.config.pr_timeout}s"
                self.logger.error(f"PR #{task.pr_number} timed out")

            except Exception as e:
                task.status = PRStatus.FAILED
                task.error = str(e)
                self.logger.error(f"PR #{task.pr_number} failed: {e}")

            finally:
                task.end_time = time.time()
                task.elapsed = task.end_time - task.start_time

    def _log_summary(self):
        """Log processing summary."""
        completed = sum(1 for t in self.tasks if t.status == PRStatus.COMPLETED)
        failed = sum(1 for t in self.tasks if t.status == PRStatus.FAILED)
        timeout = sum(1 for t in self.tasks if t.status == PRStatus.TIMEOUT)
        total_elapsed = sum(t.elapsed for t in self.tasks)

        self.logger.info("\n=== Processing Summary ===")
        self.logger.info(f"Total PRs: {len(self.tasks)}")
        self.logger.info(f"Completed: {completed}")
        self.logger.info(f"Failed: {failed}")
        self.logger.info(f"Timeout: {timeout}")
        self.logger.info(f"Total elapsed: {total_elapsed:.0f}s")

    def filter_valid_tasks(self) -> List[Dict]:
        """
        Filter completed tasks that pass validation checks.

        Returns:
            List of valid task results
        """
        valid = []
        required_files = ["task.json", "task.md", "test.patch", "run-tests.sh"]
        required_dirs = [
            "generate_query", "quality_check", "organize_tests_unified",
            "test_environment_validation", "test_query_validation",
            "commit_test_validation",
        ]

        for task in self.tasks:
            if task.status != PRStatus.COMPLETED or not task.result:
                continue

            task_dir = Path(task.result.get("task_dir", ""))
            if not task_dir.exists():
                continue

            # Check required files
            missing_files = [f for f in required_files if not (task_dir / f).exists()]
            if missing_files:
                self.logger.warning(
                    f"PR #{task.pr_number}: missing files: {missing_files}"
                )
                continue

            # Check required directories
            missing_dirs = [d for d in required_dirs if not (task_dir / d).exists()]
            if missing_dirs:
                self.logger.warning(
                    f"PR #{task.pr_number}: missing dirs: {missing_dirs}"
                )
                continue

            # Check validation results
            cv = task.result.get("commit_validation", {})
            if cv.get("validation") == "PASS":
                valid.append(task.result)
            else:
                self.logger.warning(
                    f"PR #{task.pr_number}: validation={cv.get('validation', 'UNKNOWN')}"
                )

        self.logger.info(f"Valid tasks: {len(valid)}/{len(self.tasks)}")
        return valid

    def generate_summary(self) -> Dict:
        """Generate processing statistics."""
        stats = {
            "total": len(self.tasks),
            "completed": 0,
            "failed": 0,
            "timeout": 0,
            "valid": 0,
            "tasks": [],
        }

        for task in self.tasks:
            task_info = {
                "pr_number": task.pr_number,
                "pr_title": task.pr_title,
                "status": task.status.value,
                "elapsed": round(task.elapsed, 1),
            }
            if task.result:
                task_info["final_score"] = task.result.get("final_score", 0)
                task_info["validation"] = task.result.get(
                    "commit_validation", {}
                ).get("validation", "UNKNOWN")

            stats["tasks"].append(task_info)

            if task.status == PRStatus.COMPLETED:
                stats["completed"] += 1
            elif task.status == PRStatus.FAILED:
                stats["failed"] += 1
            elif task.status == PRStatus.TIMEOUT:
                stats["timeout"] += 1

        return stats
