"""Tests for ManagedProvisioner — all Twilio REST calls are mocked."""
import asyncio

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
        mock_client.incoming_phone_numbers.assert_called_once_with("PNabc123")


class TestGetManagedProvisioner:
    def test_returns_none_when_no_db_and_env_vars_absent(self):
        with patch.dict("os.environ", {}, clear=True):
            import os
            os.environ.pop("SYSEVO_TWILIO_ACCOUNT_SID", None)
            os.environ.pop("SYSEVO_TWILIO_AUTH_TOKEN", None)
            from api.services.telephony import managed_provisioner as mp
            from api.db import db_client as db_instance
            with patch.object(
                db_instance, "get_platform_twilio_credentials",
                new=AsyncMock(return_value=None),
            ):
                result = asyncio.run(mp.get_managed_provisioner())
        assert result is None

    def test_falls_back_to_env_when_no_db_credentials(self):
        with patch.dict("os.environ", {
            "SYSEVO_TWILIO_ACCOUNT_SID": "ACtest",
            "SYSEVO_TWILIO_AUTH_TOKEN": "tokentest",
        }):
            from api.services.telephony import managed_provisioner as mp
            from api.db import db_client as db_instance
            with patch.object(
                db_instance, "get_platform_twilio_credentials",
                new=AsyncMock(return_value=None),
            ):
                result = asyncio.run(mp.get_managed_provisioner())
        assert isinstance(result, ManagedProvisioner)
        assert result.account_sid == "ACtest"

    def test_db_credentials_take_precedence_over_env(self):
        with patch.dict("os.environ", {
            "SYSEVO_TWILIO_ACCOUNT_SID": "ACenv",
            "SYSEVO_TWILIO_AUTH_TOKEN": "envtoken",
        }):
            from api.services.telephony import managed_provisioner as mp
            from api.db import db_client as db_instance
            db_creds = {"account_sid": "ACdb", "auth_token": "dbtoken"}
            with patch.object(
                db_instance, "get_platform_twilio_credentials",
                new=AsyncMock(return_value=db_creds),
            ):
                result = asyncio.run(mp.get_managed_provisioner())
        assert isinstance(result, ManagedProvisioner)
        assert result.account_sid == "ACdb"
