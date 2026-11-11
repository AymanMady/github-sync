#!/usr/bin/env python3
"""Make a few small, meaningful commits per day on my data-engineering repos.

Each run:
  1. picks a repo from config.json,
  2. picks a file that can be improved (Python without docstrings,
     SQL without comments, README),
  3. asks a free model (GitHub Models) for ONE small improvement,
  4. verifies the change is safe:
       - .py : the AST is identical once docstrings are removed
               (so the code behavior cannot change),
       - .sql: the SQL is identical once comments are removed,
       - all : the diff stays small (max_changed_lines),
  5. commits with a conventional message and pushes.

Environment variables:
  GH_PAT        token with "Contents: read & write" on the target repos
  MODELS_TOKEN  token for GitHub Models (otherwise GITHUB_TOKEN is used)
  DRY_RUN=1     show diffs, no commit or push
"""

from __future__ import annotations

import ast
import datetime as dt
import difflib
import json
import os
import random
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CONFIG = json.loads((ROOT / "config.json").read_text())
HISTORY = ROOT / "history.jsonl"
MODELS_URL = "https://models.github.ai/inference/chat/completions"
DRY_RUN = os.environ.get("DRY_RUN", "0") not in ("", "0", "false")
COMMIT_RE = re.compile(r"^(docs|style|chore)(\([\w./-]+\))?: \S.{3,70}$")


class RateLimited(Exception):
    pass


def log(msg: str) -> None:
    print(msg, flush=True)


def git(*args: str, cwd: Path | None = None) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, text=True, capture_output=True
    ).stdout


# --------------------------------------------------------------------------
# Safety checks
# --------------------------------------------------------------------------
def py_without_docstrings(src: str) -> str:
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            body = node.body
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                node.body = body[1:] or [ast.Pass()]
    return ast.dump(tree, include_attributes=False)


def sql_without_comments(src: str) -> str:
    src = re.sub(r"/\*.*?\*/", " ", src, flags=re.S)
    src = re.sub(r"--[^\n]*", " ", src)
    return " ".join(src.split()).lower()


def diff_lines(old: str, new: str) -> list[str]:
    return [
        line
        for line in difflib.unified_diff(
            old.splitlines(), new.splitlines(), lineterm="", n=0
        )
        if line[:1] in "+-" and not line.startswith(("+++", "---"))
    ]


def line_length(repo_dir: Path) -> int:
    pyproject = repo_dir / "pyproject.toml"
    if pyproject.exists():
        m = re.search(r"line-length\s*=\s*(\d+)", pyproject.read_text())
        if m:
            return int(m.group(1))
    return 88


def validate(path: str, old: str, new: str, repo_dir: Path) -> str | None:
    """Return the reason for rejection, or None if the change is OK."""
    changes = diff_lines(old, new)
    if not changes:
        return "no change"
    if len(changes) > CONFIG["max_changed_lines"]:
        return f"diff too large ({len(changes)} lines)"
    if path.endswith(".py"):
        try:
            if py_without_docstrings(old) != py_without_docstrings(new):
                return "Python code changed (only docstrings/comments are allowed)"
        except SyntaxError as exc:
            return f"invalid Python: {exc}"
        max_len = line_length(repo_dir)
        too_long = [ln for ln in changes if ln.startswith("+") and len(ln) - 1 > max_len]
        if too_long:
            return f"line longer than {max_len} characters"
    elif path.endswith(".sql"):
        if sql_without_comments(old) != sql_without_comments(new):
            return "SQL code changed (only comments are allowed)"
    return None


# --------------------------------------------------------------------------
# Picking files
# --------------------------------------------------------------------------
def missing_docstrings(src: str) -> int:
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return 0
    nodes = [tree] + [
        n
        for n in ast.walk(tree)
        if isinstance(n, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    return sum(1 for n in nodes if ast.get_docstring(n) is None)


def score(path: str, src: str) -> float:
    if len(src.strip()) < 80 or len(src) > CONFIG["max_file_chars"]:
        return 0
    if path.endswith(".py"):
        weight = min(missing_docstrings(src), 6)
        is_test = "tests/" in path or Path(path).name.startswith("test_")
        return weight * (0.3 if is_test else 1.0)
    if path.endswith(".sql"):
        lines = [ln for ln in src.splitlines() if ln.strip()]
        comments = sum(1 for ln in lines if ln.strip().startswith(("--", "/*")))
        return max(0.0, len(lines) / 15 - comments)
    if path.lower().endswith("readme.md"):
        return 0.7
    return 0


def recent_files() -> set[str]:
    if not HISTORY.exists():
        return set()
    entries = [json.loads(ln) for ln in HISTORY.read_text().splitlines() if ln.strip()]
    return {f"{e['repo']}:{e['file']}" for e in entries[-CONFIG["skip_recent"]:]}


def candidates(repo: str, repo_dir: Path, used: set[str]) -> list[tuple[str, float]]:
    out = []
    for path in git("ls-files", cwd=repo_dir).splitlines():
        if not path.endswith((".py", ".sql", ".md")):
            continue
        if path.startswith(tuple(CONFIG["exclude_prefixes"])):
            continue
        if f"{repo}:{path}" in used:
            continue
        try:
            src = (repo_dir / path).read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        s = score(path, src)
        if s > 0:
            out.append((path, s))
    return out


# --------------------------------------------------------------------------
# Model
# --------------------------------------------------------------------------
SYSTEM = (
    "You are a senior data engineer reviewing a repository. You make ONE small, "
    "focused, genuinely useful improvement per request. You never invent "
    "features. Reply only with JSON: "
    '{"skip": false, "commit_message": "...", "content": "<full new file>"} '
    'or {"skip": true} if there is nothing useful to do.'
)

TASKS = {
    ".py": (
        "Add concise, accurate Google-style docstrings to the module, classes "
        "and functions that DO NOT have one yet (at most 4). Do NOT modify any "
        "code, imports, type hints or existing docstrings. Lines <= {max_len} "
        "characters. commit_message: 'docs(<module>): add docstrings to ...'."
    ),
    ".sql": (
        "Add short SQL comments (-- ...) explaining the purpose of the file and "
        "the non-obvious parts (joins, aggregations, filters). Do NOT modify the "
        "SQL itself. commit_message: 'docs(sql): document ...'."
    ),
    ".md": (
        "Make ONE small improvement to this README: fix typos/grammar, clarify a "
        "section, or add a short missing section (e.g. project structure) using "
        "ONLY the facts from the file and the file list. Keep the same language. "
        "Change at most {max_lines} lines. commit_message: 'docs(readme): ...'."
    ),
}


def ask_model(system: str, prompt: str) -> dict:
    token = os.environ.get("MODELS_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not token:
        sys.exit("MODELS_TOKEN or GITHUB_TOKEN is missing (GitHub Models).")
    body = {
        "model": CONFIG["model"],
        "temperature": 0.2,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ],
    }
    req = urllib.request.Request(
        MODELS_URL,
        data=json.dumps(body).encode(),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = json.load(resp)
    except urllib.error.HTTPError as exc:
        if exc.code == 429:
            raise RateLimited() from exc
        raise RuntimeError(f"GitHub Models HTTP {exc.code}: {exc.read()[:300]!r}")
    text = data["choices"][0]["message"]["content"].strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    return json.loads(text)


def clean_message(msg: str, path: str) -> str:
    msg = (msg or "").strip().splitlines()[0].strip().strip("`'\"") if msg else ""
    if COMMIT_RE.match(msg):
        return msg
    name = Path(path).stem
    if path.endswith(".py"):
        return f"docs({name}): add docstrings"
    if path.endswith(".sql"):
        return f"docs(sql): document {Path(path).name}"
    return "docs(readme): improve documentation"


def improve(repo_dir: Path, path: str) -> tuple[str, str] | None:
    old = (repo_dir / path).read_text(encoding="utf-8")
    ext = ".md" if path.endswith(".md") else Path(path).suffix
    task = TASKS[ext].format(
        max_len=line_length(repo_dir), max_lines=CONFIG["max_changed_lines"] // 2
    )
    files = "\n".join(git("ls-files", cwd=repo_dir).splitlines()[:150])
    prompt = (
        f"Task: {task}\n\nRepository files:\n{files}\n\n"
        f"File `{path}`:\n<<<FILE\n{old}\nFILE>>>"
    )
    answer = ask_model(SYSTEM, prompt)
    if answer.get("skip") or not answer.get("content"):
        log("    model: nothing to do")
        return None
    new = answer["content"]
    if old.endswith("\n") and not new.endswith("\n"):
        new += "\n"
    reason = validate(path, old, new, repo_dir)
    if reason:
        log(f"    rejected: {reason}")
        return None
    return new, clean_message(answer.get("commit_message", ""), path)


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------
def clone(repo: str, dest: Path) -> Path:
    pat = os.environ.get("GH_PAT")
    auth = f"x-access-token:{pat}@" if pat else ""
    target = dest / repo.split("/")[1]
    git("clone", "--quiet", f"https://{auth}github.com/{repo}.git", str(target))
    return target


def main() -> None:
    if not DRY_RUN and not os.environ.get("GH_PAT"):
        sys.exit("GH_PAT is missing (required to push).")
    goal = random.randint(CONFIG["min_commits"], CONFIG["max_commits"])
    log(f"Goal: {goal} commit(s){' [DRY RUN]' if DRY_RUN else ''}")
    used = recent_files()
    done: list[dict] = []
    repos = CONFIG["repos"][:]
    random.shuffle(repos)

    with tempfile.TemporaryDirectory() as tmp:
        for repo in repos:
            if len(done) >= goal:
                break
            log(f"\n== {repo}")
            repo_dir = clone(repo, Path(tmp))
            pending = candidates(repo, repo_dir, used)
            made = 0
            attempts = 0
            while pending and len(done) < goal and attempts < 4:
                attempts += 1
                paths, weights = zip(*pending)
                path = random.choices(paths, weights=weights)[0]
                pending = [p for p in pending if p[0] != path]
                used.add(f"{repo}:{path}")
                log(f"  -> {path}")
                try:
                    result = improve(repo_dir, path)
                except RateLimited:
                    log("  GitHub Models quota reached, stopping.")
                    goal = len(done)
                    break
                except (RuntimeError, ValueError, KeyError) as exc:
                    log(f"    error: {exc}")
                    continue
                if not result:
                    continue
                new, message = result
                old = (repo_dir / path).read_text(encoding="utf-8")
                if DRY_RUN:
                    log("\n".join(diff_lines(old, new)))
                (repo_dir / path).write_text(new, encoding="utf-8")
                git(
                    "-c", f"user.name={CONFIG['author_name']}",
                    "-c", f"user.email={CONFIG['author_email']}",
                    "commit", "--quiet", "-am", message,
                    cwd=repo_dir,
                )
                log(f"    commit: {message}")
                made += 1
                done.append({
                    "date": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                    "repo": repo, "file": path, "message": message,
                })
                if len(done) < goal and not DRY_RUN:
                    time.sleep(random.randint(*CONFIG["pause_seconds"]))
            if made and not DRY_RUN:
                git("push", "--quiet", "origin", "HEAD", cwd=repo_dir)
                log(f"  pushed {made} commit(s)")

    if done and not DRY_RUN:
        with HISTORY.open("a") as fh:
            for entry in done:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    log(f"\nDone: {len(done)} commit(s).")


if __name__ == "__main__":
    main()
