from __future__ import annotations
import hashlib,json,re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable,Optional
import numpy as np
import soundfile as sf
from distiller_v2_dsp import SR

@dataclass(frozen=True)
class Pair:
    task_id:str; model_key:str; model_name:str; tone_id:Optional[int]; model_id:Optional[int]
    nam_split:str; role:str; dataset:str; synthetic:bool; input_path:str; target_path:str; duration_s:float

def load_pairs(root:Path)->list[Pair]:
    p=root/'tasks.jsonl'
    if not p.exists():raise FileNotFoundError(f'Missing {p}')
    out=[]
    for line in p.open(encoding='utf-8'):
        if not line.strip():continue
        r=json.loads(line);n=r['nam'];i=r['input'];ip=Path(r.get('student_input','')).expanduser();tp=Path(r.get('student_target','')).expanduser()
        if not ip.exists() or not tp.exists():continue
        key=f"tone{n.get('tone_id')}_model{n.get('model_id')}_{str(n.get('sha256',''))[:10]}"
        out.append(Pair(str(r.get('task_id','')),key,str(n.get('model_name') or key),n.get('tone_id'),n.get('model_id'),str(n.get('split') or 'unknown'),str(i.get('role') or 'fit'),str(i.get('dataset') or 'unknown'),bool(i.get('synthetic',False)),str(ip),str(tp),float(i.get('duration_s') or 0.)))
    if not out:raise RuntimeError('No usable 44.1-kHz teacher pairs')
    return out

def group_models(pairs:Iterable[Pair])->dict[str,list[Pair]]:
    g={}
    for p in pairs:g.setdefault(p.model_key,[]).append(p)
    return g

def budget(pairs:list[Pair],role:str,seconds:float,synth_fraction:float,seed:int)->list[Pair]:
    cand=[p for p in pairs if p.role==role]
    def h(p):return hashlib.sha256(f'{seed}|{p.task_id}'.encode()).hexdigest()
    syn=sorted([p for p in cand if p.synthetic],key=h);real=sorted([p for p in cand if not p.synthetic],key=h);out=[]
    def take(pool,target):
        used=0.
        for p in pool:
            if used>=target and out:break
            out.append(p);used+=max(.001,p.duration_s)
        return used
    a=take(syn,seconds*synth_fraction) if synth_fraction else 0.;used=a+take(real,max(0.,seconds-a))
    chosen={p.task_id for p in out}
    for p in sorted([p for p in cand if p.task_id not in chosen],key=h):
        if used>=seconds:break
        out.append(p);used+=max(.001,p.duration_s)
    return out

def read_pair(p:Pair):
    def rd(path):
        x,sr=sf.read(path,dtype='float32',always_2d=False)
        if sr!=SR:raise RuntimeError(f'Expected 44100 Hz: {path}')
        if x.ndim==2:x=x.mean(axis=1)
        return np.asarray(x,dtype=np.float64)
    x=rd(p.input_path);y=rd(p.target_path);n=min(len(x),len(y));return x[:n],y[:n],p

def load_audio(pairs:list[Pair]):return [read_pair(p) for p in pairs if p.duration_s>0]

def proof_keys(groups:dict[str,list[Pair]])->list[str]:
    keys=sorted(k for k,v in groups.items() if v and v[0].nam_split=='development');chosen=[]
    for pats in ([r'JC.?120',r'Clean'],[r'EDGE',r'EOB',r'Deluxe.*5'],[r'JCM.?800',r'CRUNCH'],[r'Rectifier',r'5150',r'Mark V',r'SLO'],[r'Big.?Muff',r'RAT']):
        for k in keys:
            if k not in chosen and any(re.search(p,groups[k][0].model_name,re.I) for p in pats):chosen.append(k);break
    for k in keys:
        if len(chosen)>=5:break
        if k not in chosen:chosen.append(k)
    return chosen[:5]
