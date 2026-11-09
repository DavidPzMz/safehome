# SafeHome

Aplicación de videovigilancia para casa: revisa la cámara, detecta actividad
sospechosa en la puerta (forzar cerradura, empujar, trepar, mirar alrededor) y
manda una alerta por correo con la foto.

**Integrantes:** David Pérez Martínez · Hugo Cadena Venancio

---

## Programa (Linux)

1. Entra a **Releases** (columna derecha de esta página) y descarga
   `SafeHome-linux.tar.gz`.
2. Descomprímelo y abre una terminal dentro de la carpeta.
3. Instala (solo la primera vez, tarda unos minutos):
   ```bash
   ./instalar.sh
   ```
4. Ábrelo desde el menú de aplicaciones (**SafeHome**) o con:
   ```bash
   ./SafeHome.sh
   ```

Necesita internet la primera vez.

---

## Código

| Carpeta | Qué tiene |
| --- | --- |
| `src/` | La interfaz (HTML, CSS y JavaScript) |
| `python-backend/` | La detección (YOLO-pose + OpenCV), el servidor y las alertas |
| `src-tauri/` | La ventana de escritorio (Tauri / Rust) |


1. Instala Python 3.11+, Node.js 18+ y Rust ([rustup.rs](https://rustup.rs)).
2. Librerías de Python:
   ```bash
   cd python-backend
   python3 -m venv .venv
   .venv/bin/pip install -r requirements.txt
   cd ..
   ```
3. Librerías de la interfaz:
   ```bash
   npm install
   ```
4. Ejecutar en modo desarrollo:
   ```bash
   npm run tauri dev
   ```
5. Compilar el programa final:
   ```bash
   cd src-tauri && cargo build --release
   ```

El modelo `yolov8n-pose.pt` se descarga solo la primera vez que se ejecuta.
