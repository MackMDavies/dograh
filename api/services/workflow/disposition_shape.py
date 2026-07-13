"""Canonical shape + normalizer for workflow call_disposition_codes.

`call_disposition_codes` is a workflow-level JSON column that has historically
been written in a couple of different shapes:

  - legacy list shape:   {"disposition_codes": ["SALE", "XFER", ...]}
  - legacy map shape:    {"SALE": "Sale made", "XFER": "Transferred"}
  - canonical shape:     {"items": [{"code": "SALE", "label": "Sale made"}, ...]}

The canonical shape is `{"items": [{"code", "label"}]}`. `normalize_disposition_codes`
reads any of the shapes above and returns the canonical shape so callers never
have to special-case the legacy formats.
"""


def normalize_disposition_codes(raw):
    """Normalize any known call_disposition_codes shape into the canonical
    {"items": [{"code", "label"}]} shape.

    Args:
        raw: The raw value from the workflow.call_disposition_codes column
            (may be None, a non-dict, or any of the legacy/canonical shapes).

    Returns:
        dict: {"items": [{"code": str, "label": str}, ...]}
    """
    if not isinstance(raw, dict):
        return {"items": []}
    if isinstance(raw.get("items"), list):
        return {
            "items": [
                {"code": i["code"], "label": i.get("label") or i["code"]}
                for i in raw["items"]
                if i.get("code")
            ]
        }
    if isinstance(raw.get("disposition_codes"), list):
        return {"items": [{"code": c, "label": c} for c in raw["disposition_codes"] if c]}
    return {"items": [{"code": k, "label": v} for k, v in raw.items() if isinstance(v, str)]}


def append_disposition_code(current, code):
    """Append a disposition code to the canonical shape, idempotently.

    Reads `current` in any known shape (via `normalize_disposition_codes`) and
    returns the canonical shape with `code` appended (labeled with itself)
    unless it's already present, in which case the normalized value is
    returned unchanged.

    Args:
        current: The raw value from the workflow.call_disposition_codes column.
        code: The disposition code to append.

    Returns:
        dict: {"items": [{"code": str, "label": str}, ...]}
    """
    norm = normalize_disposition_codes(current)
    if any(i["code"] == code for i in norm["items"]):
        return norm
    norm["items"].append({"code": code, "label": code})
    return norm
