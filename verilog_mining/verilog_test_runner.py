"""
Verilog test runner: detect simulator, compile, run, and parse output.

This is a new component with no direct C++ equivalent. It handles:
- Simulator detection (cocotb > verilator > iverilog priority)
- Compilation and simulation execution
- Output parsing for cocotb XML, iverilog $display, UVM reports
"""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Simulator detection
# ---------------------------------------------------------------------------

def detect_simulator(repo_path: Path) -> str:
    """
    Detect which simulator/framework to use for a repo.

    Priority: cocotb > verilator > iverilog

    Returns: 'cocotb', 'verilator', 'iverilog', or 'unknown'
    """
    repo_str = str(repo_path)

    # Check for cocotb indicators
    for root, dirs, files in os.walk(repo_path):
        # Skip .git
        dirs[:] = [d for d in dirs if d != ".git"]
        for fname in files:
            fpath = os.path.join(root, fname)
            if fname == "Makefile" or fname == "makefile":
                try:
                    with open(fpath, "r", errors="ignore") as f:
                        content = f.read(8192)
                    if "SIM=" in content or "cocotb" in content.lower():
                        return "cocotb"
                except OSError:
                    pass
            if fname.endswith(".py"):
                try:
                    with open(fpath, "r", errors="ignore") as f:
                        head = f.read(2048)
                    if "import cocotb" in head or "from cocotb" in head:
                        return "cocotb"
                except OSError:
                    pass

    # Check for verilator
    for root, dirs, files in os.walk(repo_path):
        dirs[:] = [d for d in dirs if d != ".git"]
        for fname in files:
            if fname in ("Makefile", "makefile", "CMakeLists.txt"):
                fpath = os.path.join(root, fname)
                try:
                    with open(fpath, "r", errors="ignore") as f:
                        content = f.read(8192)
                    if "verilator" in content.lower():
                        return "verilator"
                except OSError:
                    pass

    # Default: iverilog (most common for simple Verilog projects)
    return "iverilog"


def detect_simulator_from_files(files: List[str]) -> str:
    """
    Detect simulator from a list of file paths (without reading contents).
    """
    for f in files:
        fl = f.lower()
        if "cocotb" in fl:
            return "cocotb"
        if fl.endswith(".py") and ("test_" in fl or "_test.py" in fl):
            return "cocotb"  # Likely cocotb

    for f in files:
        if "verilator" in f.lower():
            return "verilator"

    return "iverilog"


# ---------------------------------------------------------------------------
# Test command generation
# ---------------------------------------------------------------------------

def generate_test_command(
    simulator: str,
    test_files: List[str],
    code_files: List[str],
    repo_path: Path,
) -> str:
    """
    Generate the appropriate test command based on detected simulator.

    Returns a shell command string.
    """
    if simulator == "cocotb":
        # Find Makefile directory
        for tf in test_files:
            makefile_dir = (repo_path / tf).parent
            makefile_path = makefile_dir / "Makefile"
            if makefile_path.exists():
                return f"cd {makefile_dir} && make SIM=icarus"
        # Fallback: try repo root Makefile
        return "make SIM=icarus"

    elif simulator == "verilator":
        # Find Makefile or CMakeLists
        makefile = repo_path / "Makefile"
        cmake = repo_path / "CMakeLists.txt"
        if cmake.exists():
            return "mkdir -p build && cd build && cmake .. && make && ./Vtop"
        elif makefile.exists():
            return "make"
        # Direct verilator command
        src_files = " ".join(code_files[:10])
        return f"verilator --cc --exe --build {src_files} && ./obj_dir/Vtop"

    else:  # iverilog
        # Find testbench files
        tb_files = [f for f in test_files if f.endswith((".v", ".sv"))]
        src_files = [f for f in code_files if f.endswith((".v", ".sv"))]

        if not tb_files:
            return "echo 'No testbench files found' && exit 1"

        all_files = " ".join(tb_files + src_files)
        return f"iverilog -g2012 -o sim.vvp {all_files} && vvp sim.vvp"


# ---------------------------------------------------------------------------
# Test execution
# ---------------------------------------------------------------------------

async def run_tests(
    repo_path: Path,
    run_tests_sh: Optional[Path] = None,
    timeout: int = 300,
    logger: Optional[logging.Logger] = None,
) -> Dict:
    """
    Run tests in a repository.

    Args:
        repo_path: Path to the repository
        run_tests_sh: Path to run-tests.sh script (optional)
        timeout: Timeout in seconds
        logger: Optional logger

    Returns:
        Dict with keys: exit_code, output, passed, failed, total, pass_rate, error
    """
    log = logger or logging.getLogger(__name__)

    result = {
        "exit_code": -1,
        "output": "",
        "passed": 0,
        "failed": 0,
        "total": 0,
        "error": 0,
        "pass_rate": 0.0,
    }

    # Determine what to run
    if run_tests_sh and run_tests_sh.exists():
        cmd = ["bash", str(run_tests_sh)]
    else:
        # Auto-detect
        simulator = detect_simulator(repo_path)
        log.info(f"Auto-detected simulator: {simulator}")
        cmd = ["bash", "-c", generate_test_command(simulator, [], [], repo_path)]

    log.info(f"Running tests: {' '.join(cmd)}")
    log.info(f"Working directory: {repo_path}")

    try:
        proc = subprocess.run(
            cmd,
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=timeout,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        )
        result["exit_code"] = proc.returncode
        result["output"] = proc.stdout + proc.stderr

        log.info(f"Test exit code: {proc.returncode}")
        if proc.stdout:
            log.info(f"stdout (last 500 chars): ...{proc.stdout[-500:]}")
        if proc.stderr:
            log.info(f"stderr (last 500 chars): ...{proc.stderr[-500:]}")

    except subprocess.TimeoutExpired:
        result["exit_code"] = -1
        result["output"] = f"Test execution timed out after {timeout}s"
        result["error"] = 1
        log.error(f"Test timed out after {timeout}s")
        return result
    except Exception as e:
        result["exit_code"] = -1
        result["output"] = str(e)
        result["error"] = 1
        log.error(f"Test execution failed: {e}")
        return result

    # Parse output
    parsed = parse_test_output(result["output"], result["exit_code"], repo_path)
    result.update(parsed)

    return result


# ---------------------------------------------------------------------------
# Output parsing
# ---------------------------------------------------------------------------

def parse_test_output(
    output: str,
    exit_code: int,
    repo_path: Optional[Path] = None,
) -> Dict:
    """
    Parse test output to extract pass/fail counts.

    Tries multiple parsers in priority order:
    1. result.json (standardized output)
    2. cocotb XML (results.xml)
    3. cocotb text format
    4. UVM report format
    5. iverilog $display PASS/FAIL
    6. Fallback: exit code
    """
    # 1. Check for result.json
    if repo_path:
        result_json = repo_path / "result.json"
        if result_json.exists():
            parsed = _parse_result_json(result_json)
            if parsed:
                return parsed

    # 2. Check for cocotb XML results
    if repo_path:
        results_xml = repo_path / "results.xml"
        if results_xml.exists():
            parsed = _parse_cocotb_xml(results_xml)
            if parsed:
                return parsed

    # 3. Try cocotb text format
    parsed = _parse_cocotb_text(output)
    if parsed:
        return parsed

    # 4. Try UVM report format
    parsed = _parse_uvm_report(output)
    if parsed:
        return parsed

    # 5. Try iverilog $display PASS/FAIL
    parsed = _parse_iverilog_display(output)
    if parsed:
        return parsed

    # 6. Fallback: exit code
    return _fallback_exit_code(exit_code)


def _parse_result_json(path: Path) -> Optional[Dict]:
    """Parse standardized result.json file."""
    try:
        with open(path) as f:
            data = json.load(f)

        passed = data.get("passed", data.get("tests_passed", 0))
        failed = data.get("failed", data.get("tests_failed", 0))
        error = data.get("error", data.get("errors", 0))
        total = data.get("total", data.get("tests_total", passed + failed + error))

        if total == 0:
            total = passed + failed + error

        return {
            "passed": passed,
            "failed": failed,
            "error": error,
            "total": total,
            "pass_rate": passed / total if total > 0 else 0.0,
        }
    except Exception:
        return None


def _parse_cocotb_xml(path: Path) -> Optional[Dict]:
    """Parse cocotb results.xml (JUnit format)."""
    try:
        tree = ET.parse(path)
        root = tree.getroot()

        total = 0
        passed = 0
        failed = 0
        error = 0

        for testsuite in root.iter("testsuite"):
            tests = int(testsuite.get("tests", 0))
            failures = int(testsuite.get("failures", 0))
            errors = int(testsuite.get("errors", 0))
            total += tests
            failed += failures
            error += errors

        if total == 0:
            # Try counting individual testcases
            for testcase in root.iter("testcase"):
                total += 1
                if testcase.find("failure") is not None:
                    failed += 1
                elif testcase.find("error") is not None:
                    error += 1

        passed = total - failed - error

        if total > 0:
            return {
                "passed": passed,
                "failed": failed,
                "error": error,
                "total": total,
                "pass_rate": passed / total if total > 0 else 0.0,
            }
    except Exception:
        pass
    return None


def _parse_cocotb_text(output: str) -> Optional[Dict]:
    """
    Parse cocotb text output.

    Looks for patterns like:
    - "X passed, Y failed"
    - "Results: X tests passed, Y tests failed"
    """
    # Pattern: "N passed" and "N failed"
    passed_match = re.search(r"(\d+)\s+passed", output, re.IGNORECASE)
    failed_match = re.search(r"(\d+)\s+failed", output, re.IGNORECASE)

    if passed_match or failed_match:
        passed = int(passed_match.group(1)) if passed_match else 0
        failed = int(failed_match.group(1)) if failed_match else 0
        total = passed + failed
        if total > 0:
            return {
                "passed": passed,
                "failed": failed,
                "error": 0,
                "total": total,
                "pass_rate": passed / total,
            }

    return None


def _parse_uvm_report(output: str) -> Optional[Dict]:
    """
    Parse UVM report output.

    Looks for UVM_ERROR and UVM_FATAL counts.
    """
    # UVM summary line: "UVM_ERROR :    0" / "UVM_FATAL :    0"
    error_match = re.search(r"UVM_ERROR\s*:\s*(\d+)", output)
    fatal_match = re.search(r"UVM_FATAL\s*:\s*(\d+)", output)
    warning_match = re.search(r"UVM_WARNING\s*:\s*(\d+)", output)
    info_match = re.search(r"UVM_INFO\s*:\s*(\d+)", output)

    if error_match or fatal_match:
        errors = int(error_match.group(1)) if error_match else 0
        fatals = int(fatal_match.group(1)) if fatal_match else 0

        total_errors = errors + fatals

        # Check for "Test passed" / "Test failed" in UVM
        if "uvm_test_top passed" in output.lower() or (total_errors == 0):
            return {
                "passed": 1,
                "failed": 0,
                "error": total_errors,
                "total": 1,
                "pass_rate": 1.0 if total_errors == 0 else 0.0,
            }
        else:
            return {
                "passed": 0,
                "failed": 1,
                "error": total_errors,
                "total": 1,
                "pass_rate": 0.0,
            }

    return None


def _parse_iverilog_display(output: str) -> Optional[Dict]:
    """
    Parse iverilog/vvp output looking for PASS/FAIL in $display statements.

    Common patterns:
    - "PASS" / "FAIL" keywords
    - "TEST PASSED" / "TEST FAILED"
    - "All tests passed"
    - "$finish" (successful completion)
    """
    output_upper = output.upper()
    lines = output.split("\n")

    pass_count = 0
    fail_count = 0

    for line in lines:
        line_upper = line.upper().strip()
        # Count explicit PASS/FAIL in test output
        if re.search(r"\bPASS(?:ED)?\b", line_upper) and "PASSWORD" not in line_upper:
            pass_count += 1
        if re.search(r"\bFAIL(?:ED)?\b", line_upper):
            fail_count += 1

    # Check for "all tests passed" pattern
    if re.search(r"ALL\s+TESTS?\s+PASS", output_upper):
        if pass_count == 0:
            pass_count = 1
        return {
            "passed": pass_count,
            "failed": 0,
            "error": 0,
            "total": pass_count,
            "pass_rate": 1.0,
        }

    total = pass_count + fail_count
    if total > 0:
        return {
            "passed": pass_count,
            "failed": fail_count,
            "error": 0,
            "total": total,
            "pass_rate": pass_count / total,
        }

    # Check for $finish (successful completion without explicit PASS/FAIL)
    if "$finish" in output or "finish called" in output.lower():
        return {
            "passed": 1,
            "failed": 0,
            "error": 0,
            "total": 1,
            "pass_rate": 1.0,
        }

    return None


def _fallback_exit_code(exit_code: int) -> Dict:
    """Fallback parser: use exit code to determine pass/fail."""
    if exit_code == 0:
        return {
            "passed": 1,
            "failed": 0,
            "error": 0,
            "total": 1,
            "pass_rate": 1.0,
        }
    else:
        return {
            "passed": 0,
            "failed": 1,
            "error": 0,
            "total": 1,
            "pass_rate": 0.0,
        }


# ---------------------------------------------------------------------------
# Compilation check
# ---------------------------------------------------------------------------

def check_compilation(
    repo_path: Path,
    files: List[str],
    simulator: str = "iverilog",
    timeout: int = 120,
) -> Dict:
    """
    Check if Verilog files compile without errors.

    Returns:
        Dict with 'success', 'output', 'errors'
    """
    verilog_files = [f for f in files if f.endswith((".v", ".sv", ".vh", ".svh"))]
    if not verilog_files:
        return {"success": True, "output": "No Verilog files to compile", "errors": []}

    file_args = " ".join(verilog_files)

    if simulator == "iverilog":
        cmd = f"iverilog -g2012 -o /dev/null {file_args}"
    elif simulator == "verilator":
        cmd = f"verilator --lint-only {file_args}"
    else:
        return {"success": True, "output": "Compilation check not supported", "errors": []}

    try:
        proc = subprocess.run(
            ["bash", "-c", cmd],
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        errors = []
        for line in proc.stderr.split("\n"):
            if "error" in line.lower():
                errors.append(line.strip())

        return {
            "success": proc.returncode == 0,
            "output": proc.stdout + proc.stderr,
            "errors": errors,
        }
    except Exception as e:
        return {
            "success": False,
            "output": str(e),
            "errors": [str(e)],
        }
