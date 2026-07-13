from api.services.workflow.disposition_shape import (
    append_disposition_code,
    normalize_disposition_codes,
)


def test_reads_all_shapes_to_items():
    assert normalize_disposition_codes({"disposition_codes": ["SALE"]}) == {
        "items": [{"code": "SALE", "label": "SALE"}]
    }
    assert normalize_disposition_codes({"items": [{"code": "SALE", "label": "Sale"}]}) == {
        "items": [{"code": "SALE", "label": "Sale"}]
    }
    assert normalize_disposition_codes({"SALE": "Sale made"}) == {
        "items": [{"code": "SALE", "label": "Sale made"}]
    }


def test_normalize_handles_none_and_junk():
    assert normalize_disposition_codes(None) == {"items": []}
    assert normalize_disposition_codes("nope") == {"items": []}


def test_auto_append_adds_code_once():
    base = {"items": [{"code": "SALE", "label": "Sale"}]}
    assert append_disposition_code(base, "XFER")["items"][-1] == {
        "code": "XFER",
        "label": "XFER",
    }
    assert append_disposition_code(base, "SALE") == base  # idempotent


def test_auto_append_reads_legacy_shape():
    legacy = {"disposition_codes": ["SALE"]}
    out = append_disposition_code(legacy, "XFER")
    assert out == {
        "items": [
            {"code": "SALE", "label": "SALE"},
            {"code": "XFER", "label": "XFER"},
        ]
    }
