"""Where a skill comes from, and getting it without running any of it. ``npm pack`` and a shallow clone with hooks off only copy files. npx is treated as what it really is: an npm package with a command. It is never simply run."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tarfile
from dataclasses import asdict, dataclass, field
from pathlib import Path

from app.foundry import sandbox

MAX_FILES = 20_000
MAX_BYTES = 300 * 1024 * 1024
_NPM_NAME = r"(?:@[a-z0-9][a-z0-9._-]*/)?[a-z0-9][a-z0-9._-]*"
_VERSION = r"[A-Za-z0-9][A-Za-z0-9._+-]*"
_GITHUB_URL = re.compile(r"(?:https?://)?github\.com/([A-Za-z0-9][A-Za-z0-9-]*)/([A-Za-z0-9._-]+?)(?:\.git)?(?:/tree/([\w./-]+?))?/?(?=$|[\s,.;)])", re.I)
_OWNER_REPO = re.compile(r"(?<![\w@/.-])([A-Za-z0-9][A-Za-z0-9-]*)/([A-Za-z0-9._-]+)(?![\w/-])")
_NPX = re.compile(r"\bnpx\s+((?:-{1,2}[\w-]+\s+)*)(" + _NPM_NAME + r")(?:@(" + _VERSION + r"))?", re.I)
# `npx skills add owner/repo --skill name`, as the skills.sh directory gives it. `skills` there is the installer, not the skill
_SKILLS_TOOL = re.compile(r"\b(?:npx|bunx|pnpx|pnpm\s+dlx|yarn\s+dlx)\s+(?:-{1,2}[\w-]+\s+)*skills(?:@[\w.-]+)?(?=\s|$)(.*)$", re.I)
_SKILLS_SH = re.compile(r"(?:https?://)?(?:www\.)?skills\.sh/([A-Za-z0-9][A-Za-z0-9-]*)/([A-Za-z0-9._-]+?)(?:/([\w.-]+))?/?(?=$|[\s,;)])", re.I)
_SKILL_NAME = re.compile(r"[A-Za-z0-9][\w. -]{0,80}")
_NPM_WORD = re.compile(r"\b(?:npm|node)\s+(?:package|module|skill)\s+[`'\"]?(" + _NPM_NAME + r")(?:@(" + _VERSION + r"))?", re.I)
_NPM_ANY = re.compile(r"[`'\"]?(?<![\w/.-])(@[a-z0-9][a-z0-9._-]*/[a-z0-9][a-z0-9._-]*)(?:@(" + _VERSION + r"))?[`'\"]?")
_LOCAL = re.compile(r"(?<![\w])((?:~|/)[\w./ -]*[\w./-])")
_DANGEROUS_NPX_FLAGS = {"-c", "--call", "-p", "--package", "--shell", "--shell-auto-fallback"}
_NOT_NAMES = {"this", "that", "the", "a", "an", "my", "it", "as", "to", "and", "for", "with", "skill", "package", "module", "github", "npm", "npx", "install", "use", "add", "run", "or"}
_REFUSED_LOCAL = (".ssh", ".aws", ".gnupg", ".hermes", "Library/Keychains", "Library/Cookies")


class SkillSourceError(Exception):
    pass


@dataclass
class Source:
    kind: str  # github | npm | npx | local
    name: str
    ref: str = ""
    subpath: str = ""
    path: str = ""
    command: str = ""
    skills: list[str] = field(default_factory=list)  # the ones named in `npx skills add owner/repo --skill name`

    def public(self) -> dict:
        return asdict(self)

    def describe(self) -> str:
        if self.kind == "github":
            return f"github.com/{self.name}" + (f"@{self.ref}" if self.ref else "") + (f"/{self.subpath}" if self.subpath else "") + (f" ({', '.join(self.skills)})" if self.skills else "")
        if self.kind in ("npm", "npx"):
            return f"{self.kind} {self.name}" + (f"@{self.ref}" if self.ref else "")
        return self.path


def _repository(text: str) -> tuple[str, str, str, list[str]] | None:
    """A skills repository, in any way the `skills` tool accepts one: owner, repo, ref and folder, and any skill it names."""
    text = text.strip("`'\"")
    page = _SKILLS_SH.fullmatch(text)
    if page:
        return page.group(1), page.group(2), "", [page.group(3)] if page.group(3) else []
    ssh = re.fullmatch(r"git@github\.com:([\w-]+)/([\w.-]+?)(?:\.git)?", text)
    if ssh:
        return ssh.group(1), ssh.group(2), "", []
    link = _GITHUB_URL.fullmatch(text)
    if link:
        ref, _, folder = (link.group(3) or "").partition("/")
        return link.group(1), link.group(2), f"{ref}\0{folder}", []
    pair = re.fullmatch(r"([A-Za-z0-9][A-Za-z0-9-]*)/([A-Za-z0-9._-]+?)(?:\.git)?(?:@([\w.-]+))?", text)
    if pair:
        return pair.group(1), pair.group(2), "", [pair.group(3)] if pair.group(3) else []
    return None


def _from_skills_tool(tail: str) -> Source:
    """`npx skills add owner/repo --skill name` is how skills are shared. `skills` there is the installer. What is wanted is the skill it names."""
    tokens = [t.strip("`'\"") for t in tail.split()]
    if tokens and tokens[0].lower() in ("add", "install", "i", "a"):
        tokens = tokens[1:]
    elif tokens and not tokens[0].startswith("-") and "/" not in tokens[0]:
        raise SkillSourceError(f"`npx skills {tokens[0]}` is a command of the skills tool, not a skill. Give a GitHub link, or `npx skills add owner/repo --skill name`")
    where, named, i = "", [], 0
    while i < len(tokens):
        token = tokens[i]
        i += 1
        if token in ("--skill", "-s", "--agent", "-a"):
            values = []
            while i < len(tokens) and not tokens[i].startswith("-"):
                values.append(tokens[i])
                i += 1
            if token in ("--skill", "-s"):
                named += values
        elif token.startswith("--skill="):
            named += token.split("=", 1)[1].split(",")
        elif not token.startswith("-") and not where:
            where = token
    if not where:
        raise SkillSourceError("`npx skills` is the tool that installs skills, not a skill. Say which one: `npx skills add owner/repo --skill name`, or paste a GitHub link")
    found = _repository(where)
    if found is None:
        raise SkillSourceError(f"I could not tell which repository `{where[:80]}` is. Give a GitHub link, or owner/repo")
    owner, repo, place, also = found
    ref, _, folder = place.partition("\0")
    wanted = [n for n in dict.fromkeys([*also, *(v.strip() for v in named)]) if _SKILL_NAME.fullmatch(n)]
    return Source("github", f"{owner}/{repo}", ref, folder, skills=wanted)


def identify(text: str) -> Source:
    """Which package is meant. Only a plain name is ever taken from what was typed: nothing here reaches a shell."""
    words = " ".join(str(text or "").split())
    if not words:
        raise SkillSourceError("which skill? Give a GitHub link, an npm package name, or an npx command")
    if re.search(r"\bnpx\b.*(?:" + "|".join(re.escape(f) for f in _DANGEROUS_NPX_FLAGS) + r")(?:\s|=)", words):
        raise SkillSourceError("that npx command runs something other than a plain package, so XENIO will not use it. Name the package instead")
    tool = _SKILLS_TOOL.search(words)
    if tool:
        return _from_skills_tool(tool.group(1))
    page = _SKILLS_SH.search(words)
    if page:
        return Source("github", f"{page.group(1)}/{page.group(2)}", skills=[page.group(3)] if page.group(3) else [])
    npx = _NPX.search(words)
    if npx and npx.group(2).lower() not in _NOT_NAMES:
        return Source("npx", npx.group(2).lower(), npx.group(3) or "", command=npx.group(2).split("/")[-1])
    github = _GITHUB_URL.search(words)
    if github:
        tail = github.group(3) or ""
        ref, _, subpath = tail.partition("/")
        return Source("github", f"{github.group(1)}/{github.group(2)}", ref, subpath)
    for npm in _NPM_WORD.finditer(words):
        if npm.group(1).lower() not in _NOT_NAMES:
            return Source("npm", npm.group(1).lower(), npm.group(2) or "")
    scoped = _NPM_ANY.search(words)
    if scoped:
        return Source("npm", scoped.group(1).lower(), scoped.group(2) or "")
    local = _LOCAL.search(words)
    if local and Path(local.group(1).strip()).expanduser().exists():
        return Source("local", Path(local.group(1).strip()).name, path=str(Path(local.group(1).strip()).expanduser()))
    pair = _OWNER_REPO.search(words)
    if pair and pair.group(1).lower() not in _NOT_NAMES and pair.group(2).lower() not in _NOT_NAMES:  # skills come from GitHub: owner/repo is enough
        return Source("github", f"{pair.group(1)}/{pair.group(2)}")
    single = re.search(r"\b(?:install|add|use)\s+[`'\"]?(" + _NPM_NAME + r")(?:@(" + _VERSION + r"))?[`'\"]?\s*(?:as a skill|skill|package)?\b", words)
    if single and single.group(1).lower() not in _NOT_NAMES:
        return Source("npm", single.group(1).lower(), single.group(2) or "")
    raise SkillSourceError("I could not tell which package that is. Give a GitHub link (github.com/owner/repo), an npm package name, or an npx command")


def tree_hash(root: Path) -> str:
    """A fingerprint of the installed files, checked again every time the skill is used."""
    digest = hashlib.sha256()
    for path in sorted(Path(root).rglob("*")):
        rel = path.relative_to(root).as_posix()
        if ".git/" in f"{rel}/" or path.is_dir() or "node_modules/.package-lock.json" in rel:
            continue
        digest.update(rel.encode())
        if path.is_symlink():
            digest.update(os.readlink(path).encode())
        elif path.is_file():
            digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def safe_extract(tarball: Path, into: Path) -> None:
    """A package is a file from a stranger: paths cannot climb out, links cannot point out, and it cannot be enormous."""
    into.mkdir(parents=True, exist_ok=True)
    top = into.resolve()
    with tarfile.open(tarball) as archive:
        members = archive.getmembers()
        if len(members) > MAX_FILES or sum(m.size for m in members) > MAX_BYTES:
            raise SkillSourceError("that package is too big to inspect")
        for member in members:
            target = (top / member.name).resolve()
            if not target.is_relative_to(top):
                raise SkillSourceError(f"that package tries to write outside its folder ({member.name[:80]})")
            if member.issym() or member.islnk():
                raise SkillSourceError(f"that package contains a link ({member.name[:80]}), which XENIO does not accept")
            if not (member.isfile() or member.isdir()):
                raise SkillSourceError(f"that package contains a special file ({member.name[:80]})")
        archive.extractall(into, filter="data")


def _env(work: Path) -> dict:
    home = work / "home"
    home.mkdir(parents=True, exist_ok=True)
    return {"HOME": str(home), "TMPDIR": str(work), "npm_config_cache": str(work / "cache"), "npm_config_ignore_scripts": "true", "npm_config_update_notifier": "false", "npm_config_audit": "false", "npm_config_fund": "false", "GIT_TERMINAL_PROMPT": "0", "GIT_LFS_SKIP_SMUDGE": "1", "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null"}


def fetch(source: Source, work: Path, runner=sandbox.run) -> dict:
    """The files, in ``work/package``, with what is known about where they came from. Nothing from the package has run."""
    work.mkdir(parents=True, exist_ok=True)
    if source.kind in ("npm", "npx"):
        return _fetch_npm(source, work, runner)
    if source.kind == "github":
        return _fetch_github(source, work, runner)
    return _fetch_local(source, work)


def _fetch_npm(source: Source, work: Path, runner) -> dict:
    npm = shutil.which("npm")
    if npm is None:
        raise SkillSourceError("npm is not installed on this Mac")
    spec = f"{source.name}@{source.ref}" if source.ref else source.name
    roots = sandbox.tool_roots("node", "npm")
    info = runner([npm, "view", spec, "--json"], work, [work], readable=roots, network=True, timeout=60.0, env=_env(work))
    if not info.ok:
        raise SkillSourceError(f"npm does not know {spec}: {info.output.strip()[-200:]}")
    try:
        meta = json.loads(info.output)
        meta = meta[-1] if isinstance(meta, list) else meta
    except ValueError:
        raise SkillSourceError("npm's answer could not be read") from None
    version = str(meta.get("version") or "")
    packed = work / "packed"
    packed.mkdir(exist_ok=True)
    done = runner([npm, "pack", f"{source.name}@{version}", "--ignore-scripts", "--pack-destination", str(packed), "--json"], work, [work], readable=roots, network=True, timeout=180.0, env=_env(work))
    if not done.ok:
        raise SkillSourceError(f"the package could not be fetched: {done.output.strip()[-200:]}")
    tarballs = list(packed.glob("*.tgz"))
    if not tarballs:
        raise SkillSourceError("npm did not produce a package file")
    published = str((meta.get("dist") or {}).get("integrity") or "")
    actual = hashlib.sha512(tarballs[0].read_bytes()).digest()
    import base64

    ours = "sha512-" + base64.b64encode(actual).decode()
    if published.startswith("sha512-") and published != ours:
        raise SkillSourceError("the package does not match the fingerprint npm publishes for it, so it was thrown away")
    safe_extract(tarballs[0], work / "unpacked")
    shutil.move(str(work / "unpacked" / "package"), str(work / "package"))
    return {"version": version, "integrity": published or ours, "license": str(meta.get("license") or ""), "description": str(meta.get("description") or "")[:400], "homepage": str(meta.get("homepage") or "")[:200], "maintainers": len(meta.get("maintainers") or []), "published": str((meta.get("time") or {}).get("modified") or "") if isinstance(meta.get("time"), dict) else ""}


def _fetch_github(source: Source, work: Path, runner) -> dict:
    git = shutil.which("git")
    if git is None:
        raise SkillSourceError("git is not installed on this Mac")
    target = work / "repo"
    args = [git, "-c", "core.hooksPath=/dev/null", "-c", "core.symlinks=false", "-c", "protocol.allow=never", "-c", "protocol.https.allow=always", "clone", "--depth", "1", "--no-tags", "--single-branch", "--no-recurse-submodules"]
    if source.ref:
        args += ["--branch", source.ref]
    args += [f"https://github.com/{source.name}.git", str(target)]
    done = runner(args, work, [work], readable=sandbox.tool_roots("git"), network=True, timeout=180.0, env=_env(work))
    if not done.ok:
        raise SkillSourceError(f"{source.name} could not be fetched from GitHub: {done.output.strip()[-200:]}")
    head = runner([git, "-c", f"safe.directory={target}", "rev-parse", "HEAD"], target, [work], readable=sandbox.tool_roots("git"), timeout=20.0, env=_env(work))
    commit = head.output.strip().splitlines()[-1] if head.ok and head.output.strip() else ""
    shutil.rmtree(target / ".git", ignore_errors=True)
    base = (target / source.subpath).resolve() if source.subpath else target.resolve()
    if not base.is_relative_to(target.resolve()) or not base.is_dir():
        raise SkillSourceError(f"{source.subpath} is not a folder in that repository")
    _no_links_out(base)
    shutil.move(str(base), str(work / "package"))
    return {"version": commit[:12] or source.ref or "unknown", "integrity": f"git-{commit}", "license": "", "description": "", "homepage": f"https://github.com/{source.name}", "maintainers": 0, "published": ""}


def _fetch_local(source: Source, work: Path) -> dict:
    origin = Path(source.path).expanduser().resolve()
    home = Path.home().resolve()
    if origin == home or origin == Path("/") or any(str(origin).startswith(str(home / p)) for p in _REFUSED_LOCAL):
        raise SkillSourceError("that folder holds private things, so XENIO will not treat it as a skill")
    if not origin.is_dir():
        raise SkillSourceError(f"{source.path} is not a folder")
    size = sum(p.stat().st_size for p in origin.rglob("*") if p.is_file() and not p.is_symlink())
    if size > MAX_BYTES:
        raise SkillSourceError("that folder is too big to inspect")
    shutil.copytree(origin, work / "package", symlinks=True, ignore=shutil.ignore_patterns(".git", "node_modules", ".venv", "__pycache__"))
    _no_links_out(work / "package")
    return {"version": tree_hash(work / "package")[:12], "integrity": "", "license": "", "description": "", "homepage": "", "maintainers": 0, "published": ""}


def _no_links_out(root: Path) -> None:
    top = Path(root).resolve()
    for path in Path(root).rglob("*"):
        if path.is_symlink() and not Path(os.path.realpath(path)).is_relative_to(top):
            raise SkillSourceError(f"it contains a link pointing outside itself ({path.name[:60]})")
