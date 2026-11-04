"""Reading a package before anything runs. It looks for what could hurt you, says where it found it, and turns it into plain words.

This is a static read, so it can be fooled: that is why even an approved skill only ever runs in the sandbox. Passing here is not proof a package is safe; failing here is a reason not to run it at all.
"""

from __future__ import annotations

import json
import re
import tomllib
from dataclasses import asdict, dataclass, field
from pathlib import Path

from app.safety.policy import redact_secrets

MAX_FILE = 600_000
ORDER = {"note": -1, "low": 0, "medium": 1, "high": 2, "critical": 3}
# What XENIO will never run: a Dockerfile, CI, tests, docs, examples, the project's own install script. What is in them is worth a note, not a verdict. (A real pentest repo was "blocked" for a curl in its Dockerfile, and for 127.0.0.1.)
_NEVER_RUN = re.compile(r"(^|/)(tests?|__tests__|spec|specs|docs?|examples?|samples?|demos?|benchmarks?|fixtures?|testdata|containers?|docker|\.github|\.gitlab|\.circleci|\.vscode|ci|deploy|deployment|infra|terraform|helm|k8s|kubernetes|website|site|assets|media|images)(/|$)|(^|/)(Dockerfile[^/]*|docker-compose[^/]*|compose\.ya?ml|Makefile|LICEN[SC]E[^/]*|CHANGELOG[^/]*|CONTRIBUTING[^/]*|README[^/]*|SECURITY[^/]*|AGENTS\.md|CLAUDE\.md)$|(^|/)scripts?/(install|setup|bootstrap|release|build|deploy|publish)[^/]*$|(^|/)(install|setup)\.(sh|ps1)$", re.I)
_INSTRUCTIONS = re.compile(r"(^|/)skill\.md$", re.I)
_SKIP_SKILL_DIRS = {"node_modules", ".git", ".venv", "venv", "__pycache__", "tests", "test", "dist", "build"}
_TEXT = {".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".py", ".sh", ".bash", ".zsh", ".rb", ".pl", ".php", ".go", ".rs", ".json", ".toml", ".yaml", ".yml", ".md", ".mdx", ".txt", ".html", ".cfg", ".ini", ".env", ""}
_DOCS = {".md", ".mdx", ".txt"}
_INSTALL_SCRIPTS = ("preinstall", "install", "postinstall", "prepare", "prepublish", "prepack", "postpack")
_BINARY_MAGIC = (b"\x7fELF", b"\xcf\xfa\xed\xfe", b"\xce\xfa\xed\xfe", b"\xca\xfe\xba\xbe", b"MZ")
_NOT_REGISTRY = re.compile(r"^(git\+|git:|https?:|file:|link:|github:|ssh:)|^[\w.-]+/[\w.-]+$")

# (severity, what it is, the pattern, in plain words)
RULES: list[tuple[str, str, re.Pattern, str]] = [
    ("critical", "pipes a download into a shell", re.compile(r"\b(curl|wget|fetch)\b[^\n|]{0,200}\|\s*(ba|z|da)?sh\b"), "it downloads a script and runs it straight away"),
    ("critical", "reads your SSH or cloud keys", re.compile(r"\.ssh/|id_rsa|id_ed25519|\.aws/credentials|\.gnupg|\.netrc|\.npmrc|\.pypirc|Library/Keychains|security\s+find-(generic|internet)-password"), "it goes looking for your private keys"),
    ("critical", "reads browser data", re.compile(r"Library/(Application Support/(Google/Chrome|Firefox)|Cookies|Safari)|Login Data|cookies\.sqlite"), "it reads what your browser stores"),
    ("critical", "reads XENIO's or an assistant's secrets", re.compile(r"\.hermes/(?:\.env|auth)|engineering/token\.json|\.claude/\.credentials|\.credentials\.json|XENIO_[A-Z_]*KEY|NABH_API_KEY"), "it goes after this assistant's own keys"),
    ("critical", "sends data to a throwaway address", re.compile(r"webhook\.site|requestbin|pipedream\.net|ngrok(-free)?\.(io|app)|pastebin\.com/api|discord(app)?\.com/api/webhooks|api\.telegram\.org/bot|https?://(?!127\.|0\.0\.0\.0|10\.|192\.168\.|172\.(?:1[6-9]|2\d|3[01])\.)\d{1,3}(\.\d{1,3}){3}"), "it talks to the kind of address used to collect stolen data"),
    ("critical", "makes itself start on its own", re.compile(r"LaunchAgents|LaunchDaemons|launchctl\s+(load|bootstrap)|crontab\s+-|/etc/(cron|rc\.)|\.(zshrc|bashrc|bash_profile|zprofile)\b"), "it sets itself up to keep running after it ends"),
    ("critical", "decodes something and runs it", re.compile(r"(eval|exec|Function)\s*\(\s*(atob|Buffer\.from|base64|b64decode|bytes\.fromhex|decodeURIComponent|unescape)|base64\s+(-d|-D|--decode)[^\n]{0,80}\|\s*(ba|z)?sh"), "it hides code and runs it"),
    ("high", "evaluates code it builds at run time", re.compile(r"\beval\s*\(|\bnew\s+Function\s*\(|\bvm\.run|\bpickle\.loads\b|\bmarshal\.loads\b|\bcompile\s*\([^)]*['\"]exec['\"]"), "it can run code that is not in the package"),
    ("high", "deletes widely", re.compile(r"\brm\s+-[a-z]*r[a-z]*f?\s+(/|~|\$HOME|\.\.)|\bshutil\.rmtree\s*\(\s*(['\"]/|os\.path\.expanduser)|\bchmod\s+-?R?\s*777\b|\bsudo\b"), "it deletes or opens up things outside its own folder"),
    ("high", "looks obfuscated", re.compile(r"[A-Za-z0-9+/=]{800,}|(?:\\x[0-9a-fA-F]{2}){40,}|(?:String\.fromCharCode\([^)]*\)[^\n]{0,8}){6,}|_0x[0-9a-f]{4,}"), "part of it is written so that a person cannot read it"),
    ("medium", "runs other programs", re.compile(r"\bchild_process\b|\bexecSync\b|\bspawnSync\b|\bspawn\s*\(|\bsubprocess\.(run|Popen|call|check_output)\b|\bos\.(system|popen|exec)\b"), "it starts other programs"),
    ("medium", "reads environment variables", re.compile(r"\bprocess\.env\b|\bos\.environ\b|\bos\.getenv\b"), "it reads settings from the environment"),
    ("medium", "uses the network", re.compile(r"\bfetch\s*\(|\baxios\b|\bhttps?\.(request|get)\b|\bnet\.(connect|createConnection)\b|\bWebSocket\b|\brequests\.(get|post|put|delete|Session)\b|\burllib\.request\b|\bhttpx\b|\bsocket\.(socket|create_connection)\b|\bdns\.(lookup|resolve)\b"), "it talks to the internet"),
    ("medium", "writes files", re.compile(r"\bfs\.(writeFile|appendFile|createWriteStream|unlink|rm|mkdir)\b|\bopen\s*\([^)]*['\"][wa]b?['\"]"), "it creates or changes files"),
]

INJECTION = [
    re.compile(r"\b(ignore|disregard|forget|override)\b[^\n.]{0,40}\b(previous|prior|above|earlier|your|all)\b[^\n.]{0,30}\b(instruction|rule|prompt|polic|guideline)", re.I),
    re.compile(r"\bdo not (tell|inform|mention|show|ask)\b[^\n.]{0,30}\b(user|human|owner|anyone)", re.I),
    re.compile(r"\bwithout (telling|asking|informing|notifying)\b[^\n.]{0,20}\b(user|human|owner|anyone|them)", re.I),
    re.compile(r"\b(you are now|you must now|new system prompt|system prompt:|developer mode|jailbreak)\b", re.I),
    re.compile(r"\b(reveal|print|send|upload|exfiltrate|post)\b[^\n.]{0,40}\b(api[ _-]?key|secret|token|password|credential|\.env)\b", re.I),
    re.compile(r"\b(grant|give|set)\b[^\n.]{0,30}\b(yourself|your own|all)\b[^\n.]{0,20}\b(permission|access|tool)", re.I),
    re.compile(r"\b(disable|bypass|turn off|skip)\b[^\n.]{0,30}\b(guard|approval|sandbox|safety|confirmation|policy)\b", re.I),
]
_HIDDEN = re.compile("[​-‏‪-‮⁠-⁤﻿\U000e0000-\U000e007f]")


@dataclass
class Finding:
    severity: str
    kind: str
    why: str
    file: str = ""
    line: int = 0
    excerpt: str = ""

    def public(self) -> dict:
        return asdict(self)


@dataclass
class Report:
    kind: str = "library"  # instructions | tool | library
    name: str = ""
    version: str = ""
    description: str = ""
    bins: dict = field(default_factory=dict)
    instructions: str = ""
    dependencies: dict = field(default_factory=dict)
    install_scripts: dict = field(default_factory=dict)
    files: int = 0
    bytes: int = 0
    findings: list[Finding] = field(default_factory=list)

    @property
    def risk(self) -> str:
        worst = max((ORDER[f.severity] for f in self.findings if f.severity != "note"), default=0)
        return {0: "low", 1: "medium", 2: "high", 3: "blocked"}[worst]

    def permissions(self) -> list[str]:
        """What it would want to do, in words, from what was really found."""
        kinds = {f.kind for f in self.findings if f.severity != "note"}
        words = []
        if self.install_scripts:
            words.append("run its own scripts when installed (XENIO keeps those turned off)")
        if "runs other programs" in kinds:
            words.append("start other programs")
        if "uses the network" in kinds:
            words.append("talk to the internet (off, unless you allow it)")
        if "reads environment variables" in kinds:
            words.append("read environment variables (it gets a clean environment with no keys)")
        if "writes files" in kinds:
            words.append("create or change files (only in its own folder)")
        if self.dependencies:
            words.append(f"bring {len(self.dependencies)} other package(s) with it")
        return words or ["read the files in its own folder"]

    def public(self) -> dict:
        data = asdict(self)
        data["risk"] = self.risk
        data["permissions"] = self.permissions()
        data["findings"] = sorted(data["findings"], key=lambda f: -ORDER[f["severity"]])[:60]
        return data


def inspect_package(root: Path) -> Report:
    root = Path(root)
    report = Report()
    _manifest(root, report)
    for path in sorted(root.rglob("*")):
        if path.is_symlink() or not path.is_file() or "node_modules" in path.parts:
            continue
        report.files += 1
        try:
            size = path.stat().st_size
        except OSError:
            continue
        report.bytes += size
        rel = path.relative_to(root).as_posix()
        try:
            with open(path, "rb") as fh:
                head = fh.read(4)
        except OSError:
            continue
        if head.startswith(_BINARY_MAGIC) or path.suffix in (".node", ".so", ".dylib", ".dll", ".exe", ".wasm"):
            report.findings.append(Finding("high", "contains a prebuilt program", "it includes a compiled program that cannot be read before it runs", rel))
            continue
        if path.suffix.lower() not in _TEXT or size > MAX_FILE:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="strict")
        except (OSError, UnicodeDecodeError):
            continue
        _scan(text, rel, path.suffix.lower(), report)
    _dedupe(report)
    if report.instructions and not report.bins:
        report.kind = "instructions"
    elif report.bins:
        report.kind = "tool"
    elif (root / "package.json").is_file() and report.name and not any((root / n).is_file() for n in ("README.md", "readme.md")):
        report.kind = "library"
    elif any(root.glob("README*")):
        report.kind = "reference"  # nothing to run: agents can read what it knows
    return report


def _manifest(root: Path, report: Report) -> None:
    manifest = root / "package.json"
    if manifest.is_file():
        try:
            data = json.loads(manifest.read_text(encoding="utf-8", errors="replace"))
        except ValueError:
            report.findings.append(Finding("high", "broken manifest", "its package.json cannot be read", "package.json"))
            data = {}
        if isinstance(data, dict):
            report.name, report.version = str(data.get("name") or ""), str(data.get("version") or "")
            report.description = str(data.get("description") or "")[:400]
            bins = data.get("bin")
            report.bins = {report.name.split("/")[-1]: bins} if isinstance(bins, str) else bins if isinstance(bins, dict) else {}
            scripts = data.get("scripts") if isinstance(data.get("scripts"), dict) else {}
            report.install_scripts = {k: str(v)[:400] for k, v in scripts.items() if k in _INSTALL_SCRIPTS}
            for name, command in report.install_scripts.items():
                report.findings.append(Finding("high", "runs code when installed", f"its {name} script would run on your Mac the moment it is installed", "package.json", 0, redact_secrets(command)[:200]))
                _scan(command, f"package.json ({name} script)", ".sh", report)
            deps = data.get("dependencies") if isinstance(data.get("dependencies"), dict) else {}
            report.dependencies = {str(k): str(v)[:80] for k, v in deps.items()}
            for name, version in report.dependencies.items():
                if _NOT_REGISTRY.search(version):
                    report.findings.append(Finding("high", "untrusted dependency", f"{name} comes from outside the npm registry, so nobody has vetted it", "package.json", 0, version[:120]))
            if len(report.dependencies) > 60:
                report.findings.append(Finding("medium", "many dependencies", f"it brings {len(report.dependencies)} other packages with it", "package.json"))
    for candidate in ("SKILL.md", "skill.md", "Skill.md"):
        path = root / candidate
        if path.is_file():
            text = path.read_text(encoding="utf-8", errors="replace")
            report.instructions = text
            found = re.match(r"^---\n(.*?)\n---", text, re.S)
            if found:
                for line in found.group(1).splitlines():
                    key, _, value = line.partition(":")
                    if key.strip() == "name" and not report.name:
                        report.name = value.strip().strip("'\"")[:80]
                    if key.strip() == "description" and not report.description:
                        report.description = value.strip().strip("'\"")[:400]
            break
    pyproject = root / "pyproject.toml"
    if pyproject.is_file():
        try:
            data = tomllib.loads(pyproject.read_text(encoding="utf-8", errors="replace"))
        except (ValueError, OSError):
            data = {}
        project = data.get("project") if isinstance(data.get("project"), dict) else {}
        poetry = ((data.get("tool") or {}).get("poetry") or {}) if isinstance(data.get("tool"), dict) else {}
        report.name = report.name or str(project.get("name") or poetry.get("name") or "")
        report.version = report.version or str(project.get("version") or poetry.get("version") or "")
        report.description = report.description or str(project.get("description") or poetry.get("description") or "")[:400]
        scripts = project.get("scripts") or poetry.get("scripts") or {}
        if isinstance(scripts, dict) and scripts:
            report.bins = {**report.bins, **{str(k): f"python:{k}" for k in scripts}}
        listed = project.get("dependencies") or []
        if isinstance(listed, list):
            report.dependencies = {**report.dependencies, **{re.split(r"[=<>!~\[ ;]", str(d), maxsplit=1)[0]: str(d)[:80] for d in listed}}
    requirements = root / "requirements.txt"
    if requirements.is_file() and not report.dependencies:
        lines = [line.split("#")[0].strip() for line in requirements.read_text(encoding="utf-8", errors="replace").splitlines()]
        report.dependencies = {re.split(r"[=<>!~\[ ;]", line, maxsplit=1)[0]: line for line in lines if line}
        for line in lines:
            if re.search(r"git\+|https?://|^-e\b|--index-url|--extra-index-url", line):
                report.findings.append(Finding("high", "untrusted dependency", "it installs something from outside PyPI", "requirements.txt", 0, line[:120]))


def _scan(text: str, rel: str, suffix: str, report: Report) -> None:
    before = len(report.findings)
    _scan_lines(text, rel, suffix, report)
    if _NEVER_RUN.search(rel) and not _INSTRUCTIONS.search(rel):
        for finding in report.findings[before:]:
            finding.severity = "note"


def _scan_lines(text: str, rel: str, suffix: str, report: Report) -> None:
    minified = suffix in (".js", ".mjs", ".cjs") and ".min." in rel
    for number, line in enumerate(text.splitlines(), 1):
        if len(line) > 20000 and not minified:
            report.findings.append(Finding("high", "looks obfuscated", "it has an enormously long line that a person cannot read", rel, number, line[:60] + "..."))
            line = line[:20000]
        if suffix not in _DOCS:
            for severity, kind, pattern, why in RULES:
                if minified and severity == "medium":
                    continue
                if pattern.search(line):
                    report.findings.append(Finding(severity, kind, why, rel, number, redact_secrets(line.strip())[:200]))
        else:
            for _severity, kind, pattern, why in RULES[:1] + RULES[1:7]:
                if kind in ("pipes a download into a shell", "decodes something and runs it") and pattern.search(line):
                    report.findings.append(Finding("high", kind, f"its instructions tell an agent that {why}", rel, number, redact_secrets(line.strip())[:200]))
        if _INSTRUCTIONS.search(rel) or (suffix in _DOCS and "/references/" in f"/{rel}") or (suffix in ("", ".json", ".yaml", ".yml") and "skill" in rel.lower()):  # what an agent would actually be given to read
            for pattern in INJECTION:
                if pattern.search(line):
                    report.findings.append(Finding("high", "tries to instruct the agent against you", "this text is written to change how an agent behaves, against your rules", rel, number, redact_secrets(line.strip())[:200]))
                    break
        if _HIDDEN.search(line) and suffix in _DOCS:
            report.findings.append(Finding("high", "hides text", "it contains invisible characters, a way to hide instructions from a person reading it", rel, number))


def _dedupe(report: Report) -> None:
    seen, kept, per = set(), [], {}
    for finding in report.findings:
        key = (finding.kind, finding.file, finding.line)
        count = per.get((finding.kind, finding.file), 0)
        if key in seen or count >= 3:
            continue
        seen.add(key)
        per[(finding.kind, finding.file)] = count + 1
        kept.append(finding)
    report.findings = kept


def read_front(text: str) -> dict[str, str]:
    found = re.match(r"^---\n(.*?)\n---", text, re.S)
    out: dict[str, str] = {}
    for line in (found.group(1).splitlines() if found else []):
        key, _, value = line.partition(":")
        if key.strip() in ("name", "description") and value.strip():
            out[key.strip()] = value.strip().strip("'\"")[:400]
    return out


def find_skills(root: Path) -> list[dict]:
    """Every skill in a repository. Many repositories hold a whole collection: skills/pdf/SKILL.md, skills/design/SKILL.md, and so on."""
    root = Path(root)
    found = []

    def walk(folder: Path, depth: int) -> None:
        try:
            entries = sorted(folder.iterdir())
        except OSError:
            return
        here = next((e for e in entries if e.is_file() and e.name.lower() == "skill.md"), None)
        if here is not None:
            try:
                front = read_front(here.read_text(encoding="utf-8", errors="replace"))
            except OSError:
                front = {}
            found.append({"folder": folder.relative_to(root).as_posix() if folder != root else "", "name": front.get("name") or folder.name, "description": front.get("description", "")})
            return  # a skill's own subfolders are part of it
        if depth >= 6:
            return
        for entry in entries:
            if entry.is_dir() and not entry.is_symlink() and entry.name not in _SKIP_SKILL_DIRS and not (entry.name.startswith(".") and entry.name not in (".claude", ".codex", ".cursor", ".agents", ".skills")):
                walk(entry, depth + 1)

    walk(root, 0)
    return found
