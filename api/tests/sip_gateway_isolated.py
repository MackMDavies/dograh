"""Exercise the actual SIP schemas, renderer and connect route without live DB."""
import ast
import importlib.util
import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import UUID

from fastapi import APIRouter, Depends, FastAPI, HTTPException
from fastapi.testclient import TestClient
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


schema = load('sip_test_schema', 'services/telephony/providers/sip/config.py')
config = load('sip_test_config', 'services/telephony/sip_gateway_config.py')
CREDS = {'domain': 'sip.fixture.test:5060', 'username': '1001', 'password': 'fixture-secret', 'number': '+12125550101'}
STORED = {**CREDS, 'gateway_id': 'a' * 32, 'resolved_ip': '8.8.8.8'}


class SchemaTests(unittest.TestCase):
    def test_requires_a_real_us_number_separate_from_the_extension(self):
        self.assertEqual(schema.SIPConfigurationRequest(**CREDS).number, CREDS['number'])
        for number in ['1001', '5060', '+447466158928', '+14165550101']:
            with self.subTest(number=number), self.assertRaises(ValueError):
                schema.SIPConfigurationRequest(**{**CREDS, 'number': number})

    def test_rejects_uri_and_configuration_injection(self):
        for field, values in {'domain': ['sip://bad.test', 'user@bad.test', 'bad.test/path', 'bad.test:70000', 'bad.test\n[global]'], 'username': ['a;context=other', 'a\n[global]', 'a@other'], 'password': ['a\n[global]', 'a\r']}.items():
            for value in values:
                with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                    schema.SIPConfigurationRequest(**{**CREDS, field: value})

    def test_per_tenant_contexts_and_ari_secrets_are_distinct(self):
        other = {**STORED, 'gateway_id': 'b' * 32}
        first_name, first_password = config.gateway_identity(STORED)
        second_name, second_password = config.gateway_identity(other)
        self.assertNotEqual(first_password, second_password)
        rendered = config.render_gateway_connections([{'credentials': STORED}, {'credentials': other}])
        self.assertIn(f'context={first_name}', rendered['pjsip_connections.conf'])
        self.assertIn(f'context={second_name}', rendered['pjsip_connections.conf'])
        self.assertIn(f'Stasis({first_name})', rendered['extensions_connections.conf'])
        self.assertNotIn('context=default', rendered['pjsip_connections.conf'])
        self.assertNotIn(CREDS['password'], rendered['ari_connections.conf'])

    def test_registration_never_accepts_unregistered_or_rejected(self):
        for value in ['Unregistered', 'Rejected', 'Status: Unregistered', 'Status: Registered elsewhere', '']:
            self.assertFalse(config.registration_is_ready(value))
        self.assertTrue(config.registration_is_ready(' Status: Registered\n'))
        self.assertTrue(config.registration_is_ready(' sysevo_' + 'a'*32 + '_registration/sip:fixture.test auth Registered (exp. 200s)'))

    def test_pins_signalling_to_a_validated_public_destination(self):
        rendered = config.render_gateway_connections([{'credentials': STORED}])['pjsip_connections.conf']
        self.assertIn('contact=sip:8.8.8.8:5060', rendered)
        self.assertIn('outbound_proxy=sip:8.8.8.8:5060\\;lr', rendered)
        for address in ['127.0.0.1', '10.0.0.2', '169.254.169.254', '::1']:
            with self.subTest(address=address), self.assertRaises(ValueError):
                config.render_gateway_connections([{'credentials': {**STORED, 'resolved_ip': address}}])

    def test_corrupt_identity_cannot_inject_control_commands(self):
        for identity in ['../other', 'a;core stop now', 'A'*32, 'a'*31]:
            with self.subTest(identity=identity), self.assertRaises(ValueError):
                config.gateway_identity({**STORED, 'gateway_id': identity})


class ConnectTests(unittest.TestCase):
    def setUp(self):
        from sqlalchemy.exc import IntegrityError
        import hmac
        import json
        self.user = SimpleNamespace(selected_organization_id=9)
        self.row = SimpleNamespace(id=17, name='SIP · ' + str(UUID(int=1)), credentials=STORED)
        self.db = SimpleNamespace(list_telephony_configurations_by_provider=AsyncMock(return_value=[]), create_telephony_configuration=AsyncMock(return_value=self.row), list_phone_numbers_for_config=AsyncMock(return_value=[]), update_telephony_configuration=AsyncMock(return_value=self.row))
        self.provider = SimpleNamespace(get_available_phone_numbers=AsyncMock(return_value=[CREDS['number']]))
        self.resolve = AsyncMock(return_value=self.provider)
        self.preprocess = AsyncMock(return_value=STORED)
        namespace = {'APIRouter': APIRouter, 'BaseModel': BaseModel, 'TelephonyConfigRequest': schema.SIPConfigurationRequest, 'UUID': UUID, 'Depends': Depends, 'get_user': lambda: self.user, 'UserModel': SimpleNamespace, 'HTTPException': HTTPException, 'db_client': self.db, '_credentials_from_payload': lambda payload: payload.model_dump(exclude={'provider', 'from_numbers'}), '_run_preprocess_hook': self.preprocess, 'get_telephony_provider_by_id': self.resolve, 'IntegrityError': IntegrityError, 'hmac': hmac, 'json': json, 'router': APIRouter()}
        tree = ast.parse((ROOT / 'routes/organization.py').read_text())
        selected = [node for node in tree.body if isinstance(node, (ast.ClassDef, ast.AsyncFunctionDef)) and node.name in ('TelephonyProviderConnectionRequest', 'connect_telephony_provider')]
        exec(compile(ast.Module(body=selected, type_ignores=[]), 'actual-connect-route', 'exec'), namespace)
        app = FastAPI()
        app.include_router(namespace['router'])
        self.client = TestClient(app)
        self.enabled = patch.dict(os.environ, {'SYSEVO_SIP_GATEWAY_ENABLED': 'true'})
        self.enabled.start()
        self.addCleanup(self.enabled.stop)

    def connect(self):
        return self.client.post('/telephony-providers/connect', json={'connection_key': str(UUID(int=1)), 'config': {'provider': 'sip', **CREDS}})

    def test_registered_response_contains_handles_and_never_credentials(self):
        response = self.connect()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {'config_id': 17, 'status': 'registered', 'numbers': [{'e164': CREDS['number'], 'label': '', 'caps': ['Voice']}]})
        self.assertNotIn(CREDS['password'], response.text)
        self.assertEqual(self.db.create_telephony_configuration.await_args.kwargs['organization_id'], 9)
        self.resolve.assert_awaited_once_with(17, 9)

    def test_pending_is_not_a_connected_number_and_retry_uses_same_account(self):
        self.provider.get_available_phone_numbers.side_effect = HTTPException(409, 'Not registered')
        response = self.connect()
        self.assertEqual(response.json(), {'config_id': 17, 'status': 'pending', 'numbers': []})
        self.db.list_telephony_configurations_by_provider.return_value = [self.row]
        self.provider.get_available_phone_numbers.side_effect = None
        self.provider.get_available_phone_numbers.return_value = [CREDS['number']]
        self.assertEqual(self.connect().json()['status'], 'registered')
        self.db.create_telephony_configuration.assert_awaited_once()

    def test_rejects_changed_credentials_under_an_existing_retry_key(self):
        self.row.credentials = {**STORED, 'password': 'other-secret'}
        self.db.list_telephony_configurations_by_provider.return_value = [self.row]
        self.db.list_phone_numbers_for_config.return_value = [SimpleNamespace(id=42)]
        self.assertEqual(self.connect().status_code, 409)
        self.resolve.assert_not_awaited()

    def test_corrections_to_pending_credentials_reuse_the_same_account(self):
        self.row.credentials = {**STORED, 'password': 'previous-typo'}
        self.db.list_telephony_configurations_by_provider.return_value = [self.row]
        self.assertEqual(self.connect().status_code, 200)
        self.db.create_telephony_configuration.assert_not_awaited()
        self.db.update_telephony_configuration.assert_awaited_once_with(17, 9, credentials=STORED)

    def test_disabled_gateway_and_missing_org_do_not_write_credentials(self):
        with patch.dict(os.environ, {'SYSEVO_SIP_GATEWAY_ENABLED': 'false'}):
            self.assertEqual(self.connect().status_code, 503)
        self.user.selected_organization_id = None
        self.assertEqual(self.connect().status_code, 400)
        self.db.create_telephony_configuration.assert_not_awaited()


if __name__ == '__main__':
    unittest.main()
