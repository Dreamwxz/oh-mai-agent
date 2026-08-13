import json


def test_manifest_capabilities():
    with open("_manifest.json", encoding="utf-8") as f:
        manifest = json.load(f)
    caps = manifest["capabilities"]
    assert "maisaka.context.append" in caps, "maisaka.context.append missing"
    assert "chat.open_session" in caps, "chat.open_session missing"


def test_manifest_valid_json():
    with open("_manifest.json", encoding="utf-8") as f:
        json.load(f)  # 未抛异常即说明是合法 JSON
