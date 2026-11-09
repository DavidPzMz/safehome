// Evita que se abra una consola extra en Windows en la version final, NO QUITAR!!
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

// solo arranca la app, todo lo demas esta en lib.rs
fn main() {
    safehome_lib::ejecutar()
}
