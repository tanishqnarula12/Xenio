"""The wall itself, tested for real with sandbox-exec: home unreadable, writes contained, network off, environment clean, time limited. And what goes through it."""

import json
import os
import socket
import sys
from pathlib import Path

import pytest

from app.foundry import sandbox
from app.foundry.deps import Dependencies, find_manifests, in_words, missing_packages, plain_packages, refused
from app.foundry.sandbox import SandboxResult, SandboxUnavailable

needs_mac = pytest.mark.skipif(not sandbox.available(), reason="needs macOS sandbox-exec")


@pytest.fixture
def box(tmp_path):
    home = tmp_path / "home"
    (home / ".ssh").mkdir(parents=True)
    (home / ".ssh" / "id_rsa").write_text("PRIVATE KEY")
    (home / "notes.txt").write_text("private note")
    work = home / "work"
    work.mkdir()
    return home, work


@needs_mac
class TestTheWall:
    def test_your_home_folder_cannot_be_read(self, box):
        home, work = box
        out = sandbox.run(["/bin/cat", str(home / ".ssh" / "id_rsa")], work, [work], home=home)
        assert not out.ok and "PRIVATE KEY" not in out.output and "not permitted" in out.output

    def test_its_own_folder_can(self, box):
        home, work = box
        (work / "mine.txt").write_text("mine")
        assert sandbox.run(["/bin/cat", "mine.txt"], work, [work], home=home).output == "mine"

    def test_a_folder_you_allowed_can_be_read_but_not_changed(self, box):
        home, work = box
        lib = home / "lib"
        lib.mkdir()
        (lib / "a.txt").write_text("lib")
        assert sandbox.run(["/bin/cat", str(lib / "a.txt")], work, [work], readable=[lib], home=home).ok
        assert not sandbox.run(["/usr/bin/touch", str(lib / "b.txt")], work, [work], readable=[lib], home=home).ok and not (lib / "b.txt").exists()

    @pytest.mark.parametrize("target", ["/tmp/xenio-escape-test", "/private/var/tmp/xenio-escape-test"])
    def test_it_cannot_write_outside(self, box, target):
        home, work = box
        assert not sandbox.run(["/usr/bin/touch", target], work, [work], home=home).ok and not os.path.exists(target)

    def test_it_can_write_inside(self, box):
        home, work = box
        assert sandbox.run(["/usr/bin/touch", "made.txt"], work, [work], home=home).ok and (work / "made.txt").exists()

    def test_a_symlink_does_not_lead_out(self, box):
        home, work = box
        (work / "link").symlink_to(home / ".ssh" / "id_rsa")
        assert "PRIVATE KEY" not in sandbox.run(["/bin/cat", "link"], work, [work], home=home).output

    def test_the_network_is_off_by_default(self, box):
        home, work = box
        code = f"import socket\ntry:\n    socket.create_connection(('1.1.1.1', 443), 3)\n    print('reached')\nexcept OSError as e:\n    print('refused', e)\n"
        out = sandbox.run([sys.executable, "-I", "-c", code], work, [work], readable=sandbox.tool_roots(sys.executable), home=home)
        assert "reached" not in out.output

    def probe(self, box, host, port, **kw):
        home, work = box
        code = f"import socket\ntry:\n    socket.create_connection(('{host}', {port}), 3)\n    print('reached')\nexcept OSError as e:\n    print('refused', e)\n"
        return sandbox.run(["/usr/bin/python3", "-c", code], work, [work], home=home, timeout=20, **kw).output

    def test_with_localhost_allowed_the_internet_is_still_off(self, box):
        assert "reached" not in self.probe(box, "1.1.1.1", 443, localhost=True) and "reached" not in self.probe(box, "8.8.8.8", 53, localhost=True)

    def test_a_program_can_talk_to_itself_on_localhost(self, box):
        home, work = box
        code = ("import socket, threading\n"
                "s = socket.socket(); s.bind(('127.0.0.1', 0)); s.listen(1); port = s.getsockname()[1]\n"
                "threading.Thread(target=lambda: s.accept(), daemon=True).start()\n"
                "socket.create_connection(('127.0.0.1', port), 3); print('talked')\n")
        out = sandbox.run(["/usr/bin/python3", "-c", code], work, [work], home=home, timeout=20, localhost=True)
        assert "talked" in out.output, out.output
        blocked = sandbox.run(["/usr/bin/python3", "-c", code], work, [work], home=home, timeout=20)
        assert "talked" not in blocked.output

    def test_the_rule_for_the_network_when_it_is_on_is_really_on(self, box):
        assert "(deny network" not in sandbox.profile([box[1]], network=True)

    def test_the_environment_is_clean(self, box, monkeypatch):
        home, work = box
        monkeypatch.setenv("NABH_API_KEY", "sk-very-secret")
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_verysecret")
        out = sandbox.run(["/usr/bin/env"], work, [work], home=home)
        assert "very" not in out.output and "NABH" not in out.output and "GITHUB" not in out.output

    def test_it_is_stopped_at_the_time_limit(self, box):
        home, work = box
        out = sandbox.run(["/bin/sleep", "30"], work, [work], home=home, timeout=0.5)
        assert out.timed_out and not out.ok and out.seconds < 5

    def test_a_cpu_limit_applies(self, box):
        home, work = box
        out = sandbox.run(["/bin/sh", "-c", "while :; do :; done"], work, [work], home=home, cpu_seconds=1, timeout=20)
        assert not out.timed_out and not out.ok

    def test_input_can_be_given(self, box):
        home, work = box
        assert sandbox.run(["/bin/cat"], work, [work], home=home, stdin="hello").output == "hello"


class TestWithoutTheWall:
    def test_it_refuses_rather_than_runs_unprotected(self, box, monkeypatch):
        monkeypatch.setattr(sandbox, "available", lambda: False)
        home, work = box
        with pytest.raises(SandboxUnavailable):
            sandbox.run(["/bin/echo", "x"], work, [work], home=home)

    def test_the_rules_deny_then_allow_in_that_order(self, tmp_path):
        text = sandbox.profile([tmp_path / "w"], [tmp_path / "r"], False, tmp_path)
        assert text.index("(deny file-read*") < text.index("(allow file-read* ") and "(deny network*)" in text and "(deny file-write*)" in text
        assert "(deny network*)" not in sandbox.profile([tmp_path], network=True)


def manifest(tmp_path, name, content):
    path = tmp_path / name
    path.write_text(content if isinstance(content, str) else json.dumps(content))
    return path


class TestWhatMayBeInstalled:
    def test_plain_packages_pass(self, tmp_path):
        assert refused(manifest(tmp_path, "requirements.txt", "fastapi==0.1\n# a note\nuvicorn[standard]>=0.2\n\npydantic\n")) == []
        assert refused(manifest(tmp_path, "package.json", {"dependencies": {"react": "^18.3.1", "vite": "~5", "x": "latest", "y": ">=1.2.0 <2"}})) == []

    @pytest.mark.parametrize("line", ["git+https://github.com/evil/pkg", "https://evil.example/x.whl", "-e .", "--index-url https://evil.example/simple", "--extra-index-url https://evil.example", "foo @ https://evil.example/foo.whl", "-r other.txt", "--trusted-host evil.example", "file:./local"])
    def test_anything_else_is_refused_in_python(self, tmp_path, line):
        assert refused(manifest(tmp_path, "requirements.txt", f"ok\n{line}\n"))

    @pytest.mark.parametrize("version", ["git+https://github.com/evil/x", "https://evil.example/x.tgz", "file:../x", "github:evil/x", "evil/x", "link:../x"])
    def test_anything_else_is_refused_in_node(self, tmp_path, version):
        assert refused(manifest(tmp_path, "package.json", {"dependencies": {"x": version}}))

    def test_broken_json_is_refused(self, tmp_path):
        assert refused(manifest(tmp_path, "package.json", "{not json"))

    def test_manifests_are_found_where_they_would_be_and_not_in_installed_folders(self, tmp_path):
        (tmp_path / "web").mkdir()
        (tmp_path / "node_modules" / "x").mkdir(parents=True)
        (tmp_path / "web" / "package.json").write_text("{}")
        (tmp_path / "requirements.txt").write_text("")
        (tmp_path / "node_modules" / "x" / "package.json").write_text("{}")
        assert {p.relative_to(tmp_path).as_posix() for p in find_manifests(tmp_path)} == {"web/package.json", "requirements.txt"}


class Recorder:
    def __init__(self, ok=True, output=""):
        self.calls, self.ok, self.output = [], ok, output

    def __call__(self, argv, cwd, writable, **kw):
        self.calls.append({"argv": argv, "cwd": str(cwd), "writable": [str(w) for w in writable], **kw})
        if "venv" in argv and self.ok:
            (Path(argv[-1]) / "bin").mkdir(parents=True, exist_ok=True)
            (Path(argv[-1]) / "bin" / "python").write_text("")
        return SandboxResult(0 if self.ok else 1, self.output, 0.1)


class TestInstalling:
    def deps(self, tmp_path, **kw):
        project = tmp_path / "project"
        project.mkdir(exist_ok=True)
        rec = Recorder(**kw)
        return Dependencies(project, runner=rec), rec, project

    def test_python_uses_wheels_only_in_the_projects_own_environment(self, tmp_path):
        deps, rec, project = self.deps(tmp_path)
        (project / "requirements.txt").write_text("fastapi\n")
        out = deps.ensure(project)
        pip = rec.calls[-1]
        assert "--only-binary=:all:" in pip["argv"] and pip["argv"][0] == str(project / ".venv" / "bin" / "python") and pip["network"] is True and "pytest" in pip["argv"] and out["installed"] == ["fastapi"]
        assert pip["env"]["HOME"].startswith(str(project)) and str(project) in pip["writable"]

    def test_node_runs_no_install_scripts(self, tmp_path):
        deps, rec, project = self.deps(tmp_path)
        (project / "package.json").write_text(json.dumps({"dependencies": {"react": "^18.0.0"}}))
        out = deps.ensure(project)
        npm = rec.calls[-1]
        assert "--ignore-scripts" in npm["argv"] and npm["env"]["npm_config_ignore_scripts"] == "true" and npm["env"]["HOME"].startswith(str(project)) and out["installed"] == ["react"]

    def test_the_same_list_is_not_installed_twice(self, tmp_path):
        deps, rec, project = self.deps(tmp_path)
        (project / "requirements.txt").write_text("fastapi\n")
        deps.ensure(project)
        count = len(rec.calls)
        assert deps.ensure(project)["unchanged"] == 1 and len(rec.calls) == count
        (project / "requirements.txt").write_text("fastapi\nuvicorn\n")
        assert deps.ensure(project)["installed"] == ["fastapi", "uvicorn"]

    def test_a_refused_package_stops_only_that_list_and_says_why(self, tmp_path):
        deps, rec, project = self.deps(tmp_path)
        (project / "requirements.txt").write_text("git+https://github.com/evil/x\n")
        out = deps.ensure(project)
        assert rec.calls == [] and "evil" in out["refused"][0] and "public registry" in in_words(out)

    def test_a_failure_is_reported(self, tmp_path):
        deps, rec, project = self.deps(tmp_path, ok=False, output="no wheel for x")
        (project / "requirements.txt").write_text("x\n")
        assert deps.ensure(project)["failed"]

    def test_installed_folders_are_kept_out_of_git(self, tmp_path):
        deps, _, project = self.deps(tmp_path)
        (project / ".git" / "info").mkdir(parents=True)
        deps.exclude_from_git()
        text = (project / ".git" / "info" / "exclude").read_text()
        assert "node_modules" in text and ".venv" in text

    def test_a_sentence_in_a_requirements_file_does_not_sink_the_install(self, tmp_path):
        deps, rec, project = self.deps(tmp_path)
        (project / "requirements.txt").write_text("flask>=3\nThis project needs Flask and nothing else.\nscikit-learn==1.5\n")
        out = deps.ensure(project)
        listed = next(a for a in rec.calls[-1]["argv"] if a.endswith(".txt"))
        assert Path(listed).read_text() == "flask>=3\nscikit-learn==1.5\n" and out["installed"] == ["flask", "scikit-learn"] and "not a package" in out["refused"][0]


class TestInstallingWhenNeeded:
    def deps(self, tmp_path, **kw):
        project = tmp_path / "project"
        project.mkdir(exist_ok=True)
        rec = Recorder(**kw)
        return Dependencies(project, runner=rec), rec, project

    def test_python_right_now_wheels_only_in_the_projects_environment(self, tmp_path):
        deps, rec, project = self.deps(tmp_path)
        out = deps.install_now(project, ["flask", "requests==2.32.0"])
        pip = rec.calls[-1]
        assert out["installed"] == ["flask", "requests==2.32.0"] and "--only-binary=:all:" in pip["argv"] and pip["argv"][0] == str(project / ".venv" / "bin" / "python") and pip["network"] is True and pip["env"]["HOME"].startswith(str(project))

    def test_node_right_now_with_no_scripts(self, tmp_path):
        deps, rec, project = self.deps(tmp_path)
        assert deps.install_now(project, ["react", "@scope/thing@1.2.0"], "node")["installed"] == ["react", "@scope/thing@1.2.0"]
        assert "--ignore-scripts" in rec.calls[-1]["argv"] and rec.calls[-1]["env"]["npm_config_ignore_scripts"] == "true"

    @pytest.mark.parametrize("name", ["git+https://github.com/evil/x", "https://evil.example/x.whl", "--index-url=https://evil.example", "-e .", "../local", "x; rm -rf /", "$(whoami)", "a b c d", "file:./x"])
    def test_only_plain_names_get_through(self, tmp_path, name):
        deps, rec, project = self.deps(tmp_path)
        out = deps.install_now(project, [name])
        assert out["installed"] == [] and out["refused"] and rec.calls == []

    @pytest.mark.parametrize("name", ["git+https://github.com/evil/x", "https://evil.example/x.tgz", "file:../x", "../x", "x && curl evil"])
    def test_for_node_too(self, name):
        assert plain_packages([name], "node")[0] == []

    def test_it_remembers_what_was_installed_along_the_way(self, tmp_path):
        deps, _, project = self.deps(tmp_path)
        deps.install_now(project, ["flask"])
        deps.install_now(project, ["rich"])
        assert deps.extras() == ["flask", "rich"]

    def test_a_failure_says_why(self, tmp_path):
        deps, _, project = self.deps(tmp_path, ok=False, output="no matching distribution for nope")
        assert "no matching distribution" in deps.install_now(project, ["nope"])["failed"][0]

    @pytest.mark.parametrize("log,expected", [
        ("ModuleNotFoundError: No module named 'flask'", ["flask"]),
        ('ImportError: No module named "yaml"', ["pyyaml"]),
        ("ModuleNotFoundError: No module named 'sklearn'", ["scikit-learn"]),
        ("ModuleNotFoundError: No module named 'flask_cors'", ["flask-cors"]),
        ("ModuleNotFoundError: No module named 'app'", []),
        ("ModuleNotFoundError: No module named 'mymodule'\n", ["mymodule"]),
        ("everything is fine", []),
    ])
    def test_what_a_crash_says_is_missing(self, log, expected):
        assert missing_packages(log) == expected

    def test_a_broken_import_of_its_own_code_is_a_bug_not_a_package(self):
        assert missing_packages("ModuleNotFoundError: No module named 'recommender'", {"recommender"}) == []


@needs_mac
class TestRunningIt:
    def previews(self):
        from app.foundry.preview import Previews

        return Previews()

    def project(self, tmp_path, files):
        root = tmp_path / "project"
        root.mkdir()
        for name, text in files.items():
            (root / name).parent.mkdir(parents=True, exist_ok=True)
            (root / name).write_text(text)
        return root

    def test_a_web_page_really_runs_and_answers(self, tmp_path):
        root = self.project(tmp_path, {"index.html": "<h1>hello</h1>"})
        work = tmp_path / "work"
        work.mkdir()
        previews = self.previews()
        out = previews.start("w", root, work, "/usr/bin/python3")
        try:
            assert out["ok"] and out["status"] == 200 and previews.status("w")["state"] == "running"
            import urllib.request

            with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(out["url"], timeout=5) as reply:
                assert b"hello" in reply.read()
        finally:
            previews.stop("w")
        assert previews.status("w")["state"] == "stopped"

    def test_a_server_that_crashes_says_why(self, tmp_path):
        root = self.project(tmp_path, {".xenio-run": "/usr/bin/python3 -c \"import nosuchmodule_x\"", "x.py": ""})
        work = tmp_path / "work"
        work.mkdir()
        out = self.previews().start("w", root, work, "/usr/bin/python3")
        assert not out["ok"] and out["state"] == "failed" and "nosuchmodule_x" in out["log"]

    def test_nothing_to_run_says_so(self, tmp_path):
        root = self.project(tmp_path, {"lib.py": "x = 1"})
        work = tmp_path / "work"
        work.mkdir()
        out = self.previews().start("w", root, work, "/usr/bin/python3")
        assert not out["ok"] and out["state"] == "nothing"

    def test_what_it_writes_does_not_land_in_the_project_and_it_cannot_reach_outside(self, tmp_path):
        code = "import os\nopen('made.db', 'w').write('x')\nleaked = 'no'\ntry:\n    open(os.path.expanduser('~/.zshrc')).read(); leaked = 'yes'\nexcept OSError:\n    pass\nimport http.server, socketserver\nclass H(http.server.BaseHTTPRequestHandler):\n    def do_GET(self):\n        self.send_response(200); self.end_headers(); self.wfile.write(leaked.encode())\n    def log_message(self, *a): pass\nhttp.server.HTTPServer(('127.0.0.1', int(os.environ['PORT'])), H).serve_forever()\n"
        root = self.project(tmp_path, {"srv.py": code, ".xenio-run": "/usr/bin/python3 srv.py"})
        work = tmp_path / "work"
        work.mkdir()
        previews = self.previews()
        out = previews.start("w", root, work, "/usr/bin/python3")
        try:
            assert out["ok"], out
            import urllib.request

            with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(out["url"], timeout=5) as reply:
                assert reply.read() == b"no"
            assert not (root / "made.db").exists()
        finally:
            previews.stop("w")

    @pytest.mark.parametrize("files,kind,needle", [
        ({"app.py": "from flask import Flask\napp = Flask(__name__)"}, "flask", "flask --app app"),
        ({"main.py": "from fastapi import FastAPI\napp = FastAPI()"}, "fastapi", "uvicorn main:app"),
        ({"app/__init__.py": "def create_app():\n    pass"}, "flask", "flask --app app"),
        ({"manage.py": "x"}, "django", "runserver"),
        ({"package.json": '{"scripts": {"dev": "vite"}}'}, "node", "npm run dev -- --port $PORT"),
        ({"package.json": '{"scripts": {"start": "node server.js"}}'}, "node", "npm run start"),
        ({"web/index.html": "<h1>x</h1>"}, "static", "http.server"),
        ({".xenio-run": "# how\n./go --port $PORT\n", "app.py": "x"}, "declared", "./go --port $PORT"),
    ])
    def test_it_works_out_how_to_start_things(self, tmp_path, files, kind, needle):
        from app.foundry.preview import detect

        found = detect(self.project(tmp_path, files), "/usr/bin/python3")
        assert found.kind == kind and needle in found.how


class TestWhatItBuiltRuns:
    @needs_mac
    def test_a_python_kept_in_the_home_folder_can_still_start(self, tmp_path):
        """The home folder is shut, and uv keeps Python's own files in it: without them Python dies before it can read a line."""
        from app.foundry.preview import Previews
        source = tmp_path / "src"
        source.mkdir()
        (source / "app.py").write_text("import http.server, os\nhttp.server.HTTPServer(('127.0.0.1', int(os.environ['PORT'])), http.server.SimpleHTTPRequestHandler).serve_forever()\n")
        previews = Previews()
        out = previews.start("k", source, tmp_path / "work", sys.executable, keep=False)
        assert out["ok"], out

    def test_a_failure_to_start_is_remembered_with_its_reason(self, tmp_path):
        from app.foundry.preview import Previews
        if not sandbox.available():
            pytest.skip("needs macOS sandbox-exec")
        source = tmp_path / "src"
        source.mkdir()
        (source / ".xenio-run").write_text("python3 -c \"raise SystemExit('boom: no database')\"\n")
        previews = Previews()
        out = previews.start("k", source, tmp_path / "work", sys.executable, keep=True)
        assert not out["ok"]
        state = previews.status("k")
        assert state["state"] == "failed" and "boom: no database" in state["log"]
        previews.stop("k")
        assert previews.status("k")["state"] == "stopped"

    def test_when_there_is_nothing_to_start_the_button_still_answers(self, tmp_path):
        from app.foundry.preview import Previews
        source = tmp_path / "src"
        source.mkdir()
        (source / "notes.txt").write_text("no program here")
        previews = Previews()
        assert previews.start("k", source, tmp_path / "work", sys.executable, keep=True)["state"] == "nothing"
        assert "nothing here to start" in previews.status("k")["detail"]

    @pytest.mark.parametrize("module", ["encodings", "os", "json", "http", "sqlite3"])
    def test_pythons_own_modules_are_never_a_package_to_install(self, module):
        assert missing_packages(f"ModuleNotFoundError: No module named '{module}'") == []

    def test_a_real_missing_package_still_is(self):
        assert missing_packages("ModuleNotFoundError: No module named 'flask'") == ["flask"]
