"""Boann's own version: one number for the whole engine family (the shared Metal/CPU machinery and
every solver: ACC, SGC, FV1, FV2, DG2, sub-grid FV, sub-grid local inertial), separate from the Runner's and Screening's.

Bump it when anything in this folder changes what a run computes or what the command line accepts
(minor for a new solver, option or output; patch for a fix; major when an existing result moves).
The Runner records it in the run manifest (software.boann) beside its own version.

Each solver module still carries its own NAME / VERSION, written as the first line of the
lisflood.log a run leaves; those are unchanged."""
BOANN_VERSION = "6.3.0"   # 6.3.0 (Runner 3.97.0): the sub-grid local-inertial engine skips dry tiles on the Apple GPU (no result moves); 6.0.0 (Runner 3.93.0): FV1, FV2 and DG2 make a face against NoData a reflecting wall (FV1/FV2/DG2 results move where water reaches NoData); 5.5.0 (Runner 3.90.0): the ACC engine skips dry tiles on the Apple GPU (DAMSAFETY_MF_TILES; no result moves); 5.4.1 (Runner 3.89.0): Mac CPU build defaults to the performance cores (no result moves); 5.4.0 (Runner 3.80.0): the profile (--profile, stats["profile"]; no result moves); 5.3.0 (Runner 3.79.0): new solver, sub-grid local inertial (gpu-subli; no existing result moves); 5.1.1 (Runner 3.76.0): CPU build skips dry cells, OpenMP on a Mac (no result moves); 5.1.0 (Runner 3.75.0): rain on the grid (ACC; new input); 5.0.0 (Runner 3.68.0): sub-grid FV positivity limiter (sub-grid FV results move); 4.0.0 (Runner 3.67.0): DG2 primitive edge velocities (DG2 results move)
