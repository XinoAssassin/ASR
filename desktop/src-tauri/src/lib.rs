//! ASR Recorder core: system-audio + microphone capture, FLAC finalisation and the persistent
//! upload queue. The Tauri shell (`app`, feature `app`) only wires these into commands/events
//! defined in `docs/desktop-ipc.md`.

pub mod config;
pub mod ipc;
pub mod recorder;
pub mod sidecar;
pub mod uploader;
pub mod util;

#[cfg(feature = "app")]
pub mod app;
