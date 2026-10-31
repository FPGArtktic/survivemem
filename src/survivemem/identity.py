# SPDX-License-Identifier: GPL-3.0-or-later
"""Project identity (§9): which project the cwd belongs to, and which clone of it this is."""

import hashlib
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import tomllib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from functools import cache
from importlib import resources
from pathlib import Path
from urllib.parse import urlsplit

from survivemem import store
from survivemem.config import Config
from survivemem.db import transaction
from survivemem.model import Checkout, Project

log = logging.getLogger(__name__)

GIT_TIMEOUT = 2.0
# First contact with a kernel-sized history; on timeout the root identity is skipped.
ROOT_TIMEOUT = 3.0
DEFAULT_PORTS = frozenset({22, 80, 443, 9418})
SSH_ALIAS_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
OTHERS_SHOWN = 5

SshResolver = Callable[[str], tuple[str, int | None]]


# ---------------------------------------------------------------------------------------------
# Remote URL normalisation


def ssh_resolve(alias: str) -> tuple[str, int | None]:
    """Real hostname and port of an SSH Host alias via `ssh -G` (reads config, no connection)."""
    if not SSH_ALIAS_RE.match(alias) or shutil.which("ssh") is None:
        return alias, None
    try:
        result = subprocess.run(
            ["ssh", "-G", alias], capture_output=True, text=True, timeout=1.0, check=True
        )
    except (OSError, subprocess.SubprocessError):
        return alias, None
    host, port = alias, None
    for line in result.stdout.splitlines():
        key, _, value = line.partition(" ")
        if key == "hostname" and value:
            host = value.strip()
        elif key == "port" and value.strip().isdigit():
            port = int(value)
    return host, port


def _is_scp_like(url: str) -> bool:
    # git: scp-like syntax only when there is no slash before the first colon
    head, sep, _ = url.partition(":")
    return bool(sep and head and "/" not in head)


def ssh_host(url: str) -> str | None:
    """Host part of an SSH remote (what `ssh -G` resolves), None for other transports."""
    if url.startswith(("ssh://", "git+ssh://", "ssh+git://")):
        authority = url.split("://", 1)[1].split("/", 1)[0]
        return authority.rpartition("@")[2].split(":")[0]
    if "://" not in url and _is_scp_like(url):
        return url.partition(":")[0].rpartition("@")[2]
    return None


def _file_identity(path: str, base: Path | None) -> str:
    p = Path(os.path.expanduser(path))
    if not p.is_absolute():
        p = (base or Path.cwd()) / p
    text = os.path.normpath(p).rstrip("/").removesuffix(".git").rstrip("/")
    return f"file:{text}"


def _authority(host: str, port: int | None, hosts: Mapping[str, str]) -> str:
    keep_port = port is not None and port not in DEFAULT_PORTS
    authority = f"{host}:{port}" if keep_port else host
    if authority in hosts:
        return hosts[authority].lower()
    if host in hosts:
        mapped = hosts[host].lower()
        return f"{mapped}:{port}" if keep_port else mapped
    return authority


def normalise_remote_url(
    url: str,
    *,
    base: Path | None = None,
    hosts: Mapping[str, str] | None = None,
    resolve: SshResolver | None = ssh_resolve,
) -> str:
    """Scheme, credentials, default ports and .git dropped; host lowercased, path kept."""
    url = url.strip()
    if url.startswith("file://"):
        return _file_identity(url.removeprefix("file://"), base)
    if "://" in url:
        parts = urlsplit(url)
        host, port, path = (parts.hostname or "").lower(), parts.port, parts.path
        is_ssh = parts.scheme in ("ssh", "git+ssh", "ssh+git")
    elif _is_scp_like(url):
        userhost, _, path = url.partition(":")
        host, port, is_ssh = userhost.rpartition("@")[2].lower(), None, True
    else:
        return _file_identity(url, base)
    if is_ssh and resolve is not None:
        real_host, real_port = resolve(host)
        host, port = real_host.lower(), port if port is not None else real_port
    path = path.strip("/").removesuffix(".git").rstrip("/")
    return f"{_authority(host, port, hosts or {})}/{path}"


# ---------------------------------------------------------------------------------------------
# Collecting identities from the working tree


@dataclass(frozen=True, slots=True)
class Location:
    root: Path
    common_dir: Path | None
    remotes: tuple[tuple[str, str], ...]
    name: str | None
    branch: str | None
    head: str | None
    config_sha: str | None

    @property
    def is_git(self) -> bool:
        return self.common_dir is not None


def _git(cwd: Path, *args: str, timeout: float = GIT_TIMEOUT) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(cwd), *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError:
        return None
    except subprocess.TimeoutExpired:
        log.warning("git %s timed out after %.1fs in %s", " ".join(args), timeout, cwd)
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def _order_remotes(remotes: Mapping[str, str], primary: str) -> tuple[tuple[str, str], ...]:
    def priority(name: str) -> tuple[int, str]:
        return (0 if name == primary else 1 if name == "upstream" else 2, name)

    return tuple((name, remotes[name]) for name in sorted(remotes, key=priority))


def _parse_remotes(text: str, primary: str) -> tuple[tuple[str, str], ...]:
    # `git remote -v` prints URLs after insteadOf rewriting, like `git remote get-url`.
    remotes: dict[str, str] = {}
    for line in text.splitlines():
        fields = line.split()
        if len(fields) == 3 and fields[2] == "(fetch)":
            remotes[fields[0]] = fields[1]
    return _order_remotes(remotes, primary)


def _toml_name(path: Path) -> str | None:
    try:
        with path.open("rb") as f:
            value = tomllib.load(f).get("project")
    except (OSError, tomllib.TOMLDecodeError):
        return None
    return str(value) if value else None


def file_sha256(path: Path) -> str | None:
    try:
        with path.open("rb") as f:
            return hashlib.file_digest(f, "sha256").hexdigest()
    except OSError:
        return None


def collect(cwd: Path, cfg: Config) -> Location | None:
    out = _git(cwd, "rev-parse", "--show-toplevel", "--git-common-dir")
    lines = out.splitlines() if out else []
    if len(lines) < 2:
        return _collect_plain(cwd)
    root = Path(lines[0])
    return Location(
        root=root,
        common_dir=(cwd / lines[1]).resolve(),
        remotes=_parse_remotes(_git(root, "remote", "-v") or "", cfg.identity_remote),
        name=_toml_name(root / ".survivemem.toml"),
        branch=_git(root, "symbolic-ref", "--short", "-q", "HEAD") or "detached",
        head=_git(root, "rev-parse", "-q", "--verify", "HEAD"),
        config_sha=file_sha256(root / ".config"),
    )


def _collect_plain(cwd: Path) -> Location | None:
    """Outside git: a .survivemem.toml above cwd, or a Quartus .qpf in cwd (§15)."""
    for directory in (cwd, *cwd.parents):
        name = _toml_name(directory / ".survivemem.toml")
        if name:
            return Location(directory, None, (), name, None, None, None)
    qpf = sorted(cwd.glob("*.qpf"))
    if qpf:
        return Location(cwd, None, (), qpf[0].stem, None, None, None)
    return None


def root_commit(loc: Location) -> str | None:
    """Oldest root: newer roots appear when a subtree is squashed in (u-boot has four)."""
    if not loc.is_git or not loc.head:
        return None
    # In a shallow clone the parentless commits are the depth boundary, not the real root, and
    # they move with --depth: no root identity is better than a wrong one.
    if _git(loc.root, "rev-parse", "--is-shallow-repository") == "true":
        return None
    out = _git(loc.root, "rev-list", "--max-parents=0", "--reverse", "HEAD", timeout=ROOT_TIMEOUT)
    return out.splitlines()[0] if out else None


def remote_identities(loc: Location, cfg: Config) -> list[str]:
    return [
        "remote:" + normalise_remote_url(url, base=loc.root, hosts=cfg.hosts)
        for _, url in loc.remotes
    ]


def path_identity(loc: Location) -> str:
    return f"path:{loc.common_dir if loc.common_dir else loc.root}"


def identities(loc: Location, cfg: Config) -> list[str]:
    ids = [f"name:{loc.name}"] if loc.name else []
    return [*ids, *remote_identities(loc, cfg), path_identity(loc)]


@cache
def families() -> Mapping[str, str]:
    text = resources.files("survivemem").joinpath("families.toml").read_text()
    roots = tomllib.loads(text).get("roots", {})
    return {str(sha): str(label) for sha, label in roots.items()}


# ---------------------------------------------------------------------------------------------
# Resolution (§9 "first hit wins")


@dataclass(frozen=True, slots=True)
class Resolution:
    project: Project | None
    step: str
    location: Location | None = None
    identities: tuple[str, ...] = ()
    root: str | None = None
    family: str | None = None
    hint: str | None = None
    checkout: Checkout | None = None

    @property
    def project_id(self) -> str | None:
        return self.project.id if self.project else None


def _first_owner(db: sqlite3.Connection, ids: Sequence[str]) -> Project | None:
    for identity in ids:
        owner = store.find_identity(db, identity)
        if owner:
            return store.get_project(db, owner.project_id)
    return None


def _by_root(
    db: sqlite3.Connection, loc: Location, ids: Sequence[str], cfg: Config, root: str | None
) -> tuple[Project | None, str, str | None]:
    """Steps 4–6: root match (adopt, or propose for families), path fallback, new project."""
    owner = _first_owner(db, [f"root:{root}"]) if root else None
    family = families().get(root or "")
    # Kernel-sized families: forks are usually distinct projects; anything else: same project.
    mode = cfg.family_root_match.get(family, "propose") if family else cfg.root_match
    if owner and mode == "adopt":
        return owner, "root", None
    by_path = _first_owner(db, [path_identity(loc)])
    if by_path:
        return by_path, "path", None
    hint = None
    if owner:
        alias = next((i for i in ids if i.startswith("remote:")), path_identity(loc))
        hint = (
            f"root commit matches project {owner.label} — run: "
            f"survivemem projects alias add {owner.id} {alias}"
        )
    return None, "created", hint


def resolve(
    db: sqlite3.Connection,
    cwd: Path,
    cfg: Config,
    *,
    writable: bool = True,
    env: Mapping[str, str] | None = None,
) -> Resolution:
    """Resolve and record. writable=False only looks up: no new project, identity or clone row
    (for a read-only DB file, where the prompt hook must still be able to inject memories)."""
    env = os.environ if env is None else env
    forced = env.get("SURVIVEMEM_PROJECT")
    loc = collect(cwd, cfg)
    if forced:
        return _resolve_forced(db, forced, loc, writable)
    if loc is None:
        return Resolution(None, "none")
    ids = identities(loc, cfg)
    if not writable:
        return _lookup_location(db, loc, ids)
    # The history walk can take seconds on first contact; never hold the write lock over it.
    root = None if _known_cheaply(db, loc, ids) else root_commit(loc)
    with transaction(db):
        return _resolve_location(db, loc, ids, cfg, root)


def _known_cheaply(db: sqlite3.Connection, loc: Location, ids: list[str]) -> bool:
    """Would the name, remote or (for a repo without remotes) path step hit?"""
    cheap = [i for i in ids if i.startswith(("name:", "remote:"))]
    if not any(i.startswith("remote:") for i in ids):
        cheap.append(path_identity(loc))
    return _first_owner(db, cheap) is not None


def resolve_tolerant(db: sqlite3.Connection, cwd: Path, cfg: Config) -> Resolution:
    """resolve(), falling back to a read-only lookup when the DB cannot be written."""
    try:
        return resolve(db, cwd, cfg)
    except sqlite3.OperationalError as e:
        log.warning("resolution without writes (%s)", e)
        return resolve(db, cwd, cfg, writable=False)


def _resolve_forced(
    db: sqlite3.Connection, forced: str, loc: Location | None, writable: bool
) -> Resolution:
    project = store.find_project(db, forced)
    if not writable:
        return Resolution(project, "env", loc, checkout=_existing_checkout(db, loc))
    with transaction(db):
        if project is None:
            project = store.create_project(db, forced)
            store.attach_identities(db, project.id, [f"name:{forced}"])
        checkout = _record(db, project, loc) if loc else None
    return Resolution(project, "env", loc, checkout=checkout)


def _lookup_location(db: sqlite3.Connection, loc: Location, ids: list[str]) -> Resolution:
    project = _first_owner(db, ids)
    step = "lookup" if project else "none"
    return Resolution(project, step, loc, tuple(ids), checkout=_existing_checkout(db, loc))


def _existing_checkout(db: sqlite3.Connection, loc: Location | None) -> Checkout | None:
    if loc is None:
        return None
    try:
        return store.get_checkout_by_path(db, str(loc.root))
    except store.NotFound:
        return None


def _resolve_location(
    db: sqlite3.Connection, loc: Location, ids: list[str], cfg: Config, walked: str | None
) -> Resolution:
    root, hint = None, None
    project, step = (_first_owner(db, ids[:1]), "name") if loc.name else (None, "remote")
    if project is None:
        remotes = [i for i in ids if i.startswith("remote:")]
        project, step = _first_owner(db, remotes), "remote"
        if project is None and not remotes:
            # Without remotes the path is the only cheap signal; checking it first keeps the
            # full-history walk of `git rev-list` to the first contact.
            project, step = _first_owner(db, [path_identity(loc)]), "path"
        if project is None:
            root = walked
            project, step, hint = _by_root(db, loc, ids, cfg, root)
    if project is None:
        project, step = store.create_project(db, loc.name or loc.root.name), "created"
    attach = [*ids, f"root:{root}"] if root else ids
    store.attach_identities(db, project.id, attach)
    return Resolution(
        project,
        step,
        loc,
        tuple(attach),
        root,
        families().get(root or ""),
        hint,
        _record(db, project, loc),
    )


def _record(db: sqlite3.Connection, project: Project, loc: Location) -> Checkout:
    state = store.CheckoutState(str(loc.root), loc.branch, loc.head, loc.config_sha)
    return store.upsert_checkout(db, project.id, state)


# ---------------------------------------------------------------------------------------------
# Rendering


def abbrev_home(path: str) -> str:
    home = str(Path.home())
    if path == home:
        return "~"
    return "~" + path[len(home) :] if path.startswith(home + "/") else path


def describe_checkout(c: Checkout) -> str:
    """'~/w/linux-de10 @socfpga-6.6 (abc1234, .config 9f3e…)'."""
    text = f"{abbrev_home(c.path)} @{c.last_branch or 'detached'}"
    details = [c.last_head[:7]] if c.last_head else []
    if c.last_config:
        details.append(f".config {c.last_config[:4]}…")
    return f"{text} ({', '.join(details)})" if details else text


def scope_for(res: Resolution, scope: str | None) -> str:
    """'project', 'branch' or 'checkout' as given by a user → stored scope for this clone."""
    if scope in (None, "", "project"):
        return "project"
    loc = res.location
    if scope == "branch" and loc and loc.branch not in (None, "detached"):
        return f"branch:{loc.branch}"
    if scope == "checkout" and res.checkout:
        return f"checkout:{res.checkout.id}"
    if scope and ":" in scope:
        return store.validate_scope(scope)
    raise store.Invalid(f"scope {scope!r} needs a git checkout (on a branch for 'branch')")


def scope_suffix(scope: str, checkouts: Mapping[str, Checkout]) -> str:
    """'[branch socfpga-6.6]' / '[clone linux-de10]' / '[clone gone]'; empty for project scope."""
    kind, _, value = scope.partition(":")
    if kind == "branch":
        return f"[branch {value}]"
    if kind != "checkout":
        return ""
    c = checkouts.get(value)
    if c is None or not c.present:
        return "[clone gone]"
    return f"[clone {c.label or Path(c.path).name}]"


def checkout_line(db: sqlite3.Connection, res: Resolution, *, always: bool = False) -> str | None:
    """'linux · this clone … · others: …' when the project has more than one clone on disk."""
    if res.project is None or res.checkout is None:
        return None
    others = [
        c for c in store.list_checkouts(db, res.project.id) if c.present and c.id != res.checkout.id
    ]
    if not others and not always:
        return None
    line = f"{res.project.label} · this clone {describe_checkout(res.checkout)}"
    if not others:
        return line
    shown = ", ".join(
        f"{abbrev_home(c.path)} @{c.last_branch or 'detached'}" for c in others[:OTHERS_SHOWN]
    )
    extra = f", +{len(others) - OTHERS_SHOWN}" if len(others) > OTHERS_SHOWN else ""
    return f"{line} · others: {shown}{extra}"
