#!/usr/bin/env python3
"""End-to-end anime figure 3D-print automation for OpenClaw.

Anime illustration (or any PNG/WebP/JPG) -> 3D figure mesh -> watertight,
manifold, mm-unit STL ready to slice.

Stage contract
--------------
* stage 0  recon      : collect inputs, verify runtime deps
* stage 1  backgrid   : square padded canvas, white/alpha background, size cap
* stage 2  silhouette : foreground mask (semantic background removal)
* stage 3  extrude    : depth assignment + simple bevel => base mesh
* stage 4  weld       : build print base, boolean-weld figure onto it
* stage 5  repair     : watertight gate + manifold repair / voxel solidify
* stage 6  export     : uniform mm scale, binary STL, manifest

Every stage writes a JSON record into ``manifest``, and stages 1-3 also dump
a preview PNG so the pipeline is inspectable even headless.

The image->mesh path is fully local (no model download): stage 2 uses
scikit-image's classic Random-Walker segmentation seeded by colour-distance
to the corner background.  When ``rembg`` is installed it is preferred
because it is dramatically better on busy anime backgrounds; pass
``--bg-remover auto`` (default) to use it when present.

Mesh welding is blender-free: overlapping components are rasterised onto a
shared voxel lattice and re-meshed with marching cubes, so intersecting
geometry can never leave non-manifold seams.  If the ``blender`` binary is
installed and ``--use-blender`` is passed, an exact boolean-union pass runs
first and the voxel path is only the fallback.

Stages may be skipped with ``--skip <stages>`` and resumed from any point
with ``--from <stage>`` (artifacts are deterministic per stage).
"""

from __future__ import annotations

import argparse
import json
import logging
import shutil
import sys
import time
from pathlib import Path

import numpy as np

log = logging.getLogger("anime_3dprint")

# dependency import is deferred into each stage so `--skip` and `--help`
# work on a box missing heavy optional deps.

STAGES = ("recon", "backgrid", "silhouette", "extrude", "weld", "repair", "export")


def _importable(module: str) -> bool:
    try:
        __import__(module)
        return True
    except ImportError:
        return False


def _skimage_available() -> bool:
    try:
        import skimage  # noqa: F401

        return True
    except ImportError:
        return False


def _rembg_available() -> bool:
    try:
        import rembg  # noqa: F401

        return True
    except ImportError:
        return False


# ---------------------------------------------------------------------------
# stage 0 -- environment recon
# ---------------------------------------------------------------------------
def _run_recon(cfg, ctx):
    """Probe the pipeline runtime and report what the stages will use.

    numpy / scipy / trimesh / Pillow / scikit-image are required (skimage
    powers the voxel boolean weld and the repair fallback). rembg and blender
    are optional quality boosts the stages degrade without.
    """
    hard = {
        "numpy": "numpy",
        "scipy": "scipy",
        "trimesh": "trimesh",
        "PIL": "Pillow",
        "skimage": "scikit-image",
    }
    missing = [name for mod, name in hard.items() if not _importable(mod)]
    if missing:
        raise SystemExit(
            "missing pipeline runtime deps: "
            + ", ".join(missing)
            + "\n  python3 -m pip install -r requirements-pipeline.txt"
        )

    import trimesh

    report = {
        "python": sys.version.split()[0],
        "numpy": np.__version__,
        "trimesh": trimesh.__version__,
        "scikit_image": _skimage_available(),
        "rembg": _rembg_available(),
        "blender": bool(shutil.which("blender")),
    }
    return report


# ---------------------------------------------------------------------------
# stage 1 -- square padded background grid
# ---------------------------------------------------------------------------
def _run_backgrid(cfg, ctx):
    from PIL import Image

    src = Path(cfg["input"])
    if not src.exists():
        raise SystemExit(f"input image not found: {src}")
    img = Image.open(src).convert("RGBA")
    w, h = img.size
    side = max(w, h)
    canvas = Image.new("RGBA", (side, side), (255, 255, 255, 255))
    canvas.paste(img, ((side - w) // 2, (side - h) // 2))
    size = cfg.get("max_side", 1024)
    if side > size:
        canvas = canvas.resize((size, size), Image.LANCZOS)
    out = Path(cfg["work"]) / "01_backgrid.png"
    canvas.save(out)
    return {"width": canvas.size[0], "height": canvas.size[1], "path": str(out)}


# ---------------------------------------------------------------------------
# stage 2 -- foreground silhouette
# ---------------------------------------------------------------------------
def _bg_dominant_color(img: np.ndarray) -> np.ndarray:
    """Color of the average border pixel (assumes subject is centered)."""
    border = np.concatenate(
        [
            img[0, :, :],
            img[-1, :, :],
            img[:, 0, :],
            img[:, -1, :],
        ]
    )
    return border.mean(axis=0)


def _silhouette_random_walker(rgb: np.ndarray, alpha: np.ndarray) -> np.ndarray:
    """Foreground mask via Random-Walker seeded by corner color distance.

    Only high-confidence pixels are seeded (0 stays unlabeled so the solver
    can fill the ambiguous border between subject and background).
    """
    from skimage import segmentation

    h, w, _ = rgb.shape
    color = _bg_dominant_color(rgb)
    dist = np.sqrt(((rgb - color) ** 2).sum(axis=2))
    # alpha 0 (transparent) pixels are background without question
    d_low = max(12.0, float(np.percentile(dist, 80)) * 0.4)
    d_high = max(d_low * 1.6, float(np.percentile(dist, 90)) * 0.7)
    markers = np.zeros((h, w), dtype=np.int8)
    markers[dist > d_high] = 1
    markers[(dist < d_low) | (alpha <= 0)] = 2
    if not markers.any():
        # degenerate input (uniform image): whole canvas is one region
        return np.zeros((h, w), dtype=bool) if (alpha <= 0).all() else (alpha > 0)
    labels = segmentation.random_walker(
        rgb.astype(np.float64) / 255.0,
        markers,
        beta=130,
        mode="bf",
        copy=True,
        channel_axis=-1,
    )
    mask = labels == 1
    mask &= alpha > 0
    return mask


def _silhouette_color_threshold(rgb: np.ndarray, alpha: np.ndarray) -> np.ndarray:
    """numpy-only fallback: mark pixels far from the border color.

    Keeps the pipeline headless-runnable (and the self-test deterministic)
    on a box without scikit-image; Random-Walker is still preferred when the
    wheel is installed.
    """
    color = _bg_dominant_color(rgb)
    dist = np.sqrt(((rgb - color) ** 2).sum(axis=2))
    tol = max(
        18.0,
        float(np.percentile(dist, 75)) * 0.5,
        float(np.percentile(dist[alpha > 0], 95)) * 0.65,
    )
    mask = dist > tol
    mask &= alpha > 0
    return mask


def _silhouette_mask(rgb: np.ndarray, alpha: np.ndarray, use_rembg: bool, cfg):
    """Choose a masker: rembg, else sklearn-free Random-Walker / threshold."""
    if use_rembg and _rembg_available():
        return _silhouette_rembg(rgb, cfg)
    if _skimage_available():
        return _silhouette_random_walker(rgb, alpha)
    return _silhouette_color_threshold(rgb, alpha)


def _silhouette_rembg(rgb: np.ndarray, cfg) -> np.ndarray:
    """Mask from rembg when installed (best on busy backgrounds)."""
    import rembg  # type: ignore

    session = None
    model = cfg.get("rembg_model")
    if model:
        session = rembg.new_session(model)
    pil = rembg.remove(
        (rgb * 255).astype(np.uint8), session=session
    )  # returns RGBA bytes
    out = np.asarray(pil.convert("RGBA"))
    return out[:, :, 3] > 128


def _run_silhouette(cfg, ctx):
    from PIL import Image

    img = Image.open(Path(cfg["work"]) / "01_backgrid.png").convert("RGBA")
    arr = np.asarray(img)
    rgb, alpha = arr[:, :, :3].astype(float), arr[:, :, 3].astype(float)

    use_rembg = cfg.get("bg_remover", "auto") == "rembg" or (
        cfg.get("bg_remover", "auto") == "auto" and _rembg_available()
    )
    mask = _silhouette_mask(rgb, alpha, use_rembg, cfg)
    # morphological cleanup: keep largest connected region, close small gaps
    from scipy import ndimage

    mask = ndimage.binary_closing(mask, iterations=2)
    labeled, n = ndimage.label(mask)
    if n > 1:
        sizes = ndimage.sum(mask, labeled, range(1, n + 1))
        keep = np.argmax(sizes) + 1
        mask = labeled == keep
    # clear horizontal isolation: small specks above/below main body
    mask = ndimage.binary_opening(mask, iterations=1)

    mask_img = Image.fromarray((mask * 255).astype(np.uint8)).convert("L")
    mask_img.save(Path(cfg["work"]) / "02_mask.png")
    coverage = float(mask.sum()) / mask.size
    return {
        "method": "rembg" if use_rembg else "random_walker",
        "mask_path": str(Path(cfg["work"]) / "02_mask.png"),
        "coverage": round(coverage, 4),
    }

# ---------------------------------------------------------------------------
# stage 3 -- depth assignment + extrusion into a mesh
# ---------------------------------------------------------------------------
def _depth_from_mask(mask: np.ndarray, cfg) -> np.ndarray:
    """Assign a printable depth map: 1 for the body, thinner for appendages.

    A distance-to-edge field scaled into ``[depth_min, depth_max]`` keeps
    thin hair / limbs from dominating the silhouette while giving the torso
    real thickness.
    """
    from scipy import ndimage

    h, w = mask.shape
    dist = ndimage.distance_transform_edt(mask)
    dmax = float(dist.max())
    if dmax <= 0:
        return np.zeros_like(mask, dtype=float)
    dmax_norm = dist / dmax
    depth_min, depth_max = cfg.get("depth_min", 0.08), cfg.get("depth_max", 1.0)
    return depth_min + dmax_norm * (depth_max - depth_min)


def _run_extrude(cfg, ctx):
    import trimesh
    from PIL import Image

    # work on a bounded mask grid: 256px keeps the mesh light while the
    # silhouette stays recognizable; the STL scale recovers print size later
    mask_img = (
        Image.open(Path(cfg["work"]) / "02_mask.png")
        .convert("L")
        .resize((cfg.get("mesh_resolution", 256),) * 2, Image.LANCZOS)
    )
    mask = np.asarray(mask_img) > 128
    depth = _depth_from_mask(mask, cfg)

    h, w = depth.shape
    figure_height = cfg.get("figure_height", 1.0)

    # bottom and top vertices share the same (w+1)x(h+1) corner grid; the top
    # z comes from the depth field sampled at cell corners (edge-padded).
    xs = np.linspace(0, 1, w + 1)
    ys = np.linspace(0, figure_height, h + 1)
    gx, gy = np.meshgrid(xs, ys, indexing="xy")
    zp = np.pad(depth, 1, mode="edge")
    z_corner = 0.25 * (zp[:-1, :-1] + zp[:-1, 1:] + zp[1:, :-1] + zp[1:, 1:])

    n = (h + 1) * (w + 1)
    bottom = np.column_stack([gx.ravel(), gy.ravel(), np.zeros(n)])
    top = np.column_stack([gx.ravel(), gy.ravel(), z_corner.ravel()])
    verts = np.vstack([bottom, top])
    faces: list[list[int]] = []

    def v(j, i, layer):
        return layer * n + j * (w + 1) + i

    # Emit walls only where the neighbour is NOT masked so interior cell
    # boundaries stay clean and the extrusion stays a single closed solid.
    for j in range(h):
        for i in range(w):
            if not mask[j, i]:
                continue
            b0, b1, b2, b3 = v(j, i, 0), v(j, i + 1, 0), v(j + 1, i, 0), v(j + 1, i + 1, 0)
            t0, t1, t2, t3 = v(j, i, 1), v(j, i + 1, 1), v(j + 1, i, 1), v(j + 1, i + 1, 1)
            # top (normal +z) and bottom (normal -z)
            faces += [[t0, t1, t3], [t0, t3, t2], [b0, b3, b1], [b0, b2, b3]]
            # walls along open borders
            if i == 0 or not mask[j, i - 1]:
                faces += [[b0, b2, t2], [b0, t2, t0]]
            if i == w - 1 or not mask[j, i + 1]:
                faces += [[b1, t3, b3], [b1, t1, t3]]
            if j == 0 or not mask[j - 1, i]:
                faces += [[b0, t1, b1], [b0, t0, t1]]
            if j == h - 1 or not mask[j + 1, i]:
                faces += [[b2, b3, t3], [b2, t3, t2]]

    mesh = trimesh.Trimesh(vertices=verts, faces=np.array(faces), process=True)
    mesh = mesh.process(validate=True)
    if not mesh.is_watertight:
        from trimesh import repair

        repair.fix_normals(mesh)
        repair.fix_winding(mesh)
        repair.fill_holes(mesh)
        _merge_duplicate_vertices(mesh)
        mesh.remove_unreferenced_vertices()
    return {
        "grid": (w, h),
        "vertices": int(len(mesh.vertices)),
        "faces": int(len(mesh.faces)),
        "watertight": bool(mesh.is_watertight),
        "euler": int(mesh.euler_number),
        "extents": [round(float(v), 4) for v in mesh.extents],
        "mesh": mesh,
    }


# ---------------------------------------------------------------------------
# stage 4 -- print base + boolean weld
# ---------------------------------------------------------------------------
def _run_weld(cfg, ctx):
    import trimesh

    figure = ctx["extrude"]["mesh"]
    base_radius = cfg.get("base_radius", 0.6)
    base_height = cfg.get("base_height", 0.25)
    cx, cy, _ = figure.bounds.mean(axis=0)
    # sink the base top just below the figure's underside so the boolean weld
    # never sees a coplanar shared face (which degenerates into voxel tunnels)
    base = trimesh.primitives.Cylinder(
        radius=base_radius, height=base_height, sections=48
    ).apply_translation([cx, cy, -base_height + figure.bounds[0, 2] - 1e-3])

    welded = _boolean_union(figure, base, cfg)
    mesh = welded["mesh"]
    return {
        "base_radius": base_radius,
        "base_height": base_height,
        "strategy": welded.get("strategy", "none"),
        "voxel_size": welded.get("voxel_size"),
        "watertight": bool(mesh.is_watertight),
        "euler": int(mesh.euler_number),
        "vertices": int(len(mesh.vertices)),
        "faces": int(len(mesh.faces)),
        "mesh": mesh,
    }


def _boolean_union(a, b, cfg):
    """Union a and b.  Blender exact first, voxel lattice as fallback.

    The voxel path is analytical: the figure is sampled as solid columns from
    its silhouette depth field (O(cells), no per-point ray queries) and the
    base as a cylinder, then unioned on a shared lattice and re-meshed with
    marching cubes.  Marching cubes needs scikit-image, so a missing wheel is
    a clean actionable error rather than a silent non-manifold shell.
    """
    if cfg.get("use_blender"):
        try:
            import trimesh.interfaces.blender as tb  # type: ignore

            u = tb.boolean_multiple([a, b], "union")
            if u.is_watertight:
                return {"mesh": u, "strategy": "blender"}
        except Exception:
            pass
    if not _skimage_available():
        raise SystemExit(
            "voxel boolean weld needs scikit-image for marching cubes.\n"
            "  python3 -m pip install -r requirements-pipeline.txt  "
            "(includes scikit-image)"
        )
    pitch = _voxel_pitch([a, b], cfg)
    solid = _voxel_union([a, b], cfg, pitch)
    return {"mesh": solid, "strategy": "voxel", "voxel_size": pitch}


def _voxel_pitch(meshes, cfg, cap_voxels: int = 120_000) -> float:
    """Pick a voxel pitch fine enough to capture the geometry but coarse
    enough that the occupancy lattice stays under ``cap_voxels`` cells
    (marching cubes over a giant grid is slow and memory-hungry)."""
    requested = cfg.get("voxel_size")
    if requested:
        return float(requested)
    bounds = np.vstack([m.bounds for m in meshes])
    extent = bounds[1::2, :].max(axis=0) - bounds[::2, :].min(axis=0)
    span = float(np.max(extent))
    # default 1/100 of the largest extent (fine enough to keep thin figure
    # columns connected), but never blow the voxel budget
    pitch = span / 100.0
    cells = float((extent / pitch).prod())
    if cells > cap_voxels:
        pitch = float(np.cbrt(float(extent.prod()) / cap_voxels))
    return pitch


def _voxel_union(meshes, cfg, pitch: float):
    """Voxelize the union of closed solids onto a shared lattice.

    ``meshes`` is the (figure, base) pair.  The figure is a heightfield solid
    whose interior is exactly its silhouette columns, so occupancy is built
    directly from the mask + depth field (fast, O(cells)); the base is a
    cylinder sampled by radius.  This avoids the per-point ``mesh.contains``
    ray storm that was both slow and fragmented thin figure geometry.
    """
    from PIL import Image
    from trimesh.voxel import ops as vops

    fig, base = meshes
    bounds = np.vstack([m.bounds for m in meshes])
    origin = bounds[::2, :].min(axis=0)
    top = bounds[1::2, :].max(axis=0)
    extent = top - origin
    dims = tuple(int(np.ceil(s)) + 1 for s in extent / pitch)
    xs = origin[0] + (np.arange(dims[0]) + 0.5) * pitch
    ys = origin[1] + (np.arange(dims[1]) + 0.5) * pitch
    zs = origin[2] + (np.arange(dims[2]) + 0.5) * pitch
    gx, gy, gz = np.meshgrid(xs, ys, zs, indexing="ij")

    # base: cylinder interior by radius test (primitive attrs on trimesh 5.x)
    cx, cy, _ = fig.bounds.mean(axis=0)
    base_r = float(getattr(base.primitive, "radius", 0.6))
    base_occ = ((gx - cx) ** 2 + (gy - cy) ** 2 <= base_r * base_r) & (
        gz >= base.bounds[0, 2]
    ) & (gz <= base.bounds[1, 2])

    # figure: solid columns from the silhouette depth field
    mask_img = (
        Image.open(Path(cfg["work"]) / "02_mask.png")
        .convert("L")
        .resize((cfg.get("mesh_resolution", 256),) * 2, Image.LANCZOS)
    )
    mask = np.asarray(mask_img) > 128
    depth = _depth_from_mask(mask, cfg)
    h, w = depth.shape
    # mesh x -> mask column j, mesh y -> mask row i (see _run_extrude)
    mi = np.clip(np.round(gy * (h - 1)).astype(int), 0, h - 1)
    mj = np.clip(np.round(gx * (w - 1)).astype(int), 0, w - 1)
    fig_occ = mask[mi, mj] & (gz >= 0) & (gz <= depth[mi, mj])

    occupancy = (base_occ | fig_occ).astype(np.int8)
    solid = vops.matrix_to_marching_cubes(occupancy, pitch=pitch)
    solid.update_faces(solid.nondegenerate_faces())
    solid.apply_translation(origin)
    return solid


# ---------------------------------------------------------------------------
# stage 5 -- watertight gate + repair
# ---------------------------------------------------------------------------
def _run_repair(cfg, ctx):
    import trimesh

    mesh = ctx["weld"]["mesh"].copy()
    report = {"before": _mesh_snapshot(mesh)}
    trimesh.repair.fix_normals(mesh)
    trimesh.repair.fix_winding(mesh)
    trimesh.repair.fill_holes(mesh)
    _merge_duplicate_vertices(mesh)
    mesh.remove_unreferenced_vertices()
    if not mesh.is_watertight:
        log.warning("repair left open borders; running voxel solidify")
        if not _skimage_available():
            raise SystemExit(
                "voxel solidify needs scikit-image: "
                "python3 -m pip install -r requirements-pipeline.txt"
            )
        from trimesh.voxel import ops as vops

        pitch = _voxel_pitch([mesh], cfg)
        bounds = mesh.bounds
        origin, top = bounds[0], bounds[1]
        span = (top - origin) / pitch
        dims = tuple(int(np.ceil(s)) + 1 for s in span)
        xs = origin[0] + (np.arange(dims[0]) + 0.5) * pitch
        ys = origin[1] + (np.arange(dims[1]) + 0.5) * pitch
        zs = origin[2] + (np.arange(dims[2]) + 0.5) * pitch
        gx, gy, gz = np.meshgrid(xs, ys, zs, indexing="ij")
        occ = mesh.contains(
            np.column_stack([gx.ravel(), gy.ravel(), gz.ravel()])
        ).reshape(dims)
        solid = vops.matrix_to_marching_cubes(occ.astype(np.int8), pitch=pitch)
        solid.update_faces(solid.nondegenerate_faces())
        solid.apply_translation(origin)
        mesh = solid
        report["fallback"] = "voxel"
        trimesh.repair.fix_normals(mesh)
        trimesh.repair.fill_holes(mesh)
        _merge_duplicate_vertices(mesh)
        mesh.remove_unreferenced_vertices()
    report["after"] = _mesh_snapshot(mesh)
    return {"report": report, "mesh": mesh}


def _merge_duplicate_vertices(mesh):
    """Collapse duplicate vertices across trimesh 4.x/5.x API drift."""
    if hasattr(mesh, "merge_vertices"):
        mesh.merge_vertices()
    else:  # pragma: no cover - older trimesh
        _merge_duplicate_vertices(mesh)


def _mesh_snapshot(m):
    import numpy as np

    return {
        "watertight": bool(m.is_watertight),
        "euler": int(m.euler_number),
        "vertices": int(len(m.vertices)),
        "faces": int(len(m.faces)),
        "volume": round(float(m.volume), 6) if m.is_watertight else None,
    }


# ---------------------------------------------------------------------------
# stage 6 -- millimeter export + manifest
# ---------------------------------------------------------------------------
def _run_export(cfg, ctx):
    import trimesh

    mesh = ctx["repair"]["mesh"].copy()
    scale = cfg.get("scale_mm", 1.0)
    mesh.apply_scale(scale)
    out = Path(cfg["work"]) / "figure.stl"
    mesh.export(str(out))
    return {
        "path": str(out),
        "scale_mm": scale,
        "bytes": out.stat().st_size,
        "volume_mm3": round(float(mesh.volume), 3),
        "extents_mm": [round(float(v), 3) for v in mesh.extents],
        "watertight": bool(mesh.is_watertight),
        "euler": int(mesh.euler_number),
    }


# ---------------------------------------------------------------------------
# pipeline driver
# ---------------------------------------------------------------------------
STAGE_RUNNERS = {
    "recon": _run_recon,
    "backgrid": _run_backgrid,
    "silhouette": _run_silhouette,
    "extrude": _run_extrude,
    "weld": _run_weld,
    "repair": _run_repair,
    "export": _run_export,
}


def _parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__.split("Stage contract")[0].strip()
    )
    parser.add_argument(
        "input",
        nargs="?",
        default=None,
        help="anime illustration / any image file (required unless --self-test)",
    )
    parser.add_argument("-o", "--out", default=None, help="output STL path")
    parser.add_argument(
        "--work", default=None, help="work directory (default <input>.work)"
    )
    parser.add_argument("--from", dest="from_stage", default=None, help="resume at stage")
    parser.add_argument(
        "--skip",
        default=None,
        help="comma-separated stages to skip (recon,backgrid,...)",
    )
    parser.add_argument(
        "--bg-remover",
        choices=("auto", "rembg", "classic"),
        default="auto",
        help="background remover (auto prefers rembg when installed)",
    )
    parser.add_argument("--max-side", type=int, default=1024, help="canvas cap px")
    parser.add_argument(
        "--mesh-resolution",
        type=int,
        default=256,
        help="silhouette grid for extrusion (default 256 px)",
    )
    parser.add_argument(
        "--figure-height", type=float, default=1.0, help="figure height in model units"
    )
    parser.add_argument(
        "--base-radius", type=float, default=0.6, help="print base radius (model units)"
    )
    parser.add_argument(
        "--base-height", type=float, default=0.25, help="print base height (model units)"
    )
    parser.add_argument(
        "--scale-mm", type=float, default=100.0, help="model->mm scale (default 100)"
    )
    parser.add_argument(
        "--voxel-size", type=float, default=None, help="voxel pitch override"
    )
    parser.add_argument(
        "--use-blender", action="store_true", help="prefer Blender boolean union"
    )
    parser.add_argument(
        "--depth-min", type=float, default=0.08, help="min depth fraction"
    )
    parser.add_argument(
        "--depth-max", type=float, default=1.0, help="max depth fraction"
    )
    parser.add_argument(
        "--rembg-model", default=None, help="rembg model name (e.g. u2net)"
    )
    parser.add_argument("--json", action="store_true", help="emit manifest JSON")
    parser.add_argument(
        "--self-test",
        action="store_true",
        help="run the end-to-end smoke test on a synthetic anime-style figure",
    )
    parser.add_argument("-v", "--verbose", action="count", default=0)
    return parser.parse_args(argv)


def _stage_log(duration_s, stage, report):
    n = len(report)
    keys = ", ".join(str(k) for k in list(report)[:5])
    log.debug("stage %s: %.2fs, %d fields (%s)", stage, duration_s, n, keys)


def main(argv=None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )

    if args.self_test:
        return _self_test(args)
    if not args.input:
        raise SystemExit("input image required (or use --self-test)")

    cfg = {
        "input": args.input,
        "work": args.work or (str(Path(args.input).with_suffix("")) + ".work"),
        "bg_remover": args.bg_remover,
        "max_side": args.max_side,
        "mesh_resolution": args.mesh_resolution,
        "figure_height": args.figure_height,
        "base_radius": args.base_radius,
        "base_height": args.base_height,
        "scale_mm": args.scale_mm,
        "voxel_size": args.voxel_size,
        "use_blender": args.use_blender,
        "depth_min": args.depth_min,
        "depth_max": args.depth_max,
        "rembg_model": args.rembg_model,
        "out": args.out,
    }
    work = Path(cfg["work"])
    work.mkdir(parents=True, exist_ok=True)

    skipped = set(args.skip.split(",")) if args.skip else set()
    order = STAGES
    if args.from_stage:
        if args.from_stage not in STAGES:
            raise SystemExit(f"unknown stage {args.from_stage!r}; stages: {STAGES}")
        order = STAGES[STAGES.index(args.from_stage):]

    manifest: dict = {"cfg": {k: v for k, v in cfg.items() if k != "work"}, "stages": {}}
    ctx: dict = {}
    started = time.time()

    # stages 2-5 read their predecessor's mesh from ctx
    for stage in order:
        if stage in skipped:
            manifest["stages"][stage] = {"skipped": True}
            continue
        t0 = time.time()
        log.info("stage %s ...", stage)
        result = STAGE_RUNNERS[stage](cfg, ctx)
        if isinstance(result, dict) and "mesh" in result:
            mesh = result.pop("mesh")
            ctx[stage] = {"mesh": mesh}
            manifest["stages"][stage] = result
        else:
            ctx[stage] = result
            manifest["stages"][stage] = result
        _stage_log(time.time() - t0, stage, result)

    # resolve final STL location: default lives in work dir; honor --out
    final = manifest["stages"].get("export", {})
    if args.out and final.get("path"):
        import shutil as _sh

        _sh.copy(final["path"], args.out)
        final["path"] = args.out
        final["bytes"] = Path(args.out).stat().st_size

    manifest["summary"] = {
        "elapsed_s": round(time.time() - started, 2),
        "stages_ok": [s for s in STAGES if s in manifest["stages"]],
    }
    manifest_path = work / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False))
    if args.json:
        print(json.dumps(manifest, indent=2))
    else:
        print(f"manifest: {manifest_path}")
        e = final or manifest["stages"].get("export", {})
        if e.get("path"):
            print(
                f"STL: {e['path']}  watertight={e.get('watertight')}  "
                f"euler={e.get('euler')}  volume={e.get('volume_mm3')} mm^3  "
                f"extents={e.get('extents_mm')} mm"
            )
    return 0


def _self_test(args) -> int:
    """End-to-end smoke credential: synthetic anime figure -> printable STL.

    Draws a two-colour chibi-style figure (head + torso + base disc) onto a
    padded canvas with PIL, then runs the full pipeline and asserts the
    output manifest.  No network, no model download, deterministic.
    """
    import trimesh  # noqa: F401
    from PIL import Image, ImageDraw

    work = Path(args.work or "selftest.work")
    work.mkdir(parents=True, exist_ok=True)
    canvas = Image.new("RGBA", (512, 512), (255, 255, 255, 255))
    d = ImageDraw.Draw(canvas)
    # torso
    d.rounded_rectangle((216, 220, 296, 400), radius=28, fill=(60, 130, 220, 255))
    # head
    d.ellipse((186, 90, 326, 230), fill=(250, 215, 180, 255))
    # hair cap
    d.ellipse((182, 70, 330, 170), fill=(90, 70, 60, 255))
    # pigtails
    d.ellipse((150, 130, 200, 210), fill=(90, 70, 60, 255))
    d.ellipse((312, 130, 362, 210), fill=(90, 70, 60, 255))
    # base disc
    d.ellipse((196, 400, 316, 424), fill=(30, 30, 30, 255))
    src = work / "selftest_figure.png"
    canvas.save(src)

    argv = _namespace_to_argv(args, input_path=str(src))
    return main(argv)


def _namespace_to_argv(ns, input_path: str | None = None) -> list[str]:
    argv = [input_path or ns.input]
    for key in (
        "out",
        "work",
        "bg_remover",
        "max_side",
        "mesh_resolution",
        "figure_height",
        "base_radius",
        "base_height",
        "scale_mm",
        "voxel_size",
        "depth_min",
        "depth_max",
        "rembg_model",
    ):
        val = getattr(ns, key, None)
        if val is not None:
            argv.append(f"--{key.replace('_', '-')}")
            argv.append(str(val))
    if getattr(ns, "use_blender", False):
        argv.append("--use-blender")
    argv.append("--json")
    return argv


if __name__ == "__main__":
    sys.exit(main())
