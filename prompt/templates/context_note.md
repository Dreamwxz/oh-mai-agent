<plugin_context_note id="{{ note_id }}" plugin_id="oh-mai-agent" kind="{{ kind }}">{# 上下文记录 XML 块：kind/content/title/question/note_id/bot_name 已由 ContextNoteBuilder 转义，模板 autoescape=False 不重复转义 #}
{% if kind == 'sent-message' %}{{bot_name}}在此流发送了消息：{{ content }}{% elif kind == 'task-reply' %}{{bot_name}}此前在此流发送了任务消息：{{ content }}{% elif kind == 'task-waiting' %}{{bot_name}}的后台子代理任务「{{ title }}」正在等待用户回复，问题：{{ question }}（请引导用户直接回复即可，收到回复后任务自动继续执行）{% endif %}
（本条为插件注入的上下文记录，不是聊天对象发言）
</plugin_context_note>
