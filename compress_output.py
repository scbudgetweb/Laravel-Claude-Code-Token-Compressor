#!/usr/bin/env python3
"""
Claude Code PreToolUse hook — trims verbose CLI output before it reaches Claude.

Drop this in your project: .claude/hooks/compress_output.py
Make it executable: chmod +x .claude/hooks/compress_output.py

How it works:
  Claude Code fires a PreToolUse event before every Bash command.
  When a verbose command is detected (ls, find, route:list, etc.), this hook:
    1. Runs the command itself with truncation
    2. Sends the trimmed output back to Claude via stderr
    3. Exits with code 2, which blocks the original command from running

  Claude receives the truncated output as context and continues normally.
  The original full command never runs, so Claude never burns tokens on it.

  Note: updatedInput JSON approach is currently bugged in Claude Code — this
  exit-2 method is the reliable workaround.
"""

import json
import sys
import re
import subprocess

# ---------------------------------------------------------------------------
# Config — tweak these line limits to taste
# ---------------------------------------------------------------------------

RULES = [
    # (regex to match command, max lines to keep, label for log)
    (r'\bls\b',                          40,  "directory listing"),
    (r'\bfind\b',                        30,  "find results"),
    (r'\bgit log\b',                     20,  "git log"),
    (r'\bgit diff\b',                   100,  "git diff"),
    (r'\bgit status\b',                  30,  "git status"),
    (r'\bphp artisan route:list\b',      60,  "route list"),
    (r'\bphp artisan\b',                 50,  "artisan output"),
    (r'\bcomposer\b',                    40,  "composer output"),
    (r'\bnpm\b|\byarn\b',                40,  "npm/yarn output"),
    (r'\bcat\b.*\.(log|txt|json)\b',     80,  "large file cat"),
    (r'\bgrep\b',                        50,  "grep results"),
]

# Commands that should never be truncated (they're short by nature or interactive)
SKIP_PATTERNS = [
    r'\bphp artisan make:',    # scaffolding — output is short and important
    r'\bphp artisan migrate',  # migration output is short and important
    r'\bphp artisan tinker',   # interactive
    r'\bgit (add|commit|push|pull|checkout|branch)\b',  # git write commands
    r'\bchmod\b|\bchown\b',
]

# ---------------------------------------------------------------------------
# Main logic
# ---------------------------------------------------------------------------

def main():
    try:
        data = json.load(sys.stdin)
    except json.JSONDecodeError:
        sys.exit(0)

    tool_name = data.get("tool_name", "")

    # We only care about Bash tool calls
    if tool_name != "Bash":
        sys.exit(0)

    command = data.get("tool_input", {}).get("command", "")

    if not command:
        sys.exit(0)

    # Don't double-truncate if someone already piped to head/tail/wc
    if re.search(r'\|\s*(head|tail|wc)\b', command):
        sys.exit(0)

    # Don't truncate commands on the skip list
    for skip in SKIP_PATTERNS:
        if re.search(skip, command):
            sys.exit(0)

    # Check each rule
    for pattern, max_lines, label in RULES:
        if re.search(pattern, command):

            # Run the command ourselves with truncation
            try:
                result = subprocess.run(
                    f"( {command} ) 2>&1 | head -{max_lines}",
                    shell=True,
                    capture_output=True,
                    text=True,
                    timeout=30
                )
                output = result.stdout.strip()
            except subprocess.TimeoutExpired:
                # Command timed out — let it pass through unmodified
                sys.exit(0)
            except Exception:
                # Any other error — let it pass through unmodified
                sys.exit(0)

            # Count total lines to report how much was trimmed
            try:
                count_result = subprocess.run(
                    f"( {command} ) 2>&1 | wc -l",
                    shell=True,
                    capture_output=True,
                    text=True,
                    timeout=30
                )
                total_lines = count_result.stdout.strip()
            except Exception:
                total_lines = "unknown"

            # Send truncated output to Claude via stderr and exit 2.
            # Exit code 2 blocks the original command and feeds stderr
            # directly to Claude as context — so Claude gets the trimmed
            # result without ever seeing the full output.
            print(
                f"[compress_output hook] {label} trimmed to {max_lines} of {total_lines} lines.\n\n"
                f"{output}",
                file=sys.stderr
            )
            sys.exit(2)

    # No rule matched — pass through unchanged
    sys.exit(0)


if __name__ == "__main__":
    main()