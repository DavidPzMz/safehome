use std::path::PathBuf;
use std::process::Stdio;
use std::sync::Mutex;
use tauri::{
    menu::{Menu, MenuItem},
    tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent},
    Emitter, Manager, AppHandle,
};
use tokio::process::{Command as ComandoTokio, Child};

// Direccion donde escucha el servidor de Python
const URL_SERVIDOR: &str = "http://127.0.0.1:8765/";

// Aqui se guarda el proceso de Python para poder matarlo al cerrar
static PROCESO_PYTHON: Mutex<Option<Child>> = Mutex::new(None);

/// La aplicacion de pruebas es el mismo programa pero abierto con `--pruebas` o desde
/// un enlace que tenga "pruebas" en el nombre (por ejemplo `safehome-pruebas`).
fn es_modo_pruebas() -> bool {
    let mut argumentos = std::env::args();
    let por_nombre = argumentos
        .next()
        .map(|a| {
            std::path::Path::new(&a)
                .file_name()
                .map(|n| n.to_string_lossy().to_lowercase().contains("pruebas"))
                .unwrap_or(false)
        })
        .unwrap_or(false);
    por_nombre || argumentos.any(|a| a == "--pruebas")
}

// nombre que sale en la bandeja y en el menu
fn nombre_app() -> &'static str {
    if es_modo_pruebas() { "SafeHome Pruebas" } else { "SafeHome" }
}

// busca la carpeta del servidor de Python (python-backend)
fn carpeta_servidor_python(aplicacion: &AppHandle) -> PathBuf {
    // primero en los recursos del paquete instalado
    if let Ok(carpeta_recursos) = aplicacion.path().resource_dir() {
        for sub in ["python-backend", "usr/lib/SafeHome/python-backend"] {
            let ruta = carpeta_recursos.join(sub);
            if ruta.exists() {
                return ruta;
            }
        }
    }

    // en desarrollo: junto al ejecutable o una carpeta arriba
    let local = PathBuf::from("python-backend");
    if local.exists() {
        return local.canonicalize().unwrap_or(local);
    }

    let arriba = PathBuf::from("../python-backend");
    if arriba.exists() {
        return arriba.canonicalize().unwrap_or(arriba);
    }

    PathBuf::from("python-backend")
}

// usa el Python del entorno virtual (.venv) si existe, si no el del sistema
fn ejecutable_python(carpeta: &PathBuf) -> PathBuf {
    #[cfg(target_os = "windows")]
    let nombre_ejecutable = "python.exe";
    #[cfg(not(target_os = "windows"))]
    let nombre_ejecutable = "python";

    let python_venv = carpeta.join(".venv").join("bin").join(nombre_ejecutable);
    if python_venv.exists() {
        return python_venv;
    }

    PathBuf::from("python3")
}

// arranca el servidor de Python y espera a que conteste (hasta 10 s)
async fn iniciar_servidor_python(aplicacion: &AppHandle) -> Result<(), String> {
    let carpeta = carpeta_servidor_python(aplicacion);
    let python = ejecutable_python(&carpeta);
    let servidor = carpeta.join("servidor_api.py");

    println!("Iniciando servidor Python desde: {:?}", carpeta);
    println!("Ejecutable Python: {:?}", python);
    println!("Archivo del servidor: {:?}", servidor);

    // si ya estaba corriendo (por ejemplo otra ventana) no se abre otro
    let cliente = reqwest::Client::new();
    if cliente.get(URL_SERVIDOR).send().await.is_ok() {
        println!("El servidor Python ya estaba corriendo.");
        return Ok(());
    }

    let mut comando = ComandoTokio::new(&python);
    comando.arg(&servidor)
        .env("SAFEHOME_MODO", if es_modo_pruebas() { "pruebas" } else { "final" })
        .current_dir(&carpeta)
        .stdout(Stdio::inherit())
        .stderr(Stdio::inherit());

    let proceso = comando.spawn().map_err(|e| format!("Error al iniciar el proceso de Python: {}", e))?;

    if let Ok(mut candado) = PROCESO_PYTHON.lock() {
        *candado = Some(proceso);
    }

    for _ in 0..20 {
        tokio::time::sleep(tokio::time::Duration::from_millis(500)).await;
        if cliente.get(URL_SERVIDOR).send().await.is_ok() {
            println!("Servidor FastAPI conectado y listo en {}", URL_SERVIDOR);
            return Ok(());
        }
    }

    println!("Advertencia: se acabo el tiempo esperando a FastAPI, pero el proceso sigue corriendo.");
    Ok(())
}

// mata el proceso de Python
async fn detener_servidor_python() {
    let proceso = if let Ok(mut candado) = PROCESO_PYTHON.lock() {
        candado.take()
    } else {
        None
    };

    if let Some(mut proceso) = proceso {
        let _ = proceso.kill().await;
        let _ = proceso.wait().await;
        println!("Proceso de Python detenido.");
    }
}

// El panel pregunta si es la aplicacion final o la de pruebas
#[tauri::command]
fn modo_app() -> String {
    if es_modo_pruebas() { "pruebas".into() } else { "final".into() }
}

// Esconde la ventana pero la vigilancia sigue en segundo plano
#[tauri::command]
fn ocultar_ventana(aplicacion: AppHandle) {
    if let Some(ventana) = aplicacion.get_webview_window("main") {
        let _ = ventana.hide();
    }
}

// Cierra todo: el servidor de Python y la aplicacion
#[tauri::command]
async fn cerrar_aplicacion(aplicacion: AppHandle) {
    detener_servidor_python().await;
    aplicacion.exit(0);
}

// vuelve a enseñar la ventana (desde la bandeja)
fn mostrar_ventana(aplicacion: &AppHandle) {
    if let Some(ventana) = aplicacion.get_webview_window("main") {
        let _ = ventana.show();
        let _ = ventana.unminimize();
        let _ = ventana.set_focus();
    }
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn ejecutar() {
    tauri::Builder::default()
        .plugin(tauri_plugin_opener::init())
        .plugin(tauri_plugin_notification::init())
        .plugin(tauri_plugin_http::init())
        // funciones que el panel (JavaScript) puede llamar
        .invoke_handler(tauri::generate_handler![
            ocultar_ventana,
            cerrar_aplicacion,
            modo_app,
        ])
        .setup(|aplicacion| {
            let manejador = aplicacion.handle().clone();

            if es_modo_pruebas() {
                if let Some(ventana) = aplicacion.get_webview_window("main") {
                    let _ = ventana.set_title("SafeHome PRUEBAS - Videovigilancia");
                }
            }

            // el servidor de Python se arranca aparte para no trabar la ventana
            tauri::async_runtime::spawn(async move {
                if let Err(e) = iniciar_servidor_python(&manejador).await {
                    eprintln!("Error iniciando el servidor Python: {}", e);
                }
            });

            // icono en la bandeja con su menu (mostrar / salir)
            let mostrar = MenuItem::with_id(aplicacion, "mostrar", format!("Mostrar {}", nombre_app()), true, None::<&str>)?;
            let salir = MenuItem::with_id(aplicacion, "salir", "Salir", true, None::<&str>)?;
            let menu = Menu::with_items(aplicacion, &[&mostrar, &salir])?;

            TrayIconBuilder::with_id("safehome-bandeja")
                .icon(aplicacion.default_window_icon().unwrap().clone())
                .tooltip(format!("{} - Videovigilancia Inteligente", nombre_app()))
                .menu(&menu)
                .show_menu_on_left_click(true)
                .on_menu_event(move |aplicacion, evento| match evento.id.as_ref() {
                    "mostrar" => mostrar_ventana(aplicacion),
                    "salir" => {
                        tauri::async_runtime::block_on(async {
                            detener_servidor_python().await;
                        });
                        aplicacion.exit(0);
                    }
                    _ => {}
                })
                .on_tray_icon_event(|bandeja, evento| {
                    if let TrayIconEvent::Click {
                        button: MouseButton::Left,
                        button_state: MouseButtonState::Up,
                        ..
                    } = evento
                    {
                        mostrar_ventana(bandeja.app_handle());
                    }
                })
                .build(aplicacion)?;

            Ok(())
        })
        .on_window_event(|ventana, evento| {
            if let tauri::WindowEvent::CloseRequested { api, .. } = evento {
                // no se cierra luego luego: se le avisa al panel para que
                // pregunte si cerrar solo la ventana o todo el servicio
                api.prevent_close();
                let _ = ventana.emit("cierre-solicitado", ());
            }
        })
        .run(tauri::generate_context!())
        .expect("error al correr la aplicacion de SafeHome");
}
