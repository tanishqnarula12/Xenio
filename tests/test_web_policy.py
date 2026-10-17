"""V3 permission rules: links, web pages, the browser, downloads, memory, Git and GitHub."""

import pytest

from app.safety import policy as p

DESKTOP = p.known_folders()["Desktop"]


@pytest.fixture
def paths(tmp_path):
    return p.PathPolicy(workspace=tmp_path / "ws")


def level(decision):
    return "BLOCKED" if decision.blocked else decision.level.name


def tool(name, args, paths, page=None):
    return p.classify_tool_call(name, args, paths, None, page=page)


# ---- links -------------------------------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("geeksforgeeks.org", "https://geeksforgeeks.org"),
    ("docs.python.org/3/library", "https://docs.python.org/3/library"),
    ("localhost:8080/x", "http://localhost:8080/x"),
    ("https://example.com", "https://example.com"),
    ("mailto:a@b.com", "mailto:a@b.com"),
    ("javascript:alert(1)", "javascript:alert(1)"),
    ("", ""),
])
def test_normalize_url(raw, expected):
    assert p.normalize_url(raw) == expected


@pytest.mark.parametrize("url,expected", [
    ("https://hermes-agent.nousresearch.com/docs", "READ"),
    ("developers.strava.com", "READ"),
    ("https://user:hunter2@example.com/", "BLOCKED"),               # credentials in the link
    ("https://example.com/?key=sk-or-v1-abcdefghijklmnopqrst", "BLOCKED"),  # API key in the link
    ("chrome://settings/passwords", "BLOCKED"),
    ("edge://wallet", "BLOCKED"),
    ("about:logins", "BLOCKED"),
    ("chrome://version", "CONFIRM_REQUIRED"),
    ("javascript:document.cookie", "BLOCKED"),
    ("javascript:void(0)", "CONFIRM_REQUIRED"),
    ("ms-settings:privacy", "CONFIRM_REQUIRED"),                   # starts another program
    ("mailto:someone@example.com", "CONFIRM_REQUIRED"),
    ("https://", "BLOCKED"),                                        # no website
    ("", "BLOCKED"),
])
def test_classify_url(url, expected, paths):
    assert level(p.classify_url(url, paths)) == expected


def test_local_file_links_follow_the_path_policy(paths):
    assert level(p.classify_url("file:///C:/Users/x/.ssh/id_rsa", paths)) == "BLOCKED"
    assert level(p.classify_url(f"file:///{DESKTOP.as_posix()}/notes.html", paths)) == "READ"


# ---- JavaScript ---------------------------------------------------------------------------------

@pytest.mark.parametrize("js,expected", [
    ("document.title", "READ"),
    ("[...document.querySelectorAll('a')].map(a => a.href)", "READ"),
    ("(() => { const t = document.body.innerText; return t.slice(0, 500); })()", "READ"),
    ("console.log('Page loaded:', window.location.href)", "READ"),
    ("document.cookie", "BLOCKED"),
    ("localStorage.getItem('token')", "BLOCKED"),
    ("document.querySelector('input[type=password]').value", "BLOCKED"),
    ("document.querySelector('#q').value = 'python'", "SAFE_WRITE"),
    ("document.querySelector('a.next').click()", "SAFE_WRITE"),
    ("document.querySelector('button[type=submit]').click()", "CONFIRM_REQUIRED"),
    ("document.querySelector('#post-comment').click()", "CONFIRM_REQUIRED"),
    ("document.querySelector('form#search').submit()", "SAFE_WRITE"),      # searching is a safe action
    ("document.querySelector('form#signup').submit()", "CONFIRM_REQUIRED"),
    ("fetch('/api/messages', {method: 'POST', body: 'hi'})", "CONFIRM_REQUIRED"),
    ("document.querySelector('#checkout').click()", "HIGH_RISK"),
    ("document.querySelector('#place-order').click()", "HIGH_RISK"),
    ("document.querySelector('.delete-account').click()", "HIGH_RISK"),
    ("eval(atob('ZG9jdW1lbnQ='))", "CONFIRM_REQUIRED"),
])
def test_classify_js(js, expected):
    assert level(p.classify_js(js)) == expected


# ---- browser_exec (Hermes' Browser Use mode) --------------------------------------------------

@pytest.mark.parametrize("code,expected", [
    # the exact calls from the user's session: reading a page must never ask
    ('new_tab("https://www.geeksforgeeks.org")', "READ"),
    ("page_info()", "READ"),
    ('print(js("document.title"))', "READ"),
    ("js(\"console.log('Page loaded:', window.location.href)\")", "READ"),
    ('new_tab("https://example.com")\nwait_for_load()\nprint(page_info())\ncapture_screenshot()', "READ"),
    ('links = js("[...document.links].map(l => l.href)")\nfor l in links[:5]:\n    print(l)', "READ"),
    ("fill_input('#search', 'python decorators')", "SAFE_WRITE"),
    ("fill_input('input[name=password]', 'x')", "HIGH_RISK"),
    ("fill_input('#comment', 'sk-or-v1-abcdefghijklmnopqrstu')", "BLOCKED"),
    ("click_at_xy(120, 300)", "CONFIRM_REQUIRED"),
    ("js(\"document.querySelector('#buy-now').click()\")", "HIGH_RISK"),
    ("print(js('document.cookie'))", "BLOCKED"),
    ("cdp('Network.getAllCookies')", "BLOCKED"),
    ("cdp('Storage.getCookies')", "BLOCKED"),
    ("cdp('Security.setIgnoreCertificateErrors', ignore=True)", "BLOCKED"),
    ("cdp('Accessibility.getFullAXTree')", "READ"),
    ("cdp('Runtime.evaluate', expression='document.title')", "READ"),
    ("cdp('Input.dispatchMouseEvent', type='mousePressed', x=1, y=2)", "CONFIRM_REQUIRED"),
    ("import os\nos.system('calc')", "CONFIRM_REQUIRED"),
    ("os.remove('x')", "CONFIRM_REQUIRED"),                   # system access without an import
    ("open('C:/Users/x/.env').read()", "BLOCKED"),
    ("mystery_helper()", "CONFIRM_REQUIRED"),                 # unknown function: fail closed
    ("new_tab('chrome://settings/passwords')", "BLOCKED"),
    ("upload_file('#file', 'C:/Users/x/.ssh/id_rsa')", "BLOCKED"),
    ("def broken(:", "CONFIRM_REQUIRED"),  # not valid Python: cannot be checked
    ("", "BLOCKED"),
])
def test_classify_browser_code(code, expected, paths):
    assert level(tool("browser_exec", {"code": code}, paths)) == expected


def test_browser_code_summary_names_the_site(paths):
    d = tool("browser_exec", {"code": 'new_tab("https://www.geeksforgeeks.org")'}, paths)
    assert d.summary == "open and read www.geeksforgeeks.org"


# ---- step-by-step browser tools ----------------------------------------------------------------

def test_builtin_browser_tools(paths):
    page = {"refs": {"e1": 'link "Next page"', "e2": 'button "Place order"', "e3": 'button "Post reply"',
                     "e4": 'textbox "Password"', "e5": 'searchbox "Search"'}, "last_typed": ""}
    assert level(tool("browser_navigate", {"url": "https://example.com"}, paths, page)) == "READ"
    assert level(tool("browser_navigate", {"url": "about:logins"}, paths, page)) == "BLOCKED"
    assert level(tool("browser_snapshot", {}, paths, page)) == "READ"
    assert level(tool("browser_click", {"ref": "@e1"}, paths, page)) == "SAFE_WRITE"
    assert level(tool("browser_click", {"ref": "@e2"}, paths, page)) == "HIGH_RISK"
    assert level(tool("browser_click", {"ref": "@e3"}, paths, page)) == "CONFIRM_REQUIRED"
    assert level(tool("browser_click", {"ref": "@e99"}, paths, page)) == "SAFE_WRITE"  # unknown element
    assert level(tool("browser_type", {"ref": "@e4", "text": "x"}, paths, page)) == "HIGH_RISK"
    assert level(tool("browser_type", {"ref": "@e5", "text": "python"}, paths, page)) == "SAFE_WRITE"
    assert level(tool("browser_press", {"key": "Enter"}, paths, {**page, "last_typed": 'searchbox "Search"'})) == "SAFE_WRITE"
    assert level(tool("browser_press", {"key": "Enter"}, paths, {**page, "last_typed": 'textbox "Message"'})) == "CONFIRM_REQUIRED"
    assert level(tool("browser_press", {"key": "Enter"}, paths, page)) == "CONFIRM_REQUIRED"
    assert level(tool("browser_press", {"key": "Tab"}, paths, page)) == "SAFE_WRITE"
    assert level(tool("browser_console", {}, paths, page)) == "READ"
    assert level(tool("browser_console", {"expression": "document.cookie"}, paths, page)) == "BLOCKED"
    assert level(tool("browser_cdp", {"method": "Network.getAllCookies"}, paths, page)) == "BLOCKED"
    assert level(tool("browser_dialog", {"action": "accept"}, paths, page)) == "CONFIRM_REQUIRED"
    assert level(tool("browser_dialog", {"action": "dismiss"}, paths, page)) == "SAFE_WRITE"
    for vault in ("browser_vault_list", "browser_vault_fill", "browser_vault_unlock"):
        assert level(tool(vault, {}, paths, page)) == "HIGH_RISK"


# ---- opening pages for the user, downloads -----------------------------------------------------

def test_open_url_for_the_user(paths):
    assert level(tool("xenio_open_url", {"url": "geeksforgeeks.org"}, paths)) == "READ"
    assert level(tool("xenio_open_url", {"url": "ms-settings:privacy"}, paths)) == "BLOCKED"
    assert level(tool("xenio_open_url", {"url": "file:///C:/Windows/notepad.exe"}, paths)) == "BLOCKED"
    assert level(tool("xenio_open_url", {"url": "https://a:b@example.com"}, paths)) == "BLOCKED"
    assert level(tool("xenio_open_url", {}, paths)) == "BLOCKED"


def test_download_rules(paths, tmp_path):
    downloads = paths.named_roots["Downloads"]
    d = tool("xenio_download_file", {"url": "https://example.com/files/report.pdf"}, paths)
    assert level(d) == "SAFE_WRITE" and "report.pdf" in d.summary
    assert p.download_target("https://example.com/files/report.pdf", None, paths) == downloads / "report.pdf"
    assert level(tool("xenio_download_file", {"url": "https://example.com/setup.exe"}, paths)) == "CONFIRM_REQUIRED"
    assert level(tool("xenio_download_file", {"url": "https://example.com/install.ps1"}, paths)) == "CONFIRM_REQUIRED"
    assert level(tool("xenio_download_file", {"url": "https://example.com/a.txt",
                                              "destination": "C:/Windows/System32/a.txt"}, paths)) == "HIGH_RISK"
    assert level(tool("xenio_download_file", {"url": "https://example.com/k",
                                              "destination": "~/.ssh/authorized_keys"}, paths)) == "BLOCKED"
    assert level(tool("xenio_download_file", {"url": "https://example.com/x",
                                              "destination": str(tmp_path / "ws" / ".env")}, paths)) == "BLOCKED"
    assert level(tool("xenio_download_file", {"url": "ftp://example.com/a.txt"}, paths)) == "BLOCKED"
    assert level(tool("xenio_download_file", {"url": "file:///C:/Users/x/a.txt"}, paths)) == "BLOCKED"
    existing = tmp_path / "ws" / "have.txt"
    existing.parent.mkdir(parents=True, exist_ok=True)
    existing.write_text("x", encoding="utf-8")
    replace = tool("xenio_download_file", {"url": "https://e.com/have.txt", "destination": str(existing),
                                           "overwrite": True}, paths)
    assert level(replace) == "CONFIRM_REQUIRED" and "replace" in replace.summary


@pytest.mark.parametrize("name,expected", [
    ("report.pdf", "report.pdf"),
    ("..%2F..%2FWindows%2Fevil.dll", "evil.dll"),  # path traversal in the link
    ("..\\..\\x.txt", "x.txt"),
    ("a<b>c?.txt", "a_b_c_.txt"),
    ("CON.txt", "_CON.txt"),
    (".env", ".env"),            # keeps its name, so the secret-file rule still sees it
    ("notes.txt. ", "notes.txt"),
    ("", "download"),
    ("...", "download"),
])
def test_safe_filename(name, expected):
    assert p.safe_filename(name) == expected


def test_download_target_never_escapes_the_folder(paths):
    target = p.download_target("https://e.com/..%2F..%2F..%2Fsecret.txt", None, paths)
    assert target.parent == paths.named_roots["Downloads"]


# ---- memory --------------------------------------------------------------------------------------

@pytest.mark.parametrize("args,expected", [
    ({"action": "add", "content": "Active project: XENIO voice assistant, repo on the Desktop"}, "SAFE_WRITE"),
    ({"action": "add", "content": "User decided to use PostgreSQL for the project"}, "SAFE_WRITE"),
    ({"action": "add", "content": "OpenRouter key: sk-or-v1-abcdefghijklmnopqrstu"}, "BLOCKED"),
    ({"action": "add", "content": "github token ghp_abcdefghijklmnopqrstuvwxyz0123"}, "BLOCKED"),
    ({"action": "add", "content": "The wifi password is sunflower42"}, "BLOCKED"),
    ({"operations": [{"action": "add", "content": "ok"}, {"action": "add", "content": "api_key=abcdef123456"}]}, "BLOCKED"),
])
def test_memory_never_stores_secrets(args, expected, paths):
    assert level(tool("memory", args, paths)) == expected


def test_skills_may_mention_env_var_names(paths):
    assert level(tool("skill_manage", {"content": "export API_KEY=${API_KEY}"}, paths)) == "SAFE_WRITE"
    assert level(tool("skill_manage", {"content": "token ghp_abcdefghijklmnopqrstuvwxyz0123"}, paths)) == "BLOCKED"


# ---- Git and GitHub ------------------------------------------------------------------------------

@pytest.mark.parametrize("command,expected", [
    ("git status", "READ"),
    ("git log --oneline -5", "READ"),
    ("git diff HEAD~1", "READ"),
    ("git branch", "READ"),
    ("git branch feature/web", "SAFE_WRITE"),
    ("git checkout -b feature/web", "SAFE_WRITE"),
    ("git switch -c feature/web", "SAFE_WRITE"),
    ("git add -A && git commit -m 'web tools'", "SAFE_WRITE"),
    ("git checkout main", "CONFIRM_REQUIRED"),
    ("git branch -d old", "CONFIRM_REQUIRED"),
    ("git branch -D old", "HIGH_RISK"),
    ("git push origin feature/web", "CONFIRM_REQUIRED"),
    ("git push --force origin main", "HIGH_RISK"),
    ("git push origin --delete old", "HIGH_RISK"),
    ("git merge feature/web", "CONFIRM_REQUIRED"),
    ("git reset --hard HEAD~1", "HIGH_RISK"),
    ("gh repo view", "READ"),
    ("gh issue list --state open", "READ"),
    ("gh pr view 12 --comments", "READ"),
    ("gh pr diff 12", "READ"),
    ("gh pr checks 12", "READ"),
    ("gh run list", "READ"),
    ("gh api repos/NousResearch/hermes-agent/issues", "READ"),
    ("gh auth status", "READ"),
    ("gh pr checkout 12", "SAFE_WRITE"),
    ("gh issue create --title x --body y", "CONFIRM_REQUIRED"),
    ("gh pr comment 12 --body hi", "CONFIRM_REQUIRED"),
    ("gh pr create --fill", "CONFIRM_REQUIRED"),
    ("gh pr merge 12 --squash", "CONFIRM_REQUIRED"),
    ("gh pr merge 12 --admin", "HIGH_RISK"),
    ("gh api -X POST repos/o/r/issues -f title=x", "CONFIRM_REQUIRED"),
    ("gh api repos/o/r/issues -f title=x", "CONFIRM_REQUIRED"),
    ("gh api -X DELETE repos/o/r", "HIGH_RISK"),
    ("gh release create v1.0", "HIGH_RISK"),
    ("gh repo delete o/r --yes", "HIGH_RISK"),
    ("gh repo edit --visibility public", "HIGH_RISK"),
    ("gh secret set TOKEN", "HIGH_RISK"),
    ("gh auth login", "HIGH_RISK"),
    ("gh auth token", "BLOCKED"),
    ("gh auth status --show-token", "BLOCKED"),
])
def test_git_and_github_commands(command, expected, paths):
    assert level(p.classify_command(command, paths)) == expected


@pytest.mark.parametrize("command,expected", [
    ("git ls-remote --heads origin", "READ"),  # live: asked "run the git command" just to list remote branches
    ("git -C ~/Xenio/v3-demo ls-remote --heads origin main", "READ"),
    ("git rev-list --count HEAD", "READ"),
    ("git -c color.ui=false log --oneline", "READ"),
    ("git -c core.quotepath=off status", "READ"),
    ("git -c user.name=X -c user.email=x@y.z commit -m msg", "SAFE_WRITE"),
    # settings and options that make git start a program are never silent
    ("git -c core.pager=calc.exe log", "CONFIRM_REQUIRED"),
    ("git -c core.fsmonitor=evil.exe status", "CONFIRM_REQUIRED"),
    ("git -c core.sshCommand='sh -c evil' fetch", "CONFIRM_REQUIRED"),
    ("git -c alias.x='!calc' x", "CONFIRM_REQUIRED"),
    ("git --exec-path=C:/evil status", "CONFIRM_REQUIRED"),
    ("git --config-env=core.pager=EVIL log", "CONFIRM_REQUIRED"),
    ("git ls-remote --upload-pack='calc.exe' origin", "CONFIRM_REQUIRED"),
    ("git ls-remote -u calc.exe origin", "CONFIRM_REQUIRED"),
    ("git fetch --upload-pack calc.exe origin", "CONFIRM_REQUIRED"),
    ("git clone --template=./hooks-dir https://github.com/o/r", "CONFIRM_REQUIRED"),
    ("git clone -c core.fsmonitor=evil https://github.com/o/r", "CONFIRM_REQUIRED"),
    ("git diff --output=notes.txt", "CONFIRM_REQUIRED"),
    ("git add -u", "SAFE_WRITE"),
])
def test_git_settings_that_can_run_programs(command, expected, paths):
    assert level(p.classify_command(command, paths)) == expected


@pytest.mark.parametrize("name", [".git/hooks/pre-commit", ".git/config", ".GIT/hooks/post-checkout"])
def test_writing_gits_internal_files_needs_confirmation(name, paths, tmp_path):
    """A hook or .git/config entry turns the next "safe" git commit/status into running a program."""
    target = str(tmp_path / "ws" / "repo" / name)
    assert level(paths.check(target, "create")) == "CONFIRM_REQUIRED"
    assert level(paths.check(target, "read")) == "READ"
    assert level(paths.check(str(tmp_path / "ws" / "repo" / ".gitignore"), "create")) == "SAFE_WRITE"


def test_policy_version_bumped():
    assert p.POLICY_VERSION == "7"  # V5.5 desktop tools -> XENIO starts a fresh Hermes session
