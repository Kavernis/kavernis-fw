"""Published resource schemas stay synchronized with runtime models."""

import json
from pathlib import Path

import pytest
from kavernis.config.network import (
    DevicesConfig,
    LogicalInterfacesConfig,
    RoutingConfig,
)


@pytest.mark.parametrize(
    "resource,model",
    [
        ("devices", DevicesConfig),
        ("interfaces", LogicalInterfacesConfig),
        ("routing", RoutingConfig),
    ],
)
def test_schema_matches_runtime_resource(resource, model):
    schema = json.loads(Path(f"schemas/network/{resource}.schema.json").read_text())
    assert schema.pop("$schema") == "https://json-schema.org/draft/2020-12/schema"
    assert schema == model.model_json_schema()
    assert schema["properties"]["version"]["const"] == 2
    assert schema["additionalProperties"] is False
