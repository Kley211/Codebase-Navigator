"""带读剧本（Learn Plan）：把代码库转成一步步可执行的「学习剧本」。

目标用户：会装环境、能跑通 pip 项目，但没系统读过开源源码的新手。
验收标准：每一步学完 = 能复述 + 能改 + 能讲。

validate_learn_plan() 是纯函数结构校验，供 Agent 生成后自检、以及离线单测使用。
"""

from __future__ import annotations

import re

MIN_STEPS = 5
MAX_STEPS = 8
MIN_CITES = 10

# 步骤标题：### 第 1 步 / ## 第 3 步……
_STEP_RE = re.compile(r"^#{2,3}\s*第\s*\d+\s*步", re.M)
# 引用：src/flask/app.py:76 或 src/flask/app.py:76-90
_CITE_RE = re.compile(r"[\w./\\\-]+\.\w+:\d+(?:-\d+)?")
# 每个步骤必须覆盖的小节
_SECTIONS = [
    "目标",
    "读这里",
    "讲解要点",
    "动手任务",
    "苏格拉底自检",
    "合格回答应包含",
    "解锁条件",
]
# 开放性问题特征词（苏格拉底式，而非判断题）
_OPEN_HINT_WORDS = ("为什么", "如果不", "怎么做", "如果", "换个", "你会", "换成")

# 剧末必须出现的验收章节关键词
_FINAL_KEYS = ("剧末验收", "毕业", "验收")


def split_steps(text: str) -> list[str]:
    """按“第 N 步”标题切分剧本，返回每步文本块。"""
    matches = list(_STEP_RE.finditer(text or ""))
    steps = []
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        steps.append(text[m.start():end])
    return steps


def validate_learn_plan(text: str) -> tuple[bool, list[str]]:
    """结构校验带读剧本，返回 (是否合格, 问题列表)。

    覆盖点：步骤数量、每步必备小节、苏格拉底开放式自检、
    引用数量、剧末验收章节。不做语义判定（那是 AI 评测的活）。
    """
    text = text or ""
    problems: list[str] = []
    steps = split_steps(text)

    if not steps:
        problems.append("未找到任何“第 N 步”章节")
    elif not MIN_STEPS <= len(steps) <= MAX_STEPS:
        problems.append(f"步骤数应为 {MIN_STEPS}-{MAX_STEPS} 步，实际 {len(steps)} 步")

    for i, step in enumerate(steps, 1):
        missing = [s for s in _SECTIONS if s not in step]
        if missing:
            problems.append(f"第 {i} 步缺少小节：{'、'.join(missing)}")
        if not any(k in step for k in _OPEN_HINT_WORDS):
            problems.append(f"第 {i} 步缺少开放式引导问法（建议用：为什么 / 如果不 / 你会怎么做）")
        q_count = step.count("？") + step.count("?")
        if q_count < 2:
            problems.append(f"第 {i} 步的自检问题数量不足（至少 2 个问句）")

    cites = _CITE_RE.findall(text)
    if len(cites) < MIN_CITES:
        problems.append(f"全文引用（路径:行号）应至少 {MIN_CITES} 处，实际 {len(cites)} 处")

    if not any(k in text for k in _FINAL_KEYS):
        problems.append("缺少「剧末验收」或等价毕业章节")

    return (len(problems) == 0, problems)


def retry_hint(problems: list[str]) -> str:
    """把结构问题转成给模型的修改指令。"""
    if not problems:
        return "你刚才的输出结构合格，但请检查语义是否真正可执行。"
    detail = "；".join(problems[:8])
    return (
        "你刚才的输出不符合带读剧本格式，请原样重写并修正以下问题："
        f"{detail}。每步必须包含「目标/读这里/讲解要点/动手任务/苏格拉底自检/"
        "合格回答应包含/解锁条件」；引用必须是 `路径:行号` 或 `路径:起-止`，"
        "行号只能取自核心文件编号行。直接输出修正后的完整剧本，禁止解释。"
    )
