"""oh-mai-agent 领域层。

将持久化数据（TaskRecord）与运行时状态（TaskRuntime）分离，
为未来子进程派遣架构奠定基础——DB 是唯一共享状态，
运行时对象（Event、队列、AgentLoop 引用）永不落库。
"""
