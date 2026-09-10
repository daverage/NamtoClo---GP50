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

import argparse,json,re,time,math
from dataclasses import asdict
from pathlib import Path
import numpy as np
import soundfile as sf
from distiller_v2_dsp import SR,controls_to_a,render_full,write_clo,warm
from distiller_v2_data import load_pairs,group_models,budget,load_audio,proof_keys,level_sweep_groups,flatten_groups
from distiller_v2_fit import distill,score,level_response_report

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

def _db_ratio(num,den):
    return 20.0*math.log10(max(float(num),1e-15)/max(float(den),1e-15))

def _real_audio(audio):
    """Prefer real guitar clips for final output-level calibration.

    Synthetic probes are valuable for fitting/diagnostics, but a single
    impulse or stress probe can have a crest factor unlike real playing and
    should not be allowed to clamp the final audible output gain by itself.
    """
    real=[z for z in audio if not bool(getattr(z[2],'synthetic',False))]
    return real or list(audio)

def _peak_safe_gain_cap(audio,a,pk,b,margin_db=0.25):
    """Return a conservative gain cap derived only from calibration material.

    A pure RMS match can make a distorted CLO sound more clipped by lifting
    transient/high-percentile levels above the NAM even when average level is
    correct. For every clip we compare both absolute sample peak and the
    99.9th percentile absolute amplitude. The strictest real-guitar clip wins.
    """
    caps=[];details=[]
    for x,t,pair in audio:
        pred=render_full(x,a,pk,b)
        n=min(len(pred),len(t));pred=np.asarray(pred[:n]);target=np.asarray(t[:n])
        if n==0:continue
        pa=np.abs(pred);ta=np.abs(target)
        p_peak=float(np.max(pa));t_peak=float(np.max(ta))
        p_q=float(np.quantile(pa,0.999));t_q=float(np.quantile(ta,0.999))
        peak_cap=_db_ratio(t_peak,p_peak);q_cap=_db_ratio(t_q,p_q);cap=min(peak_cap,q_cap)+float(margin_db)
        caps.append(cap)
        details.append({
            'task_id':getattr(pair,'task_id',''),'input_level_db':float(getattr(pair,'level_offset_db',0.0)),
            'synthetic':bool(getattr(pair,'synthetic',False)),
            'peak_cap_db':peak_cap,'p999_cap_db':q_cap,'cap_with_margin_db':cap,
            'pred_peak':p_peak,'target_peak':t_peak,'pred_p999':p_q,'target_p999':t_q,
        })
    return (min(caps) if caps else float('inf')),details

def _calibrate_output_gain(audio,a,pk,b,max_abs_db=12.0,peak_margin_db=0.25):
    """Apply one final linear output-gain correction to B, safely.

    RMS level supplies the desired correction, but real-guitar selection-set
    peak and 99.9th-percentile envelopes cap positive gain so calibration
    cannot turn a dynamics mismatch into extra clipping. Peak safety is a cap,
    never a request to attenuate an already-too-quiet model. Benchmark audio
    is never used.
    """
    if not audio:return b.copy(),0.0,None,None,{}
    source=list(audio);cal_audio=_real_audio(source)
    before=score(cal_audio,a,pk,b)
    requested_db=float(np.clip(-before.signed_level_db,-max_abs_db,max_abs_db))
    peak_cap_db,peak_details=_peak_safe_gain_cap(cal_audio,a,pk,b,peak_margin_db)
    if requested_db>0.0:
        # A negative peak cap means at least one transient is already above
        # the NAM envelope. In that case the safest correction is no boost,
        # not reversing the requested positive correction into attenuation.
        gain_db=float(np.clip(min(requested_db,max(0.0,peak_cap_db)),0.0,max_abs_db))
    else:
        # Attenuation only reduces peaks, so no peak-safety cap is required.
        gain_db=requested_db
    scaled=b*(10.0**(gain_db/20.0));after=score(cal_audio,a,pk,scaled)
    real_count=sum(1 for z in source if not bool(getattr(z[2],'synthetic',False)))
    info={
        'requested_rms_gain_db':requested_db,'peak_safe_cap_db':peak_cap_db,'peak_margin_db':peak_margin_db,
        'applied_gain_db':gain_db,'limited_by_peak_safety':bool(requested_db>0.0 and gain_db < requested_db-1e-9),'clips':peak_details,
        'clips_total':len(source),'clips_used':len(cal_audio),'real_clips_available':real_count,
        'excluded_synthetic_clips':len(source)-len(cal_audio),'calibration_material':'real-guitar' if real_count else 'all-selection',
    }
    return scaled,gain_db,before,after,info

def _group_task_ids(groups):
    return [[p.task_id for p in g] for g in groups]

def _print_level_report(label,r):
    if not r or not r.get('groups'):
        print(f'level response {label}: no matched sweep available');return
    print(f"level response {label}: RMSE={r['rmse_db']:.3f} dB max={r['max_abs_db']:.3f} dB ({r['groups']} group, {r['points']} comparisons)")
    for row in r.get('rows',[]):
        print(f"  {row['input_level_db']:+.0f} dB: NAM delta {row['nam_output_delta_db']:+.2f} dB, CLO delta {row['clo_output_delta_db']:+.2f} dB, response error {row['response_error_db']:+.2f} dB")

def run_model(ps,out,args):
    name=ps[0].model_name;print(f'\n=== {name} ===')
    fp=budget(ps,'fit',args.fit_seconds,.35,args.seed);sp=budget(ps,'selection',args.selection_seconds,.2,args.seed^0x51ec);bp=budget(ps,'benchmark',args.benchmark_seconds,0,args.seed^0xb3ac)
    fit=load_audio(fp);sel=load_audio(sp);bench=load_audio(bp)
    # These matched sweeps are additional P/K-only nonlinear evidence. They
    # never enter the B least-squares solve. Benchmark sweeps are selected here
    # but deliberately not loaded/rendered until all coefficients are frozen.
    lfg=level_sweep_groups(ps,'fit',args.level_groups,args.seed^0x1e7e1)
    lsg=level_sweep_groups(ps,'selection',args.level_groups,args.seed^0x51e71)
    lbg=level_sweep_groups(ps,'benchmark',args.level_groups,args.seed^0xb3e71)
    level_fit=load_audio(flatten_groups(lfg));level_sel=load_audio(flatten_groups(lsg))
    print(f'material fit={sum(p.duration_s for p in fp):.1f}s selection={sum(p.duration_s for p in sp):.1f}s benchmark={sum(p.duration_s for p in bp):.1f}s')
    print(f'level sweeps fit={len(lfg)} selection={len(lsg)} benchmark={len(lbg)} weight={args.level_response_weight:g} pk-passes={args.pk_passes}')
    t=time.monotonic();best=distill(
        fit,sel,args.a_controls,args.rounds,pause_ms=args.yield_ms,
        level_fit=level_fit,level_sel=level_sel,level_weight=args.level_response_weight,pk_passes=args.pk_passes
    );a=controls_to_a(best.controls_db)
    print('final P/K:', ' '.join(f'{v:.6g}' for v in best.pk))
    b_uncalibrated=best.b.copy();pre_metrics=_metrics_for_roles(fit,sel,bench,a,best.pk,b_uncalibrated)
    if args.no_level_calibration:
        b_storage=b_uncalibrated.copy();gain_db=0.0;cal_before=cal_after=None;cal_info={'disabled':True}
    else:
        b_storage,gain_db,cal_before,cal_after,cal_info=_calibrate_output_gain(sel or fit,a,best.pk,b_uncalibrated,peak_margin_db=args.peak_margin_db)
        req=cal_info.get('requested_rms_gain_db',gain_db);cap=cal_info.get('peak_safe_cap_db',gain_db)
        limited=' PEAK-LIMITED' if cal_info.get('limited_by_peak_safety') else ''
        material=cal_info.get('calibration_material','selection')
        print(f'output gain calibration ({material}): requested {req:+.2f} dB, cap {cap:+.2f} dB, applied {gain_db:+.2f} dB{limited}')
        print(f'  calibration level {cal_before.signed_level_db:+.2f}->{cal_after.signed_level_db:+.2f} dB, clips {cal_info.get("clips_used",0)}/{cal_info.get("clips_total",0)}')
    final_metrics=_metrics_for_roles(fit,sel,bench,a,best.pk,b_storage);bm=final_metrics['benchmark']
    # Benchmark matched-level material is touched only after A/P/K/B and final
    # calibration are frozen, so it cannot influence coefficient selection.
    level_bench=load_audio(flatten_groups(lbg))
    level_reports={
        'fit':level_response_report(level_fit,a,best.pk,b_storage),
        'selection':level_response_report(level_sel,a,best.pk,b_storage),
        'benchmark':level_response_report(level_bench,a,best.pk,b_storage),
    }
    _print_level_report('fit',level_reports['fit']);_print_level_report('selection',level_reports['selection']);_print_level_report('benchmark',level_reports['benchmark'])
    d=out/safe(ps[0].model_key+'__'+name);clo=d/'distilled.clo';write_clo(clo,a,best.pk,b_storage)
    previews={}
    for role,aud in [('fit',fit),('selection',sel),('benchmark',bench)]:
        if not aud:continue
        x,y,p=aud[0];pred=render_full(x,a,best.pk,b_storage)
        for tag,z in [('input',x),('nam',y),('clo',pred)]:sf.write(d/f'preview_{role}_{tag}.wav',np.asarray(z,np.float32),SR,subtype='FLOAT')
        previews[role]=p.task_id
    report={
        'method':'clean-sheet-varpro-v1-device-domain-level-aware-pk',
        'model_name':name,'model_key':ps[0].model_key,'tone_id':ps[0].tone_id,'model_id':ps[0].model_id,
        'A128':a,'pk':best.pk,'B512_uncalibrated':b_uncalibrated,'B512_device':b_storage,
        'optimizer_fit_metrics':best.fit,'optimizer_selection_metrics':best.selection,
        'optimizer_level_response':{'fit_rmse_db':best.fit_level_response_db,'selection_rmse_db':best.selection_level_response_db,'weight':args.level_response_weight,'pk_passes':args.pk_passes},
        'pre_calibration_metrics':pre_metrics,'output_gain_calibration_db':gain_db,
        'output_gain_calibration_source':'selection' if sel else 'fit','output_gain_calibration':cal_info,
        'fit_metrics':final_metrics['fit'],'selection_metrics':final_metrics['selection'],'benchmark_metrics':bm,
        'level_response':level_reports,
        'level_sweep_task_ids':{'fit':_group_task_ids(lfg),'selection':_group_task_ids(lsg),'benchmark':_group_task_ids(lbg)},
        'serialized_domain_metrics':final_metrics,'storage_b_scale':1.0,
        'cpu_controls':{'threads':args.threads,'yield_ms':args.yield_ms},
        'elapsed_seconds':time.monotonic()-t,'clo_path':str(clo),'preview_tasks':previews
    }
    dump(d/'report.json',report)
    if bm:print(f'benchmark: composite={bm.composite:.4f} ESR={bm.esr:.4f} level={bm.signed_level_db:+.2f} dB')
    print('CLO:',clo);return report

def main():
    p=argparse.ArgumentParser();p.add_argument('--teacher-root',default='~/NamtoCloTeacherDataset');p.add_argument('--output',default='~/NamtoCloDistillerProof');p.add_argument('--proof5',action='store_true');p.add_argument('--model-regex');p.add_argument('--list-models',action='store_true');p.add_argument('--fit-seconds',type=float,default=30);p.add_argument('--selection-seconds',type=float,default=15);p.add_argument('--benchmark-seconds',type=float,default=30);p.add_argument('--a-controls',type=int,default=24);p.add_argument('--rounds',type=int,default=3);p.add_argument('--seed',type=int,default=260910);p.add_argument('--threads',type=int,default=0,help='Cap native math-library worker threads; 0 keeps library defaults.');p.add_argument('--yield-ms',type=float,default=0.0,help='Sleep this many ms after each optimisation candidate to reduce sustained CPU load.');p.add_argument('--no-level-calibration',action='store_true',help='Do not apply the final selection-derived B output-gain correction.');p.add_argument('--peak-margin-db',type=float,default=.25,help='Allowed CLO overshoot above selection NAM peak/p99.9 during final level calibration.');p.add_argument('--level-groups',type=int,default=1,help='Matched real-guitar input-level sweep groups per role for nonlinear P/K fitting and diagnostics.');p.add_argument('--level-response-weight',type=float,default=.08,help='P/K and selection-gate penalty per dB RMS input-level response error.');p.add_argument('--pk-passes',type=int,default=3,help='Maximum P/K coordinate passes per round when matched-level evidence is available.');a=p.parse_args()
    if a.threads<0:raise SystemExit('--threads must be >= 0')
    if a.yield_ms<0:raise SystemExit('--yield-ms must be >= 0')
    if a.peak_margin_db<0:raise SystemExit('--peak-margin-db must be >= 0')
    if a.level_groups<0:raise SystemExit('--level-groups must be >= 0')
    if a.level_response_weight<0:raise SystemExit('--level-response-weight must be >= 0')
    if a.pk_passes<1:raise SystemExit('--pk-passes must be >= 1')
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
    dump(out/'summary.json',{'results':results,'failures':fail,'cpu_controls':{'threads':a.threads,'yield_ms':a.yield_ms},'level_controls':{'groups':a.level_groups,'weight':a.level_response_weight,'pk_passes':a.pk_passes}});return 1 if fail else 0
if __name__=='__main__':raise SystemExit(main())