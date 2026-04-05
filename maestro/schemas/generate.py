"""Generate JSON Schema files from Pydantic models."""

import json
from pathlib import Path

from maestro.models import OrchestratorConfig, ProjectConfig


SCHEMA_DIR = Path(__file__).parent


def main() -> None:
    """Generate JSON Schema files for config models."""
    schemas: dict[str, type] = {
        "project_config.json": ProjectConfig,
        "orchestrator_config.json": OrchestratorConfig,
    }

    for filename, model in schemas.items():
        schema = model.model_json_schema()
        output = SCHEMA_DIR / filename
        output.write_text(json.dumps(schema, indent=2) + "\n")
        print(f"Written {output}")


if __name__ == "__main__":
    main()
