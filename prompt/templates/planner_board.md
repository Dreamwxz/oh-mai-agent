{# Planner 任务看板 XML 块。
   由 PlannerBoardBuilder 预格式化后传入 active/scheduled/recent 三组 dict 列表，
   每组元素含 status (str)/title (str)/id8 (str)/info 或 rel (str)。
   id8 为任务 ID 前 8 位，供 Planner 复制到 task_status/task_history 等工具。
   全空时 builder 短路返回 ""，不会进入此模板。
-#}
{% if active or scheduled or recent %}<task_board session="{{ session_id }}">
当前后台任务看板：
{% if active %}活跃任务：
{% for t in active %}- [{{ t.status }}] {{ t.title }}（{{ t.info }}）[id:{{ t.id8 }}]
{% endfor %}{% endif %}{% if scheduled %}定时任务：
{% for t in scheduled %}- [{{ t.status }}] {{ t.title }}（{{ t.info }}）[id:{{ t.id8 }}]
{% endfor %}{% endif %}{% if recent %}最近完成：
{% for t in recent %}- [{{ t.status }}] {{ t.title }}（{{ t.rel }}）[id:{{ t.id8 }}]
{% endfor %}{% endif %}</task_board>{% endif %}
