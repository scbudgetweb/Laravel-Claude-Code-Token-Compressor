# Laravel Claude Code Token Compressor

A small, dependency-free hook for [Claude Code](https://code.claude.com) that keeps noisy command output out of Claude's context: test runs, `composer install`, `npm run build`, `artisan` tables and long logs. Errors, failures, warnings and summaries are kept; everything else is removed.

It works with any project, but the filters are tuned for Laravel.

```
composer install         12,935 chars  →    493 chars   (-96%)
php artisan test         only failures and the summary (Pest/Collision --compact)
cat storage/logs/...     22,705 chars  →  6,728 chars   (-70%, the ERROR line in the middle is kept)
```

*Figures are from the end-to-end test in this repo; your output will differ. Run `--stats` to see your own (see [Measuring savings](#measuring-savings)).*

---

## Why this one is safe

Plenty of "save tokens" tricks run your commands in a hook, outside Claude Code's control. This one doesn't:

- **Commands run once, and only through Claude Code itself.** Your permission rules and sandbox apply exactly as normal. The hook never executes anything.
- **Nothing is silently lost.** Removed lines are replaced with a note saying how many were removed. When long output is cut, the full version is saved to a file and its path is given to Claude, so it can read more if it needs to.
- **Exit codes are untouched.**
- **It fails open.** If anything goes wrong inside the hook, Claude simply gets the original output.

---

## How it works

One Python script, registered for two Claude Code hook events.

**1. Before a command runs (`PreToolUse`): compact test output**

`php artisan test` and `vendor/bin/pest` get `--compact` added, so the test runner prints only failures plus the summary instead of one line per passing test. It's a flag the runner already supports (Collision and Pest), so nothing is lost. Claude Code then checks the rewritten command against your permission rules as usual.

Only plain commands are rewritten (an optional leading `cd dir &&` is fine). Anything with pipes, redirects or `;` is left alone.

**2. After a command succeeds (`PostToolUse`): filter what Claude sees**

The command has already run normally. The hook rewrites the output Claude receives:

| Command | What's removed | What's kept |
|---|---|---|
| `php artisan test`, `pest`, `phpunit`, `jest`, `vitest`, `npm test` | Passing-test lines, progress dots | Failures, traces, summary |
| `composer install/update/require`, `npm/yarn/pnpm/bun install` | Per-package download/install lines, progress, deprecation and funding notices | Errors, warnings, summary, security advisories |
| `npm run build`, `vite build` | One line per built asset, "transforming…" | Warnings, errors, "built in" |
| `php artisan …` (e.g. `route:list`) | Dot leaders (`.........`) used to pad columns | Every row |
| Anything else | ANSI colour codes and progress-bar redraws, which change no content | Everything. If it's still over 8,000 chars, the head, the tail and any error/warning lines are kept, with a pointer to the full output |

Output under 1,000 characters is never touched.

**What it deliberately doesn't do**

- **Output from failed commands isn't filtered.** Claude Code doesn't allow hooks to replace it. That's why tests are handled up front with `--compact` instead. Claude Code already caps failed-command output on its own.
- **Very large output (over ~30,000 chars) is left to Claude Code**, which saves it to a file and shows Claude a 2KB preview. That's already smaller than anything this hook would produce.

---

## Requirements

- **Claude Code 2.1.121 or later.** Earlier versions ignore the output-replacement feature this relies on. Tested on 2.1.292. Check yours with `claude --version`. The VS Code extension bundles its own copy, which may be newer than your terminal's.
- **Python 3.8+.** Standard library only, nothing to install.

---

## Installation

From your Laravel project root:

```bash
mkdir -p .claude/hooks
cp /path/to/this/repo/compress_output.py .claude/hooks/
chmod +x .claude/hooks/compress_output.py
```

Then register the hooks in `.claude/settings.json`:

- **No `.claude/settings.json` yet?** Copy `settings.json` from this repo into place.
- **Already have one?** Merge the `PreToolUse` and `PostToolUse` entries from this repo's `settings.json` into your existing `"hooks"` section. Don't overwrite the file, or you'll lose your existing settings.

Restart Claude Code (or open `/hooks` to confirm the hooks are loaded).

**Permission rule tip:** if you've allowed tests with an exact rule like `Bash(php artisan test)`, change it to `Bash(php artisan test:*)`. Otherwise `php artisan test --compact` won't match the rule and you'll be asked to approve it.

### Upgrading from v1

v1 ran your commands itself in a `PreToolUse` hook, which skipped Claude Code's permission checks and could run commands twice. Replace both the script and the hook entries in `.claude/settings.json` with the new ones.

---

## Checking it works

```bash
# Run the test suite
python3 -m unittest discover tests

# Simulate a PreToolUse event by hand
echo '{"hook_event_name":"PreToolUse","tool_name":"Bash","tool_input":{"command":"php artisan test"}}' \
  | python3 .claude/hooks/compress_output.py
# → {"hookSpecificOutput": {..., "updatedInput": {"command": "php artisan test --compact"}}}
```

Then in Claude Code, ask it to run your tests or `composer install`. When something has been filtered, Claude's tool result will contain a `[compressor: …]` note.

---

## Measuring savings

Every time the hook rewrites output it logs the before and after size. To see the totals:

```bash
python3 .claude/hooks/compress_output.py --stats
```

```
filter       runs   chars before   chars after   saved
generic         1         22,705         6,728     70%
install         1         12,935           493     96%
total           2         35,640         7,221     80%

≈ 7,104 tokens kept out of context (at ~4 chars/token).
```

These numbers undercount the real benefit. Everything in Claude's context is re-sent on every later turn of the conversation, so 3,000 tokens saved early in a long session add up to far more than 3,000 tokens processed. It also delays auto-compaction, which summarises earlier context and loses detail.

---

## Configuration

Everything is at the top of `compress_output.py`:

| Setting | Default | Meaning |
|---|---|---|
| `MIN_CHARS` | `1000` | Output shorter than this is never touched |
| `MAX_CHARS` | `8000` | Generic output longer than this is cut to head + tail + errors |
| `MIN_SAVING` | `0.15` | Only rewrite output if it saves at least 15% |
| `FILTERS` | — | Which commands get which filter (first match wins). Add your own. |

**Turning it off**

- For one command: add `# nocompress` to the end of it. You can also tell Claude to do this in your `CLAUDE.md` for commands where you always want full output.
- Entirely: set `TOKEN_COMPRESSOR=off` in the environment.

**Where it writes**

`~/.claude/token-compressor/` holds `stats.jsonl` and `raw/`. `raw/` contains the full output of anything that was cut; files there are deleted after two days. Set `TOKEN_COMPRESSOR_DIR` to change the location.

---

## Bigger wins: Claude Code's built-in features

This hook handles command output. For most projects, these matter as much or more:

**Keep Claude out of directories it doesn't need.** `.claudeignore` is **not** a Claude Code feature, and a `.claudeignore` file has no effect. Use `Read` deny rules in `.claude/settings.json` instead:

```json
{
  "permissions": {
    "deny": [
      "Read(./vendor/**)",
      "Read(./node_modules/**)",
      "Read(./storage/framework/**)",
      "Read(./bootstrap/cache/**)",
      "Read(./public/build/**)"
    ]
  }
}
```

**Keep `CLAUDE.md` short.** It's loaded at the start of every session, so every line costs tokens on every request. Describe the stack, the key directories and your conventions, and nothing else. Put instructions that only matter for certain files into `.claude/rules/` so they load only when relevant.

**Let subagents do the exploring.** When you ask Claude to investigate something across many files, it can hand the search to a subagent (such as Explore). The subagent reads the files in its own context, and only the conclusion comes back to your main conversation.

**Watch your context.** `/context` shows what's taking up space, including MCP servers. Disconnect servers you aren't using. `/compact` summarises the conversation so far when you're switching tasks; `/clear` starts fresh.

---

## Limitations

- Output of failed commands is not filtered, apart from failing tests, which `--compact` handles before the run.
- Filters match on the command text. A test run through a custom script (`composer test`, `make test`) won't be recognised until you add its pattern to `FILTERS`.
- Rewritten output is what Claude sees, so a badly written custom filter could hide something important. The built-in filters only remove lines that match narrow patterns, and always leave a note saying what was removed.

---

## Licence

MIT. Do whatever you like with it.
