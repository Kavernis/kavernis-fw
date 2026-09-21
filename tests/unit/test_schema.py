import json
from pathlib import Path


def test_interfaces_schema_is_available() -> None:
    schema_path = Path("schemas/interfaces.schema.json")
    schema = json.loads(schema_path.read_text(encoding="utf-8"))

    assert schema["$defs"]["interface"]["required"] == [
        "uid",
        "id",
        "name",
        "device",
        "ipv4",
        "ipv6",
    ]
