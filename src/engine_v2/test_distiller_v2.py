#!/usr/bin/env python3
import struct,tempfile
from pathlib import Path
import numpy as np
from distiller_v2_dsp import controls_to_a,post_coeffs,render_full,write_clo,crc16_modbus

def main():
    a=controls_to_a(np.zeros(24));assert len(a)==128 and abs(a[0]-1)<1e-4 and np.max(np.abs(a[1:]))<1e-4
    assert np.all(np.isfinite(post_coeffs()))
    x=np.zeros(4096);x[100]=.1;pk=np.array([.1,.1,1.,1.]);b=np.zeros(512);b[0]=.25;y=render_full(x,a,pk,b);assert np.all(np.isfinite(y))
    with tempfile.TemporaryDirectory() as td:
        p=Path(td)/'x.clo';write_clo(p,a,pk,b);d=p.read_bytes();assert len(d)==0x0a88 and d[:4]==b'VTSI' and ((d[8]<<8)|d[9])==crc16_modbus(d[0x0c:])
        stored_b0=struct.unpack_from('<f',d,0x88+4*128)[0];assert abs(stored_b0-.25)<1e-7,stored_b0
    print('distiller_v2 self-tests passed')
if __name__=='__main__':main()
