# code-cleanup

[![skills.sh](https://skills.sh/b/0ct/code-cleanup-skill)](https://skills.sh/0ct/code-cleanup-skill)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](./LICENSE)

一个给 AI 编码 agent 用的代码卫生清理 skill。目标是**让仓库重新成为唯一可信的上下文源** —— 清掉上一轮改动、上下文压缩、agent 模型更新之后留在项目里的残留与自相矛盾，并把"我改了什么"变成可核查的增量。

An agent skill for post-edit repo hygiene: remove dead code, duplicated implementations and stale instructions left behind by half-finished edits, context compaction and model upgrades. Chinese-language instructions; read-only scanner is language-agnostic.

## 安装

```bash
# 装到当前项目（.claude/skills/ 等约定目录）
npx skills add 0ct/code-cleanup-skill --skill code-cleanup

# 装到用户级，所有项目可用
npx skills add 0ct/code-cleanup-skill --skill code-cleanup -g

# 先看看仓库里有什么，不安装
npx skills add 0ct/code-cleanup-skill --list
```

也可以直接从仓库路径装：

```bash
npx skills add https://github.com/0ct/code-cleanup-skill/tree/main/skills/code-cleanup
```

手动安装（不使用 skills CLI）：

```bash
git clone https://github.com/0ct/code-cleanup-skill /tmp/cc
cp -R /tmp/cc/skills/code-cleanup ~/.workbuddy/skills/     # WorkBuddy
# cp -R /tmp/cc/skills/code-cleanup ~/.claude/skills/      # Claude Code
```

装完重启会话，技能列表里会出现 `code-cleanup`。触发词：`清理代码`、`代码卫生`、`死代码`、`项目体检`、`deslop`、`code cleanup`。

## 它解决的问题

不是"代码不够整洁"，是**仓库和"关于仓库的描述"脱钩了**。四种漂移源：

| 漂移源 | 表现 | 清理动作 |
| --- | --- | --- |
| 代码漂移 | 上一轮改到一半的尝试、被放弃的分支、绕开报错的临时补丁留在原处 | 删除死代码与被取代的实现 |
| 记忆漂移 | 上下文压缩后 agent 凭摘要判断"这函数没人用"，于是在旁边又写一份（zombie pattern） | 用引用与调用链重建事实，删掉重复的那份 |
| 习惯漂移 | 模型/工具链更新后，旧的冗余防御、隐式 `any`、过时 API 还在被复制 | 按当前写法收敛，不改行为 |
| 指令漂移 | `AGENTS.md`/README 描述的模块已删，下一个 agent 照着文档把它写回来 | 让文档与代码对齐，或标记为待确认 |

## 检查范围

十类，每类都在 `references/audit-checklist.md` 里给了判定证据标准、检索手段、常见误判和可自动修的条件：

损坏/不可达代码 · 前后矛盾逻辑 · 重复实现 · 过时/未使用实现 · 无谓抽象与过度防御 · 低效写法 · 过期注释与文档 · 过期配置/测试 · 依赖漂移 · 指令漂移

## 工作流

八个阶段，不可跳步：

1. **建立事实基线** —— `git status` / 最近改动 / 构建与测试是否可跑
2. **通读整个项目** —— 入口约定 → 入口代码 → 核心模块 → 周边 → 文档；读完的判据是能不看文件答出入口、数据流、接口面、怎么构建测试
3. **跑只读扫描器** —— 拿线索，不下结论
4. **逐条定性** —— 四个必要条件全满足才动手（见下）
5. **最小改动修复** —— 按类别分批，每批独立可回退
6. **验证** —— 基线对比：测试数不减、通过数不降、类型错误不增
7. **复扫** —— 删除会制造新的死代码，级联影响必须再看一遍
8. **输出** —— 已修复摘要 + 待确认清单 + 未覆盖声明

## 只读扫描器

`scripts/repo_scan.py` 是纯 stdlib 单文件脚本，不装依赖、不改文件、不发网络请求。

```bash
python3 skills/code-cleanup/scripts/repo_scan.py <repo> --md scan.md --json scan.json
```

| 参数 | 作用 |
| --- | --- |
| `--focus <subdir>` | 只扫某个子目录 |
| `--since-days N` | 给近 N 天改动过的文件打 `🆕`（默认 30） |
| `--baseline old.json` | 与上一次扫描对比，输出「清理效果对比」表 |
| `--limit N` | 每类最多输出 N 条（默认 200） |
| `--max-total-bytes` | 读取总预算，默认 120MB，防大仓库 OOM |
| `--no-git` | 跳过 git 相关检查 |
| `--quiet` | 不打印进度 |

覆盖 JS/TS/monorepo（逐包 `package.json`）、Python（`pyproject`/`requirements`）、Go（`go.mod` require vs import，含 indirect 排除）、Rust（`Cargo.toml` vs `use`）。

输出示例（故意植入 12 类问题的夹具仓库，命中 18 条）：

```
- 扫描文件：8 个 / 可读文本 8 个 / 代码 5 个
- 命中条目：18 条，其中 14 条落在近 30 天改动过的文件里

| 分类 | 条目 | 其中高置信可核实 |
| --- | --- | --- |
| 失效引用 | 1 | 1 |
| 声明未使用的依赖 | 1 | 0 |
| 硬编码路径与疑似凭据 | 2 | 1 |
```

## 安全边界

**能自动改的只有那些满足四个必要条件的**：不可达性可证 + 接口不变 + 局部可逆 + 可验证。缺一条就进待确认清单，不擅自改。

风险分级：

| 级别 | 含义 | 处理 |
| --- | --- | --- |
| R0 | 无行为影响，纯文本订正 | 直接改 |
| R1 | 删除静态可证的死代码，不在导出面 | 直接改 + 复扫 |
| R2 | 涉及导出面、错误路径、跨模块契约 | 只进清单 |
| R3 | 鉴权/加密/并发/序列化/迁移/生成代码/vendored/凭据/用户未提交改动 | 只报告，绝不动手 |

硬编码凭据只报告不清除 —— 删掉不等于轮换，留着痕迹才有追溯价值。

## 已知短板

- `commented_code` 会把 docstring 里的 ASCII 图、缩进代码示例误判成"被注释掉的代码"。脚本标 `likely`，需要人过一眼；根治需要 AST，会让脚本从"任何环境能跑"变成"需要装依赖"。
- 无引用文件检测是纯文本级：反射、字符串路由、barrel 导出、`__init__` 副作用、装饰器注册、feature gate 都会绕过它。所以那类结论一律 `speculative`，`references/` 里专门列了"看起来死了其实活着"的清单。
- 单文件脚本做不了真实调用图，跨语言/跨进程的引用（配置驱动、gRPC、消息队列）看不到。
- 指令是中文的；扫描器输出也是中文。

## 仓库结构

```
skills/code-cleanup/
├── SKILL.md                        # 主流程：8 阶段 + 四条漂移源 + 收敛判据
├── references/
│   ├── audit-checklist.md          # 十类问题的判定证据标准与误判陷阱
│   ├── agent-residue.md            # 六种 agent 残留形态与归属判定
│   ├── risk-and-fix-rules.md       # R0–R3 分级、白名单/黑名单、验证矩阵
│   ├── tooling.md                  # knip / vulture / staticcheck 等工具梯子与纪律
│   └── report-template.md          # 四段式输出模板 + 硬约束 + 反例
└── scripts/repo_scan.py            # 只读事实扫描器（纯 stdlib，1315 行）
```

## License

MIT
