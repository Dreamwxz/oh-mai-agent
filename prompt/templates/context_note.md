<plugin_context_note id="{{ note_id }}" plugin_id="oh-mai-agent" kind="{{ kind }}">{# 上下文记录 XML 块：kind/content/note_id 已由 ContextNoteBuilder 转义，模板 autoescape=False 不重复转义 #}
{% if kind == 'sent-message' %}{{bot_name}}在此流发送了消息：{{ content }}{% else %}{{bot_name}}此前在此流发送了任务消息：{{ content }}{% endif %}
（本条为插件注入的上下文记录，不是聊天对象发言）
</plugin_context_note>
