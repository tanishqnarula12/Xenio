"""V6 core: roles and cost policy, the task graph (validation, dependencies, parallelism, blocking), deterministic
verification (real pytest runs in disposable projects, output parsing, false-success prevention, sandbox) and the
deterministic security scan."""

import sys
from pathlib import Path

import pytest

from app.engineering import security as S
from app.engineering import verify as V
from app.engineering.graph import PlanError, TaskGraph, in_scope, normalize_scope, scopes_overlap
from app.engineering.roles import FORBIDDEN_TOOLS, RoleError, RoleRegistry

# ---- roles --------------------------------------------------------------------------------------------------------


def test_default_roles_and_their_enforced_capabilities():
    reg = RoleRegistry()
    assert set(reg.roles) == {"planner", "frontend", "backend", "database", "tester", "security", "reviewer", "docs"}
    for name in ("planner", "security", "reviewer"):
        role = reg.get(name)
        assert not role.writes and "write_file" not in role.tools and role.terminal_mode == "read"
    assert reg.get("backend").writes_code and reg.get("backend").terminal_mode == "write"
    assert reg.get("tester").terminal_mode == "write" and "process_manage" in reg.get("tester").tools
    assert reg.get("docs").writes_docs_only and "terminal" not in reg.get("docs").tools
    for role in reg.roles.values():
        assert not role.tools & FORBIDDEN_TOOLS  # no delegate_task, memory, cron, browser control, desktop...


def test_role_overrides_and_the_cost_policy(tmp_path):
    cfg = tmp_path / "engineering_roles.toml"
    cfg.write_text('[roles.frontend]\nenabled = false\n[roles.backend]\nmodel = "nvidia/nemotron-3-super-120b-a12b:free"\n'
                   '[roles.reviewer]\nmodel = "anthropic/claude-opus-5"\nprovider = "openrouter"\n')
    reg = RoleRegistry.load(cfg)
    with pytest.raises(RoleError, match="switched off"):
        reg.get("frontend")
    assert reg.get("backend").model.endswith(":free")  # free models are fine
    with pytest.raises(RoleError, match="not a free model"):
        reg.get("reviewer")  # paid: refused, never silently swapped
    assert "reviewer" not in {r.name for r in reg.available()}
    cfg.write_text(cfg.read_text() + "[engineering]\nallow_paid = true\n")
    assert RoleRegistry.load(cfg).get("reviewer").model == "anthropic/claude-opus-5"  # only by explicit choice
    cfg.write_text('[roles.backend]\ncapabilities = ["root_access"]\n')
    with pytest.raises(RoleError, match="unknown capabilities"):
        RoleRegistry.load(cfg)


# ---- task graph ---------------------------------------------------------------------------------------------------

PLAN = [
    {"id": "analyze", "title": "Analyze", "role": "planner"},
    {"id": "api", "title": "API", "role": "backend", "depends_on": ["analyze"], "allowed_paths": ["src/api/"]},
    {"id": "ui", "title": "UI", "role": "frontend", "depends_on": ["analyze"], "allowed_paths": ["src/ui"]},
    {"id": "tests", "title": "Tests", "role": "tester", "depends_on": ["api", "ui"], "allowed_paths": ["tests"]},
    {"id": "review", "title": "Review", "role": "reviewer", "depends_on": ["tests"]},
]


def writes(task):
    return task.role in ("backend", "frontend", "tester", "database", "docs")


def finish(graph, *ids, status="succeeded"):
    for tid in ids:
        graph.tasks[tid].status = status


def test_dependency_order_parallel_writers_and_blocking():
    g = TaskGraph.from_plan(PLAN)
    assert g.validate(RoleRegistry()) == []
    assert g.order() == ["analyze", "api", "ui", "tests", "review"]
    assert [t.id for t in g.startable(4, writes)] == ["analyze"]  # dependencies first
    finish(g, "analyze")
    started = g.startable(4, writes)
    assert [t.id for t in started] == ["api", "ui"]  # independent, disjoint scopes: in parallel
    for t in started:
        t.status = "running"
    assert g.startable(4, writes) == []  # tests waits for both
    finish(g, "api")
    finish(g, "ui", status="failed")
    g.refresh()
    assert g.tasks["tests"].status == "blocked" and g.tasks["review"].status == "blocked"  # never "succeeded"
    assert "ui (failed)" in g.tasks["tests"].failure and g.finished


def test_writers_with_overlapping_or_missing_scopes_never_run_together():
    plan = [{"id": "a", "title": "A", "role": "backend", "allowed_paths": ["src"]},
            {"id": "b", "title": "B", "role": "backend", "allowed_paths": ["src/api/x.py"]},
            {"id": "c", "title": "C", "role": "backend"},  # no scope: the whole repository
            {"id": "r", "title": "R", "role": "reviewer"}]
    g = TaskGraph.from_plan(plan)
    assert [t.id for t in g.startable(4, writes)] == ["a", "r"]  # b overlaps a, c needs everything; readers are free
    assert scopes_overlap(["src/api"], ["src/api/x.py"]) and not scopes_overlap(["src/api"], ["src/ui"])
    assert scopes_overlap([], ["docs"]) and normalize_scope("./src/**/*.ts") == "src"
    assert in_scope("src/api/x.py", ["src/api"]) and not in_scope("src/ui/x.js", ["src/api"])
    assert not in_scope("src/api/../../etc/passwd", ["src/api"])


@pytest.mark.parametrize("plan,problem", [
    ([], "no tasks"),
    ([{"id": "a", "title": "A", "role": "backend"}, {"id": "a", "title": "B", "role": "backend"}], None),
    ([{"id": "a", "title": "A", "role": "backend", "depends_on": ["zzz"]}], "unknown task"),
    ([{"id": "a", "title": "A", "role": "backend", "depends_on": ["b"]},
      {"id": "b", "title": "B", "role": "backend", "depends_on": ["a"]}], "circle"),
    ([{"id": "a", "title": "A", "role": "wizard"}], "unknown role"),
    ([{"id": "a", "title": "A", "role": "backend", "allowed_paths": ["../../etc"]}], "inside the repository"),
    ([{"id": "a", "title": "A", "role": "backend", "allowed_paths": ["/etc/passwd"]}], "inside the repository"),
    ([{"id": "A B!", "title": "A", "role": "backend"}], "lowercase"),
])
def test_invalid_plans_are_rejected(plan, problem):
    if problem is None:
        with pytest.raises(PlanError, match="duplicate"):
            TaskGraph.from_plan(plan)
        return
    problems = TaskGraph.from_plan(plan).validate(RoleRegistry())
    assert any(problem in p for p in problems), problems


def test_cancel_stops_everything_not_finished():
    g = TaskGraph.from_plan(PLAN)
    finish(g, "analyze")
    g.tasks["api"].status = "running"
    assert set(g.cancel_open("cancelled by the user")) == {"api", "ui", "tests", "review"}
    assert g.tasks["analyze"].status == "succeeded" and g.finished


# ---- verification -------------------------------------------------------------------------------------------------

def project(tmp_path, tests: str, name="test_app.py") -> Path:
    root = tmp_path / "p"
    root.mkdir(parents=True, exist_ok=True)
    (root / "pytest.ini").write_text("[pytest]\n")
    (root / name).write_text(tests)
    return root


def pytest_cmd(root):
    return V.TestCommand("pytest", [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-rfE"])


def test_passing_failing_and_empty_suites_are_told_apart(tmp_path):
    ok = V.run(pytest_cmd(None), project(tmp_path / "a", "def test_one():\n    assert 1\ndef test_two():\n    assert 2\n"))
    assert ok.status == "passed" and ok.passed == 2 and ok.failed == 0
    bad = V.run(pytest_cmd(None), project(tmp_path / "b", "def test_good():\n    assert 1\ndef test_bad():\n    assert 0\n"))
    assert bad.status == "failed" and bad.failed == 1 and bad.passed == 1 and any("test_bad" in f for f in bad.failures)
    empty = V.run(pytest_cmd(None), project(tmp_path / "c", "x = 1\n", name="helper.py"))
    assert empty.status == "no_tests"  # exit code alone is never success


def test_missing_dependencies_and_timeouts_are_reported_as_such(tmp_path):
    missing = V.run(pytest_cmd(None), project(tmp_path / "a", "import not_a_real_package_xyz\ndef test_x():\n    pass\n"))
    assert missing.status in ("blocked", "failed") and (missing.status == "blocked" or missing.errors)
    slow = V.run(pytest_cmd(None), project(tmp_path / "b", "import time\ndef test_slow():\n    time.sleep(30)\n"),
                 timeout=3)
    assert slow.status == "timeout" and "did not finish" in slow.detail and slow.seconds < 15
    gone = V.run(V.TestCommand("npm", ["definitely-not-a-runner-xyz", "test"]), tmp_path)
    assert gone.status == "blocked"


@pytest.mark.skipif(sys.platform != "darwin", reason="sandbox-exec is macOS")
def test_tests_cannot_write_outside_their_worktree(tmp_path):
    target = Path.home() / "xenio-sandbox-probe.txt"
    run = V.run(pytest_cmd(None), project(tmp_path, f"def test_escape():\n    open({str(target)!r}, 'w').write('x')\n"))
    assert run.sandboxed and run.status == "failed" and not target.exists()


@pytest.mark.parametrize("runner,output,code,status,counts", [
    ("npm", "Tests:       1 failed, 5 passed, 6 total", 1, "failed", (5, 1)),
    ("npm", "Tests:       7 passed, 7 total", 0, "passed", (7, 0)),
    ("npm", "  12 passing (40ms)\n  2 failing", 1, "failed", (12, 2)),
    ("npm", "npm ERR! Missing script: \"test\"", 1, "blocked", (0, 0)),
    ("go", "ok  \texample.com/a\t0.1s\n--- FAIL: TestB (0.00s)\nFAIL\texample.com/b", 1, "failed", (1, 1)),
    ("go", "?   \texample.com/a\t[no test files]", 0, "no_tests", (0, 0)),
    ("cargo", "test result: ok. 3 passed; 0 failed; 1 ignored; 0 measured", 0, "passed", (3, 0)),
    ("npm", "everything is fine", 0, "no_tests", (0, 0)),  # exit 0 without results is not a pass
])
def test_runner_output_parsing(runner, output, code, status, counts):
    result = V.parse(runner, output, code)
    assert (result.status, result.passed, result.failed) == (status, *counts)


def test_test_command_detection_and_focus(tmp_path):
    (tmp_path / "package.json").write_text('{"scripts": {"test": "echo \\"Error: no test specified\\" && exit 1"}}')
    assert V.detect(tmp_path) == []  # npm's placeholder script is not a test suite
    (tmp_path / "package.json").write_text('{"scripts": {"test": "jest"}}')
    (tmp_path / "yarn.lock").write_text("")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_auth.py").write_text("def test_a():\n    pass\n")
    names = [c.name for c in V.detect(tmp_path)]
    assert names == ["pytest", "yarn"]
    focus = V.focused(V.detect(tmp_path), ["src/auth.py", "README.md"], tmp_path)
    assert focus and focus[0].argv[-1] == "tests/test_auth.py" and focus[0].focused


# ---- deterministic security scan ----------------------------------------------------------------------------------

DIFF = '''diff --git a/app/db.py b/app/db.py
--- a/app/db.py
+++ b/app/db.py
@@ -10,2 +10,6 @@ def find(name):
     existing = cursor.execute("SELECT 1")
+    cursor.execute(f"SELECT * FROM users WHERE name = '{name}'")
+    subprocess.run("ls " + name, shell=True)
+    # eval(this is only a comment)
+    API_KEY = "sk-or-v1-0123456789abcdef0123456789abcdef"
+    data = pickle.loads(blob)
diff --git a/package.json b/package.json
--- a/package.json
+++ b/package.json
@@ -1 +1,2 @@
+  "left-pad": "1.0.0",
'''


def test_security_scan_finds_new_risks_with_evidence():
    findings = S.scan_diff(DIFF, ["app/db.py", "package.json"])
    rules = {(f.rule, f.line) for f in findings}
    assert ("sql-injection", 11) in rules and ("command-injection", 12) in rules
    assert ("unsafe-deserialization", 15) in rules and any(f.rule == "secret" for f in findings)
    assert not any(f.rule == "code-execution" for f in findings)  # comments do not run
    assert not any(f.line == 10 for f in findings)  # existing (unchanged) code is not flagged
    assert any(f.rule == "dependency-change" and f.file == "package.json" for f in findings)
    secret = next(f for f in findings if f.rule == "secret")
    assert "0123456789abcdef0123" not in secret.evidence  # evidence is redacted
    assert S.summary([]).startswith("no findings") and "not proof" in S.summary([])
