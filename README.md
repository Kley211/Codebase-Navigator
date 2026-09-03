# Codebase Navigator

> 30 分钟读懂任意代码库。输入一个 GitHub 仓库或本地项目，生成结构化学习报告：项目概览、模块地图、关键文件、阅读路线，并支持边读边问。

## 特性

- **静态分析引擎（无需 API Key）**：目录结构、入口点、依赖、imports、文件热度，秒级输出
- **AI 学习报告**：Overview / 模块地图 / Key Files / Roadmap，每条结论带 `file:line` 引用（反幻觉）
- **默认免费模型**：OpenRouter 的 `z-ai/glm-5.2:free`（0 费用），也可切回 DeepSeek / Groq / OpenAI / 任意 OpenAI 兼容服务
- **自动降级**：免费模型共享池限流（429/过载）时自动切换到备用免费模型（`minimax/minimax-m3:free` 等），成功后记住当前模型
- **CLI 优先**：一份 Markdown 报告即交付物，方便导出、分享、写进简历
- **带读剧本（Learn Plan）**：面向「会装环境但没读过开源源码」的新手，把仓库转成 5-8 步可执行计划——每步含目标 / 精读段落 / 动手任务 / 苏格拉底式自检，验收标准是「能复述 + 能讲」（能改是加分项）
- **带读陪练（Tutor）**：先出 RoadMap 看清全程，再逐问带读——AI 按「合格回答应包含」苏格拉底式追问并判定理解；途中可 `问：…` 直接提问答疑；动手实验可选（`跳过` 即可），毕业可贴 3 分钟讲解稿验证「能讲」
- **评测驱动**：内置 10 个 GitHub Trending 热门仓库评测集，质量可量化

## 快速开始

```bash
# 1. 创建虚拟环境并安装依赖
python -m venv .venv
.venv\Scripts\activate          # Windows
source .venv/bin/activate       # macOS / Linux
pip install -r requirements.txt

# 2. 配置 API Key（可选，静态报告不需要）
cp .env.example .env            # 填 OPENROUTER_API_KEY（默认免费模型 glm-5.2:free），DeepSeek 可选

# 3. 生成静态学习报告（免费，无需 Key）
python cli.py https://github.com/psf/requests --report

# 4. 生成 AI 概览（需要 Key）
python cli.py https://github.com/psf/requests --overview

# 5. 追问具体问题
python cli.py https://github.com/psf/requests --ask "认证是如何实现的？"

# 6. 生成「带读剧本」（需要 API Key，自动存为 learn-requests.md）
python cli.py https://github.com/psf/requests --learn

# 7. 进入「AI 带读陪练」（复用/自动生成剧本，逐问判定，需要 API Key）
python cli.py https://github.com/psf/requests --tutor

# 8. 保存报告
python cli.py https://github.com/psf/requests --report --output report.md
```

也支持本地路径：`python cli.py E:\some\repo --report`。

### 带读剧本（--learn）

面向**会自己建虚拟环境、装依赖、能跑通项目、会看报错，但没系统读过开源项目源码**的开发者。
每次生成一份 Markdown 剧本：5-8 个步骤，从「跑起来」开始，经核心链路精读到综合改造；
每一步都包含「读哪里（精确到 行号区间）/ 讲解要点 / 动手任务 / 苏格拉底式自检」，以及判定
「真的懂了」的回答要点（验收 = 能复述 + 能讲，能改是加分项）。

> 注意：URL 仓库会被克隆到临时目录用于分析，动手实验请在你自己的工作副本进行
> （`git clone <repo>` 一份）。

### 带读陪练（--tutor）

先生成/复用 RoadMap（5-8 步）看清全程，再进入会话：每一步先按「读这里（精确到行号区间）」
精读，读完输入 `go` 开始自检。AI 会对照「合格回答应包含」逐条苏格拉底式追问——覆盖要点就
通过，答得浅就换角度再问，直到确认你真的理解。看不懂的地方随时输入 `问：你的问题`（或句尾
带 `？`）直接提问，AI 结合仓库答疑后会回到当前自检题。动手实验只是可选加深（输入 `跳过`
直接下一步）；毕业关卡可选：完成「改出来」后汇报，或用 `讲：` 贴 3 分钟讲解稿验证「能讲」。
随时可 `提示` / `退出`。

也可复用已有剧本：`python cli.py <仓库> --tutor --plan learn-<仓库名>.md`

## Web 界面（Phase 3）

```bash
pip install -r requirements-web.txt
python app.py
# 打开 http://127.0.0.1:7860
```

单页包含：静态报告（免费）→ AI 概览（含工具调用轨迹）→ 边读边问 → 学习进度勾选清单 → **带读陪练**。
带读陪练（Tab 05）把剧本搬进浏览器：点「开始带读」自动复用/生成剧本，先在页面上方显示
RoadMap 总览，再进入会话逐步带读；学习途中随时输入 `问：…` 直接提问答疑，动手实验可
`跳过`，毕业可用 3 分钟讲解稿验证「能讲」。进度自动保存到本地
`~/.codebase-navigator/progress.json`。AI 功能默认读取 `.env` 的 API Key，也可在页面上临时填写。

## MCP Server（供 Codex / Claude / Cursor 调用）

把代码库分析能力暴露为 MCP 工具，AI 编程助手可以直接调用：

```bash
pip install -r requirements.txt   # 包含 mcp>=2.0.0
python mcp_server.py
```

在客户端（如 Codex / Claude Desktop）的 MCP 配置中加入：

```json
{
  "mcpServers": {
    "codebase-navigator": {
      "command": "python",
      "args": ["E:/study/Codebase Navigator/mcp_server.py"]
    }
  }
}
```

暴露的工具：
- 静态分析：`list_directory_structure` / `read_file` / `search_code` / `find_files_by_pattern` / `get_imports` / `find_entry_points` / `analyze_dependencies` / `get_function_signatures`
- 报告：`generate_static_report`（免费）、`generate_ai_overview`（带 `file:line` 引用，需 API Key）
- 仓库加载：`load_repo`（GitHub URL 自动浅克隆到本地缓存）

## 评测

```bash
# 克隆并评测全部 10 个仓库（有缓存会复用）
python evals/run_evals.py

# 只跑部分仓库
python evals/run_evals.py --filter flask,gin

# 只用本地缓存
python evals/run_evals.py --local
```

评测集在 `evals/repos.yaml`，可随意增删。注意：GitHub 部分仓库在国内网络下可能无法访问，换一个可访问的仓库或镜像即可。

### AI 报告评测（Phase 2）

```bash
# 对指定仓库生成 AI 概览并自动校验引用（默认 OpenRouter 免费模型 z-ai/glm-5.2:free）
python evals/run_ai_evals.py --filter flask,requests --save evals/.cache/ai_reports
```

指标：幻觉率（引用指向不存在的文件，验收 0%）、引用精确率（歧义引用只警告不计幻觉）、章节覆盖。报告原文保存后可人工抽查。

## 项目结构

```
├── app.py                  # Gradio Web 单页界面
├── cli.py                 # 命令行入口
├── src/
│   ├── repo.py            # 仓库加载（URL 克隆 / 本地路径）
│   ├── report.py          # 静态学习报告（无 LLM）
│   ├── agent.py           # Agent 主循环（工具调用，无 LangChain）
│   ├── learn.py           # 带读剧本结构校验
│   ├── tutor.py           # 带读陪练（苏格拉底式逐问判定）
│   ├── prompts.py         # 系统提示词（反幻觉约束）
│   ├── config.py          # 多提供商 LLM 配置
│   └── tools/             # 8 个确定性分析工具
│       ├── file_explorer.py   # 目录 / 读文件 / 搜索 / 找文件
│       └── code_analyzer.py   # imports / 入口点 / 依赖 / 签名
├── evals/                 # 评测集与评测脚本
└── tests/smoke.py         # 冒烟测试
```

## 工作原理

```
GitHub URL / 本地路径
      ↓
静态分析（确定性工具，无 LLM）──► 免费静态报告
      ↓
Agent 循环：模型决定调哪个工具 → 执行 → 结果回传
      ↓
结构化学习报告（每条结论带 file:line 引用）
```

设计要点：

- **工具负责读代码，LLM 只负责总结与推理**，从根本上降低幻觉
- **自研 ReAct 式工具循环**（约 60 行），不依赖 LangChain/LangGraph，可读性强、易扩展
- **确定性工具优先**：纯 Python 实现，跨平台，不需要 rg/grep

## 添加一个新工具

1. 在 `src/tools/` 里写一个返回字符串的函数
2. 在 `src/tools/__init__.py` 的 `TOOLS` 和 `TOOL_SCHEMAS` 里注册
3. 在 `src/prompts.py` 的工具列表里加一行描述
4. 跑 `python tests/smoke.py` 验证

## 路线图

见 `PRD.md`：Phase 1 核心引擎（当前）→ Phase 2 LLM 报告 → Phase 3 CLI/Web → Phase 4 学习闭环。

## License

MIT
