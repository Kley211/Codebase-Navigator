# Codebase Navigator

> 30 分钟读懂任意代码库。输入一个 GitHub 仓库或本地项目，生成结构化学习报告：项目概览、模块地图、关键文件、阅读路线，并支持边读边问。

## 特性

- **静态分析引擎（无需 API Key）**：目录结构、入口点、依赖、imports、文件热度，秒级输出
- **AI 学习报告**：Overview / 模块地图 / Key Files / Roadmap，每条结论带 `file:line` 引用（反幻觉）
- **多模型支持**：DeepSeek（国内直连）/ OpenRouter / Groq / OpenAI / 任意 OpenAI 兼容服务
- **CLI 优先**：一份 Markdown 报告即交付物，方便导出、分享、写进简历
- **评测驱动**：内置 10 个 GitHub Trending 热门仓库评测集，质量可量化

## 快速开始

```bash
# 1. 创建虚拟环境并安装依赖
python -m venv .venv
.venv\Scripts\activate          # Windows
source .venv/bin/activate       # macOS / Linux
pip install -r requirements.txt

# 2. 配置 API Key（可选，静态报告不需要）
cp .env.example .env            # 填入 DEEPSEEK_API_KEY 等

# 3. 生成静态学习报告（免费，无需 Key）
python cli.py https://github.com/psf/requests --report

# 4. 生成 AI 概览（需要 Key）
python cli.py https://github.com/psf/requests --overview

# 5. 追问具体问题
python cli.py https://github.com/psf/requests --ask "认证是如何实现的？"

# 6. 保存报告
python cli.py https://github.com/psf/requests --report --output report.md
```

也支持本地路径：`python cli.py E:\some\repo --report`。

## Web 界面（Phase 3）

```bash
pip install -r requirements-web.txt
python app.py
# 打开 http://127.0.0.1:7860
```

单页包含：静态报告（免费）→ AI 概览（含工具调用轨迹）→ 边读边问。AI 功能默认读取 `.env` 的 API Key，也可在页面上临时填写。

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
# 对指定仓库生成 AI 概览并自动校验引用（需要配置 DeepSeek API Key）
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