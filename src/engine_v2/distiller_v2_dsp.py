from __future__ import annotations
import math, struct
from pathlib import Path
import numpy as np
from numba import njit

SR=44100; A_TAPS=128; B_TAPS=512; CLO_BYTES=0x0A88; COEFF_BASE=0x88
POST_C=np.float32(177.7158051); POST_W2=np.float32(15791.45215)
U1_A=np.array([.045728147029876709,.3325011134147644,.66320204734802246,.93385583162307739],dtype=np.float64)
U1_B=np.array([.16808754205703735,.50448572635650635,.80378085374832153],dtype=np.float64)
U2_A=np.array([.054230779409408569,.39879697561264038,.86291784048080444],dtype=np.float64)
U2_B=np.array([.19969958066940308,.62109684944152832],dtype=np.float64)
D1_A=np.array([.070765949785709381,.51316756010055542],dtype=np.float64)
D1_B=np.array([.25785309076309204,.81731736660003662],dtype=np.float64)
D2_A=np.array([.054217524826526642,.38308733701705933,.74872094392776489],dtype=np.float64)
D2_B=np.array([.19679796695709229,.57313638925552368,.91429370641708374],dtype=np.float64)

def post_coeffs(sr:int=SR)->np.ndarray:
    f=np.float32(sr); f2=np.float32(f*f); D=np.float32(f2+POST_C*f+POST_W2)
    b0=np.float32(f2/D); b1=np.float32(-2.0)*b0; b2=b0
    a1=np.float32(-(np.float32(2.0)*f2-np.float32(2.0)*POST_W2)/D)
    a2=np.float32((f2-POST_C*f+POST_W2)/D)
    return np.array([b0,b1,b2,a1,a2],dtype=np.float64)
POST=post_coeffs(); PRE=np.array([1.,0.,0.,0.,0.],dtype=np.float64)

@njit(cache=True)
def _ap(x, coeffs, states):
    for i in range(len(coeffs)):
        a=coeffs[i]; s=states[i]; y=s+a*x; states[i]=x-a*y; x=y
    return x

@njit(cache=True)
def _up(x,ac,ast,bc,bst): return _ap(x,ac,ast),_ap(x,bc,bst)

@njit(cache=True)
def _down(e,o,ac,ast,bc,bst,delay):
    x=_ap(e,ac,ast); y=_ap(o,bc,bst); return 0.5*(x+delay),y

@njit(cache=True)
def _fir(x,h):
    out=np.zeros(len(x),dtype=np.float64); hist=np.zeros(len(h),dtype=np.float64); ix=0
    for i in range(len(x)):
        hist[ix]=x[i]; s=0.; j=ix
        for k in range(len(h)):
            s+=h[k]*hist[j]; j=j-1 if j else len(h)-1
        out[i]=s; ix=ix+1 if ix+1<len(h) else 0
    return out

@njit(cache=True)
def _bq(x,c):
    out=np.empty(len(x),dtype=np.float64); w1=0.; w2=0.
    for i in range(len(x)):
        w0=x[i]*1000.-c[3]*w1-c[4]*w2; yd=c[0]*w0+c[1]*w1+c[2]*w2
        w2=w1; w1=w0; out[i]=float(np.float32(float(np.float32(yd))*0.001))
    return out

@njit(cache=True)
def _preb_from_aout(aout,pp,pn,kp,kn,post):
    n=len(aout); out=np.empty(n,dtype=np.float64)
    u1as=np.zeros(len(U1_A));u1bs=np.zeros(len(U1_B));u2as=np.zeros(len(U2_A));u2bs=np.zeros(len(U2_B))
    d1as=np.zeros(len(D1_A));d1bs=np.zeros(len(D1_B));d2as=np.zeros(len(D2_A));d2bs=np.zeros(len(D2_B))
    d1delay=0.;d2delay=0.;w1=0.;w2=0.
    for i in range(n):
        e,o=_up(aout[i],U1_A,u1as,U1_B,u1bs)
        q0,q1=_up(e,U2_A,u2as,U2_B,u2bs)
        q0=pp*(1.-math.exp(-kp*q0)) if q0>0 else pn*(math.exp(kn*q0)-1.)
        q1=pp*(1.-math.exp(-kp*q1)) if q1>0 else pn*(math.exp(kn*q1)-1.)
        z0,d1delay=_down(q0,q1,D1_A,d1as,D1_B,d1bs,d1delay)
        q0,q1=_up(o,U2_A,u2as,U2_B,u2bs)
        q0=pp*(1.-math.exp(-kp*q0)) if q0>0 else pn*(math.exp(kn*q0)-1.)
        q1=pp*(1.-math.exp(-kp*q1)) if q1>0 else pn*(math.exp(kn*q1)-1.)
        z1,d1delay=_down(q0,q1,D1_A,d1as,D1_B,d1bs,d1delay)
        z,d2delay=_down(z0,z1,D2_A,d2as,D2_B,d2bs,d2delay)
        w0=z*1000.-post[3]*w1-post[4]*w2; yd=post[0]*w0+post[1]*w1+post[2]*w2
        w2=w1;w1=w0;out[i]=float(np.float32(float(np.float32(yd))*0.001))
    return out

@njit(cache=True)
def render_preb(x,a,pk): return _preb_from_aout(_fir(_bq(x,PRE),a),pk[0],pk[1],pk[2],pk[3],POST)
@njit(cache=True)
def render_full(x,a,pk,b): return _fir(render_preb(x,a,pk),b)

def controls_to_a(ctrl_db:np.ndarray)->np.ndarray:
    ctrl=np.asarray(ctrl_db,dtype=np.float64); cf=np.geomspace(30.,20000.,len(ctrl)); nfft=2048
    f=np.fft.rfftfreq(nfft,1./SR); mag=10.**(np.interp(np.log(np.maximum(f,cf[0])),np.log(cf),ctrl)/20.)
    full=np.concatenate([mag,mag[-2:0:-1]]); cep=np.fft.ifft(np.log(np.maximum(full,1e-8))).real
    c=np.zeros_like(cep); c[0]=cep[0]; c[1:nfft//2]=2*cep[1:nfft//2]; c[nfft//2]=cep[nfft//2]
    h=np.fft.ifft(np.exp(np.fft.fft(c))).real[:A_TAPS]; s=h.sum()
    if abs(s)>1e-12: h*=mag[0]/s
    return h.astype(np.float64)

def _nextpow2(n:int)->int: return 1<<(max(1,n)-1).bit_length()
def solve_shared_b(prebs:list[np.ndarray],targets:list[np.ndarray])->np.ndarray:
    if not prebs or len(prebs)!=len(targets): raise ValueError('empty/mismatched B solve')
    N=_nextpow2(max(max(len(x),len(y)) for x,y in zip(prebs,targets))+B_TAPS)
    num=np.zeros(N//2+1,np.complex128); den=np.zeros(N//2+1); w=1./len(prebs)
    for x,y in zip(prebs,targets):
        P=np.fft.rfft(x,N); T=np.fft.rfft(y,N); num+=w*np.conj(P)*T; den+=w*np.abs(P)**2
    eps=max(1e-3*float(np.mean(den)),1e-20); H=num/(den+eps)
    return np.fft.irfft(H,N)[:B_TAPS].astype(np.float64)

def crc16_modbus(data:bytes)->int:
    crc=0xffff
    for byte in data:
        crc^=byte
        for _ in range(8): crc=(crc>>1)^0xA001 if crc&1 else crc>>1
    return crc&0xffff

def write_clo(path:Path,a:np.ndarray,pk:np.ndarray,b_device:np.ndarray)->None:
    """Write a direct-44.1-kHz GP5/GP50 compact CLO.

    Unlike EngineV2's legacy trainer-domain serializer, this distiller solves
    B directly against the final 44.1-kHz device-domain target. Therefore the
    solved B512 is already the coefficient block that belongs in the CLO and
    MUST NOT be multiplied by four during serialization.
    """
    if len(a)!=A_TAPS or len(b_device)!=B_TAPS: raise ValueError('A128/B512 required')
    d=bytearray(CLO_BYTES);d[:4]=b'VTSI';struct.pack_into('<I',d,4,CLO_BYTES);struct.pack_into('<I',d,0x14,0x0A00)
    for off,v in zip((0x18,0x20,0x28,0x30,0x38),(1.,0.,0.,0.,0.)):struct.pack_into('<d',d,off,v)
    for off,v in zip((0x40,0x48,0x50,0x58,0x60),POST):struct.pack_into('<d',d,off,float(v))
    for off,v in zip((0x68,0x6c,0x70,0x74),pk):struct.pack_into('<f',d,off,float(v))
    for off,v in ((0x78,0),(0x7c,128),(0x80,128),(0x84,512)):struct.pack_into('<I',d,off,v)
    for i,v in enumerate(a):struct.pack_into('<f',d,COEFF_BASE+4*i,float(v))
    for i,v in enumerate(np.asarray(b_device)):struct.pack_into('<f',d,COEFF_BASE+4*(128+i),float(v))
    crc=crc16_modbus(d[0x0c:]);d[8]=(crc>>8)&255;d[9]=crc&255
    path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(d);verify_clo(path)

def verify_clo(path:Path)->None:
    d=path.read_bytes()
    if len(d)!=CLO_BYTES or d[:4]!=b'VTSI':raise RuntimeError('invalid compact CLO')
    if struct.unpack_from('<I',d,4)[0]!=CLO_BYTES or struct.unpack_from('<I',d,0x84)[0]!=512:raise RuntimeError('invalid CLO header')
    if ((d[8]<<8)|d[9])!=crc16_modbus(d[0x0c:]):raise RuntimeError('CLO CRC mismatch')

def warm():
    x=np.zeros(512);x[0]=.1;a=np.zeros(128);a[0]=1.;b=np.zeros(512);b[0]=.25;pk=np.array([.1,.1,1.,1.]);render_full(x,a,pk,b)
