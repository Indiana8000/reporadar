#!/usr/bin/env python3
"""Overview of the state of your local Git repos, in the browser.

Standard library only. The HTML is generated in memory and served by a
local server (127.0.0.1); no file is written.
Ctrl+C stops the program.

Examples:
    python reporadar.py C:\\Users\\me\\code D:\\work
    python reporadar.py . --depth 2 --fetch
"""
import argparse
import hashlib
import html
import json
import os
import re
import secrets
import shlex
import subprocess
import threading
import time
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

SKIP_DIRS = {"node_modules", ".venv", "venv", "__pycache__", "target", "dist", "build"}

# Order = priority (most important state first)
STATES = ["error", "conflict", "diverged", "dirty", "unpushed", "behind", "local", "clean"]
ICON = {"error": "!", "conflict": "✕", "diverged": "⇅", "dirty": "●",
        "unpushed": "↑", "behind": "↓", "local": "○", "clean": "✓"}
LABEL = {"error": "error", "conflict": "conflict", "diverged": "diverged", "dirty": "dirty",
         "unpushed": "unpushed", "behind": "behind", "local": "local", "clean": "clean"}

GIT_ENV = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
GIT_ENV.setdefault("GIT_SSH_COMMAND", "ssh -o BatchMode=yes")

e = html.escape

LOG_COMMANDS = True
_log_lock = threading.Lock()


def log(line):
    with _log_lock:
        print(line, flush=True)


def slug(name):
    return re.sub(r"\s+", "-", name).lower()


# --------------------------------------------------------------------------- git

def git(repo, *args, timeout=60):
    cmd = ["git", "--no-optional-locks", "-c", "core.quotepath=off", "-C", repo, *args]
    if LOG_COMMANDS:
        log("$ " + shlex.join(cmd))
    try:
        p = subprocess.run(
            cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout, env=GIT_ENV, stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired:
        if LOG_COMMANDS:
            log(f"  ! timeout after {timeout}s")
        raise RuntimeError(f"git {args[0]}: timeout after {timeout}s") from None
    if p.returncode != 0:
        if LOG_COMMANDS:
            log(f"  ! exit {p.returncode}: {(p.stderr.strip().splitlines() or [''])[0]}")
        raise RuntimeError(p.stderr.strip() or f"git {args[0]} failed")
    return p.stdout


def opt(repo, *args):
    """Like git(), but returns None on failure (for optional info)."""
    try:
        return git(repo, *args)
    except Exception:
        return None


def find_repos(roots, depth):
    found = []
    for root in roots:
        base = root.rstrip(os.sep).count(os.sep)
        for cur, dirs, files in os.walk(root):
            if ".git" in dirs or ".git" in files:
                found.append((root, cur))
                dirs[:] = []
            elif cur.count(os.sep) - base >= depth:
                dirs[:] = []
            else:
                dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]
    return found


LANGS = {".py": "Python", ".java": "Java", ".js": "JavaScript", ".jsx": "JavaScript", ".ts": "TypeScript",
         ".tsx": "TypeScript", ".go": "Go", ".rs": "Rust", ".c": "C", ".h": "C", ".cpp": "C++", ".cs": "C#",
         ".sql": "SQL", ".sh": "Shell", ".ps1": "PowerShell", ".rb": "Ruby", ".php": "PHP", ".kt": "Kotlin",
         ".swift": "Swift", ".html": "HTML", ".css": "CSS", ".tf": "Terraform", ".lua": "Lua", ".dart": "Dart"}
SECRET_RE = re.compile(r"^(\.env(\..+)?|.+\.(pem|key|p12|pfx)|id_(rsa|dsa|ecdsa|ed25519)|credentials(\.json)?)$", re.I)
SECRET_OK = re.compile(r"\.(example|sample|template|dist)$", re.I)
STALE_DAYS, FETCH_OLD_DAYS = 90, 7


def human(kib):
    for unit in ("KiB", "MiB", "GiB"):
        if kib < 1024 or unit == "GiB":
            return f"{kib:.0f} {unit}" if unit == "KiB" else f"{kib:.1f} {unit}"
        kib /= 1024


def web_url(url):
    m = re.match(r"^(?:git@|ssh://git@|https?://(?:[^@/]+@)?)([^:/]+)[:/](.+?)(?:\.git)?/?$", url or "")
    return f"https://{m[1]}/{m[2]}" if m else None


def inspect(ws, root, path, fetch=False):
    rel = os.path.relpath(path, root).replace(os.sep, "/")
    if rel == ".":
        rel = os.path.basename(path)
    i = dict(id=hashlib.sha1(path.encode()).hexdigest()[:10], path=path, rel=rel, ws=ws,
             error=None, fetch_error=None, branch="?", upstream=None, ahead=0, behind=0,
             staged=0, modified=0, untracked=0, conflicts=0, files=[], stashes=[],
             branches=[], commits=[], remotes=[], activity=[0] * 30, unpushed=0,
             stale=[], default=None, operation=None, tag=None, total=None,
             last_fetch=None, last_ts=None, last_subject="", web=None, detached=False,
             host=None, size=None, local_only=0, dirty_since=None, readme=None, langs=[],
             authors=[], submodules=[], suspicious=[], hooks=0, lfs=False, worktrees=1)
    try:
        gd = git(path, "rev-parse", "--absolute-git-dir").strip()
        if fetch:
            try:
                git(path, "fetch", "--all", "--prune", "--quiet", timeout=45)
            except Exception as ex:  # offline / no remote: still show the status
                i["fetch_error"] = str(ex)

        for line in git(path, "status", "--porcelain=v2", "--branch").splitlines():
            if line.startswith("# branch.head "):
                i["branch"] = line[14:]
                i["detached"] = i["branch"] == "(detached)"
            elif line.startswith("# branch.upstream "):
                i["upstream"] = line[18:]
            elif line.startswith("# branch.ab "):
                a, b = line[12:].split()
                i["ahead"], i["behind"] = int(a[1:]), int(b[1:])
            elif line[:2] in ("1 ", "2 "):
                parts = line.split(" ", 8 if line[0] == "1" else 9)
                xy, fpath = parts[1], parts[-1].split("\t")[0]
                i["staged"] += xy[0] != "."
                i["modified"] += xy[1] != "."
                i["files"].append((xy.replace(".", " "), fpath))
            elif line.startswith("u "):
                i["conflicts"] += 1
                i["files"].append(("UU", line.split(" ", 10)[-1]))
            elif line.startswith("? "):
                i["untracked"] += 1
                i["files"].append(("??", line[2:]))

        # Remotes
        seen = set()
        for line in (opt(path, "remote", "-v") or "").splitlines():
            m = re.match(r"(\S+)\t(\S+) \(fetch\)", line)
            if m and m[1] not in seen:
                seen.add(m[1])
                i["remotes"].append((m[1], m[2]))
        main_url = next((u for n, u in i["remotes"] if n == "origin"),
                        i["remotes"][0][1] if i["remotes"] else None)
        i["web"] = web_url(main_url)
        m = (re.match(r"^(?:[a-z+]+://)?(?:[^@/]+@)?([^:/]+)[:/]", main_url)
             if main_url and ("://" in main_url or "@" in main_url) else None)
        i["host"] = m[1].lower() if m else None

        # Branches
        fmt = "%(HEAD)%09%(refname:short)%09%(upstream:short)%09%(upstream:track,nobracket)%09%(committerdate:unix)%09%(subject)"
        for line in (opt(path, "for-each-ref", "refs/heads", f"--format={fmt}") or "").splitlines():
            h, name, up, track, ts, subj = line.split("\t", 5)
            a, b = re.search(r"ahead (\d+)", track), re.search(r"behind (\d+)", track)
            i["branches"].append(dict(name=name, current=h == "*", upstream=up or None,
                                      ahead=int(a[1]) if a else 0, behind=int(b[1]) if b else 0,
                                      gone="gone" in track, ts=int(ts) if ts else None, subject=subj))
        i["branches"].sort(key=lambda b: (not b["current"], -(b["ts"] or 0)))
        for b in i["branches"]:  # never pushed, or upstream deleted
            b["local_only"] = bool(i["remotes"]) and (not b["upstream"] or b["gone"])
        i["local_only"] = sum(b["local_only"] for b in i["branches"])

        # Default branch and merged (= removable) branches
        names = {b["name"] for b in i["branches"]}
        ref = opt(path, "symbolic-ref", "--short", "refs/remotes/origin/HEAD")
        d = ref.strip().split("/", 1)[1] if ref and "/" in ref else None
        i["default"] = d or next((c for c in ("main", "master") if c in names), None)
        if i["default"] in names:
            merged = (opt(path, "for-each-ref", "refs/heads", f"--merged={i['default']}",
                          "--format=%(refname:short)") or "").split()
            i["stale"] = [n for n in merged if n not in (i["default"], i["branch"])]

        # Commits
        for line in (opt(path, "log", "-n", "10", "--format=%h%x09%ct%x09%an%x09%s") or "").splitlines():
            h, ts, an, subj = line.split("\t", 3)
            i["commits"].append(dict(hash=h, ts=int(ts), author=an, subject=subj))
        if i["commits"]:
            i["last_ts"], i["last_subject"] = i["commits"][0]["ts"], i["commits"][0]["subject"]
        else:
            i["last_subject"] = "(no commits)"
        total = opt(path, "rev-list", "--count", "HEAD")
        i["total"] = int(total) if total and total.strip().isdigit() else None
        now = time.time()
        for t in (opt(path, "log", "--all", "--since=30.days.ago", "--format=%ct") or "").split():
            k = int((now - int(t)) // 86400)
            if 0 <= k < 30:
                i["activity"][29 - k] += 1

        # Stashes, unpushed (all local branches), tag, running operation
        for line in (opt(path, "stash", "list", "--format=%gd%x09%ct%x09%gs") or "").splitlines():
            n, ts, msg = line.split("\t", 2)
            i["stashes"].append(dict(name=n, ts=int(ts), msg=msg))
        if i["remotes"]:
            n = opt(path, "rev-list", "--count", "--branches", "--not", "--remotes")
            i["unpushed"] = int(n) if n and n.strip().isdigit() else 0
        tag = opt(path, "describe", "--tags", "--abbrev=0")
        i["tag"] = tag.strip() if tag else None
        for marker, label in (("rebase-merge", "Rebase"), ("rebase-apply", "Rebase/am"),
                              ("MERGE_HEAD", "Merge"), ("CHERRY_PICK_HEAD", "Cherry-pick"),
                              ("REVERT_HEAD", "Revert"), ("BISECT_LOG", "Bisect")):
            if os.path.exists(os.path.join(gd, marker)):
                i["operation"] = label
                break
        try:
            i["last_fetch"] = int(os.path.getmtime(os.path.join(gd, "FETCH_HEAD")))
        except OSError:
            pass

        # Size, files (languages, README, secrets), authors, submodules, extras
        co = dict(re.findall(r"^([\w-]+): (\d+)$", opt(path, "count-objects", "-v") or "", re.M))
        if co:
            i["size"] = int(co.get("size", 0)) + int(co.get("size-pack", 0))
        tracked = (opt(path, "ls-files") or "").splitlines()[:50000]
        counts = {}
        for f in tracked:
            lang = LANGS.get(os.path.splitext(f)[1].lower())
            if lang:
                counts[lang] = counts.get(lang, 0) + 1
        n = sum(counts.values())
        i["langs"] = [(k, round(100 * v / n)) for k, v in sorted(counts.items(), key=lambda kv: -kv[1])[:3]]
        readme = next((f for f in tracked if "/" not in f and f.lower().startswith("readme")), None)
        if readme:
            try:
                with open(os.path.join(path, readme), encoding="utf-8", errors="replace") as fh:
                    for line in fh.read(4096).splitlines():
                        line = line.strip().lstrip("#=*> ").strip()
                        if len(line) > 2:
                            i["readme"] = line[:140]
                            break
            except OSError:
                pass
        untracked = [p for xy, p in i["files"] if xy == "??"]
        i["suspicious"] = [(p, p in untracked) for p in dict.fromkeys(tracked + untracked)
                           if SECRET_RE.match(p.rstrip("/").rsplit("/", 1)[-1])
                           and not SECRET_OK.search(p)][:20]
        mt = []
        for _, p in i["files"][:200]:
            try:
                mt.append(os.path.getmtime(os.path.join(path, p)))
            except OSError:  # deleted
                pass
        i["dirty_since"] = int(min(mt)) if mt else None
        for line in (opt(path, "shortlog", "-sn", "--since=90.days.ago", "HEAD") or "").splitlines()[:5]:
            c, _, name = line.strip().partition("\t")
            if c.isdigit():
                i["authors"].append((name, int(c)))
        for line in (opt(path, "submodule", "status") or "").splitlines():
            parts = line[1:].split()
            if len(parts) >= 2:
                i["submodules"].append(dict(path=parts[1], state={"-": "not initialized", "+": "different commit",
                                                                  "U": "conflict"}.get(line[0], "ok")))
        hooks = os.path.join(gd, "hooks")
        i["hooks"] = len([f for f in os.listdir(hooks) if not f.endswith(".sample")]) if os.path.isdir(hooks) else 0
        try:
            with open(os.path.join(path, ".gitattributes"), encoding="utf-8", errors="replace") as fh:
                i["lfs"] = "filter=lfs" in fh.read()
        except OSError:
            pass
        i["worktrees"] = sum(ln.startswith("worktree ") for ln in
                             (opt(path, "worktree", "list", "--porcelain") or "").splitlines()) or 1
    except Exception as ex:
        i["error"] = str(ex)
    i["dirty"] = bool(i["staged"] or i["modified"] or i["untracked"] or i["conflicts"])
    i["state"] = state_of(i)
    i["flags"] = flags_of(i)
    return i


def state_of(i):
    if i["error"]:
        return "error"
    if i["conflicts"]:
        return "conflict"
    if i["ahead"] and i["behind"]:
        return "diverged"
    if i["dirty"]:
        return "dirty"
    if i["ahead"]:
        return "unpushed"
    if i["behind"]:
        return "behind"
    if not i["upstream"]:
        return "local"
    return "clean"


def flags_of(i):
    f = {i["state"]}
    if i["error"]:
        return f
    if i["dirty"]:
        f.add("dirty")
    if i["behind"]:
        f.add("behind")
    if i["ahead"] or i["unpushed"]:
        f.add("unpushed")
    if i["stashes"]:
        f.add("stash")
    if i["stale"]:
        f.add("stale-branches")
    if i["default"] and i["branch"] != i["default"]:
        f.add("not-default")
    if i["operation"]:
        f.add("operation")
    if fetch_old(i):
        f.add("fetch-old")
    if i["local_only"]:
        f.add("local-branches")
    if inactive(i):
        f.add("inactive")
    if i["suspicious"]:
        f.add("secrets")
    if i["submodules"]:
        f.add("submodules")
    return f


def fetch_old(i):
    return bool(i["remotes"]) and (i["last_fetch"] is None
                                   or time.time() - i["last_fetch"] > FETCH_OLD_DAYS * 86400)


def inactive(i):
    return i["last_ts"] is not None and time.time() - i["last_ts"] > STALE_DAYS * 86400


def notes_of(i):
    n = []
    if i["error"]:
        return [i["error"].splitlines()[0]]
    if i["operation"]:
        n.append(f"{i['operation']} in progress")
    if i["detached"]:
        n.append("detached HEAD")
    elif i["default"] and i["branch"] != i["default"]:
        n.append(f"not on {i['default']}")
    if not i["remotes"]:
        n.append("no remote")
    elif not i["upstream"] and not i["detached"]:
        n.append("no upstream")
    if i["stashes"]:
        n.append(f"{len(i['stashes'])} stash")
    if i["stale"]:
        n.append(f"{len(i['stale'])} merged branches")
    if i["local_only"]:
        n.append(f"{i['local_only']} local-only branch(es)")
    if i["dirty_since"] and time.time() - i["dirty_since"] > 86400:
        n.append(f"dirty for {ago(i['dirty_since'])}")
    if i["suspicious"]:
        n.append("secret files?")
    if any(s["state"] != "ok" for s in i["submodules"]):
        n.append("submodule out of sync")
    if inactive(i):
        n.append("inactive")
    if i["fetch_error"]:
        n.append("fetch failed")
    return n


# ------------------------------------------------------------------------- state

class App:
    def __init__(self, roots, depth, fetch):
        self.depth, self.fetch0 = depth, fetch
        self.token = secrets.token_hex(16)
        self.lock = threading.Lock()
        self.repos, self.scanned = {}, 0
        names, self.workspaces = {}, []
        for r in roots:
            r = os.path.abspath(r)
            name = os.path.basename(r) or r
            if name in names:
                name = f"{name} ({os.path.basename(os.path.dirname(r))})"
            names[name] = r
            self.workspaces.append(dict(name=name, slug=slug(name), root=r))

    def ws_of(self, slug):
        return next((w for w in self.workspaces if w["slug"] == slug), None)

    def scan(self, fetch=False):
        by_root = {w["root"]: w["name"] for w in self.workspaces}
        found = find_repos(list(by_root), self.depth)
        with ThreadPoolExecutor(8) as ex:
            infos = list(ex.map(lambda t: inspect(by_root[t[0]], t[0], t[1], fetch), found))
        with self.lock:
            self.repos = {i["id"]: i for i in infos}
            self.scanned = time.time()

    def update(self, ids, fetch=False, pull=False):
        """Re-read single repos; returns (ok, message)."""
        with self.lock:
            targets = [self.repos[x] for x in ids if x in self.repos]
        msgs = []

        def work(old):
            if pull:
                try:
                    out = git(old["path"], "pull", "--ff-only", timeout=120).strip()
                    msgs.append(f"{old['rel']}: {out.splitlines()[-1] if out else 'ok'}")
                except Exception as ex:
                    msgs.append(f"{old['rel']}: {str(ex).splitlines()[0]}")
            root = next(w["root"] for w in self.workspaces if w["name"] == old["ws"])
            return inspect(old["ws"], root, old["path"], fetch)

        with ThreadPoolExecutor(8) as ex:
            new = list(ex.map(work, targets))
        with self.lock:
            for i in new:
                self.repos[i["id"]] = i
        bad = [i for i in new if i["fetch_error"]]
        if pull:
            return True, "; ".join(msgs)
        if bad:
            return False, f"Fetch failed: {bad[0]['rel']} – {bad[0]['fetch_error'].splitlines()[0]}"
        return True, f"{len(new)} repo(s) updated" if fetch else "rescanned"

    def sorted_repos(self):
        with self.lock:
            return sorted(self.repos.values(), key=lambda i: (STATES.index(i["state"]), i["rel"].lower()))


# ---------------------------------------------------------------------- Rendering

def ago(ts):
    if ts is None:
        return "–"
    s = max(0, int(time.time()) - ts)
    if s < 3600:
        return f"{max(1, s // 60)}m"
    if s < 48 * 3600:
        return f"{s // 3600}h"
    d = s // 86400
    if d < 14:
        return f"{d}d"
    if d < 90:
        return f"{d // 7}w"
    if d < 730:
        return f"{d // 30}mo"
    return f"{d // 365}y"


def when(ts):
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)) if ts else ""


def name_html(rel):
    head, _, tail = rel.rpartition("/")
    return (f'<span class="dim">{e(head)}/</span>' if head else "") + f"<b>{e(tail)}</b>"


def ic(state):
    return f'<span class="ic s-{state}" title="{LABEL[state]}">{ICON[state]}</span>'


def changes_html(i):
    parts = []
    if i["staged"]:
        parts.append(f'<span class="ch-s" title="staged">+{i["staged"]}</span>')
    if i["modified"]:
        parts.append(f'<span class="ch-m" title="modified">~{i["modified"]}</span>')
    if i["untracked"]:
        parts.append(f'<span class="ch-u" title="new (untracked)">?{i["untracked"]}</span>')
    if i["conflicts"]:
        parts.append(f'<span class="ch-c" title="conflicts">!{i["conflicts"]}</span>')
    return " ".join(parts) or '<span class="dim">·</span>'


def sync_html(a, b):
    if not (a or b):
        return '<span class="dim">·</span>'
    return " ".join(x for x in (f"{a}↑" if a else "", f"{b}↓" if b else "") if x)


def detail_of(i):
    """Short description for the attention panel."""
    if i["error"]:
        return i["error"].splitlines()[0][:80]
    s = i["state"]
    if s == "conflict":
        return f"{i['conflicts']} conflict(s)"
    if s == "diverged":
        return f"↑{i['ahead']} ↓{i['behind']}"
    if s == "dirty":
        bits = [f"{n} {w}" for n, w in ((i["staged"], "staged"), (i["modified"], "modified"),
                                        (i["untracked"], "new")) if n]
        extra = f" · on {i['branch']}, default {i['default']}" if "not-default" in i["flags"] else ""
        return ", ".join(bits) + extra
    if s == "unpushed":
        return f"{i['ahead']} commit(s) ahead"
    if s == "behind":
        return f"{i['behind']} commit(s) behind"
    return ""


def repo_row(i):
    flags = " ".join(sorted(i["flags"]))
    text = f"{i['rel']} {i['ws']} {i['branch']} {i['last_subject']} {i['path']}".lower()
    notes = notes_of(i)
    subj = f'<span class="dim"> {e(i["last_subject"])}</span>'
    fetch_cell = ago(i["last_fetch"]) if i["last_fetch"] else ("never" if i["remotes"] else "–")
    return (
        f'<tr data-href="/repo?id={i["id"]}" data-flags="{e(flags)}" data-ws="{e(slug(i["ws"]))}" data-host="{e(i["host"] or "")}" data-text="{e(text)}">'
        f'<td class="c-ic" data-v="{STATES.index(i["state"])}">{ic(i["state"])}</td>'
        f'<td data-v="{e(i["rel"].lower())}"><a href="/repo?id={i["id"]}">{name_html(i["rel"])}</a></td>'
        f'<td class="mono">{e(i["ws"])}</td>'
        f'<td class="mono">{e(i["branch"])}</td>'
        f'<td class="mono" data-v="{i["ahead"] + i["behind"]}">{sync_html(i["ahead"], i["behind"])}</td>'
        f'<td class="mono" data-v="{i["staged"] + i["modified"] + i["untracked"]}">{changes_html(i)}</td>'
        f'<td data-v="{i["last_ts"] or 0}" title="{when(i["last_ts"])}"><b>{ago(i["last_ts"])}</b>{subj}</td>'
        f'<td class="mono{" warn" if "fetch-old" in i["flags"] else ""}" data-v="{i["last_fetch"] or 0}" '
        f'title="{when(i["last_fetch"]) or "never fetched"}">{fetch_cell}</td>'
        f'<td class="notes">{e(" · ".join(notes))}</td></tr>'
    )


def overview(app):
    repos = app.sorted_repos()
    by_state = {s: [i for i in repos if i["state"] == s] for s in STATES}

    # Workspace cards
    cards = []
    for w in app.workspaces:
        rs = [i for i in repos if i["ws"] == w["name"]]
        bar = "".join(
            f'<i style="flex:{n};background:var(--s-{s})" title="{n} {LABEL[s]}"></i>'
            for s in STATES if (n := sum(i["state"] == s for i in rs)))
        legend = "".join(
            f'<span class="lg">{ic(s)} <b>{n}</b> {LABEL[s]}</span>'
            for s in STATES if (n := sum(i["state"] == s for i in rs)))
        cards.append(
            f'<section class="card ws"><div class="ws-h"><b class="big">{e(w["name"])}</b>'
            f'<span class="dim">{len(rs)} repos · <span class="mono">{e(w["root"])}</span></span></div>'
            f'<div class="ws-b"><div class="bar">{bar or "<i style=flex:1></i>"}</div><div class="legend">{legend}</div></div>'
            f'<button onclick="act(\'/api/fetch?ws={w["slug"]}\',this)">Fetch {e(w["name"])}</button></section>')

    # Needs attention
    groups = []
    for s in ("error", "conflict", "diverged", "dirty", "unpushed", "behind"):
        if by_state[s]:
            items = "".join(
                f'<a class="item" href="/repo?id={i["id"]}">{ic(s)}<span>{name_html(i["rel"])}</span>'
                f'<span class="dim">{e(detail_of(i))}</span></a>' for i in by_state[s])
            groups.append(f'<div class="grp"><h4>{LABEL[s].upper()} · {len(by_state[s])}</h4>{items}</div>')
    n_att = sum(len(by_state[s]) for s in ("error", "conflict", "diverged", "dirty", "unpushed", "behind"))
    attention = (f'<section class="card"><div class="card-h"><b>Needs attention</b>'
                 f'<span class="dim">{n_att} repos</span></div>'
                 + ("".join(groups) or '<div class="grp"><span class="ok">✓ All good</span></div>')
                 + '</section>')

    # Work at risk
    risk = []
    unp = [i for i in repos if not i["error"] and max(i["unpushed"], i["ahead"])]
    if unp:
        items = "".join(
            f'<a class="item" href="/repo?id={i["id"]}">{ic("unpushed")}<span>{name_html(i["rel"])}</span>'
            f'<span class="dim">{max(i["unpushed"], i["ahead"])} commit(s) only here</span></a>' for i in unp)
        risk.append(f'<div class="grp"><h4>UNPUSHED COMMITS · {sum(max(i["unpushed"], i["ahead"]) for i in unp)}</h4>{items}</div>')
    drt = [i for i in repos if not i["error"] and i["dirty"]]
    if drt:
        items = "".join(
            f'<a class="item" href="/repo?id={i["id"]}">{ic("dirty")}<span>{name_html(i["rel"])}</span>'
            f'<span class="dim">{len(i["files"])} file(s) at risk</span></a>' for i in drt)
        risk.append(f'<div class="grp"><h4>UNCOMMITTED CHANGES · {len(drt)}</h4>{items}</div>')
    stl = [i for i in repos if i["stashes"]]
    if stl:
        items = "".join(
            f'<a class="item" href="/repo?id={i["id"]}">{ic("clean")}<span>{name_html(i["rel"])}</span>'
            f'<span class="dim">{len(i["stashes"])} stash</span></a>' for i in stl)
        risk.append(f'<div class="grp"><h4>STASHES · {sum(len(i["stashes"]) for i in stl)}</h4>{items}</div>')
    risk_html = (f'<section class="card"><div class="card-h"><b>Work at risk</b>'
                 f'<span class="dim">only on this machine</span></div>'
                 + ("".join(risk) or '<div class="grp"><span class="ok">✓ Nothing at risk</span></div>')
                 + '</section>')

    # Filter + table
    chips = "".join(f'<button class="chip mono" data-t="is:{f}" onclick="toggle(\'is:{f}\')">is:{f}</button>'
                    for f in ("dirty", "behind", "unpushed", "diverged", "stash", "stale-branches",
                              "not-default", "local-branches", "fetch-old", "inactive", "secrets",
                              "submodules", "local", "error"))
    chips += "".join(f'<button class="chip mono" data-t="ws:{w["slug"]}" onclick="toggle(\'ws:{w["slug"]}\')">ws:{e(w["slug"])}</button>'
                     for w in app.workspaces if len(app.workspaces) > 1)
    rows = "".join(repo_row(i) for i in repos) or '<tr><td colspan="9">No repos found.</td></tr>'
    return f"""
<div class="stack">{"".join(cards)}</div>
<div class="two">{attention}{risk_html}</div>
<div class="filter"><span class="mono dim">/</span>
<input id="q" class="mono" placeholder="filter: is:dirty ws:work host:github -is:clean text …" autocomplete="off"></div>
<div class="chips">{chips}</div>
<p class="dim" id="count">{len(repos)} of {len(repos)} repos</p>
<section class="card"><table id="repos"><thead><tr>
<th data-s class="c-ic"></th><th data-s>Repo</th><th data-s>Workspace</th><th data-s>Branch</th>
<th data-s>↑↓</th><th data-s>Changes</th><th data-s>Last commit</th><th data-s>Fetch</th><th>Notes</th>
</tr></thead><tbody>{rows}</tbody></table></section>"""


def repo_page(app, i):
    st = i["state"]
    up = i["upstream"]
    br = f'<span class="mono">{e(i["branch"])}</span>'
    if up:
        br += f' <span class="dim">→</span> <span class="mono">{e(up)}</span> <b>{sync_html(i["ahead"], i["behind"]).replace("·", "")}</b>'
    elif i["remotes"] and not i["detached"]:
        br += ' <span class="dim">(no upstream)</span>'
    buttons = ""
    if i["web"]:
        buttons += f'<a class="btn" href="{e(i["web"])}" target="_blank" rel="noopener">Open on web</a>'
    buttons += (f'<button onclick="act(\'/api/fetch?id={i["id"]}\',this)">Fetch</button>'
                f'<button class="primary" title="git pull --ff-only" onclick="act(\'/api/pull?id={i["id"]}\',this)">Pull</button>')

    cards = (
        f'<div><span class="k">STATUS</span><span class="s-{st} v">{LABEL[st]}</span></div>'
        f'<div><span class="k">BRANCH</span><span class="v">{br}</span></div>'
        f'<div><span class="k">DEFAULT</span><span class="v mono">{e(i["default"] or "–")}</span></div>'
        f'<div><span class="k">LAST FETCH</span><span class="v mono">{ago(i["last_fetch"]) if i["last_fetch"] else ("never" if i["remotes"] else "–")}</span></div>'
        f'<div><span class="k">COMMITS</span><span class="v mono">{i["total"] if i["total"] is not None else "–"}</span></div>'
        f'<div><span class="k">TAG</span><span class="v mono">{e(i["tag"] or "–")}</span></div>')
    if i["operation"]:
        cards += f'<div><span class="k">OPERATION</span><span class="v s-conflict">{e(i["operation"])} in progress</span></div>'

    langs = ", ".join(f"{e(k)} {v}%" for k, v in i["langs"]) or "–"
    extras = [x for x in (f"{i['hooks']} Hook(s)" if i["hooks"] else "", "LFS" if i["lfs"] else "",
                          f"{i['worktrees']} Worktrees" if i["worktrees"] > 1 else "") if x]
    dirty_since = (f'{ago(i["dirty_since"])}' if i["dirty"] and i["dirty_since"] else "–")
    facts2 = (
        f'<div><span class="k">SIZE (.git)</span><span class="v mono">{human(i["size"]) if i["size"] is not None else "–"}</span></div>'
        f'<div><span class="k">LANGUAGES</span><span class="v">{langs}</span></div>'
        f'<div><span class="k">REMOTE HOST</span><span class="v mono">{e(i["host"] or "–")}</span></div>'
        f'<div><span class="k">DIRTY FOR</span><span class="v mono">{dirty_since}</span></div>'
        f'<div><span class="k">LOCAL-ONLY BRANCHES</span><span class="v mono">{i["local_only"]}</span></div>'
        f'<div><span class="k">EXTRAS</span><span class="v">{e(", ".join(extras) or "–")}</span></div>')

    def panel(title, meta, body):
        return f'<section class="card"><div class="card-h"><b>{title}</b><span class="dim">{meta}</span></div>{body}</section>'

    brs = "".join(
        f'<div class="row mono">{"*" if b["current"] else "&nbsp;"} <span class="{"" if not b["current"] else "cur"}">{e(b["name"])}</span>'
        + (f' <span class="dim">[ahead {b["ahead"]}]</span>' if b["ahead"] else "")
        + (f' <span class="dim">[behind {b["behind"]}]</span>' if b["behind"] else "")
        + (' <span class="dim">[upstream gone]</span>' if b["gone"] else "")
        + (' <span class="tag">merged</span>' if b["name"] in i["stale"] else "")
        + (' <span class="tag">local only</span>' if b["local_only"] else "")
        + (' <span class="tag">default</span>' if b["name"] == i["default"] else "")
        + f'<span class="grow"></span><span class="dim" title="{when(b["ts"])}">{ago(b["ts"])}</span></div>'
        for b in i["branches"]) or '<div class="row dim">no branches</div>'
    cms = "".join(
        f'<div class="row"><span class="mono dim">{e(c["hash"])}</span><span class="trunc">{e(c["subject"])}</span>'
        f'<span class="dim author">{e(c["author"])}</span><span class="dim" title="{when(c["ts"])}">{ago(c["ts"])}</span></div>'
        for c in i["commits"]) or '<div class="row dim">no commits</div>'

    files = "".join(
        f'<div class="row mono"><span class="xy">{e(xy)}</span><span class="trunc">{e(p)}</span></div>'
        for xy, p in i["files"][:100])
    if len(i["files"]) > 100:
        files += f'<div class="row dim">… and {len(i["files"]) - 100} more</div>'
    stashes = "".join(
        f'<div class="row"><span class="mono dim">{e(s["name"])}</span><span class="trunc">{e(s["msg"])}</span>'
        f'<span class="dim" title="{when(s["ts"])}">{ago(s["ts"])}</span></div>' for s in i["stashes"])
    remotes = "".join(
        f'<div class="row mono"><b>{e(n)}</b><span class="trunc dim">{e(u)}</span></div>' for n, u in i["remotes"]
    ) or '<div class="row dim">no remote configured</div>'

    mx = max(i["activity"]) or 1
    bars = "".join(
        f'<rect x="{k * 10}" y="{48 - 2 - 44 * v / mx:.1f}" width="8" height="{2 + 44 * v / mx:.1f}" rx="1" '
        f'fill="{"var(--s-behind)" if v else "var(--line)"}"><title>{v} commit(s)</title></rect>'
        for k, v in enumerate(i["activity"]))
    spark = f'<div class="spark"><svg viewBox="0 0 300 48" preserveAspectRatio="none">{bars}</svg></div>'

    left = panel("Branches", len(i["branches"]), brs)
    if i["files"]:
        left += panel("Changes", len(i["files"]), files)
    if i["stashes"]:
        left += panel("Stashes", len(i["stashes"]), stashes)
    right = panel("Commits", "last 10", cms) + panel("Activity", "30 days", spark)
    if i["authors"]:
        right += panel("Authors", "90 days", "".join(
            f'<div class="row"><span class="trunc">{e(n)}</span><span class="dim mono">{c} commit(s)</span></div>'
            for n, c in i["authors"]))
    if i["submodules"]:
        right += panel("Submodules", len(i["submodules"]), "".join(
            f'<div class="row mono"><span class="trunc">{e(m["path"])}</span>'
            f'<span class="{"dim" if m["state"] == "ok" else "warn"}">{e(m["state"])}</span></div>'
            for m in i["submodules"]))
    right += panel("Remotes", len(i["remotes"]), remotes)

    err = ""
    if i["error"]:
        err += f'<div class="alert">{e(i["error"])}</div>'
    if i["fetch_error"]:
        err += f'<div class="alert">Fetch: {e(i["fetch_error"])}</div>'
    if i["suspicious"]:
        err += ('<div class="alert">Possible secret files: ' + ", ".join(
            f'{e(p)} ({"untracked" if u else "tracked in repo!"})' for p, u in i["suspicious"]) + "</div>")
    readme = f'<p class="dim readme">{e(i["readme"])}</p>' if i["readme"] else ""
    return f"""
<div class="crumbs"><a href="/">Overview</a> / <span>{e(i["ws"])}</span> / <span>{e(i["rel"])}</span></div>
<div class="title">{ic(st)}<h1>{e(i["rel"].rpartition("/")[2])}</h1><span class="mono dim">{e(i["path"])}</span>
<span class="grow"></span>{buttons}</div>
{readme}{err}
<section class="card facts">{cards}</section>
<section class="card facts">{facts2}</section>
<div class="two">{"<div class='stack'>" + left + "</div>"}{"<div class='stack'>" + right + "</div>"}</div>"""


CSS = """
:root{color-scheme:dark;--bg:#0e131a;--panel:#131a23;--line:#222c38;--fg:#e6edf3;--dim:#8794a5;
--btn:#18212c;--s-error:#f06565;--s-conflict:#f06565;--s-diverged:#d96bd0;--s-dirty:#e5b04a;
--s-unpushed:#4fd1d9;--s-behind:#6b9cff;--s-local:#8794a5;--s-clean:#4cc38a}
@media(prefers-color-scheme:light){:root{color-scheme:light;--bg:#f5f7fa;--panel:#fff;--line:#dde3ea;
--fg:#18222e;--dim:#667385;--btn:#f0f3f7;--s-dirty:#b97d00;--s-unpushed:#0e8f99;--s-behind:#2f64d8;--s-clean:#1f9d62}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.4 system-ui,"Segoe UI",sans-serif}
a{color:inherit;text-decoration:none}
.mono{font-family:ui-monospace,"Cascadia Mono",Consolas,monospace;font-size:.92em}
.dim{color:var(--dim)} .grow{flex:1} .ok{color:var(--s-clean)}
header{display:flex;align-items:center;gap:.6rem;padding:.7rem 1.5rem;border-bottom:1px solid var(--line)}
.brand{display:flex;align-items:center;gap:.5rem;font-weight:700;margin-right:1rem}
.logo{background:var(--fg);color:var(--bg);border-radius:6px;width:1.5rem;height:1.5rem;display:grid;place-items:center;font-size:.85rem}
nav a{padding:.45rem .8rem;border-radius:8px}nav a.on{background:var(--panel)}
button,.btn{font:inherit;color:var(--fg);background:var(--btn);border:1px solid var(--line);border-radius:8px;
padding:.45rem .9rem;cursor:pointer;white-space:nowrap}
button:hover,.btn:hover{border-color:var(--dim)} button:disabled{opacity:.5;cursor:wait}
button.primary{background:var(--fg);color:var(--bg);border-color:var(--fg)}
main{padding:1.2rem 1.5rem;max-width:1500px;margin:auto}
.stack{display:flex;flex-direction:column;gap:.8rem} .stack+.two,.stack+.filter{margin-top:.9rem}
.card{background:var(--panel);border:1px solid var(--line);border-radius:12px;overflow:hidden}
.two{display:grid;grid-template-columns:1fr 1fr;gap:.9rem;align-items:start;margin-bottom:1rem}
@media(max-width:1000px){.two{grid-template-columns:1fr}}
.two>.stack{margin:0}
.ws{display:grid;grid-template-columns:minmax(180px,260px) 1fr auto;align-items:center;gap:1.2rem;padding:.9rem 1.1rem}
.ws-h{display:flex;flex-direction:column}.big{font-size:1.15rem}
.bar{display:flex;gap:2px;height:10px;border-radius:5px;overflow:hidden;background:var(--line)}.bar i{display:block}
.legend{display:flex;flex-wrap:wrap;gap:.3rem 1.1rem;margin-top:.5rem}
.lg b{font-weight:700}
.ic{display:inline-block;width:1.3em;text-align:center;font-weight:700}
.ic.s-error,.ic.s-conflict{color:var(--s-error)}.ic.s-diverged{color:var(--s-diverged)}.ic.s-dirty{color:var(--s-dirty)}
.ic.s-unpushed{color:var(--s-unpushed)}.ic.s-behind{color:var(--s-behind)}.ic.s-local{color:var(--s-local)}.ic.s-clean{color:var(--s-clean)}
.card-h{display:flex;justify-content:space-between;padding:.8rem 1.1rem;border-bottom:1px solid var(--line)}
.grp{padding:.7rem 1.1rem;border-bottom:1px dashed var(--line)}.grp:last-child{border:0}
.grp h4{margin:0 0 .35rem;font-size:.8rem;letter-spacing:.04em;color:var(--dim)}
.item{display:flex;align-items:baseline;gap:.5rem;padding:.2rem 0;font-size:1.02rem}
.item:hover b{text-decoration:underline}.item .dim{font-size:.9rem}
.filter{display:flex;gap:.6rem;align-items:center;border:1px solid var(--line);border-radius:10px;padding:.2rem .9rem;background:var(--panel)}
.filter input{flex:1;background:none;border:0;outline:0;color:var(--fg);padding:.6rem 0;font-size:1rem}
.chips{display:flex;flex-wrap:wrap;gap:.5rem;margin:.7rem 0}
.chip{border-radius:2rem;padding:.3rem .8rem;background:none}.chip.on{background:var(--fg);color:var(--bg)}
table{width:100%;border-collapse:collapse}
th{text-align:left;font-size:.8rem;letter-spacing:.04em;text-transform:uppercase;color:var(--dim);padding:.7rem .8rem;
border-bottom:1px solid var(--line);cursor:pointer;user-select:none;white-space:nowrap}
th:last-child{cursor:default}
td{padding:.55rem .8rem;border-bottom:1px solid var(--line);vertical-align:middle}
tbody tr{cursor:pointer}tbody tr:hover{background:var(--btn)}tbody tr:last-child td{border:0}
tr[hidden]{display:none}
td.c-ic,th.c-ic{width:2.4rem;padding-right:0}
td.notes{color:var(--dim)} td:nth-child(7){max-width:34ch;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.ch-s{color:var(--s-clean)}.ch-m{color:var(--s-dirty)}.ch-u{color:var(--s-behind)}.ch-c{color:var(--s-error)}
td.warn,.warn{color:var(--s-dirty)}
.readme{margin:-.3rem 0 1rem}
.crumbs{margin-bottom:1rem;color:var(--dim)}.crumbs a{text-decoration:underline}
.title{display:flex;align-items:center;gap:.8rem;margin-bottom:1rem;flex-wrap:wrap}
.title h1{margin:0;font-size:1.9rem}.title .ic{font-size:1.4rem}
.facts{display:flex;flex-wrap:wrap;margin-bottom:1rem}
.facts>div{flex:1 1 200px;padding:.9rem 1.1rem;border-right:1px solid var(--line);display:flex;flex-direction:column;gap:.3rem}
.facts>div:last-child{border:0}
.k{font-size:.8rem;letter-spacing:.04em;color:var(--dim)}.v{font-size:1.05rem}
.v.s-error,.v.s-conflict{color:var(--s-error)}.v.s-diverged{color:var(--s-diverged)}.v.s-dirty{color:var(--s-dirty)}
.v.s-unpushed{color:var(--s-unpushed)}.v.s-behind{color:var(--s-behind)}.v.s-clean{color:var(--s-clean)}
.row{display:flex;gap:.8rem;align-items:baseline;padding:.45rem 1.1rem}.row .author{font-size:.88rem}
.trunc{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.cur{font-weight:700}.tag{font-size:.75rem;border:1px solid var(--line);border-radius:4px;padding:0 .35rem;color:var(--dim)}
.xy{white-space:pre;color:var(--s-dirty);min-width:2.2ch}
.spark{padding:.8rem 1.1rem}.spark svg{width:100%;height:60px;display:block}
.alert{border:1px solid var(--s-error);color:var(--s-error);border-radius:10px;padding:.6rem 1rem;margin-bottom:1rem}
#toast{position:fixed;bottom:1.2rem;right:1.2rem;background:var(--fg);color:var(--bg);padding:.7rem 1.1rem;border-radius:10px;
max-width:60ch;opacity:0;pointer-events:none;transition:opacity .2s}#toast.show{opacity:1}
"""

JS = """
const TOKEN="__TOKEN__";
function toast(m){const t=document.getElementById('toast');t.textContent=m;t.classList.add('show');setTimeout(()=>t.classList.remove('show'),6000);}
async function act(url,btn){
  if(btn){btn.disabled=true;btn.textContent='…';}
  try{const r=await fetch(url,{method:'POST',headers:{'X-Token':TOKEN}});const j=await r.json();
    sessionStorage.setItem('toast',j.message||(j.ok?'OK':'Error'));}
  catch(x){sessionStorage.setItem('toast','Error: '+x);}
  location.reload();
}
const pending=sessionStorage.getItem('toast');
if(pending){sessionStorage.removeItem('toast');toast(pending);}
const q=document.getElementById('q');
if(q){
  const tb=document.querySelector('#repos tbody'), rows=[...tb.rows].filter(r=>r.dataset.flags!==undefined);
  function apply(){
    const toks=q.value.trim().toLowerCase().split(/\\s+/).filter(Boolean);let n=0;
    rows.forEach(r=>{
      const flags=r.dataset.flags.split(' ');let ok=true;
      for(let t of toks){
        const neg=t[0]==='-';if(neg)t=t.slice(1);if(!t)continue;
        let m;
        if(t.startsWith('is:'))m=flags.includes(t.slice(3));
        else if(t.startsWith('ws:'))m=r.dataset.ws===t.slice(3);
        else if(t.startsWith('host:'))m=r.dataset.host.includes(t.slice(5));
        else m=r.dataset.text.includes(t);
        if(m===neg){ok=false;break;}
      }
      r.hidden=!ok;if(ok)n++;
    });
    document.getElementById('count').textContent=n+' of '+rows.length+' repos';
    document.querySelectorAll('.chip').forEach(c=>c.classList.toggle('on',toks.includes(c.dataset.t)));
  }
  function toggle(tok){
    const ts=q.value.split(/\\s+/).filter(Boolean),k=ts.indexOf(tok);
    k>=0?ts.splice(k,1):ts.push(tok);q.value=ts.join(' ');apply();
  }
  q.addEventListener('input',apply);
  document.addEventListener('keydown',ev=>{if(ev.key==='/'&&document.activeElement!==q){ev.preventDefault();q.focus();}});
  tb.addEventListener('click',ev=>{
    if(ev.target.closest('a'))return;
    const r=ev.target.closest('tr');if(r&&r.dataset.href)location=r.dataset.href;
  });
  document.querySelectorAll('#repos th[data-s]').forEach(th=>th.addEventListener('click',()=>{
    const i=th.cellIndex,dir=th.dataset.dir==='asc'?'desc':'asc';th.dataset.dir=dir;
    const v=r=>{const c=r.cells[i];return c.dataset.v??c.textContent.trim().toLowerCase();};
    [...tb.rows].sort((a,b)=>{
      const x=v(a),y=v(b),nx=parseFloat(x),ny=parseFloat(y);
      const c=(String(nx)===x&&String(ny)===y)?nx-ny:x.localeCompare(y);
      return dir==='asc'?c:-c;
    }).forEach(r=>tb.appendChild(r));
  }));
  apply();
}
"""


def page(app, title, body):
    stand = f"Updated {ago(int(app.scanned))} ago" if app.scanned else ""
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{e(title)}</title><style>{CSS}</style></head><body>"
        '<header><a class="brand" href="/"><span class="logo">R</span>Repo Radar</a>'
        '<nav><a class="on" href="/">Overview</a></nav><span class="grow"></span>'
        f'<span class="mono dim">{stand}</span>'
        "<button onclick=\"act('/api/refresh',this)\">Rescan</button>"
        "<button class=\"primary\" onclick=\"act('/api/fetch',this)\">Fetch all</button></header>"
        f'<main>{body}</main><div id="toast"></div>'
        f'<script>{JS.replace("__TOKEN__", app.token)}</script></body></html>'
    )


# ------------------------------------------------------------------------- Server

def make_handler(app, port):
    allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}

    class Handler(BaseHTTPRequestHandler):
        def _send(self, code, body, ctype="text/html; charset=utf-8"):
            data = body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def _host_ok(self):
            # protects against DNS rebinding
            if self.headers.get("Host") in allowed_hosts:
                return True
            self._send(403, "forbidden", "text/plain")
            return False

        def do_GET(self):
            if not self._host_ok():
                return
            u = urlparse(self.path)
            if u.path == "/":
                self._send(200, page(app, "Repo Radar", overview(app)))
            elif u.path == "/repo":
                i = app.repos.get(parse_qs(u.query).get("id", [""])[0])
                if i:
                    self._send(200, page(app, f"{i['rel']} – Repo Radar", repo_page(app, i)))
                else:
                    self._send(404, page(app, "404", '<p>Repo not found. <a href="/"><u>Back</u></a></p>'))
            elif u.path == "/favicon.ico":
                self._send(204, "", "text/plain")
            else:
                self._send(404, "not found", "text/plain")

        def do_POST(self):
            if not self._host_ok():
                return
            if not secrets.compare_digest(self.headers.get("X-Token", ""), app.token):
                self._send(403, "forbidden", "text/plain")
                return
            u = urlparse(self.path)
            q = parse_qs(u.query)
            rid, slug = q.get("id", [None])[0], q.get("ws", [None])[0]
            try:
                if u.path == "/api/refresh":
                    app.scan()
                    ok, msg = True, "rescanned"
                elif u.path in ("/api/fetch", "/api/pull"):
                    if rid:
                        ids = [rid]
                    else:
                        w = app.ws_of(slug) if slug else None
                        ids = [i["id"] for i in app.sorted_repos() if not w or i["ws"] == w["name"]]
                    ok, msg = app.update(ids, fetch=u.path == "/api/fetch", pull=u.path == "/api/pull")
                else:
                    self._send(404, "not found", "text/plain")
                    return
            except Exception as ex:
                ok, msg = False, str(ex)
            self._send(200, json.dumps({"ok": ok, "message": msg}), "application/json")

        def log_message(self, *a):
            pass

    return Handler


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("roots", nargs="*", default=["."], help="root directories to scan (default: .)")
    ap.add_argument("--depth", type=int, default=3, help="search depth (default: 3)")
    ap.add_argument("--fetch", action="store_true", help="run git fetch for all repos on startup")
    ap.add_argument("--quiet", action="store_true", help="do not print the git commands that are run")
    ap.add_argument("--no-open", action="store_true", help="do not open the browser automatically")
    ap.add_argument("--port", type=int, default=0, help="port (default: pick a free one)")
    args = ap.parse_args()
    global LOG_COMMANDS
    LOG_COMMANDS = not args.quiet

    app = App(args.roots, args.depth, args.fetch)
    print("scanning repos …", flush=True)
    app.scan(fetch=args.fetch)
    server = ThreadingHTTPServer(("127.0.0.1", args.port), None)
    server.RequestHandlerClass = make_handler(app, server.server_port)
    url = f"http://127.0.0.1:{server.server_port}/"
    print(f"{len(app.repos)} repos · {url}  (Ctrl+C to quit)", flush=True)
    if not args.no_open:
        threading.Timer(0.2, webbrowser.open, args=(url,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
