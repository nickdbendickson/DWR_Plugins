"""Boann's own version: one number for the whole engine family (the shared Metal/CPU machinery and
every solver: ACC, SGC, FV1, FV2, DG2, sub-grid FV), separate from the Runner's and Screening's.

Bump it when anything in this folder changes what a run computes or what the command line accepts
(minor for a new solver, option or output; patch for a fix; major when an existing result moves).
The Runner records it in the run manifest (software.boann) beside its own version.

Each solver module still carries its own NAME / VERSION, written as the first line of the
lisflood.log a run leaves; those are unchanged."""
BOANN_VERSION = "4.1.0"   # 4.1.0 (Runner 3.68.0): rain on the grid (ACC; new input); 4.0.0 (Runner 3.67.0): sub-grid FV positivity limiter (sub-grid FV results move); 3.0.0 (Runner 3.66.0): FV2 face velocities (FV2 results move)
