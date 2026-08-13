from oh_mai_agent.plugin import create_plugin


def test_create_plugin_is_callable() -> None:
    assert callable(create_plugin)
