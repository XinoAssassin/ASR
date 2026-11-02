//! Native folder picker for `pick_folder` (Windows: the shell's IFileOpenDialog in
//! pick-folders mode). No dialog plugin, so the WebView gets no file-system API of its own.

use std::path::{Path, PathBuf};

/// Show the dialog (blocking; call it off the UI thread). `owner` is the parent window handle
/// (the dialog is modal to it). Ok(None) = cancelled.
#[cfg(windows)]
pub fn pick_folder(
    owner: Option<isize>,
    title: Option<&str>,
    default: Option<&Path>,
) -> Result<Option<PathBuf>, String> {
    use windows::core::HSTRING;
    use windows::Win32::Foundation::{ERROR_CANCELLED, HWND};
    use windows::Win32::System::Com::{
        CoCreateInstance, CoInitializeEx, CoTaskMemFree, CoUninitialize, IBindCtx,
        CLSCTX_INPROC_SERVER, COINIT_APARTMENTTHREADED, COINIT_DISABLE_OLE1DDE,
    };
    use windows::Win32::UI::Shell::{
        FileOpenDialog, IFileOpenDialog, IShellItem, SHCreateItemFromParsingName,
        FOS_FORCEFILESYSTEM, FOS_PATHMUSTEXIST, FOS_PICKFOLDERS, SIGDN_FILESYSPATH,
    };

    // SAFETY: COM on this thread (balanced CoUninitialize when the init succeeded); the dialog
    // and shell items are released by their Drop; the returned path string is freed with
    // CoTaskMemFree as documented for GetDisplayName.
    unsafe {
        let init = CoInitializeEx(None, COINIT_APARTMENTTHREADED | COINIT_DISABLE_OLE1DDE);
        let result = (|| -> windows::core::Result<Option<PathBuf>> {
            let dialog: IFileOpenDialog =
                CoCreateInstance(&FileOpenDialog, None, CLSCTX_INPROC_SERVER)?;
            let opts = dialog.GetOptions()?;
            dialog.SetOptions(opts | FOS_PICKFOLDERS | FOS_FORCEFILESYSTEM | FOS_PATHMUSTEXIST)?;
            if let Some(t) = title.filter(|t| !t.trim().is_empty()) {
                dialog.SetTitle(&HSTRING::from(t))?;
            }
            // Start in the deepest existing folder of `default`.
            if let Some(start) = default.and_then(|d| d.ancestors().find(|a| a.is_dir())) {
                let item: windows::core::Result<IShellItem> = SHCreateItemFromParsingName(
                    &HSTRING::from(start.as_os_str()),
                    None::<&IBindCtx>,
                );
                if let Ok(item) = item {
                    let _ = dialog.SetFolder(&item);
                }
            }
            let hwnd = owner.map(|h| HWND(h as *mut core::ffi::c_void));
            if let Err(e) = dialog.Show(hwnd) {
                if e.code() == ERROR_CANCELLED.to_hresult() {
                    return Ok(None);
                }
                return Err(e);
            }
            let item = dialog.GetResult()?;
            let p = item.GetDisplayName(SIGDN_FILESYSPATH)?;
            let s = String::from_utf16_lossy(p.as_wide());
            CoTaskMemFree(Some(p.0 as *const core::ffi::c_void));
            Ok(Some(PathBuf::from(s)))
        })();
        if init.is_ok() {
            CoUninitialize();
        }
        result.map_err(|e| format!("folder picker: {e}"))
    }
}

#[cfg(not(windows))]
pub fn pick_folder(
    _owner: Option<isize>,
    _title: Option<&str>,
    _default: Option<&Path>,
) -> Result<Option<PathBuf>, String> {
    Err("the folder picker is only available on Windows".into())
}
