#!/usr/bin/env python3
"""repo_scan.py - read-only repository hygiene scanner.

Collects factual evidence for a code-cleanup audit: junk artifacts, stale
markers, commented-out code, duplicate files/blocks, broken relative imports,
unreferenced sources, dependency drift, env-var drift, hardcoded local paths.

Design contract:
  * READ-ONLY. Never writes to the scanned repository.
  * Deterministic. Same tree in -> same report out (timestamps aside).
  * Evidence over judgement. Every finding carries file, line and a snippet
    (or a count) so a human or agent can verify it. Findings are candidates,
    not verdicts: confidence is reported, never a fix.
  * No network, no third-party imports, stdlib only (Python 3.9+).

Usage:
    python3 repo_scan.py [PATH] [--md FILE] [--json FILE] [--focus SUBDIR]
                         [--limit N] [--max-file-bytes N] [--no-git] [--quiet]

Exit codes: 0 scanned, 2 bad usage, 3 path not a directory.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from collections import defaultdict
from datetime import datetime, timezone
from fnmatch import fnmatch
from pathlib import Path

# --------------------------------------------------------------------------- #
# Constants
# --------------------------------------------------------------------------- #

IGNORE_DIRS = {
    ".git", ".hg", ".svn", ".jj",
    "node_modules", "bower_components", "jspm_packages",
    ".venv", "venv", "env", "virtualenv", ".tox", ".nox", ".eggs",
    "__pycache__", ".mypy_cache", ".pytest_cache", ".ruff_cache", ".pydantic_cache",
    ".next", ".nuxt", ".svelte-kit", ".turbo", ".parcel-cache", ".cache",
    "dist", "build", "out", "target", "obj", "bin",
    "coverage", "htmlcov", ".nyc_output", "playwright-report", "test-results",
    ".idea", ".vscode", ".fleet", ".history",
    "Pods", "DerivedData", ".gradle", ".terraform", ".serverless",
    ".workbuddy", ".claude", ".claude-code", ".serena",
    "site-packages", "vendor_bundle", "third_party",
}

LANG_BY_EXT = {
    ".py": "python", ".pyi": "python",
    ".js": "js", ".jsx": "js", ".mjs": "js", ".cjs": "js",
    ".ts": "ts", ".tsx": "ts", ".mts": "ts", ".cts": "ts",
    ".go": "go", ".rs": "rust", ".java": "java", ".kt": "kotlin",
    ".swift": "swift", ".m": "objc", ".mm": "objc",
    ".c": "c", ".h": "c", ".cc": "cpp", ".cpp": "cpp", ".hpp": "cpp", ".hh": "cpp",
    ".cs": "csharp", ".rb": "ruby", ".php": "php", ".scala": "scala",
    ".sh": "shell", ".bash": "shell", ".zsh": "shell", ".ps1": "shell",
    ".lua": "lua", ".sql": "sql",
    ".vue": "vue", ".svelte": "svelte",
    ".html": "html", ".htm": "html", ".css": "css", ".scss": "css", ".sass": "css", ".less": "css",
    ".yaml": "yaml", ".yml": "yaml", ".toml": "toml", ".ini": "ini", ".cfg": "ini",
    ".json": "json", ".jsonc": "json",
    ".md": "md", ".mdx": "md", ".rst": "md", ".txt": "text",
}

BINARY_EXT = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".pdf", ".zip", ".gz",
    ".tar", ".bz2", ".xz", ".7z", ".rar", ".exe", ".dll", ".so", ".dylib",
    ".woff", ".woff2", ".ttf", ".otf", ".eot", ".mp3", ".mp4", ".mov", ".wav",
    ".sqlite", ".db", ".class", ".jar", ".pyc", ".pyo", ".wasm", ".o", ".a",
}

SOURCE_LANGS = {"python", "js", "ts", "go", "rust", "java", "kotlin", "swift",
                "objc", "c", "cpp", "csharp", "ruby", "php", "scala", "vue", "svelte"}

COMMENT_TOKENS = {
    "python": ("#",), "ruby": ("#",), "shell": ("#",), "yaml": ("#",),
    "toml": ("#",), "ini": ("#",),
    "js": ("//",), "ts": ("//",), "go": ("//",), "rust": ("//",),
    "java": ("//",), "kotlin": ("//",), "swift": ("//",), "objc": ("//",),
    "c": ("//",), "cpp": ("//",), "csharp": ("//",), "scala": ("//",),
    "php": ("//",), "vue": ("//",), "svelte": ("//",),
    "sql": ("--",),
}

JUNK_PATTERNS = [
    ("备份/临时文件", ["*.orig", "*.rej", "*.bak", "*.bak.*", "*.old", "*.backup",
                  "*.backup.*", "*_backup.*", "*_backup", "*~",
                  "*.tmp", "*.temp", "*.swp", "*.swo", "*.save", "*.save.*",
                  "*.copy", "*.copy.*", "*.fixed", "*.fixed.*"]),
    ("系统垃圾", [".DS_Store", "Thumbs.db", "desktop.ini", ".AppleDouble"]),
    ("补丁残留", ["*.patch", "*.diff"]),
    ("命名可疑", ["*copy*.py", "*copy*.js", "*copy*.ts", "*_old.*", "*_new.*",
               "*_v1.*", "*_v2.*", "*.orig.*", "untitled*", "Untitled*",
               "新建文本文档*", "未命名*", "temp.*", "tmp.*", "scratch.*", "playground.*"]),
]

MARKER_RE = re.compile(
    r"\b(TODO|FIXME|XXX|HACK|BUG|WORKAROUND|DEPRECATED|@deprecated|"
    r"REMOVE[_-]?ME|TEMP(orary)?|PLACEHOLDER|DO[_-]?NOT[_-]?COMMIT|"
    r"noinspection|eslint-disable|ts-ignore|ts-expect-error|type:\s*ignore|"
    r"nolint|pylint:\s*disable|nosec|coverage:\s*ignore)\b",
    re.IGNORECASE,
)

DEBUG_RE = re.compile(
    r"(\bconsole\.(log|debug|dir|trace|warn|info)\s*\(|"
    r"\bdebugger\s*;?|"
    r"(?<![\w.])print\s*\(|"
    r"\bfmt\.Print(f|ln)?\s*\(|"
    r"\bSystem\.out\.print|"
    r"\bvar_dump\s*\(|\bdd\s*\(|\bdump\s*\(|"
    r"\bbreakpoint\s*\(\)|\bpdb\.set_trace\s*\(|\bimport\s+pdb\b|"
    r"\bbinding\.pry\b|\bbyebug\b|"
    r"\bputs\s+.*#\s*debug|"
    r"\bdbg!\s*\(|"
    r"\bprintStackTrace\s*\(|"
    r"console\.time\s*\()"
)

CODE_LIKE_RE = re.compile(
    r"(^\s*(if|for|while|switch|return|const|let|var|def|class|function|func|fn|"
    r"import|from|export|public|private|protected|static|async|await|try|catch|"
    r"elif|else|with|lambda|echo|print|SELECT|INSERT|UPDATE|DELETE)\b|"
    r"[;{}]\s*$|=>|->|==|!=|\)\s*;?\s*$|^\s*[\w.\[\]'\"]+\s*=[^=])"
)

REL_IMPORT_RE = re.compile(
    r"""(?:\bfrom\s+|\bimport\s+|\brequire\s*\(\s*|\bimport\s*\(\s*)['"](\.{1,2}/[^'"]+)['"]"""
)

IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

JS_IMPORT_RE = re.compile(
    r"""(?:^|\n)\s*(?:import\s+(?:[\w*{}\s,$]+\s+from\s+)?['"]([^'"]+)['"]|"""
    r"""export\s+[\w*{}\s,$]*\s*from\s+['"]([^'"]+)['"]|"""
    r"""require\s*\(\s*['"]([^'"]+)['"]\s*\)|"""
    r"""import\s*\(\s*['"]([^'"]+)['"]\s*\))"""
)

PY_IMPORT_RE = re.compile(
    r"^[ \t]*(?:from[ \t]+([\w.]+)[ \t]+import\b"
    r"|import[ \t]+([\w.]+(?:[ \t]*,[ \t]*[\w.]+)*))",
    re.MULTILINE,
)

ENV_RE = re.compile(
    r"(?:process\.env\.([A-Z][A-Z0-9_]+)|process\.env\[['\"]([A-Z][A-Z0-9_]+)['\"]\]|"
    r"os\.environ\.get\(\s*['\"]([A-Z][A-Z0-9_]+)['\"]|"
    r"os\.environ\[\s*['\"]([A-Z][A-Z0-9_]+)['\"]|"
    r"os\.getenv\(\s*['\"]([A-Z][A-Z0-9_]+)['\"]|"
    r"ENV\[['\"]([A-Z][A-Z0-9_]+)['\"]\]|"
    r"\$\{([A-Z][A-Z0-9_]+)\})"
)

SYSTEM_ENV = {
    "PATH", "HOME", "USER", "SHELL", "PWD", "LANG", "LC_ALL", "TERM", "TMPDIR",
    "NODE_ENV", "PYTHONPATH", "VIRTUAL_ENV", "CI", "PORT", "HOST", "TZ",
    "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "DEBUG", "LOG_LEVEL",
}

LOCAL_PATH_RE = re.compile(
    r"(/Users/[A-Za-z0-9._-]+/|/home/[A-Za-z0-9._-]+/|[A-Za-z]:\\\\?Users\\\\?[A-Za-z0-9._-]+)"
)

SECRET_RE = re.compile(
    r"(AKIA[0-9A-Z]{16}|sk-[A-Za-z0-9]{20,}|ghp_[A-Za-z0-9]{30,}|"
    r"-----BEGIN (?:RSA |EC |OPENSSH |PGP )?PRIVATE KEY-----|"
    r"xox[baprs]-[A-Za-z0-9-]{10,}|AIza[0-9A-Za-z_-]{35})"
)

ENTRY_STEMS = {
    "__init__", "__main__", "main", "index", "app", "server", "cli", "manage",
    "setup", "conftest", "wsgi", "asgi", "mod", "lib", "bootstrap", "start",
    "entry", "worker", "run", "config", "settings", "vite.config", "next.config",
    "webpack.config", "rollup.config", "jest.config", "vitest.config", "tailwind.config",
    "postcss.config", "tsconfig", "eslint.config", ".eslintrc", "babel.config",
    "docker-compose", "Makefile", "Dockerfile",
}

# 这些目录下的文件多为可执行入口，静态引用检查不适用
SCRIPTISH_RE = re.compile(
    r"(^|/)(scripts?|bin|cli|tools?|examples?|samples?|benchmarks?|migrations?|"
    r"fixtures?|mocks?|cmd|hack)/"
)

TOOLING_PACKAGES = {
    "typescript", "ts-node", "tsx", "tsup", "esbuild", "vite", "webpack", "rollup",
    "eslint", "prettier", "postcss", "autoprefixer", "tailwindcss", "sass", "less",
    "jest", "vitest", "mocha", "chai", "ava", "tap", "cypress", "playwright",
    "@playwright/test", "husky", "lint-staged", "nodemon", "concurrently",
    "rimraf", "cross-env", "dotenv-cli", "npm-run-all", "serve", "http-server",
    "@types/node", "semantic-release", "changesets", "@changesets/cli",
}

PY_ALIASES = {
    "pyyaml": "yaml", "python-dateutil": "dateutil", "beautifulsoup4": "bs4",
    "pillow": "PIL", "scikit-learn": "sklearn", "opencv-python": "cv2",
    "attrs": "attr", "protobuf": "google", "grpcio": "grpc",
    "psycopg2-binary": "psycopg2", "mysqlclient": "MySQLdb",
    "python-dotenv": "dotenv", "msgpack-python": "msgpack",
    "pydantic-settings": "pydantic_settings", "typing-extensions": "typing_extensions",
    "python-multipart": "multipart", "pyjwt": "jwt", "djangorestframework": "rest_framework",
}

CATEGORY_META = [
    ("git_state", "版本库基线"),
    ("junk", "临时与备份残留"),
    ("markers", "过时标记与调试残留"),
    ("commented_code", "被注释掉的代码"),
    ("empty", "空文件与占位实现"),
    ("dup_file", "重复文件"),
    ("dup_block", "重复代码块"),
    ("broken_import", "失效引用"),
    ("unused_file", "疑似无引用文件"),
    ("deps_unused", "声明未使用的依赖"),
    ("deps_undeclared", "使用未声明的依赖"),
    ("env_drift", "环境变量漂移"),
    ("version_drift", "版本号不一致"),
    ("hardcoded", "硬编码路径与疑似凭据"),
    ("test_gap", "测试陈旧或缺失"),
    ("inventory", "项目文件清单"),
]

SEV_ORDER = {"high": 0, "medium": 1, "low": 2, "info": 3}


# --------------------------------------------------------------------------- #
# Low-level helpers
# --------------------------------------------------------------------------- #

def safe_read(path: Path, limit: int) -> str | None:
    """Read text defensively. Returns None for binary/unreadable/minified files."""
    try:
        if path.suffix.lower() in BINARY_EXT:
            return None
        size = path.stat().st_size
        with open(path, "rb") as fh:
            head = fh.read(4096)
            if b"\x00" in head:
                return None
            # 压缩/打包后的单行文件：对审计无意义，且会撑爆内存
            if size > 100_000 and b"\n" not in head:
                return None
            fh.seek(0)
            raw = fh.read(min(size, limit))
        return raw.decode("utf-8", errors="replace")
    except (OSError, ValueError):
        return None


def load_gitignore(root: Path) -> list[str]:
    pats: list[str] = []
    for name in (".gitignore", ".dockerignore"):
        f = root / name
        if not f.is_file():
            continue
        text = safe_read(f, 200_000) or ""
        for raw in text.splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or line.startswith("!"):
                continue
            pats.append(line.lstrip("/").rstrip("/"))
    return pats


def is_ignored(rel: str, name: str, pats: list[str]) -> bool:
    for p in pats:
        if not p:
            continue
        if "/" in p:
            if fnmatch(rel, p):
                return True
            prefix = p.rstrip("*").rstrip("/")
            if prefix and rel.startswith(prefix + "/"):
                return True
        elif fnmatch(name, p) or fnmatch(rel, "*/" + p):
            return True
    return False


def walk_files(root: Path, pats: list[str], max_files: int) -> list[Path]:
    out: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(
            d for d in dirnames
            if (d not in IGNORE_DIRS and not d.startswith("."))
            or d in {".github", ".gitlab", ".circleci"}
        )
        for fn in sorted(filenames):
            p = Path(dirpath) / fn
            rel = p.relative_to(root).as_posix()
            if is_ignored(rel, fn, pats):
                continue
            out.append(p)
            if len(out) >= max_files:
                return out
    return out


def lang_of(path: Path) -> str | None:
    return LANG_BY_EXT.get(path.suffix.lower())


def finding(category, severity, confidence, path, line, title, evidence, note=""):
    return {
        "category": category,
        "severity": severity,
        "confidence": confidence,
        "file": str(path) if path is not None else "",
        "line": line,
        "title": title,
        "evidence": evidence,
        "note": note,
    }


def clip(s: str, n: int = 160) -> str:
    s = s.strip()
    return s if len(s) <= n else s[: n - 1] + "…"


CJK_RE = re.compile(r"[\u4e00-\u9fff]")


def in_cjk_string(line: str, pos: int) -> bool:
    """True if pos sits inside a quoted segment that contains CJK text.

    Guards against self-matching: a linter message or a doc string that
    *describes* a hardcoded path is not itself a hardcoded path.
    """
    for q in ('"', "'", "`"):
        start = line.rfind(q, 0, pos)
        if start == -1:
            continue
        end = line.find(q, pos)
        if end != -1 and CJK_RE.search(line[start:end]):
            return True
    return False


def inside_quoted(line: str, pos: int) -> bool:
    """True if pos sits inside a quoted/backticked span.

    规则文档、正则定义、告警文案里"提到"某个模式，不等于该模式真的出现。
    """
    for q in ('"', "'", "`"):
        start = line.rfind(q, 0, pos)
        if start == -1:
            continue
        end = line.find(q, pos)
        if end != -1:
            return True
    return False


# markdown 里只把"清单式的待办"当作标记；正文里提到 TODO 三个字不算
MD_MARKER_RE = re.compile(r"^\s{0,4}(?:[-*+]|\d+\.)?\s*\**\s*(TODO|FIXME|XXX|HACK)\b\s*[:：]")


# --------------------------------------------------------------------------- #
# Checks
# --------------------------------------------------------------------------- #

def check_git(root: Path, enabled: bool) -> list[dict]:
    if not enabled:
        return []
    def run(args):
        try:
            r = subprocess.run(["git", "-C", str(root)] + args,
                               capture_output=True, text=True, timeout=20)
            return r.stdout.strip() if r.returncode == 0 else None
        except (OSError, subprocess.SubprocessError):
            return None
    branch = run(["rev-parse", "--abbrev-ref", "HEAD"])
    if not branch:
        has_git = (root / ".git").exists()
        reason = "存在 .git 但无有效提交（HEAD 为空）" if has_git else "未检测到 git 元数据"
        return [finding("git_state", "info", "certain", None, 0,
                        "GIT 基线不可用", reason,
                        "无可用基线时无法回滚，改动前必须自行备份或先提交")]
    status = run(["status", "--porcelain"]) or ""
    dirty = [l for l in status.splitlines() if l.strip()]
    head = run(["log", "-1", "--format=%h %ad %s", "--date=short"]) or "?"
    ev = f"分支 {branch} · HEAD {head} · 未提交变更 {len(dirty)} 个"
    note = "工作区不干净：清理不得触碰他人未提交改动" if dirty else "工作区干净，可安全建立改动基线"
    out = [finding("git_state", "high" if dirty else "info", "certain", None, 0,
                   "工作区状态", ev, note)]
    for line in dirty[:30]:
        out.append(finding("git_state", "medium", "certain", None, 0,
                           "未提交变更", line, "属用户在途工作，默认不改动"))
    return out


def git_recent_files(root: Path, days: int) -> set[str]:
    """近 N 天被改动过的文件集合，用于把清理优先级锚在"最近动过"上。"""
    try:
        r = subprocess.run(
            ["git", "-C", str(root), "log", f"--since={days} days ago",
             "--name-only", "--pretty=format:"],
            capture_output=True, text=True, timeout=45)
        if r.returncode != 0:
            return set()
        return {ln.strip() for ln in r.stdout.splitlines() if ln.strip()}
    except (OSError, subprocess.SubprocessError):
        return set()


def check_junk(root: Path, files: list[Path]) -> list[dict]:
    out = []
    for p in files:
        rel = p.relative_to(root).as_posix()
        for label, patterns in JUNK_PATTERNS:
            if any(fnmatch(p.name, pat) for pat in patterns):
                out.append(finding("junk", "low", "certain", rel, 0,
                                   f"{label}：{p.name}",
                                   f"{rel} ({p.stat().st_size if p.exists() else 0} bytes)",
                                   "确认非构建产物后可直接删除"))
                break
    return out


def check_markers(root: Path, texts: dict[str, str], limit: int) -> list[dict]:
    out = []
    for rel, text in texts.items():
        lang = lang_of(Path(rel))
        if lang not in SOURCE_LANGS and lang not in {"yaml", "toml", "md", "json", "ini", "sql", "html", "css"}:
            continue
        for i, line in enumerate(text.splitlines(), 1):
            if len(line) > 400:
                continue
            m = MARKER_RE.search(line)
            if m:
                pos = m.start(1) if m.lastindex else m.start()
                if inside_quoted(line, pos):
                    continue
                # 中文行文里"提到 TODO"是描述，不是待办；真待办通常是 `TODO: 说明`
                if CJK_RE.search(line[max(0, pos - 12):pos]):
                    continue
                if lang == "md" and not MD_MARKER_RE.match(line):
                    continue
                out.append(finding("markers", "medium", "likely", rel, i,
                                   f"过时标记 {m.group(1).upper()}",
                                   clip(line), "标记本身不是问题；需判断描述是否仍成立"))
                continue
            d = DEBUG_RE.search(line)
            if d and lang in SOURCE_LANGS:
                stripped = line.strip()
                if stripped.startswith(("//", "#", "*")):
                    continue
                scriptish = bool(SCRIPTISH_RE.search(rel))
                out.append(finding(
                    "markers", "low" if scriptish else "medium", "likely", rel, i,
                    "调试输出残留", clip(stripped),
                    "脚本/CLI 目录中可能是有意的标准输出" if scriptish
                    else "库代码中通常应移除，或改为日志"))
    out.sort(key=lambda f: (f["file"], f["line"]))
    return out[:limit]


def check_commented_code(root: Path, texts: dict[str, str], limit: int) -> list[dict]:
    out = []
    for rel, text in texts.items():
        lang = lang_of(Path(rel))
        tokens = COMMENT_TOKENS.get(lang or "")
        if not tokens:
            continue
        tok = tokens[0]
        lines = text.splitlines()
        run_start, run_len, run_sample = None, 0, []
        for i, line in enumerate(lines + [""], 1):
            s = line.strip()
            body = s[len(tok):].strip() if s.startswith(tok) else None
            is_code = bool(body) and bool(CODE_LIKE_RE.search(body)) and len(body) < 200
            if is_code:
                if run_start is None:
                    run_start, run_len, run_sample = i, 0, []
                run_len += 1
                run_sample.append(body)
            else:
                if run_start is not None and run_len >= 3:
                    out.append(finding(
                        "commented_code", "medium", "likely", rel, run_start,
                        f"疑似注释掉的代码（{run_len} 行）",
                        clip(" / ".join(run_sample[:3])),
                        "若确为废弃实现应删除；若为文档示例则保留"))
                run_start, run_len, run_sample = None, 0, []
    out.sort(key=lambda f: (f["file"], f["line"]))
    return out[:limit]


def check_empty(root: Path, texts: dict[str, str], limit: int) -> list[dict]:
    out = []
    for rel, text in texts.items():
        lang = lang_of(Path(rel))
        if lang not in SOURCE_LANGS:
            continue
        body = "\n".join(
            l for l in text.splitlines()
            if l.strip() and not l.strip().startswith(("#", "//", "*", "/*"))
        )
        if len(body.strip()) == 0:
            out.append(finding("empty", "medium", "certain", rel, 0,
                               "空文件（无有效代码）", f"{rel}",
                               "可能为脚手架残留；确认无 import 后删除"))
        elif len(body.strip()) < 40 and "pass" in body:
            out.append(finding("empty", "low", "likely", rel, 0,
                               "占位实现（仅 pass/空返回）", clip(body, 80),
                               "确认调用方是否依赖其存在"))
    return out[:limit]


def check_dup_files(root: Path, files: list[Path], max_bytes: int) -> list[dict]:
    by_hash: dict[str, list[str]] = defaultdict(list)
    for p in files:
        if p.suffix.lower() in BINARY_EXT:
            continue
        try:
            if p.stat().st_size == 0 or p.stat().st_size > max_bytes:
                continue
            h = hashlib.blake2b(p.read_bytes(), digest_size=16).hexdigest()
        except OSError:
            continue
        by_hash[h].append(p.relative_to(root).as_posix())
    out = []
    for _, group in by_hash.items():
        if len(group) > 1:
            group.sort()
            out.append(finding("dup_file", "high", "certain", group[0], 0,
                               f"内容完全相同的文件（{len(group)} 个）",
                               " ≡ ".join(group),
                               "需确认哪一份是权威实现；保留其一并改引用，不可盲删"))
    return out


def _norm_lines(text: str, cap: int = 4000) -> list[str]:
    res = []
    for ln in text.splitlines()[:cap]:
        s = ln.strip()
        if not s or s.startswith(("//", "#", "*", "/*", "--", "<!--")):
            continue
        res.append(re.sub(r"\s+", "", s))
    return res


def check_dup_blocks(root: Path, texts: dict[str, str], limit: int) -> list[dict]:
    window, min_shared, max_windows = 8, 3, 200_000
    index: dict[str, set[str]] = defaultdict(set)
    total = 0
    for rel, text in texts.items():
        if lang_of(Path(rel)) not in SOURCE_LANGS or len(text) > 400_000:
            continue
        lines = _norm_lines(text)
        step = max(0, len(lines) - window + 1)
        total += step
        for i in range(step):
            h = hashlib.blake2b("\n".join(lines[i:i + window]).encode(),
                                digest_size=10).hexdigest()
            index[h].add(rel)
        if total > max_windows:
            break
    shared: dict[tuple[str, str], int] = defaultdict(int)
    for pset in index.values():
        if 1 < len(pset) <= 6:
            pl = sorted(pset)
            for i in range(len(pl)):
                for j in range(i + 1, len(pl)):
                    shared[(pl[i], pl[j])] += 1
    pairs = sorted(((c, a, b) for (a, b), c in shared.items() if c >= min_shared),
                   reverse=True)
    return [finding("dup_block", "medium", "likely", a, 0,
                    f"跨文件重复片段（约 {c * window} 行）",
                    f"{a}  ⇄  {b}",
                    "可能是有意的复制或应抽公共实现；需判断收敛方向")
            for c, a, b in pairs[:limit]]


def _resolve_js(base: Path, root: Path, spec: str) -> bool:
    target = (base.parent / spec).resolve()
    cands = [target]
    for ext in (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".json", ".vue", ".svelte", ".css", ".scss"):
        cands.append(Path(str(target) + ext))
    for ext in (".ts", ".tsx", ".js", ".jsx"):
        cands.append(target / f"index{ext}")
    return any(c.exists() for c in cands)


def check_broken_imports(root: Path, texts: dict[str, str], all_paths: set[str]) -> list[dict]:
    out = []
    for rel, text in texts.items():
        lang = lang_of(Path(rel))
        path = root / rel
        if lang in {"js", "ts", "vue", "svelte"}:
            for m in REL_IMPORT_RE.finditer(text):
                spec = m.group(1)
                if not _resolve_js(path, root, spec):
                    line = text[: m.start()].count("\n") + 1
                    out.append(finding("broken_import", "high", "certain", rel, line,
                                       "相对引用指向不存在的文件", spec,
                                       "可能是重构后遗留；修复需确认原目标文件"))
        elif lang == "python":
            for m in re.finditer(r"^\s*from\s+(\.{1,3}[\w.]*)\s+import", text, re.MULTILINE):
                spec = m.group(1)
                dots = len(spec) - len(spec.lstrip("."))
                mod = spec.lstrip(".")
                base = path.parent
                for _ in range(max(0, dots - 1)):
                    base = base.parent
                target = base / mod.replace(".", "/") if mod else base
                if not (target.exists() or (target.parent / (target.name + ".py")).exists()
                        or (target / "__init__.py").exists()):
                    line = text[: m.start()].count("\n") + 1
                    out.append(finding("broken_import", "high", "certain", rel, line,
                                       "相对导入解析失败", spec,
                                       "需核对包结构与 __init__.py"))
    return out


def check_unused_files(root: Path, texts: dict[str, str], files: list[Path]) -> list[dict]:
    stems: dict[str, str] = {}
    for rel in texts:
        if lang_of(Path(rel)) not in SOURCE_LANGS:
            continue
        if SCRIPTISH_RE.search(rel):
            continue
        stem = Path(rel).stem
        if stem in ENTRY_STEMS or stem.startswith("test_") or stem.endswith("_test"):
            continue
        if "." in stem or stem == "__init__":
            continue
        if Path(rel).name in {"index.ts", "index.js", "mod.rs", "lib.rs"}:
            continue
        if len(texts[rel].strip()) < 20:
            continue
        stems[stem] = rel
    if not stems:
        return []
    # 增量统计标识符出现次数，避免把全部源码拼成一个大字符串
    counter: dict[str, int] = defaultdict(int)
    for text in texts.values():
        for ident in IDENT_RE.findall(text):
            counter[ident] += 1
    out = []
    for stem, rel in stems.items():
        self_refs = len(IDENT_RE.findall(texts.get(rel, "")))
        if counter.get(stem, 0) - self_refs <= 0:
            out.append(finding("unused_file", "high", "speculative", rel, 0,
                               "无任何文本引用（疑似死文件）",
                               f"标识符 `{stem}` 在其余文件中出现 0 次",
                               "动态加载/反射/框架约定会绕过静态引用，删除前必须确认"))
    return out


def _js_package_of(spec: str) -> str | None:
    if spec.startswith((".", "/", "~", "@/")):
        return None
    if spec.startswith("node:"):
        return None
    parts = spec.split("/")
    if spec.startswith("@"):
        return "/".join(parts[:2]) if len(parts) >= 2 else spec
    return parts[0]


MANIFEST_NAMES = {
    "package.json", "pyproject.toml", "setup.py", "setup.cfg", "Pipfile",
    "requirements.txt", "requirements-dev.txt", "requirements_dev.txt",
    "dev-requirements.txt", "go.mod", "Cargo.toml",
}


def find_manifests(root: Path, files: list[Path]) -> dict[str, str]:
    """{manifest_rel: owning_dir_rel}，用于 monorepo 逐包分析；根目录记为 ""。"""
    out: dict[str, str] = {}
    for p in files:
        if p.name in MANIFEST_NAMES:
            rel = p.relative_to(root).as_posix()
            parent = Path(rel).parent.as_posix()
            out[rel] = "" if parent == "." else parent
    return out


def _scope(texts: dict[str, str], manifest_dir: str) -> dict[str, str]:
    """把文件集合收敛到某个 manifest 所在目录的子树。"""
    if not manifest_dir:
        return texts
    pref = manifest_dir + "/"
    return {r: t for r, t in texts.items() if r.startswith(pref)}


def check_deps_js(root: Path, texts: dict[str, str], config_blob: str,
                  manifests: dict[str, str]) -> list[dict]:
    out: list[dict] = []
    for mrel, mdir in sorted(manifests.items()):
        if Path(mrel).name != "package.json":
            continue
        try:
            pkg = json.loads(safe_read(root / mrel, 2_000_000) or "{}")
        except json.JSONDecodeError:
            out.append(finding("deps_unused", "medium", "certain", mrel, 0,
                               "package.json 解析失败", "JSON 语法错误", "必须先修复语法"))
            continue
        declared: dict[str, str] = {}
        for key in ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies"):
            declared.update(pkg.get(key) or {})
        if not declared:
            continue

        used: set[str] = set()
        for rel, text in _scope(texts, mdir).items():
            if lang_of(Path(rel)) not in {"js", "ts", "vue", "svelte"}:
                continue
            for m in JS_IMPORT_RE.finditer(text):
                spec = m.group(1) or m.group(2) or m.group(3) or m.group(4)
                if not spec:
                    continue
                p = _js_package_of(spec)
                if p:
                    used.add(p)

        for name in sorted(declared):
            if name in used or name.startswith("@types/") or name in TOOLING_PACKAGES:
                continue
            # 出现在脚本或配置文本中即视为被工具消费
            if re.search(rf"(?<![\w/-]){re.escape(name)}(?![\w-])", config_blob):
                continue
            out.append(finding("deps_unused", "medium", "likely", mrel, 0,
                               f"依赖声明后未被引用：{name}",
                               f"version={declared[name]} · 作用域 {mdir or '.'}",
                               "可能由 CLI/插件机制间接使用，需查 scripts 与构建配置"))
    out.sort(key=lambda f: (f["file"], f["title"]))
    return out


def _py_declared(root: Path) -> dict[str, tuple[str, str]]:
    """Return {normalized_pkg: (display_name, source_manifest)}."""
    declared: dict[str, tuple[str, str]] = {}
    sources = ["pyproject.toml", "setup.py", "setup.cfg", "Pipfile",
               "requirements.txt", "requirements-dev.txt", "requirements_dev.txt",
               "dev-requirements.txt"]
    for name in sources:
        f = root / name
        if not f.is_file():
            continue
        text = safe_read(f, 2_000_000) or ""
        if name.endswith(".txt") or name == "Pipfile":
            for line in text.splitlines():
                line = line.split("#")[0].strip()
                m = re.match(r"^([A-Za-z0-9][A-Za-z0-9._-]*)", line)
                if m and ":" not in line[: m.end()] :
                    declared.setdefault(m.group(1).lower().replace("_", "-"),
                                        (m.group(1), name))
        else:
            for m in re.finditer(r"""["']([A-Za-z0-9][A-Za-z0-9._-]*)\s*(?:\[[^\]]*\])?\s*(?:[<>=!~;][^"']*)?["']""", text):
                cand = m.group(1)
                if cand.lower() in {"python", "name", "version", "description", "readme",
                                    "license", "authors", "true", "false", "dependencies",
                                    "requires", "dev", "build", "test", "docs", "project"}:
                    continue
                declared.setdefault(cand.lower().replace("_", "-"), (cand, name))
    return declared


def _py_imports(texts: dict[str, str], mdir: str, stdlib: set[str],
                local_mods: set[str]) -> dict[str, str]:
    """{规范化模块名: 首次出现的文件}，已排除标准库与本地模块。"""
    imported: dict[str, str] = {}
    for rel, text in _scope(texts, mdir).items():
        if not rel.endswith(".py"):
            continue
        for m in PY_IMPORT_RE.finditer(text):
            mods: list[str] = []
            if m.group(1):
                mods.append(m.group(1).split(".")[0])
            if m.group(2):
                for part in m.group(2).split(","):
                    name = part.strip().split(" as ")[0].strip().split(".")[0]
                    if name:
                        mods.append(name)
            for mod in mods:
                if mod and mod not in stdlib and mod not in local_mods and mod != "import":
                    imported.setdefault(mod.lower().replace("_", "-"), rel)
    return imported


def check_deps_py(root: Path, texts: dict[str, str],
                  manifests: dict[str, str]) -> list[dict]:
    stdlib = set(getattr(sys, "stdlib_module_names", set()))
    local_mods: set[str] = set()
    for rel in texts:
        if rel.endswith(".py"):
            local_mods.add(Path(rel).stem)
            if len(Path(rel).parts) > 1:
                local_mods.add(Path(rel).parts[0])

    alias_back = {v.lower().replace("_", "-"): k.replace("_", "-")
                  for k, v in PY_ALIASES.items()}
    out: list[dict] = []
    seen: set[tuple[str, str]] = set()

    py_dirs: set[str] = set()
    py_manifest_names = {"pyproject.toml", "setup.py", "setup.cfg", "Pipfile",
                         "requirements.txt", "requirements-dev.txt",
                         "requirements_dev.txt", "dev-requirements.txt"}
    for mrel, mdir in manifests.items():
        if Path(mrel).name in py_manifest_names:
            py_dirs.add(mdir)
    for mdir in sorted(py_dirs):
        declared = _py_declared(root / mdir if mdir else root)
        if not declared:
            continue
        imported = _py_imports(texts, mdir, stdlib, local_mods)
        both = set(declared) | set(imported)
        for dist, mod in alias_back.items():
            if dist in both and mod in both:
                both.discard(mod if dist in declared else dist)

        for key in sorted(both):
            if key in declared and key not in imported and alias_back.get(key, key) not in imported:
                display, src = declared[key]
                loc = f"{mdir}/{src}" if mdir else src
                if (loc, display) in seen:
                    continue
                seen.add((loc, display))
                out.append(finding("deps_unused", "medium", "likely", loc, 0,
                                   f"依赖声明后未被 import：{display}",
                                   f"规范化名 {key} · 作用域 {mdir or '.'}",
                                   "可能仅供 CLI 使用（pytest/black/uvicorn 等），需核对脚本、CI 与插件机制"))
            elif key in imported and key not in declared and alias_back.get(key, key) not in declared:
                out.append(finding("deps_undeclared", "high", "likely", imported[key], 0,
                                   f"import 的包未在依赖清单声明：{key}",
                                   f"最近出现于 {imported[key]}",
                                   "可能依赖环境预装，干净部署会失败"))
    return out


RUST_IMPLICIT_CRATES = {"serde", "serde_json", "thiserror", "anyhow", "log", "cfg_if", "libc"}


def check_deps_go(root: Path, texts: dict[str, str],
                  manifests: dict[str, str]) -> list[dict]:
    out: list[dict] = []
    for mrel, mdir in sorted(manifests.items()):
        if Path(mrel).name != "go.mod":
            continue
        text = texts.get(mrel) or safe_read(root / mrel, 500_000) or ""
        requires: list[tuple[str, bool]] = []
        block = re.search(r"^require\s*\((.*?)^\)", text, re.MULTILINE | re.DOTALL)
        chunks = [block.group(1)] if block else []
        chunks += re.findall(r"^require\s+([^\s(][^\n]*)$", text, re.MULTILINE)
        for chunk in chunks:
            for line in chunk.splitlines():
                line = line.strip()
                if not line or line.startswith("//"):
                    continue
                m = re.match(r"([a-z0-9][a-z0-9.\-]*\.[a-z]{2,}/\S+)\s+v\S+", line)
                if m:
                    requires.append((m.group(1), "// indirect" in line))
        if not requires:
            continue
        imports: set[str] = set()
        for rel, t in _scope(texts, mdir).items():
            if not rel.endswith(".go"):
                continue
            for m in re.finditer(r'^\s*(?:import\s+)?(?:_\s+|\.\s+|\w+\s+)?"([^"\s]+)"', t, re.MULTILINE):
                imports.add(m.group(1))
        for mod, indirect in requires:
            if indirect:
                continue
            if any(i == mod or i.startswith(mod + "/") for i in imports):
                continue
            out.append(finding("deps_unused", "medium", "likely", mrel, 0,
                               f"go.mod 声明后未被 import：{mod}",
                               f"作用域 {mdir or '.'}",
                               "可能被 tools.go / 构建标签 / 生成器使用，先跑 `go mod tidy` 观察差异"))
    return out


def check_deps_rust(root: Path, texts: dict[str, str],
                    manifests: dict[str, str]) -> list[dict]:
    out: list[dict] = []
    for mrel, mdir in sorted(manifests.items()):
        if Path(mrel).name != "Cargo.toml":
            continue
        text = texts.get(mrel) or safe_read(root / mrel, 500_000) or ""
        declared: dict[str, str] = {}
        section = ""
        for raw in text.splitlines():
            line = raw.strip()
            if line.startswith("["):
                section = line.strip("[]").strip()
                continue
            if section not in {"dependencies", "dev-dependencies", "build-dependencies",
                               "workspace.dependencies"}:
                continue
            m = re.match(r"^([A-Za-z0-9_\-]+)\s*=", line)
            if m:
                declared[m.group(1)] = section
        if not declared:
            continue
        # 必须排除清单文件本身，否则 `tokio = "1"` 会把依赖自己判成"被使用"
        blob = "\n".join(t for r, t in _scope(texts, mdir).items()
                         if not r.endswith((".toml", ".lock")))
        for name, sec in sorted(declared.items()):
            crate = name.replace("-", "_")
            if name in RUST_IMPLICIT_CRATES or crate in RUST_IMPLICIT_CRATES:
                continue
            if re.search(rf"\b{re.escape(crate)}\s*::", blob) or f"extern crate {crate}" in blob:
                continue
            if re.search(rf"^\s*{re.escape(crate)}\s*=", blob, re.MULTILINE):
                continue
            out.append(finding("deps_unused", "medium", "likely", mrel, 0,
                               f"Cargo 依赖声明后未被使用：{name}",
                               f"段落 [{sec}] · 作用域 {mdir or '.'}",
                               "derive 宏或 feature 触发的间接依赖会误报，先跑 `cargo machete` 交叉验证"))
    return out


def check_env(root: Path, texts: dict[str, str]) -> list[dict]:
    declared: set[str] = set()
    example_found = None
    for cand in (".env.example", ".env.sample", ".env.template", ".env.dist", ".env.defaults"):
        f = root / cand
        if f.is_file():
            example_found = cand
            for line in (safe_read(f, 200_000) or "").splitlines():
                m = re.match(r"^\s*([A-Z][A-Z0-9_]+)\s*=", line)
                if m:
                    declared.add(m.group(1))
    if not example_found:
        return []
    used: dict[str, str] = {}
    for rel, text in texts.items():
        if lang_of(Path(rel)) not in SOURCE_LANGS:
            continue
        for m in ENV_RE.finditer(text):
            name = next((g for g in m.groups() if g), None)
            if name and name not in SYSTEM_ENV:
                used.setdefault(name, rel)
    out = []
    for name in sorted(used):
        if name not in declared:
            out.append(finding("env_drift", "high", "likely", used[name], 0,
                               f"环境变量未在 {example_found} 声明：{name}",
                               name, "新环境部署会因缺变量而失败"))
    return out


def check_versions(root: Path, texts: dict[str, str]) -> list[dict]:
    pairs: list[tuple[str, str, str]] = []
    pkg = root / "package.json"
    if pkg.is_file():
        try:
            v = json.loads(safe_read(pkg, 2_000_000) or "{}").get("version")
            if v:
                pairs.append(("package.json", v, "node"))
        except json.JSONDecodeError:
            pass
    py = root / "pyproject.toml"
    if py.is_file():
        m = re.search(r'^\s*version\s*=\s*["\']([^"\']+)["\']', safe_read(py, 1_000_000) or "", re.MULTILINE)
        if m:
            pairs.append(("pyproject.toml", m.group(1), "python"))
    cargo = root / "Cargo.toml"
    if cargo.is_file():
        m = re.search(r'^\s*version\s*=\s*["\']([^"\']+)["\']', safe_read(cargo, 1_000_000) or "", re.MULTILINE)
        if m:
            pairs.append(("Cargo.toml", m.group(1), "rust"))
    if len({v for _, v, _ in pairs}) > 1:
        return [finding("version_drift", "medium", "certain", None, 0,
                        "多处声明的项目版本不一致",
                        " · ".join(f"{f}={v}" for f, v, _ in pairs),
                        "需确认唯一权威版本源，避免发布产物版本错乱")]
    return []


def check_hardcoded(root: Path, texts: dict[str, str], limit: int) -> list[dict]:
    out = []
    for rel, text in texts.items():
        if lang_of(Path(rel)) not in SOURCE_LANGS and not rel.endswith((".yaml", ".yml", ".json", ".toml", ".ini")):
            continue
        if re.search(r"(test|spec|fixture|mock|example|sample|\.md$)", rel, re.IGNORECASE):
            continue
        for i, line in enumerate(text.splitlines(), 1):
            if len(line) > 2000:
                continue
            sec = SECRET_RE.search(line)
            if sec and not in_cjk_string(line, sec.start()):
                out.append(finding("hardcoded", "high", "likely", rel, i,
                                   "疑似硬编码凭据", clip(re.sub(r"\S{8,}", "***", line), 100),
                                   "绝不自动改写；需人工轮换并从仓库移出"))
                continue
            lp = LOCAL_PATH_RE.search(line)
            if lp and not in_cjk_string(line, lp.start()):
                out.append(finding("hardcoded", "medium", "certain", rel, i,
                                   "本地绝对路径硬编码", clip(line, 120),
                                   "换机即失效，应改为相对路径或配置项"))
    return out[:limit]


def check_tests(root: Path, texts: dict[str, str], files: list[Path]) -> list[dict]:
    test_rels = [r for r in texts if re.search(r"(^|/)(tests?|__tests__|spec)/|(test_|_test\.|\.test\.|\.spec\.)", r)]
    src_rels = [r for r in texts
                if lang_of(Path(r)) in SOURCE_LANGS
                and not re.search(r"(^|/)(tests?|__tests__|spec)/|(test_|_test\.|\.test\.|\.spec\.)", r)
                and Path(r).stem not in ENTRY_STEMS]
    out = []
    if not test_rels and not any(Path(r).name in {"pytest.ini", "jest.config.js", "vitest.config.ts"} for r in texts):
        out.append(finding("test_gap", "info", "certain", None, 0,
                           "项目无任何测试", "未发现测试文件或测试配置",
                           "清理后无法用测试验证行为不变，必须做静态推演与冒烟验证"))
        return out
    test_blob = "\n".join(texts[r] for r in test_rels)
    for rel in src_rels:
        stem = Path(rel).stem
        if len(texts.get(rel, "")) < 400:
            continue
        if not re.search(rf"\b{re.escape(stem)}\b", test_blob):
            out.append(finding("test_gap", "info", "likely", rel, 0,
                               "改动热点缺少对应测试",
                               f"`{stem}` 未出现在任何测试文件中",
                               "该文件若被清理，回归风险靠人工判断"))
    return out[:40]


# --------------------------------------------------------------------------- #
# Report
# --------------------------------------------------------------------------- #

def build_markdown(root: Path, findings: list[dict], stats: dict,
                   caps: dict[str, int] | None = None,
                   baseline: dict | None = None) -> str:
    caps = caps or {}
    ts = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M %Z")
    by_cat: dict[str, list[dict]] = defaultdict(list)
    for f in findings:
        by_cat[f["category"]].append(f)
    for cat in by_cat:
        # 近期改动优先，其次严重度：清理顺序应跟着"上下文漂移"走
        by_cat[cat].sort(key=lambda x: (not x.get("recent"), SEV_ORDER.get(x["severity"], 9),
                                        x["file"], x["line"]))

    recent_n = sum(1 for f in findings if f.get("recent") and f["category"] != "inventory")
    lines = [
        "# 项目体检事实报告",
        "",
        f"- 根目录：`{root}`",
        f"- 生成时间：{ts}",
        f"- 扫描文件：{stats['files']} 个 / 可读文本 {stats['texts']} 个 / 代码 {stats['source']} 个",
        f"- 命中条目：{sum(len(v) for k, v in by_cat.items() if k != 'inventory')} 条"
        + (f"，其中 {recent_n} 条落在近 {stats.get('since_days', 30)} 天改动过的文件里" if recent_n else ""),
    ]
    if stats.get("skipped_budget"):
        lines.append(
            f"- ⚠ 有 {stats['skipped_budget']} 个文件因超出内存预算未读取；"
            f"用 `--focus` 分批扫描，或在报告中声明未覆盖范围。")
    if stats.get("limited"):
        lines.append("- ⚠ 已达文件数上限（`--max-files`），结果不完整。")
    lines += [
        "",
        "> 本报告只提供事实与线索，不做任何修改。`certain` = 可直接核实；"
        "`likely` = 需人工判定；`speculative` = 仅作线索。",
        "",
        "| 分类 | 条目 | 其中高置信可核实 |",
        "| --- | --- | --- |",
    ]
    for cat, label in CATEGORY_META:
        items = by_cat.get(cat, [])
        if not items:
            continue
        certain = sum(1 for i in items if i["confidence"] == "certain")
        lines.append(f"| {label} | {len(items)} | {certain} |")
    lines.append("")

    for cat, label in CATEGORY_META:
        items = by_cat.get(cat, [])
        if not items:
            continue
        lines.append(f"## {label}")
        lines.append("")
        if cat == "inventory":
            dist = " · ".join(f"`{it['title']}` {it['evidence']}" for it in items)
            lines.append(f"- {dist}")
            lines.append("")
            continue
        for it in items:
            loc = f"`{it['file']}`" + (f":{it['line']}" if it["line"] else "") if it["file"] else "（项目级）"
            mark = " 🆕" if it.get("recent") else ""
            lines.append(f"- **[{it['severity']}/{it['confidence']}]**{mark} {loc} — {it['title']}")
            if it["evidence"]:
                lines.append(f"  - 证据：`{it['evidence']}`")
            if it["note"]:
                lines.append(f"  - 提示：{it['note']}")
        cap = caps.get(cat)
        if cap and len(items) >= cap:
            lines.append("")
            lines.append(f"> 该类结果已达上限 {cap} 条，其余未列出。"
                         f"用 `--focus <子目录>` 或更大的 `--limit` 缩小范围后再扫。")
        lines.append("")

    if baseline:
        lines += render_delta(baseline, findings, stats)

    lines.append("---")
    lines.append("")
    lines.append("## 下一步")
    lines.append("")
    lines.append("1. 🆕 条目优先：近 N 天改动过的地方，残留最可能来自上一轮未收尾的修改。")
    lines.append("2. 对 `certain` 且不改变行为/接口的条目，按最小改动直接修复，逐项记入修复摘要。")
    lines.append("3. 其余全部进入待确认清单，附判断依据与潜在风险，不擅自改动。")
    lines.append("4. `speculative` 条目必须先补充验证（运行、日志、调用链追踪）再定性。")
    return "\n".join(lines)


def render_delta(baseline: dict, findings: list[dict], stats: dict) -> list[str]:
    """对比两次扫描，把"清理了多少"变成可核查的数字。"""
    old_counts: dict[str, int] = defaultdict(int)
    for f in baseline.get("findings", []):
        old_counts[f.get("category", "")] += 1
    new_counts: dict[str, int] = defaultdict(int)
    for f in findings:
        new_counts[f["category"]] += 1

    lines = ["## 清理效果对比", ""]
    old_stats = baseline.get("stats", {})
    lines.append(f"- 代码文件：{old_stats.get('source', '?')} → {stats.get('source', '?')}")
    lines.append(f"- 命中总数：{sum(old_counts.values())} → {sum(new_counts.values())}")
    lines.append("")
    lines.append("| 分类 | 清理前 | 现在 | 变化 |")
    lines.append("| --- | --- | --- | --- |")
    for cat, label in CATEGORY_META:
        if cat == "inventory":
            continue
        old, new = old_counts.get(cat, 0), new_counts.get(cat, 0)
        if not old and not new:
            continue
        delta = new - old
        mark = "—" if delta == 0 else (f"{delta:+d}")
        lines.append(f"| {label} | {old} | {new} | {mark} |")
    lines.append("")
    lines.append("> 增长不一定是回退：清理会暴露出此前被掩盖的问题。但**同类条目在第二遍仍然大量存在**，"
                 "说明该类的判定标准没收敛，需要重新审这一类的规则，而不是继续删。")
    lines.append("")
    return lines


def main() -> int:
    ap = argparse.ArgumentParser(description="Read-only repository hygiene scan.")
    ap.add_argument("path", nargs="?", default=".", help="repository root")
    ap.add_argument("--md", metavar="FILE", help="write markdown report to FILE")
    ap.add_argument("--json", metavar="FILE", help="write raw findings to FILE")
    ap.add_argument("--focus", metavar="SUBDIR", help="scan only this subdirectory")
    ap.add_argument("--limit", type=int, default=200, help="max findings per category")
    ap.add_argument("--max-file-bytes", type=int, default=2_000_000)
    ap.add_argument("--max-total-bytes", type=int, default=120_000_000,
                    help="total text budget kept in memory (default 120MB)")
    ap.add_argument("--max-files", type=int, default=30_000)
    ap.add_argument("--since-days", type=int, default=30,
                    help="tag findings in files changed within N days (0 = off)")
    ap.add_argument("--baseline", metavar="JSON",
                    help="previous --json output, to report the cleanup delta")
    ap.add_argument("--no-git", action="store_true")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    root = Path(args.path).resolve()
    if args.focus:
        root = (root / args.focus).resolve()
    if not root.is_dir():
        print(f"error: not a directory: {root}", file=sys.stderr)
        return 3

    pats = load_gitignore(root)
    files = walk_files(root, pats, args.max_files)

    texts: dict[str, str] = {}
    used_bytes = 0
    skipped_budget = 0
    for p in files:
        if p.suffix.lower() in BINARY_EXT:
            continue
        try:
            size = p.stat().st_size
        except OSError:
            continue
        if used_bytes + size > args.max_total_bytes:
            skipped_budget += 1
            continue
        t = safe_read(p, args.max_file_bytes)
        if t is not None:
            texts[p.relative_to(root).as_posix()] = t
            used_bytes += len(t)

    # 只收集"会消费依赖名"的文本：package.json 仅取 scripts，其余配置文件全文。
    # 若把 dependencies 段也算进来，每个未使用依赖都会被自己的声明擦掉。
    config_blob = ""
    for pkg in files:
        name = pkg.name
        if name == "package.json":
            try:
                meta = json.loads(safe_read(pkg, 500_000) or "{}")
                config_blob += "\n".join(str(v) for v in (meta.get("scripts") or {}).values()) + "\n"
            except json.JSONDecodeError:
                continue
        elif name == "tsconfig.json" or pkg.suffix in {".config.js", ".config.ts", ".config.mjs"}:
            config_blob += (safe_read(pkg, 500_000) or "") + "\n"

    manifests = find_manifests(root, files)
    findings: list[dict] = []
    checks = [
        lambda: check_git(root, not args.no_git),
        lambda: check_junk(root, files),
        lambda: check_markers(root, texts, args.limit),
        lambda: check_commented_code(root, texts, args.limit),
        lambda: check_empty(root, texts, args.limit),
        lambda: check_dup_files(root, files, args.max_file_bytes),
        lambda: check_dup_blocks(root, texts, 40),
        lambda: check_broken_imports(root, texts, set(texts)),
        lambda: check_unused_files(root, texts, files),
        lambda: check_deps_js(root, texts, config_blob, manifests),
        lambda: check_deps_py(root, texts, manifests),
        lambda: check_deps_go(root, texts, manifests),
        lambda: check_deps_rust(root, texts, manifests),
        lambda: check_env(root, texts),
        lambda: check_versions(root, texts),
        lambda: check_hardcoded(root, texts, args.limit),
        lambda: check_tests(root, texts, files),
    ]
    for fn in checks:
        try:
            findings.extend(fn())
        except Exception as exc:  # 单项失败不影响整体
            print(f"warn: checker failed: {exc}", file=sys.stderr)

    # 近期改动标记：清理优先级按"是不是最近动过的"排，而不是按绝对严重度
    recent: set[str] = set()
    if not args.no_git and args.since_days > 0:
        recent = git_recent_files(root, args.since_days)
    for f in findings:
        f["recent"] = bool(f["file"]) and f["file"] in recent

    stats = {
        "files": len(files),
        "texts": len(texts),
        "source": sum(1 for r in texts if lang_of(Path(r)) in SOURCE_LANGS),
        "bytes": used_bytes,
        "limited": len(files) >= args.max_files,
        "skipped_budget": skipped_budget,
        "since_days": args.since_days,
        "manifests": len(manifests),
    }
    inventory = [
        finding("inventory", "info", "certain", f"{lang}", 0,
                f"{lang}", f"{n} 个文件")
        for lang, n in sorted(
            defaultdict(int, {
                k: sum(1 for r in texts if lang_of(Path(r)) == k)
                for k in {lang_of(Path(r)) for r in texts} if k
            }).items()
        )
    ]
    findings.extend(inventory)

    caps = {
        "markers": args.limit, "commented_code": args.limit, "empty": args.limit,
        "hardcoded": args.limit, "dup_block": 40, "dup_file": 40,
        "unused_file": 60, "test_gap": 40, "broken_import": 100,
        "deps_unused": 60, "deps_undeclared": 40,
    }
    grouped: dict[str, list[dict]] = defaultdict(list)
    for f in findings:
        grouped[f["category"]].append(f)
    trimmed: list[dict] = []
    for cat, items in grouped.items():
        cap = caps.get(cat)
        trimmed.extend(items[:cap] if cap else items)
    findings = trimmed

    baseline = None
    if args.baseline:
        try:
            baseline = json.loads(Path(args.baseline).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"warn: baseline unreadable ({exc}); delta section skipped", file=sys.stderr)

    md = build_markdown(root, findings, stats, caps, baseline)
    if args.md:
        Path(args.md).write_text(md, encoding="utf-8")
        if not args.quiet:
            print(f"markdown -> {args.md}")
    elif not args.json:
        print(md)

    if args.json:
        payload = {"root": str(root), "stats": stats, "findings": findings}
        Path(args.json).write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                                   encoding="utf-8")
        if not args.quiet:
            print(f"json -> {args.json}")

    if not args.quiet and (args.md or args.json):
        order = [c for c, _ in CATEGORY_META]
        counts = {c: sum(1 for f in findings if f["category"] == c) for c in order}
        print("summary: " + " · ".join(f"{c}={n}" for c, n in counts.items() if n))
    return 0


if __name__ == "__main__":
    sys.exit(main())
