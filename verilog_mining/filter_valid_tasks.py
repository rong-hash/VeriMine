"""
Post-hoc task filtering and reporting for Verilog task mining.

Scans task directories, validates completeness and quality,
generates reports, and optionally copies valid tasks to a destination.

Adapted from agent-task-craft/new_feature/filter_valid_tasks.py
"""
from __future__ import annotations

import json
import logging
import shutil
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

# Required files in a valid task directory
REQUIRED_FILES = [
    "task.json",
    "task.md",
    "test.patch",
    "code.patch",
    "run-tests.sh",
]

# Required phase directories
REQUIRED_DIRS = [
    "generate_query",
    "quality_check",
    "organize_tests_unified",
    "test_environment_validation",
    "test_query_validation",
    "commit_test_validation",
]


class TaskFilter:
    """Validates and filters generated task packages."""

    def __init__(
        self,
        min_improvement: float = 0.1,
        min_target_pass_rate: float = 0.8,
        max_base_pass_rate: float = 0.5,
        min_final_score: float = 3.0,
    ):
        self.min_improvement = min_improvement
        self.min_target_pass_rate = min_target_pass_rate
        self.max_base_pass_rate = max_base_pass_rate
        self.min_final_score = min_final_score

    def find_task_dirs(self, search_dir: Path) -> List[Path]:
        """Recursively find all task_* directories."""
        task_dirs = []
        for path in sorted(search_dir.rglob("task_*")):
            if path.is_dir() and (path / "task.json").exists():
                task_dirs.append(path)
        return task_dirs

    def check_task(self, task_dir: Path) -> Dict:
        """
        Validate a single task directory.

        Returns:
            Dict with 'valid', 'reason', 'details'
        """
        result = {
            "task_dir": str(task_dir),
            "valid": False,
            "reason": "",
            "details": {},
        }

        # Check required files
        missing_files = []
        for f in REQUIRED_FILES:
            if not (task_dir / f).exists():
                missing_files.append(f)
            elif f == "test.patch" and (task_dir / f).stat().st_size == 0:
                missing_files.append(f"{f} (empty)")

        if missing_files:
            result["reason"] = f"Missing files: {', '.join(missing_files)}"
            return result

        # Check required directories
        missing_dirs = []
        for d in REQUIRED_DIRS:
            if not (task_dir / d).exists():
                missing_dirs.append(d)

        if missing_dirs:
            result["reason"] = f"Missing directories: {', '.join(missing_dirs)}"
            return result

        # Check run-tests.sh is executable
        run_tests = task_dir / "run-tests.sh"
        if run_tests.exists():
            # Ensure it's executable
            if not os.access(run_tests, os.X_OK):
                run_tests.chmod(0o755)

        # Check validation metrics
        validation_file = task_dir / "commit_test_validation" / "result.json"
        if validation_file.exists():
            try:
                data = json.loads(validation_file.read_text())
                target = data.get("target_commit", {})
                base = data.get("base_commit", {})
                delta = data.get("delta", {})

                target_pass_rate = target.get("pass_rate", 0)
                base_pass_rate = base.get("pass_rate", 0)
                improvement = delta.get("pass_rate_improvement", 0)
                validation_status = data.get("validation", "UNKNOWN")

                result["details"] = {
                    "target_pass_rate": target_pass_rate,
                    "base_pass_rate": base_pass_rate,
                    "improvement": improvement,
                    "validation": validation_status,
                }

                if validation_status == "PATCH_ERROR":
                    result["reason"] = "Patch application error"
                    return result

                if target_pass_rate < self.min_target_pass_rate:
                    result["reason"] = f"Target pass rate too low: {target_pass_rate:.1%}"
                    return result

                if base_pass_rate > self.max_base_pass_rate:
                    result["reason"] = f"Base pass rate too high: {base_pass_rate:.1%}"
                    return result

                if improvement < self.min_improvement:
                    result["reason"] = f"Improvement too low: {improvement:.1%}"
                    return result

            except Exception as e:
                result["reason"] = f"Error reading validation: {e}"
                return result
        else:
            result["reason"] = "No validation results"
            return result

        # All checks passed
        result["valid"] = True
        result["reason"] = "All checks passed"
        return result

    def filter_tasks(self, search_dir: Path) -> Dict:
        """
        Filter all tasks in a directory.

        Returns:
            Dict with 'valid_tasks', 'invalid_tasks', 'summary'
        """
        task_dirs = self.find_task_dirs(search_dir)
        logger.info(f"Found {len(task_dirs)} task directories in {search_dir}")

        valid_tasks = []
        invalid_tasks = []
        failure_reasons = {}

        for task_dir in task_dirs:
            check = self.check_task(task_dir)

            if check["valid"]:
                valid_tasks.append(check)
            else:
                invalid_tasks.append(check)
                reason = check["reason"].split(":")[0]  # Group by reason type
                failure_reasons[reason] = failure_reasons.get(reason, 0) + 1

        summary = {
            "total": len(task_dirs),
            "valid": len(valid_tasks),
            "invalid": len(invalid_tasks),
            "failure_reasons": failure_reasons,
        }

        logger.info(f"Valid: {summary['valid']}/{summary['total']}")
        for reason, count in failure_reasons.items():
            logger.info(f"  {reason}: {count}")

        return {
            "valid_tasks": valid_tasks,
            "invalid_tasks": invalid_tasks,
            "summary": summary,
        }

    def copy_valid_tasks(
        self,
        search_dir: Path,
        dest_dir: Path,
    ) -> int:
        """
        Copy valid tasks to destination directory.

        Returns:
            Number of tasks copied
        """
        dest_dir.mkdir(parents=True, exist_ok=True)

        result = self.filter_tasks(search_dir)
        valid = result["valid_tasks"]

        copied = 0
        for task in valid:
            src = Path(task["task_dir"])
            dst = dest_dir / src.name

            try:
                if dst.exists():
                    shutil.rmtree(dst)
                shutil.copytree(src, dst)
                copied += 1
                logger.info(f"Copied: {src.name}")
            except Exception as e:
                logger.error(f"Failed to copy {src.name}: {e}")

        logger.info(f"Copied {copied}/{len(valid)} valid tasks to {dest_dir}")
        return copied

    def generate_report(self, search_dir: Path, output_file: Path):
        """Generate a JSON report of all tasks."""
        result = self.filter_tasks(search_dir)

        report = {
            "search_dir": str(search_dir),
            "summary": result["summary"],
            "valid_tasks": result["valid_tasks"],
            "invalid_tasks": result["invalid_tasks"],
        }

        output_file.parent.mkdir(parents=True, exist_ok=True)
        with open(output_file, "w") as f:
            json.dump(report, f, indent=2, ensure_ascii=False)

        logger.info(f"Report saved to {output_file}")


# Import os for access check
import os


def main():
    """CLI entry point."""
    import argparse

    parser = argparse.ArgumentParser(description="Filter valid Verilog tasks")
    parser.add_argument("search_dir", help="Directory to search for task_* dirs")
    parser.add_argument("--dest", help="Destination for valid tasks")
    parser.add_argument("--report", help="Output report JSON file")
    parser.add_argument("--min-improvement", type=float, default=0.1)
    parser.add_argument("--min-target-pass-rate", type=float, default=0.8)
    parser.add_argument("--max-base-pass-rate", type=float, default=0.5)

    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(message)s")

    task_filter = TaskFilter(
        min_improvement=args.min_improvement,
        min_target_pass_rate=args.min_target_pass_rate,
        max_base_pass_rate=args.max_base_pass_rate,
    )

    if args.report:
        task_filter.generate_report(Path(args.search_dir), Path(args.report))
    elif args.dest:
        task_filter.copy_valid_tasks(Path(args.search_dir), Path(args.dest))
    else:
        result = task_filter.filter_tasks(Path(args.search_dir))
        print(json.dumps(result["summary"], indent=2))


if __name__ == "__main__":
    main()
