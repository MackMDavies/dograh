"""Regulatory bundles for in-country numbers — all Twilio REST calls are mocked.

Why these exist: Twilio refuses a GB local number without an approved Regulatory Bundle,
and quick-connect used to fall back SILENTLY to a US number -- a UK business got a +1
line. Each client's bundle is now built from their KYC in the account that buys numbers.
"""
from unittest.mock import MagicMock, patch

import pytest

from api.services.telephony.managed_provisioner import (
    ManagedProvisioner,
    RegulationNotAutomatable,
)

GB_REGULATION = {
    "end_user": [{
        "name": "Business",
        "type": "business",
        "requirement_name": "business_info",
        "fields": [
            "business_name", "business_registration_identifier", "business_registration_number",
            "business_website", "first_name", "last_name", "phone_number", "email",
            "business_identity", "is_subassigned", "comments",
        ],
        # As Twilio returns it: an EMPTY constraint marks an optional field.
        "detailed_fields": [
            {"machine_name": f, "constraint": ("" if f == "comments" else f"business['{f}'] != null")}
            for f in [
                "business_name", "business_registration_identifier", "business_registration_number",
                "business_website", "first_name", "last_name", "phone_number", "email",
                "business_identity", "is_subassigned", "comments",
            ]
        ],
    }],
    "supporting_document": [[{
        "name": "Business Address",
        "type": "document",
        "requirement_name": "business_address_proof_info",
        "accepted_documents": [{"name": "Address", "type": "customer_profile_address", "fields": ["address_sids"]}],
    }]],
}

KYC = {
    "business": {
        "name": "Bright Smile Dental Ltd",
        "registration_identifier": "UK:CRN",
        "registration_number": "01234567",
        "website": "https://brightsmile.example",
    },
    "representative": {
        "first_name": "Ada", "last_name": "Lovelace",
        "email": "ada@brightsmile.example", "phone": "+447700900123",
    },
    "address": {
        "street": "1 High Street", "street2": "Floor 2", "city": "Manchester",
        "region": "Greater Manchester", "postal_code": "M1 1AA", "country": "GB",
    },
}


@pytest.fixture
def provisioner():
    return ManagedProvisioner(account_sid="ACtest", auth_token="tokentest")


def _regulation(requirements):
    reg = MagicMock()
    reg.sid = "RNgb"
    reg.requirements = requirements
    return reg


class TestRegulationFor:
    def test_a_regulated_country_needs_a_bundle(self, provisioner):
        with patch.object(provisioner, "_client") as client:
            client.numbers.v2.regulatory_compliance.regulations.list.return_value = [_regulation(GB_REGULATION)]
            reg = provisioner.regulation_for("gb")
        assert reg["requires_bundle"] is True
        assert reg["sid"] == "RNgb"
        client.numbers.v2.regulatory_compliance.regulations.list.assert_called_once_with(
            iso_country="GB", number_type="local", end_user_type="business", include_constraints=True,
        )

    def test_a_country_with_no_regulation_needs_nothing(self, provisioner):
        with patch.object(provisioner, "_client") as client:
            client.numbers.v2.regulatory_compliance.regulations.list.return_value = []
            reg = provisioner.regulation_for("US")
        assert reg == {"sid": None, "requires_bundle": False}


class TestCreateBundle:
    def _client(self, client, evaluation_status="compliant"):
        rc = client.numbers.v2.regulatory_compliance
        rc.regulations.list.return_value = [_regulation(GB_REGULATION)]
        client.addresses.create.return_value = MagicMock(sid="ADaddr")
        rc.bundles.create.return_value = MagicMock(sid="BUbundle")
        rc.end_users.create.return_value = MagicMock(sid="ITuser")
        rc.supporting_documents.create.return_value = MagicMock(sid="RDdoc")
        bundle = rc.bundles.return_value
        bundle.evaluations.create.return_value = MagicMock(status=evaluation_status, results=[{"friendly_name": "x"}])
        bundle.update.return_value = MagicMock(status="pending-review")
        return rc, bundle

    def test_builds_address_end_user_document_and_submits(self, provisioner):
        with patch.object(provisioner, "_client") as client:
            rc, bundle = self._client(client)
            out = provisioner.create_bundle("GB", KYC, email="ops@sysevo.io", status_callback="https://cb")

        assert out == {"bundle_sid": "BUbundle", "address_sid": "ADaddr", "status": "pending-review", "failures": []}
        client.addresses.create.assert_called_once()
        addr = client.addresses.create.call_args.kwargs
        assert addr["customer_name"] == "Bright Smile Dental Ltd"
        assert addr["street"] == "1 High Street, Floor 2"
        assert addr["iso_country"] == "GB"

        attrs = rc.end_users.create.call_args.kwargs["attributes"]
        assert attrs["business_name"] == "Bright Smile Dental Ltd"
        assert attrs["business_registration_identifier"] == "UK:CRN"
        assert attrs["phone_number"] == "+447700900123"
        # Optional and not in our KYC: left out, not reported missing.
        assert "comments" not in attrs
        # Constants: the bundle is the CLIENT's (a direct customer), and we assign the number to them.
        assert attrs["business_identity"] == "DIRECT_CUSTOMER"
        assert attrs["is_subassigned"] == "YES"

        doc = rc.supporting_documents.create.call_args.kwargs
        assert doc["type"] == "customer_profile_address"
        assert doc["attributes"] == {"address_sids": ["ADaddr"]}

        assigned = [c.kwargs["object_sid"] for c in bundle.item_assignments.create.call_args_list]
        assert assigned == ["ITuser", "RDdoc"]
        bundle.update.assert_called_once_with(status="pending-review")

    def test_sends_the_phone_as_strict_e164(self, provisioner):
        # Our KYC form allows "+44 7700 900123"; Twilio's constraint is ^\+[1-9]\d{1,14}$.
        spaced = {**KYC, "representative": {**KYC["representative"], "phone": "+44 7700 (900)-123"}}
        with patch.object(provisioner, "_client") as client:
            rc, _ = self._client(client)
            provisioner.create_bundle("GB", spaced, email="ops@sysevo.io")
        assert rc.end_users.create.call_args.kwargs["attributes"]["phone_number"] == "+447700900123"

    def test_does_not_submit_a_bundle_twilio_evaluates_as_noncompliant(self, provisioner):
        with patch.object(provisioner, "_client") as client:
            rc, bundle = self._client(client, evaluation_status="noncompliant")
            out = provisioner.create_bundle("GB", KYC, email="ops@sysevo.io")
        assert out["status"] == "draft"
        assert out["failures"]
        bundle.update.assert_not_called()

    def test_names_what_is_missing_rather_than_sending_an_incomplete_bundle(self, provisioner):
        incomplete = {**KYC, "business": {**KYC["business"], "registration_number": ""}}
        with patch.object(provisioner, "_client") as client:
            self._client(client)
            with pytest.raises(ValueError, match="business_registration_number"):
                provisioner.create_bundle("GB", incomplete, email="ops@sysevo.io")
            client.addresses.create.assert_not_called()

    def test_refuses_a_regulation_that_needs_an_uploaded_file(self, provisioner):
        needs_file = {**GB_REGULATION, "supporting_document": [[{
            "name": "Business registration", "type": "document", "requirement_name": "reg_doc",
            "accepted_documents": [{"name": "Certificate", "type": "business_registration", "fields": ["business_name"]}],
        }]]}
        with patch.object(provisioner, "_client") as client:
            client.numbers.v2.regulatory_compliance.regulations.list.return_value = [_regulation(needs_file)]
            with pytest.raises(RegulationNotAutomatable, match="Business registration"):
                provisioner.create_bundle("FR", {**KYC, "address": {**KYC["address"], "country": "FR"}}, email="x@y.z")


class TestGetBundle:
    def test_reads_status_and_country(self, provisioner):
        fetched = MagicMock(sid="BUbundle", status="twilio-approved", valid_until=None)
        fetched.regulation_sid = "RNgb"
        with patch.object(provisioner, "_client") as client:
            client.numbers.v2.regulatory_compliance.bundles.return_value.fetch.return_value = fetched
            client.numbers.v2.regulatory_compliance.regulations.return_value.fetch.return_value = MagicMock(iso_country="GB")
            out = provisioner.get_bundle("BUbundle")
        assert out["status"] == "twilio-approved"
        assert out["iso_country"] == "GB"
