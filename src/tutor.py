"""交互式带读陪练（Socratic Tutor）：把「带读剧本」变成一问一答的学习会话。

工作方式：
- 剧本来自 --learn 生成的 5-8 步 Learn Plan，每步含 目标/读这里/讲解要点/
  动手任务/苏格拉底自检/合格回答应包含/解锁条件。
- 每步先让学习者按「读这里」去读（精确到行号区间），读完进入自检：
  逐条对照「合格回答应包含」发起苏格拉底式追问，AI 判定覆盖度；
  没覆盖就换角度再问，覆盖后才解锁下一步。
- 判定由 LLM 完成（JSON 输出 mastered/comment/follow_up），是
  「对话式判定」而不是关键词匹配。
- 验收标准贯穿全程：能复述 + 能改 + 能讲。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from .learn import split_steps

MAX_ATTEMPTS = 4  # 每个自检要点最多追问轮数

_SECTION_LABELS = (
    "目标", "读这里", "讲解要点", "动手任务",
    "苏格拉底自检", "合格回答应包含", "解锁条件",
)
_SECTION_RE = re.compile(r"\*\*(" + "|".join(_SECTION_LABELS) + r")\*\*")
_BULLET_RE = re.compile(r"(?m)^\s*[-*]\s*(.+?)\s*$")
_QUESTION_RE = re.compile(r"(?m)^\s*[-*]\s*问题\s*\d*\s*[：:]\s*(.+)$")
_RUBRIC_RE = re.compile(r"(?m)^\s*\d+\s*[.、)]\s*(.+)$")
_CITE_RE = re.compile(r"[\w./\\\-]+\.\w+:\d+(?:-\d+)?")
_FINAL_HEAD_RE = re.compile(r"(?m)^#{1,3}\s*(剧末验收|毕业|.*验收关卡).*$")

JUDGE_SYS = """你是「Codebase Navigator」的苏格拉底式带读导师。你的职责是判断学习者的回答是否覆盖了当前自检要点的核心内容，并用追问引导他自己想出来。

规则：
1. 只判断「要点覆盖度」，不要输出完整答案，不要长篇讲解。
2. 已覆盖 → mastered=true，comment 用一两句肯定并点出他覆盖到的关键点。
3. 未覆盖 → mastered=false，comment 用一两句指出缺口（只说缺什么，不给答案）；follow_up 出一个新的苏格拉底追问（为什么/如果不/换你会怎么做），引导他自己补上。
4. 只输出一个 JSON 对象：{"mastered": bool, "comment": "…", "follow_up": "…"}，不要输出任何其他内容。"""

TASK_JUDGE_SYS = """你是「Codebase Navigator」的带读导师。学习者在完成一个动手实验/改造任务后向你汇报结果。你的职责是判断他是否真的动手做了并观察到了结果。

规则：
1. done=true 的条件：汇报里包含「做了什么改动/命令 + 观察到的实际结果 + 一句话解释为什么」（三要素缺一不可）。
2. 未达标 → done=false，comment 指出缺哪一要素；follow_up 出一个引导问题让他补上（禁止直接给答案）。
3. 只输出一个 JSON 对象：{"done": bool, "comment": "…", "follow_up": "…"}，不要输出任何其他内容。"""

HINT_SYS = """你是「Codebase Navigator」的带读导师。学习者卡住了，需要提示。
给出 1-2 句不剧透答案的引导（比如让他回去看哪一段、注意哪个函数、思考哪个角度），不要说破结论。直接输出提示文本。"""


@dataclass
class LearnStep:
    title: str = ""
    objective: str = ""
    read_lines: list[str] = field(default_factory=list)
    hints: list[str] = field(default_factory=list)
    task: str = ""
    questions: list[str] = field(default_factory=list)
    rubric: list[str] = field(default_factory=list)
    unlock: str = ""
    raw: str = ""


def split_sections(step_text: str) -> dict[str, str]:
    """按 **小节名** 切分步骤文本，返回 {小节名: 内容}。"""
    out: dict[str, str] = {}
    marks = list(_SECTION_RE.finditer(step_text))
    for i, m in enumerate(marks):
        label = m.group(1)
        end = marks[i + 1].start() if i + 1 < len(marks) else len(step_text)
        content = step_text[m.end():end]
        content = content.lstrip("：:").strip()
        out[label] = content
    return out


def _first_heading(raw: str) -> str:
    m = re.search(r"(?m)^#{2,3}\s*(.+)$", raw)
    return m.group(1).strip() if m else ""


def _bullet_lines(content: str) -> list[str]:
    return [m.group(1).strip() for m in _BULLET_RE.finditer(content or "")]


def _numbered_points(content: str) -> list[str]:
    """把「合格回答应包含」拆成独立要点。

    模型可能写成多行（1. …\n2. …），也可能挤在一行用分号分隔
    （1. …；2. …），这里统一按编号切分。
    """
    segments = re.split(r"(?=\d+[.、)])", content or "")
    points = []
    for seg in segments:
        m = re.match(r"^\d+[.、)]\s*(.*)$", seg.strip("；; \n"), re.S)
        if m:
            points.append(m.group(1).strip())
    return points

def parse_plan(text: str) -> tuple[list[LearnStep], str]:
    """解析剧本正文 → (步骤列表, 剧末验收文本)。"""
    steps: list[LearnStep] = []
    tail = ""
    final_match = _FINAL_HEAD_RE.search(text or "")
    if final_match:
        tail = text[final_match.start():].strip()
        text = text[:final_match.start()]
    for raw in split_steps(text):
        secs = split_sections(raw)
        step = LearnStep(
            title=_first_heading(raw),
            objective=secs.get("目标", ""),
            read_lines=_bullet_lines(secs.get("读这里", "")),
            hints=_bullet_lines(secs.get("讲解要点", "")),
            task=secs.get("动手任务", "").strip(),
            questions=[
                m.group(1).strip()
                for m in _QUESTION_RE.finditer(secs.get("苏格拉底自检", ""))
            ],
            rubric=_numbered_points(secs.get("合格回答应包含", "")),
            unlock=secs.get("解锁条件", "").strip(),
            raw=raw.strip(),
        )
        steps.append(step)
    return steps, tail


def _extract_json(text: str) -> dict | None:
    """从模型回复中稳健提取 JSON 对象。"""
    text = (text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[A-Za-z]*\s*", "", text).rstrip("`").strip()
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        return None


def _judge_user(step: LearnStep, question: str, point: str, history: list[str], answer: str) -> str:
    """拼装自检判定请求。"""
    lines = [
        "# 学习步骤",
        step.raw[:2600],
        "",
        "# 当前自检",
        f"提问：{question}",
        f"要求覆盖的要点：{point}",
        "",
        "# 本轮对话",
    ]
    for i, (q, a) in enumerate(history, 1):
        lines.append(f"[{i}] 你问：{q}")
        lines.append(f"[{i}] 学习者答：{a}")
    lines.append(f"[{len(history) + 1}] 你问：{question}")
    lines.append(f"[{len(history) + 1}] 学习者最新回答：{answer}")
    lines.append("")
    lines.append("请按规则输出 JSON 判定。")
    return "\n".join(lines)


def _task_user(step: LearnStep, task: str, history: list[str], answer: str) -> str:
    lines = [
        "# 学习步骤（动手任务）",
        step.raw[:2000],
        "",
        "# 动手任务",
        task,
        "",
        "# 学习者汇报",
    ]
    lines += [f"[{i}] 汇报：{a}" for i, (_, a) in enumerate(history, 1)]
    lines.append(f"[{len(history) + 1}] 汇报：{answer}")
    lines.append("")
    lines.append("请按规则输出 JSON 判定。")
    return "\n".join(lines)

class TutorSession:
    """一次带读会话：逐步骤推进，苏格拉底自检 + 动手实验判定。"""

    def __init__(self, llm, plan_text: str, ask=input, say=print):
        self.llm = llm  # 提供 direct(system, user, temperature, max_tokens)
        self.ask = ask
        self.say = say
        steps, tail = parse_plan(plan_text)
        self.steps = steps
        self.tail = tail
        self.status = "running"

    # ---------- 工具 ----------
    def _verdict(self, system: str, user: str, key: str) -> dict:
        """调 LLM 判定，返回归一化 {key: bool, comment, follow_up}。"""
        text = self.llm.direct(system, user, temperature=0.1, max_tokens=700)
        obj = _extract_json(text) or {}
        ok = bool(obj.get(key)) if key in obj else False
        return {
            key: ok,
            "comment": str(obj.get("comment", "")).strip(),
            "follow_up": str(obj.get("follow_up", "")).strip(),
        }

    def _say_block(self, text: str) -> None:
        if text:
            self.say(text)

    def _read(self, prompt: str) -> str:
        try:
            line = self.ask(prompt).strip()
        except (EOFError, KeyboardInterrupt):
            return "退出"
        if not line:
            return self._read(prompt)
        return line

    # ---------- 主循环 ----------
    def run(self) -> str:
        if not self.steps:
            self.say("⚠️ 剧本解析失败：没有找到可用的步骤。请先重新生成剧本（--learn）。")
            return "failed"
        self.say("=" * 64)
        self.say("🎓 Codebase Navigator · 带读陪练会话")
        self.say(f"剧本共 {len(self.steps)} 步。每一步：先按提示读代码，再接受苏格拉底式自检；")
        self.say("自检和动手实验都通过才会解锁下一步。随时输入「提示」要引导，输入「退出」结束。")
        self.say("=" * 64)

        for idx, step in enumerate(self.steps, 1):
            if self.status == "aborted":
                return "aborted"
            self._run_step(idx, step)
            if self.status == "aborted":
                return "aborted"

        if self.status == "aborted":
            return "aborted"
        self._run_graduation()
        return "completed"

    # ---------- 单步 ----------
    def _run_step(self, idx: int, step: LearnStep) -> None:
        self.say("\n" + "■" * 64)
        self.say(f"第 {idx}/{len(self.steps)} 步｜{step.title}")
        self.say("■" * 64)
        if step.objective:
            self.say(f"🎯 目标：{step.objective}")
        if step.read_lines:
            self.say("")
            self.say("📖 读这里（按顺序打开，不用一次读完，可边读边想）：")
            for line in step.read_lines:
                self.say(f"  · {line}")
        if step.hints:
            self.say("")
            self.say("💡 边读边想（想不出来可在自检时输入「提示」）：")
            for h in step.hints[:4]:
                self.say(f"  · {h}")

        while True:
            cmd = self._read("读完请输 go 开始自检（或 提示 / 退出）：")
            if cmd == "退出":
                self.status = "aborted"
                return
            if cmd == "提示":
                self._hint(step, "")
                continue
            if cmd == "go":
                break
            self.say("（请输入 go / 提示 / 退出）")

        # —— 苏格拉底自检：逐条对照「合格回答应包含」——
        rubric = step.rubric or [step.objective]
        questions = step.questions or ["你能用自己的话讲讲刚读的这段在做什么吗？"]
        for j, point in enumerate(rubric, 1):
            if self.status == "aborted":
                return
            question = questions[(j - 1) % len(questions)]
            self._quiz_point(step, j, len(rubric), question, point)

        # —— 动手任务 ——
        if step.task:
            self._task_phase(step)

        self.say("")
        if step.unlock:
            self.say(f"🔓 解锁条件达成：{step.unlock}")
        self.say(f"✅ 第 {idx} 步完成——你已经能复述它了；继续下一步前，先用自己的话把它讲一遍。")

    def _quiz_point(self, step: LearnStep, j: int, total: int, question: str, point: str) -> None:
        self.say(f"\n🎯 自检 {j}/{total}｜要求：{point}")
        history: list[tuple[str, str]] = []
        follow = question
        for attempt in range(1, MAX_ATTEMPTS + 1):
            if self.status == "aborted":
                return
            role = "问" if attempt == 1 else "追问"
            self.say(f"\n🧑‍🏫 {role}：{follow}")
            ans = self._read("你的回答：")
            if ans == "退出":
                self.status = "aborted"
                return
            if ans == "提示":
                self._hint(step, point)
                continue
            history.append((follow, ans))
            verdict = self._verdict(
                JUDGE_SYS, _judge_user(step, follow, point, history, ans), "mastered"
            )
            if verdict["mastered"]:
                self.say(f"✅ 已覆盖要点 {j}：{verdict['comment'] or '回答到位。'}")
                return
            follow = verdict.get("follow_up") or question
            note = verdict.get("comment") or ""
            if note:
                self.say(f"  · {note}")
            if attempt == MAX_ATTEMPTS:
                self.say(f"（多次追问仍未覆盖，先记下参考答案再回看代码）\n  · 参考要点：{point}")
                return
        self.say("")

    def _task_phase(self, step: LearnStep) -> None:
        self.say("\n🛠️ 动手任务（做之前先不看答案，做出来才算数）：")
        self._say_block(step.task)
        self.say("")
        history: list[tuple[str, str]] = []
        for attempt in range(1, MAX_ATTEMPTS + 1):
            if self.status == "aborted":
                return
            ans = self._read("完成后汇报：你改了什么、看到了什么结果、为什么（或 提示 / 退出）：")
            if ans == "退出":
                self.status = "aborted"
                return
            if ans == "提示":
                self._hint(step, step.task)
                continue
            history.append(("", ans))
            verdict = self._verdict(
                TASK_JUDGE_SYS, _task_user(step, step.task, history, ans), "done"
            )
            if verdict["done"]:
                self.say(f"✅ 动手实验通过：{verdict['comment'] or '干得漂亮。'}")
                return
            follow = verdict.get("follow_up") or ""
            note = verdict.get("comment") or ""
            if note:
                self.say(f"  · {note}")
            if follow:
                self.say(f"🧑‍🏫 追问：{follow}")
            if attempt == MAX_ATTEMPTS:
                self.say("（动手实验暂未达标，建议回到任务描述重做；也可以继续，但请记得补上。）")
                return

    def _hint(self, step: LearnStep, context: str) -> None:
        user = f"学习者卡在：{context or step.title}\n请给一句不剧透的引导提示。"
        try:
            hint = self.llm.direct(HINT_SYS, user, temperature=0.4, max_tokens=300)
        except Exception as e:  # 模型不可用时退化为静态提示
            hint = f"回到「读这里」的段落，重点想：这段代码的职责是什么？为什么这么写？({e})"
        self.say(f"💡 提示：{hint.strip()}")

    # ---------- 毕业关卡 ----------
    def _run_graduation(self) -> None:
        self.say("\n" + "🏁" * 10)
        self.say("全部步骤已走完，进入毕业关卡")
        self.say("🏁" * 10)
        if self.tail:
            self.say(self.tail)
        self.say("")
        self.say("完成「改出来」后回来汇报（改了什么 / 结果 / 为什么），我会帮你把关。")
        history: list[tuple[str, str]] = []
        for attempt in range(1, MAX_ATTEMPTS + 1):
            if self.status == "aborted":
                return
            ans = self._read("毕业汇报（或 跳过 / 退出）：")
            if ans == "退出":
                self.status = "aborted"
                return
            if ans in ("跳过", "skip"):
                self.say("毕业关卡可稍后补做。别忘了：验收 = 能复述 + 能改 + 能讲。")
                return
            history.append(("", ans))
            verdict = self._verdict(
                TASK_JUDGE_SYS,
                _task_user(LearnStep(title="剧末验收", raw=self.tail or "", task="完成剧末「改出来」综合改造"), "完成剧末「改出来」综合改造任务并汇报", history, ans),
                "done",
            )
            if verdict["done"]:
                self.say(f"🎓 恭喜毕业！{verdict['comment'] or ''}")
                self.say("现在你可以：① 给别人讲 3 分钟这个项目；② 照「下一步」去提第一个 PR。")
                return
            note = verdict.get("comment") or ""
            if note:
                self.say(f"  · {note}")
            follow = verdict.get("follow_up") or ""
            if follow:
                self.say(f"🧑‍🏫 追问：{follow}")
        self.say("（毕业关卡暂未达标也没关系，完成后随时回来汇报。）")


def run_tutor_loop(llm, plan_text: str, ask=input, say=print) -> str:
    """启动一次带读会话，返回 completed / aborted / failed。"""
    session = TutorSession(llm, plan_text, ask=ask, say=say)
    return session.run()
