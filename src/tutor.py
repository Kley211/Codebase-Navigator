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

SKIP_WORDS = ("跳过", "跳過", "skip", "先跳过", "不做了", "不想做", "暂不")

_QUESTION_STARTS = (
    "问", "提问", "为什么", "怎么", "如何", "是什么", "什么是", "啥是",
    "解释", "讲讲", "讲一下", "说明", "能不能", "能否", "可以", "请",
    "帮我", "哪个", "哪些", "哪里", "区别", "干嘛", "入口", "流程",
)
_QUESTION_LEADS = ("这个", "那个", "这段", "这里", "该", "项目", "代码", "函数", "模块", "文件", "类")
_QUESTION_WORDS = (
    "为什么", "怎么", "如何", "是什么", "什么是", "啥", "能不能", "能否",
    "可以", "区别", "干嘛", "入口", "流程", "作用", "哪里", "哪些",
)


def _is_question(text: str) -> bool:
    """判断用户输入是「自由提问」而不是命令或回答。"""
    t = (text or "").strip()
    if not t:
        return False
    if t.startswith(_QUESTION_STARTS):
        return True
    if t.endswith(("？", "?")):
        return True
    if t.startswith(_QUESTION_LEADS) and any(w in t[:24] for w in _QUESTION_WORDS):
        return True
    return False


def _draw_topic(text: str) -> str | None:
    """解析「画图/图/diagram」命令；不是画图命令则返回 None。"""
    t = (text or "").strip()
    patterns = (
        r"^画图[:：]?\s*(.*)$",
        r"^diagram[:：]?\s*(.*)$",
        r"^图[:：]\s*(.+)$",
    )
    for pat in patterns:
        m = re.match(pat, t, re.I)
        if m:
            return m.group(1).strip()
    return None


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
_MMD_RE = re.compile(r"```mermaid\s*\n(.*?)```", re.S)

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

GRAD_TALK_SYS = """你是「Codebase Navigator」的毕业把关导师。学习者给你一份 3 分钟口头讲解稿，用来验证「能讲」。

判断标准（四项都覆盖才算过）：
1. 这个项目解决什么问题、给谁用；
2. 整体架构 / 入口与核心链路怎么走；
3. 核心模块各负责什么、怎么协作；
4. 至少一个「为什么这样设计」的自己的判断。

规则：pass=true 时 comment 具体肯定他讲得好的地方；pass=false 时 comment 指出缺哪一项，follow_up 出一个问题引导他补上。只输出 JSON：{"pass": bool, "comment": "…", "follow_up": "…"}。"""

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
    diagram: str = ""   # 该步可选的 Mermaid 局部图（配合「读这里」建立画面）


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
        mm = _MMD_RE.search(raw)
        diagram = mm.group(1).strip() if mm else ""
        raw_clean = _MMD_RE.sub("", raw).strip() if mm else raw.strip()
        step = LearnStep(
            title=_first_heading(raw_clean or raw),
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
            raw=raw_clean,
            diagram=diagram,
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
        self.say("动手实验为可选（输入「跳过」可直接下一步）。随时输入「提示」要引导、")
        self.say("用「问：你的问题」或句尾带 ? 直接提问；输入「退出」结束。")
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
            cmd = self._read("读完请输 go 开始自检（或 提示 / 问：… / 退出）：")
            if cmd == "退出":
                self.status = "aborted"
                return
            if cmd == "提示":
                self._hint(step, "")
                continue
            if cmd == "go":
                break
            if _is_question(cmd):
                self._answer_free(step, cmd)
                continue
            self.say("（请输入 go / 提示 / 退出，或用「问：…」提问）")

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
            if _is_question(ans):
                self._answer_free(step, ans)
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
        self.say("\n🛠️ 动手任务（可选）——想亲手验证就做，做完回来汇报；不想做输入「跳过」直接下一步：")
        self._say_block(step.task)
        self.say("")
        history: list[tuple[str, str]] = []
        for attempt in range(1, MAX_ATTEMPTS + 1):
            if self.status == "aborted":
                return
            ans = self._read("完成后汇报：你改了什么、看到了什么结果、为什么（或 跳过 / 提示 / 退出）：")
            if ans == "退出":
                self.status = "aborted"
                return
            if ans == "提示":
                self._hint(step, step.task)
                continue
            if ans in SKIP_WORDS:
                self.say("⏭️ 已跳过动手实验。先用自己的话复述这一步，然后进入下一步。")
                return
            if _is_question(ans):
                self._answer_free(step, ans)
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

    def _answer_free(self, step: LearnStep, text: str) -> None:
        """自由提问：结合仓库直接答疑，不消耗自检次数。"""
        ctx = f"学习者正在学「{step.title}」。\n" if step else ""
        prompt = (
            f"{ctx}这是「带读陪练」里的自由提问。请结合仓库代码直接解答："
            "关键结论带 `路径:行号` 引用；不要整段贴代码；解答完用一句「回到当前学习」的话收尾。\n\n"
            f"学习者问：{text}"
        )
        try:
            answer = self.llm.chat(prompt)
        except Exception as e:
            answer = f"（自由提问暂时不可用：{e}）"
        self.say(f"\n💬 你问：{text}")
        self.say(answer)

    # ---------- 毕业关卡 ----------
    def _run_graduation(self) -> None:
        self.say("\n" + "🏁" * 10)
        self.say("全部步骤已走完，进入毕业关卡（可选）")
        self.say("🏁" * 10)
        if self.tail:
            self.say(self.tail)
        self.say("")
        self.say("二选一即可：① 完成「改出来」后回来汇报；② 把 3 分钟讲解稿贴在「讲：」后面。")
        self.say("两者都不想做，输入「跳过」结束（验收 = 能复述 + 能讲，能改是加分项）。")
        history: list[tuple[str, str]] = []
        for attempt in range(1, MAX_ATTEMPTS + 1):
            if self.status == "aborted":
                return
            ans = self._read("毕业汇报（改完汇报 / 讲：讲解稿 / 跳过 / 退出）：")
            if ans == "退出":
                self.status = "aborted"
                return
            if ans in SKIP_WORDS:
                self.say("⏭️ 已跳过毕业关卡。仍建议用 3 分钟给别人讲一遍这个项目。")
                return
            if re.match(r"^讲[:：]\s*", ans):
                if self._grad_talk_check(re.sub(r"^讲[:：]\s*", "", ans)):
                    return
                continue
            if _is_question(ans):
                self._answer_free(LearnStep(title="剧末验收", raw=self.tail or ""), ans)
                continue
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

    def _grad_talk_check(self, talk: str) -> bool:
        """毕业「能讲」把关：讲解稿达标返回 True。"""
        if not talk:
            self.say("（请把讲解稿贴在「讲：」后面，例如：讲：这个项目是……）")
            return False
        self.say("\n🧑‍🏫 收到 3 分钟讲解稿，我来把关：")
        for attempt in range(1, MAX_ATTEMPTS + 1):
            if self.status == "aborted":
                return False
            prompt = (
                f"# 仓库带读剧末验收背景\n{(self.tail or '')[:600]}\n\n"
                f"# 学习者的 3 分钟讲解稿\n{talk}\n\n请按规则输出 JSON 判定。"
            )
            verdict = self._verdict(GRAD_TALK_SYS, prompt, "pass")
            if verdict["pass"]:
                self.say(f"🎓 恭喜毕业——能讲也过关了！{verdict['comment'] or ''}")
                self.say("现在你可以：① 给别人讲 3 分钟这个项目；② 照「下一步」去提第一个 PR。")
                return True
            note = verdict.get("comment") or ""
            if note:
                self.say(f"  · {note}")
            if attempt == MAX_ATTEMPTS:
                self.say("（讲解稿暂未达标也没关系，可以先听别人怎么讲，再回来补。）")
                return False
            follow = verdict.get("follow_up") or ""
            if follow:
                self.say(f"🧑‍🏫 追问：{follow}")
            talk = self._read("补充后的讲解稿（直接粘贴，或 退出）：")
            if talk == "退出":
                self.status = "aborted"
                return False
        return False


def run_tutor_loop(llm, plan_text: str, ask=input, say=print) -> str:
    """启动一次带读会话，返回 completed / aborted / failed。"""
    session = TutorSession(llm, plan_text, ask=ask, say=say)
    return session.run()

class WebTutor:
    """Web 用逐轮带读状态机：不阻塞等待输入，一次 respond() 处理一条用户消息。

    与 CLI 的 TutorSession 共用 parse_plan / 判定提示词 / JSON 解析，
    但把「读代码 → 自检 → 动手 → 毕业」拆成显式状态机，便于 Gradio 逐轮驱动。
    """

    PHASE_NAMES = {
        "read": "阅读",
        "quiz": "苏格拉底自检",
        "task": "动手(可选)",
        "grad": "毕业关卡",
        "done": "已结束",
        "idle": "未开始",
    }

    def __init__(self, llm, plan_text: str, repo_key: str = "", memory_file=None):
        steps, tail = parse_plan(plan_text)
        self.llm = llm
        self.steps = steps
        self.tail = tail
        self.idx = -1              # 当前步骤下标
        self.phase = "idle"        # read/quiz/task/grad/done/idle
        self.j = 0                 # 自检要点下标
        self.attempts = 0          # 当前阶段已尝试次数
        self.history: list[tuple[str, str]] = []
        self.follow = ""           # 当前待回答的追问
        self.pending_diagram = ""  # 「画图：主题」按需生成的局部图（优先于步骤自带配图）
        self.step_weak = False     # 当前步骤是否有自检要点被追问多次才覆盖
        self.repo_key = repo_key
        self.memory_file = memory_file
        self._memory_data: dict | None = None
        self.memory: dict | None = None   # 与当前剧本标题一致时才有值（断点/薄弱点记忆）
        if repo_key:
            from . import tutor_memory as tm

            if self.memory_file is None:
                self.memory_file = tm.default_path()
            self._memory_data = tm.load(self.memory_file) or {}
            self.memory = tm.entry_for(
                self._memory_data, repo_key, [s.title for s in self.steps]
            )
        self._out: list[str] = []

    # ---------- 输出缓冲 ----------
    def _emit(self, text: str) -> None:
        if text:
            self._out.append(text)

    def _flush(self) -> list[str]:
        out, self._out = self._out, []
        return out

    def _record_step(self, passed: bool, weak: bool) -> None:
        """一步完成时写入本地记忆（无 repo_key 时静默跳过，带读仍可离线工作）。"""
        if not self.repo_key or self.memory_file is None:
            return
        from . import tutor_memory as tm

        if self._memory_data is None:
            self._memory_data = tm.load(self.memory_file) or {}
        entry = tm.ensure_entry(
            self._memory_data, self.repo_key, [s.title for s in self.steps]
        )
        tm.mark(entry, self.idx, passed=passed, weak=weak)
        self.memory = entry
        tm.save(self._memory_data, self.memory_file)

    # ---------- LLM 判定 ----------
    def _verdict(self, system: str, user: str, key: str) -> dict:
        text = self.llm.direct(system, user, temperature=0.1, max_tokens=700)
        obj = _extract_json(text) or {}
        ok = bool(obj.get(key)) if key in obj else False
        return {
            key: ok,
            "comment": str(obj.get("comment", "")).strip(),
            "follow_up": str(obj.get("follow_up", "")).strip(),
        }

    def _hint(self, context: str) -> None:
        try:
            step = self.steps[self.idx] if 0 <= self.idx < len(self.steps) else None
            user = f"学习者卡在：{context or (step.title if step else '当前步骤')}\n请给一句不剧透的引导提示。"
            hint = self.llm.direct(HINT_SYS, user, temperature=0.4, max_tokens=300)
        except Exception as e:
            hint = f"回到「读这里」的段落，想这段代码的职责和为什么这么写。（{e}）"
        self._emit(f"💡 提示：{hint.strip()}")

    # ---------- RoadMap 总览 ----------
    def roadmap_md(self) -> str:
        """整条学习路线总览：先看路线，再按步骤逐步带读。"""
        if not self.steps:
            return "暂无路线。加载仓库后点「开始带读」生成。"
        lines = [f"🗺️ **RoadMap · 共 {len(self.steps)} 步**", ""]
        for i, s in enumerate(self.steps, 1):
            goal = (s.objective or "").replace("\n", " ").strip()
            badge = " 🛠️" if s.task else ""
            line = f"{i}. **{s.title}**{badge}" + (f"　— {goal[:70]}" if goal else "")
            if self.memory is not None:
                st = self.memory.get("steps", [])[i - 1]
                if st.get("passed"):
                    line = "✅ " + line
                if st.get("weak"):
                    line += "　⚠ 薄弱（建议先复述）"
            lines.append(line)
        lines += [
            "",
            "> 路线即编号顺序：每步 = 精读 → 苏格拉底自检 → 动手实验（可选）。",
            "> 学习途中随时可输入 `问：你的问题`（或句尾带 `？`）直接提问，不打断进度。",
        ]
        return "\n".join(lines)

    def current_diagram(self) -> str:
        """当前要展示的配图（按需图优先，其次步骤自带图），无图返回空串。"""
        if self.pending_diagram:
            return self.pending_diagram
        if 0 <= self.idx < len(self.steps):
            return self.steps[self.idx].diagram
        return ""

    # ---------- 会话入口 ----------
    def start(self) -> list[str]:
        if not self.steps:
            self._emit("⚠️ 剧本解析失败：没有找到可用步骤，请点「重新生成剧本」。")
            return self._flush()
        self.idx = 0
        self._emit("🎓 **带读陪练开始**：共 {n} 步。先看上方 RoadMap 了解全程，"
                   "再按步骤 精读 → 苏格拉底自检 →（可选）动手。\n\n"
                   "命令：`go`(读完/继续) · `提示`(要引导) · `问：…`(自由提问) · `跳过`(跳过动手) · `退出`"
                   .format(n=len(self.steps)))
        resume = 0
        if self.memory is not None:
            from . import tutor_memory as tm

            completed = tm.completed_count(self.memory)
            weak = tm.weak_indexes(self.memory)
            resume = tm.next_index(self.memory)
            if resume >= len(self.steps):
                resume = 0
                self._emit("🎓 **上次全部步骤已完成 ✅**。本次从第 1 步开始巩固"
                           + (f"，RoadMap 已标出 ⚠ 薄弱点（{len(weak)} 处）。" if weak else "。"))
            elif resume > 0:
                self._emit(
                    f"📌 **上次进度已记忆**：已完成 {completed}/{len(self.steps)} 步，"
                    f"本次从第 {resume + 1} 步继续。"
                    + (f"⚠ 薄弱 {len(weak)} 处，建议先复述再往下。" if weak else "")
                )
        if resume:
            self.idx = resume
        self._enter_step()
        return self._flush()

    # ---------- 步骤状态机 ----------
    def _enter_step(self) -> None:
        self.pending_diagram = ""
        self.step_weak = False
        step = self.steps[self.idx]
        parts = [f"### 第 {self.idx + 1}/{len(self.steps)} 步｜{step.title}"]
        if step.objective:
            parts.append(f"🎯 **目标**：{step.objective}")
        if step.read_lines:
            parts.append("📖 **读这里**（按行号打开精读）：")
            parts += [f"- {line}" for line in step.read_lines]
        if step.hints:
            parts.append("💡 **边读边想**：")
            parts += [f"- {h}" for h in step.hints[:4]]
        self._emit("\n\n".join(parts))
        self.phase = "read"
        self.j = 0
        self.attempts = 0
        self.history = []
        self._emit("读完输入 `go` 开始自检（不懂可 `问：…` 直接问，卡住可 `提示`）。")

    def _enter_quiz(self) -> None:
        step = self.steps[self.idx]
        rubric = step.rubric or [step.objective]
        if self.j < len(rubric):
            self._emit(f"🎯 **自检 {self.j + 1}/{len(rubric)}**｜要求覆盖：{rubric[self.j]}")
            self._ask_quiz_question()
            self.phase = "quiz"
            self.attempts = 0
            self.history = []
            return
        self._enter_task()

    def _current_quiz_question(self) -> str:
        """当前自检点待回答的问句（优先返回未答完的追问）。"""
        if self.follow:
            return self.follow
        step = self.steps[self.idx]
        questions = step.questions or ["用你自己的话讲讲刚读的这段在做什么？"]
        return questions[self.j % len(questions)]

    def _ask_quiz_question(self) -> None:
        self._emit(f"🧑‍🏫 {self._current_quiz_question()}")

    def _enter_task(self) -> None:
        step = self.steps[self.idx]
        self._emit("🛠️ **动手任务（可选）**——目的是加深理解；不想动手可输入 `跳过` 直接下一步：")
        self._emit(step.task or "（本步没有明确任务，用一句话说说你能怎么验证理解。）")
        self.phase = "task"
        self.attempts = 0
        self.history = []

    def _finish_step(self) -> None:
        self._record_step(passed=True, weak=self.step_weak)
        step = self.steps[self.idx]
        if step.unlock:
            self._emit(f"🔓 **解锁条件**：{step.unlock}")
        self._emit(f"✅ 第 {self.idx + 1} 步完成。先用自己的话把它讲一遍，再继续。")
        if self.step_weak:
            self._emit("⚠ 该步有自检要点没一次掌握，已记为薄弱点：下次回来 RoadMap 会标出，建议先复述再继续。")
        if self.idx + 1 < len(self.steps):
            self.idx += 1
            self._enter_step()
        else:
            self._enter_graduation()

    def _enter_graduation(self) -> None:
        self.pending_diagram = ""
        self._emit("🏁 **进入毕业关卡（可选）**——目标：能给别人讲清楚这个项目。")
        if self.tail:
            self._emit(self.tail)
        self.phase = "grad"
        self.attempts = 0
        self.history = []
        self._emit("二选一：① 完成「改出来」后回来汇报（改了什么/结果/为什么）；"
                   "② 把 3 分钟讲解稿贴在 `讲：` 后面。都不想做就输入 `跳过` 结束。")

    def _draw_diagram(self, topic: str) -> None:
        """「画图：主题」：按需生成当前步骤的局部图（flowchart/时序），不推进、不消耗自检。"""
        from .diagram import extract_mermaid_block, looks_valid_mermaid

        step = self.steps[self.idx]
        lines = [f"# 学习步骤上下文（第 {self.idx + 1}/{len(self.steps)} 步｜{step.title}）"]
        if step.objective:
            lines.append(f"目标：{step.objective}")
        if step.read_lines:
            lines.append("读这里：")
            lines += [f"- {r}" for r in step.read_lines]
        if step.hints:
            lines.append("边读边想：")
            lines += [f"- {h}" for h in step.hints[:4]]
        base_context = "\n".join(lines)
        user = (
            f"{base_context}\n\n想画的主题：{topic}\n"
            "请画一张局部小图帮新手理解这一步（flowchart TD 或 sequenceDiagram，节点/消息不超过 8 个）。"
            "内容只能来自上面「读这里/边读边想」里真实出现的文件、组件或函数；"
            "flowchart 标签用双引号包裹，禁止出现 [ ] { } ( ) # \" 等字符；"
            "sequenceDiagram 参与者命名要短。只输出一个合法的 ```mermaid 代码块。"
        )
        system = "你是带读导师的图解助手。只输出一个合法的 ```mermaid 代码块，禁止任何解释或额外文字。"
        why = ""
        for _ in range(2):
            content = self.llm.direct(system, user, temperature=0.2, max_tokens=2048)
            code = extract_mermaid_block(content) or content.strip()
            ok, why = looks_valid_mermaid(code, allow_sequence=True)
            if ok:
                self.pending_diagram = code
                self._emit(
                    f"📊 **已按需配图**：主题「{topic}」的小图见上方配图区。"
                    "读图时对照「读这里」的行号理解；可继续 `go` 或 `问：…` 追问。"
                )
                return
            user = (
                f"你上次输出的 Mermaid 不合格：{why}。请只输出一个合法 ```mermaid 代码块，不要解释。"
                f"\n\n{base_context}\n\n想画的主题：{topic}"
            )
        self._emit(
            f"❌ 按需配图失败：模型两次输出都不符合 Mermaid 结构（{why}）。"
            "可稍后再试，或继续用文字提问。"
        )

    # ---------- 逐轮响应 ----------
    def respond(self, text: str) -> list[str]:
        text = (text or "").strip()
        if not text:
            self._emit("（输入内容为空，请直接回答或输入命令。）")
            return self._flush()
        if text in ("退出", "exit", "quit"):
            self.phase = "done"
            self._emit("👋 会话已退出。随时可以点「开始带读」继续（会从第 1 步重新走）。")
            return self._flush()
        if self.phase == "done":
            self._emit("👋 会话已结束，点「开始带读」再来一轮。")
            return self._flush()
        if text in ("提示", "hint"):
            self._hint(self._context_hint())
            return self._flush()
        if self.phase == "task" and text in SKIP_WORDS:
            self._emit("⏭️ 已跳过动手实验。先用自己的话把这一步复述一遍，再继续下一步。")
            self._finish_step()
            return self._flush()
        if self.phase == "grad" and text in SKIP_WORDS:
            self.phase = "done"
            self._emit("⏭️ 已跳过毕业综合改造。毕业非强制；仍建议用 3 分钟给别人讲一遍项目，"
                       "或用 `问：…` 继续追问。")
            return self._flush()
        if self.phase == "grad" and re.match(r"^讲[:：]\s*", text):
            self._answer_grad_talk(re.sub(r"^讲[:：]\s*", "", text))
            return self._flush()
        topic = _draw_topic(text)
        if topic is not None:
            if self.phase not in ("read", "quiz", "task", "grad") or not (0 <= self.idx < len(self.steps)):
                self._emit("请先点「开始带读」进入某一步后，再用 `画图：主题` 要一张局部小图。")
                return self._flush()
            self._draw_diagram(topic or self.steps[self.idx].title)
            return self._flush()
        if _is_question(text):
            self._answer_free_question(text)
            return self._flush()
        if self.phase == "read":
            if text in ("go", "开始", "开始自检"):
                self._enter_quiz()
            else:
                self._emit("（阅读阶段：输入 `go` 表示读完了要开始自检，或输入 `提示` 要引导。）")
            return self._flush()
        if self.phase == "quiz":
            self._answer_quiz(text)
            return self._flush()
        if self.phase == "task":
            self._answer_task(text)
            return self._flush()
        if self.phase == "grad":
            self._answer_grad(text)
            return self._flush()
        self._emit("（请先点「开始带读」启动会话。）")
        return self._flush()

    def _context_hint(self) -> str:
        if self.phase == "quiz":
            return self.follow or "当前自检问题"
        if self.phase == "task":
            step = self.steps[self.idx]
            return step.task
        return "当前步骤"

    # ---------- 各阶段回答处理 ----------
    def _answer_quiz(self, answer: str) -> None:
        step = self.steps[self.idx]
        rubric = step.rubric or [step.objective]
        point = rubric[min(self.j, len(rubric) - 1)]
        question = self._current_quiz_question()
        self.attempts += 1
        self.history.append((question, answer))
        verdict = self._verdict(JUDGE_SYS, _judge_user(step, question, point, self.history, answer), "mastered")
        if verdict["mastered"]:
            self._emit(f"✅ 要点 {self.j + 1} 覆盖：{verdict['comment'] or '回答到位。'}")
            self.j += 1
            self.follow = ""
            if self.j < len(rubric):
                self._enter_quiz()
            else:
                self._enter_task()
            return
        if verdict.get("comment"):
            self._emit(f"· {verdict['comment']}")
        if self.attempts >= MAX_ATTEMPTS:
            self._emit(f"（多次追问仍未覆盖，先记下参考答案：{point}。建议回看再回来。）")
            self.step_weak = True
            self.j += 1
            self.follow = ""
            if self.j < len(rubric):
                self._enter_quiz()
            else:
                self._enter_task()
            return
        self.follow = verdict.get("follow_up") or question
        self._emit(f"🧑‍🏫 追问：{self.follow}")

    def _answer_task(self, report: str) -> None:
        step = self.steps[self.idx]
        self.attempts += 1
        self.history.append(("", report))
        verdict = self._verdict(TASK_JUDGE_SYS, _task_user(step, step.task, self.history, report), "done")
        if verdict["done"]:
            self._emit(f"✅ 动手实验通过：{verdict['comment'] or '干得漂亮。'}")
            self._finish_step()
            return
        if verdict.get("comment"):
            self._emit(f"· {verdict['comment']}")
        if self.attempts >= MAX_ATTEMPTS:
            self._emit("（动手实验暂未达标。建议回到任务描述重做，完成后可继续下一步。）")
            self._finish_step()
            return
        self.follow = verdict.get("follow_up") or ""
        if self.follow:
            self._emit(f"🧑‍🏫 追问：{self.follow}")
        else:
            self._emit("请再试一次：说清你改了什么、看到了什么结果、为什么。")

    def _answer_grad(self, report: str) -> None:
        self.attempts += 1
        self.history.append(("", report))
        task = "完成剧末「改出来」综合改造任务并汇报：改了什么 / 看到什么结果 / 为什么"
        stub = LearnStep(title="剧末验收", task=task, raw=self.tail or task)
        verdict = self._verdict(TASK_JUDGE_SYS, _task_user(stub, task, self.history, report), "done")
        if verdict["done"]:
            self.phase = "done"
            self._emit(f"🎓 **恭喜毕业！** {verdict['comment'] or ''}")
            self._emit("现在你可以：① 给别人讲 3 分钟这个项目；② 照剧末「下一步」去提第一个 PR。")
            return
        if verdict.get("comment"):
            self._emit(f"· {verdict['comment']}")
        if self.attempts >= MAX_ATTEMPTS:
            self.phase = "done"
            self._emit("（毕业关卡暂未达标也没关系，完成后随时回来汇报。）")
            return
        self.follow = verdict.get("follow_up") or ""
        if self.follow:
            self._emit(f"🧑‍🏫 追问：{self.follow}")
        else:
            self._emit("请再试一次：说清你改了什么、看到了什么结果、为什么。")

    def _answer_free_question(self, text: str) -> None:
        """会话中自由提问：结合仓库答疑，不消耗自检次数、不推进进度。"""
        step = self.steps[self.idx] if 0 <= self.idx < len(self.steps) else None
        ctx = ""
        if step:
            ctx = f"学习者正在学第 {self.idx + 1}/{len(self.steps)} 步「{step.title}」。\n"
        prompt = (
            f"{ctx}这是「带读陪练」里的自由提问。请结合仓库代码直接解答："
            "关键结论带 `路径:行号` 引用；不要整段贴代码；解答完用一句「回到当前学习」的话收尾。\n\n"
            f"学习者问：{text}"
        )
        try:
            answer = self.llm.chat(prompt)
        except Exception as e:
            answer = f"（自由提问暂时不可用：{e}）"
        self._emit(f"💬 **你问**：{text}")
        self._emit(answer)
        self._resume_prompt()

    def _resume_prompt(self) -> None:
        """答疑后把学习者带回当前待办，避免进度被打断。"""
        if self.phase == "quiz":
            self._emit("（回到刚才的自检——用自己的话回答，先别照抄上面的讲解。）")
            self._ask_quiz_question()
        elif self.phase == "task":
            self._emit("（回到动手实验——完成后来汇报；想先推进可输入 `跳过`。）")
        elif self.phase == "grad":
            self._emit("（回到毕业关卡——改完回来汇报，或 `讲：` 贴 3 分钟讲解稿，或 `跳过` 结束。）")
        elif self.phase == "read":
            self._emit("（回到阅读——读完这一段后输入 `go` 开始自检。）")

    def _answer_grad_talk(self, talk: str) -> None:
        """毕业「能讲」路径：对 3 分钟讲解稿做口头把关。"""
        if not talk:
            self._emit("（请把讲解稿贴在 `讲：` 后面，例如：`讲：这个项目是……`）")
            return
        self.attempts += 1
        prompt = (
            f"# 仓库带读剧末验收背景\n{(self.tail or '')[:600]}\n\n"
            f"# 学习者的 3 分钟讲解稿\n{talk}\n\n请按规则输出 JSON 判定。"
        )
        verdict = self._verdict(GRAD_TALK_SYS, prompt, "pass")
        if verdict["pass"]:
            self.phase = "done"
            self._emit(f"🎓 **恭喜毕业——能讲也过关了！** {verdict['comment'] or ''}")
            self._emit("现在你可以：① 给别人讲 3 分钟这个项目；② 照剧末「下一步」去提第一个 PR。")
            return
        if verdict.get("comment"):
            self._emit(f"· {verdict['comment']}")
        if self.attempts >= MAX_ATTEMPTS:
            self.phase = "done"
            self._emit("（讲解稿暂未达标也没关系，可以先听别人怎么讲这个项目，再回来补。）")
            return
        self.follow = verdict.get("follow_up") or ""
        if self.follow:
            self._emit(f"🧑‍🏫 追问：{self.follow}（补全后再以 `讲：` 开头发一次）")
        else:
            self._emit("请补上缺的要点后再次发送（仍以 `讲：` 开头）。")

    # ---------- 状态摘要 ----------
    def summary(self) -> str:
        if self.phase in ("idle",):
            return "尚未开始。加载仓库后点「开始带读」。"
        if self.phase == "done":
            return "会话已结束。点「开始带读」再来一轮。"
        step_text = f"第 {self.idx + 1}/{len(self.steps)} 步"
        return f"`{step_text} · {self.PHASE_NAMES.get(self.phase, self.phase)}`" + (
            f" · 自检 {self.j + 1}/{max(len(self.steps[self.idx].rubric or [self.steps[self.idx].objective]), 1)}"
            if self.phase == "quiz" else ""
        )
