mod docker;

use docker::{DockerDetection, EngineController, EngineStatus};
use std::{path::Path, process::Command, sync::Arc};
use tauri::{
    menu::{Menu, MenuItem},
    tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent},
    AppHandle, Manager, State,
};

struct AppState(Arc<EngineController>);

#[tauri::command]
async fn docker_detection(state: State<'_, AppState>) -> Result<DockerDetection, String> {
    let engine = state.0.clone();
    tauri::async_runtime::spawn_blocking(move || engine.detection()).await.map_err(|error| error.to_string())
}
#[tauri::command] async fn engine_status(state: State<'_, AppState>) -> Result<EngineStatus, String> { let engine = state.0.clone(); tauri::async_runtime::spawn_blocking(move || engine.status()).await.map_err(|error| error.to_string())?.map_err(|error| error.to_string()) }
#[tauri::command] async fn start_engine(state: State<'_, AppState>) -> Result<(), String> { let engine = state.0.clone(); tauri::async_runtime::spawn_blocking(move || engine.start()).await.map_err(|error| error.to_string())?.map_err(|error| error.to_string()) }
#[tauri::command] async fn pull_image(state: State<'_, AppState>) -> Result<(), String> { let engine = state.0.clone(); tauri::async_runtime::spawn_blocking(move || engine.pull()).await.map_err(|error| error.to_string())?.map_err(|error| error.to_string()) }
#[tauri::command] async fn run_engine(state: State<'_, AppState>) -> Result<(), String> { let engine = state.0.clone(); tauri::async_runtime::spawn_blocking(move || engine.run()).await.map_err(|error| error.to_string())?.map_err(|error| error.to_string()) }
#[tauri::command] async fn engine_logs(tail: u16, state: State<'_, AppState>) -> Result<String, String> { let engine = state.0.clone(); tauri::async_runtime::spawn_blocking(move || engine.logs(tail)).await.map_err(|error| error.to_string())?.map_err(|error| error.to_string()) }

#[tauri::command]
async fn launch_docker_desktop() -> Result<(), String> {
    tauri::async_runtime::spawn_blocking(|| {
        #[cfg(target_os = "macos")]
        { Command::new("open").args(["-a", "Docker"]).spawn()?.wait().map(|_| ()) }
        #[cfg(not(target_os = "macos"))]
        { Err(std::io::Error::new(std::io::ErrorKind::Unsupported, "Docker Desktop launch is only supported on macOS")) }
    }).await.map_err(|error| error.to_string())?.map_err(|error| error.to_string())
}

#[tauri::command]
async fn reveal_data_folder(state: State<'_, AppState>) -> Result<(), String> {
    let path = state.0.data_dir().to_owned();
    tauri::async_runtime::spawn_blocking(move || {
        std::fs::create_dir_all(&path)?;
        reveal(&path)
    }).await.map_err(|error| error.to_string())?.map_err(|error| error.to_string())
}

fn reveal(path: &Path) -> std::io::Result<()> {
    #[cfg(target_os = "macos")]
    let mut command = { let mut command = Command::new("open"); command.arg("-R").arg(path); command };
    #[cfg(target_os = "windows")]
    let mut command = { let mut command = Command::new("explorer"); command.arg(path); command };
    #[cfg(all(not(target_os = "macos"), not(target_os = "windows")))]
    let mut command = { let mut command = Command::new("xdg-open"); command.arg(path); command };
    command.spawn()?.wait().map(|_| ())
}

fn show_main_window(app: &AppHandle) {
    if let Some(window) = app.get_webview_window("main") { let _ = window.show(); let _ = window.set_focus(); }
}

fn setup_tray(app: &tauri::App) -> tauri::Result<()> {
    let open = MenuItem::with_id(app, "open", "Open Woltspace", true, None::<&str>)?;
    let quit = MenuItem::with_id(app, "quit", "Quit", true, None::<&str>)?;
    let menu = Menu::with_items(app, &[&open, &quit])?;
    TrayIconBuilder::new()
        .icon(app.default_window_icon().expect("configured app icon").clone())
        .menu(&menu)
        .tooltip("Woltspace")
        .on_menu_event(|app, event| match event.id.as_ref() { "open" => show_main_window(app), "quit" => app.exit(0), _ => {} })
        .on_tray_icon_event(|tray, event| {
            if let TrayIconEvent::Click { button: MouseButton::Left, button_state: MouseButtonState::Up, .. } = event { show_main_window(tray.app_handle()); }
        })
        .build(app)?;
    Ok(())
}

pub fn run() {
    let engine = EngineController::from_environment().expect("a home directory is required");
    tauri::Builder::default()
        .plugin(tauri_plugin_notification::init())
        .plugin(tauri_plugin_deep_link::init())
        .manage(AppState(Arc::new(engine)))
        .setup(|app| { setup_tray(app)?; Ok(()) })
        .on_window_event(|window, event| {
            if window.label() == "main" {
                if let tauri::WindowEvent::CloseRequested { api, .. } = event { api.prevent_close(); let _ = window.hide(); }
            }
        })
        .invoke_handler(tauri::generate_handler![docker_detection, engine_status, start_engine, pull_image, run_engine, engine_logs, reveal_data_folder, launch_docker_desktop])
        .run(tauri::generate_context!())
        .expect("error while running Woltspace");
}
