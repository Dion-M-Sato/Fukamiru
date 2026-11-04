"""Export an executable workflow graph without importing optional runtime dependencies."""

from __future__ import annotations

import argparse
import ast
import json
import runpy
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "src/fukamiru/workflow/templates.py"
PARAGRAPH_ARGUMENT_MOVE_CATALOG = runpy.run_path(
    str(ROOT / "src/fukamiru/authoring/paragraph_moves.py")
)["PARAGRAPH_ARGUMENT_MOVE_CATALOG"]


class Spec:
    def __init__(self, **values):
        self.resource_lane = "ingestion_cpu"
        self.retry = None
        self.join_from = []
        self.join_generation_source = None
        self.condition = "success"
        self.__dict__.update(values)

    def model_copy(self, *, update):
        return type(self)(**{**vars(self), **update})

    def model_dump(self):
        return vars(self).copy()

    @classmethod
    def model_validate(cls, value):
        return cls(**value)


def export(*, version: str = "31", workflow_name: str | None = None) -> dict:
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"), filename=str(SOURCE))
    namespace = {
        "NodeSpec": Spec,
        "EdgeSpec": Spec,
        "RetryPolicy": Spec,
        "WorkflowDefinition": Spec,
    }
    module = ast.Module(
        body=[item for item in tree.body if isinstance(item, ast.FunctionDef)],
        type_ignores=[],
    )
    exec(compile(module, str(SOURCE), "exec"), namespace)
    workflow = (
        namespace[workflow_name]()
        if workflow_name
        else namespace["authoring_workflow"](version=version)
    )
    nodes = [
        {
            "id": node.id,
            "title": node.title,
            "handler": node.handler,
            "resource_lane": node.resource_lane,
            "max_attempts": getattr(getattr(node, "retry", None), "max_attempts", 1),
            **(
                {"human_review": node.human_review}
                if getattr(node, "human_review", "never") != "never"
                else {}
            ),
            "join_from": getattr(node, "join_from", []),
            "join_generation_source": getattr(node, "join_generation_source", None),
        }
        for node in workflow.nodes
    ]
    edges = [
        {"from": edge.source, "to": edge.target, "condition": edge.condition}
        for edge in workflow.edges
    ]
    node_ids = {node["id"] for node in nodes}
    has_invalid_endpoint = any(
        edge["from"] not in node_ids or edge["to"] not in node_ids for edge in edges
    )
    if len(nodes) != len(node_ids) or has_invalid_endpoint:
        raise ValueError(f"V{version} DAG has duplicate nodes or an unresolved edge endpoint")
    return {
        "name": workflow.name,
        "version": workflow.version,
        "nodes": nodes,
        "edges": edges,
        "paragraph_argument_move_catalog": [dict(item) for item in PARAGRAPH_ARGUMENT_MOVE_CATALOG],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", default="32")
    parser.add_argument("--workflow", default="authoring_workflow")
    args = parser.parse_args()
    print(
        json.dumps(
            export(version=args.version, workflow_name=args.workflow), ensure_ascii=False, indent=2
        )
    )


if __name__ == "__main__":
    main()
