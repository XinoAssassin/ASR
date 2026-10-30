//! Small shared helpers.

use std::io::Write;
use std::path::Path;

/// Write a file atomically (temp file in the same directory + rename).
pub fn write_atomic(path: &Path, data: &[u8]) -> std::io::Result<()> {
    write_atomic_mode(path, data, false)
}

/// `write_atomic` for secrets: on Unix the file is created with mode 0600 (owner only), so it
/// is never readable by other users, not even briefly. (On Windows the per-user app data dir's
/// ACL already restricts it.)
pub fn write_atomic_private(path: &Path, data: &[u8]) -> std::io::Result<()> {
    write_atomic_mode(path, data, true)
}

fn write_atomic_mode(path: &Path, data: &[u8], private: bool) -> std::io::Result<()> {
    if let Some(dir) = path.parent() {
        std::fs::create_dir_all(dir)?;
    }
    let tmp = path.with_extension("tmp");
    // A leftover temp file would keep its old permissions: start from a fresh one.
    match std::fs::remove_file(&tmp) {
        Err(e) if e.kind() != std::io::ErrorKind::NotFound => return Err(e),
        _ => {}
    }
    let mut opts = std::fs::OpenOptions::new();
    opts.write(true).create_new(true);
    #[cfg(unix)]
    if private {
        use std::os::unix::fs::OpenOptionsExt;
        opts.mode(0o600);
    }
    #[cfg(not(unix))]
    let _ = private;
    {
        let mut f = opts.open(&tmp)?;
        f.write_all(data)?;
        f.sync_all()?;
    }
    std::fs::rename(&tmp, path)
}

/// Current local time formatted as the recording file stem `YYYY-MM-DD_HH-MM-SS`.
pub fn recording_stem(now: chrono::DateTime<chrono::Local>) -> String {
    now.format("%Y-%m-%d_%H-%M-%S").to_string()
}

pub fn iso(t: chrono::DateTime<chrono::Utc>) -> String {
    t.to_rfc3339_opts(chrono::SecondsFormat::Secs, true)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn atomic_write_replaces_and_leaves_no_temp() {
        let dir = tempfile::tempdir().unwrap();
        let p = dir.path().join("x.json");
        std::fs::write(p.with_extension("tmp"), b"stale").unwrap();
        write_atomic(&p, b"one").unwrap();
        write_atomic(&p, b"two").unwrap();
        assert_eq!(std::fs::read(&p).unwrap(), b"two");
        assert!(!p.with_extension("tmp").exists());
    }

    #[cfg(unix)]
    #[test]
    fn private_files_are_owner_only() {
        use std::os::unix::fs::PermissionsExt;
        let dir = tempfile::tempdir().unwrap();
        let p = dir.path().join("api-key");
        // a stale world-readable temp file must not pass its mode on
        std::fs::write(p.with_extension("tmp"), b"old").unwrap();
        std::fs::set_permissions(
            p.with_extension("tmp"),
            std::fs::Permissions::from_mode(0o644),
        )
        .unwrap();
        write_atomic_private(&p, b"k").unwrap();
        let mode = std::fs::metadata(&p).unwrap().permissions().mode() & 0o777;
        assert_eq!(mode, 0o600);
        assert_eq!(std::fs::read(&p).unwrap(), b"k");
    }
}
