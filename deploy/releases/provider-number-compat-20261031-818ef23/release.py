"""Image-only API release, preserving the actual running base and Compose stack.

prepare builds and tests without switching production. apply refuses drift or
active calls. Runtime state stores hashes/identifiers only, never credentials.
"""
import hashlib,json,os,subprocess,sys,time
from pathlib import Path

ROOT=Path(__file__).resolve().parent
MANIFEST=json.loads((ROOT/'manifest.json').read_text())
STATE=ROOT/'.runtime-prepared.json'
API='dograh-api-1';WORKER='dograh-worker-1'

def run(args,timeout=90):
 result=subprocess.run(args,capture_output=True,text=True,timeout=timeout)
 if result.returncode:
  raise RuntimeError('Command failed: '+args[0]+' (output withheld; credentials may be present)')
 return result.stdout

def logged(args,name,timeout=180):
 with (ROOT/name).open('w') as output:
  result=subprocess.run(args,stdout=output,stderr=subprocess.STDOUT,timeout=timeout)
 if result.returncode:raise RuntimeError(name+' failed; review the private server-side log')

def inspect(name):return json.loads(run(['docker','inspect',name]))[0]
def digest(value):return hashlib.sha256(json.dumps(value,sort_keys=True).encode()).hexdigest()
def image(name):return json.loads(run(['docker','image','inspect',name]))[0]

def sources(container=None,tag=None):
 code='from pathlib import Path;import json,hashlib;print(json.dumps({p:hashlib.sha256(Path(p).read_bytes()).hexdigest() for p in '+repr(list(MANIFEST['new_source_hashes']))+' if Path(p).exists()}))'
 args=['docker','exec',container,'python','-c',code] if container else ['docker','run','--rm','--network','none','--entrypoint','python',tag,'-c',code]
 return json.loads(run(args))

def compose(api,extra=None):
 labels=api['Config']['Labels'];files=labels['com.docker.compose.project.config_files'].split(',')
 if extra:files.append(str(extra))
 args=['docker','compose','--project-directory',labels['com.docker.compose.project.working_dir'],'-p',labels['com.docker.compose.project']]
 for file in files:
  if not Path(file).is_file():raise RuntimeError('An active Compose file is missing')
  args+=['-f',file]
 return args

def configuration(api,extra=None):return json.loads(run(compose(api,extra)+['config','--format','json']))

def verify_configuration(api,extra):
 before=configuration(api);after=configuration(api,extra)
 before['services']['api']['image']=after['services']['api']['image']
 if before!=after:raise RuntimeError('Compose changes more than the API image')
 actual=dict(pair.split('=',1) for pair in api['Config']['Env'])
 declared=before['services']['api'].get('environment',{})
 differing=[key for key,value in declared.items() if (value is None and key in actual) or (value is not None and actual.get(key)!=str(value))]
 if differing:raise RuntimeError('Compose environment differs from running container; key names only: '+','.join(sorted(differing)))

def active_calls():
 # Same live modes as call_concurrency.LIVE_CALL_MODES. Persistent textchat
 # conversations intentionally retain running state; they are not live audio.
 sql="SELECT count(*) FROM workflow_runs WHERE mode IN ('ari','plivo','twilio','vonage','vobiz','cloudonix','telnyx','webrtc','smallwebrtc') AND is_completed IS NOT TRUE AND (state='running' OR (state='initialized' AND created_at>now()-interval '2 minutes'))"
 result=run(['docker','exec','dograh-postgres-1','sh','-c','psql -U "$POSTGRES_USER" -d "${POSTGRES_DB:-$POSTGRES_USER}" -Atc "$1"','sh',sql])
 return int(result.strip())

def runtime_activity():
 code="""import os,json,redis
from pathlib import Path
r=redis.Redis.from_url(os.environ['REDIS_URL'])
slots=sum(r.zcard(key) for key in r.scan_iter('concurrent_calls:*'))
ports=range(int(os.getenv('UVICORN_BASE_PORT','8000')),int(os.getenv('UVICORN_BASE_PORT','8000'))+int(os.getenv('FASTAPI_WORKERS','1')))
connections=0
for name in ['tcp','tcp6']:
 for line in Path('/proc/net/'+name).read_text().splitlines()[1:]:
  fields=line.split()
  if fields[3]=='01' and int(fields[1].split(':')[-1],16) in ports:connections+=1
print(json.dumps({'call_slots':slots,'api_connections':connections}))"""
 return json.loads(run(['docker','exec',API,'python','-c',code]))

def fingerprint(api):
 config={**api['Config'],'Env':sorted(api['Config']['Env'])}
 # Docker may emit the same mounts in either order across inspect calls.
 mounts=sorted(api['Mounts'],key=lambda mount:mount['Destination'])
 return digest({'Config':config,'HostConfig':api['HostConfig'],'Mounts':mounts})

def prepare():
 api=inspect(API);worker=inspect(WORKER);current=sources(container=API)
 if all(current.get(key)==value for key,value in MANIFEST['new_source_hashes'].items()):
  print('Already running the reviewed number-cap sources');return
 if any(current.get(key)!=value for key,value in MANIFEST['old_source_hashes'].items()):raise RuntimeError('Running source changed; rebase before preparing')
 base=api['Image'];base_tag='dograh-local/provider-number-caps-base:'+base.split(':')[-1][:16]
 candidate='dograh-local/provider-number-caps:20261031-'+MANIFEST['canonical_commit'][:7]+'-'+base.split(':')[-1][:12]
 run(['docker','tag',base,base_tag])
 if image(base_tag)['Id']!=base:raise RuntimeError('Base tag does not resolve to actual running image')
 logged(['docker','build','--network','none','--build-arg','BASE_IMAGE='+base_tag,'-t',candidate,str(ROOT)],'build.log',300)
 old=image(base);new=image(candidate)
 if new['RootFS']['Layers'][:len(old['RootFS']['Layers'])]!=old['RootFS']['Layers']:raise RuntimeError('Candidate lost running image layers')
 for key in ['Env','Cmd','Entrypoint','WorkingDir','User','Healthcheck','Volumes']:
  if new['Config'].get(key)!=old['Config'].get(key):raise RuntimeError('Candidate changed inherited image configuration: '+key)
 if sources(tag=candidate)!=MANIFEST['new_source_hashes']:raise RuntimeError('Candidate source hashes do not match reviewed main')
 logged(['docker','run','--rm','--network','none','--entrypoint','python',candidate,'-m','unittest','api.tests.test_subscription_imported_numbers','-v'],'candidate-tests.log')
 logged(['docker','run','--rm','--network','none','--entrypoint','python','-e','ENVIRONMENT=test','-e','DATABASE_URL=postgresql+asyncpg://test:test@localhost:5432/test','-e','REDIS_URL=redis://localhost:6379/0',candidate,'-c','import api.app; print("PASS application import")'],'candidate-import.log')
 override=ROOT/'.runtime-compose.json';override.write_text(json.dumps({'services':{'api':{'image':candidate}}}));os.chmod(override,0o600)
 verify_configuration(api,override)
 if inspect(API)['Image']!=base or inspect(WORKER)['Image']!=worker['Image']:raise RuntimeError('Running image changed during preparation; rebuild from current image')
 state={'api_id':api['Id'],'api_base_image':base,'api_base_tag':base_tag,'api_fingerprint':fingerprint(api),
  'worker_id':worker['Id'],'worker_image':worker['Image'],'candidate':candidate,'candidate_image':new['Id'],
  'override':str(override),'compose_digest':digest(configuration(api)),'source_commit':MANIFEST['canonical_commit']}
 STATE.write_text(json.dumps(state,indent=2)+'\n');os.chmod(STATE,0o600)
 print(json.dumps({'prepared':True,'candidate':candidate,'retained_base_layers':len(old['RootFS']['Layers']),'offline_tests':9,'application_import':True,'worker_changes':False,'active_calls':active_calls(),'runtime_activity':runtime_activity()}))

def apply():
 state=json.loads(STATE.read_text());api=inspect(API);worker=inspect(WORKER)
 if api['Id']!=state['api_id'] or api['Image']!=state['api_base_image'] or fingerprint(api)!=state['api_fingerprint']:raise RuntimeError('API changed; prepare again against the current service')
 if worker['Id']!=state['worker_id'] or worker['Image']!=state['worker_image']:raise RuntimeError('Worker changed; review concurrent release before applying')
 override=Path(state['override']);verify_configuration(api,override)
 if digest(configuration(api))!=state['compose_digest']:raise RuntimeError('Compose changed during review')
 current=sources(container=API)
 if any(current.get(key)!=value for key,value in MANIFEST['old_source_hashes'].items()):raise RuntimeError('Running source changed')
 if active_calls()!=0 or any(runtime_activity().values()):raise RuntimeError('Live audio activity present; no service was changed')
 dirty_before=run(['git','-C','/root/SYSEVO/dograh','status','--porcelain'])
 logged(compose(api,override)+['up','-d','--no-deps','--no-build','api'],'apply.log',180)
 for _ in range(24):
  now=inspect(API)
  if now['State'].get('Health',{}).get('Status')=='healthy':break
  time.sleep(2)
 else:raise RuntimeError('API did not become healthy; retain prepared state for controlled rollback')
 now=inspect(API);worker_after=inspect(WORKER)
 if now['Image']!=state['candidate_image'] or sources(container=API)!=MANIFEST['new_source_hashes']:raise RuntimeError('Running API source verification failed')
 if sorted(now['Config']['Env'])!=sorted(api['Config']['Env']):raise RuntimeError('Runtime environment changed')
 for key in ['Cmd','Entrypoint','WorkingDir','User']:
  if now['Config'].get(key)!=api['Config'].get(key):raise RuntimeError('Runtime configuration changed: '+key)
 for key in ['RestartPolicy','Memory','NanoCpus','PortBindings']:
  if now['HostConfig'].get(key)!=api['HostConfig'].get(key):raise RuntimeError('Runtime service settings changed: '+key)
 mounts=lambda item:sorted((m['Type'],m['Source'],m['Destination'],m['RW']) for m in item['Mounts'])
 if mounts(now)!=mounts(api):raise RuntimeError('Runtime mounts changed')
 if worker_after['Id']!=worker['Id'] or worker_after['Image']!=worker['Image']:raise RuntimeError('Worker was unexpectedly changed')
 if run(['git','-C','/root/SYSEVO/dograh','status','--porcelain'])!=dirty_before:raise RuntimeError('Unrelated working tree changed during switch; inspect concurrent writer')
 print(json.dumps({'applied':True,'api_image':now['Image'],'worker_unchanged':True,'environment_unchanged':True,'mounts_unchanged':True,'sources_verified':True,'git_work_preserved':True}))

def rollback():
 state=json.loads(STATE.read_text());api=inspect(API);worker=inspect(WORKER)
 if api['Image']!=state['candidate_image'] or worker['Id']!=state['worker_id']:raise RuntimeError('Concurrent service change prevents automatic rollback')
 if active_calls()!=0 or any(runtime_activity().values()):raise RuntimeError('Live audio activity prevents automatic rollback')
 override=ROOT/'.runtime-rollback.json';override.write_text(json.dumps({'services':{'api':{'image':state['api_base_tag']}}}));os.chmod(override,0o600)
 verify_configuration(api,override)
 logged(compose(api,override)+['up','-d','--no-deps','--no-build','api'],'rollback.log',180)
 for _ in range(24):
  now=inspect(API)
  if now['State'].get('Health',{}).get('Status')=='healthy' and now['Image']==state['api_base_image']:
   print('Restored previous API image; worker unchanged');return
  time.sleep(2)
 raise RuntimeError('Rollback health verification needs immediate operator review')

if __name__=='__main__':
 if len(sys.argv)!=2 or sys.argv[1] not in ['prepare','apply','rollback']:raise SystemExit('Use prepare, apply or rollback')
 try:globals()[sys.argv[1]]()
 except Exception as error:
  if sys.argv[1]=='apply' and STATE.exists() and inspect(API)['Image']==json.loads(STATE.read_text())['candidate_image']:
   try:rollback()
   except Exception as recovery:raise SystemExit(str(error)+'; '+str(recovery))
  raise SystemExit(str(error))
