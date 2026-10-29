fn main() {
    println!("cargo:rerun-if-changed=build.rs");
    println!("cargo:rerun-if-changed=windows-app-manifest.xml");
    #[cfg(feature = "app")]
    {
        // One manifest with everything: common controls v6 (Tauri dialogs), asInvoker and
        // long paths (what MinGW's default manifest would add).
        let windows = tauri_build::WindowsAttributes::new()
            .app_manifest(include_str!("windows-app-manifest.xml"));
        tauri_build::try_build(tauri_build::Attributes::new().windows_attributes(windows))
            .expect("tauri-build failed");
    }
    suppress_mingw_default_manifest();
}

/// MinGW's gcc driver links `default-manifest.o` into every executable, which collides with the
/// manifest resource above ("ld: .rsrc merge failure: multiple non-default manifests", two
/// RT_MANIFEST #1 entries). Shadow it with an empty COFF object found first via `-B`.
fn suppress_mingw_default_manifest() {
    let target_os = std::env::var("CARGO_CFG_TARGET_OS").unwrap_or_default();
    let target_env = std::env::var("CARGO_CFG_TARGET_ENV").unwrap_or_default();
    if target_os != "windows" || target_env != "gnu" {
        return;
    }
    let dir =
        std::path::PathBuf::from(std::env::var("OUT_DIR").unwrap()).join("no-default-manifest");
    std::fs::create_dir_all(&dir).unwrap();
    // IMAGE_FILE_HEADER only: machine AMD64 (or i386), no sections, no symbols.
    let machine: u16 = if std::env::var("CARGO_CFG_TARGET_ARCH").as_deref() == Ok("x86") {
        0x014c
    } else {
        0x8664
    };
    let mut obj = Vec::with_capacity(20);
    obj.extend_from_slice(&machine.to_le_bytes());
    obj.extend_from_slice(&[0u8; 18]);
    std::fs::write(dir.join("default-manifest.o"), obj).unwrap();
    println!("cargo:rustc-link-arg=-B{}", dir.display());
}
