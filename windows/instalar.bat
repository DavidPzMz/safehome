@echo off
rem Instala lo que necesita SafeHome (solo se corre la primera vez)
setlocal
cd /d "%~dp0"
set CARPETA=%cd%

rem busca Python 3 (de preferencia 3.12)
set PY=
py -3.12 --version >nul 2>nul && set PY=py -3.12
if not defined PY py -3 --version >nul 2>nul && set PY=py -3
if not defined PY python --version >nul 2>nul && set PY=python
if not defined PY (
    echo Falta Python 3. Instalalo desde https://www.python.org/downloads/
    echo ^(version 3.12 y marca "Add python.exe to PATH"^) y vuelve a correr este archivo.
    pause
    exit /b 1
)

echo === Creando el entorno de Python ===
%PY% -m venv python-backend\.venv
if errorlevel 1 (
    echo No se pudo crear el entorno de Python.
    pause
    exit /b 1
)
set PIP=python-backend\.venv\Scripts\python.exe -m pip

echo === Instalando librerias (tarda unos minutos, necesita internet) ===
%PIP% install --upgrade pip -q
rem torch version solo procesador (mucho mas ligera que la de tarjeta de video)
%PIP% install torch torchvision --index-url https://download.pytorch.org/whl/cpu -q
if errorlevel 1 goto error
%PIP% install -r python-backend\requirements.txt -q
if errorlevel 1 goto error

echo === Creando accesos directos en el escritorio ===
powershell -NoProfile -Command ^
  "$e = [Environment]::GetFolderPath('Desktop'); $w = New-Object -ComObject WScript.Shell;" ^
  "$a = $w.CreateShortcut(\"$e\SafeHome.lnk\"); $a.TargetPath = '%CARPETA%\SafeHome.exe'; $a.WorkingDirectory = '%CARPETA%'; $a.IconLocation = '%CARPETA%\icono.ico'; $a.Save();" ^
  "$p = $w.CreateShortcut(\"$e\SafeHome Pruebas.lnk\"); $p.TargetPath = '%CARPETA%\SafeHome.exe'; $p.Arguments = '--pruebas'; $p.WorkingDirectory = '%CARPETA%'; $p.IconLocation = '%CARPETA%\icono.ico'; $p.Save()"

echo.
echo Listo. Abre SafeHome desde el acceso directo del escritorio.
pause
exit /b 0

:error
echo Error instalando las librerias. Revisa tu conexion e intentalo otra vez.
pause
exit /b 1
