# Claude Code Token Compressor

A lightweight Claude Code hook that intercepts verbose CLI commands and truncates their output **before** it reaches the model — reducing token usage without sacrificing code quality.

Built for Laravel projects but works with any codebase.

---

## How it works

Claude Code fires a `PreToolUse` event before every tool call. When this hook detects a verbose Bash command (`ls`, `find`, `git log`, `php artisan route:list`, etc.), it:

1. Runs the command itself with truncation (`| head -N`)
2. Sends the trimmed output back to Claude via stderr
3. Exits with code 2, which blocks the original full command from running

Claude receives the truncated output as context and continues normally — it never sees the full output, so it never burns tokens on it.

```
You ask Claude something
       ↓
Claude decides to run: php artisan route:list
       ↓
PreToolUse hook fires
       ↓
Hook runs: (php artisan route:list) 2>&1 | head -60
       ↓
Hook exits 2 — original command blocked
       ↓
Claude receives: 60 lines + "[trimmed to 60 of 364 lines]"
       ↓
Claude works with the trimmed result and offers to filter further if needed
```

**Real result:** 60 tokens worth of routes instead of 364. Claude understood the context and offered smart filtering options (`--path=vacancies`, `--path=admin`, etc.) rather than blindly working with partial data.

> **Note on the "hook error" label:** Claude Code labels stderr output from exit-2 hooks as a "hook error" in the UI. This looks alarming but is purely cosmetic — it's just how the exit-2 mechanism surfaces output to Claude. The hook is working correctly. This label would disappear if Anthropic fix the `updatedInput` bug (see [Known Issues](#known-issues) below).

---

## File structure

Place these files in your Laravel project:

```
your-laravel-app/
├── .claude/
│   ├── settings.json        ← registers the hook with Claude Code
│   ├── CLAUDE.md            ← tells Claude about your app (see tip below)
│   └── hooks/
│       └── compress_output.py
├── app/
├── vendor/
└── ...
```

---

## Installation

```bash
# From your Laravel project root:
mkdir -p .claude/hooks

# Copy both files into place
cp compress_output.py .claude/hooks/compress_output.py
cp settings.json .claude/settings.json

# Make the script executable
chmod +x .claude/hooks/compress_output.py
```

Restart Claude Code if it's already running — hooks are loaded at session start.

---

## Configuration

Open `.claude/hooks/compress_output.py` and find the `RULES` list near the top:

```python
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
```

Increase or decrease the line limits to suit your workflow. More lines = more context for Claude but more tokens used.

### Commands that are never truncated

The `SKIP_PATTERNS` list protects commands where full output matters:

```python
SKIP_PATTERNS = [
    r'\bphp artisan make:',    # scaffolding output is short and important
    r'\bphp artisan migrate',  # migration output is short and important
    r'\bphp artisan tinker',   # interactive
    r'\bgit (add|commit|push|pull|checkout|branch)\b',
    r'\bchmod\b|\bchown\b',
]
```

Add your own patterns here if Claude needs full output from a specific command.

Commands that already pipe through `head` or `tail` are also skipped automatically — no double-truncation.

---

## Testing

### 1. Test the script directly

```bash
echo '{"tool_name":"Bash","tool_input":{"command":"php artisan route:list"}}' \
  | python3 .claude/hooks/compress_output.py
```

Expected: no stdout output (exit 2 suppresses it), and the trimmed route list printed to stderr.

### 2. Test a skip-list command (should pass through unchanged)

```bash
echo '{"tool_name":"Bash","tool_input":{"command":"php artisan migrate"}}' \
  | python3 .claude/hooks/compress_output.py
echo "Exit code: $?"
```

Expected: exit code 0, no output — command passes through unmodified.

### 3. Test a non-Bash tool (should be ignored)

```bash
echo '{"tool_name":"Read","tool_input":{"file_path":"app/Models/User.php"}}' \
  | python3 .claude/hooks/compress_output.py
echo "Exit code: $?"
```

Expected: exit code 0, no output.

### 4. Verify Claude Code picks it up

Start a Claude Code session and run:
```
/hooks
```
This lists all registered hooks and their status. You should see your `PreToolUse` hook for Bash listed.

### 5. Watch it live

Ask Claude something that triggers a verbose command:
> "List all my routes"

You'll see a `PreToolUse:Bash hook error` message in the output — this is expected and correct (see the note in [How it works](#how-it-works) above). Claude will receive the trimmed output and respond accordingly.

---

## Pair with `.claudeignore`

The hook reduces token usage from *command output*. For even bigger savings, also tell Claude Code to ignore large directories it doesn't need to read:

```
# .claudeignore (place in your project root)
vendor/
node_modules/
storage/logs/
storage/framework/
bootstrap/cache/
public/build/
.git/
*.lock
```

`vendor/` alone in a Laravel app can be enormous — ignoring it is typically the single biggest win.

---

## Pair with `CLAUDE.md`

Tell Claude about your project upfront so it doesn't need to explore:

```markdown
# My App (.claude/CLAUDE.md)

Laravel 11, MySQL, Livewire, Tailwind CSS.

Key directories:
- app/Http/Controllers — controllers
- app/Models — Eloquent models
- resources/views — Blade templates

Don't read vendor/, storage/, or tests/ unless explicitly asked.
```

This reduces exploratory tool calls that would otherwise burn tokens on discovery.

---

## Known issues

### `updatedInput` is currently bugged in Claude Code

The cleaner approach for this hook would be to return modified JSON from the hook script telling Claude Code to run a truncated version of the command. Claude Code supports an `updatedInput` field in the `hookSpecificOutput` JSON response for exactly this purpose.

However, `updatedInput` is currently silently ignored in the VSC extension and CLI ([open GitHub issue](https://github.com/anthropics/claude-code/issues/15897)). The exit-2 workaround used here achieves the same result reliably.

When this bug is fixed upstream, the script can be simplified: instead of running the command itself and exiting 2, it would return the modified command and exit 0. The "hook error" label in the UI would also disappear.

---

## Troubleshooting

**Hook isn't firing at all**
- Check the script is executable: `ls -la .claude/hooks/compress_output.py`
- Verify `settings.json` is valid JSON: `python3 -m json.tool .claude/settings.json`
- Run `/hooks` inside Claude Code to confirm registration
- Restart Claude Code — hooks load at session start

**Command isn't being intercepted**
- Run the direct test above to confirm the script fires
- Check whether your command matches a `SKIP_PATTERNS` entry
- Add `print(command, file=sys.stderr)` temporarily before the rules loop to log what the hook is seeing

**Hook fires but Claude re-runs the command anyway**
- This can happen if Claude decides the "hook error" result is insufficient — try increasing the line limit for that rule so Claude gets more context on the first pass

---

## Requirements

- Python 3 (standard library only — no dependencies to install)
- Claude Code with hooks support

---

## Licence

MIT — do whatever you like with it.