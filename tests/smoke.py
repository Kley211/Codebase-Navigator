"""冒烟测试：创建一个小仓库，验证工具与静态报告可用。

运行：python tests/smoke.py
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.report import generate_report
from src.tools import call_tool
from src.context import build_overview_context, _is_large, _module_layout
from src.progress import MILESTONES, ProgressStore
from src.learn import validate_learn_plan
from src.tutor import parse_plan, WebTutor, _is_question
from src import tutor_memory
from src.diagram import (
    architecture_facts,
    extract_mermaid_block,
    looks_valid_mermaid,
    mermaid_html,
    module_map_mermaid,
)


def make_fake_repo() -> Path:
    tmp = Path(tempfile.mkdtemp(prefix="nav-test-"))
    (tmp / "README.md").write_text(
        "# Demo\n\n一个用于测试的演示项目。\n\n## 安装\n\npip install -r requirements.txt\n",
        encoding="utf-8",
    )
    (tmp / "requirements.txt").write_text("requests>=2.0\nflask==3.0\n", encoding="utf-8")
    (tmp / "app.py").write_text(
        "import os\nfrom flask import Flask\n\napp = Flask(__name__)\n\n"
        "def hello():\n    return 'hi'\n\n"
        "if __name__ == '__main__':\n    app.run()\n",
        encoding="utf-8",
    )
    (tmp / "src").mkdir()
    (tmp / "src" / "core.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    (tmp / "tests").mkdir()
    (tmp / "tests" / "test_app.py").write_text("def test_hello():\n    assert True\n", encoding="utf-8")
    return tmp


def make_large_fake_repo() -> Path:
    """构造超过大仓库阈值（300 源码文件）的分层仓库。"""
    tmp = Path(tempfile.mkdtemp(prefix="nav-large-"))
    (tmp / "README.md").write_text("# Large Demo\n", encoding="utf-8")
    for module in ("core", "api", "worker", "cli"):
        for i in range(80):
            (tmp / module).mkdir(parents=True, exist_ok=True)
            (tmp / module / f"mod_{module}_{i:03d}.py").write_text(
                f"def fn{i}():\n    return {i}\n", encoding="utf-8"
            )
    return tmp


def make_good_learn_plan() -> str:
    """构造一份结构合格的最小带读剧本（供离线校验使用）。"""
    step_tpl = """### 第 {n} 步：占位步骤标题
**目标**：这一步完成后你能用自己的话复述并讲清楚。
**读这里**：
- `app.py:{a}-{b}` —— 为什么读这段（一句话）
- `src/core.py:{c}-{d}` —— 为什么读这段（一句话）
**讲解要点**：为什么这里要这样写？如果不这样做会怎样？
**动手任务**：改 `app.py` 第 {a} 行附近的一个小改动，运行并观察效果。
**苏格拉底自检**：
- 问题 1：你能用自己的话解释刚才读的这段在做什么吗？为什么？
- 问题 2：如果去掉这个判断/分支，会发生什么？
**合格回答应包含**：1. 说清这段的职责；2. 说清它和调用方的关系。
**解锁条件**：复述覆盖要点 + 动手实验跑通。

"""
    parts = ["# 示例仓库带读剧本", "## 剧本总览\n值得学什么；学完你能做到什么。\n"]
    for i in range(5):
        parts.append(step_tpl.format(n=i + 1, a=1 + i * 3, b=10 + i * 3, c=20 + i, d=30 + i))
    parts.append("## 剧末验收（毕业关卡）\n- **讲出来**：给别人讲 3 分钟提纲\n- **改出来**：综合改动任务\n")
    plan = "\n".join(parts)
    # 第 1 步插入可选 Mermaid 局部图，测试配图解析与逐步渲染
    first_marker = "### 第 1 步"
    return plan.replace(
        first_marker,
        first_marker + "\n```mermaid\nflowchart TD\n  A[\"app.py\"] --> B[\"src/core.py\"]\n```",
        1,
    )


def main() -> int:
    repo = make_fake_repo()
    large_repo = make_large_fake_repo()
    checks = [
        ("目录结构", call_tool("list_directory_structure", {"repo_path": str(repo)})),
        ("入口点", call_tool("find_entry_points", {"repo_path": str(repo)})),
        ("依赖", call_tool("analyze_dependencies", {"repo_path": str(repo)})),
        ("读文件", call_tool("read_file", {"file_path": str(repo / "app.py")})),
        ("搜索", call_tool("search_code", {"repo_path": str(repo), "pattern": "Flask"})),
        ("找文件", call_tool("find_files_by_pattern", {"repo_path": str(repo), "pattern": "**/*.py"})),
        ("imports", call_tool("get_imports", {"file_path": str(repo / "app.py")})),
        ("签名", call_tool("get_function_signatures", {"file_path": str(repo / "app.py")})),
    ]

    failed = 0
    for name, out in checks:
        ok = out and not out.startswith("错误")
        print(f"[{'✅' if ok else '❌'}] {name}")
        if not ok:
            failed += 1
            print(out)

    report = generate_report(str(repo))
    for expected in ("项目学习报告", "app.py", "flask", "学习路线"):
        if expected not in report:
            print(f"[❌] 报告缺少关键内容：{expected}")
            failed += 1

    context = build_overview_context(str(repo))
    for expected in ("目录结构", "依赖", "入口点", "### 文件：app.py", "src/core.py"):
        if expected not in context:
            print(f"[❌] AI 上下文缺少关键内容：{expected}")
            failed += 1

    if _is_large(repo):
        print("[❌] 小仓库不应被判定为大仓库")
        failed += 1
    if not _is_large(large_repo):
        print("[❌] 大仓库应被判定为大仓库")
        failed += 1

    modules = [name for name, _ in _module_layout(large_repo)]
    if "core" not in modules or "api" not in modules:
        print(f"[❌] 大仓库模块识别缺失：{modules}")
        failed += 1
    large_context = build_overview_context(str(large_repo))
    for expected in ("## 模块：core/", "## 模块：api/", "### 文件：core/"):
        if expected not in large_context:
            print(f"[❌] 大仓库分层上下文缺少：{expected}")
            failed += 1

    # 学习进度存储：初始化 → 勾选 → 持久化 → 重置
    progress_path = Path(tempfile.mkdtemp(prefix="nav-progress-")) / "progress.json"
    pstore = ProgressStore(progress_path)
    pstore.ensure("demo", str(repo))
    items = pstore.items("demo")
    for milestone in MILESTONES:
        if milestone not in items:
            print(f"[❌] 学习清单缺少里程碑：{milestone}")
            failed += 1
    if len(items) <= len(MILESTONES):
        print(f"[❌] 学习清单应包含关键文件项：{items}")
        failed += 1
    pstore.update("demo", items[:2])
    if ProgressStore(progress_path).done("demo") != items[:2]:
        print("[❌] 学习进度未持久化")
        failed += 1
    pstore.reset("demo")
    if ProgressStore(progress_path).done("demo"):
        print("[❌] 学习进度重置失败")
        failed += 1

    # 带读剧本结构校验（离线，不调 LLM）
    ok, problems = validate_learn_plan(make_good_learn_plan())
    if not ok:
        print(f"[❌] 带读剧本结构校验：{problems}")
        failed += 1
    ok_bad, _ = validate_learn_plan("# 空剧本\n没有任何步骤")
    if ok_bad:
        print("[❌] 带读剧本应拒绝没有步骤的内容")
        failed += 1

    # 带读剧本解析（离线）：步骤齐全、每个步骤都有自检问句与判定要点
    parsed_steps, parsed_tail = parse_plan(make_good_learn_plan())
    if len(parsed_steps) != 5 or not parsed_tail:
        print(f"[❌] 带读剧本解析步骤异常：{len(parsed_steps)} 步, tail={bool(parsed_tail)}")
        failed += 1
    if not parsed_steps[0].questions or not parsed_steps[0].rubric or not parsed_steps[0].task:
        print("[❌] 带读剧本解析缺少 自检问句/判定要点/动手任务")
        failed += 1
    if "flowchart TD" not in parsed_steps[0].diagram or "```mermaid" in parsed_steps[0].raw:
        print("[❌] 第 1 步配图应解析进 diagram 并从 raw 中剥离")
        failed += 1
    if parsed_steps[1].diagram:
        print("[❌] 未配图步骤的 diagram 应为空")
        failed += 1

    # WebTutor 离线状态机：RoadMap 总览 → 途中自由提问 → 自检通过 → 动手可跳过 → 下一步
    class _FakeLLM:
        """离线的 LLM 替身：判定一律通过，答疑直接返回（不真实调模型）。"""
        def __init__(self):
            self.judged = 0
            self.drawn = 0
            self.answers = []
        def direct(self, system, user, temperature=0.2, max_tokens=800):
            if "mermaid" in (system or "").lower() or "想画的主题" in (user or ""):
                self.drawn += 1
                return "```mermaid\nsequenceDiagram\n  用户->>app: 请求\n  app->>core: 处理\n```"
            self.judged += 1
            return '{"mastered": true, "comment": "回答到位", "follow_up": ""}'
        def chat(self, message):
            self.answers.append(message)
            return "答疑：它负责把请求路由到对应视图，见 app.py:8。"

    fake = _FakeLLM()
    wt = WebTutor(fake, make_good_learn_plan())
    roadmap = wt.roadmap_md()
    if "RoadMap" not in roadmap or f"共 {len(wt.steps)} 步" not in roadmap:
        print("[❌] RoadMap 总览缺步骤")
        failed += 1
    wt.start()
    if wt.phase != "read" or wt.idx != 0:
        print("[❌] WebTutor 启动后应停在第 1 步阅读")
        failed += 1
    if "flowchart TD" not in wt.current_diagram():
        print("[❌] 第 1 步配图未进入 WebTutor 当前步骤")
        failed += 1
    # 阅读阶段自由提问：答疑但不推进
    replies = wt.respond("这个项目是怎么跑起来的？")
    if not fake.answers or wt.phase != "read" or not any("答疑：" in m for m in replies):
        print("[❌] 阅读阶段自由提问未答疑")
        failed += 1
    # 画图命令：按需局部图（不推进、不消耗自检判定）
    replies = wt.respond("画图：请求是怎么路由到核心模块的？")
    if fake.drawn != 1 or wt.phase != "read" or "sequenceDiagram" not in wt.current_diagram():
        print("[❌] 画图命令未生成按需局部图")
        failed += 1
    if not any("已按需配图" in m for m in replies):
        print("[❌] 画图后应提示已按需配图")
        failed += 1
    # 进入自检后提问：答疑并回到原题，不消耗判定次数
    fake.answers.clear()
    judged_before = fake.judged
    wt.respond("go")
    if wt.phase != "quiz":
        print("[❌] go 后应进入自检")
        failed += 1
    replies = wt.respond("为什么要这样设计？")
    if wt.j != 0 or not fake.answers or fake.judged != judged_before:
        print("[❌] 自检中提问应答疑且不消耗判定")
        failed += 1
    if not any("回到刚才的自检" in m for m in replies):
        print("[❌] 答疑后应回到原自检题")
        failed += 1
    # 两个自检要点依次通过 → 进入可选动手
    wt.respond("我的理解：它负责解析请求，并把结果交给调用方。")
    wt.respond("如果去掉这步，调用方拿不到结果，所以不能省。")
    if wt.phase != "task":
        print(f"[❌] 自检通过后应进入动手（可选），实际 phase={wt.phase}")
        failed += 1
    # 跳过动手 → 解锁下一步
    replies = wt.respond("跳过")
    if wt.idx != 1 or wt.phase != "read" or not any("第 2/5 步" in m for m in replies):
        print("[❌] 跳过动手后应进入第 2 步")
        failed += 1
    if wt.current_diagram():
        print("[❌] 无配图步骤 current_diagram 应为空")
        failed += 1
    # 提问识别规则
    if not _is_question("为什么这里要这么写？") or not _is_question("问：入口文件是哪个") or _is_question("因为它更简单"):
        print("[❌] 自由提问识别规则异常")
        failed += 1

    # 带读记忆闭环：断点续读 + 薄弱点标注 + 重学补强清标（Learning Agent 最小闭环）
    class _ScriptedLLM:
        """离线 LLM 替身：前 fail_first 次自检判定失败（模拟多次追问），之后一律通过。"""
        def __init__(self, fail_first: int = 0):
            self.judged = 0
            self.fail_first = fail_first
        def direct(self, system, user, temperature=0.2, max_tokens=800):
            if "mermaid" in (system or "").lower():
                return "```mermaid\nflowchart TD\n  A[\"x\"] --> B[\"y\"]\n```"
            self.judged += 1
            if self.judged <= self.fail_first:
                return '{"mastered": false, "comment": "还缺一个点", "follow_up": "换个角度再答一次？"}'
            return '{"mastered": true, "comment": "回答到位", "follow_up": ""}'
        def chat(self, message):
            return "答疑：看 app.py:8。"

    mem_dir = Path(tempfile.mkdtemp(prefix="nav-mem-"))
    mem_file = mem_dir / "tutor_memory.json"
    plan_text = make_good_learn_plan()
    plan_titles = [s.title for s in parse_plan(plan_text)[0]]

    def _pass_one_step(wt, answer1: str, answer2: str) -> None:
        """读完 → go → 两个自检要点 → 跳过动手 → 完成第 1 步。"""
        wt.respond("go")
        wt.respond(answer1)
        wt.respond(answer2)
        wt.respond("跳过")

    # A. 干净完成第 1 步 → 记忆 passed=true；新会话断点续读停在未完成的第 2 步
    wt_clean = WebTutor(_ScriptedLLM(0), plan_text, repo_key="demo", memory_file=mem_file)
    wt_clean.start()
    _pass_one_step(
        wt_clean,
        "我的理解：这段把输入处理成结果交给调用方。",
        "少了一环调用方就拿不到结果，所以不能省。",
    )
    if wt_clean.idx != 1:
        print(f"[❌] 干净完成第 1 步后应进入第 2 步，实际 idx={wt_clean.idx}")
        failed += 1
    mem_json = json.loads(mem_file.read_text(encoding="utf-8"))
    first_step = mem_json["demo"]["steps"][0]
    if (
        mem_json["demo"]["titles"] != plan_titles
        or not first_step.get("passed")
        or first_step.get("weak")
    ):
        print("[❌] 干净完成第 1 步后记忆应记 passed=true、weak=false")
        failed += 1
    wt_resume = WebTutor(_ScriptedLLM(0), plan_text, repo_key="demo", memory_file=mem_file)
    msgs_resume = wt_resume.start()
    if wt_resume.idx != 1 or not any("上次进度已记忆" in m for m in msgs_resume):
        print(f"[❌] 断点续读应停在第 2 步并提示已记忆，实际 idx={wt_resume.idx}")
        failed += 1

    # B. 某要点被追问到上限仍未覆盖 → 步骤记为薄弱，RoadMap 标 ⚠，续读开场提示
    mem_file_weak = mem_dir / "tutor_memory_weak.json"
    wt_weak = WebTutor(_ScriptedLLM(fail_first=4), plan_text, repo_key="demo", memory_file=mem_file_weak)
    wt_weak.start()
    wt_weak.respond("go")
    for _ in range(4):
        wt_weak.respond("我不太确定，可能是这样吧……")
    wt_weak.respond("调用方拿到结果才能继续，缺了就断链。")
    wt_weak.respond("跳过")
    weak_json = json.loads(mem_file_weak.read_text(encoding="utf-8"))
    weak_first = weak_json["demo"]["steps"][0]
    if not (weak_first.get("passed") and weak_first.get("weak")):
        print("[❌] 多次追问仍未覆盖的步骤应记 passed + weak")
        failed += 1
    wt_weak_road = WebTutor(_ScriptedLLM(0), plan_text, repo_key="demo", memory_file=mem_file_weak)
    if "⚠ 薄弱" not in wt_weak_road.roadmap_md():
        print("[❌] RoadMap 应标出薄弱点 ⚠")
        failed += 1
    if not any("薄弱" in m for m in wt_weak_road.start()):
        print("[❌] 续读开场应提示薄弱点数量")
        failed += 1

    # C. tm API：重学干净通过后 mark(weak=False) 清除旧 ⚠；全部完成后 next_index=len
    data_c = {}
    entry_c = tutor_memory.ensure_entry(data_c, "demo", plan_titles)
    tutor_memory.mark(entry_c, 0, passed=True, weak=True)
    if not entry_c["steps"][0]["weak"]:
        print("[❌] mark(weak=True) 应写入薄弱标记")
        failed += 1
    tutor_memory.mark(entry_c, 0, passed=True, weak=False)
    if entry_c["steps"][0]["weak"]:
        print("[❌] 重学补强后 mark(weak=False) 应清除薄弱标记")
        failed += 1
    for i in range(len(plan_titles)):
        tutor_memory.mark(entry_c, i, passed=True, weak=False)
    if tutor_memory.next_index(entry_c) != len(plan_titles):
        print("[❌] 全部步骤通过后 next_index 应等于步骤总数")
        failed += 1

    # 图渲染抽象（离线，无 LLM）：静态模块地图 + Mermaid 提取/预检 + HTML 封装
    mm = module_map_mermaid(str(large_repo))
    if not mm.startswith("flowchart TD") or "core/" not in mm or "api/" not in mm:
        print(f"[❌] 静态模块地图应包含 core/api 模块：\n{mm}")
        failed += 1

    facts = architecture_facts(str(repo))
    for expected in ("# 仓库", "app.py", "入口点"):
        if expected not in facts:
            print(f"[❌] 架构素材缺少关键内容：{expected}")
            failed += 1

    sample = "前言\n```mermaid\nflowchart TD\n  A[\"入口\"] ==> B[\"核心\"]\n```\n"
    code = extract_mermaid_block(sample)
    ok, why = looks_valid_mermaid(code or "")
    if not ok:
        print(f"[❌] Mermaid 提取/预检失败：{why}")
        failed += 1
    ok_bad, _ = looks_valid_mermaid("A --> B")
    if ok_bad:
        print("[❌] Mermaid 预检应拒绝缺少 flowchart 声明的内容")
        failed += 1
    ok_seq, why_seq = looks_valid_mermaid("sequenceDiagram\n  A->>B: hi", allow_sequence=True)
    ok_seq_bad, _ = looks_valid_mermaid("sequenceDiagram\n  A->>B: hi", allow_sequence=False)
    if not ok_seq or ok_seq_bad:
        print("[❌] sequenceDiagram 预检应仅在 allow_sequence 时通过")
        failed += 1

    html_out = mermaid_html(code or "", caption="测试仓库 · 总体架构图")
    for expected in ("cn-diagram", "mermaid", "flowchart TD", "复制源码", "mermaid.live"):
        if expected not in html_out:
            print(f"[❌] mermaid_html 输出缺少关键内容：{expected}")
            failed += 1

    if failed == 0:
        print("[✅] 静态报告")
        print("[✅] 带读剧本结构校验")
        print("[✅] 带读剧本解析")
        print("[✅] RoadMap / 自由提问 / 动手可选")
        print("[✅] 带读记忆闭环（断点续读 / 薄弱点）")
        print("[✅] 架构图渲染抽象")
    else:
        print("[❌] 静态报告")
    print(f"\n结果：{len(checks) + 6 - failed}/{len(checks) + 6} 通过")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
