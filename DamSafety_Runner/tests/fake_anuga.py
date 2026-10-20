"""A tiny stand-in for the parts of ANUGA 4.0.1 that core/anuga_worker.py uses, for the
3.10.35 checkpoint tests (tests/test_anuga_checkpoint.py). No hydraulics: the water level
of each triangle follows a fixed function of time, stepped at a few internal time steps
per yield step, so the envelope operator, checkpoints (pickled like Domain.evolve does,
before each yield) and resume can be checked without ANUGA. Also a stand-in for
matplotlib.tri's trifinder (nearest centroid inside the mesh box). Classes are at module
level so the plain pickle module can store a domain."""
import os
import pickle
import sys
import types

import numpy as np

__version__ = "fake-4.0.1"
STOP_AT = {"t": None}        # raise SystemExit inside evolve when the time reaches this


class Geo_reference:
    def __init__(self, xllcorner=0.0, yllcorner=0.0):
        self.xllcorner, self.yllcorner = xllcorner, yllcorner


class Quantity:
    def __init__(self, n):
        self.centroid_values = np.zeros(n)


class Operator:
    def __init__(self, domain, description=None, label=None, **kw):
        self.domain = domain
        self.label = label
        self.stage_c = domain.quantities["stage"].centroid_values
        self.elev_c = domain.quantities["elevation"].centroid_values
        domain.fractional_step_operators.append(self)


class Inlet_operator:
    def __init__(self, domain, line, Q=None, default=0.0):
        self.Q, self.default = Q, default
        domain.fractional_step_operators.append(self)

    def __call__(self):
        pass


class Transmissive_boundary:
    def __init__(self, domain):
        pass


class Dirichlet_boundary:
    def __init__(self, v):
        pass


class Domain:
    def __init__(self, points, triangles, boundary, geo_reference=None):
        pts = np.asarray(points, float)
        tri = np.asarray(triangles, int)
        self.nodes, self.triangles = pts, tri
        n = len(tri)
        self.centroid_coordinates = pts[tri].mean(axis=1)
        self.areas = np.full(n, 1.0)
        self.boundary = dict(boundary)
        self.quantities = {k: Quantity(n) for k in ("stage", "elevation", "xmomentum",
                                                     "ymomentum", "friction")}
        self.fractional_step_operators = []
        self.time = 0.0
        self.evolved_called = False
        self.yieldstep_counter = 0
        self.checkpoint = False
        self.checkpoint_step = 10
        self.name = "domain"

    def __len__(self):
        return len(self.triangles)

    def set_name(self, n):
        self.name = n

    def get_name(self):
        return self.name

    def set_store(self, v):
        pass

    def set_flow_algorithm(self, a):
        pass

    def set_minimum_storable_height(self, h):
        pass

    def set_boundary(self, b):
        pass

    def get_time(self):
        return self.time

    def set_quantity(self, name, value=None, location="centroids", expression=None):
        q = self.quantities[name].centroid_values
        if expression is not None:
            q[:] = self.quantities[expression].centroid_values
        elif callable(value):
            c = self.centroid_coordinates
            q[:] = value(c[:, 0], c[:, 1])
        else:
            q[:] = value

    def set_checkpointing(self, checkpoint=True, checkpoint_dir="CHECKPOINTS",
                          checkpoint_step=10, checkpoint_time=None):
        self.checkpoint = checkpoint
        if checkpoint:
            os.makedirs(checkpoint_dir, exist_ok=True)
            self.checkpoint_dir = checkpoint_dir
            self.checkpoint_step = 0 if checkpoint_time is not None else checkpoint_step
            self.checkpoint_time = checkpoint_time

    def _water(self, t):
        """depth by triangle at time t: a wave moving along x, then draining."""
        x = self.centroid_coordinates[:, 0]
        return np.clip(1.5 * np.sin(np.pi * min(t, 3000.0) / 3000.0) - x / 1000.0, 0, None)

    def evolve(self, yieldstep, finaltime):
        """Like Domain.evolve: no initial yield once evolve has run (a loaded checkpoint);
        a checkpoint is pickled before a yield; the counter goes up after it."""
        skip = self.evolved_called
        self.evolved_called = True
        if not skip:
            self._save()
            yield self.get_time()
            self.yieldstep_counter += 1
        sub = 3
        while self.time < finaltime - 1e-9:
            t_next = min(self.time + yieldstep, finaltime)
            dt = (t_next - self.time) / sub
            for _ in range(sub):
                if STOP_AT["t"] is not None and self.time >= STOP_AT["t"]:
                    raise SystemExit(143)
                self.time += dt
                h = self._water(self.time)
                st = self.quantities["stage"].centroid_values
                st[:] = self.quantities["elevation"].centroid_values + h
                self.quantities["xmomentum"].centroid_values[:] = 0.5 * h
                for op in self.fractional_step_operators:
                    op()
            self.time = t_next
            self._save()
            yield self.get_time()
            self.yieldstep_counter += 1

    def _save(self):
        if self.checkpoint and self.checkpoint_step and \
                self.yieldstep_counter % self.checkpoint_step == 0:
            p = os.path.join(self.checkpoint_dir, self.get_name()) + "_" + str(self.get_time()) \
                + ".pickle"
            with open(p, "wb") as f:
                pickle.dump(self, f)


def create_domain_from_regions(boundary, boundary_tags=None, maximum_triangle_area=None,
                               interior_regions=None, mesh_geo_reference=None, use_cache=False,
                               verbose=False, breaklines=None):
    """A regular grid of 2 triangles per square over the boundary's box."""
    b = np.asarray(boundary, float)
    x0, y0 = b.min(axis=0)
    x1, y1 = b.max(axis=0)
    nx, ny = 10, 6
    xs, ys = np.linspace(x0, x1, nx + 1), np.linspace(y0, y1, ny + 1)
    pts = [(x, y) for y in ys for x in xs]
    tris, bnd = [], {}
    for j in range(ny):
        for i in range(nx):
            a = j * (nx + 1) + i
            tris.append([a, a + 1, a + nx + 2])
            tris.append([a, a + nx + 2, a + nx + 1])
            if j == 0:
                bnd[(len(tris) - 2, 2)] = "outflow"
    return Domain(pts, tris, bnd)


def load_checkpoint_file(domain_name="domain", checkpoint_dir=".", time=None):
    p = os.path.join(checkpoint_dir, domain_name) + "_" + str(float(time)) + ".pickle"
    with open(p, "rb") as f:
        return pickle.load(f)


# ---------------------------------------------------------------- matplotlib.tri stand-in
class Triangulation:
    def __init__(self, x, y, triangles):
        self.c = np.column_stack([np.asarray(x)[triangles].mean(axis=1),
                                  np.asarray(y)[triangles].mean(axis=1)])
        self.box = (np.min(x), np.min(y), np.max(x), np.max(y))

    def get_trifinder(self):
        def finder(X, Y):
            X, Y = np.asarray(X, float), np.asarray(Y, float)
            d = (X[..., None] - self.c[:, 0]) ** 2 + (Y[..., None] - self.c[:, 1]) ** 2
            i = np.argmin(d, axis=-1)
            x0, y0, x1, y1 = self.box
            out = (X < x0) | (X > x1) | (Y < y0) | (Y > y1)
            return np.where(out, -1, i)
        return finder


def install():
    """Put this module in sys.modules as `anuga` and `matplotlib.tri`; returns an undo."""
    me = sys.modules[__name__]
    saved = {k: sys.modules.get(k) for k in ("anuga", "matplotlib", "matplotlib.tri")}
    mpl = types.ModuleType("matplotlib")
    mtri = types.ModuleType("matplotlib.tri")
    mtri.Triangulation = Triangulation
    mpl.tri = mtri
    sys.modules.update({"anuga": me, "matplotlib": mpl, "matplotlib.tri": mtri})

    def undo():
        for k, v in saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v
    return undo
