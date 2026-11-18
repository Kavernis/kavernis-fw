"""Regenerate the public schemas from the runtime resource models.

Run with PYTHONPATH=backend python scripts/generate_network_schemas.py.
Topology/reference rules are enforced by core after resource validation.
"""

import json
from pathlib import Path

from kavernis.config.network import (
    DevicesConfig,
    LogicalInterfacesConfig,
    RoutingConfig,
)


def main() -> None:
    directory = Path(__file__).resolve().parents[1] / "schemas/network"
    directory.mkdir(parents=True, exist_ok=True)
    for resource, model in (
        ("devices", DevicesConfig),
        ("interfaces", LogicalInterfacesConfig),
        ("routing", RoutingConfig),
    ):
        schema = model.model_json_schema()
        schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
        (directory / f"{resource}.schema.json").write_text(
            json.dumps(schema, indent=2) + "\n", encoding="utf-8"
        )


if __name__ == "__main__":
    main()
