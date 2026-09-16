// Mobile deliberately uses the accepted foreground Web/Wasm host. It does not
// spawn the desktop Python daemon, Node helper or a local code agent.
#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .run(tauri::generate_context!())
        .expect("VibApp mobile host failed to start");
}
