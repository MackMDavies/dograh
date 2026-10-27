"""Run-local entry points for the Studio's authenticated browser voice tests.

The saved definition and its real start flags are never changed. Phone, campaign,
public and text runs cannot opt into this browser-only testing behavior.
"""
from copy import deepcopy

ANNOTATION = 'studio_test_start_node_id'
BROWSER_MODE = 'smallwebrtc'


class StudioTestEntryError(ValueError):
    pass


def test_entry_annotations(mode: str, node_id: str | None, definition: dict | None,
                           annotations: dict | None = None) -> dict:
    result = deepcopy(annotations or {})
    if node_id is None:
        return result
    if mode != BROWSER_MODE:
        raise StudioTestEntryError('Starting from a selected step is only available for browser voice tests')
    node = next((n for n in (definition or {}).get('nodes', []) if n.get('id') == node_id), None)
    if not node or node.get('type') not in ('startCall', 'agentNode'):
        raise StudioTestEntryError('Choose a conversation step in the saved draft to test from')
    result[ANNOTATION] = node_id
    return result


def apply_test_entry(graph, mode: str, annotations: dict | None) -> None:
    node_id = (annotations or {}).get(ANNOTATION)
    if not node_id:
        return
    if mode != BROWSER_MODE:
        raise StudioTestEntryError('A selected test step cannot run in a phone call')
    node = graph.nodes.get(node_id)
    if node is None or str(node.node_type) not in ('startCall', 'agentNode'):
        raise StudioTestEntryError('The selected test step is unavailable in this run definition')
    graph.start_node_id = node_id
