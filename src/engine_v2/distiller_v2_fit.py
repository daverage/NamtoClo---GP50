from __future__ import annotations
import math
from dataclasses import dataclass
import numpy as np
from distiller_v2_dsp import controls_to_a,render_preb,render_full,solve_shared_b

@dataclass
class Metrics: composite:float; esr:float; spectral:float; level_db:float
@dataclass
class Candidate: controls_db:np.ndarray; pk:np.ndarray; b:np.ndarray; fit:Metrics; selection:Metrics

def _aligned_esr(p,t,maxlag=256):
    n=min(len(p),len(t));p=p[:n];t=t[:n];dec=max(1,n//100000);ps=p[::dec];ts=t[::dec];ml=max(1,maxlag//dec);best=-1e99;lag0=0
    for l in range(-ml,ml+1):
        a,b=(ps[l:],ts[:len(ps)-l]) if l>=0 else (ps[:len(ps)+l],ts[-l:])
        if len(a)<128:continue
        v=float(np.dot(a,b))
        if v>best:best=v;lag0=l*dec
    if lag0>=0:p,t=p[lag0:],t[:n-lag0]
    else:p,t=p[:n+lag0],t[-lag0:]
    den=float(np.dot(t,t));return float(np.dot(p-t,p-t)/den) if den>1e-20 else 0.

def _spectral(p,t):
    n=min(len(p),len(t),131072)
    if n<1024:return 0.
    p=p[:n];t=t[:n];pr=math.sqrt(float(np.mean(p*p))+1e-30);tr=math.sqrt(float(np.mean(t*t))+1e-30);p=p*(tr/max(pr,1e-15));w=np.hanning(n)
    P=np.abs(np.fft.rfft(p*w));T=np.abs(np.fft.rfft(t*w));mask=T>max(float(T.max())*1e-5,1e-10)
    return float(np.sqrt(np.mean(np.log(np.maximum(P[mask],1e-12)/T[mask])**2))) if np.any(mask) else 0.

def score(audio,a,pk,b)->Metrics:
    if not audio:return Metrics(float('inf'),float('inf'),float('inf'),float('inf'))
    E=[];S=[];L=[]
    for x,t,_ in audio:
        p=render_full(x,a,pk,b);E.append(_aligned_esr(p,t));S.append(_spectral(p,t));pr=math.sqrt(float(np.mean(p*p))+1e-30);tr=math.sqrt(float(np.mean(t*t))+1e-30);L.append(abs(20*math.log10(max(pr,1e-15)/max(tr,1e-15))))
    e=float(np.mean(E));s=float(np.mean(S));l=float(np.mean(L));return Metrics(.7*e+.3*s+.01*l,e,s,l)

def fit_b(audio,a,pk):return solve_shared_b([render_preb(x,a,pk) for x,_,_ in audio],[t for _,t,_ in audio])
def evaluate(fit,sel,ctrl,pk):
    a=controls_to_a(ctrl);b=fit_b(fit,a,pk);return Candidate(ctrl.copy(),pk.copy(),b,score(fit,a,pk,b),score(sel or fit,a,pk,b))

def distill(fit,sel,controls=24,rounds=3,status=print):
    ctrl=np.zeros(controls);pk=np.array([.1,.1,1.,1.]);best=evaluate(fit,sel,ctrl,pk);status(f'seed fit={best.fit.composite:.6g} sel={best.selection.composite:.6g}')
    for r,(astep,pstep) in enumerate(zip((3.,1.5,.75,.35),(.45,.28,.16,.08)),1):
        if r>rounds:break
        before=best;work=best
        for i in range(controls):
            for d in (astep,-astep):
                c=work.controls_db.copy();c[i]=np.clip(c[i]+d,-18,18);q=evaluate(fit,sel,c,work.pk)
                if q.fit.composite<work.fit.composite:work=q
        for i in range(4):
            for s in (1.,-1.):
                p=work.pk.copy();p[i]*=math.exp(s*pstep);p[:2]=np.clip(p[:2],.01,2.);p[2:]=np.clip(p[2:],.05,80.);q=evaluate(fit,sel,work.controls_db,p)
                if q.fit.composite<work.fit.composite:work=q
        if work.selection.composite<before.selection.composite:best=work;status(f'round {r} ACCEPT sel {before.selection.composite:.6g}->{best.selection.composite:.6g}')
        else:status(f'round {r} REJECT sel {before.selection.composite:.6g}->{work.selection.composite:.6g}')
    return best
