@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ============================================
echo   Corriendo la suite de QA de Arcano...
echo ============================================
echo.
venv\Scripts\python.exe tests\qa_suite.py
echo.
echo Listo. Mira los resultados de arriba.
echo Podes cerrar esta ventana cuando quieras.
pause
