"""GitHub, READ: repositories, issues, pull requests, branches, commits and CI ("test") results.

Two ways in, the first available wins:
  * the GitHub CLI ``gh`` when the user has signed in (``gh auth login``): ``gh api`` with the user's own
    login, private repositories included - XENIO never sees the token
  * GitHub's public REST API without a token: public repositories only, 60 requests per hour

Write operations (create issue/branch/PR, push) are not here: they stay with git/gh in Hermes' terminal,
where V3's confirmation rules apply. Issue and PR text is written by other people: untrusted content.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from typing import Any

from app.integrations.base import AuthState, Capability, Integration, IntegrationError, clip
from app.safety.policy import RiskLevel

API = "https://api.github.com"
TIMEOUT = 20
_REPO = re.compile(r"^[A-Za-z0-9-]{1,39}/[A-Za-z0-9._-]{1,100}$")
URLOPEN: Callable[..., Any] = urllib.request.urlopen  # replaced in tests


def _gh_exe() -> str | None:
    return shutil.which("gh")


class GitHubIntegration(Integration):
    name = "github"
    title = "GitHub"
    capabilities = (
        Capability("github.repositories", "read", RiskLevel.READ, "your repositories, most recently updated first"),
        Capability("github.overview", "read", RiskLevel.READ, "a repository's state: issues, PRs, last commits, CI runs"),
        Capability("github.issues", "read", RiskLevel.READ, "open/closed issues"),
        Capability("github.pulls", "read", RiskLevel.READ, "open/closed pull requests"),
        Capability("github.branches", "read", RiskLevel.READ, "branches"),
        Capability("github.commits", "read", RiskLevel.READ, "recent commits"),
        Capability("github.runs", "read", RiskLevel.READ, "recent GitHub Actions (test/CI) results"),
    )
    RUN = staticmethod(subprocess.run)  # replaced in tests

    def __init__(self, user: str | None = None, use_gh: bool | None = None):
        self.user = (user or os.environ.get("XENIO_GITHUB_USER") or "").strip() or None
        self._use_gh = use_gh

    # ---- transport -------------------------------------------------------------------------------

    def gh_signed_in(self) -> bool:
        if self._use_gh is not None:
            return self._use_gh
        exe = _gh_exe()
        if not exe:
            self._use_gh = False
            return False
        try:
            self._use_gh = self.RUN([exe, "auth", "status"], capture_output=True, text=True, timeout=15).returncode == 0
        except (OSError, subprocess.SubprocessError):
            self._use_gh = False
        return self._use_gh

    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        query = urllib.parse.urlencode({k: v for k, v in (params or {}).items() if v is not None})
        path = path + ("?" + query if query else "")
        if self.gh_signed_in():
            try:
                proc = self.RUN([_gh_exe() or "gh", "api", "-H", "Accept: application/vnd.github+json", path],
                                capture_output=True, text=True, timeout=TIMEOUT, encoding="utf-8", errors="replace")
            except (OSError, subprocess.SubprocessError) as exc:
                raise IntegrationError(f"gh did not run ({type(exc).__name__}).") from None
            if proc.returncode != 0:
                msg = (proc.stderr or proc.stdout or "").strip().splitlines()
                raise IntegrationError(f"GitHub: {clip(msg[0] if msg else 'request failed', 200)}")
            return json.loads(proc.stdout or "null")
        req = urllib.request.Request(f"{API}/{path.lstrip('/')}", headers={
            "Accept": "application/vnd.github+json", "User-Agent": "XENIO-personal-assistant"})
        try:
            with URLOPEN(req, timeout=TIMEOUT) as resp:
                return json.loads(resp.read().decode("utf-8") or "null")
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                raise IntegrationError("GitHub: not found (a private repository needs `gh auth login`).") from None
            if exc.code in (403, 429):
                raise IntegrationError("GitHub's limit for requests without sign-in is used up (60 per hour); "
                                       "sign in with `gh auth login` for more.") from None
            raise IntegrationError(f"GitHub request failed (HTTP {exc.code}).") from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise IntegrationError(f"Could not reach GitHub ({getattr(exc, 'reason', exc)}).") from None
        except ValueError:
            raise IntegrationError("GitHub sent an unreadable answer.") from None

    def auth_state(self) -> AuthState:
        if self.gh_signed_in():
            return AuthState("connected", "GitHub CLI signed in (private repositories included)")
        gh = "GitHub CLI installed but not signed in" if _gh_exe() else "GitHub CLI not installed"
        who = f" for {self.user}" if self.user else ""
        return AuthState("connected", f"public data only{who} ({gh})",
                         "For private repositories and higher limits run `gh auth login` in a terminal (your browser "
                         "signs you in; XENIO never sees the token).")

    # ---- reads -----------------------------------------------------------------------------------

    def _repo(self, repo: str | None) -> str:
        repo = (repo or "").strip().removeprefix("https://github.com/").removesuffix(".git").strip("/")
        if repo and "/" not in repo and self.user:
            repo = f"{self.user}/{repo}"
        if not _REPO.match(repo):
            raise IntegrationError("Name the repository as owner/name (or set up a project with its repository).")
        return repo

    def repositories(self, limit: int = 10) -> dict:
        limit = max(1, min(int(limit or 10), 30))
        if self.gh_signed_in():
            data = self._get("user/repos", {"sort": "updated", "per_page": limit})
        elif self.user:
            data = self._get(f"users/{self.user}/repos", {"sort": "updated", "per_page": limit})
        else:
            raise IntegrationError("I don't know your GitHub account: set XENIO_GITHUB_USER in .env or run `gh auth login`.")
        repos = [{"repo": r.get("full_name"), "description": clip(r.get("description"), 140), "private": r.get("private", False),
                  "open_issues_and_prs": r.get("open_issues_count", 0), "pushed": r.get("pushed_at"),
                  "language": r.get("language")} for r in data or [] if isinstance(r, dict)]
        return {"account": self.user, "repositories": repos}

    def issues(self, repo: str, state: str = "open", limit: int = 10) -> dict:
        repo = self._repo(repo)
        data = self._get(f"repos/{repo}/issues", {"state": _state(state), "per_page": max(1, min(int(limit or 10), 30))})
        issues = [{"number": i.get("number"), "title": clip(i.get("title"), 200), "author": (i.get("user") or {}).get("login"),
                   "labels": [lbl.get("name") for lbl in i.get("labels") or [] if isinstance(lbl, dict)][:5],
                   "comments": i.get("comments", 0), "updated": i.get("updated_at"), "excerpt": clip(i.get("body"), 240)}
                  for i in data or [] if isinstance(i, dict) and "pull_request" not in i]  # the issues API includes PRs
        return {"repo": repo, "state": _state(state), "issues": issues}

    def pulls(self, repo: str, state: str = "open", limit: int = 10) -> dict:
        repo = self._repo(repo)
        data = self._get(f"repos/{repo}/pulls", {"state": _state(state), "per_page": max(1, min(int(limit or 10), 30))})
        pulls = [{"number": p.get("number"), "title": clip(p.get("title"), 200), "author": (p.get("user") or {}).get("login"),
                  "branch": (p.get("head") or {}).get("ref"), "draft": p.get("draft", False), "updated": p.get("updated_at")}
                 for p in data or [] if isinstance(p, dict)]
        return {"repo": repo, "state": _state(state), "pull_requests": pulls}

    def branches(self, repo: str, limit: int = 20) -> dict:
        repo = self._repo(repo)
        data = self._get(f"repos/{repo}/branches", {"per_page": max(1, min(int(limit or 20), 50))})
        return {"repo": repo, "branches": [b.get("name") for b in data or [] if isinstance(b, dict)]}

    def commits(self, repo: str, limit: int = 5) -> dict:
        repo = self._repo(repo)
        data = self._get(f"repos/{repo}/commits", {"per_page": max(1, min(int(limit or 5), 20))})
        commits = [{"sha": str(c.get("sha", ""))[:7], "message": clip(((c.get("commit") or {}).get("message") or "").splitlines()[0]
                                                                      if (c.get("commit") or {}).get("message") else "", 160),
                    "author": ((c.get("commit") or {}).get("author") or {}).get("name"),
                    "date": ((c.get("commit") or {}).get("author") or {}).get("date")} for c in data or [] if isinstance(c, dict)]
        return {"repo": repo, "commits": commits}

    def runs(self, repo: str, limit: int = 5) -> dict:
        repo = self._repo(repo)
        data = self._get(f"repos/{repo}/actions/runs", {"per_page": max(1, min(int(limit or 5), 20))}) or {}
        runs = [{"workflow": r.get("name"), "branch": r.get("head_branch"), "status": r.get("status"),
                 "result": r.get("conclusion"), "started": r.get("run_started_at") or r.get("created_at")}
                for r in data.get("workflow_runs") or [] if isinstance(r, dict)]
        return {"repo": repo, "runs": runs, **({} if runs else {"note": "no GitHub Actions runs (no CI set up)"})}

    def overview(self, repo: str) -> dict:
        repo = self._repo(repo)
        info = self._get(f"repos/{repo}") or {}
        out = {"repo": repo, "description": clip(info.get("description"), 160), "default_branch": info.get("default_branch"),
               "private": info.get("private", False), "pushed": info.get("pushed_at")}
        out["open_issues"] = self.issues(repo, "open", 5)["issues"]
        out["open_pull_requests"] = self.pulls(repo, "open", 5)["pull_requests"]
        out["recent_commits"] = self.commits(repo, 3)["commits"]
        out["ci_runs"] = self.runs(repo, 3)["runs"]
        return out


def _state(value: str) -> str:
    value = (value or "open").lower()
    return value if value in ("open", "closed", "all") else "open"
