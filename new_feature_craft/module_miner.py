"""
Module Miner - Phase 3 of the new_feature_craft pipeline.

Identifies core RTL modules, removes them (by line ranges), and validates:
  - base state (code removed): tests FAIL
  - target state (code restored): tests PASS

Supports three removal modes:
  1. Whole file deletion (start_line=1, end_line=-1)
  2. Partial file deletion (specific line range within a file)
  3. Cross-file deletion (ranges across multiple files)
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from agents.claude_code_executor import ClaudeCodeExecutor, setup_logger
from models import ModuleInfo, ModuleMining, RemovalRange
from test_setup import run_test_script

logger = logging.getLogger(__name__)


class ModuleMiner:
    """Identifies and validates module mining targets."""

    def __init__(
        self,
        model: str,
        flash_model: str = "",
        test_timeout: int = 300,
        max_modules: int = 3,
    ):
        self.model = model
        self.flash_model = flash_model or model
        self.test_timeout = test_timeout
        self.max_modules = max_modules

        # Load prompt
        prompts_dir = Path(__file__).parent / "config" / "prompts"
        self.identify_prompt = self._load_prompt(prompts_dir / "identify_core_modules.txt")

    @staticmethod
    def _load_prompt(path: Path) -> str:
        if path.exists():
            return path.read_text(encoding="utf-8")
        logger.warning(f"Prompt not found: {path}")
        return ""

    # ------------------------------------------------------------------
    # Phase 3a: Identify candidate modules
    # ------------------------------------------------------------------

    async def identify_candidates(
        self,
        repo_path: Path,
        repo_analysis: Dict,
        output_dir: Path,
        log: logging.Logger,
    ) -> List[ModuleInfo]:
        """Use LLM to identify candidate modules for mining."""
        phase_dir = output_dir / "phase3_module_mining"
        phase_dir.mkdir(parents=True, exist_ok=True)

        prompt = self.identify_prompt.format(
            repo_path=repo_path,
            repo_analysis=json.dumps(repo_analysis, indent=2, ensure_ascii=False),
            max_modules=self.max_modules,
        )

        executor = ClaudeCodeExecutor(
            work_dir=repo_path,
            output_dir=phase_dir,
            model=self.model,
            task_name="identify_modules",
            logger=log,
        )

        try:
            result = await executor.execute_with_json_retry(
                query=prompt,
                timeout=25200,
                max_retries=3,
                must_include_keys=["candidates"],
            )

            if not result or not result.get("candidates"):
                log.error("No candidate modules identified")
                return []

            candidates = []
            for c in result["candidates"][:self.max_modules]:
                # Parse removal_ranges
                ranges = []
                for r in c.get("removal_ranges", []):
                    ranges.append(RemovalRange(
                        file=r.get("file", ""),
                        start_line=int(r.get("start_line", 1)),
                        end_line=int(r.get("end_line", -1)),
                    ))

                # Fallback: if no removal_ranges but has file_path / dependent_files
                if not ranges:
                    fp = c.get("file_path", "")
                    deps = c.get("dependent_files", [])
                    for f in (deps or [fp]):
                        if f:
                            ranges.append(RemovalRange(file=f, start_line=1, end_line=-1))

                info = ModuleInfo(
                    module_name=c.get("module_name", ""),
                    removal_ranges=ranges,
                    importance_score=float(c.get("importance_score", 0)),
                    reasoning=c.get("reasoning", ""),
                    test_files=c.get("test_files", []),
                    estimated_complexity=c.get("estimated_complexity", "medium"),
                )
                candidates.append(info)

            candidates.sort(key=lambda x: x.importance_score, reverse=True)

            log.info(f"Identified {len(candidates)} candidate modules: "
                     f"{[c.module_name for c in candidates]}")

            # Save candidates
            candidates_file = phase_dir / "candidates.json"
            candidates_file.write_text(
                json.dumps(result, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )

            return candidates

        except Exception as e:
            log.error(f"Module identification failed: {e}")
            return []
        finally:
            try:
                await executor.disconnect()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Line-range removal and restoration
    # ------------------------------------------------------------------

    @staticmethod
    def _remove_ranges(
        repo_path: Path,
        ranges: List[RemovalRange],
        log: logging.Logger,
    ) -> List[Dict[str, Any]]:
        """
        Remove specified line ranges from files. Returns list of removed content
        for later restoration.

        Each entry: {"file": path, "start_line": int, "end_line": int,
                      "content": str, "was_whole_file": bool}
        """
        removed = []

        for r in ranges:
            abs_path = repo_path / r.file
            if not abs_path.exists():
                log.warning(f"File not found: {abs_path}")
                continue

            lines = abs_path.read_text(encoding="utf-8", errors="replace").split("\n")
            total_lines = len(lines)

            # Resolve end_line=-1 to actual last line
            start = r.start_line  # 1-based
            end = total_lines if r.end_line == -1 else r.end_line

            # Clamp
            start = max(1, min(start, total_lines))
            end = max(start, min(end, total_lines))

            is_whole_file = (start == 1 and end >= total_lines)

            # Extract content (convert to 0-based indexing)
            removed_lines = lines[start - 1:end]
            content = "\n".join(removed_lines)

            removed.append({
                "file": r.file,
                "start_line": start,
                "end_line": end,
                "content": content,
                "was_whole_file": is_whole_file,
            })

            if is_whole_file:
                # Delete entire file
                abs_path.unlink()
                log.info(f"Removed whole file: {r.file} ({total_lines} lines)")
            else:
                # Remove specific lines, keep the rest
                remaining = lines[:start - 1] + lines[end:]
                abs_path.write_text("\n".join(remaining), encoding="utf-8")
                log.info(f"Removed lines {start}-{end} from {r.file} "
                         f"({end - start + 1} lines)")

        return removed

    @staticmethod
    def _restore_ranges(
        repo_path: Path,
        removed_content: List[Dict[str, Any]],
        log: logging.Logger,
    ):
        """Restore previously removed line ranges."""
        for entry in removed_content:
            abs_path = repo_path / entry["file"]
            content_lines = entry["content"].split("\n")

            if entry["was_whole_file"]:
                # Recreate the file
                abs_path.parent.mkdir(parents=True, exist_ok=True)
                abs_path.write_text(entry["content"], encoding="utf-8")
                log.info(f"Restored whole file: {entry['file']}")
            else:
                if not abs_path.exists():
                    log.warning(f"Cannot restore lines to missing file: {abs_path}")
                    continue
                # Re-insert lines at the original position
                lines = abs_path.read_text(encoding="utf-8", errors="replace").split("\n")
                start = entry["start_line"]  # 1-based
                insert_pos = start - 1  # 0-based
                lines = lines[:insert_pos] + content_lines + lines[insert_pos:]
                abs_path.write_text("\n".join(lines), encoding="utf-8")
                log.info(f"Restored lines {entry['start_line']}-{entry['end_line']} "
                         f"in {entry['file']}")

    # ------------------------------------------------------------------
    # Phase 3b: Mine a single module
    # ------------------------------------------------------------------

    async def mine_module(
        self,
        repo_path: Path,
        module_info: ModuleInfo,
        run_tests_sh: Path,
        output_dir: Path,
        log: logging.Logger,
    ) -> Optional[ModuleMining]:
        """
        Attempt to mine a single module:
        1. Save content of removal ranges
        2. Remove code → base state
        3. Run tests → must FAIL
        4. Restore code → target state
        5. Run tests → must PASS
        """
        module_dir = output_dir / f"module_{module_info.module_name}"
        module_dir.mkdir(parents=True, exist_ok=True)

        # Save module info
        info_file = module_dir / "module_info.json"
        info_file.write_text(json.dumps({
            "module_name": module_info.module_name,
            "removal_ranges": [
                {"file": r.file, "start_line": r.start_line, "end_line": r.end_line}
                for r in module_info.removal_ranges
            ],
            "importance_score": module_info.importance_score,
            "reasoning": module_info.reasoning,
            "test_files": module_info.test_files,
            "estimated_complexity": module_info.estimated_complexity,
        }, indent=2, ensure_ascii=False), encoding="utf-8")

        if not module_info.removal_ranges:
            log.error(f"No removal ranges for module {module_info.module_name}")
            return None

        mining = ModuleMining(module_info=module_info)

        # Step 1: Remove code → base state
        log.info(f"Removing code for {module_info.module_name} "
                 f"({len(module_info.removal_ranges)} ranges)...")
        removed_content = self._remove_ranges(repo_path, module_info.removal_ranges, log)

        if not removed_content:
            log.error(f"No content removed for module {module_info.module_name}")
            return None

        mining.removed_content = removed_content

        # Save removed content
        removed_dir = module_dir / "removed_files"
        removed_dir.mkdir(parents=True, exist_ok=True)
        for entry in removed_content:
            backup_path = removed_dir / f"{entry['file'].replace('/', '_')}_L{entry['start_line']}-L{entry['end_line']}.v"
            backup_path.write_text(entry["content"], encoding="utf-8")

        # Step 2: Run tests on base state → should FAIL
        log.info("Running tests on base state (expecting FAIL)...")
        base_result = await run_test_script(
            repo_path=repo_path,
            run_tests_sh=run_tests_sh,
            timeout=self.test_timeout,
            log=log,
        )

        base_result_file = module_dir / "base_test_result.json"
        base_result_file.write_text(
            json.dumps(base_result, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

        if base_result["exit_code"] == 0 and base_result.get("pass_rate", 0) == 1.0:
            log.warning(f"Base state tests PASSED — removal had no effect! "
                        f"Skipping {module_info.module_name}")
            self._restore_ranges(repo_path, removed_content, log)
            return None

        mining.base_state_valid = True
        log.info(f"Base state validation OK: exit_code={base_result['exit_code']}, "
                 f"pass_rate={base_result.get('pass_rate', 0)}")

        # Step 3: Restore code → target state
        log.info("Restoring code for target state...")
        self._restore_ranges(repo_path, removed_content, log)

        # Step 4: Run tests on target state → should PASS
        log.info("Running tests on target state (expecting PASS)...")
        target_result = await run_test_script(
            repo_path=repo_path,
            run_tests_sh=run_tests_sh,
            timeout=self.test_timeout,
            log=log,
        )

        target_result_file = module_dir / "target_test_result.json"
        target_result_file.write_text(
            json.dumps(target_result, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

        if target_result["exit_code"] != 0:
            log.warning(f"Target state tests FAILED! "
                        f"exit_code={target_result['exit_code']}")
            return None

        mining.target_state_valid = True
        log.info(f"Target state validation OK: pass_rate={target_result.get('pass_rate', 0)}")

        return mining

    # ------------------------------------------------------------------
    # Phase 3: Full mining pipeline for a repo
    # ------------------------------------------------------------------

    async def mine_modules(
        self,
        repo_path: Path,
        repo_analysis: Dict,
        run_tests_sh: Path,
        output_dir: Path,
        log: logging.Logger,
    ) -> List[ModuleMining]:
        """Full Phase 3: identify candidates → mine each → return valid ones."""
        candidates = await self.identify_candidates(
            repo_path, repo_analysis, output_dir, log
        )
        if not candidates:
            log.warning("No candidate modules found")
            return []

        valid_minings = []
        for i, candidate in enumerate(candidates):
            log.info(f"Mining module {i+1}/{len(candidates)}: {candidate.module_name}")
            try:
                mining = await self.mine_module(
                    repo_path=repo_path,
                    module_info=candidate,
                    run_tests_sh=run_tests_sh,
                    output_dir=output_dir,
                    log=log,
                )
                if mining and mining.base_state_valid and mining.target_state_valid:
                    valid_minings.append(mining)
                    log.info(f"Module {candidate.module_name}: VALID mining target")
                else:
                    log.info(f"Module {candidate.module_name}: INVALID mining target")
            except Exception as e:
                log.error(f"Failed to mine {candidate.module_name}: {e}")
                # Ensure code is restored on error
                try:
                    self._restore_ranges(
                        repo_path,
                        [{"file": r.file, "start_line": r.start_line,
                          "end_line": r.end_line, "content": "", "was_whole_file": False}
                         for r in candidate.removal_ranges],
                        log,
                    )
                except Exception:
                    pass

        log.info(f"Phase 3 complete: {len(valid_minings)}/{len(candidates)} valid modules")
        return valid_minings
