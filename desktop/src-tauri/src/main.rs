// Release builds are GUI-subsystem binaries (no console window).
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

fn main() {
    asr_recorder::app::run();
}
