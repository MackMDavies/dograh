"""Execute the real API-key auth function with synthetic DB/service dependencies."""
import ast,sys,types,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from api.services.subscription_api_meter import NativeApiMeterError

class HTTPError(Exception):
    def __init__(self,status_code,detail):self.status_code=status_code;self.detail=detail

class NativeAuthTests(unittest.IsolatedAsyncioTestCase):
    async def exercise(self, *, included=True, valid=True):
        events=[]
        key=SimpleNamespace(id=7,organization_id=42,created_by=8,key_prefix='synthetic')
        user=SimpleNamespace(id=8,selected_organization_id=999)
        async def validate(_):return key if valid else None
        async def owner(_):return user
        async def meter(actual,db,*,is_mcp=False):
            self.assertIs(actual,key)
            events.append('monthly-meter')
            if isinstance(included,Exception):raise included
            return included
        counter=types.ModuleType('api.services.api_usage_counter');counter.record_api_request=lambda identity:events.append('postpaid-counter')
        bridge=types.ModuleType('api.services.subscription_api_meter');bridge.meter_native_api_request=meter;bridge.NativeApiMeterError=NativeApiMeterError
        path=Path(__file__).parents[1]/'services/auth/depends.py'
        function=next(node for node in ast.parse(path.read_text()).body if isinstance(node,ast.AsyncFunctionDef) and node.name=='_handle_api_key_auth')
        namespace={'UserModel':object,'HTTPException':HTTPError,'db_client':SimpleNamespace(validate_api_key=validate,get_user_by_id=owner),'logger':SimpleNamespace(debug=lambda *_:None)}
        exec(compile(ast.Module(body=[function],type_ignores=[]),str(path),'exec'),namespace)
        with patch.dict(sys.modules,{counter.__name__:counter,bridge.__name__:bridge}):
            try:return await namespace['_handle_api_key_auth']('synthetic-key'),events
            except HTTPError as error:return error,events

    async def test_admitted_self_serve_request_does_not_enter_postpaid_counter(self):
        user,events=await self.exercise()
        self.assertEqual(events,['monthly-meter'])
        self.assertEqual(user.selected_organization_id,42)
        self.assertEqual(user.api_key_id,7)

    async def test_legacy_keeps_postpaid_after_authoritative_meter_classification(self):
        _,events=await self.exercise(included=False)
        self.assertEqual(events,['monthly-meter','postpaid-counter'])

    async def test_exhausted_and_unavailable_requests_never_return_authenticated_access(self):
        for status in (429,403,503):
            error,events=await self.exercise(included=NativeApiMeterError(status,'safe denial'))
            self.assertIsInstance(error,HTTPError)
            self.assertEqual(error.status_code,status)
            self.assertEqual(events,['monthly-meter'])

    async def test_actual_voice_mcp_forwards_the_mcp_surface_for_both_key_headers(self):
        path=Path(__file__).parents[1]/'mcp_server/auth.py'
        function=next(node for node in ast.parse(path.read_text()).body if isinstance(node,ast.AsyncFunctionDef) and node.name=='authenticate_mcp_request')
        for headers in ({'x-api-key':'synthetic-key'},{'authorization':'Bearer synthetic-key'}):
            calls=[]
            async def auth(key,**kwargs):
                calls.append((key,kwargs))
                return SimpleNamespace(id=8,selected_organization_id=42)
            ns={'UserModel':object,'HTTPException':HTTPError,'get_http_headers':lambda **_:headers,
                '_handle_api_key_auth':auth,'trace':SimpleNamespace(get_current_span=lambda:SimpleNamespace(is_recording=lambda:False))}
            exec(compile(ast.Module(body=[function],type_ignores=[]),str(path),'exec'),ns)
            await ns['authenticate_mcp_request']()
            self.assertEqual(calls,[('synthetic-key',{'is_mcp':True})])

    async def test_browser_jwt_auth_never_enters_the_native_key_meter(self):
        import __future__
        path=Path(__file__).parents[1]/'services/auth/depends.py'
        function=next(node for node in ast.parse(path.read_text()).body if isinstance(node,ast.AsyncFunctionDef) and node.name=='get_user')
        user=object()
        async def jwt(_):return user
        async def native(*_,**__):self.fail('Browser traffic entered developer-key accounting')
        ns={'AUTH_PROVIDER':'supabase','_handle_supabase_auth':jwt,'_handle_api_key_auth':native}
        exec(compile(ast.Module(body=[function],type_ignores=[]),str(path),'exec',flags=__future__.annotations.compiler_flag),ns)
        self.assertIs(await ns['get_user'](authorization='Bearer synthetic-session'),user)

    async def test_invalid_keys_are_rejected_before_metering(self):
        error,events=await self.exercise(valid=False)
        self.assertEqual(error.status_code,401)
        self.assertEqual(events,[])

if __name__=='__main__':unittest.main()
