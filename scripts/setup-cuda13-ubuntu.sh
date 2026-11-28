#!/usr/bin/env bash
# Install the CUDA 13 runtime libraries Agent-Hire's level model needs to run on an NVIDIA GPU (onnxruntime-gpu 1.32),
# straight from NVIDIA's signed Ubuntu 24.04 repository, the safe way:
#   * the NVIDIA apt repository is NOT added (its default pin, priority 600, lets `apt upgrade` replace Ubuntu's
#     Secure-Boot-signed driver with NVIDIA's own build);
#   * the driver is not touched (needs >= 580, which supports every CUDA 13.x release);
#   * /usr/local/cuda keeps pointing at your existing CUDA (the 13.x config package would otherwise take it over);
#   * every file is verified like apt does: InRelease signature (NVIDIA key) -> Packages.gz -> each .deb's SHA256.
#
#   scripts/setup-cuda13-ubuntu.sh [--with-cudnn] [--dry-run] [download_dir]
#
# Installs: cudart, cuBLAS/cuBLASLt, cuRAND (what onnxruntime's CUDA provider links, ~0.7 GB). --with-cudnn adds
# cuDNN 9 (~0.9 GB): onnxruntime lists it as a requirement, but the level model runs on CUDA without it (measured).
# --dry-run downloads and verifies everything and simulates the install, without sudo.
# Undo: sudo apt-get remove cuda-cudart-13-5 libcublas-13-5 libcurand-13-5 cuda-toolkit-13-5-config-common \
#       cuda-toolkit-13-config-common libcudnn9-cuda-13     (keep cuda-toolkit-config-common: CUDA 12 uses it too)
set -euo pipefail

CUDA_MINOR=13-5            # newest CUDA 13 in the repo (13.5); same versions measured working on a GTX 1660 Ti + 580
REPO=https://developer.download.nvidia.com/compute/cuda/repos/ubuntu2404/x86_64
KEYRING=/usr/share/keyrings/cuda-archive-keyring.gpg
WITH_CUDNN=0 DRY=0 DL=""
for arg in "$@"; do
  case "$arg" in
    --with-cudnn) WITH_CUDNN=1 ;;
    --dry-run) DRY=1 ;;
    -h|--help) sed -n 2,19p "$0"; exit 0 ;;
    *) DL=$arg ;;
  esac
done
DL=${DL:-$HOME/cuda13-debs}
say() { printf '\033[36m▸\033[0m %s\n' "$*"; }
ok() { printf '\033[32m✓\033[0m %s\n' "$*"; }
die() { printf '\033[31m✗\033[0m %s\n' "$*" >&2; exit 1; }

# ── preflight ────────────────────────────────────────────────────────────────
[ "$(uname -m)" = x86_64 ] || die "x86_64 only"
command -v nvidia-smi >/dev/null || die "no NVIDIA driver (nvidia-smi missing) — install the driver first (Ubuntu: Additional Drivers)"
drv=$(nvidia-smi --query-gpu=driver_version --format=csv,noheader | head -1)
[ "${drv%%.*}" -ge 580 ] || die "driver $drv is older than 580; CUDA 13 needs >= 580"
ok "driver $drv (CUDA 13 capable), GPU: $(nvidia-smi --query-gpu=name --format=csv,noheader | head -1)"
for tool in curl gpgv sha256sum python3; do command -v $tool >/dev/null || die "missing $tool"; done
if [ ! -f "$KEYRING" ]; then  # NVIDIA's signing key, from the cuda-keyring package (only the key file is used)
  say "fetching NVIDIA's repository key"
  mkdir -p "$DL" && curl -fsSL -o "$DL/cuda-keyring.deb" "$REPO/cuda-keyring_1.1-1_all.deb"
  dpkg-deb -x "$DL/cuda-keyring.deb" "$DL/keyring" && KEYRING=$(find "$DL/keyring" -name cuda-archive-keyring.gpg | head -1)
  [ -f "$KEYRING" ] || die "could not get the NVIDIA key"
fi
PKGS=(cuda-toolkit-config-common cuda-toolkit-13-config-common "cuda-toolkit-$CUDA_MINOR-config-common"
      "cuda-cudart-$CUDA_MINOR" "libcublas-$CUDA_MINOR" "libcurand-$CUDA_MINOR")
[ $WITH_CUDNN = 1 ] && PKGS+=(libcudnn9-cuda-13)

# ── download + verify ────────────────────────────────────────────────────────
mkdir -p "$DL" && cd "$DL"
say "verifying NVIDIA's repository signature"
curl -fsSL -o InRelease "$REPO/InRelease"
gpgv --keyring "$KEYRING" InRelease 2>/dev/null || die "InRelease signature check FAILED"
want=$(awk '/^SHA256:/{f=1;next} /^[^ ]/{f=0} f && $3=="Packages.gz"{print $1}' InRelease)
curl -fsSL -o Packages.gz "$REPO/Packages.gz"
echo "$want  Packages.gz" | sha256sum -c --quiet - || die "Packages.gz does not match the signed index"
ok "repository index signed by NVIDIA and intact"

python3 - "${PKGS[@]}" > plan.txt <<'PY' || die "a package is missing from the repository"
import gzip, re, subprocess, sys
best = {}
for rec in gzip.open("Packages.gz", "rt").read().split("\n\n"):
    f = dict(re.findall(r"^([\w-]+): (.*)$", rec, re.M))
    p, v = f.get("Package"), f.get("Version")
    if p in sys.argv[1:] and (p not in best or subprocess.run(["dpkg", "--compare-versions", v, "gt", best[p]["Version"]]).returncode == 0):
        best[p] = f
missing = [p for p in sys.argv[1:] if p not in best]
if missing:
    sys.exit(f"not in repo: {missing}")
for p in sys.argv[1:]:
    print(p, best[p]["Version"], best[p]["Filename"].split("/")[-1], best[p]["SHA256"], best[p]["Size"])
PY
debs=()
while read -r pkg ver file sha size; do
  if ! { [ -f "$file" ] && echo "$sha  $file" | sha256sum -c --quiet - 2>/dev/null; }; then
    say "downloading $pkg $ver ($((size / 1048576)) MB)"
    curl -fL --retry 3 -o "$file" "$REPO/$file"
    echo "$sha  $file" | sha256sum -c --quiet - || die "$file failed its checksum"
  fi
  debs+=("./$file")
done < plan.txt
ok "all packages downloaded and verified: $(cut -d' ' -f1,2 plan.txt | tr '\n' ' ')"

# ── install ──────────────────────────────────────────────────────────────────
prev_cuda=$(readlink -f /usr/local/cuda 2>/dev/null || true)
if [ $DRY = 1 ]; then
  say "dry run: simulating the install"
  apt-get install -s --no-install-recommends "${debs[@]}" | grep -E "^(Inst|Remv|Conf)" | sed 's/^/    /'
  apt-get install -s --no-install-recommends "${debs[@]}" | grep -qE "^Remv|^Inst (nvidia|libnvidia|linux-)" \
    && die "the simulation removes packages or touches the driver — stop" || ok "simulation: nothing removed, driver untouched"
  [ -n "$prev_cuda" ] && say "after install, /usr/local/cuda would be pinned back to $prev_cuda"
  exit 0
fi
say "installing (sudo)"
sudo apt-get install -y --no-install-recommends "${debs[@]}"
if [ -n "$prev_cuda" ] && [ "$(readlink -f /usr/local/cuda)" != "$prev_cuda" ]; then
  sudo update-alternatives --set cuda "$prev_cuda"   # keep nvcc / CUDA 12 projects on what they used before
  ok "/usr/local/cuda kept on $prev_cuda"
fi
sudo ldconfig

# ── check ────────────────────────────────────────────────────────────────────
for lib in libcudart.so.13 libcublas.so.13 libcublasLt.so.13 libcurand.so.10; do
  ldconfig -p | grep -q "$lib " && ok "$lib found by the system loader" || die "$lib not found after install"
done
[ $WITH_CUDNN = 1 ] && { ldconfig -p | grep -q "libcudnn.so.9 " && ok "libcudnn.so.9 found" || die "cuDNN not found"; }
ok "done. Restart Agent-Hire: its log should say 'level model loaded on CUDA'. Downloads in $DL can be deleted."
