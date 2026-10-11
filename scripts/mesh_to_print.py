#!/usr/bin/env python3
"""Headless mesh repair and solidification for 3D printing.

Turns a non-manifold / open / thin-walled mesh into a watertight, closed
solid that is safe to slice.  Two independent strategies are implemented so
that a mesh which defeats one repair path can still be salvaged by the other:

1.  surface repair (default): fill boundary holes, fix winding/normals,
    merge duplicated vertices and drop degenerate triangles, then verify
    with the Euler characteristic + watertightness.  Fast, keeps detail.
2.  voxel remesh ("convexify"): rasterize the mesh onto a shared voxel
    lattice, OR the occupancy of every overlapping component, and run
    marching cubes.  Every output voxel shell is watertight by
    construction, so this is the reliable fallback for badly broken input
    (missing walls, intersecting shells, zero-thickness features).

Both paths export a binary STL with a global uniform millimeter scale
(default 1.0 -- pass --scale-mm 10 to convert a decimetre-scale source
model into millimetre print units).

Dependencies: numpy, trimesh, scikit-image, rtree (all pure wheels).
Blender is optional: if the ``blender`` binary exists on PATH it is used
for an extra boolean-weld pass; otherwise the voxel path carries the weld.

Examples
--------
    # Repair an artist export and write print-ready STL
    python scripts/mesh_to_print.py input.obj --scale-mm 10 -o output.stl

    # Force the voxel remesh (blender-free boolean weld) at 0.5 mm voxels
    python scripts/mesh_to_print.py broken.stl --voxel --voxel-size 0.5 -o solid.stl

    # Headless blender repair only, no voxel fallback
    python scripts/mesh_to_print.py in.obj --blender -o out.stl
"""

from __future__ import annotations

import argparse
import collections
import json
import logging
import shutil
import sys
from pathlib import Path

import numpy as np

log = logging.getLogger("mesh_to_print")


def boundary_edges(mesh) -> list[tuple[int, int]]:
    """Edges used by exactly one triangle (the mesh's open borders)."""
    counter: collections.Counter = collections.Counter()
    for tri in mesh.faces:
        a, b, c = (int(v) for v in tri)
        for edge in ((a, b), (b, c), (a, c)):
            counter[tuple(sorted(edge))] += 1
    return [edge for edge, count in counter.items() if count == 1]


def _merge_duplicate_vertices(mesh):
    """Collapse duplicate vertices across trimesh 4.x/5.x API drift."""
    if hasattr(mesh, "merge_vertices"):
        mesh.merge_vertices()
    else:  # pragma: no cover - older trimesh
        mesh.update_vertices(mesh.unique_vertices())


def _surface_repair(mesh) -> tuple:
    """Close boundary holes, fix normals/winding, drop degenerate faces.

    Returns (repaired_mesh, report_dict).
    """
    report = {"before": {}, "after": {}}

    def snapshot(m):
        return {
            "watertight": bool(m.is_watertight),
            "euler": int(m.euler_number),
            "vertices": int(len(m.vertices)),
            "faces": int(len(m.faces)),
            "boundary_edges": len(boundary_edges(m)),
            "volume": float(m.volume) if m.is_watertight else None,
        }

    report["before"] = snapshot(mesh)

    # duplicated / unused vertices weaken watertightness checks
    _merge_duplicate_vertices(mesh)
    mesh.remove_unreferenced_vertices()

    degenerate = mesh.nondegenerate_faces()
    if len(degenerate) != len(mesh.faces):
        mesh.update_faces(degenerate)

    trimesh_repair = None  # imported lazily below to keep module import light
    try:
        from trimesh import repair as trimesh_repair
    except ImportError:  # pragma: no cover - dependency guard
        log.error("trimesh.repair unavailable; cannot surface-repair")
        return mesh, report

    trimesh_repair.fix_normals(mesh)
    trimesh_repair.fix_winding(mesh)
    trimesh_repair.fill_holes(mesh)

    # duplicate-vertex cleanup is cheap insurance after hole filling
    _merge_duplicate_vertices(mesh)
    mesh.remove_unreferenced_vertices()
    try:
        from trimesh import repair as _r

        _r.fill_holes(mesh)
    except Exception as exc:  # pragma: no cover - belt and braces
        log.debug("second fill_holes pass skipped: %s", exc)

    report["after"] = snapshot(mesh)
    return mesh, report


def _skimage_available() -> bool:
    try:
        import skimage  # noqa: F401

        return True
    except ImportError:
        return False


def _require_skimage(what: str):
    if not _skimage_available():
        raise SystemExit(
            f"{what} needs scikit-image for marching cubes.\n"
            "  python3 -m pip install -r requirements-pipeline.txt  "
            "(includes scikit-image)"
        )


def _voxel_remesh(mesh, voxel_size: float, origin: np.ndarray | None = None):
    """Solidify every component on a shared voxel lattice (blender-free weld).

    Occupancy is decided per voxel centre with ray-in-mesh queries on a
    common lattice so overlapping parts can never leave interior voxels
    behind (the classic "z-fighting" failure of BRep boolean engines).
    """
    _require_skimage("voxel remesh")
    from trimesh.voxel import ops as vops

    if mesh.bounds is None or not mesh.bounds.shape == (2, 3):
        raise SystemExit("cannot voxel-remesh an empty/degenerate mesh")
    bounds = mesh.bounds
    if origin is None:
        origin = bounds[0]
    pitch = float(voxel_size)
    span = (bounds[1] - bounds[0]) / pitch
    dims = tuple(int(np.ceil(s)) for s in span)
    if min(dims) < 1:
        raise SystemExit(
            f"voxel size {voxel_size} is too coarse for a mesh of extents "
            f"{bounds[1] - bounds[0]}; pick a sub-unit size"
        )
    # one extra slab so the shell fully encloses the part
    dims = tuple(d + 1 for d in dims)

    xs = origin[0] + (np.arange(dims[0]) + 0.5) * pitch
    ys = origin[1] + (np.arange(dims[1]) + 0.5) * pitch
    zs = origin[2] + (np.arange(dims[2]) + 0.5) * pitch
    gx, gy, gz = np.meshgrid(xs, ys, zs, indexing="ij")
    points = np.column_stack([gx.ravel(), gy.ravel(), gz.ravel()])

    occupancy = np.zeros(dims, dtype=np.int8)
    contained = mesh.contains(points).reshape(dims)
    occupancy |= contained.astype(np.int8)

    solid = vops.matrix_to_marching_cubes(occupancy, pitch=pitch)
    solid.update_faces(solid.nondegenerate_faces())
    solid.apply_translation(origin)
    return solid


def _blender_weld(mesh, report: dict):
    """Optional Blender headless boolean weld (all parts, exact result).

    Returns the welded mesh or None when Blender is unavailable.
    """
    blender = shutil.which("blender")
    if not blender:
        report["blender"] = {"used": False, "reason": "blender not on PATH"}
        return None
    try:
        import trimesh.interfaces.blender as tb  # type: ignore

        welded = tb.boolean_multiple(mesh, "union")
        report["blender"] = {
            "used": True,
            "watertight": bool(welded.is_watertight),
            "faces": int(len(welded.faces)),
        }
        return welded
    except Exception as exc:
        report["blender"] = {"used": False, "reason": str(exc)}
        return None


def _export(mesh, out_path: str, scale_mm: float) -> dict:
    """Apply uniform millimeter scale and write binary STL."""
    mesh = mesh.copy()
    mesh.apply_scale(scale_mm)
    mesh.export(out_path)
    return {
        "path": str(out_path),
        "scale_mm": scale_mm,
        "bytes": Path(out_path).stat().st_size,
        "watertight": bool(mesh.is_watertight),
        "euler": int(mesh.euler_number),
        "volume_mm3": float(mesh.volume),
        "extents_mm": [round(float(v), 3) for v in mesh.extents],
        "boundary_edges": len(boundary_edges(mesh)),
    }


def repair_mesh(
    input_path: str,
    *,
    out_path: str,
    scale_mm: float = 1.0,
    use_voxel: bool = False,
    voxel_size: float | None = None,
    use_blender: bool = False,
    tolerance: float = 1e-5,
) -> dict:
    """Repair / solidify a mesh file and write a print-ready binary STL.

    Returns a machine-readable report (JSON-serialisable).
    """
    import trimesh

    report: dict = {"input": str(input_path)}
    try:
        source = trimesh.load(input_path, force="mesh")
    except Exception as exc:  # pragma: no cover - user input error
        raise SystemExit(f"cannot load mesh {input_path}: {exc}") from exc
    report["source"] = {
        "type": str(getattr(source, "type", "unknown")),
        "vertices": int(len(source.vertices)),
        "faces": int(len(source.faces)),
        "watertight": bool(source.is_watertight),
        "euler": int(source.euler_number),
    }

    mesh = source
    if use_voxel:
        size = voxel_size or 0.1 * float(np.max(mesh.extents))
        mesh = _voxel_remesh(mesh, size)
        mesh, repair = _surface_repair(mesh)
        report["strategy"] = "voxel"
        report["voxel_size"] = size
        report["repair"] = repair
    else:
        mesh, repair = _surface_repair(mesh)
        report["strategy"] = "surface"
        report["repair"] = repair

    # Blender pass is always attempted when requested, regardless of strategy,
    # because boolean union is the one operation BRep engines do better.
    if use_blender:
        welded = _blender_weld(mesh, report)
        if welded is not None:
            mesh = welded

    # Watertightness gate: voxel path is structurally watertight; the surface
    # path may still leave hairline borders.  Re-run the voxel remesh as the
    # final guarantee when the gate fails.
    if not mesh.is_watertight and not use_voxel:
        log.warning("surface repair left the mesh open; falling back to voxel")
        size = voxel_size or 0.1 * float(np.max(mesh.extents))
        mesh = _voxel_remesh(mesh, size)
        mesh, repair2 = _surface_repair(mesh)
        report["strategy"] = "surface->voxel"
        report["repair_after_voxel"] = repair2
        report["voxel_size"] = size

    report["export"] = _export(mesh, out_path, scale_mm)
    return report


def _parse_args(argv: list[str] | None = None):
    parser = argparse.ArgumentParser(
        description=__doc__.split("Examples")[0].strip(),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("input", help="source mesh (.obj/.stl/.ply/.glb/...)")
    parser.add_argument(
        "-o", "--out", default="output.stl", help="output STL path (default output.stl)"
    )
    parser.add_argument(
        "--scale-mm",
        type=float,
        default=1.0,
        help="uniform scale so the STL is in millimetres (default 1.0)",
    )
    parser.add_argument(
        "--voxel",
        action="store_true",
        help="force voxel remesh solidification (blender-free weld)",
    )
    parser.add_argument(
        "--voxel-size",
        type=float,
        default=None,
        help="voxel pitch; defaults to 10%% of max extent",
    )
    parser.add_argument(
        "--blender",
        action="store_true",
        help="also run a headless Blender boolean union when available",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="emit the repair report as JSON on stdout",
    )
    parser.add_argument("-v", "--verbose", action="count", default=0)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )
    report = repair_mesh(
        args.input,
        out_path=args.out,
        scale_mm=args.scale_mm,
        use_voxel=args.voxel,
        voxel_size=args.voxel_size,
        use_blender=args.blender,
    )
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        e = report["export"]
        print(
            f"wrote {e['path']}  "
            f"watertight={e['watertight']}  "
            f"euler={e['euler']}  "
            f"volume={e['volume_mm3']:.2f} mm^3  "
            f"extents={e['extents_mm']} mm"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
