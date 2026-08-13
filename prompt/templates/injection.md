<plugin_injected_instruction id="{{ note_id }}" plugin_id="oh-mai-agent">{# 注入指令 XML 块：instruction/note_id 已由 InjectionMessageBuilder 转义，模板 autoescape=False 不重复转义 #}
用户/管理者注入了新指令：{{ instruction }}，请优先处理。
（本条为插件注入的上下文记录，不是聊天对象发言）
</plugin_injected_instruction>
