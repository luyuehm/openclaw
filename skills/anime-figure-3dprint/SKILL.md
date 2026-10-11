---
name: anime_figure_3dprint
description: >-
  OpenClaw anime-3D-figure 3D-printing workflow: anime illustration (or any
  image) -> silhouette -> extruded figure mesh -> print base boolean-weld ->
  non-manifold repair/solidification -> millimetre-unit binary STL. Use when
  the user wants to turn an anime/PNG illustration into a printable figure
  model (.stl) or fix a broken mesh for printing.
metadata:
  openclaw:
    emoji: 🧸
    requires:
      bins: ["python3"]
---

# Anime Figure 3D-Print Workflow

Turns an anime illustration (PNG/JPG/WebP or an existing mesh) into a
watertight, manifold, millimetre-unit binary STL that a slicer will accept.
The whole pipeline is local and headless; no Blender is required (it is only
an optional quality boost).

## When to use

- "把这张动漫立绘做成可打印的3D模型 / STL"
- "turn this anime art into a printable figure"
- "fix this mesh so it 3D prints" / "repair non-manifold STL"
- Any request for a print-ready STL from an image or a broken mesh.

## Pipeline

Stages run headless and write a JSON manifest into `<input>.work/manifest.json`:

1. `recon`      — verify Python deps (trimesh, scikit-image, numpy, rtree).
2. `backgrid`   — square, padded, white/alpha-normalized canvas, size cap.
3. `silhouette` — foreground mask. Uses `rembg` when installed; otherwise a
   local Random-Walker seeded by corner background colour (no model download).
4. `extrude`    — depth-from-silhouette (thicker for the torso, thinner for
   hair/limbs) -> closed heightfield mesh.
5. `weld`       — print base (cylinder) boolean-unioned onto the figure.
   Blender-free: overlapping parts are rasterised on a shared voxel lattice
   and re-meshed with marching cubes, so intersecting geometry can never leave
   non-manifold seams.
6. `repair`     — watertightness gate; trimesh fill_holes/fix_normals, then a
   voxel solidify fallback if the surface still leaks.
7. `export`     — uniform millimetre scale (default 100 model units -> mm),
   binary STL, manifest.

Preview PNGs (`01_backgrid.png`, `02_mask.png`) are written into the work dir
so a human can inspect each stage even though the run is headless.

## Commands

Image -> printable figure:

```bash
python3 scripts/anime_3dprint_pipeline.py character.png \
  -o character.stl --scale-mm 100 --bg-remover auto
```

Run with `rembg` for busy anime backgrounds once it is installed:

```bash
pip install rembg onnxruntime
python3 scripts/anime_3dprint_pipeline.py busy-art.png --bg-remover rembg
```

Fix / solidify an existing mesh (default surface repair; `--voxel` is the
bulletproof fallback for badly broken input):

```bash
python3 scripts/mesh_to_print.py broken.glb -o fixed.stl --scale-mm 10
python3 scripts/mesh_to_print.py broken.stl --voxel --voxel-size 0.5 -o solid.stl
```

End-to-end smoke credential (synthetic anime figure -> mm STL, no network):

```bash
python3 scripts/anime_3dprint_pipeline.py --self-test
```

## Output contract

Every run produces a JSON manifest:

- `stages.<stage>` — per-stage report (counts, watertightness, method).
- `stages.export`  — STL path, `scale_mm`, `volume_mm3`, `extents_mm`,
  `watertight`, `euler` (2 for genus-0 closed solids), bytes.

Pass `--json` to print the manifest to stdout. The STL lands in
`<input>.work/figure.stl` unless `-o` is given.

**Quality gates** (all must hold in the manifest before handoff):

| Gate | Value |
| ---- | ----- |
| `stages.export.watertight` | `true` |
| `stages.export.euler` | `0` or `2` (genus-0 closed shell) |
| STL slices in a slicer | a solid model with no dangling sheets |
| `--self-test` | exit 0, full 7-stage manifest, watertight euler 0 STL |

## Known limits

- The image->mesh path is a 2.5D extrusion, not a full 3D reconstruction:
  good for chibi/statue silhouettes and sturdy print bases, not for
  articulated or undercut-heavy models.
- `--bg-remover classic` (Random-Walker) works best on clean backgrounds;
  busy scenes need `rembg`.
- Voxel welding resolution is auto-capped (~120k cells) for memory; pass
  `--voxel-size` to trade resolution for fidelity.

## Safety

- All stages run on the local machine; no network calls, no model download
  by default (rembg downloads its ONNX model on first use, opt-in).
- Never run user-supplied scripts from an untrusted image; raster data is
  treated as data only.