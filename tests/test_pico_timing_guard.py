import importlib.util
from pathlib import Path
p=Path(__file__).resolve().parents[1]/'umi_collection/vendor/galaxis/galaxis-core/galaxis-core/galaxis_core/pico_timing.py'
s=importlib.util.spec_from_file_location('pico_timing',p);m=importlib.util.module_from_spec(s);s.loader.exec_module(m)

def feed(g, n=200, speed=1.0, start=0):
 for i in range(start,start+n):
  t=i/90
  g.push(t,10**18+round(t*speed*1e9),10**12+round(t*1e9),i,'pico')
 return g.failure(t)

def test_normal_90hz_passes():
 assert feed(m.PicoTiming()) is None

def test_old_data_burst_cannot_start():
 assert '积压' in feed(m.PicoTiming(),speed=21.5/11.6)

def test_small_transport_jitter_passes():
 g=m.PicoTiming()
 for i in range(200):
  t=i/90;g.push(t,10**18+round(t*1e9),10**12+round((t+(i%2)*.005)*1e9),i,'pico')
 assert g.failure(t) is None

def test_reconnect_requires_fresh_window_then_recovers():
 g=m.PicoTiming();feed(g)
 g.push(3,10**18,10**12,0,'new')
 assert g.failure(3)
 for i in range(200):
  t=3+(i+1)/90;g.push(t,10**18+round(t*1e9),10**12+round(t*1e9),i+1,'new')
 assert g.failure(t) is None

def test_invalid_and_stale_inputs_block():
 g=m.PicoTiming();g.push(0,0,0,0,'');assert g.failure(0)
 g=m.PicoTiming();feed(g);assert g.failure(10)
