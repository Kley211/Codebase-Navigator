"""代码分析工具：解析入口点、依赖、imports、函数签名（纯规则，无 AI）。"""

from __future__ import annotations

import json
import re
from pathlib import Path

from .file_explorer import IGNORE_DIRS

# 常见入口文件（按语言）
ENTRY_POINT_PATTERNS = {
    "python": [
        "main.py", "app.py", "__main__.py", "run.py", "server.py",
        "cli.py", "manage.py", "wsgi.py", "asgi.py",
    ],
    "javascript": [
        "index.js", "index.ts", "index.tsx", "main.js", "main.ts",
        "app.js", "app.ts", "server.js", "server.ts",
    ],
    "go": ["main.go", "cmd/main.go"],
    "rust": ["main.rs", "lib.rs"],
    "java": ["Main.java", "Application.java"],
}

# import 解析正则（按扩展名）
IMPORT_PATTERNS = {
    ".py": [r"^\s*(?:import|from)\s+(\S+)"],
    ".ts": [
        r"^import\s+.*\s+from\s+['\"]([^'\"]+)['\"]",
        r"^import\s+['\"]([^'\"]+)['\"]",
        r"^export\s+.*\s+from\s+['\"]([^'\"]+)['\"]",
    ],
    ".tsx": [
        r"^import\s+.*\s+from\s+['\"]([^'\"]+)['\"]",
        r"^import\s+['\"]([^'\"]+)['\"]",
    ],
    ".js": [
        r"^import\s+.*\s+from\s+['\"]([^'\"]+)['\"]",
        r"^const\s+\w+\s*=\s*require\(['\"]([^'\"]+)['\"]\)",
        r"^require\(['\"]([^'\"]+)['\"]\)",
    ],
    ".jsx": [
        r"^import\s+.*\s+from\s+['\"]([^'\"]+)['\"]",
        r"^import\s+['\"]([^'\"]+)['\"]",
    ],
    ".go": [
        r"^\s*import\s+['\"]([^'\"]+)['\"]",
        r"^\s+['\"]([^'\"]+)['\"]",
    ],
    ".rs": [
        r"^\s*use\s+(\S+)",
        r"^\s*extern\s+crate\s+(\S+)",
    ],
}

# Python 标准库（简化集合）
PY_STDLIB = {
    "os", "sys", "re", "json", "typing", "pathlib", "collections",
    "datetime", "time", "logging", "subprocess", "asyncio", "functools",
    "itertools", "dataclasses", "abc", "enum", "copy", "io", "math",
    "random", "string", "tempfile", "shutil", "glob", "argparse",
    "unittest", "threading", "multiprocessing", "socket", "http",
    "urllib", "email", "html", "xml", "sqlite3", "hashlib", "secrets",
    "contextlib", "queue", "traceback", "warnings", "statistics",
}


def get_imports(file_path: str) -> str:
    """提取文件的 import 语句，按 标准库/第三方/本地 分类。"""
    path = Path(file_path)
    if not path.exists():
        return f"错误：文件不存在 - {file_path}"
    suffix = path.suffix.lower()
    if suffix not in IMPORT_PATTERNS:
        return f"错误：暂不支持文件类型 '{suffix}'"

    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
    except Exception as e:
        return f"读取失败：{e}"

    imports: set[str] = set()
    for line in content.splitlines():
        line = line.strip()
        for pattern in IMPORT_PATTERNS[suffix]:
            m = re.match(pattern, line)
            if m:
                imports.add(m.group(1))
                break

    if not imports:
        return f"{path.name} 中没有找到 import 语句"

    stdlib, third_party, local = [], [], []
    for imp in sorted(imports):
        if imp.startswith((".", "./", "../")):
            local.append(imp)
        elif suffix == ".py" and imp.split(".")[0] in PY_STDLIB:
            stdlib.append(imp)
        elif imp.startswith("@") or "/" not in imp:
            third_party.append(imp)
        else:
            local.append(imp)

    lines = [f"📦 {path.name} 的 imports：", "─" * 50]
    if stdlib:
        lines += ["", "🔧 标准库："] + [f"  - {i}" for i in stdlib]
    if third_party:
        lines += ["", "📚 第三方："] + [f"  - {i}" for i in third_party]
    if local:
        lines += ["", "📁 本地/相对："] + [f"  - {i}" for i in local]
    return "\n".join(lines)


def find_entry_points(repo_path: str) -> str:
    """识别代码库中的主要入口点（常见文件名 + 配置文件声明）。"""
    repo = Path(repo_path)
    if not repo.exists():
        return f"错误：路径不存在 - {repo_path}"

    found = []
    for lang, patterns in ENTRY_POINT_PATTERNS.items():
        for pattern in patterns:
            for match in repo.glob(f"**/{pattern}"):
                rel = match.relative_to(repo)
                if any(part in IGNORE_DIRS for part in rel.parts):
                    continue
                found.append((str(rel), lang, pattern))

    # package.json 的 main / scripts
    pkg = repo / "package.json"
    if pkg.exists():
        try:
            data = json.loads(pkg.read_text(encoding="utf-8"))
            if data.get("main"):
                found.append((str(data["main"]), "javascript", "package.json main"))
            for name in ("start", "dev"):
                if name in data.get("scripts", {}):
                    found.append((f"npm run {name}", "javascript", str(data["scripts"][name])))
        except Exception:
            pass

    # pyproject.toml 的 [project.scripts]
    pyproject = repo / "pyproject.toml"
    if pyproject.exists():
        content = pyproject.read_text(encoding="utf-8", errors="replace")
        m = re.search(r"\[project\.scripts\]\s*\n((?:\s*\w+\s*=.*\n?)+)", content)
        if m:
            for line in m.group(1).strip().splitlines():
                if "=" in line:
                    name, target = line.split("=", 1)
                    found.append((name.strip(), "python", f"CLI: {target.strip()}"))

    if not found:
        return "未发现明显的入口点，可尝试查找 main 函数或启动脚本。"

    lines = ["🚀 发现的入口点：", "─" * 50]
    for path, lang, note in found:
        lines.append(f"  [{lang}] {path}")
        if note != path:
            lines.append(f"         └── {note}")
    return "\n".join(lines)


def analyze_dependencies(repo_path: str) -> str:
    """从依赖文件解析项目依赖（requirements/pyproject/package.json/go.mod/Cargo.toml）。"""
    repo = Path(repo_path)
    if not repo.exists():
        return f"错误：路径不存在 - {repo_path}"

    lines = ["📦 依赖分析：", "─" * 50]

    def add_deps(title: str, deps: list[str], limit: int = 25):
        if deps:
            lines.append(f"\n{title}")
            for dep in deps[:limit]:
                lines.append(f"  - {dep}")
            if len(deps) > limit:
                lines.append(f"  ... 共 {len(deps)} 项，仅显示前 {limit} 项")

    # Python: requirements.txt
    req = repo / "requirements.txt"
    if req.exists():
        deps = []
        for line in req.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or line.startswith("-"):
                continue
            deps.append(re.split(r"[<>=!~]", line)[0].strip())
        add_deps("🐍 Python (requirements.txt)：", deps)

    # Python: pyproject.toml
    pyproject = repo / "pyproject.toml"
    if pyproject.exists():
        content = pyproject.read_text(encoding="utf-8", errors="replace")
        deps = re.findall(r'^\s*["\']([^"\']+)["\']\s*$', content, re.M)
        deps = [re.split(r"[<>=!~]", d)[0].strip() for d in deps if d]
        add_deps("🐍 Python (pyproject.toml)：", deps)

    # Node: package.json
    pkg = repo / "package.json"
    if pkg.exists():
        try:
            data = json.loads(pkg.read_text(encoding="utf-8"))
            add_deps("🟨 Node.js (dependencies)：", list(data.get("dependencies", {})))
            add_deps("🟨 Node.js (devDependencies)：", list(data.get("devDependencies", {})), limit=10)
        except Exception:
            pass

    # Go: go.mod
    go_mod = repo / "go.mod"
    if go_mod.exists():
        deps = []
        for line in go_mod.read_text(encoding="utf-8", errors="replace").splitlines():
            m = re.match(r"\s+(\S+)\s+v\d", line)
            if m and m.group(1) != "go":
                deps.append(m.group(1))
        add_deps("🐹 Go (go.mod)：", deps)

    # Rust: Cargo.toml
    cargo = repo / "Cargo.toml"
    if cargo.exists():
        deps = []
        in_deps = False
        for line in cargo.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if line.startswith("[dependencies"):
                in_deps = True
                continue
            if in_deps and line.startswith("["):
                break
            if in_deps and "=" in line and not line.startswith("#"):
                deps.append(line.split("=")[0].strip())
        add_deps("🦀 Rust (Cargo.toml)：", deps)

    if len(lines) == 2:
        lines.append("\n未找到依赖文件（requirements.txt / pyproject.toml / package.json / go.mod / Cargo.toml）")
    return "\n".join(lines)


def get_function_signatures(file_path: str) -> str:
    """提取文件中的函数/类签名（含行号）。"""
    path = Path(file_path)
    if not path.exists():
        return f"错误：文件不存在 - {file_path}"
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception as e:
        return f"读取失败：{e}"

    suffix = path.suffix.lower()
    signatures: list[tuple[int, str]] = []

    if suffix == ".py":
        class_re = re.compile(r"^class\s+(\w+)(?:\(.*?\))?:")
        func_re = re.compile(r"^(\s*)(?:async\s+)?def\s+(\w+)\s*\((.*?)\)(?:\s*->.*?)?:")
        current_class: str | None = None
        for i, line in enumerate(lines, 1):
            cm = class_re.match(line)
            if cm:
                current_class = cm.group(1)
                signatures.append((i, f"class {current_class}"))
                continue
            fm = func_re.match(line)
            if fm:
                indent, name, params = fm.groups()
                if indent and current_class:
                    signatures.append((i, f"  {current_class}.{name}(...)  # 方法"))
                else:
                    current_class = None
                    signatures.append((i, f"def {name}({params[:60]}{'...' if len(params) > 60 else ''})"))

    elif suffix in {".ts", ".tsx", ".js", ".jsx"}:
        patterns = [
            re.compile(r"^(?:export\s+)?(?:async\s+)?function\s+(\w+)\s*\(.*?\)"),
            re.compile(r"^(?:export\s+)?const\s+(\w+)\s*=\s*(?:async\s+)?\("),
            re.compile(r"^(?:export\s+)?class\s+(\w+)"),
        ]
        for i, line in enumerate(lines, 1):
            for p in patterns:
                if p.match(line):
                    signatures.append((i, line.strip()[:80]))
                    break

    elif suffix == ".go":
        func_re = re.compile(r"^func\s+(?:\(.*?\)\s*)?(\w+)\s*\(")
        type_re = re.compile(r"^type\s+(\w+)\s+(?:struct|interface)")
        for i, line in enumerate(lines, 1):
            if func_re.match(line) or type_re.match(line):
                signatures.append((i, line.strip()[:80]))

    elif suffix == ".rs":
        patterns = [
            re.compile(r"^\s*(?:pub\s+)?(?:async\s+)?fn\s+(\w+)\s*[<(]"),
            re.compile(r"^\s*(?:pub\s+)?struct\s+(\w+)"),
            re.compile(r"^\s*(?:pub\s+)?enum\s+(\w+)"),
            re.compile(r"^\s*impl\s+"),
        ]
        for i, line in enumerate(lines, 1):
            for p in patterns:
                if p.match(line):
                    signatures.append((i, line.strip()[:80]))
                    break

    if not signatures:
        return f"{path.name} 中未发现函数/类"

    lines_out = [f"📋 {path.name} 中的签名：", "─" * 50]
    lines_out += [f"  L{no:4d}: {sig}" for no, sig in signatures]
    return "\n".join(lines_out)