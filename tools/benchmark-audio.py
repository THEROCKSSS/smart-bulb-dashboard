"""Repeatable CPU-only conditioning/FFT benchmark; never opens audio or a bulb."""
import argparse
import json
from pathlib import Path
import platform
import sys
import time

parser = argparse.ArgumentParser()
parser.add_argument('--backend', default=str(Path(__file__).resolve().parents[1] / 'backend'))
parser.add_argument('--iterations', type=int, default=5000)
args = parser.parse_args()
sys.path.insert(0, args.backend)
import numpy as np
import audio_reactive as ar
import audio_signal

conditioner = audio_signal.SignalConditioner(band_gains=[1, 1, 1])
t = np.arange(1024) / 44100
frame = sum(.1 * np.sin(2 * np.pi * hz * t) for hz in (60, 1000, 8000))
durations = []
for i in range(args.iterations + 100):
    start = time.perf_counter()
    conditioned, _ = conditioner.process(frame)
    bands = conditioner.apply_band_gains(ar.analyze_frame(conditioned, extra_band_edges=ar.log_band_edges(6)))
    if i >= 100:
        durations.append((time.perf_counter() - start) * 1000)
assert all(np.isfinite(bands['energies']))
print(json.dumps({'python': platform.python_version(), 'platform': platform.system(), 'numpy': np.__version__,
                  'frames': args.iterations, 'samples': 1024, 'sample_rate': 44100,
                  'p50_ms': round(float(np.percentile(durations, 50)), 4),
                  'p95_ms': round(float(np.percentile(durations, 95)), 4)}))
