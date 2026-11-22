# 确定性工具梯子

脚本能发现"形态"，工具能发现"结构"。两者互补：脚本跨语言、无需安装、但只看文本；工具懂入口点与模块图、结论更硬、但配置错了会成片误报。

顺序：**先脚本拿全貌 → 再工具验硬结论 → 冲突时读代码**。

---

## 一、按语言

### JavaScript / TypeScript

| 工具 | 命令 | 能发现 | 注意 |
| --- | --- | --- | --- |
| **knip** | `npx knip`<br>`npx knip --production` | 未使用的文件、导出、依赖、枚举成员；monorepo 与 workspace；内置 200+ 框架插件（Next/Vite/Vitest/Jest/ESLint/Storybook/Cypress） | **首选**。入口点配置错 → 活文件被报成死文件 |
| depcheck | `npx depcheck` | 仅 package.json 与 import 的差异 | 与 knip 交叉验证依赖部分 |
| ts-prune | `npx ts-prune` | 仅未使用的导出 | npm 上的版本较旧，且只读 tsconfig；用 knip 更稳 |
| ESLint | `npx eslint . --report-unused-disable-directives` | 未使用变量/导入、失效的 disable 指令 | 逐文件，看不到跨文件 |
| jscpd | `npx jscpd --min-lines 5 src/` | 跨文件复制粘贴块 | 重复检测比脚本的窗口哈希更准 |

### Python

| 工具 | 命令 | 能发现 | 注意 |
| --- | --- | --- | --- |
| **vulture** | `vulture src/ --min-confidence 80` | 未使用的函数/类/变量/属性（AST 分析，带置信度） | `--fix` 存在，**先不加 --fix 看输出** |
| deptry | `deptry .` | 缺失（DEP001）、未使用（DEP002）、错放在 dev（DEP004）依赖 | 支持 pip/poetry/pdm/uv/PEP 621 |
| ruff | `ruff check --select F401,F841 .` | 未使用 import / 未使用局部变量 | 语言级可判，可直接采信 |
| pyflakes | `pyflakes .` | 未使用导入、未定义名 | 无依赖，轻量 |

### Go

| 工具 | 命令 | 能发现 | 注意 |
| --- | --- | --- | --- |
| **deadcode** | `deadcode ./...`<br>`deadcode -test ./...` | 从 main 不可达的函数与方法（RTA 全程序分析） | 库必须加 `-test`；看不到 assembly / `go:linkname` |
| staticcheck | `staticcheck ./...`（或 `U1000`） | 包内未使用的函数/类型/字段/常量 | 把导出名视为已使用，因此**不报未使用的导出** |
| go mod tidy | `go mod tidy -diff` | 多余模块 | 会改 go.mod/go.sum，用 `-diff` 只看差异 |

### Rust

| 工具 | 命令 | 能发现 | 注意 |
| --- | --- | --- | --- |
| **clippy / 编译器** | `cargo clippy --all-targets` | `dead_code` lint：未使用的私有项 | 导出项不报（给下游用） |
| cargo-machete | `cargo machete` | 未使用的依赖 | 稳定版可用，比 udeps 好装 |
| cargo-udeps | `cargo +nightly udeps` | 未使用的依赖 | 需要 nightly |

### Java / Kotlin

| 工具 | 命令 | 能发现 |
| --- | --- | --- |
| IntelliJ | "Unused declaration" 全项目检查 | 未使用声明（需先配框架入口注解） |
| PMD | `pmd check -d src -R category/java/bestpractices.xml` | `UnusedPrivateMethod`/`UnusedLocalVariable` 等，只覆盖私有与局部，因此可安全采信 |
| Maven | `mvn dependency:analyze` | 声明未用 / 使用未声明（读字节码） |
| Gradle | Dependency Analysis Plugin | 同上 |

### C# / .NET

`IDE0051`（未使用私有成员）、`IDE0052`（只写不读）在 `.editorconfig` 里设为 warning，随构建输出。公开成员需要 Rider/ReSharper 做全解决方案分析。

### PHP

PHPStan + `shipmonk/dead-code-detector`（含 Symfony/Laravel/Doctrine/PHPUnit/Twig 入口支持）；依赖用 `composer-dependency-analyser`。

### Swift

`periphery scan --project <xcodeproj>`，覆盖未使用的类/结构/协议/函数/属性/枚举 case。

### 跨语言

- 重复代码：`jscpd --min-lines 5 <dir>`
- 复杂度：`lizard <dir> --CCN 10 --length 50`、`radon cc src/ -a -nc`（Python）、`gocyclo -over 10 .`（Go）
- 复杂度只用来定位"值得读的可疑位置"，**不是清理依据**——低效与复杂不是本 skill 的清理对象，只进报告

---

## 二、五条使用纪律

### 1. 先 dry-run，永不直接 `--fix`

`knip --fix`、`vulture --fix`、`deadcode --fix` 都会直接改文件。入口点配错时，它们会把活代码成片删掉。凡带 `--fix` 的工具，第一次一律只跑不带 fix 的版本，逐条看输出，确认能解释每一条之后再决定是否采纳。

### 2. 入口点配置是唯一失败模式

所有"未使用"判断都建立在"我知道哪些是入口"之上。以下都是入口，配漏了就会误报：

- 框架文件路由（Next.js `app/`、Nuxt `pages/`）
- 配置里字符串引用的文件（CI、Docker、`package.json scripts`、`Makefile`）
- 反射/装饰器/DI 装配的类
- 测试收集（pytest 的 `test_*`、jest 的 `*.test.*`）
- 构建期插件加载的模块
- 迁移脚本、CLI 子命令、`conftest.py`

**判据：工具报某文件未使用，但这个文件出现在任何配置文本里 → 以配置为准，不删。**

### 3. 工具与脚本冲突时，读代码

| 冲突 | 含义 | 处理 |
| --- | --- | --- |
| 工具说未使用，脚本也 0 引用 | 强证据，但仍需排除动态入口 | 大概率可删，走 R1 |
| 工具说未使用，脚本发现被字符串引用 | 动态使用（路由/反射/配置） | 以字符串引用为准，不删 |
| 工具说已使用，脚本 0 引用 | 工具知道插件/入口知识 | 信工具，不删 |
| 两者都说未使用，但代码明显是公开 API | 外部消费者 | 不删，进 R2 并注明"疑似对外契约" |

冲突点本身值得写进报告——它通常意味着这个模块的边缘知识只存在于某个人脑子里。

### 4. 不要为了让工具跑起来而改项目

- 用 `npx`/`uvx`/`go run` 临时执行，不要往 `package.json`/`pyproject.toml` 里加依赖
- 不要为扫描新增配置文件（`knip.json`、`.vulture_whitelist.py` 之类）——那是改动，不是清理
- 工具需要联网下载时，若环境不允许，退回脚本 + grep，并在报告中声明"未做工具交叉验证"

### 5. 报告里记录命令与结论

可复现才能复核。每条工具结论标注：工具名 + 命令 + 采纳/未采纳的理由。未采纳的也要写，否则下一个人会重跑一遍。

---

## 三、什么时候不用工具

- 项目没有可用的包管理器或工具链（离线、老旧环境）
- 依赖分析会触发网络或需要凭据
- 单文件/小改动场景：`repo_scan.py --focus <dir>` 足够
- 工具报出几百条且无法区分优先级：先修入口点配置，再跑；否则输出不可用

**工具是可选的加速器，不是前提。** 没有工具时，脚本 + 引用计数 + 读代码仍然是完整流程。
