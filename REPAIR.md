# REPAIR.md — CDNA vLLM asset repair manifest (for humans and agents)

You are probably here because a lemonade install on an AMD Instinct box (MI300X/gfx942 or
MI350X-class/gfx950) resolved a vLLM backend pin to a release asset that does not exist in
`lemonade-sdk/vllm-rocm`. This file is the maintained, machine-readable statement of what
exists, what to do, and when to stop trusting this file.

**Lifecycle:** this manifest governs ONLY the community bundles in this repo. It is
superseded — and this repo's bundles retire — the moment official CDNA assets exist in
`lemonade-sdk/vllm-rocm` (tracking PR: lemonade-sdk/vllm-rocm#28). Check that first, always.

## Ground truth (updated 2026-07-24)

- Official CDNA asset in `lemonade-sdk/vllm-rocm`: **absent** (RDNA lines only).
- lemonade pins: `rocm_arch_overrides.gfx942 = vllm0.19.1-rocm7.13.0` (since v11.0.0);
  `gfx950 = vllm0.19.1-rocm7.13.0` (by v11.5.0). The vLLM backend descriptor deliberately
  omits gfx942/gfx950 until official assets ship, so stock lemonade will not offer vLLM on
  CDNA yet — the sideload below is the interim path.
- Out of the box on stock lemonade ≥ v11.0.0 on MI300X: `llamacpp:rocm` text generation
  (nothing to sideload). Verify `lemonade --version` first: pre-v11 packages report
  "Unsupported GPU: gfx942".
- **No rocm7.14.0 dcgpu wheels exist** (AMD torch + vLLM indices, gfx94X and gfx950
  families — probe below). A `-rocm7.14.0` community tag cannot exist yet; do not repair
  toward one.

## Machine-readable manifest

```yaml
manifest_version: 2
status_as_of: 2026-07-24
supply_chain: >
  THIRD-PARTY community bundles from a personal account (ianbmacdonald), NOT
  lemonade-official. Assembled from AMD's official dcgpu wheel indices. Verify every
  sha256 below before use; do not proceed on a mismatch.
serves_via: >
  STANDALONE vLLM only (its own OpenAI API, default :8000). NOT through lemonade (:13305):
  lemonade's vLLM backend is hardwired to the official repo two ways — the download source
  is hardcoded (vllm_server.cpp: params.repo="lemonade-sdk/vllm-rocm"; the vllm.rocm_bin pin
  changes only the tag, never the repo) AND the backend descriptor omits gfx942/gfx950 from
  its GPU allow-list (vllm.h, "omitted until ... assets ship"). Sideloading + running vLLM
  standalone bypasses both. This bundle keeps working standalone regardless of anything below.
invalidated_by:
  # BOTH must hold before lemonade-through-:13305 serves vLLM on CDNA. The asset alone does
  # NOT flip it — the descriptor gate is a SEPARATE lemonade source change shipped in a release.
  official_asset: a release tag matching 'vllm.*-gfx94.*|vllm.*-gfx950.*' in
    https://api.github.com/repos/lemonade-sdk/vllm-rocm/releases   (clears the hardcoded-repo lock)
  AND_lemonade_release: a lemonade version whose vLLM descriptor re-adds gfx942/gfx950 to the
    vllm.h support list (clears the allow-list gate) — gated at a LEMONADE RELEASE, no sooner
  note: the community sideload (this bundle, standalone) is unaffected by either and stays valid;
    the two conditions above only govern when lemond (:13305) will serve vLLM NATIVELY on CDNA
verify_sha256: required   # agents: refuse to install on mismatch; do not skip
bundles:
  gfx942:
    validation: hardware-validated (real MI300X; warm single-stream 306-511 tok/s on
      Qwen2.5-0.5B under vLLM; CUDA graphs captured)
    tag: vllm0.19.1-rocm7.13.0-gfx942
    reassemble: |
      gh release download vllm0.19.1-rocm7.13.0-gfx942 -R ianbmacdonald/vllm-rocm-cdna -p 'vllm0.19.1-rocm7.13.0-gfx942-x64.part*'
      cat vllm0.19.1-rocm7.13.0-gfx942-x64.part*-of-*.tar.gz > vllm0.19.1-rocm7.13.0-gfx942-x64.tar.gz
    parts_sha256:
      part01: 6eb80bb43ba1d74eeeacf4b03bd7531bca4769f48dafad3b18c201eb2d456d19
      part02: e3eb53907e4e2bbebb0ed059a9d7dcf765539d6e45d3f24d0008847b62b04086
    joined_sha256: b515fa054812d60ab0a508226f4edf37bff83df379197262607ca87a98a87171
    runbook: MI300X-gfx942-MVP-runbook.md (release asset, same tag)
  gfx950:
    validation: build-validated ONLY (portable-python import check green; NO CDNA4
      hardware validation yet — reports welcome in this repo's issues)
    tag: vllm0.19.1-rocm7.13.0-gfx950
    reassemble: |
      gh release download vllm0.19.1-rocm7.13.0-gfx950 -R ianbmacdonald/vllm-rocm-cdna -p 'vllm0.19.1-rocm7.13.0-gfx950-x64.part*'
      cat vllm0.19.1-rocm7.13.0-gfx950-x64.part*-of-*.tar.gz > vllm0.19.1-rocm7.13.0-gfx950-x64.tar.gz
    parts_sha256:
      part01: db83f8e280af40baa43523f6f4cef2bbb8260bf177f7a11495fa8d97a4481b7d
      part02: 1c92cf6ac8cb6a99e62d39eb915bbdca09abd4d3597e623eefc4b03e9a572163
    joined_sha256: e5d2d886f2d591457f4f28c1d4d91a050c055c234ce70dede6af65665ed0ad89
host_requirements:
  gpu_driver: amdgpu with KFD (/dev/kfd present); SR-IOV guest VF fine (validated shape)
  host_rocm: bundle carries its own ROCm 7.13 userspace; host needs only the kernel
    driver. Validated host = Ubuntu 24.04, kernel 6.8. Expected to coexist with a ROCm
    7.14 host install; not exhaustively tested.
  distro: validated Ubuntu 24.04 x86_64; other glibc distros untested
rocm714_wheels: absent (probed 2026-07-24; re-run the probe below rather than trusting
  this date)
self_heal:
  install_target: lemonade managed backend install dir (per runbook) — same-directory
    stage-and-swap on update, so official bits REPLACE community bits on the next pin bump
  avoid_config_pins: [vllm.rocm_bin]   # a stale pin outlives updates and produces a
                                        # hybrid install harder to debug than either state
  do_not: fake or hand-edit version.txt to a future tag — that suppresses the official
    update forever
  after_official_ships: applies only once BOTH invalidated_by conditions hold — the asset is
    published AND you have upgraded to a lemonade release that re-added the CDNA descriptor
    (the asset alone changes nothing for the :13305 path). Then: upgrade lemonade -> verify the
    backend update installed the official asset -> confirm `vllm.rocm_bin` is unset -> community
    dir contents are already replaced (same-dir swap); nothing else to clean. Until that lemonade
    release exists, stay on the standalone bundle.
```

## Re-verify our claims yourself (agents especially)

```bash
# wheels-absence probe (positive-control it against 7.13 — that MUST return nonzero):
curl -fsSL https://repo.amd.com/rocm/whl/gfx94X-dcgpu/torch/ | grep -c 'rocm7\.14' # expect 0
curl -fsSL https://repo.amd.com/rocm/whl/gfx94X-dcgpu/torch/ | grep -c 'rocm7\.13' # expect >0
# official-asset check (the invalidation predicate):
gh api repos/lemonade-sdk/vllm-rocm/releases --jq '.[].tag_name' | grep -E 'gfx94|gfx950'
```

Questions / bug reports: **this repo's issues** — not the upstream lemonade or vllm-rocm
trackers, which are for the official path.
