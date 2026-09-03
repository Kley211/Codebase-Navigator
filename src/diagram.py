"""图渲染抽象：把「模块 + 关系 + 链路」渲染成学习图（当前后端 = Mermaid）。

设计原则：
- 图是「理解层」的渲染能力，不是独立功能点；先把图放进真实学习现场，再谈引擎。
- 图数据尽量来自代码事实（模块 / 入口 / README），LLM 只负责挑重点、定层次、标主链路，
  避免凭空画错拓扑。
- 渲染后端可替换：将来想接 D2 / Archify，只需在模块内新增 render 函数并让上层换调用，
  页面展示与提示词无需改动。

模块职责：
- mermaid_html()：把 Mermaid 代码包成可渲染 HTML（懒加载 mermaid，失败降级为源码）
- extract_mermaid_block() / looks_valid_mermaid()：从模型输出里取出并粗检 Mermaid
- module_map_mermaid()：不依赖 LLM 的静态模块地图（兜底）
- architecture_facts()：供 LLM 构图的紧凑事实（控制 token 成本）
"""

from __future__ import annotations

import html as _html
import json
import re
import uuid
from pathlib import Path

from .context import SOURCE_EXTENSIONS, _clip, _module_layout
from .report import _readme_digest
from .tools.code_analyzer import find_entry_points

# Mermaid 加载来源：先本地文件 → gradio 静态路径 → 两个 CDN 兜底
MERMAID_LOCAL_JS = [
    "/web/mermaid.min.js",                  # 手动放入 web/ 的本地副本
    "/gradio_api/file=web/mermaid.min.js",  # app.py 用 gr.set_static_paths(["web"]) 托管
]
MERMAID_CDN_JS = [
    "https://cdn.jsdelivr.net/npm/mermaid@10.9.1/dist/mermaid.min.js",
    "https://unpkg.com/mermaid@10.9.1/dist/mermaid.min.js",
]
MAX_MODULES_FACTS = 10    # 架构素材里最多列出的模块数
MAX_MAP_NODES = 12        # 静态模块地图节点上限
MAX_RENDER_CHARS = 6000   # 超过则放弃渲染，避免撑爆页面


def extract_mermaid_block(text: str) -> str | None:
    """从模型输出里提取第一个 ```mermaid 代码块。"""
    m = re.search(r"```mermaid\s*\n(.*?)```", text or "", re.S)
    return m.group(1).strip() if m else None


def looks_valid_mermaid(code: str, allow_sequence: bool = False) -> tuple[bool, str]:
    """廉价的 Mermaid 结构预检（不做真正语法解析，只决定是否让模型重写）。"""
    code = (code or "").strip()
    if allow_sequence and code.startswith("sequenceDiagram"):
        head_ok = True
    else:
        head_ok = bool(re.match(r"^(flowchart|graph)\s+(TB|TD|BT|LR|RL)\b", code))
    if not head_ok:
        return False, "必须以 flowchart TD/LR 开头" + ("（或 sequenceDiagram）" if allow_sequence else "")
    arrows = ("-->", "==>", "-.->", "->>", "-->>", "--)")
    if not any(a in code for a in arrows):
        return False, "缺少连线/消息（--> / ==>/->>）"
    for pair in ("[]", "{}", "()"):
        if code.count(pair[0]) != code.count(pair[1]):
            return False, f"{pair[0]}{pair[1]} 括号不配对"
    if code.count('"') % 2 != 0:
        return False, "双引号不配对"
    return True, ""


def _node_label(text: str, limit: int = 60) -> str:
    """清洗成 Mermaid 文本节点标签安全的字符串。"""
    text = re.sub(r"\s+", " ", str(text)).strip()
    text = re.sub(r'["\[\]{}()#\\]', "", text)
    return text[:limit]


def module_map_mermaid(repo_path: str, max_nodes: int = MAX_MAP_NODES) -> str:
    """基于目录结构的静态模块地图（兜底图，无 LLM）。

    结构：仓库根 → 各源码模块（标注源码量）；扁平仓库则把根目录源码文件画成节点。
    定位是「AI 语义图失败时的保底」，不是主力产品图。
    """
    repo = Path(repo_path).resolve()
    nodes: list[tuple[str, str]] = [("R", _node_label(repo.name or "repo"))]
    edges: list[tuple[str, str]] = []

    modules = _module_layout(repo)
    for i, (name, size) in enumerate(modules[: max_nodes - 1]):
        label = f"{_node_label(name)}/ · {max(1, size // 1024)} KB"
        nodes.append((f"M{i}", label))
        edges.append(("R", f"M{i}"))
    if not modules:
        root_files = [
            child for child in sorted(repo.iterdir())
            if child.is_file() and not child.name.startswith(".")
            and child.suffix.lower() in SOURCE_EXTENSIONS
        ]
        for i, child in enumerate(root_files[: max_nodes - 1]):
            try:
                kb = max(1, child.stat().st_size // 1024)
            except OSError:
                kb = 0
            nodes.append((f"F{i}", _node_label(f"{child.name} · {kb} KB")))
            edges.append(("R", f"F{i}"))

    lines = ["flowchart TD"]
    for nid, label in nodes:
        lines.append(f'    {nid}["{label}"]')
    lines += [f"    {src} --> {dst}" for src, dst in edges]
    return "\n".join(lines) + "\n"


def architecture_facts(repo_path: str) -> str:
    """给 LLM 构图的紧凑事实：模块（名称+源码量）+ 根目录源码文件 + 入口点 + README。

    刻意只放「名称级」信息，不让模型读大段源码——它的任务是整理成一张可读的层次图，
    而不是解释每段代码，从而控制 token 成本与生成失败率。
    """
    repo = Path(repo_path).resolve()
    lines = [f"# 仓库：{repo.name}"]

    modules = _module_layout(repo)[:MAX_MODULES_FACTS]
    if modules:
        lines.append("## 源码模块（按源码量降序）")
        lines += [f"- `{name}/`：约 {max(1, size // 1024)} KB" for name, size in modules]

    root_files = sorted(
        child.name for child in repo.iterdir()
        if child.is_file() and not child.name.startswith(".")
        and child.suffix.lower() in SOURCE_EXTENSIONS
    )
    if root_files:
        lines.append("## 根目录源码文件（小仓库/扁平仓库通常是入口）")
        lines += [f"- `{name}`" for name in root_files[:10]]

    lines.append("## 入口点")
    lines.append(_clip(find_entry_points(str(repo)), 800))
    lines.append("## README 摘要")
    lines.append(_clip(_readme_digest(repo), 1400))
    return "\n".join(lines)


_DIAGRAM_HTML_TEMPLATE = """<div class="cn-diagram">
<div class="cn-diagram-bar">
__CAPTION__
<span class="cn-diagram-actions">
  <button type="button" class="cn-copy-btn" data-copy="src-__UID__">复制源码</button>
  <a class="cn-mmd-link" href="https://mermaid.live" target="_blank" rel="noopener">mermaid.live ↗</a>
</span>
</div>
<textarea id="src-__UID__" class="cn-mmd-src" readonly>__TEXT__</textarea>
<div class="cn-diagram-body" id="cn-mmd-__UID__">渲染中…</div>
<details class="cn-diagram-fallback" style="display:none">
<summary>渲染失败？查看 / 复制 Mermaid 源码</summary>
<pre>__CODE__</pre>
</details>
</div>
<script>
(function () {
  var root = document.getElementById('cn-mmd-__UID__');
  var details = root.parentElement.querySelector('.cn-diagram-fallback');
  var srcBox = document.getElementById('src-__UID__');
  var code = __PAYLOAD__;
  function esc(s) { return String(s).replace(/[<>&]/g, function (c) { return {'<':'&lt;','>':'&gt;','&':'&amp;'}[c]; }); }
  function failed(msg) {
    root.innerHTML = '<p class="cn-diagram-note">' + esc(msg) + '</p>';
    if (details) details.style.display = '';
  }
  function draw() {
    if (!window.mermaid) { failed('未加载到 Mermaid 渲染库。'); return; }
    try {
      window.mermaid.initialize({
        startOnLoad: false,
        securityLevel: 'loose',
        theme: 'base',
        themeVariables: {
          background: '#0a0e0c',
          primaryColor: '#10170f',
          primaryBorderColor: '#f2a62a',
          primaryTextColor: '#e8e6df',
          secondaryColor: '#131f19',
          tertiaryColor: '#0d1210',
          lineColor: '#8aa293',
          clusterBkg: 'rgba(242,166,42,0.06)',
          clusterBorder: '#3a4a40',
          fontSize: '14px',
          fontFamily: "'Source Sans Pro','IBM Plex Mono',sans-serif"
        }
      });
      root.textContent = code;
      window.mermaid.run({ nodes: [root] });
      if (details) details.style.display = 'none';
    } catch (e) {
      failed('图解析失败：' + esc(e && e.message || e));
    }
  }
  function loadChain(list, i) {
    if (!list || i >= list.length) { failed('未加载到 Mermaid：请联网（CDN）或把 mermaid.min.js 放到 web/ 目录。'); return; }
    var s = document.createElement('script');
    s.src = list[i];
    s.onload = function () { if (window.mermaid) { draw(); } else { loadChain(list, i + 1); } };
    s.onerror = function () { loadChain(list, i + 1); };
    document.head.appendChild(s);
  }
  if (window.mermaid) { draw(); }
  else { loadChain(__ALL_JS__, 0); }
  var copyBtn = document.querySelector('[data-copy="src-__UID__"]');
  if (copyBtn && srcBox) {
    copyBtn.addEventListener('click', function () {
      var done = false;
      srcBox.select();
      srcBox.setSelectionRange(0, 999999);
      try { done = document.execCommand('copy'); } catch (e) {}
      if (!done && navigator.clipboard) { navigator.clipboard.writeText(srcBox.value).then(function () { done = true; }); }
      copyBtn.textContent = done ? '已复制 ✓' : '复制';
      setTimeout(function () { copyBtn.textContent = '复制源码'; }, 1600);
    });
  }
})();
</script>
"""


def mermaid_html(code: str, caption: str = "") -> str:
    """把 Mermaid 代码包成可渲染 HTML；渲染失败时保留源码便于复制到 mermaid.live。"""
    code = (code or "").strip()
    if len(code) > MAX_RENDER_CHARS:
        body = (
            '<div class="cn-diagram-note">⚠️ 图过长（%d 字符），已改为源码展示。</div>'
            '<pre class="cn-diagram-fallback">%s</pre>'
        ) % (len(code), _html.escape(code[:MAX_RENDER_CHARS]))
        return '<div class="cn-diagram">%s</div>' % body

    uid = uuid.uuid4().hex[:10]
    payload = json.dumps(code, ensure_ascii=False).replace("</", "<\\/")
    caption_html = '<div class="cn-diagram-caption">%s</div>' % _html.escape(caption or "")
    all_js = json.dumps(MERMAID_LOCAL_JS + MERMAID_CDN_JS, ensure_ascii=False)
    return (
        _DIAGRAM_HTML_TEMPLATE
        .replace("__UID__", uid)
        .replace("__CAPTION__", caption_html)
        .replace("__ALL_JS__", all_js)
        .replace("__PAYLOAD__", payload)
        .replace("__CODE__", _html.escape(code))
        .replace("__TEXT__", _html.escape(code))
    )
