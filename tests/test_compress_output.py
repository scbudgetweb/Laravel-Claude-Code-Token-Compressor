"""Run with: python3 -m unittest discover tests"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["TOKEN_COMPRESSOR_DIR"] = tempfile.mkdtemp()

import compress_output as co  # noqa: E402

PEST_FAILING = "\n".join(
    ["", "   PASS  Tests\\Unit\\ExampleTest", "  ✓ that true is true", ""]
    + [f"   PASS  Tests\\Feature\\Vacancy{i}Test\n  ✓ it lists vacancies 0.05s\n  ✓ it creates a vacancy 0.07s\n  ✓ it validates input 0.02s" for i in range(40)]
    + [
        "   FAIL  Tests\\Feature\\ApplicationTest",
        "  ⨯ it submits an application 0.11s",
        "  ────────────────────────────────────────────",
        "   FAILED  Tests\\Feature\\ApplicationTest > it submits an application",
        "  Expected response status code [201] but received 500.",
        "",
        "  at tests/Feature/ApplicationTest.php:42",
        "     41▕     $response = $this->post('/applications', $data);",
        "  ➜  42▕     $response->assertCreated();",
        "",
        "  Tests:    1 failed, 121 passed (240 assertions)",
        "  Duration: 4.21s",
    ]
)

COMPOSER_INSTALL = "\n".join(
    ["Installing dependencies from lock file (including require-dev)", "Verifying lock file contents can be installed on current platform.", "Package operations: 110 installs, 0 updates, 0 removals"]
    + [f"  - Downloading vendor{i}/package{i} (v1.{i}.0)" for i in range(110)]
    + [f"  - Installing vendor{i}/package{i} (v1.{i}.0): Extracting archive" for i in range(110)]
    + ["laravel/framework suggests installing ext-ftp (Required to use the Flysystem FTP driver.)"] * 5
    + ["Generating optimized autoload files", "> @php artisan package:discover --ansi", "  INFO  Discovering packages.", "  laravel/sail ....................................... DONE", "82 packages you are using are looking for funding.", "Use the `composer fund` command to find out more!", "No security vulnerability advisories found."]
)

VITE_BUILD = "\n".join(
    ["> build", "> vite build", "", "\x1b[36mvite v5.4.2 \x1b[32mbuilding for production...\x1b[39m", "transforming...", "✓ 214 modules transformed.", "rendering chunks...", "computing gzip size..."]
    + [f"public/build/assets/chunk-{i:04x}a1b2.js   {i * 1.37:.2f} kB │ gzip: {i * 0.4:.2f} kB" for i in range(1, 80)]
    + ["(!) Some chunks are larger than 500 kB after minification.", "✓ built in 3.42s"]
)

ROUTE_LIST = "\n".join(
    f"  GET|HEAD   admin/vacancies/{i} ........................................ admin.vacancies.show{i} › Admin\\VacancyController@show"
    for i in range(60)
) + "\n\n                                                  Showing [60] routes"


class FilterTests(unittest.TestCase):
    def test_small_output_untouched(self):
        self.assertIsNone(co.compress("ls", "a\nb\nc", ""))

    def test_pest_keeps_failure_and_summary_drops_passes(self):
        out, _, label = co.compress("php artisan test", PEST_FAILING, "")
        self.assertEqual(label, "tests")
        self.assertIn("Expected response status code [201] but received 500.", out)
        self.assertIn("ApplicationTest.php:42", out)
        self.assertIn("Tests:    1 failed, 121 passed", out)
        self.assertNotIn("✓ it lists vacancies", out)
        self.assertLess(len(out), len(PEST_FAILING) * 0.3)

    def test_composer_install_keeps_summary(self):
        out, _, label = co.compress("composer install", COMPOSER_INSTALL, "")
        self.assertEqual(label, "install")
        self.assertIn("Package operations: 110 installs", out)
        self.assertIn("No security vulnerability advisories found.", out)
        self.assertNotIn("Extracting archive", out)
        self.assertLess(len(out), len(COMPOSER_INSTALL) * 0.15)

    def test_vite_build_drops_assets_and_ansi(self):
        out, _, label = co.compress("npm run build", VITE_BUILD, "")
        self.assertEqual(label, "build")
        self.assertNotIn("\x1b", out)
        self.assertIn("Some chunks are larger than 500 kB", out)
        self.assertIn("✓ built in 3.42s", out)
        self.assertNotIn("chunk-0001", out)

    def test_route_list_collapses_dot_leaders(self):
        out, _, label = co.compress("php artisan route:list", ROUTE_LIST, "")
        self.assertEqual(label, "artisan")
        self.assertIn("admin/vacancies/7 … admin.vacancies.show7", out)
        self.assertNotIn(".....", out)

    def test_generic_never_alters_content_below_budget(self):
        text = "\n".join(f"line {i} ......... keep my dots" for i in range(100))
        self.assertIsNone(co.compress("cat notes.txt", text, ""))

    def test_huge_output_keeps_head_tail_and_errors(self):
        # Between MAX_CHARS and Claude Code's ~30k cap, where the budget applies.
        lines = [f"[2026-10-07 10:{i // 60:02d}:{i % 60:02d}] local.INFO: request handled id={i}" for i in range(400)]
        lines[200] = "[2026-10-07 10:03:20] local.ERROR: SQLSTATE[42S02]: Base table not found"
        text = "\n".join(lines)
        out, _, label = co.compress("cat storage/logs/laravel.log", text, "", "toolu_test")
        self.assertEqual(label, "generic")
        self.assertLessEqual(len(out), co.MAX_CHARS + 300)
        self.assertIn("id=0", out)
        self.assertIn("id=399", out)
        self.assertIn("Base table not found", out)
        self.assertIn("Full output:", out)
        raw = Path(out.split("Full output: ")[1].split("\n")[0])
        self.assertEqual(raw.read_text(), text)

    def test_progress_bar_carriage_returns(self):
        bar = "\r".join(f"Downloading {p}%" for p in range(0, 101)) + "\n"
        self.assertEqual(co.clean(bar).strip(), "Downloading 100%")


class CompactTests(unittest.TestCase):
    def test_adds_compact_to_plain_test_commands(self):
        for cmd in ["php artisan test", "php artisan test --filter=VacancyTest", "./vendor/bin/pest tests/Feature",
                    "vendor/bin/pest", "cd app && php artisan test --parallel"]:
            self.assertEqual(co.compact_tests(cmd), cmd + " --compact", cmd)

    def test_leaves_other_commands_alone(self):
        for cmd in ["php artisan test --compact", "php artisan test --help", "php artisan test | tail -20",
                    "php artisan test > out.txt", "php artisan test; echo done", "php artisan test && git push",
                    "php artisan tinker", "vendor/bin/phpunit", "echo php artisan test", "php artisan test $(cat args)"]:
            self.assertIsNone(co.compact_tests(cmd), cmd)


class HookTests(unittest.TestCase):
    def run_hook(self, payload, env=None):
        return subprocess.run(
            [sys.executable, str(ROOT / "compress_output.py")],
            input=json.dumps(payload), capture_output=True, text=True,
            env={**os.environ, **(env or {})},
        )

    def payload(self, command, stdout, tool="Bash"):
        return {
            "hook_event_name": "PostToolUse", "tool_name": tool, "tool_use_id": "toolu_x",
            "cwd": "/tmp/app", "tool_input": {"command": command},
            "tool_response": {"stdout": stdout, "stderr": "", "interrupted": False, "isImage": False},
        }

    def test_emits_updated_tool_output(self):
        r = self.run_hook(self.payload("composer install", COMPOSER_INSTALL))
        self.assertEqual(r.returncode, 0)
        out = json.loads(r.stdout)["hookSpecificOutput"]
        self.assertEqual(out["hookEventName"], "PostToolUse")
        self.assertIn("Package operations", out["updatedToolOutput"]["stdout"])
        self.assertFalse(out["updatedToolOutput"]["interrupted"])

    def test_pretooluse_rewrites_test_command(self):
        payload = {"hook_event_name": "PreToolUse", "tool_name": "Bash",
                   "tool_input": {"command": "php artisan test", "description": "Run tests"}}
        out = json.loads(self.run_hook(payload).stdout)["hookSpecificOutput"]
        self.assertEqual(out["updatedInput"], {"command": "php artisan test --compact", "description": "Run tests"})
        self.assertNotIn("permissionDecision", out)

    def test_pretooluse_ignores_everything_else(self):
        payload = {"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {"command": "composer install"}}
        self.assertEqual(self.run_hook(payload).stdout, "")

    def test_failure_event_is_ignored(self):
        payload = {"hook_event_name": "PostToolUseFailure", "tool_name": "Bash",
                   "tool_input": {"command": "composer install"}, "error": "Exit code 1\n" + COMPOSER_INSTALL}
        self.assertEqual(self.run_hook(payload).stdout, "")

    def test_opt_out_comment_and_env(self):
        r = self.run_hook(self.payload("composer install # nocompress", COMPOSER_INSTALL))
        self.assertEqual(r.stdout, "")
        r = self.run_hook(self.payload("composer install", COMPOSER_INSTALL), {"TOKEN_COMPRESSOR": "off"})
        self.assertEqual(r.stdout, "")

    def test_steps_aside_when_claude_code_persisted_the_output(self):
        payload = self.payload("composer install", COMPOSER_INSTALL)
        payload["tool_response"]["persistedOutputPath"] = "/tmp/tool-results/x.txt"
        self.assertEqual(self.run_hook(payload).stdout, "")

    def test_ignores_other_tools_and_bad_input(self):
        self.assertEqual(self.run_hook(self.payload("x", COMPOSER_INSTALL, tool="Read")).stdout, "")
        r = subprocess.run([sys.executable, str(ROOT / "compress_output.py")], input="not json", capture_output=True, text=True)
        self.assertEqual((r.returncode, r.stdout), (0, ""))


if __name__ == "__main__":
    unittest.main()
