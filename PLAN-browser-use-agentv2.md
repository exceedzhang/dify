# pydantic-ai-harness `BrowserUse` × AgentV2 集成计划

> 状态:**P0 已完成(2026-10-31,全部验收通过,结论见 §2.2)**,P1 已完成(2026-10-31,代码+单测+真实容器集成验证全过),**P2 已完成(2026-10-31,API 配置链+单测+全栈 E2E 全过)** | 基于 #43833 合并后的原生 Pydantic AI 模块架构
> 已复核至本地 HEAD `caa64dd017`(较初稿 `cc14bb83e0` 前进 430 个 commit,§2.1 为增量复核结论)
> 前一版方案(Agenton `DifyBrowserLayer(PydanticAILayer)` 路线)因 #43833 删除 Agenton 而作废,本文为更新版。
> P0 实测发现 chromium 156 DevTools 仅绑 loopback,架构已相应调整(§3/§4.1,引入 in-job CDP forwarder)。

## 1. 背景与目标

给 AgentV2 agent 增加默认 `browse_web` 能力:由 pydantic-ai-harness 的 `BrowserUse` capability 提供单个
`browse_web(task: str) -> str` 工具,内部 browser-use 子代理通过 CDP 驱动真实 Chromium 执行多步网页任务
(填表、抓取、登录态操作等),返回文本/结构化结果。

约束与目标:

- LLM 密钥不出 agent_backend:子代理复用宿主模型(`ctx.model`,推理仍走 plugin daemon),sandbox 内不持有任何密钥。
- 浏览器进程跑在 sandbox 容器(local_sandbox / E2B),复用 shellctl job 生命周期与 Landlock 隔离。
- SSRF 双保险:harness `block_ip_addresses=True`(拦 IP/localhost)+ sandbox 出站经 squid 代理(拒私网)。
- 零 DB 迁移:`AgentSoulConfig` 加带默认值字段,旧快照/旧配置保持有效。
- 默认关闭,全局开关 + 每 agent 开关,灰度可控。

## 2. 现状评估(#43833 合并后)

| #43833 前的 v1 假设 | 合并后现状(已逐文件核实) | 对本方案的影响 |
|---|---|---|
| Agenton `PydanticAILayer` + `tools()` 注入 | Agenton 已删除。新模块契约:`Config(BaseModel)` + `State(BaseModel)` + **恰好一个** `Capability(AbstractCapability[Deps])` 或 `Toolset(AbstractToolset[Deps])`,注册于 `dify-agent/src/dify_agent/runtime/modules.py:33` 的 `REGISTRY`,API 以 `RunLayerSpec{name, config}`(composition v2)传 JSON 配置,`State` 进 snapshot v2 | 按新契约实现 `browser` 模块,用 **Capability**(而非 Toolset)以获得 `wrap_run` 生命周期 |
| Agenton `on_context_create/delete` 管理浏览器进程 | Capability `wrap_run(ctx, handler)` 包裹整个 run;`ctx.deps.resources` 为 run 级资源表(shell 模块同款模式,`layers/shell/layer.py`) | 浏览器 job 启停完全套用 shell 模板,零 Agenton 概念 |
| `tools()` 直接返回 harness `get_toolset()` | 实测 pydantic-ai 2.31.0:capability 的 `get_toolset()`/`get_instructions()` 在 **Agent 构造期**调用(`agent/__init__.py:746/761`),早于 `wrap_run` —— 此时 CDP 地址未知 | 工具必须**惰性**解析 CDP:自建 `FunctionToolset` 工具,调用时才从 `ctx.deps.resources` 取 session |
| 未细化模型传递 | `RunContext.model: AbstractModel` 存在(`pydantic_ai/_run_context.py:61-66`) | `wrap_run` 内 `BrowserUse(llm=ctx.model, ...)` 直接复用主模型 |
| 未关注 CDP 注入时机 | 实测 harness 0.20.x:`BrowserUseToolset._build_session()` 在**每次工具调用时**读 `self._cdp_url`(`_toolset.py:413`);`cdp_url` attach 语义 = "结束时只断连不杀进程(browser-use 只杀自己启动的浏览器)" | sandbox 宿主浏览器 + agent_backend attach 是 harness 原生支持的设计;CDP 地址可在 wrap_run 注入 |
| — | `pydantic-ai-harness[browser-use]` extra **当前未安装**(import 即 `ImportError`) | `dify-agent/pyproject.toml` 需加 extra 并钉 0.20.x |

### 2.1 增量复核(`cc14bb83e0` → HEAD `caa64dd017`,430 commits)

| 变更 | 位置 | 对本方案的影响 |
|---|---|---|
| 新模块 `knowledge_fs` 落地(KnowledgeFS) | `layers/knowledge_fs/`、`modules.py` REGISTRY/`_REFERENCES`、`Services.knowledge_session_store`、`load_modules` 服务缺失校验 | **最新模块集成先例**(与 shell 并列的模板):API 字段 → `request_builder` 输入字段 + `_knowledge_fs_spec()` helper → REGISTRY + references → 可选的服务依赖校验。browser 模块完全套用同一模式 |
| **Landlock 改为 fail-closed**(#44152) | `cmd/runner/main.go:312-313`:Restrict 失败即 `exit 125`,脚本不执行;Linux ≥5.13 内核自动启用(`SHELLCTL_ENABLE_PATH_ISOLATION` 可显式关) | **P0 阻断项升级**:chromium 作为 job 子进程继承 Landlock,其全部文件访问必须落在策略内(§4.5);`--disable-dev-shm-usage`(策略拒绝 `/dev/shm`)与 `--disable-gpu`(拒绝 `/dev/dri`)从"建议"变**必须** |
| shellctl 本地鉴权硬化 | `--require-auth-token`(local 部署必须配 `SHELLCTL_AUTH_TOKEN`;E2B/Enterprise 共享镜像默认关) | 无影响;`port_entrypoint` 与 lease 协议(`runtime_backend/protocols.py`)未变 |
| 应用级 tracing | `runner.py` +106(`application_observability`、`_finalize_application_tracing`)、execution_context 加 `tracing` 字段、API `application_tracing_hooks` | 无影响;模块加载/Agent 构造流程未变 |
| shell 层加 KnowledgeFS 钩子 | `layers/shell/layer.py` +90(`knowledge_observation`/`knowledge_lease_lost`/`BindingLostError`) | 纯增量;`Capability.wrap_run`/`ShellSession`/工具结构不变,模板依旧可用 |
| API:KnowledgeFS 配置链 | `agent_config_entities.py` +`AgentSoulKnowledgeConfig.spaces`/`AgentKnowledgeSpaceConfig`;`request_builder.py` 加 `knowledge_fs` 输入与 `DIFY_KNOWLEDGE_FS_LAYER_ID`,runtime+shell 层 spec 重构为单个 `layers.extend([runtime, shell])` 块(line 305-306/443-444);feature manifest knowledge 状态细分 | browser 层追加位置随该重构微调(§4.4),模式不变 |
| 镜像基座 | runtime 阶段 `python:3.12-slim` → **`python:3.13-slim-bookworm`**、Node 24.20.0 → 24.22.1 | 仍是 Debian 12 bookworm → apt `chromium` 版本不变;Dockerfile 改动点不变 |
| E2B 2.55.1(快照/回放改进) | `runtime_backend/e2b.py` | 无影响;`get_host(port)` 用法仍在 `e2b.py:361` |

**结论:模块契约、`RuntimeLease` 协议、composition v2、snapshot v2 均无破坏性变更,原方案全部有效;需同步 §4.4/§4.5 的落点与 Landlock 阻断项。**

### 2.2 P0 实测结论(2026-10-31,本地 arm64 + `python:3.13-slim-bookworm` 镜像,chromium 156.0.8078.73)

| 验收项 | 结果 |
|---|---|
| fail-closed Landlock 下 chromium 真实 job 启动 | **通过**。job(默认策略)内 chromium 完整起来(zygote/gpu/network/storage/renderer 子进程);被拒访问(dbus `/run/dbus`、OOM score 调整、cpuinfo)均非致命;无 exit 125 |
| 页面加载 | **通过**。`--dump-dom http://example.com` → `<title>Example Domain</title>`,exit 0(容器直连出站 OK) |
| job stdout 捕获 DevTools 行 | **通过,但有前提**:runner 的 stdio 模式将 stderr **单独**写入 `stderr.log`(API 不可见)→ launcher **必须** `2>&1`;加上后 `read_output` 可见 `DevTools listening on ws://127.0.0.1:PORT/devtools/browser/<id>` |
| 跨容器 CDP 可达(agent_backend 视角) | **通过,但架构调整**:`--remote-debugging-address=0.0.0.0` 在 chromium 156 **被静默忽略**(DevTools 只绑 127.0.0.1,新版反 DNS-rebinding 加固);且 DevTools HTTP 层**拒绝非 IP/localhost 的 Host 头**("Host header is specified and is not an IP address or localhost.")→ 解法 = **in-job CDP forwarder**(job 内 python 子进程,0.0.0.0:随机端口 → 127.0.0.1:cdp_port,首请求 Host 头改写为 `localhost`);实测同 docker network 第二容器经 forwarder 访问 `/json/version` 200,**WS 握手 101 + `Browser.getVersion` CDP 往返成功** |
| job 终止清理 | **通过,无需兜底**。`POST /v1/jobs/{id}/terminate` 后 tmux kill-session 带走 chromium **全部**子进程(含 detach 的 crashpad handler),`ps` 零残留;并发多 job 互不串扰 |
| 并发 profile 冲突 | **新发现**:共享 `--user-data-dir` 时第二实例 exit 21(`SingletonLock: File exists`),`$PWD` 对所有 job 都是 `$HOME` 不唯一 → **必须每 job 独立 profile 目录**,经 job `env` overlay 下发(runner 不注入 job_id 环境变量),如 `BROWSER_PROFILE_DIR=$HOME/.browser/<run-uuid>` |
| 镜像体积 | 1.35GB(基线 `dify-agent-local-sandbox:1.18.0` 567MB,**+783MB**,chromium+依赖+CJK 字体) |
| E2B 内存 | `sync-e2b-template.sh` `DEFAULT_MEMORY_MB` 1024→**2048** 已改(E2B 资源由模板固定,`AsyncSandbox.create` 不按次传内存;影响 = 全部 E2B 沙箱内存翻倍,成本决策点见 §9) |

## 3. 总体架构

```
api  AgentSoulConfig.browser + AGENT_BROWSER_ENABLED(全局开关)
 └─ clients/agent_backend/request_builder.py
     include_browser → RunLayerSpec{name:"browser", config:{...}}(composition v2)
 └─ agent_backend /runs
     runtime/modules.py  REGISTRY["browser"] → load_modules 校验 Config/State/引用
  └─ browser 模块 Capability(AbstractCapability[Deps])
      ├─ get_ordering: requires/wrapped_by RuntimeCapability(需要 RuntimeLease)
      ├─ wrap_run:借 lease → shellctl job 运行 launcher(python 编排脚本)
      │           ├─ launcher 启动 headless chromium(2>&1,profile 经 job env 下发)
      │           ├─ 解析 "DevTools listening on ws://127.0.0.1:PORT/..."(chromium≥141 只绑 loopback)
      │           ├─ 起 in-job CDP forwarder(0.0.0.0:随机端口 → 127.0.0.1:PORT,
      │           │    首请求 Host 头改写 localhost;chromium DevTools 拒绝非 IP Host)
      │           ├─ job stdout 打印 "CDP_READY ws://0.0.0.0:FWD_PORT/devtools/browser/<id>"
      │           └─ wrap_run 经 read_output 轮询 CDP_READY
      │           → RuntimeLease.port_entrypoint(FWD_PORT) 拼 CDP ws URL
      │           → 构造 harness BrowserUse(cdp_url=..., llm=ctx.model, **cfg)
      │           → ctx.deps.resources.browsers[name]
      ├─ get_toolset:FunctionToolset{ browse_web(ctx, task) }   ← 惰性解析,名称与 harness 一致
      └─ get_instructions:透传 harness 静态 guidance(含 untrusted-content 安全规则)
  浏览器进程:local_sandbox / E2B 容器内,headless chromium,
             出站经 squid(拒私网);job 结束 tmux kill-session 连带回收
             chromium 与 forwarder 全部子进程(已实测零残留)
 ```

要点:浏览器**不在** agent_backend 进程内,agent_backend 只是 CDP 客户端;
LLM 密钥全程留在 agent_backend 侧(plugin daemon 通道)。

## 4. 详细设计

### 4.1 agent_backend:新 `browser` 模块

位置:`dify-agent/src/dify_agent/layers/browser/`(`configs.py` + `layer.py` + `__init__.py`)。

**`configs.py` — `DifyBrowserLayerConfig(BaseModel)`**(API 直接 `from dify_agent.layers.browser import ...`,与 shell 的 `DifyShellLayerConfig` 同模式,不重复定义):

| 字段 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `runtime` | `str` | `"runtime"` | 模块引用,`_REFERENCES` 校验(同 shell → runtime 模式) |
| `max_steps` | `int` | `30` | 子代理步数上限,每步一次 LLM 调用 |
| `use_vision` | `bool \| Literal['auto']` | `'auto'` | 截图是否进模型 |
| `headless` | `bool` | `True` | 沙箱内固定 headless |
| `allowed_domains` | `list[str] \| None` | `None` | 导航硬拦截(glob 可用);None=不限制,靠 squid+block_ip |
| `block_ip_addresses` | `bool` | `True` | 禁止 IP/localhost 导航 |
| `session_scope` | `Literal['call','agent']` | `'call'` | 每任务断连复用同一浏览器,并行安全 |
| `use_judge` | `bool` | `False` | 省一次 LLM 调用/任务 |
| `step_timeout_seconds` | `int` | `180` | 子代理单步超时 |

**`layer.py` — `class Capability(AbstractCapability[Deps])`**,以 `layers/shell/layer.py` 为模板(最新先例:`layers/knowledge_fs/layer.py`,同为 `Config/State/Capability` 三导出,`get_ordering()` 以**其他模块的 Capability 类型**声明依赖,如 knowledge_fs 声明 `wrapped_by/requires=[ShellCapability]`):

- `__init__(self, name)`;导出 `__all__ = ["Config", "State", "Capability", "DifyBrowserLayerConfig", ...]`。
- `Config = DifyBrowserLayerConfig`。
- `State(BaseModel)` **留空**:snapshot v2 只存 State;resume 后原浏览器 job 已死,
  `wrap_run` 重新拉起即可;登录态由 `$HOME/.browser-profile` 持久化(workspace home 复用),语义简单可靠。
- `get_ordering()` → `CapabilityOrdering(wrapped_by=[RuntimeCapability], requires=[RuntimeCapability])`。
- `get_instructions()` → 透传 harness capability 的静态 guidance。**禁止**配置 `guidance=''`
  (默认 guidance 含 untrusted-content 安全规则,网页内容不可信)。
- `get_toolset()`:返回**自有** `FunctionToolset[Deps](id=self.name, sequential=True)`,
  单工具 `@toolset.tool async def browse_web(self, ctx: RunContext[Deps], task: str) -> str`。
  工具体:从 `ctx.deps.resources.browsers[self.name]` 取 session(必然晚于 `wrap_run`),
  `return await session.harness_toolset.browse_web(task)`。
  —— 不依赖 harness 私有字段,对 `get_toolset()` 构造期调用不敏感(时序鲁棒)。
- `wrap_run(ctx, *, handler)`:
  1. `lease = ctx.deps.resources.leases[config.runtime]`(缺失即错误;API 侧保证 runtime 层存在);
  2. `job = await lease.commands.run(launcher_script, mode="stdio", env={"BROWSER_PROFILE_DIR":
     "<home>/.browser/<run-uuid>", ...}, ...)`(脚本见下;profile 目录**必须每 job 唯一**,
     共享目录第二实例 exit 21 SingletonLock 冲突,已实测);
  3. 轮询 `wait(job_id, offset)` / `read_output(job_id, offset)` 直到输出出现
     `CDP_READY ws://0.0.0.0:FWD_PORT/devtools/browser/<id>`(launcher 打印,含 forwarder 端口;
     带超时,失败则 kill job 并报错);
  4. `entry = lease.port_entrypoint(FWD_PORT)`(新增 API,见 4.2),以 entry 的 host 替换
     `0.0.0.0` 拼完整 CDP ws URL(**host 必须是容器 IP/可达名**;DevTools 经 forwarder 后
     Host 头已被改写为 `localhost`,不再受 chromium 的 Host 校验限制);
  5. `harness = BrowserUse(cdp_url=ws_url, llm=ctx.model, max_steps=..., use_vision=...,
     allowed_domains=..., block_ip_addresses=..., session_scope=...,
     agent_settings=BrowserAgentSettings(use_judge=False, step_timeout=...))`;
     取 `harness.get_toolset()` 存 `BrowserSession{name, job_id, harness_toolset, aclose}` 到
     `ctx.deps.resources.browsers[self.name]`(新字段);
  6. `await handler()`;
  7. `finally`:kill job(经 lease.commands 的 kill/interrupt,shell 层 `_delete_tracked_jobs_best_effort` 同款;
     **已实测** tmux kill-session 连带回收 chromium+forwarder 全部子进程,零残留)+
     `await harness.aclose()`('call' scope 下为 no-op,防御性调用)+ `del ctx.deps.resources.browsers[self.name]`。

**launcher 脚本(P0 各组件已实测,orchestration 为 P1 交付物)**:单进程 python 编排
(镜像自带 python3.13,不新增依赖),内联字符串不新增 sandbox 文件:

```python
import asyncio, os, re, sys

PROFILE = os.environ["BROWSER_PROFILE_DIR"]          # job env overlay 下发,每 job 唯一
CHROME = ["chromium", "--headless=new", "--no-sandbox", "--disable-gpu",
          "--disable-dev-shm-usage", "--remote-debugging-port=0",
          "--user-data-dir", PROFILE]
# 代理:job 环境已有 HTTP_PROXY(local_sandbox=squid,E2B 无→直连),chromium 自动读取

async def main():
    proc = await asyncio.create_subprocess_exec(
        *CHROME, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    fwd = None
    rx = re.compile(rb"DevTools listening on ws://127\.0\.0\.1:(\d+)(/\S+)")
    while True:
        line = await proc.stdout.readline()          # chromium 2>&1 合流(实测必需)
        if not line:
            break
        out = sys.stdout.buffer; out.write(line); out.flush()   # 透传给 job output
        if fwd is None:
            m = rx.search(line)
            if m:
                srv = await asyncio.start_server(
                    make_forwarder_handler(int(m.group(1))), "0.0.0.0", 0)
                fwd = srv.sockets[0].getsockname()[1]
                out.write(b"CDP_READY ws://0.0.0.0:%d%s\n" % (fwd, m.group(2)))
                out.flush()
    rc = await proc.wait()
    if fwd is not None:
        pass  # CDP_READY 已打印;chromium 死后 forwarder 由本进程退出连带结束
    sys.exit(rc or 0)

asyncio.run(main())
```

`make_forwarder_handler(up_port)`:P0 实测版本(约 60 行,纯 asyncio):双向 TCP 管道 +
**首请求 Host 头改写**(`(?im)^Host:[^\r\n]*\r\n` → `Host: localhost\r\n`,缓冲至
`\r\n\r\n` 处理半包,兼容 WS 握手后续裸帧透传)。已存为 P1 实现基准。

参数理由(实测更新):

- `--remote-debugging-port=0`:自动选端口,共享容器多 run 并发不冲突;**`--remote-debugging-address=0.0.0.0` 已删除**
  (chromium ≥141 静默忽略,DevTools 只绑 loopback —— 跨容器由 in-job forwarder 解决);
- `2>&1` / `stderr=STDOUT`:**必须**。runner stdio 模式将 stderr 单独写 `stderr.log`(API 不可见),
  DevTools 行在 stderr,不合流则 `read_output` 永远看不到;
- `--disable-dev-shm-usage`:**必须** —— Landlock 策略整体拒绝 `/dev/shm`(EACCES 即崩),
  且 Docker 默认 shm 仅 64MB;改走 TMPDIR=job cwd(Landlock RW 区,同时绕开被拒的 `/tmp`);
- `--disable-gpu`:**必须**(`/dev/dri` 不在 Landlock 策略内);
- `--user-data-dir=$BROWSER_PROFILE_DIR`:**必须每 job 唯一**(job env 下发,
  `$HOME/.browser/<run-uuid>`;$PWD 对所有 job 都是 $HOME 不可用);
  跨 run 登录态持久化因 SingletonLock 互斥,降级为 P3 议题;
- `--no-sandbox`:容器内非 root 无法启用 chromium 自带 sandbox;安全边界=容器+Landlock,需安全评审确认;
- 代理:job 环境读取 `HTTP_PROXY` —— compose 已为 `local_sandbox` 设置 `http://agent_ssrf_proxy:3128`(走 squid),
  E2B 无该变量则直连。**无需 API 侧传代理**,部署形态自动适配。

### 4.2 `RuntimeLease.port_entrypoint(port: int) -> PortEntrypoint`(新增 Protocol 方法)

> **实施偏差(已落地)**:返回类型从 `str` 升级为 `PortEntrypoint(base_url: str, headers: dict[str, str])`
> 冻结 dataclass(`runtime_backend/protocols.py`)。原因:E2B 沙箱以 `allow_public_traffic: False`
> 创建,**所有**端口流量(含 CDP)必须携带 `e2b-traffic-access-token` 请求头,纯 host:port 字符串
> 无法表达该凭证。`headers` 默认为空 dict,local 场景无影响。

- local(`runtime_backend/local.py`):从已存 `endpoint`(如 `http://local_sandbox:5004`)用
  `urlsplit(...).hostname` 推导 host,返回 `PortEntrypoint("http://local_sandbox:<port>")`
  (两服务同在 `agent_sandbox_network`,任意端口可达);
- e2b(`E2BRuntimeLease.port_entrypoint`):`https://{sandbox.get_host(port)}` +
  `headers={"e2b-traffic-access-token": <token>}`(同款用法已存在于 `e2b.py:361`);
  缺 token 时抛 `BindingAcquireError`(与 shellctl 通道同口径)。
- openshell / enterprise:抛 `PortEntrypointUnsupportedError`(新增,`RuntimeBackendError` 子类)
  → 该后端下 browser 特性不可用(API 侧按 provider 决定是否下发 layer)。
- 共享实现:`ShellctlRuntimeLease` 增加 `port_entrypoint_factory: Callable[[int], PortEntrypoint] | None`
  字段(经 `create_shellctl_lease`/`create_owned_shellctl_lease` 透传),无 factory 时抛
  `PortEntrypointUnsupportedError`;local 的 binding lease 直接就是 `ShellctlRuntimeLease`,
  factory 由 `LocalExecutionBindingBackend._port_entrypoint` 提供。

### 4.3 其他 agent_backend 改动

- `runtime/modules.py`:`REGISTRY` 注册 `"browser": browser`;`_REFERENCES["browser"] = {"runtime": runtime}`;
- `runtime/context.py`:`RunResources` 新增 `browsers: dict[str, BrowserSession] = field(default_factory=dict)`;
- `pyproject.toml`:`pydantic-ai-harness[browser-use]>=0.20,<0.21`(extra 拉入 browser-use + patchright 等;镜像体积变化在 P0 评估)。

### 4.4 API 侧

1. `api/models/agent_config_entities.py`:
- 新增 `AgentSoulBrowserConfig(AgentFlexibleConfig)`:字段同 4.1 配置表 + `enabled: bool = False`;
- `AgentSoulConfig` 新增 `browser: AgentSoulBrowserConfig = Field(default_factory=AgentSoulBrowserConfig)`
(`extra="forbid"` 模型加带默认值字段,旧快照/旧配置向后兼容,无迁移;
先例:同文件 KnowledgeFS 的 `AgentSoulKnowledgeConfig.spaces` 即如此增量添加)。
2. `api/configs/extra/agent_backend_config.py`:新增 `AGENT_BROWSER_ENABLED: bool = False`
(全局 kill-switch,与 `AGENT_SHELL_ENABLED`(line 65)同模式)。
3. `api/clients/agent_backend/request_builder.py`(行号为当前 HEAD):
- 常量 `BROWSER_LAYER_ID = "browser"`;
- `AgentBackendWorkflowNodeRunInput`(line ~207)/ `AgentBackendAgentAppRunInput`(line ~246,extra=forbid)各加
`include_browser: bool = False` + `browser_config: DifyBrowserLayerConfig | None = None`;
- build 逻辑(两个 build 方法,line 305-306 / 443-444):现状为
`include_shell = run_input.include_shell or run_input.config_layer_config is not None` 后
`if include_shell: layers.extend([runtime_spec, shell_spec])`。改动:
  - 新增局部 `include_browser = run_input.include_browser`(browser **不**依赖 shell,不并入 `include_shell`
    —— 该变量同时驱动 shell spec 与 `tools.shell` 引用(line 359/499),不能污染);
  - 门控改为 `if include_shell or include_browser:` → 先 `layers.append(runtime_spec)`,
    再 `if include_shell: layers.append(shell_spec)`;
  - 块后追加 `if include_browser: layers.append(RunLayerSpec(name=BROWSER_LAYER_ID,
    config=(run_input.browser_config or DifyBrowserLayerConfig()).model_dump(mode="json")))`。
  (对照先例:KnowledgeFS 的 `_knowledge_fs_spec()` helper,line 57-66 —— 它因需要 shell 而强制
  `include_shell`,browser 无此约束。)
4. `api/core/workflow/nodes/agent_v2/runtime_request_builder.py`(line 290 附近)与
`api/core/app/apps/agent_app/runtime_request_builder.py`(line 216 附近):
- `include_browser=dify_config.AGENT_BROWSER_ENABLED and agent_soul.browser.enabled`(且 sandbox 可用);
- 新增模块级 `build_browser_layer_config(agent_soul)`(仿 `build_shell_layer_config`,workflow 侧 line 750)。
5. `runtime_feature_manifest.py`:`SUPPORTED` 集合加 `"browser"`;
`reserved_status["browser"] = "forwarded_to_browser_layer_config"`(同 sandbox 处理;
该文件近期已按 knowledge 细分状态,风格参照)。
6. validators:browser 开启时要求 runtime/sandbox 可下发(与 shell 同条件)。
7. 控制台 UI(可选,建议 P2 同 PR 或独立):AgentV2 配置页 browser 开关与参数;最小方案 = 直接改 Agent JSON。

### 4.5 sandbox / 部署

- `dify-agent-runtime/docker/Dockerfile`(runtime 阶段,当前基座 `python:3.13-slim-bookworm`,
Node 24.22.1;仍为 Debian 12,apt `chromium` 版本不受基座升级影响):
`apt-get install chromium fonts-liberation fonts-noto-cjk fonts-noto-color-emoji`
(chromium 装于 `/usr`,Landlock RO+exec 区,可执行;CJK 字体避免中文页面豆腐块)。
- **Landlock 策略核对(已对照 `internal/landlock/config.go` 实码,fail-closed 后为 P0 阻断项)**:
  策略范围 = RW:`$HOME`、job cwd(TMPDIR 亦指 cwd);RO+exec:`/usr /bin /sbin /lib /lib64 /etc /proc
  /opt/dify-agent-tools /opt/homebrew /snap`;RW 设备:`/dev/{null,zero,urandom,random,tty}`。
  chromium 访问推演:
  - `/usr/bin/chromium` + 依赖库 + `/usr/share/fonts` → RO+exec ✓;
  - profile `$HOME/.browser-profile` → RW ✓;
  - `--disable-dev-shm-usage` → **必须**(策略**拒绝** `/dev/shm`,chromium 默认用它放 IPC/共享内存,
    EACCES 会导致崩溃;重定向到 TMPDIR=cwd 后落 RW ✓);
  - `--disable-gpu` → **必须**(`/dev/dri` 不在策略内);
  - 其余被拒访问(`/var/log`、`/run/dbus`、`/dev/dri`)对 headless 均非致命;
  - 部署级逃生阀:镜像无需重建,可用 env `SHELLCTL_LANDLOCK_RO_PATHS`/`SHELLCTL_LANDLOCK_RW_PATHS`
    (逗号分隔)追加路径;`SHELLCTL_ENABLE_PATH_ISOLATION=false` 可整体关闭(不推荐,仅应急)。
  - #44152 后 Landlock **setup** 失败 = job exit 125 且不执行脚本(此前仅告警)——
    故 chromium 启动是否被策略卡死必须在 P0 用真实 job 验证(见 §8)。
- **需实机验证**:`tmux kill-session` 能否带走 chromium 全部子进程(多进程架构),
  兜底方案 = launcher 外层包 `bash -c '... ; pkill -f'` 或 job 结束钩子。
- E2B:同镜像经 `dify-agent-runtime/docker/sync-e2b-template.sh` 同步(`sync-e2b-template-dev/prod` Makefile 目标);
`MEMORY_MB` 1024 → 2048(headless chromium 单实例约 300-500MB)。E2B 2.55.1 的快照/回放改进与本方案无耦合。
- `docker/docker-compose.yaml`:**无需改动**(local_sandbox 与 agent_backend 已在 `agent_sandbox_network`,
代理 env 已设置)。

## 5. 默认值与理由

| 项 | 默认 | 理由 |
|---|---|---|
| `AGENT_BROWSER_ENABLED` / `browser.enabled` | `False` | 每 step 一次 LLM 调用,成本不可忽略;灰度起步 |
| `max_steps` | 30(harness 默认 50) | 成本/有效性平衡 |
| `use_vision` | `'auto'` | 截图按模型能力自动决定,省图像 token |
| `use_judge` | `False`(harness 默认 True) | 省一次 LLM 调用/任务 |
| `session_scope` | `'call'` | 并行安全;attach 模式下浏览器常驻由 job 管理 |
| `allowed_domains` | `None` | 靠 squid(拒私网)+ `block_ip_addresses` 双保险;收窄域留给敏感场景 |
| `guidance` | harness 默认 | 保留 untrusted-content 安全规则,不可置空 |

## 6. 文件变更清单

| 文件 | 变更 |
|---|---|
| `dify-agent/src/dify_agent/layers/browser/{__init__,configs,layer}.py` | ✅ 新增模块(P1) |
| `dify-agent/src/dify_agent/runtime/modules.py` | ✅ REGISTRY + `_REFERENCES`(P1) |
| `dify-agent/src/dify_agent/runtime/context.py` | ✅ `RunResources.browsers`(P1) |
| `dify-agent/src/dify_agent/runtime_backend/{protocols,errors,shellctl,local,e2b,openshell,enterprise,__init__}.py` | ✅ `PortEntrypoint` + `port_entrypoint`(P1,含 `PortEntrypointUnsupportedError`) |
| `dify-agent/pyproject.toml` | ✅ `pydantic-ai-harness[browser-use]` + `browser-use>=0.13.6,<0.13.10`(server extra)+ `[tool.uv] override-dependencies`(见 §9-11)(P1) |
| `dify-agent/tests/local/...` | ✅ 新增单测 33 项(P1,见 §7;含 forwarder 5 项) |
| `dify-agent/docs/dify-agent/...` | user-manual browser 模块页 + index |
| `api/models/agent_config_entities.py` | `AgentSoulBrowserConfig` + `AgentSoulConfig.browser` |
| `api/configs/extra/agent_backend_config.py` | `AGENT_BROWSER_ENABLED` |
| `api/clients/agent_backend/request_builder.py` | `include_browser` + runtime 层条件 |
| `api/core/workflow/nodes/agent_v2/runtime_request_builder.py` | `include_browser` + `build_browser_layer_config` |
| `api/core/app/apps/agent_app/runtime_request_builder.py` | 同上 |
| `api/core/workflow/nodes/agent_v2/runtime_feature_manifest.py` | `browser` 条目 |
| `api/tests/...` | request_builder / manifest / 兼容性单测 |
| `dify-agent-runtime/docker/Dockerfile` | ✅ chromium + fonts(P0 已改,已构建验证) |
| `dify-agent-runtime/docker/sync-e2b-template.sh` | ✅ `DEFAULT_MEMORY_MB` 1024→2048(P0 已改,成本决策见 §9-10) |

## 7. 测试策略

1. **agent_backend 单元(`dify-agent/tests/local/`,不依赖活体)**:
   - `DifyBrowserLayerConfig`/`State` 校验;REGISTRY/`_REFERENCES` 接线(引用缺失报错);
   - `port_entrypoint`:local 解析、e2b mock、openshell/enterprise 抛 unsupported;
    - DevTools 输出行解析(纯函数:从输出抽取 ws URL/端口,含多行噪音输入);
    - `wrap_run` 资源管理:mock `RuntimeLease`/`ShellCommandProtocol`,验证 job kill + `aclose` + 资源删除在异常路径也执行;
    - CDP forwarder(`_BROWSER_FORWARDER_SOURCE` 供单测 exec):`_rewrite_host` 改写 Host(含 Host 为末行 header)、`_pump` 单包/跨包重组后**恰好一个** `\r\n\r\n` 分隔符 + 裸帧透传、上游不可达时关 client。
2. **子代理 mock**:harness `browser_agent` 工厂是文档化测试钩子("substitute a fake in tests"),
   单测注入 fake 子代理验证 `browse_web` 工具链路,无需真实 chromium/LLM。
3. **集成(opt-in,需活体 sandbox,不进默认 CI)✅ P1 已跑通(2026-10-31,本地 arm64)**:起 shellctl 容器 → 启动 job →
   断言 CDP `/json/version` 经 forwarder 可达(200)→ `delete(force=True)` 后断言无 chromium 孤儿进程(10→0)。
   WS 握手/`Browser.getVersion` 已在 P0 跨容器验证,集成中因 slim 镜像无 `websockets` 降级为跳过。
4. **API 单元**:request_builder 的 browser layer 下发/不下发矩阵(include_browser × AGENT_BROWSER_ENABLED × browser.enabled)、
   runtime 层条件(`include_browser` 单独开启时 runtime 层存在)、feature manifest、
   `AgentSoulConfig` 旧快照(无 `browser` 字段)反序列化默认值兼容。
5. **手工 e2e 清单 ✅ 核心链已验证(2026-10-31)**:driver 容器与 sandbox 同 Docker network(等价 compose 拓扑),
   真实 lease→launcher→forwarder→`port_entrypoint`→browser-use 子代理(真实 LLM+chromium)→`browse_web` 返回
   example.com 页面内容→run 结束 chromium 0 残留。**约束**:agent 须能路由到 forwarder 端口(同网络/发布端口;
   macOS host 直连容器临时端口不可达,ws 503)。**剩余(可选)**:docker compose 全栈 + SSE 事件 + 控制台 UI(P3)。
6. 文档:dify-agent user-manual 新页 + `dify-agent/AGENTS.md` 如有模块清单则更新。

## 8. 分期实施

| 期 | 范围 | 交付/验收 |
|---|---|---|
| **P0 sandbox 镜像** ✅ 已完成(2026-10-31) | Dockerfile chromium+fonts(已改);E2B `DEFAULT_MEMORY_MB` 2048(已改);kill-session 清理验证;镜像体积评估(1.35GB,+783MB) | 全部通过:Landlock 下启动✓、页面加载✓、stdout 捕获✓(需 2>&1)、跨容器 CDP+WS✓(需 in-job forwarder,见 §2.2)、terminate 零残留✓(无需孤儿兜底)。**遗留**:amd64 平台构建验证(本地仅 arm64 实测,Debian chromium 两平台同源,风险低) |
| **P1 agent_backend 模块** ✅ 已完成(2026-10-31) | `layers/browser/`(含 launcher 编排 + CDP forwarder)、`PortEntrypoint`/`port_entrypoint`、`RunResources.browsers`、REGISTRY、pyproject extra(+uv overrides,§9-11)、单测 33 项 | 单测全绿(1057 passed/13 skipped,0 回归);`load_modules` 接受 browser layer ✓;typecheck 0 错、lint 通过 ✓。**真实容器集成验证通过**:launcher job 起 chromium✓、`CDP_READY` 解析✓、forwarder 改写 Host 后 `/json/version` 200✓、`delete(force=True)` 零残留✓(chromium 10→0)。集成中揪出并修复 forwarder 两 bug:①`_pump` 半包重组把 `\r\n\r\n` 分隔符重复写两遍→DevTools 直接断连(改 `pending[end+4:]`);②`_rewrite_host` 要求 Host 行带尾随 CRLF,当 Host 是末行 header 时漏改写(正则去掉强制 `\r\n`,只替换值)。forwarder 源码抽为 `_BROWSER_FORWARDER_SOURCE` 供单测 exec,5 项新单测覆盖 |
| **P2 API 配置链 + E2E** ✅ 已完成(2026-10-31) | `AgentSoulBrowserConfig`+`AgentSoulConfig.browser`、`AGENT_BROWSER_ENABLED`(默认关)、双 `runtime_request_builder`(workflow+agent_app)+`build_browser_layer_config`、request_builder(双 RunInput `include_browser`/`browser_config`)、feature manifest `browser`→`forwarded_to_browser_layer_config`、`_LOCKED_AGENT_SOUL_KEYS`+`browser`、API 单测;`dify-agent` 模块懒加载(无 browser-use 时优雅降级,修 P1 在 API venv 的 import 回归);用户手册 `browser-layer/index.md`+mkdocs 注册 | API 单测全绿(7205 passed,0 回归);dify-agent 1057 passed/13 skipped、typecheck 0 错、lint 通过 ✓。**全栈 E2E 通过**:driver 容器与 sandbox 同 Docker network(真实部署拓扑),真实 lease→launcher job→in-job CDP forwarder→`port_entrypoint`→browser-use 子代理(真实 LLM+真实 headless Chromium)→`browse_web` 返回 example.com 页面内容→job 删除零残留(chromium 0)。**关键约束(非 bug)**:`port_entrypoint` 复用 endpoint host,故 agent 必须能路由到 forwarder 端口——同 Docker network ✓;从 macOS host 直连容器内临时端口 ✗(无路由,表现为 ws 503),须走共享网络或发布端口 |
| **P3 增强**(按需) ✅ 后端四项已完成(2026-10-31),控制台 UI 单独排期 | ①`persist_login_state` 跨 run 登录态(CDP setCookies/getCookies 经 forwarder 由 layer 进程读写 sandbox 内 state file);②local 共享容器 browser 并发上限(`BrowserSessionSlots`+`browser_max_concurrent_sessions`,超限 fail-fast 429 语义);③`output_schema` 结构化返回(JSON Schema→pydantic);④`sensitive_data`(透传 harness 规则校验) | dify-agent 1081 passed/0 回归、typecheck 0 错、lint 通过;API 单测全绿(7205 passed,P3 字段 entity+builder 映射全覆盖)。**全栈 E2E 通过(10/10)**:run1 设 cookie→shutdown 经 CDP dump state file→run2 同 binding 启动即载 cookie(httpbin 回显 token)→jar 跨 run 保持→**run3 agent 主动关全部 tab→watchdog 即时恢复→最终访问仍见 cookie**→同 HOME 双并发 run 互不干扰(XDG_CONFIG_HOME 隔离进程单例)→零 chromium 残留。**关键发现(已修复)**:此 Chromium 构建在**某 browser context 失去最后一个 page target 时重置整个 session cookie jar**;launcher 首 tab 是 profile 目录的 file:// URL(chromium wrapper `--load-extension=` 空参把 profile 路径当启动 URL 解析),browser-use SecurityWatchdog attach 时必关该 tab→载入的 cookie 被连带清空。修复 ①`_prepare_page_targets`:load 前关非 web 首 tab 并保证留一个 about:blank。修复 ②`_jar_watchdog`:独立 CDP 连接经 `Target.*` 事件按 context 追踪 page target,某 context 失去最后一个 page 时立即重放 `Storage.setCookies` 快照(快照每 2s 刷新且仅保留非空 jar;全局无 page 时另建 landing tab);消费端永不 cancel 挂起的 `Queue.get`(曾与 2s 超时竞态丢失 wipe 事件),refresh/stop 改由 refresher 任务投哨兵项。shutdown teardown guard:jar dump 为空而 state file 有 cookie 时不覆盖。**遗留**:镜像侧根治需修 wrapper 空参(P0 资产,另行处理) |

## 9. 风险与决策点

1. **默认开关**:建议 `AGENT_BROWSER_ENABLED=false` 起步,验证成本/质量后灰度打开。
2. **`--no-sandbox` 安全评审**:容器内非 root 无法启用 chromium 自带 sandbox;边界=容器+Landlock,需评审签字。
3. ~~**Landlock fail-closed(#44152)**~~ → **P0 已验证通过**(2026-10-31):默认策略 job 内 chromium 完整启动、
页面加载成功,被拒访问(dbus/OOM score/cpuinfo)均非致命;`--disable-dev-shm-usage`/`--disable-gpu` 为必须项。
残余 = amd64 平台未实测(本地 arm64;Debian chromium 两平台同源,风险低)。
4. **CDP forwarder 无鉴权**(新,P0 引入):forwarder 绑容器内 0.0.0.0 随机端口,任何能到达 sandbox 容器网络的
进程(当前=agent_backend 等 compose 服务)知道端口即可驱动该浏览器。缓解事实:端口随机、仅存在于
**需 Bearer token 才能读取**的 job 输出中、生命周期=job(秒~分钟级)、容器网络不对外发布。
若评审认为不够,P3 可加首请求 token 头校验(browser-use 侧需能发自定义头,需验证)。
5. **local 共享容器内存**:单容器 N 并发 run = N chromium(各 300-500MB,实测 RSS 含 renderer 约 300-500MB),
当前无上限;P3 加 job 级并发上限。
6. **harness 0.x API 漂移**:钉 0.20.x;自有 toolset 包装已隔离其私有 API,影响面收敛在 browser 模块。
7. **SSRF**:harness 拦 IP/localhost + squid 拒私网双保险;CDP ws 连接走 agent_backend↔sandbox 内网,不受导航拦截影响。
8. **不可信网页内容**:保持 harness 默认 guidance;若未来支持自定义 guidance,必须保留 untrusted-content 规则。
9. **子代理成本不受宿主 `UsageLimits` 约束**(独立 Agent 实例):控制手段 = `max_steps` + `step_timeout` + `max_failures`(harness 默认 5)+ 宿主 1h run 超时。
10. **E2B 内存全局翻倍**(P0 已实施):模板固定资源、`AsyncSandbox.create` 不按次传内存,
`DEFAULT_MEMORY_MB` 1024→2048 影响**所有** E2B 沙箱(2× 成本)。若成本敏感,回退选项 =
保持 1024 + browser 开启的部署单独 `--memory-mb 2048` 同步模板(两模板名区分)。
11. **browser-use 与 dify-agent 的 LLM SDK 硬钉冲突**(P1 已解决):browser-use 0.13.6–0.13.9 硬钉
`anthropic==0.76.0`/`google-auth==2.48.0`/`google-genai==1.65.0`/`openai==2.26.0`,与
`pydantic-ai-slim[anthropic,google,openai]==2.31.0` 的最低版本(anthropic≥0.108/google-genai≥1.70/
openai≥2.45)直接矛盾;0.13.10+ 又要求 pydantic 2.13,超出 dify-agent 的 `pydantic<2.13`。
解决:server extra 钉 `browser-use>=0.13.6,<0.13.10` + `[tool.uv] override-dependencies` 抬升
四个 SDK 到 pydantic-ai-slim 要求的下限。**安全性依据**:Dify 恒传宿主 Pydantic AI 模型
(`llm=ctx.model`),browser-use 从不实例化自己的 provider client,被 override 的 SDK 仅存
import 面;实测 `browser_use` 全量 import + `BrowserUse` 构造 + `get_toolset()`/`get_instructions()`
在同步后环境全部通过。残余风险 = 未来 browser-use 在我们会走的代码路径(CDP/浏览器,不涉 LLM SDK)
用到旧 SDK API;版本范围 `<0.13.10` 限制漂移。
