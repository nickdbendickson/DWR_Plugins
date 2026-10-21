"""Tiny ANUGA OpenMP scaling check (Runner 3.10.50, .github/workflows/anuga-omp-scaling.yml):
the same dam-break at 1, 2 and 4 threads, each in a fresh process. Prints the wall time, the
OpenMP runtime's own thread count (DamSafety_Runner/core/omp_probe.py) and a sha256 of the
final stage; fails when the results differ or the runtime runs another count.
Run it in an ANUGA environment: python tools/anuga_omp_bench.py   (NOT on the shared box
while models run: it is a real, if short, ANUGA run)."""
import hashlib, json, os, subprocess, sys, time

CHILD = r'''
import hashlib, json, os, sys, time
sys.path.insert(0, sys.argv[2])
from omp_probe import omp_runtime_threads
import anuga, numpy as np
n = int(sys.argv[1])
anuga.set_omp_num_threads(n, verbose=False)
dom = anuga.rectangular_cross_domain(150, 150, len1=300.0, len2=300.0)   # 90,000 triangles
dom.set_quantity("elevation", 0.0)
dom.set_quantity("friction", 0.03)
dom.set_quantity("stage", lambda x, y: np.where(x < 100.0, 2.0, 0.1))
dom.set_boundary({k: anuga.Reflective_boundary(dom) for k in ("left", "right", "top", "bottom")})
dom.set_name("bench_%d" % n); dom.set_datadir(os.getcwd()); dom.store = False
t0 = time.perf_counter()
for _ in dom.evolve(yieldstep=10.0, finaltime=60.0):
    pass
dt = time.perf_counter() - t0
st = dom.quantities["stage"].centroid_values
print(json.dumps({"threads": n, "wall_s": round(dt, 2), "runtime": omp_runtime_threads(),
                  "anuga_reported": anuga.get_omp_num_threads(),
                  "sha": hashlib.sha256(np.ascontiguousarray(st).tobytes()).hexdigest()[:16]}))
'''

here = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "DamSafety_Runner", "core")      # omp_probe.py, the Runner's copy
res = []
for n in (1, 2, 4):
    env = dict(os.environ, OMP_NUM_THREADS=str(n))
    out = subprocess.run([sys.executable, "-c", CHILD, str(n), here], env=env,
                         capture_output=True, text=True, timeout=1800)
    line = [l for l in out.stdout.splitlines() if l.startswith("{")]
    if out.returncode or not line:
        print(out.stdout[-3000:], out.stderr[-3000:]); sys.exit(1)
    res.append(json.loads(line[-1])); print(line[-1], flush=True)
base = res[0]["wall_s"]
print("cpu_count", os.cpu_count(), "| speedup:",
      {r["threads"]: round(base / r["wall_s"], 2) for r in res})
assert len({r["sha"] for r in res}) == 1, "results differ between thread counts"
assert all(r["runtime"][0] == r["threads"] for r in res), "runtime thread count != requested"
