"""
File classification for Verilog/SystemVerilog code and tests.

Adapted from VeriMine's hwrepo_pipeline/diff_classifier.py with extensions
for cocotb Python test files and build/config file detection.
"""
from __future__ import annotations

import re
from pathlib import PurePosixPath
from typing import Dict, List, Optional, Tuple


# ---------------------------------------------------------------------------
# Verilog/SystemVerilog source extensions
# ---------------------------------------------------------------------------
VERILOG_EXTENSIONS = {".v", ".vh", ".sv", ".svh"}

# Additional HDL-adjacent extensions (treated as "code" when NOT in test dirs)
HDL_ADJACENT_EXTENSIONS = {".vhd", ".vhdl"}  # VHDL

# ---------------------------------------------------------------------------
# Test file patterns (filename-based)
# ---------------------------------------------------------------------------
TEST_FILE_PATTERNS = [
    "*_tb.sv", "*_tb.v",
    "tb_*.sv", "tb_*.v",
    "*_test.sv", "*_test.v",
    "*_tb_*.sv", "*_tb_*.v",
    "*testbench*.sv", "*testbench*.v",
]

# Cocotb / Python test files
COCOTB_TEST_PATTERNS = [
    "test_*.py",       # cocotb convention
    "*_test.py",       # alternate
    "cocotb_*.py",     # explicit cocotb prefix
]

# ---------------------------------------------------------------------------
# Test directory patterns
# ---------------------------------------------------------------------------
TEST_DIR_PATTERNS = [
    "tb", "test", "tests", "testbench", "testbenches",
    "verif", "verification", "bench",
    "dv",       # design verification
    "uvm",      # UVM testbenches
    "cocotb",   # cocotb tests
]

# ---------------------------------------------------------------------------
# Build / config file patterns (treated as "other")
# ---------------------------------------------------------------------------
BUILD_FILE_PATTERNS = [
    "Makefile", "makefile", "GNUmakefile",
    "CMakeLists.txt",
    "*.tcl", "*.do", "*.f", "*.xdc", "*.sdc", "*.qsf", "*.ucf",
]

# Files that should always be classified as "other"
OTHER_EXTENSIONS = {
    ".md", ".txt", ".rst", ".json", ".yaml", ".yml", ".toml",
    ".cfg", ".ini", ".gitignore", ".dockerignore",
    ".png", ".jpg", ".svg", ".pdf",
    ".mem", ".hex", ".bin",
}


# ---------------------------------------------------------------------------
# Classification helpers
# ---------------------------------------------------------------------------

def is_verilog_file(path: str) -> bool:
    """Check if a file is a Verilog/SystemVerilog source file."""
    suffix = PurePosixPath(path).suffix.lower()
    return suffix in VERILOG_EXTENSIONS


def is_hdl_file(path: str) -> bool:
    """Check if a file is any HDL file (Verilog/SV/VHDL)."""
    suffix = PurePosixPath(path).suffix.lower()
    return suffix in VERILOG_EXTENSIONS or suffix in HDL_ADJACENT_EXTENSIONS


def is_cocotb_test(path: str) -> bool:
    """Check if a file is a cocotb/Python test file."""
    name = PurePosixPath(path).name.lower()
    if not name.endswith(".py"):
        return False
    # Must be in a test-like directory or match cocotb naming
    path_lower = path.lower()
    if any(f"/{d}/" in path_lower or path_lower.startswith(f"{d}/") for d in TEST_DIR_PATTERNS):
        return True
    if name.startswith("test_") or name.endswith("_test.py") or name.startswith("cocotb_"):
        return True
    return False


def is_test_file(path: str) -> bool:
    """
    Check if a file is a test file based on path patterns.

    Covers:
    - Verilog testbench files (*_tb.v, tb_*.sv, etc.)
    - Cocotb Python tests (test_*.py in test dirs)
    - Files in test directories (test/, tb/, verif/, dv/, uvm/, cocotb/)
    """
    path_lower = path.lower()

    # Check for test/tb keywords in path
    if "test" in path_lower or "/tb/" in path_lower or "/tb_" in path_lower:
        return True
    if "_tb/" in path_lower or "_tb." in path_lower:
        return True

    # Verification directories
    if "/verif/" in path_lower or "/dv/" in path_lower or "/uvm/" in path_lower:
        return True
    if "/cocotb/" in path_lower or "/bench/" in path_lower:
        return True

    # Cocotb Python test files
    if is_cocotb_test(path):
        return True

    # Check filename patterns for testbenches
    name = PurePosixPath(path).name.lower()
    if name.startswith("tb_") or "_tb." in name or "_tb_" in name:
        return True
    if "testbench" in name:
        return True

    return False


def classify_file(path: str) -> str:
    """
    Classify a file as 'code', 'test', or 'other'.

    Returns:
        'test'  - Test/testbench file (any file type in test paths)
        'code'  - Verilog/SV source file (non-test)
        'other' - Non-Verilog, non-test file
    """
    # First check if it's a test file (any file type)
    if is_test_file(path):
        return "test"

    # Then check if it's Verilog/SV code
    if is_verilog_file(path):
        return "code"

    # Check for "other" extensions
    suffix = PurePosixPath(path).suffix.lower()
    if suffix in OTHER_EXTENSIONS:
        return "other"

    return "other"


def classify_files(files: List[Dict]) -> Tuple[List[Dict], List[Dict], List[Dict]]:
    """
    Classify a list of file changes.

    Args:
        files: List of file dicts with at least 'filename' key

    Returns:
        Tuple of (code_files, test_files, other_files)
    """
    code_files: List[Dict] = []
    test_files: List[Dict] = []
    other_files: List[Dict] = []

    for f in files:
        path = f.get("filename") or f.get("path", "")
        file_type = classify_file(path)
        f_copy = dict(f)
        f_copy["classification"] = file_type

        if file_type == "code":
            code_files.append(f_copy)
        elif file_type == "test":
            test_files.append(f_copy)
        else:
            other_files.append(f_copy)

    return code_files, test_files, other_files


def has_code_and_test_changes(files: List[str]) -> Dict:
    """
    Check if a file list contains both code and test changes.

    Args:
        files: List of file paths

    Returns:
        Dict with 'has_both', 'code_files', 'test_files'
    """
    code_files = []
    test_files = []

    for path in files:
        file_type = classify_file(path)
        if file_type == "code":
            code_files.append(path)
        elif file_type == "test":
            test_files.append(path)

    return {
        "has_both": len(code_files) > 0 and len(test_files) > 0,
        "code_files": code_files,
        "test_files": test_files,
    }


def detect_test_framework(files: List[str], repo_path: Optional[str] = None) -> str:
    """
    Detect which test framework a repo uses based on file patterns.

    Returns one of: 'cocotb', 'uvm', 'vunit', 'verilator', 'iverilog', 'unknown'
    """
    files_lower = [f.lower() for f in files]

    # Check for cocotb
    for f in files_lower:
        if "cocotb" in f or (f.endswith(".py") and is_test_file(f)):
            return "cocotb"

    # Check for UVM
    for f in files_lower:
        if "/uvm/" in f or "_uvm" in f or "uvm_" in f:
            return "uvm"

    # Check for VUnit
    for f in files_lower:
        if "vunit" in f:
            return "vunit"

    # Check for verilator
    for f in files_lower:
        if "verilator" in f or f.endswith(".cpp") and is_test_file(f):
            return "verilator"

    # Default: assume iverilog for Verilog testbenches
    for f in files_lower:
        if is_verilog_file(f) and is_test_file(f):
            return "iverilog"

    return "unknown"


def detect_build_tool(files: List[str]) -> str:
    """
    Detect build tool from file list.

    Returns one of: 'make', 'cmake', 'fusesoc', 'none'
    """
    files_lower = [PurePosixPath(f).name.lower() for f in files]

    if "cmakelists.txt" in files_lower:
        return "cmake"

    for name in files_lower:
        if name in ("makefile", "gnumakefile") or name.endswith(".mk"):
            return "make"

    for f in files:
        if "fusesoc" in f.lower() or f.endswith(".core"):
            return "fusesoc"

    return "none"
