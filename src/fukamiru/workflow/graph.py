"""Declarative workflow graph with explicit loop and transition semantics."""

from __future__ import annotations

from collections import deque
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from fukamiru.workflow.identity import is_manuscript_generator_workflow

TransitionCondition = Literal[
    "success",
    "conditional_success",
    "failure",
    "needs_review",
    "approved",
    "always",
]
ResourceLane = Literal["ingestion_cpu", "embedding_gpu", "codex_remote", "local_llm_gpu"]


class RetryPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_attempts: int = Field(default=1, ge=1, le=100)
    timeout_seconds: int | None = Field(default=None, ge=1)


class NodeSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    handler: str
    title: str
    resource_lane: ResourceLane = "ingestion_cpu"
    input_artifact_types: list[str] = Field(default_factory=list)
    output_artifact_types: list[str] = Field(default_factory=list)
    input_json_contract: str | None = None
    output_json_contract: str | None = None
    human_review: Literal["never", "on_defect", "always"] = "never"
    retry: RetryPolicy = Field(default_factory=RetryPolicy)
    join_from: list[str] = Field(default_factory=list)
    join_generation_source: str | None = None


class EdgeSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: str
    target: str
    condition: TransitionCondition = "success"
    label: str | None = None


class WorkflowDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    version: str
    title: str
    entry_node: str
    nodes: list[NodeSpec]
    edges: list[EdgeSpec]

    @model_validator(mode="after")
    def validate_graph(self) -> WorkflowDefinition:
        node_ids = [node.id for node in self.nodes]
        if len(node_ids) != len(set(node_ids)):
            raise ValueError("workflow node ids must be unique")
        node_set = set(node_ids)
        if self.entry_node not in node_set:
            raise ValueError("entry_node must reference an existing node")
        for edge in self.edges:
            if edge.source not in node_set or edge.target not in node_set:
                raise ValueError(f"edge references an unknown node: {edge.source} -> {edge.target}")
        by_id = {node.id: node for node in self.nodes}
        for edge in self.edges:
            source_contract = by_id[edge.source].output_json_contract
            target_contract = by_id[edge.target].input_json_contract
            if (
                source_contract is not None
                and target_contract is not None
                and source_contract != target_contract
            ):
                raise ValueError(
                    "edge JSON contract mismatch: "
                    f"{edge.source} ({source_contract}) -> "
                    f"{edge.target} ({target_contract})"
                )
        direct_edges = {(edge.source, edge.target) for edge in self.edges}
        for node in self.nodes:
            if bool(node.join_from) != bool(node.join_generation_source):
                raise ValueError(
                    f"join node {node.id} must set both join_from and join_generation_source"
                )
            if not node.join_from:
                continue
            if len(node.join_from) < 2 or len(node.join_from) != len(set(node.join_from)):
                raise ValueError(f"join node {node.id} requires at least two unique parents")
            if node.join_generation_source not in node_set:
                raise ValueError(
                    f"join node {node.id} references an unknown generation source"
                )
            missing_edges = [
                parent for parent in node.join_from if (parent, node.id) not in direct_edges
            ]
            if missing_edges:
                raise ValueError(
                    f"join node {node.id} is missing direct parent edges: "
                    + ", ".join(sorted(missing_edges))
                )
        reachable = self._reachable_nodes()
        unreachable = node_set - reachable
        if unreachable:
            raise ValueError(f"unreachable workflow nodes: {', '.join(sorted(unreachable))}")
        self._validate_final_artifact_barrier()
        return self

    def _validate_final_artifact_barrier(self) -> None:
        """Keep canonical-HTML mutation on the input side of the final gate.

        The final coherence result is only meaningful for the exact artifact that is
        persisted. V23+ additionally permits its read-only discourse validator on
        the way to the persistence sink. No path may mutate that artifact.
        """

        by_id = {node.id: node for node in self.nodes}
        final = by_id.get("validate_final_coherence")
        if final is None:
            return
        outgoing = [edge for edge in self.edges if edge.source == final.id]
        if not outgoing:
            raise ValueError("validate_final_coherence must lead to a persistence sink")
        required_conditions = {"success", "conditional_success"}
        actual_conditions = {edge.condition for edge in outgoing}
        if not required_conditions.issubset(actual_conditions):
            raise ValueError(
                "validate_final_coherence must persist both success and conditional_success"
            )
        readonly_handlers = set()
        if (
            self.name == "manuscript_rewrite"
            and str(self.version).isdigit()
            and int(self.version) >= 23
        ):
            readonly_handlers.add("manuscript_rewrite.validate_terms")
        if is_manuscript_generator_workflow(self.name):
            readonly_handlers.update(
                {
                    "authoring.evaluate_human_revision_requirement",
                    "authoring.submission_package",
                    "authoring.human_approval",
                }
            )
        persistence_handlers = {
            "manuscript_rewrite.persist_proposal",
            "manuscript_rewrite.persist_proposal_v26",
            "authoring.finalize_authoring",
        }

        def validate_tail(
            node_id: str, path: frozenset[str], *, persisted: bool = False
        ) -> None:
            handler = by_id[node_id].handler
            following = [edge for edge in self.edges if edge.source == node_id]
            if persisted and handler == "authoring.human_approval":
                # A deliberate human revision starts a new candidate generation.
                # It is not an automatic transformation of the frozen artifact.
                following = [edge for edge in following if not (
                    edge.condition == "failure"
                    and edge.target == "start_paragraph_formation"
                    and by_id[edge.target].handler == "authoring.start_paragraph_formation"
                )]
            if node_id in path:
                raise ValueError(
                    "canonical artifact transformation is forbidden after "
                    "validate_final_coherence: " + node_id
                )
            if handler in persistence_handlers:
                if persisted:
                    raise ValueError("final artifact may be persisted only once")
                if not following:
                    return
                for edge in following:
                    validate_tail(edge.target, path | {node_id}, persisted=True)
                return
            if handler not in readonly_handlers:
                raise ValueError(
                    "canonical artifact transformation is forbidden after "
                    "validate_final_coherence: " + node_id
                )
            if not following and persisted:
                return
            if not following or not any(
                edge.condition in {"success", "always"} for edge in following
            ):
                raise ValueError("final read-only validation must lead to a persistence sink")
            for edge in following:
                validate_tail(edge.target, path | {node_id}, persisted=persisted)

        for edge in outgoing:
            validate_tail(edge.target, frozenset({final.id}))

        renderers = [
            node
            for node in self.nodes
            if node.handler == "manuscript_rewrite.render_references"
        ]
        if not renderers:
            return
        adjacency: dict[str, list[str]] = {node.id: [] for node in self.nodes}
        for edge in self.edges:
            adjacency[edge.source].append(edge.target)

        def can_reach(source: str, target: str) -> bool:
            queue = deque([source])
            visited: set[str] = set()
            while queue:
                current = queue.popleft()
                if current == target:
                    return True
                if current in visited:
                    continue
                visited.add(current)
                queue.extend(adjacency[current])
            return False

        misplaced = [
            node.id for node in renderers if not can_reach(node.id, final.id)
        ]
        if misplaced:
            raise ValueError(
                "reference rendering must precede validate_final_coherence: "
                + ", ".join(sorted(misplaced))
            )

    def _reachable_nodes(self) -> set[str]:
        adjacency: dict[str, list[str]] = {node.id: [] for node in self.nodes}
        for edge in self.edges:
            adjacency[edge.source].append(edge.target)
        visited: set[str] = set()
        queue = deque([self.entry_node])
        while queue:
            node = queue.popleft()
            if node in visited:
                continue
            visited.add(node)
            queue.extend(adjacency[node])
        return visited

    def next_nodes(self, node_id: str, outcome: TransitionCondition) -> list[str]:
        known = {node.id for node in self.nodes}
        if node_id not in known:
            raise KeyError(node_id)
        return [
            edge.target
            for edge in self.edges
            if edge.source == node_id and edge.condition in {outcome, "always"}
        ]
