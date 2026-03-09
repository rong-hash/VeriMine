"""
Test Organizer for Verilog task mining.

Phase 4: Generate run-tests.sh script using Claude Code.

Analyzes test structure, detects test framework, and generates
a shell script to run the relevant tests.

Adapted from agent-task-craft/new_feature/new_feature_task_generator.py Phase 4.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Dict, List, Optional

from agents.claude_code_executor import ClaudeCodeExecutor

logger = logging.getLogger(__name__)


class TestOrganizer:
    """Generates run-tests.sh scripts for Verilog projects."""

    def __init__(self, model: str = "", flash_model: str = ""):
        self.model = model
        self.flash_model = flash_model or model

        # Load prompt template
        prompts_dir = Path(__file__).parent.parent / "config" / "prompts"
        prompt_file = prompts_dir / "organize_tests_verilog.txt"
        if prompt_file.exists():
            self.prompt_template = prompt_file.read_text(encoding="utf-8")
        else:
            self.prompt_template = self._default_prompt()

    def _default_prompt(self) -> str:
        return """You are analyzing a Verilog/SystemVerilog project to generate a test execution script.

**Repository path:** {repo_path}
**Test files:** {test_files}
**Code files:** {code_files}

Your task:
1. Examine the test files and determine the test framework (cocotb, UVM, VUnit, or raw iverilog testbench)
2. Generate a `run-tests.sh` script that:
   - Compiles and runs all relevant tests
   - Produces clear PASS/FAIL output
   - Returns exit code 0 on all pass, non-zero on any failure
   - Works with open-source tools only (iverilog, verilator, cocotb)
   - Does NOT require proprietary EDA tools (VCS, Questa, Xcelium)

**Detection rules:**
- If Makefile contains `SIM=` or Python files import cocotb → use `make SIM=icarus`
- If Makefile/CMakeLists references verilator → use verilator
- Default: use `iverilog -g2012 -o sim.vvp <files> && vvp sim.vvp`

**Output:** Return JSON:
```json
{{
  "test_framework": "cocotb|uvm|vunit|iverilog",
  "test_command": "the shell command to run tests",
  "build_command": "any build/compile command needed (or empty)",
  "run_tests_sh": "#!/bin/bash\\nset -e\\n... full script content ...",
  "reasoning": "why this approach was chosen"
}}
```
"""

    async def generate_test_script(
        self,
        repo_path: Path,
        test_files: List[str],
        code_files: List[str],
        task_dir: Path,
        log: logging.Logger,
        base_commit: str = "",
        target_commit: str = "",
        query: str = "",
        repo_url: str = "",
    ) -> Optional[Dict]:
        """
        Generate run-tests.sh for a Verilog project.

        Args:
            repo_path: Path to repository
            test_files: List of test file paths
            code_files: List of code file paths
            task_dir: Task output directory
            log: Logger
            base_commit: Base commit SHA
            target_commit: Target commit SHA
            query: User query
            repo_url: Repository URL

        Returns:
            Dict with test_framework, test_command, run_tests_sh, etc.
        """
        output_dir = task_dir / "organize_tests_unified"
        output_dir.mkdir(parents=True, exist_ok=True)

        # Build prompt
        prompt = self.prompt_template.format(
            repo_path=repo_path,
            test_files="\n".join(f"  - {f}" for f in test_files[:30]),
            code_files="\n".join(f"  - {f}" for f in code_files[:30]),
            base_commit=base_commit[:8] if base_commit else "",
            target_commit=target_commit[:8] if target_commit else "",
            query=query,
            repo_url=repo_url,
        )

        executor = ClaudeCodeExecutor(
            work_dir=repo_path,
            output_dir=output_dir,
            model=self.flash_model,
            task_name="organize_tests",
        )

        try:
            result = await executor.execute_with_json_retry(
                query=prompt,
                timeout=3000,
                max_retries=5,
                must_include_keys=["run_tests_sh"],
            )

            if not result:
                log.error("Failed to generate test script")
                return None

            # Extract and save run-tests.sh
            script_content = result.get("run_tests_sh", "")
            if not script_content:
                log.error("Empty run_tests_sh in response")
                return None

            # Write run-tests.sh to task directory
            run_tests_path = task_dir / "run-tests.sh"
            run_tests_path.write_text(script_content, encoding="utf-8")
            run_tests_path.chmod(0o755)
            log.info(f"Generated run-tests.sh ({len(script_content)} chars)")

            # Save result metadata
            (output_dir / "result.json").write_text(
                json.dumps(result, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )

            return result

        except Exception as e:
            log.error(f"Test script generation failed: {e}")
            return None
        finally:
            try:
                await executor.disconnect()
            except Exception:
                pass

    def generate_fallback_script(
        self,
        test_files: List[str],
        code_files: List[str],
        task_dir: Path,
        log: logging.Logger,
    ) -> str:
        """
        Generate a simple fallback run-tests.sh without LLM.

        Uses iverilog as default.
        """
        tb_files = [f for f in test_files if f.endswith((".v", ".sv"))]
        src_files = [f for f in code_files if f.endswith((".v", ".sv"))]

        if not tb_files:
            script = "#!/bin/bash\necho 'No testbench files found'\nexit 1\n"
        else:
            all_files = " ".join(tb_files + src_files)
            script = f"""#!/bin/bash
set -e

echo "=== Compiling with iverilog ==="
iverilog -g2012 -o sim.vvp {all_files}

echo "=== Running simulation ==="
vvp sim.vvp

echo "=== Simulation complete ==="
"""

        run_tests_path = task_dir / "run-tests.sh"
        run_tests_path.write_text(script, encoding="utf-8")
        run_tests_path.chmod(0o755)
        log.info(f"Generated fallback run-tests.sh")

        return script
