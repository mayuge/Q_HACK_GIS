@echo off
setlocal
cd /d "%~dp0"
title QHackGIS EXE Builder

 echo ========================================
 echo QHackGIS EXE Builder
 echo ========================================
 echo Working folder: %CD%
 echo.

if not exist "qhackgis.py" (
    echo ERROR: qhackgis.py was not found in this folder.
    echo Put build_exe.bat and qhackgis.py in the same folder.
    goto :error
)

set "PYTHON_CMD="
where py >nul 2>nul
if not errorlevel 1 set "PYTHON_CMD=py"
if not defined PYTHON_CMD (
    where python >nul 2>nul
    if not errorlevel 1 set "PYTHON_CMD=python"
)
if not defined PYTHON_CMD (
    echo ERROR: Python was not found. Install Python and try again.
    goto :error
)

echo Using Python command: %PYTHON_CMD%
%PYTHON_CMD% --version
if errorlevel 1 goto :error

echo.
echo [1/3] Installing PyInstaller...
%PYTHON_CMD% -m pip install --upgrade pyinstaller
if errorlevel 1 goto :error

echo.
echo [2/3] Building QHackGIS.exe...
%PYTHON_CMD% -m PyInstaller --clean --noconfirm --onefile --console --name QHackGIS --add-data "qhackgis.py;." qhackgis.py
if errorlevel 1 goto :error

echo.
echo [3/3] Checking output...
if not exist "dist\QHackGIS.exe" goto :error

echo.
echo BUILD COMPLETED:
echo %CD%\dist\QHackGIS.exe
echo Copy only QHackGIS.exe to distribute the app.
echo QGIS must still be installed on the target PC.
echo.
pause
exit /b 0

:error
echo.
echo BUILD FAILED. Read the error above.
echo.
pause
exit /b 1
