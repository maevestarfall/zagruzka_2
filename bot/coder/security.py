from __future__ import annotations

import ast
import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)

# Secret scrubbing regex list
SECRET_PATTERNS = [
    re.compile(r"(?i)(api[_-]?key|secret|token|password|passwd|auth_key)[\"']?\s*[:=]\s*[\"']?([a-zA-Z0-9_\-]{16,})[\"']?"),
    re.compile(r"(?i)(Bearer|Basic)\s+([a-zA-Z0-9_\-]{16,})")
]

# Forbidden AST nodes/calls/imports for Skill Install Gate
FORBIDDEN_IMPORTS = {"os", "subprocess", "shutil", "sys", "socket", "ctypes", "pty"}
FORBIDDEN_CALLS = {"eval", "exec", "open", "compile", "globals", "locals"}

class SecurityViolation(Exception):
    """Raised when a security or compliance rule is violated."""
    pass

def scrub_secrets(line: str) -> str:
    """Scrub sensitive API keys, secrets, or tokens from logs at emission time."""
    scrubbed = line
    for pattern in SECRET_PATTERNS:
        # Replace the actual secret match group with [SCRUBBED]
        def repl(match):
            full_match = match.group(0)
            secret = match.group(2)
            return full_match.replace(secret, "[SCRUBBED]")
        scrubbed = pattern.sub(repl, scrubbed)
    return scrubbed

def assert_path_inside_sandbox(run_id: str, target_path: str) -> None:
    """Path-escape protection: verify target_path is strictly isolated within the run's sandbox."""
    sandbox_root = Path(f"coder_runs/{run_id}").resolve()
    target = Path(target_path).resolve()
    
    # Check if target is a descendant of sandbox_root
    try:
        target.relative_to(sandbox_root)
    except ValueError:
        raise SecurityViolation(f"Path escape detected! Target path '{target_path}' is outside sandbox root '{sandbox_root}'")

def validate_skill_ast(code_str: str) -> None:
    """AST Skill-Install Gate: analyze code on a denylist of forbidden imports/calls using ast.parse()."""
    try:
        tree = ast.parse(code_str)
    except Exception as e:
        raise SecurityViolation(f"Failed to parse skill code AST: {e}")
        
    for node in ast.walk(tree):
        # Check imports
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] in FORBIDDEN_IMPORTS:
                    raise SecurityViolation(f"Forbidden import '{alias.name}' detected in skill code.")
        elif isinstance(node, ast.ImportFrom):
            if node.module and node.module.split(".")[0] in FORBIDDEN_IMPORTS:
                raise SecurityViolation(f"Forbidden import from '{node.module}' detected in skill code.")
                
        # Check forbidden function calls
        elif isinstance(node, ast.Call):
            func_name = ""
            if isinstance(node.func, ast.Name):
                func_name = node.func.id
            elif isinstance(node.func, ast.Attribute):
                func_name = node.func.attr
                
            if func_name in FORBIDDEN_CALLS:
                raise SecurityViolation(f"Forbidden function call '{func_name}' detected in skill code.")
