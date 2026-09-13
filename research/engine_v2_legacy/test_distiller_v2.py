#!/usr/bin/env python3
import struct,tempfile
from pathlib import Path
from types import SimpleNamespace
import numpy as np
from distiller_v2_dsp import controls_to_a,post_coeffs,render_full,write_clo,crc16_modbus
from distiller_v2_fit import Candidate,Metrics,_selection_score,_aligned_esr,score,_level_response_stats,_linear_residual_db
from distiller_v2_data import Pair,level_sweep_groups
from train_namtoclo_distiller import _calibrate_output_gain

def main():
    a=controls_to_a(np.zeros(24));assert len(a)==128 and abs(a[0]-1)<1e-4 and np.max(np.abs(a[1:]))<1e-4
    assert np.all(np.isfinite(post_coeffs()))
    x=np.zeros(4096);x[100]=.1;pk=np.array([.1,.1,1.,1.]);b=np.zeros(512);b[0]=.25;y=render_full(x,a,pk,b);assert np.all(np.isfinite(y))

    audio=[(x,y,None)];exact=score(audio,a,pk,b);dummy=Candidate(np.zeros(24),pk,b,exact,exact);fast=_selection_score(audio,dummy)
    assert exact.esr<1e-15,exact.esr
    assert fast.esr<1e-12,fast.esr
    assert abs(fast.signed_level_db-exact.signed_level_db)<1e-8,(fast.signed_level_db,exact.signed_level_db)

    # Output calibration scales B512, the final linear block after P/K.
    scaled,gain_db,before,after,info=_calibrate_output_gain([(x,2.0*y,None)],a,pk,b)
    assert abs(gain_db-6.020599913)<1e-5,gain_db
    assert abs(scaled[0]-.5)<1e-6,scaled[0]
    assert abs(after.signed_level_db)<1e-5,after.signed_level_db
    assert abs(info['requested_rms_gain_db']-6.020599913)<1e-5,info
    assert info['stage']=='B512 post-P/K output',info
    assert not info['limited_by_peak_safety'],info
    assert info['peak_safe_cap_db']>=gain_db,info

    real=SimpleNamespace(synthetic=False,task_id='real',level_offset_db=0.0)
    synth=SimpleNamespace(synthetic=True,task_id='synth',level_offset_db=0.0)
    scaled2,gain2,_,after2,info2=_calibrate_output_gain([(x,2.0*y,real),(x,0.5*y,synth)],a,pk,b)
    assert abs(gain2-6.020599913)<1e-5,(gain2,info2)
    assert abs(scaled2[0]-.5)<1e-6,scaled2[0]
    assert abs(after2.signed_level_db)<1e-5,after2.signed_level_db
    assert info2['clips_used']==1 and info2['clips_total']==2,info2
    assert info2['excluded_synthetic_clips']==1,info2
    assert info2['calibration_material']=='real-guitar',info2

    peak=float(np.max(np.abs(y)));flat=np.where(y>=0.0,.9*peak,-.9*peak)
    scaled3,gain3,_,_,info3=_calibrate_output_gain([(x,flat,real)],a,pk,b)
    assert info3['requested_rms_gain_db']>0.0,info3
    assert info3['peak_safe_cap_db']<0.0,info3
    assert abs(gain3)<1e-12,(gain3,info3)
    assert np.allclose(scaled3,b),gain3
    assert info3['limited_by_peak_safety'],info3

    def pair(task,level,start=1.25,role='fit'):
        return Pair(task,'m','model',1,2,'development',role,'test',False,'in.wav','out.wav',2.0,task,'/tmp/source.wav','sourcehash',start,level)
    sweep=[pair('m12',-12),pair('m6',-6),pair('z0',0),pair('p6',6),pair('other',0,start=9.0)]
    groups=level_sweep_groups(sweep,'fit',max_groups=1,seed=1,min_levels=3)
    assert len(groups)==1,groups
    assert [p.level_offset_db for p in groups[0]]==[-12,-6,0,6],groups[0]

    wave=np.sin(np.linspace(0,40*np.pi,4096));targets=[.5*wave,wave,1.5*wave]
    pairs=[SimpleNamespace(dataset='test',source_sha256='same',source_path='/tmp/g.wav',start_s=0.,duration_s=1.,role='fit',level_offset_db=l,task_id=str(l)) for l in (-12,0,6)]
    preds=[.25*t for t in targets]
    lr=_level_response_stats(preds,targets,pairs,True);assert lr['rmse_db']<1e-10,lr
    bad=[preds[0]*2,preds[1],preds[2]]
    lr_bad=_level_response_stats(bad,targets,pairs,True);assert lr_bad['rmse_db']>4.0,lr_bad

    # The nonlinear-residual metric must be invariant to post-output gain but
    # react strongly when actual nonlinear distortion is introduced.
    rng=np.random.default_rng(260910);xin=rng.standard_normal(65536)*.05
    h=np.zeros(512);h[0]=.8;h[7]=-.2;h[31]=.1
    N=1<<(len(xin)+len(h)-1).bit_length();lin=np.fft.irfft(np.fft.rfft(xin,N)*np.fft.rfft(h,N),N)[:len(xin)]
    base=_linear_residual_db(xin,lin);boosted=_linear_residual_db(xin,lin*3.0)
    clipped=_linear_residual_db(xin,np.tanh(8.0*lin)/8.0)
    assert abs(base-boosted)<1e-8,(base,boosted)
    assert clipped>base+6.0,(base,clipped)

    t=rng.standard_normal(4096);p=np.concatenate((np.zeros(17),t[:-17]));assert _aligned_esr(p,t)<1e-12

    with tempfile.TemporaryDirectory() as td:
        p=Path(td)/'x.clo';write_clo(p,a,pk,b);d=p.read_bytes();assert len(d)==0x0a88 and d[:4]==b'VTSI' and ((d[8]<<8)|d[9])==crc16_modbus(d[0x0c:])
        stored_b0=struct.unpack_from('<f',d,0x88+4*128)[0];assert abs(stored_b0-.25)<1e-7,stored_b0
    print('distiller_v2 self-tests passed')
if __name__=='__main__':main()
