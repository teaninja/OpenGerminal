#!/usr/bin/env python3
"""Can the OpenMM CUDA platform actually load on this node?

Run this before setting `use_gpu_relax: true` in a run config. It takes a few
seconds and answers the one question that otherwise costs hours of GPU time to
discover from a traceback in the middle of a run.

    apptainer exec --nv <image> python /workspace/tools/probe_cuda_platform.py

Exit status
    0   the CUDA platform built a working context  -> use_gpu_relax: true is safe here
    3   the CUDA platform is not even registered   -> use_gpu_relax: false
    4   it is registered but failed to load        -> use_gpu_relax: false
        (CUDA_ERROR_UNSUPPORTED_PTX_VERSION here means this node's driver is
        older than the toolkit the image was built against)

Three things in this probe are deliberate, and it is wrong without any of them.

  * It asks for the CUDA platform BY NAME. Letting OpenMM pick would let a
    silent fall-back to the CPU report success.

  * It builds a context that needs real CUDA kernels (512 particles with a
    NonbondedForce). A bare context, or one with no forces, loads no PTX
    module at all and therefore passes no matter what.

  * It is meant to be run against a known-bad image first. A probe whose
    control arm passes proves nothing about the experimental arm.
"""

from __future__ import annotations

import importlib.metadata as metadata
import sys


def report_versions():
    try:
        print("openmm version:", metadata.version("openmm"))
    except Exception as exc:
        print("openmm version: unknown (%s)" % exc)
    for package in ("cuda-version", "cuda-nvrtc"):
        try:
            print("%s: %s" % (package, metadata.version(package)))
        except Exception:
            print("%s: not installed as a package" % package)


def main():
    report_versions()

    import openmm as mm
    from openmm import Platform

    try:
        names = [
            Platform.getPlatform(i).getName()
            for i in range(Platform.getNumPlatforms())
        ]
        print("platforms registered:", names)
    except Exception as exc:
        print("could not enumerate platforms (%s); continuing" % exc)
        names = None

    if names is not None and "CUDA" not in names:
        print("RESULT: FAIL - the CUDA platform is not registered in this image")
        return 3

    platform = Platform.getPlatformByName("CUDA")

    system = mm.System()
    force = mm.NonbondedForce()
    for _ in range(512):
        system.addParticle(12.0)
        force.addParticle(0.0, 0.3, 0.5)
    system.addForce(force)
    integrator = mm.VerletIntegrator(0.001)

    try:
        context = mm.Context(system, integrator, platform)
    except Exception as exc:
        print("RESULT: FAIL - %s: %s" % (type(exc).__name__, exc))
        print(
            "Keep use_gpu_relax: false on this node. Relaxation will run on the "
            "OpenMM CPU platform, which evaluates the same force field and the "
            "same minimiser."
        )
        return 4

    print("RESULT: PASS - context built on", context.getPlatform().getName())
    print("use_gpu_relax: true is safe on this node.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
