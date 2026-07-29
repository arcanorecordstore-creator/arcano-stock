@echo off
REM Arcano Stock - Script de arranque
cd /d "%~dp0"

if not exist "venv\Scripts\activate.bat" (
    echo ERROR: No se encontro el entorno virtual.
    echo Ejecutar primero:
    echo   python -m venv venv
    echo   .\venv\Scripts\Activate.ps1
    echo   pip install -r requirements.txt
    pause
    exit /b 1
)

call venv\Scripts\activate.bat

REM Obtener IP local en la red (para acceder desde celular)
set "LOCAL_IP="
for /f "tokens=2 delims=:" %%a in ('ipconfig ^| findstr /R "IPv4"') do (
    if not defined LOCAL_IP set "LOCAL_IP=%%a"
)
set "LOCAL_IP=%LOCAL_IP: =%"

echo.
echo ============================================
echo   ARCANO STOCK - Iniciando servidor...
echo.
echo   En esta PC:     http://127.0.0.1:5001
if defined LOCAL_IP (
echo   En tu celular:  http://%LOCAL_IP%:5001
echo                   (mismo wifi, abrir desde el navegador)
)
echo.
echo   Para apagar: Ctrl+C
echo ============================================
echo.
python app.py
pause
