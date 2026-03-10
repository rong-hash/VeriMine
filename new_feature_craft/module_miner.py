"""
Module Miner - Phase 3 of the new_feature_craft pipeline.

Identifies core RTL modules, removes them, and validates that:
  - base state (module removed): tests FAIL
  - target state (module restored): tests PASS

This is the key differentiator from the PR-based pipeline.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from agents.claude_code_executor import ClaudeCodeExecutor, setup_logger
from models import ModuleInfo, ModuleMining
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
        """
        Use LLM to identify candidate modules for mining.

        Returns list of ModuleInfo, sorted by importance_score descending.
        """
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
                timeout=18000,
                max_retries=3,
                must_include_keys=["candidates"],
            )

            if not result or not result.get("candidates"):
                log.error("No candidate modules identified")
                return []

            candidates = []
            for c in result["candidates"][:self.max_modules]:
                info = ModuleInfo(
                    module_name=c.get("module_name", ""),
                    file_path=c.get("file_path", ""),
                    dependent_files=c.get("dependent_files", []),
                    importance_score=float(c.get("importance_score", 0)),
                    reasoning=c.get("reasoning", ""),
                    test_files=c.get("test_files", []),
                    estimated_complexity=c.get("estimated_complexity", "medium"),
                )
                candidates.append(info)

            # Sort by importance
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
    # Phase 3b: Mine a single module (remove → test fail → restore → test pass)
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
        1. Save module files content
        2. Remove module files → base state
        3. Run tests → must FAIL
        4. Restore module files → target state
        5. Run tests → must PASS

        Returns ModuleMining if valid, None otherwise.
        """
        module_dir = output_dir / f"module_{module_info.module_name}"
        module_dir.mkdir(parents=True, exist_ok=True)

        # Save module info
        info_file = module_dir / "module_info.json"
        info_file.write_text(json.dumps({
            "module_name": module_info.module_name,
            "file_path": module_info.file_path,
            "dependent_files": module_info.dependent_files,
            "importance_score": module_info.importance_score,
            "reasoning": module_info.reasoning,
            "test_files": module_info.test_files,
            "estimated_complexity": module_info.estimated_complexity,
        }, indent=2, ensure_ascii=False), encoding="utf-8")

        # Determine files to remove
        files_to_remove = module_info.dependent_files
        if not files_to_remove:
            files_to_remove = [module_info.file_path]

        # Step 1: Save file contents (gold patch)
        module_content = {}
        removed_files_dir = module_dir / "removed_files"
        removed_files_dir.mkdir(parents=True, exist_ok=True)

        for fpath in files_to_remove:
            abs_path = repo_path / fpath
            if abs_path.exists():
                content = abs_path.read_text(encoding="utf-8", errors="replace")
                module_content[fpath] = content
                # Save a copy
                backup_path = removed_files_dir / fpath
                backup_path.parent.mkdir(parents=True, exist_ok=True)
                backup_path.write_text(content, encoding="utf-8")
                log.info(f"Saved: {fpath} ({len(content)} chars)")
            else:
                log.warning(f"File not found: {abs_path}")

        if not module_content:
            log.error(f"No files found for module {module_info.module_name}")
            return None

        mining = ModuleMining(
            module_info=module_info,
            removed_files=list(module_content.keys()),
            module_content=module_content,
        )

        # Step 2: Remove files → base state
        log.info(f"Removing {len(module_content)} files for base state...")
        for fpath in module_content:
            abs_path = repo_path / fpath
            if abs_path.exists():
                abs_path.unlink()
                log.info(f"Removed: {fpath}")

        # Step 3: Run tests on base state → should FAIL
        log.info("Running tests on base state (expecting FAIL)...")
        base_result = await run_test_script(
            repo_path=repo_path,
            run_tests_sh=run_tests_sh,
            timeout=self.test_timeout,
            log=log,
        )

        # Save base test result
        base_result_file = module_dir / "base_test_result.json"
        base_result_file.write_text(
            json.dumps(base_result, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

        if base_result["exit_code"] == 0 and base_result.get("pass_rate", 0) == 1.0:
            log.warning(f"Base state tests PASSED — module removal had no effect! "
                        f"Skipping {module_info.module_name}")
            # Restore files before returning
            self._restore_files(repo_path, module_content, log)
            return None

        mining.base_state_valid = True
        log.info(f"Base state validation OK: exit_code={base_result['exit_code']}, "
                 f"pass_rate={base_result.get('pass_rate', 0)}")

        # Step 4: Restore files → target state
        log.info("Restoring module files for target state...")
        self._restore_files(repo_path, module_content, log)

        # Step 5: Run tests on target state → should PASS
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
            log.warning(f"Target state tests FAILED — module restoration broken! "
                        f"exit_code={target_result['exit_code']}")
            return None

        mining.target_state_valid = True
        log.info(f"Target state validation OK: pass_rate={target_result.get('pass_rate', 0)}")

        return mining

    @staticmethod
    def _restore_files(repo_path: Path, module_content: Dict[str, str], log: logging.Logger):
        """Restore removed module files from saved content."""
        for fpath, content in module_content.items():
            abs_path = repo_path / fpath
            abs_path.parent.mkdir(parents=True, exist_ok=True)
            abs_path.write_text(content, encoding="utf-8")
            log.info(f"Restored: {fpath}")

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
        """
        Full Phase 3: identify candidates → mine each → return valid ones.
        """
        # Identify candidates
        candidates = await self.identify_candidates(
            repo_path, repo_analysis, output_dir, log
        )
        if not candidates:
            log.warning("No candidate modules found")
            return []

        # Mine each candidate
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
                # Ensure files are restored on error
                self._restore_files(
                    repo_path,
                    {f: "" for f in candidate.dependent_files or [candidate.file_path]},
                    log,
                )

        log.info(f"Phase 3 complete: {len(valid_minings)}/{len(candidates)} valid modules")
        return valid_minings
