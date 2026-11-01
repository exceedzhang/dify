# browser-use → AgentV2 移植文档

> 目标读者:需要在**另一个 Dify 代码库/版本/部署形态**中复刻"AgentV2 浏览器能力"(`browse_web`)的工程团队。
> 本文基于本仓库已落地实现(方案全文见 `PLAN-browser-use-agentv2.md`,P0–P3 全部完成并验收),并补入后续运维中沉淀的模型兼容性加固。
> 基线:Dify AgentV2 原生 Pydantic AI 模块架构(#43833 后)、`dify-agent` 独立 agent_backend、shellctl sandbox 运行时。

---

## 1. 移植内容概述

给 AgentV2 agent 增加一个 `browse_web(task: str) -> str` 工具:

- 由 `pydantic-ai-harness` 的 `BrowserUse` capability 提供,browser-use 子代理通过 CDP 驱动**真实 Chromium** 执行多步网页任务(填表、抓取、登录态操作),返回文本/结构化结果。
- **浏览器进程跑在 sandbox 容器**(local_sandbox / E2B)内,复用 shellctl job 生命周期与 Landlock 隔离;agent_backend 只是 CDP 客户端。
- **LLM 密钥不出 agent_backend**:子代理复用宿主模型(`ctx.model`),推理仍走 plugin daemon,sandbox 内零密钥。

## 2. 版本与依赖基线

| 组件 | 版本 | 说明 |
|---|---|---|
| pydantic-ai-slim | `==2.31.0` | dify-agent 主依赖,Agent/Model 契约来源 |
| pydantic-ai-harness | `>=0.20.0,<0.21.0` | `BrowserUse` capability 提供方;server extra 拉 `[browser-use]` |
| browser-use | `>=0.13.6,<0.13.10` | 0.13.10+ 要求 pydantic 2.13,与 dify-agent `pydantic<2.13` 冲突 |
| pydantic | `<2.13` | 硬约束,见上 |
| Python | 3.13 | agent_backend 与 sandbox 镜像均 3.13 |
| sandbox 基座 | `python:3.13-slim-bookworm`(Debian 12) | apt `chromium` ≈ 156.x |
| shellctl | 带 `port_entrypoint`/lease 协议的当前版 | `RuntimeLease` 协议必须可加方法 |
| Landlock | Linux ≥5.13,fail-closed(#44152 语义) | 策略拒绝 `/dev/shm`、`/dev/dri`,见 §4.1 |

**依赖冲突与解法(必做)**:browser-use 0.13.6–0.13.9 硬钉 `anthropic==0.76.0`/`google-auth==2.48.0`/`google-genai==1.65.0`/`openai==2.26.0`,与 `pydantic-ai-slim[anthropic,google,openai]` 的下限直接矛盾。因 Dify 恒传宿主 Pydantic AI 模型(`llm=ctx.model`),browser-use **从不实例化自己的 provider client**,被钉 SDK 仅存 import 面——用 uv 解决:

```toml
# dify-agent/pyproject.toml
[project.optional-dependencies]
server = [
  "pydantic-ai-harness[browser-use]>=0.20.0,<0.21.0",
  "browser-use>=0.13.6,<0.13.10",
  "pydantic-ai-slim[anthropic,google,openai]==2.31.0",
  # ...
]

[tool.uv]
override-dependencies = [
  # 把四个 SDK 抬到 pydantic-ai-slim 要求的下限
  "anthropic>=0.108", "google-auth>=2.48", "google-genai>=1.70", "openai>=2.45",
]
```

验收:`browser_use` 全量 import + `BrowserUse` 构造 + `get_toolset()`/`get_instructions()` 在锁定环境通过。

## 3. 总体架构

```
api  AgentSoulConfig.browser(enabled/参数) + AGENT_BROWSER_ENABLED(全局开关)
 └─ clients/agent_backend/request_builder.py
     include_browser → RunLayerSpec{name:"browser", config:{...}}(composition v2)
 └─ agent_backend /runs
     runtime/modules.py  REGISTRY["browser"](依赖可导入时注册)
     └─ browser 模块 Capability(AbstractCapability[Deps])
         ├─ get_ordering: requires/wrapped_by RuntimeCapability(需要 RuntimeLease)
         ├─ wrap_run:借 lease → shellctl job 运行内联 python launcher
         │           ├─ headless chromium(--remote-debugging-port=0,2>&1 合流)
         │           ├─ in-job CDP forwarder(0.0.0.0:随机端口 → 127.0.0.1:cdp_port,
         │           │    首请求 Host 头改写 localhost)
         │           ├─ stdout 打印 "CDP_READY ws://0.0.0.0:FWD_PORT/devtools/browser/<id>"
         │           └─ wrap_run 轮询 CDP_READY → port_entrypoint(FWD_PORT) 拼 ws URL
         │           → BrowserUse(cdp_url=..., llm=ctx.model, **cfg) → run 资源表
         ├─ get_toolset:FunctionToolset{ browse_web(ctx, task) } ← 惰性解析 session
         └─ get_instructions:透传 harness 静态 guidance(含 untrusted-content 安全规则)
 浏览器进程:sandbox 容器内;出站经 squid(拒私网);job 结束 tmux kill-session
             连带回收 chromium + forwarder 全部子进程(实测零残留)
```

关键时序事实(决定设计,移植前必须验证你的版本同样成立):

1. pydantic-ai 在 **Agent 构造期**调用 capability 的 `get_toolset()`/`get_instructions()`,早于 `wrap_run` → 工具必须**惰性**解析 CDP 地址(调用时才从 run 资源表取 session)。
2. harness `BrowserUseToolset` 在**每次工具调用时**读 `cdp_url` → CDP 地址可以在 `wrap_run` 内注入。
3. chromium ≥141 的 `--remote-debugging-address=0.0.0.0` **被静默忽略**(DevTools 只绑 loopback),且 DevTools HTTP 层**拒绝非 IP/localhost 的 Host 头** → 必须 in-job forwarder。

## 4. 移植步骤

### 4.1 Step 1:sandbox 镜像

`dify-agent-runtime/docker/Dockerfile` runtime 阶段:

```dockerfile
apt-get install -y --no-install-sandbox-recommends \
    chromium fonts-liberation fonts-noto-cjk fonts-noto-color-emoji
```

- 体积:+783MB(567MB → 1.35GB,arm64 实测)。
- E2B:`sync-e2b-template.sh` 的 `DEFAULT_MEMORY_MB` 1024 → **2048**(headless chromium 单实例 RSS 300–500MB)。注意:模板资源固定,`AsyncSandbox.create` 不按次传内存——该项影响**所有** E2B 沙箱(成本决策点)。
- chromium 启动参数(launcher 内,全部实测必需):

| 参数 | 原因 |
|---|---|
| `--headless=new --no-sandbox` | 容器内非 root 无法启用 chromium 自带 sandbox;安全边界=容器+Landlock(需评审) |
| `--disable-dev-shm-usage` | Landlock 策略**拒绝** `/dev/shm`,EACCES 即崩;改走 TMPDIR=job cwd(RW 区) |
| `--disable-gpu` | `/dev/dri` 不在 Landlock 策略内 |
| `--remote-debugging-port=0` | 自动选端口,共享容器多 run 并发不冲突 |
| `--user-data-dir=$BROWSER_PROFILE_DIR` | **必须每 job 唯一**(经 job env overlay 下发,如 `$HOME/.browser/<run-uuid>`);共享目录第二实例 exit 21(SingletonLock) |
| 环境变量 `BROWSER_PROFILE_DIR` | runner 不注入 job_id,profile 目录只能经 job env 下发 |

出站代理:job 环境读取 `HTTP_PROXY`(local_sandbox 已由 compose 设为 squid `http://agent_ssrf_proxy:3128`;E2B 无则直连)——**无需 API 传代理**,部署形态自动适配。

### 4.2 Step 2:agent_backend `browser` 模块

位置 `dify-agent/src/dify_agent/layers/browser/`(以 `layers/shell/`、`layers/knowledge_fs/` 为模板):

| 文件 | 内容 |
|---|---|
| `configs.py` | `DifyBrowserLayerConfig(BaseModel)`,`extra="forbid"`,字段见 §5;`output_schema` 在配置期编译 pydantic 模型(失败早暴露);`sensitive_data` 平铺值必须配非通配 `allowed_domains` |
| `layer.py` | `class Capability(AbstractCapability[Deps])`;内嵌 launcher 脚本与 CDP forwarder 源码(抽为模块级常量供单测 exec) |
| `output_schema.py` | JSON Schema → pydantic 模型(`build_output_model`) |
| `resilient_model.py` | `ResilientPydanticAIChatModel`(模型加固,见 §4.6) |
| `__init__.py` | 导出 `Config/State/Capability/DifyBrowserLayerConfig` 等 |

`Capability` 实现要点(`layer.py`):

- `get_ordering()` → `CapabilityOrdering(wrapped_by=[RuntimeCapability], requires=[RuntimeCapability])`。
- `State` 留空:snapshot v2 只存 State;resume 后原浏览器 job 已死,`wrap_run` 重新拉起即可;登录态用 workspace home 持久化(可选 `persist_login_state`)。
- `get_toolset()` 返回**自有** `FunctionToolset(id=name, sequential=True)`,单工具 `browse_web(ctx, task)`:从 `ctx.deps.resources.browsers[name]` 取 session → `session.harness_toolset.browse_web(task)`。**不要**直接暴露 harness 私有 toolset(构造期时序不鲁棒,且绑定其私有字段)。
- `get_instructions()` 透传 harness 静态 guidance;**禁止** `guidance=''`(默认 guidance 含 untrusted-content 安全规则)。
- `wrap_run(ctx, handler)` 七步:
  1. `lease = ctx.deps.resources.leases[config.runtime]`(缺失即报错;API 侧保证 runtime 层存在);
  2. `lease.commands.run(launcher, mode="stdio", env={"BROWSER_PROFILE_DIR": ...})` 起 job;
  3. 轮询 `read_output` 直到 `CDP_READY ws://...`(带超时,失败 kill job 报错);
  4. `lease.port_entrypoint(FWD_PORT)` 拼完整 ws URL(host 必须是容器可达地址);
  5. 构造 `BrowserUse(cdp_url, llm=加固后的 ctx.model, **cfg)`;`get_toolset()` 存 `BrowserSession{name, job_id, harness_toolset, ws_url, state_file}` 到资源表;
  6. `await handler()`;
  7. `finally`:kill job(实测 tmux kill-session 带走 chromium 全部子进程含 crashpad,零残留,无需 pkill 兜底)+ `harness.aclose()` + 删资源表项。

launcher 脚本(单进程 python 编排,内联字符串,不新增 sandbox 文件):

```python
# 核心结构(完整实现见 layer.py 的 _BROWSER_LAUNCHER_SCRIPT)
proc = chromium(..., stderr=STDOUT)          # 2>&1 合流:必须,stderr 行 API 不可见
while line := proc.stdout.readline():
    透传 line 到 job output
    首次匹配 "DevTools listening on ws://127.0.0.1:(\d+)(/\S+)":
        起 asyncio forwarder(0.0.0.0:0 → 127.0.0.1:cdp_port)
        print("CDP_READY ws://0.0.0.0:<fwd_port><path>")
```

CDP forwarder(约 60 行纯 asyncio,完整实现见 `_BROWSER_FORWARDER_SOURCE`):

- 双向 TCP 管道;首请求 Host 头改写 `(?im)^Host:[^\r\n]*` → `Host: localhost`(正则**不要**强制尾随 CRLF——Host 常是末行 header);
- 缓冲至 `\r\n\r\n` 处理半包,WS 握手后裸帧透传;半包重组后必须**恰好一个** `\r\n\r\n` 分隔符(重复写两遍会令 DevTools 直接断连——移植时高频踩坑点);
- 上游不可达时关闭 client。

### 4.3 Step 3:`RuntimeLease.port_entrypoint` 协议

`runtime_backend/protocols.py` 新增(返回类型用冻结 dataclass,**不能**用纯字符串——E2B 需要请求头):

```python
@dataclass(frozen=True)
class PortEntrypoint:
    base_url: str
    headers: dict[str, str] = field(default_factory=dict)

class ShellctlRuntimeLease(Protocol):
    def port_entrypoint(self, port: int) -> PortEntrypoint: ...
```

各后端:

| 后端 | 实现 |
|---|---|
| local | 从已存 endpoint 推导 host:`http://<sandbox_host>:<port>`,headers 空(同 `agent_sandbox_network` 任意端口可达) |
| e2b | `https://{sandbox.get_host(port)}` + `headers={"e2b-traffic-access-token": <token>}`;缺 token 抛 `BindingAcquireError` |
| openshell / enterprise | 抛 `PortEntrypointUnsupportedError`(`RuntimeBackendError` 子类)→ API 侧按 provider 不下发 browser 层 |

共享实现:`ShellctlRuntimeLease` 加 `port_entrypoint_factory: Callable[[int], PortEntrypoint] | None`(经 `create_shellctl_lease` 透传),local 的 factory 由 `LocalExecutionBindingBackend._port_entrypoint` 提供。

### 4.4 Step 4:模块注册与运行时接线

- `runtime/modules.py`:`REGISTRY["browser"] = browser`、`_REFERENCES[browser] = {"runtime": runtime}`。**懒注册**:browser-use 是可选 server extra,`try: import ... except ImportError: browser = None`,仅可导入时注册(否则 API venv 等无 extra 环境 import 即炸——P2 实测回归)。
- `runtime/context.py`:`RunResources` 加 `browsers: dict[str, BrowserSession] = field(default_factory=dict)`。
- `pyproject.toml`:按 §2 钉依赖。
- 并发防护(P3):`BrowserSessionSlots` + `browser_max_concurrent_sessions`,超限 fail-fast(429 语义);本地共享容器单容器 N 并发 run = N 个 chromium,必须限流。

### 4.5 Step 5:API 配置链

1. `api/models/agent_config_entities.py`:
   - `AgentSoulBrowserConfig`:字段同 §5 + `enabled: bool = False`;
   - `AgentSoulConfig` 加 `browser: AgentSoulBrowserConfig = Field(default_factory=...)`(`extra="forbid"` 模型加带默认值字段,旧快照/旧配置反序列化兼容,**零 DB 迁移**)。
2. `api/configs/extra/agent_backend_config.py`:`AGENT_BROWSER_ENABLED: bool = False`(全局 kill-switch)。
3. `api/clients/agent_backend/request_builder.py`:
   - 常量 `DIFY_BROWSER_LAYER_ID = "browser"`;
   - 两个 RunInput(workflow/agent_app)各加 `include_browser: bool = False` + `browser_config: DifyBrowserLayerConfig | None = None`;
   - build 逻辑:`include_browser` **不并入** `include_shell`(该变量同时驱动 shell spec 与 `tools.shell` 引用,不能污染);门控 `if include_shell or include_browser:` → 先 `layers.append(runtime_spec)`,再分别 append shell/browser spec;
   - browser spec = `RunLayerSpec(name="browser", config=(browser_config or DifyBrowserLayerConfig()).model_dump(mode="json"))`。
4. 双 `runtime_request_builder`(workflow `api/core/workflow/nodes/agent_v2/` + agent_app `api/core/app/apps/agent_app/`):
   - `include_browser = dify_config.AGENT_BROWSER_ENABLED and agent_soul.browser.enabled`(且 sandbox 后端支持);
   - 模块级 `build_browser_layer_config(agent_soul)`(仿 shell 版)。
5. `runtime_feature_manifest.py`:`SUPPORTED` 加 `"browser"`,`reserved_status["browser"] = "forwarded_to_browser_layer_config"`。
6. validators:browser 开启时要求 runtime/sandbox 可下发(与 shell 同条件)。
7. 控制台 UI:最小方案=直接改 Agent JSON;正式 UI 单独排期(配置页开关 + 参数表单)。

### 4.6 Step 6:模型兼容性与子代理加固(运维沉淀,强烈建议一并移植)

移植后真实运行会暴露模型侧问题,以下三项在本仓库已落地,属**同等必要**的移植件:

1. **工具调用格式(崩溃级)**:openai 兼容模型(如 Qwen3 经 vLLM)的模型凭证必须 `function_calling_type: "tool_call"`(现代 `tools` 字段)。旧版 `"function_call"`(`functions` 字段)下 vLLM 的 Qwen3 模板不启用 JSON 工具调用,模型返回空响应或把 XML 工具调用写成纯文本 → pydantic-ai `ToolRetryError: Please return text or call a tool` → 重试耗尽 → 整个 run 崩溃。诊断手段:在 agent_backend 前挂日志代理捕获插件真实请求体,确认用的是 `tools` 还是 `functions`。
2. **thinking-only 空响应重采样**:`EmptyResponseRetryModel`(包装 pydantic-ai Model,非流式响应无 text/tool_call 时原地重采样,默认 3 次)。thinking 模型偶发"只思考不干活",属采样抖动,重试可恢复。
3. **子代理输出重试预算**:`ResilientPydanticAIChatModel`(继承 harness `PydanticAIChatModel`,重建内部 Agent 设 `retries={"output": 3}`;父类默认仅 1 次)。**注意**:harness 子代理经 `PydanticAIChatModel.ainvoke` → 内部 `Agent.run()` 是**非流式**,模型加固必须包在这一层;传 pydantic-ai `Model` 会被 `resolve_chat_model` 包成默认无预算的 `PydanticAIChatModel`,传 `BaseChatModel` 则原样透传——所以自定义子类直接作为 `llm` 传入 `BrowserUse`。
4. 主代理同样受益于 `create_agent(..., retries={"output": 3})`(thinking-only 主轮次多几次机会)。

接线(最终形态):

```python
llm=ResilientPydanticAIChatModel(EmptyResponseRetryModel(cast(Model, ctx.model)))
```

预算链:子代理每步 = 内部 Agent 输出重试 4 次 × 重采样 3 次 = 最多 12 次模型调用,再叠 browser-use `max_failures`(默认 5)步骤重试。

## 5. 配置字段(最终形态)

| 字段 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `runtime` | `str` | `"runtime"` | 模块引用,`_REFERENCES` 校验 |
| `max_steps` | `int` | `30` | 子代理步数上限,每步一次 LLM 调用(成本主旋钮) |
| `use_vision` | `bool\|'auto'` | `'auto'` | 截图按模型能力自动决定 |
| `headless` | `bool` | `True` | 沙箱内固定 headless |
| `allowed_domains` | `list[str]\|None` | `None` | 导航硬拦截(glob 可用);None=靠 squid+block_ip |
| `block_ip_addresses` | `bool` | `True` | 禁止 IP/localhost 导航 |
| `session_scope` | `'call'\|'agent'` | `'call'` | 每任务断连复用同一浏览器,并行安全 |
| `use_judge` | `bool` | `False` | 省一次 LLM 调用/任务 |
| `step_timeout_seconds` | `int` | `180` | 子代理单步超时 |
| `output_schema` | `dict\|None` | `None` | JSON Schema → 结构化返回(配置期编译) |
| `sensitive_data` | `dict\|None` | `None` | 密钥占位符,平铺值必须配非通配 `allowed_domains` |
| `persist_login_state` | `bool` | `False` | 跨 run 登录态(CDP dump/load state file) |
| `enabled` | `bool` | `False` | 每 agent 开关(API 侧) |

全局开关:`AGENT_BROWSER_ENABLED`(默认 False,灰度起步)。

## 6. 验证清单

**单元(不依赖活体,进 CI)**:
- 配置/State 校验、REGISTRY 引用、`port_entrypoint` 三后端矩阵;
- DevTools 输出行解析(纯函数,含噪音输入);
- CDP forwarder:Host 改写(含末行 header)、半包重组恰好一个分隔符、裸帧透传、上游不可达;
- `wrap_run` 资源管理:mock lease,异常路径也 kill job + aclose + 删资源;
- 子代理 mock:经 harness 文档化钩子 `browser_agent` 工厂注入 fake,验证 `browse_web` 工具链;
- 模型加固:重采样次数、重试预算、`UnexpectedModelBehavior` 抛出时机;
- API:下发/不下发矩阵(`include_browser × AGENT_BROWSER_ENABLED × browser.enabled`)、旧快照反序列化兼容。

**集成(opt-in,活体 sandbox)**:shellctl 容器起 job → CDP `/json/version` 经 forwarder 200 → `delete(force=True)` 后无 chromium 孤儿。

**E2E(手工清单)**:真实 lease → launcher → forwarder → `port_entrypoint` → browser-use 子代理(真实 LLM + 真实 Chromium)→ 返回页面内容 → job 删除零残留。P3 附加:run1 设 cookie → run2 启动即载 → agent 关全部 tab 后 watchdog 恢复 → 同 HOME 双并发互不干扰。

## 7. 移植踩坑清单(按发生顺序)

1. **DevTools 只绑 loopback**:`--remote-debugging-address=0.0.0.0` 被静默忽略 → in-job forwarder。
2. **Host 头校验**:DevTools 拒绝非 IP/localhost Host → forwarder 首请求改写;正则不强制尾随 CRLF(Host 常是末行)。
3. **半包重组重复分隔符**:`\r\n\r\n` 写两遍 → DevTools 直接断连;重组用 `pending[end+4:]`。
4. **stderr 不可见**:runner stdio 模式把 stderr 写独立文件 → launcher 必须 `2>&1`。
5. **SingletonLock**:共享 profile 目录第二实例 exit 21 → 每 job 唯一目录(job env 下发)。
6. **Landlock fail-closed**:`/dev/shm`、`/dev/dri` 被拒即崩/exit 125 → 两个 `--disable-*` 参数必须。
7. **SDK 硬钉冲突**:§2 的 uv overrides。
8. **import 回归**:browser-use 是可选 extra,模块注册必须 try-import 懒加载。
9. **cookie jar 整体重置**:该 Chromium 构建在某 context 失去最后一个 page target 时清空全 jar;launcher 首 tab 是 file:// wrapper 页,SecurityWatchdog attach 必关 → 关前预清理 + 保 about:blank + `_jar_watchdog` 事件驱动重放快照。
10. **模型工具调用格式**:§4.6-1(旧版 `functions` 字段 → 全 run 崩溃)。
11. **thinking-only 空响应**:§4.6-2/3(不加固则偶发 run 崩溃)。
12. **fake-IP 代理构建**:NLTK 自带 `pathsec` 拦截保留段 fake-IP(198.18.0.0/15)→ `docker build --add-host=<域名>:<真实IP>`。

## 8. 安全评审项

| 项 | 现状 | 评审要求 |
|---|---|---|
| `--no-sandbox` | 容器内非 root 无法启用 chromium sandbox | 边界=容器+Landlock,需安全签字 |
| SSRF | harness 拦 IP/localhost + squid 拒私网双保险;`allowed_domains` 可收窄 | 敏感场景必须配 `allowed_domains` |
| CDP forwarder 无鉴权 | 端口随机、仅存于需 Bearer 才可读的 job 输出、生命周期=job、容器网络不发布 | 评审若不够,加首请求 token 头(browser-use 侧需验证自定义头) |
| 不可信网页内容 | 保持 harness 默认 guidance 的 untrusted-content 规则 | 自定义 guidance 必须保留该规则 |
| 子代理成本 | 不受宿主 UsageLimits 约束(独立 Agent) | `max_steps` + `step_timeout` + `max_failures` + 宿主 run 超时 |

## 9. 附录:文件变更清单

**dify-agent**:
- `src/dify_agent/layers/browser/{__init__,configs,layer,output_schema,resilient_model}.py`(新增模块)
- `src/dify_agent/runtime/modules.py`(懒注册 + `_REFERENCES`)
- `src/dify_agent/runtime/context.py`(`RunResources.browsers`)
- `src/dify_agent/runtime_backend/{protocols,errors,shellctl,local,e2b,openshell,enterprise,__init__}.py`(`PortEntrypoint`/`port_entrypoint`)
- `src/dify_agent/runtime/agent_factory.py`(主代理 `retries={"output": 3}`)
- `src/dify_agent/adapters/llm/{__init__,empty_response_retry}.py`(重采样包装)
- `pyproject.toml`(harness extra + browser-use 钉版 + uv overrides)
- `tests/local/dify_agent/layers/browser/`、`tests/local/dify_agent/runtime/test_agent_factory.py`、`tests/local/dify_agent/adapters/llm/test_empty_response_retry.py`、`tests/local/dify_agent/runtime_backend/test_port_entrypoint.py` 等
- `docs/dify-agent/user-manual/browser-layer/index.md`

**api**:
- `models/agent_config_entities.py`(`AgentSoulBrowserConfig` + `AgentSoulConfig.browser`)
- `configs/extra/agent_backend_config.py`(`AGENT_BROWSER_ENABLED`)
- `clients/agent_backend/request_builder.py`(`include_browser`/`browser_config`/spec 下发)
- `core/workflow/nodes/agent_v2/{runtime_request_builder,runtime_feature_manifest,validators}.py`
- `core/app/apps/agent_app/runtime_request_builder.py`
- 对应 `tests/unit_tests/` 用例

**dify-agent-runtime**:
- `docker/Dockerfile`(chromium + 字体)
- `docker/sync-e2b-template.sh`(`DEFAULT_MEMORY_MB` 2048)

**docker**:`docker-compose.yaml` **无需改动**(local_sandbox 与 agent_backend 已在 `agent_sandbox_network`,squid 代理 env 已设)。
