from blackcell.runtime import list_runtime_adapters


def test_runtime_adapters_include_opencode() -> None:
    adapters = {adapter.name: adapter for adapter in list_runtime_adapters()}

    assert "dry-run" in adapters
    assert "opencode" in adapters
    assert adapters["opencode"].kind == "external-agent"
