from __future__ import annotations

import ast
from pathlib import Path

from music_insight.api.contracts.history import (
    HistoryDetail as ContractHistoryDetail,
)
from music_insight.api.history import HistoryDetail


def test_history_contracts_keep_legacy_import_compatibility():
    assert HistoryDetail is ContractHistoryDetail


def test_application_services_do_not_import_fastapi_dependencies_module():
    services = Path("src/music_insight/api/services")
    violations: list[str] = []
    for path in sorted(services.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == (
                "music_insight.api.dependencies"
            ):
                violations.append(f"{path}:{node.lineno}")
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "music_insight.api.dependencies":
                        violations.append(f"{path}:{node.lineno}")

    assert violations == []
