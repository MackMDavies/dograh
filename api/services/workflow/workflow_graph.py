import re
from collections import Counter
from typing import Dict, List, Set

from api.services.workflow.dto import EdgeDataDTO, NodeType, ReactFlowDTO
from api.services.workflow.errors import ItemKind, WorkflowError
from api.services.workflow.node_data import BaseNodeData
from api.services.workflow.node_specs import get_spec

# Regex for matching {{ variable }} template placeholders.
# Captures: group(1) = variable path, group(2) = the whole fallback expression.
# The fallback is captured verbatim (colons included) and only split on ":" by
# the renderer for the legacy {{var | fallback:default}} form — otherwise a
# perfectly ordinary default like "Hi: friend" would be truncated at the colon.
# Shared with api.utils.template_renderer via import.
TEMPLATE_VAR_PATTERN = r"\{\{\s*([^|\s}]+)(?:\s*\|\s*([^}]+))?\s*\}\}"

# Legacy fallback filter prefix: {{var | fallback:default}}
LEGACY_FALLBACK_FILTER = "fallback"

# Variables injected by the system at runtime, not from source data.
# Variables the platform supplies at render time, so they must never be demanded
# as columns in a campaign's source data. The date/time set below is resolved by
# _resolve_builtin_variable() in api/utils/template_renderer.py — keep the two in
# step: a name listed here but not resolved there renders blank on a live call,
# which is worse than the validation error, because it fails silently.
# Everything the Sysevo memory pre-call fetch supplies. Unlike the rest of
# _SYSTEM_VARIABLES these are NOT available when the call opens — they arrive
# when the fetch returns. Anything that has to decide whether it can proceed
# without the fetch (see api.services.pipecat.opening_gate) needs to tell the
# two groups apart, so keep this the single source of truth for the split.
PRE_CALL_FETCH_VARIABLES = {
    # Inbound caller recognition, injected by the Sysevo memory pre-call fetch.
    # Someone ringing our outbound number is usually ringing BACK, so the agent
    # is told who they are before it speaks. These must be listed here or a
    # template that greets by name is treated as having unfilled variables and
    # blocks campaign launch.
    "caller_name",
    "caller_company",
    "caller_email",
    "caller_known",
    "caller_known_from",
    "caller_memory",
    "is_callback",
    "prior_outbound_attempts",
    # The rest of what dograh-memory-inbound-hook returns. These are supplied at
    # runtime exactly like the ones above, but were never listed, so a prompt
    # referencing one failed campaign launch as a missing CSV column —
    # caller_last_call blocked a 58-row run. Every key the hook can return must
    # appear here or it is a launch blocker waiting for someone to use it.
    "caller_phone",
    "caller_total_calls",
    "caller_last_call",
    "caller_last_sentiment",
}

_SYSTEM_VARIABLES = {
    "campaign_id",
    "provider",
    "source_uuid",
    # Date / time built-ins. An agent that has to work the date out itself gets
    # it wrong — this prompt set was added after a real customer was booked into
    # the wrong YEAR.
    "current_time",
    "current_weekday",
    "current_date",
    "current_date_spoken",
    "current_day",
    "current_year",
    "time_now",
    "time_now_spoken",
    "upcoming_days",
    *PRE_CALL_FETCH_VARIABLES,
}


def extract_template_variables(text: str, *, include_system: bool = False) -> Set[str]:
    """Extract template variable names from a string, excluding nested paths,
    variables with a fallback filter, and (unless `include_system`) the
    system-injected variables."""
    variables: Set[str] = set()
    for match in re.finditer(TEMPLATE_VAR_PATTERN, text):
        var_name = match.group(1).strip()
        filter_name = match.group(2).strip() if match.group(2) else None

        # Skip nested paths (runtime-resolved, e.g. gathered_context.city)
        if "." in var_name:
            continue
        # Skip variables with a fallback (they have a default value)
        # Supports both {{var | default}} and legacy {{var | fallback:default}}
        if filter_name is not None:
            continue
        # Skip system-injected variables
        if not include_system and var_name in _SYSTEM_VARIABLES:
            continue

        variables.add(var_name)
    return variables


class Edge:
    def __init__(self, source: str, target: str, data: EdgeDataDTO):
        self.source = source
        self.target = target

        self.label = data.label
        self.condition = data.condition
        self.transition_speech = data.transition_speech

        self.data = data

    def get_function_name(self):
        return re.sub(r"[^a-z0-9]", "_", self.label.lower())

    def __eq__(self, other):
        if not isinstance(other, Edge):
            return False
        return self.source == other.source and self.target == other.target

    def __hash__(self):
        return hash((self.source, self.target))


class Node:
    def __init__(self, id: str, node_type: str, data: BaseNodeData):
        self.id, self.node_type, self.data = id, node_type, data
        self.out: Dict[str, "Node"] = {}  # forward nodes
        self.out_edges: List[Edge] = []  # forward edges with properties

        # name/is_start/is_end live on every per-type data class (base).
        self.name = data.name
        self.is_start = data.is_start
        self.is_end = data.is_end

        # Type-specific fields — read with getattr so this works for every
        # node variant in the discriminated union.
        self.prompt = getattr(data, "prompt", None)
        self.allow_interrupt = getattr(data, "allow_interrupt", False)
        self.extraction_enabled = getattr(data, "extraction_enabled", False)
        self.extraction_prompt = getattr(data, "extraction_prompt", None)
        self.extraction_variables = getattr(data, "extraction_variables", None)
        # Names of extraction variables that gate this node's outgoing
        # transitions — only meaningful when extraction actually runs, so a
        # node with extraction disabled can never soft-lock a call on a
        # variable that would never be populated.
        self.required_variable_names: List[str] = (
            [
                v.name
                for v in (self.extraction_variables or [])
                if getattr(v, "required_for_exit", False)
            ]
            if self.extraction_enabled
            else []
        )
        self.add_global_prompt = getattr(data, "add_global_prompt", True)
        self.greeting = getattr(data, "greeting", None)
        self.greeting_type = getattr(data, "greeting_type", None)
        self.greeting_recording_id = getattr(data, "greeting_recording_id", None)
        self.delayed_start = getattr(data, "delayed_start", False)
        self.delayed_start_duration = getattr(data, "delayed_start_duration", None)
        self.tool_uuids = getattr(data, "tool_uuids", None)
        self.document_uuids = getattr(data, "document_uuids", None)
        self.mcp_tool_filters = getattr(data, "mcp_tool_filters", None)
        self.pre_call_fetch_enabled = getattr(data, "pre_call_fetch_enabled", False)
        self.pre_call_fetch_url = getattr(data, "pre_call_fetch_url", None)
        self.pre_call_fetch_credential_uuid = getattr(
            data, "pre_call_fetch_credential_uuid", None
        )

        self.data = data


class WorkflowGraph:
    """
    *All* business invariants (acyclic, cardinality, etc.) are verified here.
    The constructor accepts a validated ReactFlowDTO.
    """

    def __init__(self, dto: ReactFlowDTO):
        # Build adjacency list from validated DTO nodes. Core node comparisons
        # still use NodeType string enums; integration nodes remain plain
        # strings and resolve constraints through node specs.
        self.nodes: Dict[str, Node] = {
            n.id: Node(n.id, n.type, n.data) for n in dto.nodes
        }

        # Store all edges
        self.edges: List[Edge] = []

        for e in dto.edges:
            source_node = self.nodes[e.source]
            target_node = self.nodes[e.target]

            # Create the edge with properties from dto
            edge = Edge(source=e.source, target=e.target, data=e.data)

            # Add to the edge list
            self.edges.append(edge)

            # Add to the source node's outgoing edges
            source_node.out_edges.append(edge)

            # Set up the node references for backward compatibility
            source_node.out[target_node.id] = target_node

        self._validate_graph()

        # Get a reference to the start node
        self.start_node_id = [n.id for n in dto.nodes if n.data.is_start][0]

        # Get a reference to the global node
        try:
            self.global_node_id = [
                n.id for n in dto.nodes if n.type == NodeType.globalNode.value
            ][0]
        except IndexError:
            self.global_node_id = None

    # -----------------------------------------------------------
    # template variable extraction
    # -----------------------------------------------------------
    def get_required_template_variables(self) -> Set[str]:
        """Extract all template variables referenced in node prompts/greetings
        and edge transition speeches.

        Scans:
          - Start node: prompt, greeting
          - Agent / End / Global nodes: prompt
          - All edges: transition_speech

        Returns a set of top-level variable names that the workflow expects
        from the source data (excluding nested paths, fallback vars, and
        system-injected vars).
        """
        variables: Set[str] = set()

        for node in self.nodes.values():
            if node.node_type in (
                NodeType.startNode,
                NodeType.agentNode,
                NodeType.endNode,
                NodeType.globalNode,
            ):
                if node.prompt:
                    variables |= extract_template_variables(node.prompt)

            # greeting is only relevant on the start node
            if node.node_type == NodeType.startNode and node.greeting:
                variables |= extract_template_variables(node.greeting)

        for edge in self.edges:
            if edge.transition_speech:
                variables |= extract_template_variables(edge.transition_speech)

        return variables

    # -----------------------------------------------------------
    # validators
    # -----------------------------------------------------------
    def _validate_graph(self) -> None:
        errors: list[WorkflowError] = []

        # TODO: Figure out what kind of cyclic contraints can be applied, since there can be a cycle in the graph
        # try:
        #     self._assert_acyclic()
        # except ValueError as e:
        #     errors.append(
        #         WorkflowError(
        #             kind=ItemKind.workflow, id=None, field=None, message=str(e)
        #         )
        #     )

        errors.extend(self._assert_start_node())
        errors.extend(self._assert_connection_counts())
        errors.extend(self._assert_global_node())
        errors.extend(self._assert_node_configs())
        if errors:
            raise ValueError(errors)

    def _assert_acyclic(self):
        color: Dict[str, str] = {}  # white / gray / black

        def dfs(n: Node):
            if color.get(n.id) == "gray":  # back-edge
                raise ValueError("workflow contains a cycle")
            if color.get(n.id) != "black":
                color[n.id] = "gray"
                for m in n.out.values():
                    dfs(m)
                color[n.id] = "black"

        for n in self.nodes.values():
            dfs(n)

    def _assert_start_node(self):
        errors: list[WorkflowError] = []
        start_nodes = [n for n in self.nodes.values() if n.data.is_start]
        if not start_nodes:
            errors.append(
                WorkflowError(
                    kind=ItemKind.workflow,
                    id=None,
                    field=None,
                    message="Workflow has no start node — exactly one is required",
                )
            )
        elif len(start_nodes) > 1:
            errors.append(
                WorkflowError(
                    kind=ItemKind.workflow,
                    id=None,
                    field=None,
                    message=(
                        f"Workflow has {len(start_nodes)} start nodes — "
                        f"exactly one is required"
                    ),
                )
            )
        return errors

    def _assert_global_node(self):
        errors: list[WorkflowError] = []
        global_node = [
            n for n in self.nodes.values() if n.node_type == NodeType.globalNode.value
        ]
        if not len(global_node) <= 1:
            errors.append(
                WorkflowError(
                    kind=ItemKind.workflow,
                    id=None,
                    field=None,
                    message="Workflow must have at most one global node",
                )
            )
        return errors

    def _assert_connection_counts(self):
        """Enforce per-type incoming/outgoing edge constraints.

        Driven by `NodeSpec.graph_constraints` so a single source of truth
        in the spec dictates what's legal. Types without a `graph_constraints`
        block are unconstrained (e.g. agentNode on the outgoing side).
        """
        errors: list[WorkflowError] = []

        out_deg = Counter()
        in_deg = Counter()
        for n in self.nodes.values():
            out_deg[n.id] = in_deg[n.id] = 0
        for src, n in self.nodes.items():
            for m in n.out.values():
                out_deg[src] += 1
                in_deg[m.id] += 1

        for n in self.nodes.values():
            spec = get_spec(n.node_type)
            if spec is None or spec.graph_constraints is None:
                continue
            gc = spec.graph_constraints
            in_d, out_d = in_deg[n.id], out_deg[n.id]
            label = spec.display_name

            if gc.max_incoming is not None and in_d > gc.max_incoming:
                msg = (
                    f"{label} cannot have incoming edges"
                    if gc.max_incoming == 0
                    else f"{label} can have at most {gc.max_incoming} incoming edge(s)"
                )
                errors.append(
                    WorkflowError(kind=ItemKind.node, id=n.id, field=None, message=msg)
                )
            if gc.min_incoming is not None and in_d < gc.min_incoming:
                errors.append(
                    WorkflowError(
                        kind=ItemKind.node,
                        id=n.id,
                        field=None,
                        message=f"{label} must have at least {gc.min_incoming} incoming edge(s)",
                    )
                )
            if gc.max_outgoing is not None and out_d > gc.max_outgoing:
                msg = (
                    f"{label} cannot have outgoing edges"
                    if gc.max_outgoing == 0
                    else f"{label} can have at most {gc.max_outgoing} outgoing edge(s)"
                )
                errors.append(
                    WorkflowError(kind=ItemKind.node, id=n.id, field=None, message=msg)
                )
            if gc.min_outgoing is not None and out_d < gc.min_outgoing:
                errors.append(
                    WorkflowError(
                        kind=ItemKind.node,
                        id=n.id,
                        field=None,
                        message=f"{label} must have at least {gc.min_outgoing} outgoing edge(s)",
                    )
                )

        return errors

    def _assert_node_configs(self):
        """Validate node-specific configuration constraints."""
        errors: list[WorkflowError] = []

        for node in self.nodes.values():
            # Validate StartNode constraints
            if node.node_type == NodeType.startNode:
                # No specific validations for start node at this time
                pass

        return errors
