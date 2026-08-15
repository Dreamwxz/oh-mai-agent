# 回复润色与发送

任务执行完，最终回复怎么送出去、以什么面目送出去，是最后一个值得设计的问题。本功能回答两条：回复要不要加工（LLM 润色 + 黑话匹配），以及加工后怎么可靠送达（两条发送出口 + 指数退避重试）。

**核心模块**：`executor/instant.py`（`ReplySender` 两条发送出口 + `PolishService` 润色 + 失败处理）+ `executor/splitter.py`（确定性回复分割器）。

## 设计目标

**为什么需要润色？** 任务的结果文本（intent 或工具返回）是给机器看的产物，直接甩到聊天流里口吻生硬、缺乏上下文，而回复质量直接决定用户对任务完成度的感知。润色层解决三个问题：

1. **回复质量**：拉取目标聊天流的近期消息作为上下文，让 LLM（`replyer` 模型）把结果改写成贴合当前对话风格的回复。
2. **黑话适配**：聊天流维护了黑话表（Jargon），机械匹配命中的黑话注入 system prompt，回复口吻能贴近群聊成员的习惯。
3. **跨流直发**：agent 任务的目标流（`task.reply_target`）不一定是发起流，回复要直发到正确的流，且允许回复"去往别处"后补上动机说明。

**为什么回复要独立成任务？** 回复本质是一次单步即时动作——润色 + 发送，正好落在 instant 任务模型上。把回复拆成独立的 INSTANT 任务落库入队，换来两个收益：一是进程崩溃后回复任务可恢复重发，消息不丢；二是回复路径与任务主体解耦，agent 循环结束即释放，发送由执行器统一兜底。这也意味着回复与任务主体共享同一套状态机、持久化与调度语义，不需要为"发一条消息"另造一套机制。

**为什么发送出口与上下文写入分离？** 回复链路存在两类完全不同的输出：**对用户可见的消息**（`ctx.send.text`）与**对用户不可见的上下文记录**（`ctx.maisaka.context.append`，写给 MaiBot / Planner 的认知层，让它们了解插件正在做什么）。两者语义不同，不应混在同一个"发送"接口里。因此 `ReplySender` 的约定是：**两条发送出口只负责对用户可见的发送**（分割 + 重试），不做任何 `context.append`；上下文写入（纯文本记录、XML 动机注释）是独立的显式能力，由需要的地方按场景调用——这是让 MaiBot / Planner 感知插件工作内容的关键通道，值得当一等公民对待。

**为什么润色绝不能阻塞发送？** 润色是体验增强而非功能主体。LLM 调用可能超时、模型可能不可用，若润色失败导致回复发不出去，任务结果就丢了。因此 PolishService 的设计底线是：任何异常都返回原始文本，润色只执行一次，不做重试。重试只覆盖发送步骤——发送是硬依赖，润色是软增强，两者的可靠性要求不同，处理方式也因此分开。

**为什么选中 instant 执行器？** 上面这些约束——单次执行、无 LLM 推理循环、无工具调用、意图即消息——恰好匹配 instant 任务的全部特征。它是最轻量的执行器：零等待、零并发控制，创建后立刻完成。把"润色 + 发送"收敛到 InstantExecutor 里，而不是散落在 agent 循环或命令处理器中，是为了让所有回复路径（agent 完成回复、即时任务回复、send_message 工具、失败通知）都复用同一段润色与重试逻辑，避免各路径各自实现导致行为漂移。

**可靠性 vs 延迟的权衡**。回复润色涉及三个步骤：拉取上下文（DB 查询）、LLM 润色（网络调用）、发送（API 调用）。每一步都可能失败，但失败的影响不同。设计上做了三层分级：上下文查询失败只打 warning，润色失败降级原文，只有发送失败值得重试。这个分级意味着：系统宁愿给用户一条未润色的原文，也不愿让用户多等几秒重试润色——因为等的是 LLM 调用（120s 超时），而用户等回复的耐心窗口比这短得多。

> 架构变更：v0.1.0 曾把 instant 任务迁到独立子进程（WorkerManager + StdioTransport），后因复杂度与收益不匹配回退。当前 InstantExecutor 在 Runner 进程内同步执行润色与发送。

## 设计方案

**回复的完整链路**。agent 任务完成后，AgentExecutor 的 `send_final` 回调默认就是 `TaskControl._dispatch_reply_instant()`（core/usecases/task_control.py:49-72）：把回复文本拆成一个新的 INSTANT reply 任务（`stream_id=task.reply_target`、经 `mark_as_reply()` 打标记，键 `META_IS_REPLY`）落库并交给调度器入队。调度器通过 `enqueue` 将任务加入 PENDING 队列，随后 `_try_dispatch` 检查并发额度，额度可用时 `transition(RUNNING)` 并 `create_task(InstantExecutor.execute(...))`。回复任务不经过 AgentLoop，不占用 agent 并发额度，走的是独立的 instant 执行通道。

`InstantExecutor.execute()` 是整个回复路径的终点：经 `exec_ctx.sender`（`ReplySender`）发送——`send_polished` 润色分割发送，跨流回复（`reply_stream_id` 或 `is_reply_task()`）再补 `append_motivation_note` 动机注释，随后判终态收尾。整个执行路径不跨进程，上下文经 `executor/context.py` 的 `current_task` ContextVar 传递。执行结果（COMPLETED / FAILED）通过 `scheduler.on_task_completed()` 回调通知调度器释放额度，不经过命令总线的事件分发——instant 任务没有 AgentLoop 那样的事件监听循环，终态通知是直调的。

**ReplySender：两条发送出口**。`ReplySender`（executor/instant.py）是统一发送出口，构造时注入 `ctx` / `config_getter`（每次读取，热更新生效）/ `prompt_service`：

- **出口1 `send_raw(text, stream_id)`**：直发。分割（跟随 `config.splitter`）+ 指数退避重试（`config.send.max_retries`），无润色、无上下文写入。用于**确定性文本**——命令回复、失败通知，内容不能被 LLM 改写。
- **出口2 `send_polished(text, stream_id, *, relay_from=None)`**：完整链路。信息获取（上下文/黑话，内聚在 `PolishService`）→ 润色 → 复用出口1的发送段。用于**任务回复、提问、send_message 工具**。`relay_from` 非空 = 转达他人之言（润色点名委托人），缺省 = 本人发言。

两个出口共享内部 `_split`（分割）与 `_send_segments`（重试），**但都不做任何 `context.append`**——发送出口只对用户可见。

**PolishService：黑话匹配评分**。`PolishService`（executor/instant.py）是润色核心，三件事：

1. `_load_context()` 拉取目标流最近消息（排除 bot 自己的消息），数量跟随 MaiBot 全局配置（群聊 `chat.max_context_size` 默认 40，私聊 `chat.max_private_context_size` 默认 60）；
2. `_match_jargons()` 机械子串匹配黑话——复刻 MaiBot `jargon_context_matcher` 的匹配与评分：`_jargon_in_scope` 按 `is_global` 或 `session_id_dict` 过滤黑话范围，`_calculate_match_score` 复刻高频词加权评分（命中高频词加 1000 分基数、按出现次数加权、按消息位置微调），取前 `MAX_JARGON_REFERENCE_MATCHES`（10）条；
3. 把上下文、黑话与原始结果交给 `prompt_service.build("polish")` 渲染系统提示词，`ctx.llm.generate(model="replyer", timeout_ms=120000)` 润色一次。任何异常记录 warning 后返回原文。

**机械匹配 vs 语义匹配**。黑话匹配采用机械子串匹配而非语义匹配（embedding 相似度），是 MaiBot 的既有设计。机械匹配的好处是确定性——命中了就是命中了，不会因为语义漂移误把无关词汇当成黑话注入。代价是匹配不到同义词或变形词（如"yyds"匹配不到"永远的神"）。系统不在此处做语义匹配，因为黑话匹配只是润色的辅助输入，不是润色的核心；匹配的多或寡在 LLM 的最终润色效果里会被稀释。`PolishService` 的 `use_jargon` 开关（默认 True）允许用户关闭黑话匹配，退回纯上下文润色。

**PolishService 的构造与提示词注入**。PolishService 构造时只接受 `ctx`、`config`（`PolishConfig`）与 `prompt_service`——`prompt_manager` 是传而不用、已被移除的冗余依赖。`polish()` 内部调用 `prompt_service.build("polish", ...)` 而非直接渲染模板——这是为了复用已有的 builder 注册机制，确保润色提示词也经过模板校验（`PromptManager.render` 的变量超集/子集检查）和 XML 转义。若 `prompt_service` 为 None，则由 `PolishBuilder` 兜底，但这只是一种容错回退，正常运行时永远不会触发。

**转达模式：自动判定 + 单一 `relay_from` 通道**。润色只有一种模式切换：**这条消息是本人发言还是转达他人之言**。`send_polished` 的 `relay_from` 参数承载这个判断——非空（如 `"张三"`）= 转达，`polish` 模板输出转达纪律块并点名委托人（"由 张三 委托"）；缺省 = 本人发言，不输出转达块。`relay_from` 由**自动转达判定**填充（`executor/instant.py` 的 `_resolve_relay`）：比对任务发起人（`task.owner`，创建入口拼的 `platform:user_id`）与目标私聊流用户（`chat.get_all_streams` 反查，宿主 session_id 为哈希无法直接解析），不同即转达、点名发起人昵称（`chat.get_stream_by_user_id` 反查，兜底 owner 原文）。两条注入路径：**任务回复**（`InstantExecutor`）与 **send_message 工具**（Agent 循环版经 `resolve_relay` 回调基于 `current_task` 判定）；Planner 版无任务上下文，一律本人发言。**群目标无单一传出用户，不自动转达**（避免"我自己转达我自己"的怪象）；判定失败/流未找到一律保守按本人发言处理。

**润色模板与主程序 replyer 对齐**。`polish.md` 的段落顺序刻意对齐 MaiBot 主程序回复生成提示词（`prompts/zh-CN/maisaka_replyer.prompt`）：人格设定（对应 `{identity}`，取自主程序 `[personality].personality`）→ 表达风格（对应 `{reply_style}`，取自主程序 `[personality].reply_style`）→ 参考信息软约束（对应"你可以参考【回复信息参考】中的信息，但是视情况而定，不用完全遵守"）→ 注意事项位 → 输入材料 → 输出指令（结尾对应 `replyer_output_instruction` 的中文文案）。人格、表达风格与机器人昵称（`[bot].nickname`，缺省"麦麦"）在 `PolishService.polish()` 每次经 `ctx.config.get` 读取主程序配置，热更新即时生效；未配置（空串）时模板不输出人格/风格节，保留默认人格行（以 `{{bot_name}}` 命名，兜底"麦麦"）。两处无法与主程序一致的插件特有内容单独安放：一是**转达纪律块放在注意事项位**——对应主程序 `group_chat_attention_block`（"在该聊天中的注意事项"）的位置，两者都是"本条消息的场景级纪律，优先于一般润色要求"；二是上下文、黑话表与原始结果集中为【输入材料】节（主程序 replyer 的聊天记录经自身机制注入，此处以显式材料呈现）。

**send_raw / send_polished：分割 + 逐段指数退避重试**。发送阶段是双层循环（`_send_segments`）：外层遍历分段，内层 `for attempt in range(max_retries)` 对每段指数退避重试——每次尝试调用 `ctx.send.text(segment, stream_id)`，成功即进入下一段；失败时按 `2^attempt` 指数退避 sleep，间隔 1s → 2s，最多 `config.send.max_retries`（默认 3）次。**任一段重试耗尽即停止发送后续分段**并抛出最后一个异常交由上层标记 FAILED（已发出的分段保留）。退避间隔短，是因为回复是用户等待中的即时消息，不值得为一次发送等待过久；重试次数有限，是因为发送失败大概率是目标流本身的问题，重试再多也是徒劳。重试次数已从硬编码下沉到 `[send]` 配置，热更新即时生效。

**回复分割：确定性切分器**。`executor/splitter.py` 的 `split_message()` 复刻 MaiBot `response_splitter` 的切分规则（MaiBot `split_into_sentences_w_remove_punctuation` 与 `merge_sentences_to_max_count`），但改为**确定性算法**并保留原文：连续换行归一化后，按换行（即"按行分割"）、句末标点（。！？!?；;）切分，逗号/空格按守卫条件软切分——成对引号内部一律不切（保护"他说：'你好，世界'"）、中英文冒号旁边不切、空格两侧为字母/数字时不切（"hello world"不断开）、破折号旁边不切。切分点标点随左侧片段保留，`"".join(分段)` 与归一化原文完全一致，不丢任何内容。随后贪心打包：每段尽量不超过 `max_length`（默认 1000 字符），段数达到 `max_messages`（默认 5）上限后尾部合并进最后一条（复刻 MaiBot `merge_sentences_to_max_count` 思路）；无标点的超长句（如长代码块）按 `max_length` 硬切兜底——与 MaiBot 超长时返回默认回复"呃呃"不同，任务回复**绝不丢内容**。短文本（≤ `max_length`）原样单条发送，行为与未开启分割时完全一致。分割行为统一跟随 `[splitter]` 配置，两条出口都不再暴露 `split` 参数。

**重试循环的边界**。重试只覆盖 `ctx.send.text` 这一行。一旦发送成功，函数立即返回，不再关注后续任何写入——发送是硬性主路径，上下文写入是附带的显式能力。

**静默掉包检测**。MaiBot SDK 的 `ctx.send.text()` 在底层发送失败但不抛异常时可能返回 `False` 或 `None`（如 API 返回空响应但状态码 200）。`_send_segments` 每次尝试后显式检查返回值，检测到即转成 `RuntimeError` 触发重试。如实说明：这是对 SDK 边界行为的**防御性检测**，不是系统设计特性——若全部 `max_retries` 次重试都遇到掉包，消息确实发不出去，任务最终标记 FAILED。检测逻辑封装在发送出口内部，外部调用方（InstantExecutor、send_message 工具、Planner 的 send_message API）无需自行处理。

**上下文注释：独立能力**。`ReplySender.append_motivation_note(stream_id, content)` 是对用户不可见的上下文写入：用 `context_note` 模板渲染任务动机生成 XML 标签，写入目标流，让从别处发来的任务结果在聊天上下文里可追溯。判断条件由调用方决定——`InstantExecutor.execute()` 对跨流回复（`task.reply_stream_id` 或 `is_reply_task()`）调用它，覆盖了通过 `_dispatch_reply_instant` 创建的回复任务。send_message 工具（`tools/send_message.py`）也保留自己的上下文记录（纯文本 + XML `sent-message`），显式调用 `ctx.maisaka.context.append`——同样是"发送出口纯发送、上下文写入由工具层显式负责"的体现。

**成功与失败的终态收尾**。发送成功后，`execute()` 重新 `store.get` 判终态（防并发终态覆盖），非终态才走 `complete_and_notify`（executor/base.py:114-118）：`transition(COMPLETED)`（竞争时 `force` 兜底）→ 落库 → `scheduler.on_task_completed()` 释放并发额度。异常路径走 `fail_task()`（executor/instant.py）：先做双重终态守卫——本地 `is_terminal()` 直接返回，再重载持久化记录检查（防进程内快照过期）——随后可选 `send_message` 先发"任务执行失败"通知（**走 `send_raw` 直发出口**：错误文本是确定性内容，不应被润色改写；失败通知本身发送失败也不影响状态更新，异常静默吞掉），再 `transition(FAILED)`、被状态机拒绝时 `force(FAILED)` 兜底，落盘并通知调度器。终态守卫的意义在于：任务可能已被调度器超时判 FAILED 或用户取消，执行器不能用一个过期的进程内快照覆盖并发终态。`fail_task` 的 `send_message` 可选特性也说明：失败通知不是必须的——`execute()` 异常路径总是 `send_message=True`，但其他调用方（如 AgentLoop 的异常处理）可以选择不发消息，只悄悄地标记 FAILED。

**调用路径汇总**。`ReplySender` 的两条出口是所有回复的统一入口，调用方按语义选择：

1. **instant 任务**：`InstantExecutor.execute()` 走 `send_polished`（+ 跨流时 `append_motivation_note`），这是最主流的路径。
2. **agent 任务完成回复**：经 `_dispatch_reply_instant` 拆成新的 instant 任务，最终仍落回路径 1。
3. **send_message 工具**：Agent 循环内主动发消息，回调绑定 `send_polished`（`relay_from` 由注入的自动转达判定填充）。
4. **失败通知**：`fail_task(send_message=True)` 走 `send_raw` 直发"任务执行失败"消息。

四类调用共享同一套分割、指数退避重试与静默掉包检测，这保证了发送可靠性的行为一致；其中 1/2/3 另共享 PolishService 润色。

**与 ask_user 的边界**。ask_user 提问消息走 `send_polished`（`TaskManager._ask_callback`）——提问默认润色（项目的优势是"更像人"，提问同样以聊天口吻呈现），并获得与回复相同的重试、掉包检测与分割保障。唯一区别是提问不创建 instant 任务：它是 Agent 循环内的双向交互（等待用户回复后继续），而非任务结束后的单向推送。详见 [AI 提问](./07-ask-user.md)。

**命令响应的例外**。命令响应（`cmd_reply`，见 [命令](./13-commands.md)）走 **`send_raw` 直发出口**——命令是排障/操作场景，任务 ID、状态等关键信息不能被 LLM 改写，且不写入 MaiBot 上下文（命令响应是操作回显，无需进入认知层）。可靠性保障（重试、掉包检测、分割）与润色消息一致，区别只在是否经过 LLM 与上下文。

## 使用与配置

**配置项**。`[polish]` 节有一个开关（config.py）：

| 字段 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `use_jargon` | `bool` | `True` | 润色时是否机械匹配黑话注入 system prompt |

`use_jargon` 是取舍开关：开启则润色提示词里带上命中黑话的释义，回复更贴近群聊口吻，但黑话匹配本身有 DB 查询开销和注入噪声；关闭则退回纯上下文润色，行为更可预测。大多数用户保持默认即可。

**回复分割配置**。`[splitter]` 节控制长回复的分割行为：

| 字段 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `enable` | `bool` | `True` | 是否把长回复拆成多条消息发送 |
| `max_length` | `int` | `1000` | 单条消息目标最大长度（字符），无标点的超长句会被硬切 |
| `max_messages` | `int` | `5` | 一次回复最多拆成几条消息，超过时尾部合并进最后一条 |

分割只影响**发送形态**，不改变内容（`"".join(分段) == 原文`）。短回复（≤ `max_length`）不受影响，仍单条发送。分割统一跟随全局配置，不再提供按消息覆盖参数。

**发送配置**。`[send]` 节控制发送重试：

| 字段 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `max_retries` | `int` | `3` | 消息发送最大重试次数（指数退避 1s→2s），超过后放弃发送并抛异常 |

**面向使用者的回复路径**。普通用户不需要关心润色细节，只需知道：任务完成后，最终回复会自动经过润色、分割与逐段重试后直发到目标聊天流。通过 `send_message` 工具（Agent 循环内或 Planner 侧）主动发消息时，同样自动走润色与分割；发送对象不是任务发起人时，系统自动按转达处理并点名委托人（Planner 版无任务上下文，按本人发言）。回复是单向推送，用户无需操作；唯一特殊的是 ask_user 提问（见 [AI 提问](./07-ask-user.md)），那是需要用户回应的双向交互。

**硬编码参数**（不在配置中暴露，修改需改代码）：

| 参数 | 默认值 | 来源 |
|---|---|---|
| 群聊上下文条数 | 40 | `chat.max_context_size`（MaiBot 全局） |
| 私聊上下文条数 | 60 | `chat.max_private_context_size`（MaiBot 全局） |
| 黑话注入上限 | 10 | `MAX_JARGON_REFERENCE_MATCHES` |
| 重试间隔 | 1s → 2s | `2^attempt` 硬编码 |
| LLM 润色超时 | 120s | `timeout_ms=120000` |

**已知限制**：

- **静默掉包不是特性**：`False/None` 检测仅覆盖经 `ReplySender` 的发送路径；外部直接调用 `ctx.send.text()` 而不检查返回值，仍可能面临消息静默未发送。这是 MaiBot SDK 的边界行为，不是系统能修复的。
- **重试只覆盖发送、不覆盖润色**：LLM 偶发失败（超时、模型不可用）时用户收到的是未润色原文。这是有意取舍——润色不该成为发送的阻塞点。如果将来需要润色重试，应该在 PolishService 的 `except` 块内加重试逻辑，但需要权衡：润色重试会让用户等更久，而回复延迟的体验伤害可能大于不润色。
- **崩溃恢复可能重复发送**：回复以独立 PENDING instant 任务落库，若发送完成后、落盘前崩溃，重启后任务重新执行，同一条消息可能发两次。这是持久化事务边界的固有权衡，无去重机制，详见 [持久化与恢复](./03-persistence-recovery.md)。
- **失败通知本身可能失败**：`fail_task(send_message=True)` 先发失败消息，若目标流本身有问题，异常被吞掉，用户可能收不到失败通知。这是权衡后的选择：宁可漏发失败通知，也不让发送失败阻塞任务状态更新。
- **硬编码参数不可配置**：黑话注入上限 10、重试间隔公式 `2^attempt`、润色超时 120s 均硬编码在代码常量中，不在配置节暴露。改动这些参数需要改代码，无法通过 WebUI 配置。
- **分段发送是尽力语义**：某段重试耗尽时停止后续分段并抛异常，任务标记 FAILED 并发送失败通知；此前已发出的分段保留在目标流中，用户可能收到"部分回复 + 失败通知"。这是与 MaiBot 一致的"首段失败即停"策略，权衡是避免在目标流不可用时继续刷屏。
- **命令响应不写入上下文**：`send_raw` 直发的命令响应不写 `context.append`，MaiBot 上下文看不到命令回显——这是"发送出口纯发送"原则的代价，命令响应属于操作回显而非认知内容。

**相关文档**：[任务模型](./01-task-model.md)（instant 级任务模型与状态机）、[持久化与恢复](./03-persistence-recovery.md)（回复任务的恢复语义）、[AI 提问](./07-ask-user.md)（提问路径）、[命令总线](./11-command-bus.md)（任务执行期的命令/事件交互）。
