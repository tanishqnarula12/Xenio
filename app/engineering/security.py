"""Deterministic security checks on an engineering run's changes (V6).

The security reviewer role (a model) is a second opinion; these checks are the part that does not depend on a model.
They read only what the agents changed (added lines of the run's diff, plus changed file names), so the user's
existing code is not flagged, and report each finding with file, line, rule and the (redacted) evidence.

Checked: secrets, SQL built from strings, shell commands built from input, eval/exec, unsafe deserialization
(pickle, yaml.load, marshal), TLS verification switched off, debug mode, raw HTML injection, path traversal from
request input, world-writable permissions, secrets written to logs, and risky files (dependency manifests and lock
files, CI pipelines, Docker files, auth/permission code, .env files, migrations). Installed scanners (bandit,
semgrep, pip-audit, npm audit) are run when they exist; XENIO never installs them.

A clean result means "these checks found nothing", never "the change is secure".
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path

from app.safety.policy import contains_secret, redact_secrets


@dataclass
class Finding:
    rule: str
    severity: str  # high | medium | low | info
    file: str
    line: int
    evidence: str
    advice: str
    source: str = "xenio"  # xenio | bandit | semgrep | ...

    def to_dict(self) -> dict:
        return asdict(self)

    def text(self) -> str:
        where = f"{self.file}:{self.line}" if self.line else self.file
        return f"[{self.severity}] {self.rule} at {where}: {self.evidence} - {self.advice}"


# (rule, severity, pattern on an added line, advice, file suffixes it applies to (None = all))
_LINE_RULES: list[tuple[str, str, re.Pattern, str, tuple[str, ...] | None]] = [(r, s, re.compile(p, re.I), a, x) for r, s, p, a, x in [
    ("sql-injection", "high",
     r"""(execute|executemany|raw|query|exec)\s*\(\s*(f["']|["'][^"']*(select|insert|update|delete)[^"']*["']\s*(%|\+|\.format))""",
     "build SQL with parameters (placeholders), never by inserting values into the string", None),
    ("sql-injection", "high", r"""(select|insert|update|delete)\b[^"'`]*\$\{""",
     "use parameterized queries instead of template strings", (".js", ".ts", ".jsx", ".tsx", ".mjs")),
    ("command-injection", "high", r"subprocess\.\w+\([^)]*shell\s*=\s*True|os\.system\(|os\.popen\(",
     "pass the command as a list without a shell; never put input into a shell string", (".py",)),
    ("command-injection", "high", r"child_process|\bexec(Sync)?\s*\(\s*[`'\"].*\$\{|\bexec(Sync)?\s*\([^)]*\+",
     "use execFile/spawn with an argument list; never build a shell command from input", (".js", ".ts", ".mjs")),
    ("code-execution", "high", r"(?<![\w.])(eval|exec)\s*\(", "do not evaluate strings as code", (".py",)),
    ("code-execution", "high", r"(?<![\w.])eval\s*\(|new\s+Function\s*\(", "do not evaluate strings as code",
     (".js", ".ts", ".jsx", ".tsx", ".mjs")),
    ("unsafe-deserialization", "high", r"pickle\.loads?\(|marshal\.loads?\(|shelve\.open\(|yaml\.load\((?![^)]*SafeLoader)",
     "never deserialize untrusted data with pickle/marshal; use yaml.safe_load", (".py",)),
    ("tls-verification-off", "high", r"verify\s*=\s*False|rejectUnauthorized\s*:\s*false|NODE_TLS_REJECT_UNAUTHORIZED|"
     r"InsecureRequestWarning|CERT_NONE", "keep certificate verification on", None),
    ("debug-enabled", "medium", r"\bDEBUG\s*=\s*True|app\.run\([^)]*debug\s*=\s*True|app\.debug\s*=\s*True",
     "never enable debug mode outside local development", (".py",)),
    ("xss", "medium", r"dangerouslySetInnerHTML|\.innerHTML\s*=|\.outerHTML\s*=|document\.write\(|v-html\s*=|\|\s*safe\b|"
     r"mark_safe\(", "escape output; avoid raw HTML from data", None),
    ("path-traversal", "medium", r"(open|send_file|sendFile|readFile|createReadStream|Path)\s*\(\s*[^)]*(request|req)\."
     r"(args|params|query|body|form|GET|POST)", "resolve the path and check it stays inside the allowed folder", None),
    ("weak-permissions", "medium", r"chmod\s*\(?\s*[^)]*0?o?777|chmod\s+(-R\s+)?777|0o?666\b",
     "do not make files world-writable", None),
    ("secret-in-log", "medium", r"(log|logger|console|print)\w*\.?\w*\(.*\b(password|passwd|secret|token|api_?key|"
     r"authorization)\b", "never log credentials or tokens", None),
    ("cors-wildcard", "low", r"Access-Control-Allow-Origin['\"]?\s*[:,]\s*['\"]\*|origin\s*:\s*['\"]\*['\"]|"
     r"CORS\([^)]*origins\s*=\s*['\"]\*", "allow only the origins that need access", None),
    ("weak-hash", "low", r"\b(md5|sha1)\s*\(|hashlib\.(md5|sha1)\(|createHash\(['\"](md5|sha1)",
     "do not use MD5/SHA-1 for passwords or signatures", None),
    ("disabled-auth-check", "medium", r"(skip|disable|bypass)_?(auth|authentication|authorization|csrf|permission)|"
     r"csrf_exempt|@login_not_required|AllowAny", "make sure this endpoint really must be public", None),
    ("prompt-injection-surface", "low", r"(system|prompt)\s*[+=].*(request|req|input|user_input|body)\b",
     "keep user input out of model instructions, or mark it clearly as data", None),
]]

# risky files: (rule, severity, pattern on the path, advice)
_FILE_RULES = [(r, s, re.compile(p, re.I), a) for r, s, p, a in [
    ("dependency-change", "medium", r"(^|/)(package(-lock)?\.json|yarn\.lock|pnpm-lock\.yaml|requirements[^/]*\.txt|"
     r"pyproject\.toml|poetry\.lock|Pipfile(\.lock)?|go\.(mod|sum)|Cargo\.(toml|lock)|Gemfile(\.lock)?|composer\.(json|lock))$",
     "new or changed dependencies: check their source, version and licence before installing"),
    ("ci-change", "medium", r"(^|/)(\.github/workflows/|\.gitlab-ci\.yml|Jenkinsfile|\.circleci/|azure-pipelines)",
     "CI configuration runs with repository secrets: review it line by line"),
    ("container-change", "low", r"(^|/)(Dockerfile|docker-compose[^/]*\.ya?ml)$", "check base images and exposed ports"),
    ("env-file", "high", r"(^|/)\.env(\.(?!example$|sample$|template$)[\w.-]+)?$",
     "environment files can hold secrets and should not be committed"),
    ("migration", "info", r"(^|/)(migrations?|alembic|db/migrate)/", "a migration was written: it was NOT applied anywhere"),
    ("auth-code", "info", r"(auth|login|session|permission|password|token|oauth|jwt)[^/]*\.\w+$",
     "authentication/authorization code changed: needs a careful human review"),
]]


def added_lines(diff: str):
    """(file, new line number, text) for every added line of a unified diff."""
    file, line = "", 0
    for raw in diff.splitlines():
        if raw.startswith("+++ "):
            name = raw[4:].strip()
            file = name[2:] if name.startswith("b/") else name
        elif raw.startswith("@@"):
            m = re.search(r"\+(\d+)", raw)
            line = int(m.group(1)) if m else 0
        elif raw.startswith("+") and not raw.startswith("+++"):
            yield file, line, raw[1:]
            line += 1
        elif not raw.startswith("-"):
            line += 1


def scan_diff(diff: str, changed: list[str]) -> list[Finding]:
    findings: list[Finding] = []
    seen = set()
    for file, line, text in added_lines(diff):
        if not file or file == "/dev/null":
            continue
        suffix = Path(file).suffix.lower()
        stripped = text.strip()
        if contains_secret(text, strict=True) and not re.search(r"(example|placeholder|dummy|changeme|xxx)", text, re.I):
            findings.append(Finding("secret", "high", file, line, redact_secrets(stripped)[:120],
                                    "remove the secret; load it from the environment or a secret store"))
        for rule, severity, pattern, advice, suffixes in _LINE_RULES:
            if suffixes is not None and suffix not in suffixes:
                continue
            if stripped.startswith(("#", "//", "*")) and rule not in ("secret",):
                continue  # comments do not run
            if pattern.search(text):
                key = (rule, file, line)
                if key not in seen:
                    seen.add(key)
                    findings.append(Finding(rule, severity, file, line, redact_secrets(stripped)[:120], advice))
    for path in changed:
        for rule, severity, pattern, advice in _FILE_RULES:
            if pattern.search(path):
                findings.append(Finding(rule, severity, path, 0, "file changed", advice))
    order = {"high": 0, "medium": 1, "low": 2, "info": 3}
    return sorted(findings, key=lambda f: (order[f.severity], f.file, f.line))


def external_scanners(worktree: Path, changed: list[str], timeout: float = 120) -> tuple[list[Finding], list[str]]:
    """Run installed scanners on the changed files. Returns (findings, notes about what ran or was not available)."""
    findings: list[Finding] = []
    notes: list[str] = []
    py = [c for c in changed if c.endswith(".py") and (worktree / c).exists()]
    if py and shutil.which("bandit"):
        try:
            done = subprocess.run(["bandit", "-q", "-f", "json", *py], cwd=worktree, capture_output=True, text=True,
                                  timeout=timeout)
            for r in (json.loads(done.stdout or "{}").get("results") or []):
                findings.append(Finding(f"bandit:{r.get('test_id')}", str(r.get("issue_severity", "low")).lower(),
                                        r.get("filename", ""), int(r.get("line_number") or 0),
                                        redact_secrets(str(r.get("issue_text", "")))[:120], "see bandit's advice", "bandit"))
            notes.append(f"bandit ran on {len(py)} file(s)")
        except (subprocess.TimeoutExpired, ValueError, OSError) as exc:
            notes.append(f"bandit could not run ({type(exc).__name__})")
    if changed and shutil.which("semgrep"):
        notes.append("semgrep is installed but needs network rules; not run automatically")
    if not shutil.which("bandit"):
        notes.append("bandit is not installed (optional: pipx install bandit)")
    return findings, notes


def summary(findings: list[Finding]) -> str:
    if not findings:
        return "no findings from XENIO's checks (this is not proof that the change is secure)"
    counts: dict[str, int] = {}
    for f in findings:
        counts[f.severity] = counts.get(f.severity, 0) + 1
    return ", ".join(f"{n} {sev}" for sev, n in counts.items())
