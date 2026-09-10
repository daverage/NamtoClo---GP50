#!/usr/bin/env python3
from __future__ import annotations
import argparse,json,re,time
from dataclasses import asdict
from pathlib import Path
import numpy as np
import soundfile as sf
from distiller_v2_dsp import SR,controls_to_a,render_full,write_clo,warm
from distiller_v2_data import load_pairs,group_models,budget,load_audio,proof_keys
from distiller_v2_fit import distill,score

def safe(s):return re.sub(r'[^A-Za-z0-9._()+-]+','_',s).strip('_')[:110]
def dump(path,obj):
    def cv(x):
        if isinstance(x,np.ndarray):return x.tolist()
        if hasattr(x,'__dataclass_fields__'):return asdict(x)
        if isinstance(x,np.generic):return x.item()
        raise TypeError(type(x).__name__)
    path.parent.mkdir(parents=True,exist_ok=True);path.write_text(json.dumps(obj,indent=2,default=cv))

def run_model(ps,out,args):
    name=ps[0].model_name;print(f'\n=== {name} ===')
    fp=budget(ps,'fit',args.fit_seconds,.35,args.seed);sp=budget(ps,'selection',args.selection_seconds,.2,args.seed^0x51ec);bp=budget(ps,'benchmark',args.benchmark_seconds,0,args.seed^0xb3ac)
    fit=load_audio(fp);sel=load_audio(sp);bench=load_audio(bp);print(f'material fit={sum(p.duration_s for p in fp):.1f}s selection={sum(p.duration_s for p in sp):.1f}s benchmark={sum(p.duration_s for p in bp):.1f}s')
    t=time.monotonic();best=distill(fit,sel,args.a_controls,args.rounds);a=controls_to_a(best.controls_db);bm=score(bench,a,best.pk,best.b) if bench else None
    # The EngineV2 compact serializer stores trainer-domain B multiplied by 4.
    # Score that serialized coefficient domain separately so research reports
    # never confuse the fitted internal model with the actual bytes in the CLO.
    b_storage=best.b*4.0
    serialized_metrics={
        'fit':score(fit,a,best.pk,b_storage) if fit else None,
        'selection':score(sel,a,best.pk,b_storage) if sel else None,
        'benchmark':score(bench,a,best.pk,b_storage) if bench else None,
    }
    d=out/safe(ps[0].model_key+'__'+name);clo=d/'distilled.clo';write_clo(clo,a,best.pk,best.b)
    previews={}
    for role,aud in [('fit',fit),('selection',sel),('benchmark',bench)]:
        if not aud:continue
        x,y,p=aud[0];pred_internal=render_full(x,a,best.pk,best.b);pred_storage=render_full(x,a,best.pk,b_storage)
        for tag,z in [('input',x),('nam',y),('clo_internal',pred_internal),('clo_storage',pred_storage)]:sf.write(d/f'preview_{role}_{tag}.wav',np.asarray(z,np.float32),SR,subtype='FLOAT')
        previews[role]=p.task_id
    report={'method':'clean-sheet-varpro-v1','model_name':name,'model_key':ps[0].model_key,'tone_id':ps[0].tone_id,'model_id':ps[0].model_id,'A128':a,'pk':best.pk,'B512_internal':best.b,'B512_storage':b_storage,'fit_metrics':best.fit,'selection_metrics':best.selection,'benchmark_metrics':bm,'serialized_domain_metrics':serialized_metrics,'storage_b_scale':4.0,'elapsed_seconds':time.monotonic()-t,'clo_path':str(clo),'preview_tasks':previews}
    dump(d/'report.json',report)
    if bm:print(f'benchmark internal: composite={bm.composite:.4f} ESR={bm.esr:.4f} level={bm.signed_level_db:+.2f} dB')
    sm=serialized_metrics['benchmark']
    if sm:print(f'benchmark serialized: composite={sm.composite:.4f} ESR={sm.esr:.4f} level={sm.signed_level_db:+.2f} dB')
    print('CLO:',clo);return report

def main():
    p=argparse.ArgumentParser();p.add_argument('--teacher-root',default='~/NamtoCloTeacherDataset');p.add_argument('--output',default='~/NamtoCloDistillerProof');p.add_argument('--proof5',action='store_true');p.add_argument('--model-regex');p.add_argument('--list-models',action='store_true');p.add_argument('--fit-seconds',type=float,default=30);p.add_argument('--selection-seconds',type=float,default=15);p.add_argument('--benchmark-seconds',type=float,default=30);p.add_argument('--a-controls',type=int,default=24);p.add_argument('--rounds',type=int,default=3);p.add_argument('--seed',type=int,default=260910);a=p.parse_args()
    root=Path(a.teacher_root).expanduser();out=Path(a.output).expanduser();groups=group_models(load_pairs(root))
    if a.list_models:
        for k in sorted(groups):print(groups[k][0].nam_split,groups[k][0].model_name,k)
        return 0
    if a.proof5:keys=proof_keys(groups)
    elif a.model_regex:
        rx=re.compile(a.model_regex,re.I);keys=[k for k in sorted(groups) if rx.search(groups[k][0].model_name)]
    else:raise SystemExit('Choose --proof5, --model-regex REGEX, or --list-models')
    if not keys:raise SystemExit('No models matched')
    out.mkdir(parents=True,exist_ok=True);print('selected:',*[groups[k][0].model_name for k in keys],sep='\n  ');print('warming JIT...');warm()
    results=[];fail=[]
    for k in keys:
        try:results.append(run_model(groups[k],out,a))
        except Exception as e:print('FAILED',groups[k][0].model_name,e);fail.append({'key':k,'error':str(e)})
    dump(out/'summary.json',{'results':results,'failures':fail});return 1 if fail else 0
if __name__=='__main__':raise SystemExit(main())
