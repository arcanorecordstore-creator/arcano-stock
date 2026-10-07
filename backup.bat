@echo off
REM Arcano Stock - Backup de la base de datos LOCAL (la de prueba de esta PC)
REM Copia instance\arcano_stock.db a la carpeta backups\ con fecha y hora.
REM OJO: la base REAL de la tienda vive en Render. Para respaldarla, entra al
REM admin -> Ajustes -> Backup (descarga la base y las portadas).

cd /d "%~dp0"

set DB=instance\arcano_stock.db

if not exist "%DB%" (
    echo ERROR: No se encontro %DB%.
    echo Asegurate de ejecutar este archivo desde la carpeta del proyecto.
    pause
    exit /b 1
)

if not exist "backups" mkdir backups

REM Fecha y hora YYYYMMDD_HHMMSS (PowerShell: wmic ya no viene en Windows 11)
for /f %%I in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd_HHmmss"') do set fecha=%%I

set backup_file=backups\arcano_stock_%fecha%.db

copy "%DB%" "%backup_file%" > nul

if errorlevel 1 (
    echo ERROR: Fallo la copia.
    pause
    exit /b 1
)

echo.
echo ============================================
echo   BACKUP REALIZADO (base local de prueba)
echo   Origen:  %DB%
echo   Destino: %backup_file%
echo.
echo   La base REAL se respalda desde el admin:
echo   Ajustes -^> Backup
echo ============================================
echo.
echo Listado de backups existentes:
dir /b backups\*.db 2>nul
echo.
pause
