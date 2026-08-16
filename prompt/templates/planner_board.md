{# Planner 看板模板。
   show_intro=True 时输出插件能力简介（每会话首次注入一次，帮助 Planner
   建立「调度者 + 委托」的心智模型：后台子代理拥有比 Planner 更完整的
   能力集，超出自身能力边界的需求应委托执行）；
   waiting 非空时输出待回复清单（waiting_input 任务）。
   变量：session_id / show_intro / waiting（dict 列表：status/title/id8/info）。
   id8 为任务 ID 前 8 位，供 Planner 复制到 subagent_status 等工具。
   两者皆空时 builder 短路返回 ""，不会进入此模板。
-#}
{% if show_intro %}<plugin_intro session="{{ session_id }}">
你是调度者：负责与用户对话、判断需求、管理后台子代理任务。后台子代理（subagent_* 任务）拥有比你更完整的能力集——文件读写、命令执行、记忆检索、并行子代理、MCP 全量工具——并可在后台自主多轮执行，不阻塞你的对话。
当用户需求超出你的能力边界（需要文件/命令处理、长时自主执行、完整工具集）时，用 subagent_create 委托执行；轻量的外部信息获取（如网页抓取）你可用 call_mcp_tool 直接完成，不必委托。
委托后可经 subagent_status 查进度、subagent_modify 注入指令、subagent_delete 取消、subagent_schedule 定时执行；任务等待用户输入（waiting_input）时，请引导用户直接回复即可。
</plugin_intro>
{% endif %}
{% if waiting %}<task_board session="{{ session_id }}">
当前需要你处理的子代理任务：
待用户回复（任务在等待用户输入，请引导用户直接回复即可）：
{% for t in waiting %}- [{{ t.status }}] {{ t.title }}（{{ t.info }}）[id:{{ t.id8 }}]
{% endfor %}</task_board>
{% endif %}