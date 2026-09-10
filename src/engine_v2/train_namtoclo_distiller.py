#!/usr/bin/env python3
from __future__ import annotations
import os,sys

def _early_thread_cap(argv):
    for i,arg in enumerate(argv):
        if arg.startswith('--threads='):
            try:return max(1,int(arg.split('=',1)[1]))
            except ValueError:return None
        if arg=='--threads' and i+1<len(argv):
            try:return max(1,int(argv[i+1]))
            except ValueError:return None
    return None

# Apply native math-library limits before importing numpy. This covers the
# common macOS Accelerate/OpenBLAS/OMP backends without adding a dependency.
_THREAD_CAP=_early_thread_cap(sys.argv[1:])
if _THREAD_CAP:
    for _var in ('VECLIB_MAXIMUM_THREADS','OMP_NUM_THREADS','OPENBLAS_NUM_THREADS','MKL_NUM_THREADS','NUMEXPR_NUM_THREADS','NUMBA_NUM_THREADS'):
        os.environ[_var]=str(_THREAD_CAP)

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

def _metrics_for_roles(fit,sel,bench,a,pk,b):
    return {
        'fit':score(fit,a,pk,b) if fit else None,
        'selection':score(sel,a,pk,b) if sel else None,
        'benchmark':score(bench,a,pk,b) if bench else None,
    }

def _calibrate_output_gain(audio,a,pk,b,max_abs_db=12.0):
    """Apply one final linear output-gain correction to B.

    The final B block is linear, so a constant level error should not be left
    for P/K or the A controls to absorb.  Calibration uses selection material
    only (fit material when no selection split exists), never benchmark audio.
    The clamp prevents pathological silent/broken clips causing huge gains.
    """
    if not audio:return b.copy(),0.0,None,None
    before=score(audio,a,pk,b)
    gain_db=float(np.clip(-before.signed_level_db,-max_abs_db,max_abs_db))
    scaled=b*(10.0**(gain_db/20.0))
    after=score(audio,a,pk,scaled)
    return scaled,gain_db,before,after

def run_model(ps,out,args):
    name=ps[0].model_name;print(f'\n=== {name} ===')
    fp=budget(ps,'fit',args.fit_seconds,.35,args.seed);sp=budget(ps,'selection',args.selection_seconds,.2,args.seed^0x51ec);bp=budget(ps,'benchmark',args.benchmark_seconds,0,args.seed^0xb3ac)
    fit=load_audio(fp);sel=load_audio(sp);bench=load_audio(bp);print(f'material fit={sum(p.duration_s for p in fp):.1f}s selection={sum(p.duration_s for p in sp):.1f}s benchmark={sum(p.duration_s for p in bp):.1f}s')
    t=time.monotonic();best=distill(fit,sel,args.a_controls,args.rounds,pause_ms=args.yield_ms);a=controls_to_a(best.controls_db)
    b_uncalibrated=best.b.copy();pre_metrics=_metrics_for_roles(fit,sel,bench,a,best.pk,b_uncalibrated)
    if args.no_level_calibration:
        b_storage=b_uncalibrated.copy();gain_db=0.0;cal_before=cal_after=None
    else:
        b_storage,gain_db,cal_before,cal_after=_calibrate_output_gain(sel or fit,a,best.pk,b_uncalibrated)
        print(f'output gain calibration: {gain_db:+.2f} dB (selection level {cal_before.signed_level_db:+.2f}->{cal_after.signed_level_db:+.2f} dB)')
    final_metrics=_metrics_for_roles(fit,sel,bench,a,best.pk,b_storage);bm=final_metrics['benchmark']
    d=out/safe(ps[0].model_key+'__'+name);clo=d/'distilled.clo';write_clo(clo,a,best.pk,b_storage)
    previews={}
    for role,aud in [('fit',fit),('selection',sel),('benchmark',bench)]:
        if not aud:continue
        x,y,p=aud[0];pred=render_full(x,a,best.pk,b_storage)
        for tag,z in [('input',x),('nam',y),('clo',pred)]:sf.write(d/f'preview_{role}_{tag}.wav',np.asarray(z,np.float32),SR,subtype='FLOAT')
        previews[role]=p.task_id
    report={
        'method':'clean-sheet-varpro-v1-device-domain',
        'model_name':name,'model_key':ps[0].model_key,'tone_id':ps[0].tone_id,'model_id':ps[0].model_id,
        'A128':a,'pk':best.pk,'B512_uncalibrated':b_uncalibrated,'B512_device':b_storage,
        'optimizer_fit_metrics':best.fit,'optimizer_selection_metrics':best.selection,
        'pre_calibration_metrics':pre_metrics,
        'output_gain_calibration_db':gain_db,
        'output_gain_calibration_source':'selection' if sel else 'fit',
        'fit_metrics':final_metrics['fit'],'selection_metrics':final_metrics['selection'],'benchmark_metrics':bm,
        'serialized_domain_metrics':final_metrics,'storage_b_scale':1.0,
        'cpu_controls':{'threads':args.threads,'yield_ms':args.yield_ms},
        'elapsed_seconds':time.monotonic()-t,'clo_path':str(clo),'preview_tasks':previews
    }
    dump(d/'report.json',report)
    if bm:print(f'benchmark: composite={bm.composite:.4f} ESR={bm.esr:.4f} level={bm.signed_level_db:+.2f} dB')
    print('CLO:',clo);return report

def main():
    p=argparse.ArgumentParser();p.add_argument('--teacher-root',default='~/NamtoCloTeacherDataset');p.add_argument('--output',default='~/NamtoCloDistillerProof');p.add_argument('--proof5',action='store_true');p.add_argument('--model-regex');p.add_argument('--list-models',action='store_true');p.add_argument('--fit-seconds',type=float,default=30);p.add_argument('--selection-seconds',type=float,default=15);p.add_argument('--benchmark-seconds',type=float,default=30);p.add_argument('--a-controls',type=int,default=24);p.add_argument('--rounds',type=int,default=3);p.add_argument('--seed',type=int,default=260910);p.add_argument('--threads',type=int,default=0,help='Cap native math-library worker threads; 0 keeps library defaults.');p.add_argument('--yield-ms',type=float,default=0.0,help='Sleep this many ms after each optimisation candidate to reduce sustained CPU load.');p.add_argument('--no-level-calibration',action='store_true',help='Do not apply the final selection-derived B output-gain correction.');a=p.parse_args()
    if a.threads<0:raise SystemExit('--threads must be >= 0')
    if a.yield_ms<0:raise SystemExit('--yield-ms must be >= 0')
    root=Path(a.teacher_root).expanduser();out=Path(a.output).expanduser();groups=group_models(load_pairs(root))
    if a.list_models:
        for k in sorted(groups):print(groups[k][0].nam_split,groups[k][0].model_name,k)
        return 0
    if a.proof5:keys=proof_keys(groups)
    elif a.model_regex:
        rx=re.compile(a.model_regex,re.I);keys=[k for k in sorted(groups) if rx.search(groups[k][0].model_name)]
    else:raise SystemExit('Choose --proof5, --model-regex REGEX, or --list-models')
    if not keys:raise SystemExit('No models matched')
    out.mkdir(parents=True,exist_ok=True);print('selected:',*[groups[k][0].model_name for k in keys],sep='\n  ')
    if a.threads:print(f'native math thread cap: {a.threads}')
    if a.yield_ms:print(f'CPU yield: {a.yield_ms:g} ms/candidate')
    print('warming JIT...');warm()
    results=[];fail=[]
    for k in keys:
        try:results.append(run_model(groups[k],out,a))
        except Exception as e:print('FAILED',groups[k][0].model_name,e);fail.append({'key':k,'error':str(e)})
    dump(out/'summary.json',{'results':results,'failures':fail,'cpu_controls':{'threads':a.threads,'yield_ms':a.yield_ms}});return 1 if fail else 0
if __name__=='__main__':raise SystemExit(main())
