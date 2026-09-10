from __future__ import annotations
import math,time
from dataclasses import dataclass
import numpy as np
from distiller_v2_dsp import B_TAPS,controls_to_a,render_preb,render_full

@dataclass
class Metrics:
    composite:float
    esr:float
    spectral:float
    level_db:float
    signed_level_db:float

@dataclass
class Candidate:
    controls_db:np.ndarray
    pk:np.ndarray
    b:np.ndarray
    fit:Metrics
    selection:Metrics

@dataclass
class _FitWorkspace:
    nfft:int
    targets:list[np.ndarray]
    target_ffts:list[np.ndarray]
    lengths:list[int]


def _nextpow2(n:int)->int:
    return 1 << (max(1,n)-1).bit_length()


def _workspace(audio)->_FitWorkspace:
    if not audio:raise ValueError('empty audio workspace')
    nfft=_nextpow2(max(max(len(x),len(t)) for x,t,_ in audio)+B_TAPS)
    targets=[np.asarray(t,dtype=np.float64) for _,t,_ in audio]
    target_ffts=[np.fft.rfft(t,nfft) for t in targets]
    lengths=[min(len(x),len(t)) for x,t,_ in audio]
    return _FitWorkspace(nfft,targets,target_ffts,lengths)


def _aligned_esr(p,t,maxlag=256):
    n=min(len(p),len(t));p=p[:n];t=t[:n]
    if n<128:return 0.
    dec=max(1,n//100000);ps=p[::dec];ts=t[::dec];ml=max(1,maxlag//dec)
    # FFT cross-correlation is mathematically the same raw dot-product search
    # as the old +/-lag loop but avoids hundreds of long np.dot calls per clip.
    nf=_nextpow2(len(ps)+len(ts)-1)
    corr=np.fft.irfft(np.fft.rfft(ps,nf)*np.fft.rfft(ts[::-1],nf),nf)
    centre=len(ts)-1;lo=max(0,centre-ml);hi=min(len(ps)+len(ts)-1,centre+ml+1)
    lag0=(lo+int(np.argmax(corr[lo:hi]))-centre)*dec
    if lag0>=0:p,t=p[lag0:],t[:n-lag0]
    else:p,t=p[:n+lag0],t[-lag0:]
    den=float(np.dot(t,t));return float(np.dot(p-t,p-t)/den) if den>1e-20 else 0.


def _spectral(p,t):
    n=min(len(p),len(t),131072)
    if n<1024:return 0.
    p=p[:n];t=t[:n];pr=math.sqrt(float(np.mean(p*p))+1e-30);tr=math.sqrt(float(np.mean(t*t))+1e-30);p=p*(tr/max(pr,1e-15));w=np.hanning(n)
    P=np.abs(np.fft.rfft(p*w));T=np.abs(np.fft.rfft(t*w));mask=T>max(float(T.max())*1e-5,1e-10)
    return float(np.sqrt(np.mean(np.log(np.maximum(P[mask],1e-12)/T[mask])**2))) if np.any(mask) else 0.


def _score_predictions(preds,targets)->Metrics:
    if not preds:return Metrics(float('inf'),float('inf'),float('inf'),float('inf'),float('inf'))
    E=[];S=[];L=[];SL=[]
    for p,t in zip(preds,targets):
        E.append(_aligned_esr(p,t));S.append(_spectral(p,t));pr=math.sqrt(float(np.mean(p*p))+1e-30);tr=math.sqrt(float(np.mean(t*t))+1e-30);signed=20*math.log10(max(pr,1e-15)/max(tr,1e-15));SL.append(signed);L.append(abs(signed))
    e=float(np.mean(E));s=float(np.mean(S));l=float(np.mean(L));sl=float(np.mean(SL));return Metrics(.7*e+.3*s+.01*l,e,s,l,sl)


def score(audio,a,pk,b)->Metrics:
    if not audio:return Metrics(float('inf'),float('inf'),float('inf'),float('inf'),float('inf'))
    preds=[];targets=[]
    for x,t,_ in audio:
        preds.append(render_full(x,a,pk,b));targets.append(t)
    return _score_predictions(preds,targets)


def _solve_b_and_score(prebs:list[np.ndarray],ws:_FitWorkspace):
    """Solve B and score the fit while reusing each candidate's pre-B render.

    The old path rendered A+PK+Post once to solve B, then rendered the entire
    chain a second time and ran a 512-tap sample-by-sample FIR to score it.
    Here the already-computed pre-B FFTs are reused for both the least-squares
    solve and a mathematically equivalent FFT convolution of the solved B.
    """
    nfft=ws.nfft;w=1./len(prebs)
    num=np.zeros(nfft//2+1,np.complex128);den=np.zeros(nfft//2+1,dtype=np.float64);pffts=[]
    for p,T in zip(prebs,ws.target_ffts):
        P=np.fft.rfft(p,nfft);pffts.append(P);num+=w*np.conj(P)*T;den+=w*np.abs(P)**2
    eps=max(1e-3*float(np.mean(den)),1e-20);H=num/(den+eps)
    b=np.fft.irfft(H,nfft)[:B_TAPS].astype(np.float64)
    B=np.fft.rfft(b,nfft)
    preds=[np.fft.irfft(P*B,nfft)[:n] for P,n in zip(pffts,ws.lengths)]
    return b,_score_predictions(preds,ws.targets)


def _evaluate_fit(fit,ctrl,pk,ws:_FitWorkspace):
    a=controls_to_a(ctrl)
    prebs=[render_preb(x,a,pk) for x,_,_ in fit]
    b,m=_solve_b_and_score(prebs,ws)
    inf=Metrics(float('inf'),float('inf'),float('inf'),float('inf'),float('inf'))
    return Candidate(ctrl.copy(),pk.copy(),b,m,inf)


def _selection_score(audio,c:Candidate)->Metrics:
    a=controls_to_a(c.controls_db)
    # Selection is deliberately evaluated only at the seed and round gates.
    # It does not influence coordinate choices inside a round.
    return score(audio,a,c.pk,c.b)


def evaluate(fit,sel,ctrl,pk):
    """Compatibility wrapper used by experiments/tests outside distill()."""
    ws=_workspace(fit);c=_evaluate_fit(fit,ctrl,pk,ws);c.selection=_selection_score(sel or fit,c);return c


def _yield_cpu(pause_ms:float):
    if pause_ms>0:time.sleep(pause_ms/1000.0)


def distill(fit,sel,controls=24,rounds=3,status=print,pause_ms=0.0):
    fit_ws=_workspace(fit);gate_audio=sel or fit
    ctrl=np.zeros(controls);pk=np.array([.1,.1,1.,1.]);best=_evaluate_fit(fit,ctrl,pk,fit_ws);best.selection=_selection_score(gate_audio,best);_yield_cpu(pause_ms);status(f'seed fit={best.fit.composite:.6g} sel={best.selection.composite:.6g}')
    for r,(astep,pstep) in enumerate(zip((3.,1.5,.75,.35),(.45,.28,.16,.08)),1):
        if r>rounds:break
        rt=time.monotonic();before=best;work=best;evals=0
        for i in range(controls):
            for d in (astep,-astep):
                c=work.controls_db.copy();c[i]=np.clip(c[i]+d,-18,18);q=_evaluate_fit(fit,c,work.pk,fit_ws);evals+=1;_yield_cpu(pause_ms)
                if q.fit.composite<work.fit.composite:work=q
        for i in range(4):
            for s in (1.,-1.):
                p=work.pk.copy();p[i]*=math.exp(s*pstep);p[:2]=np.clip(p[:2],.01,2.);p[2:]=np.clip(p[2:],.05,80.);q=_evaluate_fit(fit,work.controls_db,p,fit_ws);evals+=1;_yield_cpu(pause_ms)
                if q.fit.composite<work.fit.composite:work=q
        # Selection is a round gate only. The previous implementation scored
        # it inside every candidate evaluation even though those values were
        # never consulted, roughly doubling expensive device renders.
        work.selection=_selection_score(gate_audio,work);_yield_cpu(pause_ms)
        elapsed=time.monotonic()-rt
        if work.selection.composite<before.selection.composite:
            best=work;status(f'round {r} ACCEPT sel {before.selection.composite:.6g}->{best.selection.composite:.6g} ({evals} candidates, {elapsed:.1f}s)')
        else:
            status(f'round {r} REJECT sel {before.selection.composite:.6g}->{work.selection.composite:.6g} ({evals} candidates, {elapsed:.1f}s)')
    return best
