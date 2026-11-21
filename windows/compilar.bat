@echo off
rem Compila SafeHome en Windows y arma la carpeta lista para repartir:
rem   windows\SafeHome-windows\  (SafeHome.exe + python-backend + instalar.bat)
rem Necesita Rust (rustup.rs) y Visual Studio Build Tools con "Desarrollo de escritorio con C++".
setlocal
cd /d "%~dp0.."
set RAIZ=%cd%
set SALIDA=%RAIZ%\windows\SafeHome-windows

where cargo >nul 2>nul
if errorlevel 1 (
    echo Falta Rust. Instalalo desde https://rustup.rs y vuelve a correr este archivo.
    pause
    exit /b 1
)

echo === Compilando la aplicacion (la primera vez tarda varios minutos) ===
cd /d "%RAIZ%\src-tauri"
cargo build --release
if errorlevel 1 (
    echo Error al compilar.
    if defined CI exit /b 1
    pause
    exit /b 1
)

echo === Armando la carpeta %SALIDA% ===
if exist "%SALIDA%" rmdir /s /q "%SALIDA%"
mkdir "%SALIDA%"
copy /y "%RAIZ%\src-tauri\target\release\safehome.exe" "%SALIDA%\SafeHome.exe" >nul
copy /y "%RAIZ%\windows\instalar.bat" "%SALIDA%\" >nul
copy /y "%RAIZ%\windows\LEEME.txt" "%SALIDA%\" >nul
copy /y "%RAIZ%\src-tauri\icons\icon.ico" "%SALIDA%\icono.ico" >nul

rem el backend sin el entorno virtual, videos, logs ni caches
robocopy "%RAIZ%\python-backend" "%SALIDA%\python-backend" /e /xd .venv venv videos logs __pycache__ evidencias /xf remitente.json *.pyc >nul
if errorlevel 8 (
    echo Error copiando python-backend.
    if defined CI exit /b 1
    pause
    exit /b 1
)

rem el modelo YOLO (si no esta, se descarga solo al primer uso)
if not exist "%SALIDA%\python-backend\yolov8n-pose.pt" (
    echo === Descargando el modelo yolov8n-pose.pt ===
    curl -L -s -o "%SALIDA%\python-backend\yolov8n-pose.pt" https://github.com/ultralytics/assets/releases/download/v8.3.0/yolov8n-pose.pt
)

echo.
echo Listo: %SALIDA%
echo Para usarlo: entra a esa carpeta y corre instalar.bat (solo la primera vez).
if not defined CI pause
exit /b 0
