"""Pure preview-boundary tests: no DB, HTTP requests, or actual credentials."""
import ipaddress

import pytest

from api.services.workflow.tools import preview_security as security


@pytest.fixture(autouse=True)
def public_dns(monkeypatch):
    monkeypatch.setattr(security, "_resolve_hostname_ips", lambda host, port: [ipaddress.ip_address("93.184.216.34")])


def test_same_origin_keeps_unsaved_path_and_argument_edits():
    security.validate_preview_target("https://api.example.test/v1", "https://API.example.test:443/v2?name=Test", has_credentials=True)


def test_saved_credential_cannot_follow_an_unsaved_host_change():
    with pytest.raises(ValueError, match="Save the tool"):
        security.validate_preview_target("https://api.example.test/v1", "https://other.example.test/v1", has_credentials=True)


def test_saved_headers_cannot_follow_a_port_change():
    with pytest.raises(ValueError, match="Save the tool"):
        security.validate_preview_target("https://api.example.test/v1", "https://api.example.test:8443/v1", has_credentials=True)


def test_without_saved_headers_a_public_new_origin_can_be_tested():
    security.validate_preview_target("https://api.example.test/v1", "https://other.example.test/v1", has_credentials=False)


@pytest.mark.parametrize("address", ["127.0.0.1", "10.0.0.1", "169.254.169.254", "100.64.0.1", "::1", "0.0.0.0"])
def test_internal_destinations_are_blocked(monkeypatch, address):
    monkeypatch.setattr(security, "_resolve_hostname_ips", lambda host, port: [ipaddress.ip_address(address)])
    with pytest.raises(ValueError, match="public IP"):
        security.validate_preview_target("https://api.example.test", "https://api.example.test", has_credentials=False)


def test_one_public_dns_answer_cannot_hide_a_private_one(monkeypatch):
    monkeypatch.setattr(security, "_resolve_hostname_ips", lambda host, port: [ipaddress.ip_address("93.184.216.34"), ipaddress.ip_address("127.0.0.1")])
    with pytest.raises(ValueError, match="public IP"):
        security.validate_preview_target("https://api.example.test", "https://api.example.test", has_credentials=False)


@pytest.mark.parametrize("url", ["http://api.example.test", "https://name:password@api.example.test", "file:///etc/passwd"])
def test_https_and_no_embedded_credentials(url):
    with pytest.raises(ValueError):
        security.validate_preview_target("https://api.example.test", url, has_credentials=False)


def test_custom_credential_headers_are_masked_case_insensitively():
    headers = {"X-Custom": "fixture-value", "Authorization": "Bearer fixture-value", "Content-Type": "application/json"}
    assert security.mask_preview_headers(headers, {"x-custom"}) == {"X-Custom": "••••", "Authorization": "••••", "Content-Type": "application/json"}
