# 风险分级与最小改动规则

## 一、四级风险

| 级别 | 判据 | 动作 | 例子 |
| --- | --- | --- | --- |
| **R0** 纯残留 | 不在任何编译/运行/测试路径上 | 直接删 | `*.bak`、`.DS_Store`、空文件、`src/old/` 下已确认无引用的整块注释代码 |
| **R1** 局部低风险 | 影响面限于单文件/单函数，语言级可判 | 直接改 + 验证 | 未使用 import、局部未使用变量、库代码里的 `console.log`、注释参数名错、私有函数引用计数 0 |
| **R2** 需确认 | 有外部可见性、跨模块语义、或无法本地验证 | 只进清单 | 删导出符号、合并重复实现、删依赖、改默认值、删测试、修逻辑矛盾、改配置项 |
| **R3** 禁改 | 错一次的代价远大于收益 | 只进清单并标注"勿自动处理" | 鉴权/加密/签名、并发与锁、序列化与迁移脚本、feature flag 内部、生成代码、vendored、凭据、用户未提交改动 |

升级规则（只看一条就必须升级）：

- 删掉的东西出现在**导出面**（`export`、`__all__`、`pub`、barrel 文件、`package.json exports`、接口/协议声明）→ 至少 R2
- 改动会改变**错误路径**（分支、catch、默认值、重试）→ 至少 R2
- 改动跨**进程边界**（序列化格式、消息 schema、URL、DB 列）→ R3
- **无法运行验证**且不能静态证明等价 → 至少 R2
- 删除依据只来自**工具输出**、未核实动态入口 → 至少 R2
- 目标是**指令文件/文档/配置文本**（`AGENTS.md`、README、CI、脚本）→ R2
- 目标文件**有未提交改动** → R3

## 二、自动修改的四个必要条件

全部满足才动手，任一不满足即进清单：

1. **不可达性可证**——引用计数为 0，且已排除动态入口
2. **接口不变**——签名、字段、路由、schema、配置键、错误码、日志字段都不变
3. **局部可逆**——单文件或单函数内；删掉不影响其他调用点；能单独回退
4. **可验证**——有测试/构建/类型检查，或能静态证明前后等价

写代码前先在脑内回答：**这条改动如果错了，谁会先报错？** 答不出"谁会报错"，说明验证手段不足 → R2。

## 三、白名单：算清理

- 删除未使用的 import / 局部变量 / 私有函数（引用计数 0 且非动态入口）
- 删除库代码里的调试输出（CLI、脚本、测试除外）
- 删除备份文件、系统垃圾、构建残留
- 删除已确认无引用的注释代码块
- 订正与实现不符的注释（参数名、返回类型、行为描述）
- 删除已无任何消费方的配置项（先确认无外部部署依赖）
- 删除不可能执行到的语句（同一 block 内 `return` 之后）
- 合并被同名定义遮蔽后完全失效的旧定义

## 四、黑名单：不算清理，是重构

哪怕只做一处也不允许，出现即说明越界：

- 重排/分组 import、调整空行、改缩进/引号/分号/尾逗号、`prettier`/`black` 全量格式化
- 重命名任何标识符（含私有）
- 抽取公共函数、拆文件、合并文件、引入新抽象或设计模式
- 加类型标注、加 docstring、加注释解释"这里清理过"
- 改错误信息文案、日志格式、日志级别
- 改默认值、改超时、改重试次数
- 升级/降级依赖、替换等价库、切换构建工具
- 新增测试、新增文档、新增 `.gitignore` 条目（除非残留文件确实需要长期忽略）
- 顺手修的"小 bug"——发现就写进清单，别夹带
- 改公共 API 的返回形状（哪怕"更合理"）
- **对工具直接跑 `--fix`**（`knip --fix`、`vulture --fix`、`deadcode --fix`）——入口点配错时会成片误删
- 删除无谓抽象层、合并双份实现、给 `any` 补类型——都属重构
- 在代码里留 `// removed`、`// cleanup`、`// 这里以前是 xxx` 之类墓碑注释

## 五、patch-only 模式

满足任一条，就**不要**在工作区直接改，改为产出 patch：

- 工作区有未提交改动，且清理目标与它们重叠
- 无 git，且用户未同意做备份
- 跑不了任何验证（无测试、无法构建、无法启动）
- 用户明确要求"先给我看改什么"

做法：

```bash
# 1. 在临时副本上改，不动原仓库
rsync -a --exclude .git <repo>/ /tmp/cleanup-work/
# 2. 在副本里执行清理
# 3. 产出可 review 的补丁
diff -ruN --exclude .git <repo> /tmp/cleanup-work > /tmp/cleanup.patch
```

报告里给出补丁路径、改动文件列表、每处改动的依据。**由用户决定是否应用**，不代替用户落盘。

判断"能不能直接改"只有一个问句：**改错了，我能在 30 秒内让它完全复原吗？** 能 → 直接改；不能 → patch-only。

## 五、各语言的"看起来死了其实活着"

删除前必须逐条排除对应项。

### Python

- `__init__.py` 的副作用导入（注册、patch）、`__all__` 里的名字
- 装饰器注册：Flask/FastAPI 路由、Celery task、pytest fixture、click command
- `importlib.import_module`、`pkgutil` 扫描、`entry_points` 插件
- Pydantic/dataclass 字段：代码不读，序列化在读
- `if TYPE_CHECKING:` 块内的导入（类型检查器要用）
- `conftest.py`、`test_*.py` 由 pytest 收集，无静态引用
- 模块级 `__getattr__`（延迟导出）
- `setup.py` / `pyproject` 的 `scripts` 入口

### JavaScript / TypeScript

- barrel 文件 `index.ts` 的 `export *`；`package.json` 的 `exports`/`main`/`types`
- 副作用导入 `import './polyfill'`、`import './styles.css'`
- 动态 `import()`、`require(variable)`、`require.context`（webpack）
- 框架文件路由（Next.js `app/`、Nuxt `pages/`）、约定式导出（`getServerSideProps`、`loader`、`action`）
- 装饰器（Angular/NestJS `@Injectable`、`@Controller`）
- `@types/*`、`core-js`、`tslib`、`reflect-metadata`
- `tsconfig` 的 `paths` 别名——用别名引用时静态搜索找不到原路径

### Go

- `init()` 函数、空白导入 `_ "driver"`（注册驱动）
- 方法看似无人调用，实为接口实现（`var _ Iface = (*T)(nil)`）
- build tag 区内代码（`//go:build linux`）在当前平台"不可达"是正常的
- `//go:embed` 引用的静态资源
- 导出的包级符号供外部模块使用
- struct tag 驱动的序列化（字段代码不读）

### Rust

- `pub` 项供下游 crate 使用；`#[used]`、`#[no_mangle]`；trait impl 的方法
- feature gate 后的 `#[cfg(feature = "...")]`
- `PhantomData`、生命周期标记
- `mod` 声明链——删文件前先删 `mod`

### Java / Kotlin

- Spring/Guice 的 `@Component`/`@Bean`/`@Autowired`（反射装配）
- JPA 实体与 getter/setter（ORM 反射）
- Jackson/Gson 序列化字段
- AOP 切点方法、`@Scheduled`、`@EventListener`
- 重载方法被框架按签名调用

### Swift / Objective-C

- `@objc` 选择器、Storyboard/XIB 的 outlet 与 action
- 协议实现（`UITableViewDataSource` 等）
- `Info.plist` 引用的类型与 URL scheme

### 跨语言通用

- 其他仓库/客户端在消费（公开 API、SDK、CLI 参数、webhook 契约）
- 运维侧在用：cron、监控探针、日志解析规则、仪表盘查询
- 数据侧在用：SQL 视图、ETL 脚本、报表读取的列
- 字符串拼出来的调用（路由、事件名、table 名、i18n key）

## 六、验证矩阵

| 改动类型 | 最低验证 | 更强的验证 |
| --- | --- | --- |
| 删备份/垃圾文件 | `git status` 无预期外变更 | 构建产物无差异 |
| 删未使用 import | 编译/类型检查 | lint + 单测 |
| 删调试输出 | 编译 + 冒烟运行 | 单测（若断言了 stdout 则必须） |
| 订正注释 | 人工复核实现 | — |
| 删私有函数 | 类型检查 + 单测 | 覆盖率对比（下降需解释） |
| 删注释代码 | 编译 + 单测 | 人工确认非文档示例 |
| 删配置项 | 启动/加载配置 | 部署 dry-run |
| 删依赖 | 干净环境安装 + 全量测试 | 构建 + 镜像构建 |
| 改逻辑矛盾 | 单测（须有覆盖该逻辑的用例） | 端到端 |
| 删无谓抽象 | 调用链全部走通 + 单测 | 集成测试 |
| 改指令/文档 | 逐条执行文档里的命令 | 新会话按文档走一遍 |

**基线对比硬约束**：测试总数不减、通过数不降、类型检查错误数不增。任何一项变差 → 回退该处改动，改入清单。

**无法验证时**：报告中该条写成"已改待验证"，并列出建议的验证命令。不许写"已修复"。

### 无测试时的行为快照

没有测试不等于不能验证。用同一批输入跑改动前后的版本，比对输出：

```bash
# 1. 改动前采集
python3 -c "import api; print(api.summarize(SEED_INPUT))" > /tmp/before.txt 2>&1; echo $? >> /tmp/before.txt
# CLI 项目直接跑固定参数
./cli --input fixtures/sample.json --json > /tmp/before.json 2>&1
# 2. 改动后再跑一次，diff
diff /tmp/before.txt /tmp/after.txt && echo "行为一致"
```

要点：

- **覆盖错误路径**，不只跑 happy path：喂一个非法输入，确认报错信息与退出码不变
- 有副作用（写文件/发请求）时用 mock 或 dry-run 模式，别打真实环境
- 快照只能证明"这条路径没变"，不能证明全部。报告里写清采样了哪些输入

## 七、执行与回退

- **先清 `🆕`**：近期改动过的文件里的残留，最可能是上一轮没收尾的，且它们离当前上下文最近
- 按**类别**分批，每批一个 `git commit`（用户同意提交时）或产出独立 patch 文件
- 每批结束跑一次验证，失败只回退本批
- 每批结束重跑 `repo_scan.py`：删除会制造新的死代码，级联影响必须再看一遍
- 全部结束后跑一次带 `--baseline` 的对比，把"清掉了多少"变成数字
- 结束前 `git diff --stat` 自查：改动行数应与"删除量"同量级；出现大量新增行说明越界了

**回退粒度**：一处改动一个可独立回退的单元。做不到独立回退的改动，说明它跨了文件或跨了语义，本身就该进清单。

## 八、判断示例

| 现场 | 判断 | 理由 |
| --- | --- | --- |
| `utils/legacy_helper.py` 全仓 0 引用，且模块内无装饰器、不在 `__init__` 导出 | R1 删 | 引用为 0，非导出面，非动态入口 |
| 同文件里 `def _parse()` 0 引用 | R1 删 | 私有 + 静态可判 |
| `api_v1.py` 里有 `/v1/users` 路由，客户端已迁到 v2 | R2 清单 | 对外契约，可能有旧客户端 |
| 两处 `MAX_RETRY`：`config.py=3`、`client.py=5` | R2 清单 | 需知道哪个是意图，不能猜 |
| `.env.example` 有 `SENTRY_DSN`，代码只读 `SENTRY_DSN_URL` | R2 清单 | 可能是部署侧注入的旧键 |
| `package.json` 里 `lodash` 无 import，但 `scripts.build` 用它做 CLI | 不报或 R2 | 工具链消费，静态引用看不到 |
| 注释写"按 id 升序"，代码是 `desc()` | R1 改注释 | 实现有测试佐证，注释是过期的 |
| 测试 `test_legacy_format` 断言已删字段 | R2 清单 | 删测试=放弃契约，需确认 |
| `src/generated/api_client.ts` 有未使用函数 | R3 只报 | 生成代码，改了会被覆盖 |
| 硬编码 `sk-...` 出现在源码 | R3 只报 | 必须人工轮换，不能只删 |
| `getUser` 与 `fetchUser` 两份实现，后者多了 try/catch，调用点各 4 处 | R2 清单 | 双份实现，须定权威版本；`knip` 两份都算"已使用" |
| `def get_user(uid): return repo.find(uid)`，仅 1 处调用、无测试 | R2 清单 | 无谓抽象；删它会改调用链，且可能是有意接缝 |
| `except Exception: return []` 包住一次 DB 查询 | R3 只报 | 错误路径从"抛出"变"静默空列表"，产品影响大 |
| `AGENTS.md` 写"定价逻辑在 src/pricing/v1.py"，该文件已删 | R2 清单（高优先） | 指令漂移，会诱导下一个 agent 复活已删代码 |
| `knip` 报 `src/legacy/` 未使用，但 `Dockerfile` 里 `cp -r src/legacy` | 不删 | 工具看不到部署期引用 |
