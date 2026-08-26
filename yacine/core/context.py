"""Context Engine — repository understanding & search.

Recursively indexes a target project: file tree, languages, imports,
README/docs, git history, config files, dependency manifests. Everything
becomes searchable via keyword (and optional embedding) search so the agents
can answer "where is X", "what does this module do", etc.

Pure-stdlib + regex so it runs offline with no extra deps.
"""
from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

# Directories / files that should never be indexed.
IGNORE_DIRS = {
    ".git", ".venv", "venv", "node_modules", "__pycache__", ".mypy_cache",
    ".pytest_cache", "dist", "build", ".idea", ".vscode", "target", "bin",
    "obj", ".tox", "site-packages", "logs",
}
MAX_FILE_CHARS = 60_000  # cap per-file content we keep for search


@dataclass
class FileRecord:
    path: str
    rel: str
    language: str
    size: int
    ext: str
    imports: List[str] = field(default_factory=list)
    symbols: List[str] = field(default_factory=list)
    sha: str = ""
    snippet: str = ""  # first chunk of text for preview


_LANG_BY_EXT = {
    ".py": "python", ".js": "javascript", ".ts": "typescript", ".tsx": "typescript",
    ".jsx": "javascript", ".java": "java", ".go": "go", ".rs": "rust",
    ".c": "c", ".cpp": "cpp", ".h": "c", ".hpp": "cpp", ".cs": "csharp",
    ".php": "php", ".rb": "ruby", ".kt": "kotlin", ".swift": "swift",
    ".dart": "dart", ".html": "html", ".css": "css", ".scss": "scss",
    ".json": "json", ".yaml": "yaml", ".yml": "yaml", ".toml": "toml",
    ".md": "markdown", ".sql": "sql", ".sh": "bash", ".ps1": "powershell",
    ".r": "r", ".vue": "vue", ".lua": "lua",
}

_IMPORT_PATTERNS = {
    "python": re.compile(r"^\s*(?:from\s+([\w\.]+)\s+import|import\s+([\w\.]+))", re.M),
    "javascript": re.compile(r"(?:import\s+[^;]+?from\s+['\"]([^'\"]+)['\"]|require\(['\"]([^'\"]+)['\"]\))", re.M),
    "typescript": re.compile(r"(?:import\s+[^;]+?from\s+['\"]([^'\"]+)['\"]|import\s+['\"]([^'\"]+)['\"])", re.M),
    "go": re.compile(r"^\s*\"([^\"]+)\"", re.M),
    "rust": re.compile(r"^\s*use\s+([\w:]+)", re.M),
    "java": re.compile(r"^\s*import\s+([\w.]+);", re.M),
    "csharp": re.compile(r"using\s+([\w.]+);", re.M),
}

_SYMBOL_PATTERNS = {
    "python": re.compile(r"^\s*(?:def|class)\s+([A-Za-z_]\w*)", re.M),
    "javascript": re.compile(r"(?:function\s+([A-Za-z_$][\w$]*)|(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=)", re.M),
    "typescript": re.compile(r"(?:function\s+([A-Za-z_$][\w$]*)|class\s+([A-Za-z_$][\w$]*)|(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=)", re.M),
    "go": re.compile(r"^\s*(?:func|type)\s+([A-Za-z_]\w*)", re.M),
    "rust": re.compile(r"^\s*(?:fn|struct|enum|trait|impl)\s+([A-Za-z_]\w*)", re.M),
}


class ContextEngine:
    """Indexes a project directory and provides search + summaries."""

    def __init__(self, root: Path, logger=None) -> None:
        self.root = Path(root)
        self.logger = logger
        self.files: Dict[str, FileRecord] = {}
        self.ignore_dirs = set(IGNORE_DIRS)
        self.indexed = False

    # -- indexing ---------------------------------------------------------
    def index(self, force: bool = False) -> int:
        if self.indexed and not force:
            return len(self.files)
        self.files.clear()
        if self.logger:
            self.logger.info("context", f"Indexing {self.root}")
        for p in self._walk():
            try:
                rec = self._index_file(p)
                if rec:
                    self.files[str(p)] = rec
            except Exception as e:
                if self.logger:
                    self.logger.warning("context", f"skip {p}: {e}")
        self.indexed = True
        if self.logger:
            self.logger.info("context", f"Indexed {len(self.files)} files")
        return len(self.files)

    def _walk(self):
        for dirpath, dirnames, filenames in os.walk(self.root):
            dirnames[:] = [d for d in dirnames if d not in self.ignore_dirs
                           and not d.startswith(".") or d in (".github",)]
            for fn in filenames:
                full = Path(dirpath) / fn
                yield full

    def _index_file(self, path: Path) -> Optional[FileRecord]:
        rel = str(path.relative_to(self.root))
        ext = path.suffix.lower()
        lang = _LANG_BY_EXT.get(ext, "text")
        try:
            size = path.stat().st_size
        except OSError:
            return None
        if size > 2_000_000:  # skip huge binaries
            return FileRecord(path=str(path), rel=rel, language="binary",
                              size=size, ext=ext)
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            return FileRecord(path=str(path), rel=rel, language=lang,
                              size=size, ext=ext)
        sha = hashlib.sha1(text.encode("utf-8", "ignore")).hexdigest()[:12]
        imports = self._extract(_IMPORT_PATTERNS, lang, text)
        symbols = self._extract(_SYMBOL_PATTERNS, lang, text)
        snippet = text[:MAX_FILE_CHARS]
        return FileRecord(path=str(path), rel=rel, language=lang, size=size,
                          ext=ext, imports=imports, symbols=symbols,
                          sha=sha, snippet=snippet)

    @staticmethod
    def _extract(patterns: Dict[str, re.Pattern], lang: str, text: str) -> List[str]:
        pat = patterns.get(lang)
        if not pat:
            return []
        out = []
        for m in pat.finditer(text):
            val = next((g for g in m.groups() if g), None)
            if val and val not in out:
                out.append(val)
        return out[:200]

    # -- search -----------------------------------------------------------
    def search(self, query: str, limit: int = 10, language: Optional[str] = None) -> List[FileRecord]:
        q = query.lower()
        scored = []
        for rec in self.files.values():
            if language and rec.language != language:
                continue
            score = 0
            rel = rec.rel.lower()
            if q in rel:
                score += 10
            for s in rec.symbols:
                if q in s.lower():
                    score += 5
            for im in rec.imports:
                if q in im.lower():
                    score += 2
            if q in rec.snippet.lower():
                score += 1
            if score > 0:
                scored.append((score, rec))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [r for _, r in scored[:limit]]

    def get_file(self, rel: str) -> Optional[FileRecord]:
        for rec in self.files.values():
            if rec.rel == rel or rec.path.endswith(rel):
                return rec
        return None

    def languages(self) -> Dict[str, int]:
        out: Dict[str, int] = {}
        for rec in self.files.values():
            out[rec.language] = out.get(rec.language, 0) + 1
        return out

    def summary(self) -> str:
        self.index()
        name = self.root.name or str(self.root)
        lines = [f"Project: {name}", f"Files indexed: {len(self.files)}",
                 "Languages:"]
        for lang, n in sorted(self.languages().items(), key=lambda x: -x[1]):
            lines.append(f"  - {lang}: {n}")
        # Top-level layout
        top = sorted({Path(rec.rel).parts[0] for rec in self.files.values() if Path(rec.rel).parts})
        lines.append("Top-level entries: " + ", ".join(top[:30]))
        return "\n".join(lines)

    def to_context_block(self) -> str:
        """Build a compact text block describing the repo for the LLM."""
        self.index()
        parts = [f"# Repository: {self.root.name}\n"]
        parts.append("## File tree")
        tree = self._tree()
        parts.append(tree)
        parts.append("\n## Key symbols")
        sym_lines = []
        for rec in list(self.files.values())[:80]:
            if rec.symbols:
                sym_lines.append(f"{rec.rel}: " + ", ".join(rec.symbols[:15]))
        parts.append("\n".join(sym_lines[:60]))
        # Git history (recent commits) when available.
        hist = self.git_history(limit=10)
        if hist:
            parts.append("\n## Git history (recent)")
            parts.append("\n".join(hist))
        # Detected coding conventions.
        style = self.coding_style()
        if style:
            parts.append("\n## Coding conventions")
            parts.append("\n".join(f"- {k}: {v}" for k, v in style.items()))
        return "\n".join(parts)

    # -- git history ------------------------------------------------------
    def git_history(self, limit: int = 15) -> List[str]:
        """Return recent commit subjects (best-effort; empty if not a repo)."""
        try:
            import subprocess
            r = subprocess.run(
                ["git", "-C", str(self.root), "log", f"--oneline", f"-{limit}"],
                capture_output=True, text=True, timeout=20,
            )
            if r.returncode != 0:
                return []
            return [l.strip() for l in r.stdout.splitlines() if l.strip()]
        except Exception:
            return []

    def is_git_repo(self) -> bool:
        return (self.root / ".git").exists()

    # -- coding style detection ------------------------------------------
    def coding_style(self) -> Dict[str, str]:
        """Heuristically infer conventions so agents can match existing style."""
        conventions: Dict[str, str] = {}
        py_files = [r for r in self.files.values() if r.language == "python"]
        js_files = [r for r in self.files.values()
                    if r.language in ("javascript", "typescript")]
        if py_files:
            indent_spaces = 0
            indent_tabs = 0
            dunder_main = 0
            type_hints = 0
            total = 0
            for r in py_files[:40]:
                for line in (r.snippet or "").splitlines():
                    total += 1
                    if re.match(r"^    ", line):
                        indent_spaces += 1
                    elif re.match(r"^\t", line):
                        indent_tabs += 1
                    if "def main" in line or "__main__" in line:
                        dunder_main += 1
                    if re.search(r":\s*(int|str|float|bool|list|dict|Optional|List|Dict)\b", line):
                        type_hints += 1
            if indent_spaces > indent_tabs:
                conventions["indentation"] = "4 spaces"
            elif indent_tabs > indent_spaces:
                conventions["indentation"] = "tabs"
            if py_files:
                conventions["language"] = "python"
                conventions["type_hints"] = "yes" if type_hints > 0 else "rare/none"
                conventions["main_guard"] = "yes" if dunder_main > 0 else "no"
        if js_files:
            conventions.setdefault("language", "javascript/typescript")
            consts = sum(1 for r in js_files[:40]
                         if "const " in (r.snippet or ""))
            conventions["js_const_style"] = "uses const/let" if consts else "var-style"
        if self.is_git_repo():
            conventions["vcs"] = "git"
        return conventions

    def _tree(self, max_depth: int = 3) -> str:
        roots = {}
        for rec in self.files.values():
            parts = Path(rec.rel).parts
            d = roots
            for p in parts[:-1]:
                d = d.setdefault(p, {})
            d.setdefault("__files__", []).append(parts[-1])
        lines = []

        def walk(d, prefix=""):
            for k in sorted(d):
                if k == "__files__":
                    for f in sorted(d[k])[:8]:
                        lines.append(f"{prefix}{f}")
                else:
                    lines.append(f"{prefix}{k}/")
                    walk(d[k], prefix + "  ")
        walk(roots)
        return "\n".join(lines[:120])
