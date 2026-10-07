#!/usr/bin/env python3
"""
Laravel Claude Code Token Compressor — hooks for Claude Code's Bash tool.

PreToolUse:  adds --compact to `php artisan test` / `vendor/bin/pest`, so test
             runs only print failures. The rewritten command still goes through
             Claude Code's permission checks and sandbox like any other.
PostToolUse: after a command succeeds, rewrites what Claude *sees*: noise is
             stripped (progress bars, passing tests, install chatter, dot
             leaders) while errors, warnings and summaries are kept. Anything
             still too long is cut to head + tail with important middle lines
             kept, and the untouched output is saved to a file Claude can read.

Commands only ever run once, and only via Claude Code itself. Claude Code does
not let hooks replace the output of failed commands, which is why tests are
handled up front by the PreToolUse rewrite.

The hook fails open: on any error it exits 0 with no output and Claude gets
the original result unchanged.

Usage:
  As a hook:  configured in .claude/settings.json (reads hook JSON on stdin)
  Stats:      python3 .claude/hooks/compress_output.py --stats
"""

import json
import os
import re
import sys
import time
from pathlib import Path

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

# Output shorter than this is passed through untouched.
MIN_CHARS = 1000

# After filtering, anything longer than this is cut to head + tail.
# Above ~30,000 chars Claude Code already saves output to a file and shows
# Claude a 2KB preview, so this hook steps aside; this budget covers the range
# below that, which Claude Code would otherwise put into context in full.
MAX_CHARS = 8000

# Only rewrite the output if we save at least this fraction of it.
MIN_SAVING = 0.15

# Lines in the dropped middle of a long output that are always kept.
IMPORTANT = re.compile(
    r"\b(error|exception|fatal|fail(ed|ure|ing)?|warn(ing)?|denied|"
    r"not found|undefined|cannot|unable|traceback|panic)\b|✗|⨯|✘",
    re.I,
)

# Put "# nocompress" in a command (or set TOKEN_COMPRESSOR=off) to skip it.
OPT_OUT = re.compile(r"#\s*nocompress\b")

DATA_DIR = Path(os.environ.get("TOKEN_COMPRESSOR_DIR", Path.home() / ".claude" / "token-compressor"))
RAW_DIR = DATA_DIR / "raw"
STATS_FILE = DATA_DIR / "stats.jsonl"
RAW_KEEP_SECONDS = 2 * 24 * 3600

# Test commands that get --compact appended. Only plain commands are rewritten
# (an optional leading `cd dir &&` is fine): anything with pipes, redirects,
# command lists or substitutions is left alone, since appending would change
# what it means.
COMPACT_TEST = re.compile(r"^(cd [^\s;&|]+ && )?(php artisan test|(\./)?vendor/bin/pest|pest)(\s[^|;&<>`$#\n]*)?$")

# ---------------------------------------------------------------------------
# Generic cleanup — applied to everything. Only removes rendering artifacts,
# so file contents shown with `cat` etc. are never altered.
# ---------------------------------------------------------------------------

ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07]*\x07|\x1b[=>]")


def clean(text):
    text = ANSI.sub("", text)
    lines = []
    for line in text.split("\n"):
        # A carriage return redraws the line (progress bars): keep the final state.
        if "\r" in line:
            line = line.rstrip("\r").split("\r")[-1]
        lines.append(line.rstrip())
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Command-specific filters. Each takes a list of lines and returns a new list.
# ---------------------------------------------------------------------------

def collapse_repeats(lines):
    """Replace runs of 3+ identical lines with one copy and a count."""
    out, i = [], 0
    while i < len(lines):
        j = i
        while j + 1 < len(lines) and lines[j + 1] == lines[i]:
            j += 1
        out.append(lines[i])
        if j - i >= 2:
            out.append(f"  [… previous line repeated {j - i} more times]")
            i = j + 1
        else:
            i += 1
    return out


def squeeze_blanks(lines):
    out = []
    for line in lines:
        if line.strip() or (out and out[-1].strip()):
            out.append(line)
    return out


def drop(lines, pattern, label):
    """Remove lines matching pattern, leaving a one-line note of how many."""
    kept = [l for l in lines if not pattern.search(l)]
    n = len(lines) - len(kept)
    if n:
        kept.append(f"[compressor: {n} {label} lines removed]")
    return kept


# Pest / PHPUnit / artisan test / Jest / Vitest
TEST_PASS = re.compile(
    r"^\s*(✓|✔|√|PASS\b|\[PASS\]|ok \d)"          # passing test lines
    r"|^\s*[.SIRW]{3,}\s*(\d+\s*/\s*\d+\s*\(\s*\d+%\))?\s*$"  # PHPUnit progress dots
)


def filter_tests(lines):
    return squeeze_blanks(drop(lines, TEST_PASS, "passing-test"))


# composer / npm / yarn / pnpm / bun install-type commands
INSTALL_NOISE = re.compile(
    r"^\s*-\s+(Installing|Downloading|Upgrading|Downgrading|Updating|Removing|Locking|Syncing)\b"
    r"|^\s*\d+/\d+\s*\[[=>\-. ]*\]"                # composer progress
    r"|^\s*(npm (WARN|warn) deprecated|warning .* deprecated)"
    r"|^\s*(Progress|Resolving|Fetching|Linking|Building fresh packages)"
    r"|^\s*[@\w./-]+ (suggests|is suggesting)\b"
    r"|^\s*\d+ packages? you are using (is|are) looking for funding"
    r"|^\s*(Run `npm fund`|  run `npm fund`)"
)


def filter_install(lines):
    return squeeze_blanks(collapse_repeats(drop(lines, INSTALL_NOISE, "install-progress")))


# Vite / webpack / mix build output: one line per emitted asset.
BUILD_ASSET = re.compile(r"^\s*\S+\.(js|css|map|json|svg|png|jpe?g|woff2?|ttf|ico|webp)\s+[\d.,]+\s*(kB|KiB|B|MB|bytes)\b", re.I)
BUILD_NOISE = re.compile(r"^\s*(transforming|rendering chunks|computing gzip size)", re.I)


def filter_build(lines):
    lines = drop(lines, BUILD_ASSET, "built-asset")
    return squeeze_blanks(drop(lines, BUILD_NOISE, "build-progress"))


# `php artisan route:list`, `about`, `schedule:list` etc. pad columns with dots.
DOT_LEADER = re.compile(r"\s*\.{4,}\s*")


def filter_artisan(lines):
    return squeeze_blanks([DOT_LEADER.sub(" … ", l) for l in lines])


# Order matters: first match wins. (regex on the command, filter, label)
FILTERS = [
    (r"\b(artisan test|pest|phpunit|paratest|jest|vitest)\b|\b(npm|yarn|pnpm|bun) (run )?test\b", filter_tests, "tests"),
    (r"\bcomposer (install|update|require|remove|upgrade|i|u)\b|\b(npm|yarn|pnpm|bun) (install|ci|add|i|update|upgrade|remove)\b|^\s*(yarn|pnpm|bun)\s*$", filter_install, "install"),
    (r"\b(vite|webpack|mix)\b.*\bbuild\b|\bvite build\b|\b(npm|yarn|pnpm|bun) (run )?(build|prod|production|dev)\b", filter_build, "build"),
    (r"\bartisan\b", filter_artisan, "artisan"),
]

# ---------------------------------------------------------------------------
# Budget: keep head + tail, plus important lines from the middle.
# ---------------------------------------------------------------------------

def fit(text, budget, raw_path):
    if len(text) <= budget:
        return text
    lines = text.split("\n")
    head_budget, tail_budget = int(budget * 0.3), int(budget * 0.5)
    mid_budget = budget - head_budget - tail_budget

    head, used = [], 0
    for line in lines:
        if used + len(line) > head_budget:
            break
        head.append(line)
        used += len(line) + 1

    tail, used = [], 0
    for line in reversed(lines[len(head):]):
        if used + len(line) > tail_budget:
            break
        tail.insert(0, line)
        used += len(line) + 1

    middle = lines[len(head):len(lines) - len(tail)]
    important, used = [], 0
    for line in middle:
        if IMPORTANT.search(line) and used + len(line) <= mid_budget:
            important.append(line)
            used += len(line) + 1

    where = f" Full output: {raw_path}" if raw_path else ""
    note = f"[compressor: {len(middle)} lines omitted here"
    note += f", {len(important)} error/warning lines kept below]" if important else "]"
    return "\n".join(head + [note + where] + important + (["[…]"] if important else []) + tail)


# ---------------------------------------------------------------------------
# Hook entry point
# ---------------------------------------------------------------------------

def save_raw(tool_use_id, stdout, stderr):
    try:
        RAW_DIR.mkdir(parents=True, exist_ok=True)
        cutoff = time.time() - RAW_KEEP_SECONDS
        for old in RAW_DIR.glob("*.log"):
            if old.stat().st_mtime < cutoff:
                old.unlink(missing_ok=True)
        safe_id = re.sub(r"[^\w-]", "", tool_use_id or str(time.time_ns()))
        path = RAW_DIR / f"{safe_id}.log"
        path.write_text(stdout + ("\n--- stderr ---\n" + stderr if stderr else ""))
        return str(path)
    except OSError:
        return None


def log_stats(data, label, before, after):
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with STATS_FILE.open("a") as f:
            f.write(json.dumps({
                "ts": int(time.time()),
                "project": data.get("cwd", ""),
                "filter": label,
                "command": data.get("tool_input", {}).get("command", "")[:200],
                "before": before,
                "after": after,
            }) + "\n")
    except OSError:
        pass


def compress(command, stdout, stderr, tool_use_id=None):
    """Return (new_stdout, new_stderr, label), or None to leave output untouched."""
    before = len(stdout) + len(stderr)
    if before < MIN_CHARS:
        return None

    out, err = clean(stdout), clean(stderr)
    label = "generic"
    for pattern, fn, name in FILTERS:
        if re.search(pattern, command):
            out = "\n".join(fn(out.split("\n"))) if out else out
            err = "\n".join(fn(err.split("\n"))) if err else err
            label = name
            break
    if len(out) + len(err) > MAX_CHARS:
        raw_path = save_raw(tool_use_id, stdout, stderr)
        # Errors usually matter more than stdout noise: give stderr its share first.
        err_share = min(len(err), MAX_CHARS // 3)
        err = fit(err, max(err_share, 500), raw_path)
        out = fit(out, max(MAX_CHARS - len(err), 500), raw_path)

    after = len(out) + len(err)
    if before - after < before * MIN_SAVING:
        return None
    return out, err, label


def compact_tests(command):
    """Return the command with --compact added, or None to leave it alone."""
    command = command.strip()
    if not COMPACT_TEST.match(command) or re.search(r"\s--(compact|help)\b|\s-h\b", command):
        return None
    return command + " --compact"


def main():
    if os.environ.get("TOKEN_COMPRESSOR", "").lower() in ("off", "0", "false"):
        return
    data = json.load(sys.stdin)
    if data.get("tool_name") != "Bash":
        return
    tool_input = data.get("tool_input", {})
    command = tool_input.get("command", "")
    if OPT_OUT.search(command):
        return

    if data.get("hook_event_name") == "PreToolUse":
        rewritten = compact_tests(command)
        if rewritten:
            # No permissionDecision: Claude Code checks the rewritten command
            # against your permission rules exactly as it would any other.
            print(json.dumps({
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "updatedInput": {**tool_input, "command": rewritten},
                }
            }))
        return

    response = data.get("tool_response")
    if not isinstance(response, dict) or response.get("isImage"):
        return
    if response.get("persistedOutputPath"):
        return  # too big for context anyway: Claude Code shows a 2KB preview

    stdout, stderr = response.get("stdout") or "", response.get("stderr") or ""
    result = compress(command, stdout, stderr, data.get("tool_use_id"))
    if result is None:
        return
    out, err, label = result
    log_stats(data, label, len(stdout) + len(stderr), len(out) + len(err))

    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": data.get("hook_event_name", "PostToolUse"),
            "updatedToolOutput": {**response, "stdout": out, "stderr": err},
        }
    }))


def stats():
    if not STATS_FILE.exists():
        print("No stats yet.")
        return
    rows = [json.loads(l) for l in STATS_FILE.read_text().splitlines() if l.strip()]
    by = {}
    for r in rows:
        b = by.setdefault(r["filter"], [0, 0, 0])
        b[0] += 1
        b[1] += r["before"]
        b[2] += r["after"]
    total_before = sum(b[1] for b in by.values())
    total_after = sum(b[2] for b in by.values())
    print(f"{'filter':<10} {'runs':>6} {'chars before':>14} {'chars after':>13} {'saved':>7}")
    for name, (n, before, after) in sorted(by.items(), key=lambda kv: kv[1][1] - kv[1][2], reverse=True):
        print(f"{name:<10} {n:>6} {before:>14,} {after:>13,} {1 - after / before:>7.0%}")
    if total_before:
        print(f"{'total':<10} {len(rows):>6} {total_before:>14,} {total_after:>13,} {1 - total_after / total_before:>7.0%}")
        print(f"\n≈ {(total_before - total_after) // 4:,} tokens kept out of context (at ~4 chars/token).")


if __name__ == "__main__":
    if "--stats" in sys.argv:
        stats()
        sys.exit(0)
    try:
        main()
    except Exception:
        pass  # fail open: Claude gets the original output
    sys.exit(0)
