"""Detached Kaggle service: pinned Nimble Q8 replicas, authenticated gateway, optional ngrok.

Vendored from nimble-kaggle-deploy. Agent-Hire runs it WITHOUT ngrok: with no `ngrok_token` in the
config the tunnel is skipped and the app reaches the gateway on 127.0.0.1 through the notebook's own
Jupyter kernel, so only the Kaggle connection link is needed.
"""
import argparse,concurrent.futures,hashlib,hmac,importlib.util,json,os,pathlib,secrets,signal,socket,subprocess,sys,tarfile,threading,time,urllib.error,urllib.parse,urllib.request
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer

ROOT=pathlib.Path(__file__).resolve().parent
CACHE=pathlib.Path('/tmp/nimble-q8-cache')
MODEL_REV='a72bdbadb355ca3014f7f9d2585380917cc59971'
MODEL_FILE='Bespoke-Nimble-9B-v3-Q8_0.gguf'
MODEL_BYTES=9527503392
RUNTIME_TAG='b11429'
RUNTIME_SHA='b13c64b9224d3c89d9993945d70cc21177ad00178c21b8d34237e3bc139834f1'
# CUDA 12 runtime libraries for the same build. Kaggle images moved to CUDA 13, where the CUDA-12
# llama.cpp build cannot find libcudart/libcublas and silently falls back to the CPU (~6 tokens/s).
# Shipping the runtime makes the build depend only on the GPU driver.
CUDART_SHA='f4ab593e31507320b121f2cb21791f4c72420a2483d3df8bf2ca1a97d04e6845'
PACKAGES=[
 ('https://archive.ubuntu.com/ubuntu/pool/main/g/glibc/libc6_2.39-0ubuntu8.9_amd64.deb','ff5557d99b51f761c4b7c92368b9cc45565eda17df9bf9eb4b134d09825008be'),
 ('https://archive.ubuntu.com/ubuntu/pool/main/g/gcc-14/libstdc++6_14.2.0-4ubuntu2~24.04.1_amd64.deb','a51f8de7829211db961a31f02158058ad1a95f92ac6d0a5dff6350e2821c54c0'),
 ('https://archive.ubuntu.com/ubuntu/pool/main/g/gcc-14/libgcc-s1_14.2.0-4ubuntu2~24.04.1_amd64.deb','aa7fadbe33b78bcf99885318040601c550c208929565b179891d9a3cc2aa68cd')]
STOP=threading.Event()

def dependencies():
    target=ROOT/'python-deps';sys.path.insert(0,str(target))
    if any(importlib.util.find_spec(m) is None for m in ['requests','huggingface_hub','psutil']):
        target.mkdir(exist_ok=True)
        subprocess.run([sys.executable,'-m','pip','install','--disable-pip-version-check','--no-cache-dir','--target',str(target),'requests>=2.32,<3','huggingface_hub>=0.30,<2','psutil>=5.9,<8'],check=True)

def write_state(state):
    temp=ROOT/'state.partial';temp.write_text(json.dumps(state,indent=2));temp.replace(ROOT/'state.json')

def digest(path):
    h=hashlib.sha256()
    with path.open('rb') as f:
        for b in iter(lambda:f.read(1024*1024),b''):h.update(b)
    return h.hexdigest()

def fetch(url,path,sha=None):
    path.parent.mkdir(parents=True,exist_ok=True)
    if path.exists() and (not sha or digest(path)==sha):return
    tmp=path.with_suffix(path.suffix+'.partial')
    # archive.ubuntu.com answers 403 to urllib's default 'Python-urllib/3.x' agent; a browser-style one is served.
    req=urllib.request.Request(url,headers={'User-Agent':'Mozilla/5.0 (X11; Linux x86_64) agent-hire-deploy'})
    with urllib.request.urlopen(req,timeout=90) as r,tmp.open('wb') as f:
        while True:
            b=r.read(1024*1024)
            if not b:break
            f.write(b)
    if sha and digest(tmp)!=sha:tmp.unlink();raise ValueError('Checksum mismatch for '+path.name)
    tmp.replace(path)

def install_runtime():
    legacy=pathlib.Path('/tmp/nimble_t4_20261010')
    if (legacy/'prebuilt/llama-b11429/llama-server').exists() and (legacy/'private-runtime/usr/lib64/ld-linux-x86-64.so.2').exists():
        archive=legacy/'prebuilt/server.tar.gz'
        base=legacy if archive.exists() and digest(archive)==RUNTIME_SHA else CACHE
    else:base=CACHE
    archive=base/'prebuilt/server.tar.gz'
    binary=base/'prebuilt/llama-b11429/llama-server'
    fetch(f'https://github.com/ggml-org/llama.cpp/releases/download/{RUNTIME_TAG}/llama-{RUNTIME_TAG}-bin-ubuntu-cuda-12.8-x64.tar.gz',archive,RUNTIME_SHA)
    if not binary.exists():
        with tarfile.open(archive) as t:t.extractall(archive.parent,filter='data')
    private=base/'private-runtime';private.mkdir(exist_ok=True)
    for url,sha in PACKAGES:
        name=url.rsplit('/',1)[1];p=base/'packages'/name
        # Superseded Ubuntu builds leave archive.ubuntu.com; Launchpad keeps every published build.
        # The pinned SHA-256 is checked either way, so the fallback cannot change the bytes.
        try:fetch(url,p,sha)
        except urllib.error.HTTPError as e:
            if e.code not in (403,404):raise  # removed (404) or refused (403): Launchpad keeps every build
            fetch('https://launchpad.net/ubuntu/+archive/primary/+files/'+urllib.parse.quote(name,safe='~+'),p,sha)
        subprocess.run(['dpkg-deb','-x',str(p),str(private)],check=True,stdout=subprocess.DEVNULL)
    cudart_archive=CACHE/'prebuilt/cudart-12.8.tar.gz'
    fetch(f'https://github.com/ggml-org/llama.cpp/releases/download/{RUNTIME_TAG}/cudart-llama-{RUNTIME_TAG}-bin-ubuntu-cuda-12.8-x64.tar.gz',cudart_archive,CUDART_SHA)
    cudart=CACHE/'cudart-12.8'
    if not any(cudart.rglob('libcudart.so.12')):
        cudart.mkdir(parents=True,exist_ok=True)
        with tarfile.open(cudart_archive) as t:t.extractall(cudart,filter='data')
    cudart_dirs=sorted({p.parent for p in cudart.rglob('*.so*')})
    libs=':'.join(map(str,[private/'usr/lib/x86_64-linux-gnu',private/'lib/x86_64-linux-gnu',*cudart_dirs,binary.parent,pathlib.Path('/usr/local/nvidia/lib64'),pathlib.Path('/usr/local/cuda/lib64')]))
    prefix=[str(private/'usr/lib64/ld-linux-x86-64.so.2'),'--library-path',libs]
    subprocess.run([*prefix,str(binary),'--version'],cwd=binary.parent,check=True,stdout=subprocess.DEVNULL)
    devices=subprocess.run([*prefix,str(binary),'--list-devices'],cwd=binary.parent,capture_output=True,text=True,timeout=120).stdout
    if 'CUDA0' not in devices:raise RuntimeError('llama.cpp cannot see the GPU (CUDA backend not loaded); refusing to run the model on the CPU')
    return binary,prefix

def install_model():
    legacy=pathlib.Path('/tmp/nimble_t4_20261010/models')/MODEL_FILE
    if legacy.exists() and legacy.stat().st_size==MODEL_BYTES:return legacy
    from huggingface_hub import hf_hub_download
    path=pathlib.Path(hf_hub_download('ggml-org/Bespoke-Nimble-9B-v3-GGUF',MODEL_FILE,revision=MODEL_REV,local_dir=CACHE/'models',cache_dir=CACHE/'hf-cache'))
    if path.stat().st_size!=MODEL_BYTES:raise ValueError('Unexpected Q8 model size')
    return path

def install_ngrok(method):
    if method=='apt':
        if os.geteuid()!=0:raise RuntimeError('The apt installation option requires root; use --ngrok-install portable')
        fetch('https://ngrok-agent.s3.amazonaws.com/ngrok.asc',pathlib.Path('/etc/apt/trusted.gpg.d/ngrok.asc'))
        pathlib.Path('/etc/apt/sources.list.d/ngrok.list').write_text('deb https://ngrok-agent.s3.amazonaws.com bookworm main\n')
        subprocess.run(['apt-get','update'],check=True,timeout=300)
        subprocess.run(['apt-get','install','-y','ngrok'],check=True,timeout=300)
        return pathlib.Path('/usr/local/bin/ngrok') if pathlib.Path('/usr/local/bin/ngrok').exists() else pathlib.Path('/usr/bin/ngrok')
    binary=CACHE/'ngrok/ngrok'
    if not binary.exists():
        archive=CACHE/'ngrok/ngrok.tgz';fetch('https://bin.equinox.io/c/bNyj1mQVY4c/ngrok-v3-stable-linux-amd64.tgz',archive)
        with tarfile.open(archive) as t:t.extractall(binary.parent,filter='data')
        binary.chmod(0o755)
    subprocess.run([str(binary),'version'],check=True)
    return binary

def stop_owned(state,wait=15):
    """Signal only our supervisor after checking PID creation time and exact argv."""
    import psutil
    pid=state.get('pid')
    if not pid:return False
    try:
        p=psutil.Process(pid)
        if abs(p.create_time()-state['process_created'])>.01 or str(ROOT/'remote_service.py') not in p.cmdline() or 'supervise' not in p.cmdline():return False
        family=p.children(recursive=True)+[p];p.terminate()
        _,alive=psutil.wait_procs(family,timeout=wait)
        for child in alive:
            try:child.kill()
            except psutil.NoSuchProcess:pass
        return True
    except psutil.NoSuchProcess:return False

class Gateway:
    def __init__(self,backends,key,deployment_id,max_inflight=16):
        import requests
        self.backends=backends;self.key=key;self.deployment_id=deployment_id
        self.lock=threading.Lock();self.next=0;self.slots=threading.BoundedSemaphore(max_inflight);self.local=threading.local()
    def session(self):
        import requests
        if not hasattr(self.local,'session'):self.local.session=requests.Session()
        return self.local.session
    def backend(self):
        with self.lock:
            url=self.backends[self.next%len(self.backends)];self.next+=1;return url
    def ready(self):
        for url in self.backends:
            try:
                if self.session().get(url+'/health',timeout=3).status_code!=200:return False
            except Exception:return False
        return True

def gateway(port,backends,deployment_id,key,max_inflight,serve=True):
    gate=Gateway(backends,key,deployment_id,max_inflight)
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def reply(self,status,body,headers=None):
            data=json.dumps(body,allow_nan=False).encode();self.send_response(status);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(data)))
            if headers:
                for k,v in headers.items():self.send_header(k,v)
            self.end_headers();self.wfile.write(data)
        def authenticated(self):
            got=self.headers.get('Authorization','');return hmac.compare_digest(got.encode('utf-8'),('Bearer '+gate.key).encode('utf-8'))
        def do_GET(self):
            if self.path=='/health':
                ready=gate.ready();self.reply(200 if ready else 503,{'ready':ready,'model':'nimble-q8','deployment_id':gate.deployment_id});return
            if not self.authenticated():self.reply(401,{'error':'A valid Bearer API key is required'});return
            if self.path=='/v1/models':self.reply(200,{'object':'list','data':[{'id':'nimble-q8','object':'model','owned_by':'local'}]});return
            self.reply(404,{'error':'Unknown endpoint'})
        def do_POST(self):
            if not self.authenticated():self.reply(401,{'error':'A valid Bearer API key is required'});return
            if self.path!='/v1/systemone':self.reply(404,{'error':'Unknown endpoint'});return
            try:
                n=int(self.headers.get('Content-Length','0'))
                if not 0<n<=2*1024*1024:self.reply(413,{'error':'Body must be between 1 byte and 2 MiB'});return
                self.connection.settimeout(20)
                body=json.loads(self.rfile.read(n),parse_constant=lambda s:(_ for _ in ()).throw(ValueError('Non-finite JSON')))
                if not isinstance(body,dict) or 'state' not in body or not isinstance(body.get('questions'),dict) or not body['questions']:raise ValueError('state and a nonempty questions object are required')
            except (ValueError,TimeoutError):self.reply(400,{'error':'Invalid System One JSON request'});return
            if not gate.slots.acquire(blocking=False):self.reply(429,{'error':'Server is busy; retry shortly'},{'Retry-After':'1'});return
            try:
                r=gate.session().post(gate.backend()+'/v1/systemone',json=body,timeout=(5,240))
                self.reply(r.status_code,r.json())
            except Exception:self.reply(502,{'error':'Model backend is unavailable'})
            finally:gate.slots.release()
    server=ThreadingHTTPServer(('127.0.0.1',port),Handler);server.daemon_threads=True
    if serve:server.serve_forever()
    return server

def supervise(config):
    dependencies()
    import psutil,requests
    ROOT.mkdir(parents=True,exist_ok=True);CACHE.mkdir(parents=True,exist_ok=True)
    state={'deployment_id':config['deployment_id'],'pid':os.getpid(),'process_created':psutil.Process().create_time(),'phase':'installing','url':config['domain'],'port':config['port'],'model':'nimble-q8','model_revision':MODEL_REV,'runtime':RUNTIME_TAG,'children':[]}
    procs=[]
    def phase(name,**extra):state.update(phase=name,**extra);write_state(state);print(name,flush=True)
    def spawn(name,cmd,env=None,cwd=None):
        p=subprocess.Popen(cmd,env=env,cwd=cwd,stdout=open(ROOT/(name+'.log'),'w'),stderr=subprocess.STDOUT,start_new_session=True);procs.append(p);state['children'].append({'name':name,'pid':p.pid});write_state(state);return p
    def wait_ready(url,p,seconds=300):
        deadline=time.monotonic()+seconds
        while time.monotonic()<deadline and not STOP.is_set():
            if p.poll() is not None:raise RuntimeError('Child process exited; inspect its log')
            try:
                if requests.get(url+'/health',timeout=3).status_code==200:return
            except requests.RequestException:pass
            STOP.wait(1)
        raise RuntimeError('Service readiness timed out')
    signal.signal(signal.SIGTERM,lambda *_:STOP.set());signal.signal(signal.SIGINT,lambda *_:STOP.set())
    try:
        # Refuse occupied ports rather than interfering with unrelated notebook work.
        tunnel_wanted=bool(config.get('ngrok_token'))
        for port in [config['port'],8801,8802]+([4041] if tunnel_wanted else []):
            with socket.socket() as s:
                try:s.bind(('127.0.0.1',port))
                except OSError:raise RuntimeError('Required local port is already occupied: '+str(port))
        gpu_info=subprocess.check_output(['nvidia-smi','--query-gpu=index,name,memory.total,memory.used','--format=csv,noheader,nounits'],text=True).strip().splitlines()
        if not gpu_info:raise RuntimeError('Enable a GPU in Kaggle')
        replicas=min(len(gpu_info),2) if config['replicas']=='auto' else int(config['replicas'])
        if replicas>len(gpu_info):raise RuntimeError('Requested more replicas than available GPUs')
        for line in gpu_info[:replicas]:
            fields=[s.strip() for s in line.split(',')]
            if float(fields[2])-float(fields[3])<12000:raise RuntimeError('At least 12,000 MiB free VRAM per selected GPU is required')
        state.update(replicas=replicas,gpus=gpu_info[:replicas]);write_state(state)
        with concurrent.futures.ThreadPoolExecutor(3) as ex:
            fr=ex.submit(install_runtime);fm=ex.submit(install_model)
            fn=ex.submit(install_ngrok,config.get('ngrok_install','portable')) if tunnel_wanted else None
            binary,prefix=fr.result();model=fm.result();ngrok=fn.result() if fn else None
        phase('starting_models')
        backends=[]
        for i in range(replicas):
            env=os.environ.copy();env.update(CUDA_VISIBLE_DEVICES=str(i),OMP_NUM_THREADS='2',CUDA_CACHE_PATH=str(CACHE/'cuda-cache'))
            cmd=[*prefix,str(binary),'--model',str(model),'--alias','nimble-q8','--n-gpu-layers','99','--split-mode','none','--parallel','8','--ctx-size','32768','--batch-size','4096','--ubatch-size','512','--flash-attn','auto','--cache-type-k','f16','--cache-type-v','f16','--cache-ram','0','--no-cache-idle-slots','--threads','2','--threads-batch','2','--metrics','--host','127.0.0.1','--port',str(8801+i)]
            (ROOT/f'model-{i}-command.json').write_text(json.dumps({'command':cmd,'gpu':i},indent=2))
            p=spawn('model-'+str(i),cmd,env,binary.parent);backends.append('http://127.0.0.1:'+str(8801+i))
        for url,p in zip(backends,procs[-replicas:]):wait_ready(url,p)
        phase('warming_models')
        warm={'state':'Distributed systems engineer building software services.','questions':{'software':{'type':'noul','instructions':'Does this role develop software?'}}}
        with concurrent.futures.ThreadPoolExecutor(replicas) as ex:
            fs=[ex.submit(requests.post,u+'/v1/systemone',json=warm,timeout=180) for u in backends]
            for f in fs:
                r=f.result();r.raise_for_status()
                if r.json()['answers']['software']['noul']<.5:raise RuntimeError('Model control failed')
        env=os.environ.copy();env['NIMBLE_API_KEY']=config['api_key']
        gate=spawn('gateway',[sys.executable,str(ROOT/'remote_service.py'),'gateway','--port',str(config['port']),'--backends',*backends,'--deployment-id',config['deployment_id'],'--max-inflight',str(config['max_inflight'])],env)
        wait_ready('http://127.0.0.1:'+str(config['port']),gate,30)
        if not tunnel_wanted:
            phase('ready')
            config.clear()
            while not STOP.wait(2):
                if any(p.poll() is not None for p in procs):raise RuntimeError('A service child stopped unexpectedly')
            return
        ngrok_config={'version':3,'agent':{'web_addr':'127.0.0.1:4041','inspect_db_size':0,'update_check':False,'remote_management':False,'log':'stdout','log_format':'json'},'endpoints':[{'name':'nimble-q8','url':config['domain'],'upstream':{'url':str(config['port'])}}]}
        ngrok_path=ROOT/'ngrok.yml';ngrok_path.write_text(json.dumps(ngrok_config,indent=2));ngrok_path.chmod(0o600)
        subprocess.run([str(ngrok),'config','check','--config',str(ngrok_path)],check=True)
        phase('starting_ngrok')
        env=os.environ.copy();env['NGROK_AUTHTOKEN']=config['ngrok_token']
        tunnel=spawn('ngrok',[str(ngrok),'start','nimble-q8','--config',str(ngrok_path)],env)
        env.clear()
        deadline=time.monotonic()+60
        while time.monotonic()<deadline and not STOP.is_set():
            if tunnel.poll() is not None:raise RuntimeError('ngrok exited; inspect ngrok.log for the account/domain error')
            try:
                tunnels=requests.get('http://127.0.0.1:4041/api/tunnels',timeout=3).json()['tunnels']
                if any(t.get('public_url','').rstrip('/')==config['domain'].rstrip('/') for t in tunnels):break
            except (requests.RequestException,KeyError,ValueError):pass
            STOP.wait(1)
        else:raise RuntimeError('ngrok did not publish the requested domain')
        phase('ready')
        config.clear()  # Credentials now live only in their child process environments.
        while not STOP.wait(2):
            if any(p.poll() is not None for p in procs):raise RuntimeError('A service child stopped unexpectedly')
    except Exception as e:
        message=str(e)
        for name in ['ngrok_token','api_key']:
            if config.get(name):message=message.replace(config[name],'[REDACTED]')
        if STOP.is_set():phase('stopped')
        else:phase('failed',error=message)
    finally:
        for p in reversed(procs):
            try:
                parent=psutil.Process(p.pid);family=parent.children(recursive=True)+[parent]
                for child in family:
                    try:child.terminate()
                    except psutil.NoSuchProcess:pass
                _,alive=psutil.wait_procs(family,timeout=8)
                for child in alive:
                    try:child.kill()
                    except psutil.NoSuchProcess:pass
            except psutil.NoSuchProcess:pass
        if state['phase']!='failed':phase('stopped')

if __name__=='__main__':
    ap=argparse.ArgumentParser();sub=ap.add_subparsers(dest='mode',required=True)
    sub.add_parser('supervise')
    g=sub.add_parser('gateway');g.add_argument('--port',type=int,required=True);g.add_argument('--backends',nargs='+',required=True);g.add_argument('--deployment-id',required=True);g.add_argument('--max-inflight',type=int,default=16)
    a=ap.parse_args()
    if a.mode=='supervise':supervise(json.loads(sys.stdin.readline()))
    else:
        dependencies();key=os.environ.pop('NIMBLE_API_KEY',None)
        if not key:raise SystemExit('NIMBLE_API_KEY is required')
        gateway(a.port,a.backends,a.deployment_id,key,a.max_inflight)
