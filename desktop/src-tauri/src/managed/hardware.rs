//! GPU detection and the cpu / cuda / rocm suggestions of `managed_backend_detect`.
//!
//! Detection lists the display adapters (Windows: DXGI; Linux: PCI class 03 in sysfs) and maps
//! them to the torch builds `backend/pyproject.toml` can install:
//!
//! * NVIDIA → `cuda` (pytorch.org's CUDA 12.6 build: Maxwell .. Hopper; no Blackwell kernels, so an
//!   RTX 50 / Blackwell card is reported as unavailable).
//! * AMD gfx1151 (Strix Halo: Radeon 8060S / 8050S / 8040S, PCI id 1002:1586) → `rocm`. On Windows
//!   the `rocm` extra installs AMD's TheRock build, which ships gfx1151 kernels only, so any
//!   other AMD GPU (e.g. Radeon 890M = gfx1150) is reported as unavailable with the reason.
//! * Everything else → `cpu`.

use crate::ipc::{HardwareSuggestion, ManagedHardware};

pub const VENDOR_AMD: u32 = 0x1002;
pub const VENDOR_NVIDIA: u32 = 0x10DE;
pub const VENDOR_MICROSOFT: u32 = 0x1414;

/// Approximate one-time downloads (Python + locked packages; the contract's figures).
pub const DOWNLOAD_CPU: u64 = 1_000_000_000;
pub const DOWNLOAD_CUDA: u64 = 2_500_000_000;
pub const DOWNLOAD_ROCM: u64 = 5_000_000_000;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Gpu {
    pub name: String,
    pub vendor_id: u32,
    pub device_id: u32,
}

/// The gfx target of an AMD GPU, from its PCI device id or (as a fallback) its marketing name.
pub fn amd_gfx_target(device_id: u32, name: &str) -> Option<&'static str> {
    match device_id {
        0x1586 => return Some("gfx1151"), // Strix Halo
        0x150E => return Some("gfx1150"), // Strix Point
        _ => {}
    }
    let n = name.to_ascii_uppercase();
    let has = |s: &str| n.contains(s);
    if has("8060S") || has("8050S") || has("8040S") {
        Some("gfx1151")
    } else if has("890M") || has("880M") {
        Some("gfx1150")
    } else if has("860M") || has("840M") || has("820M") {
        Some("gfx1152")
    } else {
        None
    }
}

/// The ROCm target the bundled Windows build has kernels for.
pub const ROCM_TARGET: &str = "gfx1151";

/// NVIDIA GPUs without kernels in the CUDA 12.6 torch build (Blackwell, sm_100 / sm_120).
fn nvidia_unsupported(name: &str) -> bool {
    let n = name.to_ascii_uppercase();
    n.contains("RTX 50")
        || n.contains("BLACKWELL")
        || n.contains(" B100")
        || n.contains(" B200")
        || n.contains("GB10")
}

/// cpu / cuda / rocm suggestions for these adapters, exactly one `recommended`
/// (an available CUDA GPU first, then ROCm, else CPU). `detail` says why an option is not
/// available.
pub fn suggest(gpus: &[Gpu]) -> Vec<HardwareSuggestion> {
    let real: Vec<&Gpu> = gpus
        .iter()
        .filter(|g| g.vendor_id != VENDOR_MICROSOFT)
        .collect();
    let item = |id, available, label: String, detail: Option<String>, bytes| HardwareSuggestion {
        id,
        available,
        recommended: false,
        label,
        detail,
        download_bytes: Some(bytes),
    };

    let nvidia: Vec<&Gpu> = real
        .iter()
        .copied()
        .filter(|g| g.vendor_id == VENDOR_NVIDIA)
        .collect();
    let cuda_ok = nvidia.iter().find(|g| !nvidia_unsupported(&g.name));
    let cuda = match (cuda_ok, nvidia.first()) {
        (Some(g), _) => item(
            ManagedHardware::Cuda,
            true,
            format!("{} (CUDA 12.6)", g.name.trim()),
            None,
            DOWNLOAD_CUDA,
        ),
        (None, Some(g)) => item(
            ManagedHardware::Cuda,
            false,
            format!("{} (CUDA)", g.name.trim()),
            Some(
                "此 GPU（Blackwell / RTX 50 系列）需要 CUDA 12.8+，内置的 CUDA 12.6 版 PyTorch 不支持 / \
                 This GPU (Blackwell / RTX 50 series) needs CUDA 12.8+; the bundled CUDA 12.6 PyTorch build has no kernels for it"
                    .into(),
            ),
            DOWNLOAD_CUDA,
        ),
        (None, None) => item(
            ManagedHardware::Cuda,
            false,
            "NVIDIA GPU (CUDA)".into(),
            Some("未检测到 NVIDIA 显卡 / No NVIDIA GPU found".into()),
            DOWNLOAD_CUDA,
        ),
    };

    let amd: Vec<(&Gpu, Option<&'static str>)> = real
        .iter()
        .copied()
        .filter(|g| g.vendor_id == VENDOR_AMD)
        .map(|g| (g, amd_gfx_target(g.device_id, &g.name)))
        .collect();
    let rocm_ok = amd.iter().find(|(_, t)| *t == Some(ROCM_TARGET));
    let rocm = match (rocm_ok, amd.first()) {
        (Some((g, t)), _) => item(
            ManagedHardware::Rocm,
            true,
            format!("{} (ROCm, {})", g.name.trim(), t.unwrap_or(ROCM_TARGET)),
            None,
            DOWNLOAD_ROCM,
        ),
        (None, Some((g, t))) => item(
            ManagedHardware::Rocm,
            false,
            format!("{} (ROCm)", g.name.trim()),
            Some(format!(
                "此 GPU 为 {}，内置的 Windows ROCm 版 PyTorch 只包含 {ROCM_TARGET}（Radeon 8060S / 8050S / 8040S）的内核 / \
                 This GPU is {}; the bundled Windows ROCm PyTorch build only has {ROCM_TARGET} kernels (Radeon 8060S / 8050S / 8040S)",
                t.unwrap_or("未知型号"),
                t.unwrap_or("an unknown target")
            )),
            DOWNLOAD_ROCM,
        ),
        (None, None) => item(
            ManagedHardware::Rocm,
            false,
            "AMD GPU (ROCm)".into(),
            Some("未检测到 AMD 显卡 / No AMD GPU found".into()),
            DOWNLOAD_ROCM,
        ),
    };

    let cpu = item(ManagedHardware::Cpu, true, "CPU".into(), None, DOWNLOAD_CPU);
    let mut out = vec![cpu, cuda, rocm];
    let pick = if out[1].available {
        1
    } else if out[2].available {
        2
    } else {
        0
    };
    out[pick].recommended = true;
    out
}

/// Display adapters of this PC (best effort; empty when they cannot be listed).
pub fn detect_gpus() -> Vec<Gpu> {
    let mut v = platform_gpus();
    let mut seen = std::collections::HashSet::new();
    v.retain(|g| seen.insert((g.vendor_id, g.device_id, g.name.clone())));
    v
}

#[cfg(windows)]
fn platform_gpus() -> Vec<Gpu> {
    use windows::Win32::Graphics::Dxgi::{
        CreateDXGIFactory1, IDXGIFactory1, DXGI_ADAPTER_FLAG_SOFTWARE,
    };
    let mut out = Vec::new();
    // SAFETY: plain DXGI enumeration; every returned interface is released by its Drop.
    unsafe {
        let factory: IDXGIFactory1 = match CreateDXGIFactory1() {
            Ok(f) => f,
            Err(e) => {
                log::warn!("managed backend: DXGI unavailable: {e}");
                return out;
            }
        };
        let mut i = 0;
        while let Ok(adapter) = factory.EnumAdapters1(i) {
            i += 1;
            let Ok(desc) = adapter.GetDesc1() else {
                continue;
            };
            if desc.Flags & (DXGI_ADAPTER_FLAG_SOFTWARE.0 as u32) != 0 {
                continue;
            }
            let len = desc
                .Description
                .iter()
                .position(|c| *c == 0)
                .unwrap_or(desc.Description.len());
            out.push(Gpu {
                name: String::from_utf16_lossy(&desc.Description[..len]),
                vendor_id: desc.VendorId,
                device_id: desc.DeviceId,
            });
        }
    }
    out
}

#[cfg(target_os = "linux")]
fn platform_gpus() -> Vec<Gpu> {
    let hex = |p: std::path::PathBuf| {
        std::fs::read_to_string(p)
            .ok()
            .and_then(|s| u32::from_str_radix(s.trim().trim_start_matches("0x"), 16).ok())
    };
    let Ok(rd) = std::fs::read_dir("/sys/bus/pci/devices") else {
        return Vec::new();
    };
    rd.flatten()
        .filter_map(|d| {
            let p = d.path();
            let class = hex(p.join("class"))?;
            if class >> 16 != 0x03 {
                return None;
            }
            let (vendor_id, device_id) = (hex(p.join("vendor"))?, hex(p.join("device"))?);
            Some(Gpu {
                name: format!("PCI {vendor_id:04x}:{device_id:04x}"),
                vendor_id,
                device_id,
            })
        })
        .collect()
}

#[cfg(not(any(windows, target_os = "linux")))]
fn platform_gpus() -> Vec<Gpu> {
    Vec::new()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn gpu(name: &str, vendor_id: u32, device_id: u32) -> Gpu {
        Gpu {
            name: name.into(),
            vendor_id,
            device_id,
        }
    }

    fn by(v: &[HardwareSuggestion], id: ManagedHardware) -> &HardwareSuggestion {
        v.iter().find(|s| s.id == id).unwrap()
    }

    fn recommended(v: &[HardwareSuggestion]) -> ManagedHardware {
        let r: Vec<_> = v.iter().filter(|s| s.recommended).collect();
        assert_eq!(r.len(), 1, "exactly one recommended: {v:?}");
        r[0].id
    }

    #[test]
    fn strix_halo_gets_rocm() {
        // what DXGI reports on the dev machine (Radeon 8060S, PCI\VEN_1002&DEV_1586)
        let v = suggest(&[
            gpu("AMD Radeon(TM) 8060S Graphics", 0x1002, 0x1586),
            gpu("Microsoft Basic Render Driver", 0x1414, 0x8c),
        ]);
        assert_eq!(v.len(), 3);
        assert_eq!(recommended(&v), ManagedHardware::Rocm);
        let r = by(&v, ManagedHardware::Rocm);
        assert!(r.available && r.label.contains("8060S") && r.label.contains("gfx1151"));
        assert_eq!(r.detail, None);
        assert_eq!(r.download_bytes, Some(DOWNLOAD_ROCM));
        assert!(!by(&v, ManagedHardware::Cuda).available);
        assert!(by(&v, ManagedHardware::Cpu).available);
        // by name when the device id is unknown (e.g. a future board revision)
        let v = suggest(&[gpu("AMD Radeon 8050S", 0x1002, 0xffff)]);
        assert_eq!(recommended(&v), ManagedHardware::Rocm);
    }

    #[test]
    fn other_amd_gpus_explain_why_rocm_is_unavailable() {
        let v = suggest(&[gpu("AMD Radeon 890M Graphics", 0x1002, 0x150E)]);
        assert_eq!(recommended(&v), ManagedHardware::Cpu);
        let r = by(&v, ManagedHardware::Rocm);
        assert!(!r.available);
        let d = r.detail.as_deref().unwrap();
        assert!(d.contains("gfx1150") && d.contains("gfx1151"), "{d}");
        let v = suggest(&[gpu("AMD Radeon RX 7900 XTX", 0x1002, 0x744C)]);
        assert!(!by(&v, ManagedHardware::Rocm).available);
        assert_eq!(recommended(&v), ManagedHardware::Cpu);
    }

    #[test]
    fn nvidia_gets_cuda_except_blackwell() {
        let v = suggest(&[
            gpu("AMD Radeon(TM) 8060S Graphics", 0x1002, 0x1586),
            gpu("NVIDIA GeForce RTX 4070", 0x10DE, 0x2786),
        ]);
        assert_eq!(recommended(&v), ManagedHardware::Cuda);
        assert!(by(&v, ManagedHardware::Rocm).available);
        let v = suggest(&[gpu("NVIDIA GeForce RTX 5090", 0x10DE, 0x2B85)]);
        let c = by(&v, ManagedHardware::Cuda);
        assert!(!c.available && c.detail.as_deref().unwrap().contains("12.8"));
        assert_eq!(recommended(&v), ManagedHardware::Cpu);
    }

    #[test]
    fn no_gpu_means_cpu() {
        for gpus in [
            vec![],
            vec![gpu("Intel(R) Arc(TM) Graphics", 0x8086, 0x7D55)],
        ] {
            let v = suggest(&gpus);
            assert_eq!(recommended(&v), ManagedHardware::Cpu);
            assert!(by(&v, ManagedHardware::Cuda).detail.is_some());
            assert!(by(&v, ManagedHardware::Rocm).detail.is_some());
            let cpu = by(&v, ManagedHardware::Cpu);
            assert_eq!(cpu.detail, None);
            assert_eq!(cpu.download_bytes, Some(DOWNLOAD_CPU));
        }
    }

    #[test]
    fn gfx_targets() {
        assert_eq!(amd_gfx_target(0x1586, ""), Some("gfx1151"));
        assert_eq!(amd_gfx_target(0, "AMD Radeon 8040S"), Some("gfx1151"));
        assert_eq!(amd_gfx_target(0, "AMD Radeon 880M"), Some("gfx1150"));
        assert_eq!(amd_gfx_target(0, "AMD Radeon 860M"), Some("gfx1152"));
        assert_eq!(amd_gfx_target(0, "AMD Radeon RX 6600"), None);
    }

    #[test]
    fn detection_does_not_panic() {
        let _ = detect_gpus();
    }
}
