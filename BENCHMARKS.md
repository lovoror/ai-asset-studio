# Benchmarks (RTX 5090, real jobs, 2026-09-12)

Machine: Windows 11 Pro, one RTX 5090 32 GB (driver 616.92), 46 GiB RAM available to the workers. Every number comes from
`samples/<job>/manifest.json` (aggregated in `samples/acceptance_report.json`). No upstream H100 figures are used.

> These runs were made on the earlier container layout. asset-studio now runs natively (see
> [`docs/DISTRIBUTED.md`](docs/DISTRIBUTED.md)); the stage code, models and settings are unchanged, so the numbers
> still describe the pipeline.

## Per-stage wall clock

| Asset | Preset | Reference candidates | Reference stage | Pixal3D stage (load / generate / export) | Blender stage | Total (stages) |
|---|---|---|---|---|---|---|
| crate | balanced (1024) | 1 × 285 s | 339 s | 248 s (52 / 112 / 56) | 78 s | 11.1 min |
| pump | quality (1536) | 2 × 297 s, 288 s | 631 s | 226 s (54 / 85 / 77) | 80 s | 15.6 min |
| mug | balanced (1024) | 1 × 306 s | 347 s | 250 s (48 / 53 / 125) | 74 s | 11.2 min |

Qwen-Image-2512 runs at the model card's full-quality settings (50 steps, true CFG 4.0, 1328×1328, BF16): 5.7–6.0 s/step
with the transformer split GPU/CPU (PCIe-bound). Pixal3D pays ~52 s of model loading per job because each stage is a fresh
subprocess (this is what guarantees CUDA memory is released between stages).

## Memory

| Stage | torch max reserved | nvidia-smi peak (whole GPU) | process peak RSS |
|---|---|---|---|
| crate: Qwen reference (36/60 blocks resident) | 30.7 GB | 31.3 GB | 21.0 GB |
| crate: Pixal3D 1024 low_vram | generate 15.0 GB, export 12.1 GB | 18.7 GB | 26.7 GB |
| pump: Qwen reference (36/60 blocks resident) | 30.5 GB | 31.1 GB | 21.8 GB |
| pump: Pixal3D 1536 low_vram | generate 24.3 GB, export 14.4 GB | 26.7 GB | 26.9 GB |
| mug: Qwen reference (36/60 blocks resident) | 30.5 GB | 31.1 GB | 21.7 GB |
| mug: Pixal3D 1024 low_vram | generate 15.0 GB, export 8.5 GB | 16.4 GB | 26.7 GB |

The Qwen stage peaked at the 32 GB ceiling without failing; the default is now 30 resident blocks + VAE tiling (≈2.7 GB less).

## Reduction results (meshoptimizer, error-bounded)

| Asset | Master tris | Optimized | Method | Relative error | LOD1 / LOD2 | Collision | Validation |
|---|---|---|---|---|---|---|---|
| crate | 954,901 | 7,998 | voxel_proxy_decimate_rebake | 1.4 % | 3,997 / 1,996 | 200 | all ok |
| pump | 997,520 | 19,803 | decimate_rebake | 2.3 % | 9,813 / 5,227 | 200 | all ok |
| mug | 969,785 | 6,000 | decimate_rebake | 1.4 % | 3,088 / 1,500 | 254 | all ok |

`decimate_rebake` = meshoptimizer `simplifyWithAttributes` (normals, prune) directly on the welded master within a 5 % error
bound; `voxel_proxy_decimate_rebake` = the bound could not be met (thin multi-shell planks), so a single-shell voxel proxy of the
copy was reduced instead. All maps (base colour, metallic, roughness, normal) are baked from the untouched master.

## Previews (master bottom-left, reference top-left, optimized asset elsewhere)

### crate

![crate](samples/crate/previews/contact_sheet.png)

### pump

![pump](samples/pump/previews/contact_sheet.png)

### mug

![mug](samples/mug/previews/contact_sheet.png)

## Robustness

* Cancel mid-stage: subprocess stopped within seconds, status `cancelled`.
* Worker restart mid-Pixal3D: job requeued, reference stage reused, completed.
* Reprocess: `POST /v1/jobs/{id}/retry {"from_stage":"blender","optimize":{...}}` re-optimised all three assets without regenerating.
* Khronos glTF validator: 0 errors on all 15 GLBs; headless Godot 4.7.2 import succeeded for all.
* Offline: workers run with `HF_HUB_OFFLINE=1`; the loaders were verified with the network disabled.
