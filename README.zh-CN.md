# Hermes 上下文管家

[English](README.md) · [上架说明](PUBLISHING.md) · [MIT 许可证](LICENSE)

纯 Python 上下文引擎插件，不修改 Hermes 核心。上下文达到模型窗口的检查线
（默认 50%）时，使用一次独立的辅助模型调用，同时裁决模式并生成交接摘要。
用户仍在同一个聊天窗口，旧消息由 Hermes 归档保存，插件不删除历史、不启动服务。

设计参考 [DS Bot README 中的 context steward](https://github.com/FeiZhuLulu/DeepSeek-Bot/blob/main/ds-bot/README.md)，
为 Hermes 独立实现，不依赖 DS Bot 代码。

## 工作方式

| 裁决 | 新上下文 |
| --- | --- |
| `checkpoint` | 系统指令、结构化交接摘要（目标、已完成、进行中、文件/命令/决定、待办）、最近 N 条用户消息原文，以及带有完整工具调用/结果的保护尾部。 |
| `fresh` | 系统指令、简短交接条、最近 N 条用户消息原文、最后一条已完成的助手回复。去掉其余旧助手/工具上下文，避免把已回答的用户要求重新执行。 |

若 `context.steward` 未显式配置 `check_ratio`，默认自动沿用当前 profile 的 `compression.threshold`，两者皆无时为 `0.5`。

如果摘要仍列有进行中事项或待办，或者最后一条消息不是已完成的助手回复
（例如工具轮次或尚未回答的用户消息），`fresh` 会降为 `checkpoint`。

每条交接载体（`checkpoint`、`fresh`，以及机械兜底压缩）末尾都固定带一句插件自有提示：
早期对话原文已归档在会话历史中，摘要里没有的细节（原话、路径、数字、命令、报错、
早前工具输出）必须先用 `session_search` 查原文，不得猜。该提示由插件在脱敏之后拼接，
不依赖辅助模型输出；多次压缩始终只保留一份（重拼前先清掉旧副本，且不会作为
上次摘要回灌给模型）。
默认逐字保留最近 3 条真实用户消息，包括空格和换行；带有 diagnostic 等展示标记的真实用户请求均受保护，已知系统内部恢复提示和历史交接条不算真实用户消息。

裁决通过 Hermes 的插件 LLM 门面（`ctx.llm`；目录安装时插件自建同一个
`agent.plugin_llm` 对象）发起，路由、凭据、超时和重试都归宿主所有。默认走会话当前
使用的模型；若想改走更便宜的 `auxiliary.compression` 槽位，需要显式授权：

```yaml
plugins:
  entries:
    context-steward:
      llm:
        allow_task_override: true   # 裁决改走 auxiliary.compression
```

`verdict_model` 同样需要 `allow_model_override: true` 才会生效，否则以宿主路由为准。
没有硬编码 provider、endpoint 或 key。输入包含较早消息的均匀抽样、最近 24 条消息、
上次摘要和可选记忆；长消息有明确省略标记，不会把百万 token 原始会话全部发出。
进程重启或恢复会话后，会从消息中的摘要载体单独恢复完整上次交接条，不混入普通抽样。

## 自选压缩模型

两个可选配置块，分别控制两个阶段；不写则完全沿用 Hermes 的 `auxiliary.compression`。

```yaml
context:
  engine: context-steward
  steward:
    verdict:                    # 裁决 checkpoint / fresh
      provider: magpie
      model: opendesign/deepseek-v4.1-flash
    summary:                    # 写交接摘要
      provider: magpie
      model: opendesign/glm-5.3-flash
```

- **只写一个块**：用作唯一一次合并调用。
- **两块相同**：仍是一 次调用。
- **两块不同**（provider 或 model）：两次调用——小模型只裁决模式，另一个写摘要。
- 也接受字符串简写：`verdict_model: 某模型`（旧写法）或 `verdict: 某模型`。

插件**不自己调 provider**：两个模型都由 Hermes 解析、鉴权和重试。因此还需要操作者授权，
且名字必须存在于该 profile 自己的 provider 配置里：

```yaml
plugins:
  entries:
    context-steward:
      llm:
        allow_provider_override: true
        allowed_providers: [magpie]
        allow_model_override: true
        allowed_models: [opendesign/deepseek-v4.1-flash, opendesign/glm-5.3-flash]
```

`base_url` 与 `api_key_env` 为与其他插件配置对称而保留，但 **Hermes 公开的插件 LLM 接口
不支持**：它只接受 provider 名称，不接受自定 endpoint/密钥。填写时会在日志中明确警告并
忽略。想用自己的端点，请在 `config.yaml` 里声明为 custom provider（由它拥有 URL 和密钥），
再用 provider 名字引用。

非法配置不静默：格式错误（未知键、只写 provider 不写 model、类型不对、`api_key_env`/
`base_url` 格式错）会在加载时报错，由 Hermes 记录原因并回退到内置压缩器；provider 不存在
或操作者未授权时，会记录一条指向具体配置键的 warning，并回退到下一条允许的路由
（aux `compression` → 会话模型）。

要求返回结构化 JSON，具体格式见英文 README。`handoff` 必须包含：

- `goal`：非空字符串。
- `completed`：已完成事项字符串列表。
- `in_progress`：进行中事项字符串列表。
- `key_context`：文件、命令、关键决定及原因字符串列表。
- `todo`：待办字符串列表。

模式无效但摘要完整时按 `checkpoint` 处理。JSON 解析失败、结构不完整、响应为空、超时或
任何传输错误，都降级为**机械 checkpoint**：Hermes 照常归档旧内容，最近用户原文与
保护尾部原样保留，交接条只写能确定的内容（未完成请求 + 从原文机械提取的路径/命令/报错），
不编造、不静默丢弃。若连机械兜底也无法缩小上下文，或保留的用户原文/尾部会变，
则原样返回输入。

## 兼容范围

已在**干净上游代码**（无本地补丁）上验证：v0.23.0 `2bd378ed1b8d6bf2192cb26e0a55ff3091e6bd19`
与 `main` `90d6b98f`（2026-11-06）。两次均把干净源码放在 `PYTHONPATH` 最前、只复用宿主 venv：
64 个测试全部通过，`hermes plugins validate` 通过且**无任何 warning**：
插件只 import 文档化模块（`agent.context_engine`、`agent.plugin_llm`、`hermes_cli.config`），
不继承、不引用 Hermes 内部压缩器。

另在隔离测试 bot 上做过一次真跑：压缩后追问一个只存在于归档原文的细节，模型确实调用了
`session_search` 并答对。这只是单模型单环境的结果，不保证所有模型都照做。

声明最低版本 `>=0.23.0`，未来升级需要复验。没有额外 Python 依赖。

## 安装与启用（由操作者执行）

以下仅提供操作说明，开发与测试不会自动安装或启用插件。
把仓库内容完整放到 `$HERMES_HOME/plugins/context-steward/`，默认目录为
`~/.hermes/plugins/context-steward/`，保证 `__init__.py`、`steward.py` 和 `plugin.yaml`
位于该目录根部。目录名必须是 `context-steward`。不需要 pip 安装。

在对应 profile 的 `config.yaml` 中合并以下内容，保留已有其他配置：

```yaml
context:
  engine: context-steward
  steward:
    check_ratio: 0.5
    keep_user_messages: 3
    verdict_model: ""  # 可选；需下方 allow_model_override 授权才生效
```

目录型上下文引擎不要求加入 `plugins.enabled`。同一时间只能选择一个上下文引擎。
配置在 Hermes 下一次正常初始化 agent 时加载；已有 agent 可能继续持有原来的引擎。
恢复默认行为时，将 `context.engine` 改为 `compressor`。

| 配置键（`context.steward` 下） | 默认 | 含义 |
| --- | --- | --- |
| `check_ratio` | `0.5` | 严格介于 0 和 1 之间的有限数，按完整模型窗口计算；无 75% 小窗口下限。 |
| `keep_user_messages` | `3` | 正整数，两种模式均保留最近多少条真实用户原文。 |
| `verdict_model` | `""` | 可选裁决模型名；需 `plugins.entries.context-steward.llm.allow_model_override: true` 才生效。 |

拼错键、非法值会让插件加载失败，不会偷偷用其他值代替。
`compression.model_thresholds` 不覆盖管家的检查线。
如果宿主传入输出 token 预留，检查线会限制在可用输入窗口以内。
`compression.enabled` 仍决定是否自动压缩；手动 `/compress` 同样经过本插件，并传入关注主题。
请保持 `compression.prepare_ahead` 和微压缩关闭（Hermes 默认值）；继承的这些功能不经过管家裁决。

## 隐私与存储

- 网络出口是 Hermes 已配置的 compression 辅助模型，会收到最近消息和抽样历史。
- 配置通过 Hermes API 读取，不直接读取 `.env`、`auth.json`、钥匙串或其他凭据文件。
- 发给裁决模型的文本、生成的交接摘要均强制脱敏；对凭据赋值另外保守屏蔽。
  用户原文保留在独立消息中，不属于生成的摘要，因此其中用户自己提供的敏感内容仍会保留。
- 脱敏依赖凭据形态识别，无法保证识别没有任何标签的任意秘密或模型改写后的秘密。
  不要在聊天中粘贴凭据。插件自身的警告不记录裁决正文或 provider 异常原文；继承的父类诊断遵循 Hermes 日志策略。
- 不创建插件数据库、不收集遥测、不执行 shell、不运行后台任务或服务。
  正常历史归档和检索由 Hermes 管理。测试会实际使用项目内临时 SQLite 库及全文搜索。

## 运行测试和校验

在本仓库执行：

```bash
~/.hermes/hermes-agent/venv/bin/python scripts/check.py
```

其他安装位置可以传 `--hermes-root /path/to/hermes`。
只校验插件时加 `--validate-only`。
脚本先运行单元测试，再执行真实的 Hermes `validate_plugin_dir`，包括隔离注册探测、
安全扫描和禁止覆写核心检查。所有可变状态放在项目内 `.test-hermes/` 和 `.tmp/`，
关闭 bytecode 和懒安装，不安装依赖、不写用户配置、不启动服务。

**真跑部分**：Python 测试、真实 ContextEngine 契约、插件 LLM 门面、配置加载、目录加载器、
真实 SQLite 归档与 FTS、官方准入校验（子进程）。
**模拟部分**：裁决模型的响应（用替身顶替 `ctx.llm`），本套测试不发起任何 provider 调用。

官方校验通过且无 warning：插件不 import 任何内部进口。

## 已知限制

摘要是有损压缩，抽样可能遗漏早期细节，需要时查 Hermes 历史库。
检查线与状态计数复用 Hermes；实际 usage 到来前和候选大小对比使用粗略 token 估算。
最近用户原文或一个工具轮次特别大时，可能无法压到检查线以下，此时保留原上下文。
多模态消息只在保护尾部内原样保留，不做历史图片占位改写；若保留的用户原文或尾部会变，
整个候选被拒绝。相比内置压缩器，本插件不跑内置的主动工具结果裁剪，也不会在摘要失败后
换第二个模型重试（改为机械 checkpoint）；作为补偿，它自己保证工具调用/结果配对并清理孤儿。

`fresh` 是同一聊天中的逻辑上下文新段，不创建新 UI 窗口、不模拟 `/reset`。
宿主决定提交时是在原会话归档还是轮换会话，生命周期和记忆仍由 Hermes 管理。
引擎隐藏自动成功状态提示，但宿主警告与手动 `/compress` 的输出仍可能显示。
Hermes 的压缩检查可能发生在 preflight 或工具循环中，并不保证只在完整回合结束后发生。

插件只用文档化接口（ContextEngine 契约 + `agent.plugin_llm`）；摘要载体的前缀与结束
标记是复制文本（测试断言与宿主当前常量一致）。若上游改动这两段文字，需要同步复验。
市场上架还需要公开仓库、精确 commit pin 和维护者人工批准；本项目未发布、未提交市场。
