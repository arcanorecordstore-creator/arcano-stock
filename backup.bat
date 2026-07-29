@echo off
REM Arcano Stock - Backup automatico de la base de datos
REM Copia arcano_stock.db a la carpeta backups/ con timestamp

cd /d "%~dp0"

if not exist "arcano_stock.db" (
    echo ERROR: No se encontro arcano_stock.db en esta carpeta.
    echo Asegurate de ejecutar este archivo desde la carpeta del proyecto.
    pause
    exit /b 1
)

if not exist "backups" mkdir backups

REM Generar timestamp YYYYMMDD_HHMMSS (formato seguro para Windows)
for /f "tokens=2 delims==" %%I in ('wmic os get localdatetime /value') do set datetime=%%I
set fecha=%datetime:~0,8%_%datetime:~8,6%

set backup_file=backups\arcano_stock_%fecha%.db

copy "arcano_stock.db" "%backup_file%" > nul

if errorlevel 1 (
    echo ERROR: Fallo la copia.
    pause
    exit /b 1
)

echo.
echo ============================================
echo   BACKUP REALIZADO
echo   Origen:  arcano_stock.db
echo   Destino: %backup_file%
echo ============================================
echo.
echo Listado de backups existentes:
dir /b backups\*.db 2>nul
echo.
pause
