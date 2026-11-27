//! Fixed notebook kernel requests for the Kaggle service lifecycle.

pub(super) const STATE_CODE: &str = r#"
import json, pathlib, os
root = pathlib.Path('/kaggle/working/agent-hire-decision'); cache = pathlib.Path('/tmp/nimble-q8-cache')
state = json.loads((root/'state.json').read_text()) if (root/'state.json').exists() else {}
size = 0
for p in (cache/'models').rglob('*') if (cache/'models').exists() else []:
    try:
        size += p.stat().st_size if p.is_file() else 0
    except OSError:
        pass
legacy = pathlib.Path('/tmp/nimble_t4_20261010/models/Bespoke-Nimble-9B-v3-Q8_0.gguf')
if legacy.exists(): size = max(size, legacy.stat().st_size)
log = ''
if (root/'supervisor.log').exists(): log = (root/'supervisor.log').read_text()[-600:]
cls = json.loads((root/'classifier_state.json').read_text()) if (root/'classifier_state.json').exists() else {}
if cls.get('pid'):
    try:
        os.kill(cls['pid'], 0)
    except OSError:
        cls['phase'] = 'stopped'
if cls.get('phase') == 'failed' and (root/'classifier.log').exists(): cls['log'] = (root/'classifier.log').read_text()[-400:]
print(json.dumps({'phase': state.get('phase'), 'deployment_id': state.get('deployment_id'), 'error': state.get('error'),
                  'replicas': state.get('replicas'), 'gpus': state.get('gpus'), 'downloaded': size, 'log': log,
                  'classifiers': cls}))
"#;

pub(super) const START_CODE: &str = r#"
import base64, json, pathlib, subprocess, sys
meta = get_ipython().kernel.get_parent()['metadata']
root = pathlib.Path('/kaggle/working/agent-hire-decision'); root.mkdir(parents=True, exist_ok=True)
(root/'remote_service.py').write_bytes(base64.b64decode(meta['service']))
old = json.loads((root/'state.json').read_text()) if (root/'state.json').exists() else {}
if old.get('phase') not in (None, 'failed', 'stopped'):
    sys.path.insert(0, str(root)); import remote_service; remote_service.dependencies(); remote_service.stop_owned(old)
# A stopped deployment needs a few seconds to release its ports; starting on top of it fails.
import socket, time
def _free(port):
    with socket.socket() as s:
        try:
            s.bind(('127.0.0.1', port)); return True
        except OSError:
            return False
deadline = time.time() + 45
while time.time() < deadline and not all(_free(p) for p in (8073, 8801, 8802)):
    time.sleep(1)
p = subprocess.Popen([sys.executable, str(root/'remote_service.py'), 'supervise'], stdin=subprocess.PIPE,
                     stdout=open(root/'supervisor.log', 'w'), stderr=subprocess.STDOUT, start_new_session=True, text=True)
p.stdin.write(json.dumps(meta['config']) + '\n'); p.stdin.close()
print(json.dumps({'pid': p.pid}))
"#;

pub(super) const CLASSIFY_CODE: &str = r#"
import json, concurrent.futures, urllib.request, urllib.error
meta = get_ipython().kernel.get_parent()['metadata']
def ask(body):
    req = urllib.request.Request('http://127.0.0.1:8073/v1/systemone', data=json.dumps(body).encode(),
                                 headers={'Content-Type': 'application/json', 'Authorization': 'Bearer ' + meta['key']})
    try:
        with urllib.request.urlopen(req, timeout=240) as r: return json.loads(r.read())
    except urllib.error.HTTPError as e:
        return {'error': 'http_' + str(e.code), 'detail': e.read().decode('utf-8', 'replace')[:300]}
    except Exception as e: return {'error': type(e).__name__}
with concurrent.futures.ThreadPoolExecutor(min(16, len(meta['bodies']))) as ex:
    out = list(ex.map(ask, meta['bodies']))
print(json.dumps(out))
"#;

pub(super) const STOP_CODE: &str = r#"
import json, pathlib, sys
root = pathlib.Path('/kaggle/working/agent-hire-decision')
state = json.loads((root/'state.json').read_text()) if (root/'state.json').exists() else {}
stopped = False
if state:
    sys.path.insert(0, str(root)); import remote_service; remote_service.dependencies(); stopped = remote_service.stop_owned(state)
print(json.dumps({'stopped': stopped}))
"#;

pub(super) const SIZES_CODE: &str = r#"
import json, os
meta = get_ipython().kernel.get_parent()['metadata']
print(json.dumps({p: (os.path.getsize(p) if os.path.exists(p) else -1) for p in meta['paths']}))
"#;

pub(super) const CLASSIFIER_START_CODE: &str = r#"
import base64, json, os, pathlib, signal, subprocess, sys, time
meta = get_ipython().kernel.get_parent()['metadata']
root = pathlib.Path('/kaggle/working/agent-hire-decision'); root.mkdir(parents=True, exist_ok=True)
service = root/'classifier_service.py'
service.write_bytes(base64.b64decode(meta['service']))
old = json.loads((root/'classifier_state.json').read_text()) if (root/'classifier_state.json').exists() else {}
if old.get('pid'):
    try:
        if 'classifier_service.py' in open(f"/proc/{old['pid']}/cmdline").read():
            os.killpg(os.getpgid(old['pid']), signal.SIGTERM); time.sleep(3)
    except (OSError, ProcessLookupError):
        pass
(root/'classifier_state.json').unlink(missing_ok=True)
n = len(subprocess.check_output(['nvidia-smi', '-L'], text=True).strip().splitlines())
config = dict(meta['config'], gpus=[n - 1] if n >= 2 else [0])
p = subprocess.Popen([sys.executable, str(service), 'serve'], stdin=subprocess.PIPE, stdout=open(root/'classifier.log', 'w'),
                     stderr=subprocess.STDOUT, start_new_session=True, text=True)
p.stdin.write(json.dumps(config) + '\n'); p.stdin.close()
print(json.dumps({'pid': p.pid, 'gpus': config['gpus']}))
"#;

pub(super) const CLASSIFIER_STOP_CODE: &str = r#"
import json, os, pathlib, signal
root = pathlib.Path('/kaggle/working/agent-hire-decision')
state = json.loads((root/'classifier_state.json').read_text()) if (root/'classifier_state.json').exists() else {}
stopped = False
if state.get('pid'):
    try:
        if 'classifier_service.py' in open(f"/proc/{state['pid']}/cmdline").read():
            os.killpg(os.getpgid(state['pid']), signal.SIGTERM); stopped = True
    except (OSError, ProcessLookupError):
        pass
print(json.dumps({'stopped': stopped}))
"#;

pub(super) const ENCODE_CODE: &str = r#"
import json, urllib.request
meta = get_ipython().kernel.get_parent()['metadata']
req = urllib.request.Request('http://127.0.0.1:8074/classify',
                             data=json.dumps({'model': meta['model'], 'texts': meta['texts']}).encode(),
                             headers={'Content-Type': 'application/json', 'Authorization': 'Bearer ' + meta['key']})
with urllib.request.urlopen(req, timeout=600) as r:
    print(r.read().decode())
"#;
