"""Tests for ManagedProvisioner — all Twilio REST calls are mocked."""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from api.services.telephony.managed_provisioner import ManagedProvisioner, ProvisionedNumber


@pytest.fixture
def provisioner():
    return ManagedProvisioner(account_sid="ACtest", auth_token="tokentest")


class TestCarrierLookup:
    def test_lookup_returns_carrier_and_country(self, provisioner):
        mock_lookup = MagicMock()
        mock_lookup.line_type_intelligence = {"carrier_name": "EE", "type": "mobile"}
        mock_lookup.country_code = "GB"

        with patch.object(provisioner, "_client") as mock_client:
            mock_client.lookups.v2.phone_numbers.return_value.fetch.return_value = mock_lookup
            result = provisioner.lookup_carrier("+441234567890")

        assert result["carrier"] == "EE"
        assert result["country"] == "GB"
        assert result["line_type"] == "mobile"

    def test_lookup_returns_none_carrier_on_api_error(self, provisioner):
        with patch.object(provisioner, "_client") as mock_client:
            mock_client.lookups.v2.phone_numbers.return_value.fetch.side_effect = Exception("API error")
            result = provisioner.lookup_carrier("+441234567890")

        assert result["carrier"] is None
        assert result["country"] == "GB"  # parsed from number even on error


class TestSearchNumbers:
    def test_search_returns_list_of_e164_numbers(self, provisioner):
        mock_number = MagicMock()
        mock_number.phone_number = "+12125551234"

        with patch.object(provisioner, "_client") as mock_client:
            mock_client.available_phone_numbers.return_value.local.list.return_value = [mock_number]
            numbers = provisioner.search_available_numbers("US", area_code="212")

        assert numbers == ["+12125551234"]


class TestProvisionNumber:
    def test_provision_returns_provisioned_number(self, provisioner):
        mock_number = MagicMock()
        mock_number.phone_number = "+12125559876"
        mock_number.sid = "PNabc123"

        with patch.object(provisioner, "_client") as mock_client:
            mock_client.incoming_phone_numbers.create.return_value = mock_number
            result = provisioner.provision_number("+12125559876", voice_url="https://api.sysevo.io/api/v1/telephony/twiml")

        assert result.e164 == "+12125559876"
        assert result.twilio_sid == "PNabc123"


class TestReleaseNumber:
    def test_release_calls_delete(self, provisioner):
        with patch.object(provisioner, "_client") as mock_client:
            mock_client.incoming_phone_numbers.return_value.delete.return_value = True
            result = provisioner.release_number("PNabc123")

        assert result is True
        mock_client.incoming_phone_numbers.return_value.delete.assert_called_once()
