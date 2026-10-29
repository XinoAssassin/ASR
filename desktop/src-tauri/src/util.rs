//! Small shared helpers.

use std::io::Write;
use std::path::Path;

/// Write a file atomically (temp file in the same directory + rename).
pub fn write_atomic(path: &Path, data: &[u8]) -> std::io::Result<()> {
    if let Some(dir) = path.parent() {
        std::fs::create_dir_all(dir)?;
    }
    let tmp = path.with_extension("tmp");
    {
        let mut f = std::fs::File::create(&tmp)?;
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
