import sys,os,json,pathlib,time,subprocess,types,hashlib,re,urllib.request,urllib.error
import socket
with socket.socket() as listener:
    listener.bind(('127.0.0.1',0))
    PORT=listener.getsockname()[1]
ROOT=pathlib.Path(__file__).resolve().parents[3]
LIVE=pathlib.Path(os.environ.get('STRATA_LIVE_REPO',str(ROOT)))
sys.path.insert(0,str(ROOT/'tools'))
import agent_turns_bench as h
OUT=pathlib.Path(sys.argv[2]);OUT.mkdir(parents=True,exist_ok=True)
configs=json.loads(pathlib.Path(sys.argv[1]).read_text())
work=h.turns()
request_class=urllib.request.Request
def request(url,*args,**kwargs):
    if kwargs.get('data'):
        data=json.loads(kwargs['data']);data['reasoning_effort']='low';data['reasoning_budget_tokens']=0;data['min_p']=0.05
        kwargs['data']=json.dumps(data).encode()
    return request_class(url,*args,**kwargs)
h.urllib=types.SimpleNamespace(request=types.SimpleNamespace(Request=request,urlopen=urllib.request.urlopen),error=urllib.error)
# Use the same server code and engine as the restored daily service; workload text was already read from the isolated branch.
h.ROOT=LIVE
rows=[];responses=[]
meta={
    'workload':'23 fixed real-repository tool-result continuations plus closed-loop read/search tasks',
    'source_commit':subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
    'engine_sha256':hashlib.sha256((LIVE/'engine/strata').read_bytes()).hexdigest(),
    'server_sha256':hashlib.sha256((LIVE/'serve/server.py').read_bytes()).hexdigest(),
    'sampling':{'temperature':1.0,'top_p':0.95,'top_k':20,'min_p':0.05,'seed':1234,
                'max_tokens':512,'reasoning_effort':'low','reasoning_budget_tokens':0},
    'order':[],
    'prompt_sha256':{tag:hashlib.sha256(json.dumps(msgs,sort_keys=True).encode()).hexdigest() for tag,msgs in work},
}
(OUT/'metadata.json').write_text(json.dumps(meta,indent=2))
def measured(model,rnd,tag,msgs,kind):
    started=time.perf_counter();r,ttft=h.post(PORT,msgs,512,1234);wall=(time.perf_counter()-started)*1000
    metric=h.turn_metrics(r.get('timings'),r.get('usage'),round(wall,1),ttft)
    choice=r['choices'][0]
    row={'model':model,'round':rnd,'turn':tag,'kind':kind,'finish_reason':choice['finish_reason'],**metric}
    rows.append(row);responses.append({'model':model,'round':rnd,'turn':tag,'message':choice['message']})
    with (OUT/'rows.jsonl').open('a') as f:f.write(json.dumps(row)+'\n')
    with (OUT/'responses.jsonl').open('a') as f:f.write(json.dumps(responses[-1])+'\n')
    print(model,rnd,kind,h.turn_line(tag,metric),'finish',choice['finish_reason'],flush=True)
    return choice
read_tools=[t for t in h.TOOLS if t['function']['name'] in ('read_file','grep')]
read_tools[0]=json.loads(json.dumps(read_tools[0]));read_tools[0]['function']['parameters']['properties'].update(start_line={'type':'integer'},max_lines={'type':'integer'})
def execute_tool(call):
    try:
        name=call['function']['name'];arg=json.loads(call['function']['arguments'])
        if name=='read_file':
            p=(ROOT/arg['path']).resolve();rel=p.relative_to(ROOT)
            if any(x.startswith('.') for x in rel.parts) or (rel.parts[0] not in ('src','include','serve','tools','docs') and str(rel) not in ('README.md','CMakeLists.txt','setup.py')):raise ValueError('read-only public source paths only')
            lines=p.read_text(errors='replace').splitlines();start=max(1,int(arg.get('start_line',1)));n=max(1,min(240,int(arg.get('max_lines',160))))
            return '\n'.join(f'{i+1}: {lines[i]}' for i in range(start-1,min(len(lines),start-1+n)))+f'\nTotal lines: {len(lines)}'
        if name=='grep':
            p=subprocess.run(['git','grep','-n','-I','-E','--',str(arg['pattern']),'src/','include/','serve/','tools/','docs/'],cwd=ROOT,text=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=20)
            return '\n'.join(p.stdout.splitlines()[:60]) or 'No matches.'
        return 'Only read_file and grep are available. Do not edit files or run commands.'
    except Exception as e:return 'Tool error: '+str(e)
live_tasks=[
('cache-live','Investigate how native expert cache hits, RAM experts and PCIe transfers are selected during decode. Use the read tools, cite exact files and lines, then explain the likely V100 bottleneck. Do not edit files.','src/core/expert_source.cpp',1880),
('stream-live','Trace how streamed tool calls are reconstructed and how incomplete JSON arguments are handled. Read the actual implementation and give a concise explanation with file and line citations. Do not edit files.','serve/server.py',2650),
('spec-live','Investigate how MTP draft acceptance and the verification expert cap affect output and decode speed. Inspect the implementation and distinguish exact optimizations from answer-changing approximations. Cite files and lines. Do not edit files.','src/core/verify.cpp',1)]
active=subprocess.run(['systemctl','--user','is-active','--quiet','strata-v100.service']).returncode==0
try:
    if active:subprocess.run(['systemctl','--user','stop','strata-v100.service'],check=True)
    starts=h.session_starts(work)
    for rnd in range(2):
        for model in (('iq3','nvfp4') if rnd==0 else ('nvfp4','iq3')):
            meta['order'].append({'model':model,'round':rnd});(OUT/'metadata.json').write_text(json.dumps(meta,indent=2))
            cfg=h.isolated_config(configs[model],model,rnd,OUT,PORT);paths=h.arm_paths(OUT,model,rnd);paths['dir'].mkdir(parents=True,exist_ok=True);paths['disk'].mkdir(exist_ok=True)
            cp=paths['dir']/'config.json';cp.write_text(json.dumps(cfg,indent=2));proc=h.start_arm(cp,PORT,paths['dir']/'server.log',dict(os.environ),'strata')
            try:
                t=time.monotonic();h.wait_ready(PORT,proc);print('READY',model,rnd,round(time.monotonic()-t,1),flush=True)
                original_tools=h.TOOLS
                for (tag,msgs),cold in zip(work,starts):measured(model,rnd,tag,msgs,'repo-cold' if cold else 'repo-followup')
                h.TOOLS=read_tools
                for tag,task,source,line in live_tasks:
                    seed_lines=(ROOT/source).read_text().splitlines()[line-1:line-1+800]
                    msgs=[{'role':'system','content':'You are a coding assistant. Investigate the repository using read_file and grep. Only read; do not modify or execute code. Give a concise answer with evidence after inspecting the relevant implementation.'},{'role':'user','content':task+'\nInitial real source excerpt from '+source+' starting at line '+str(line)+':\n'+'\n'.join(seed_lines)}]
                    completed=False
                    for step in range(8):
                        choice=measured(model,rnd,f'{tag}.{step}',msgs,'closed-loop-cold' if step==0 else 'closed-loop-followup')
                        message=choice['message'];msgs.append(message);calls=message.get('tool_calls') or []
                        if not calls:
                            completed=choice['finish_reason']=='stop';break
                        for call in calls:msgs.append({'role':'tool','tool_call_id':call['id'],'content':execute_tool(call)})
                    with (OUT/'tasks.jsonl').open('a') as f:f.write(json.dumps({'model':model,'round':rnd,'task':tag,'completed':completed,'requests':step+1})+'\n')
                h.TOOLS=original_tools
            finally:
                h.stop_arm(proc)
finally:
    if active:subprocess.run(['systemctl','--user','start','strata-v100.service'],check=True)
print('REAL REPOSITORY COMPARISON COMPLETE',len(rows),'requests',flush=True)
