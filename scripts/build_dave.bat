@echo off
rem Build external/deadly-dave with MSVC + Ninja (VS Build Tools 2022).
rem Usage: scripts\build_dave.bat   (run from the repo root)
setlocal
set "VSWHERE=%ProgramFiles(x86)%\Microsoft Visual Studio\Installer\vswhere.exe"
for /f "usebackq delims=" %%i in (`"%VSWHERE%" -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath`) do set "VSROOT=%%i"
if not defined VSROOT (
  echo error: no Visual Studio install with the C++ x64 toolset found 1>&2
  exit /b 2
)
call "%VSROOT%\VC\Auxiliary\Build\vcvars64.bat" >nul || exit /b 2
set "PATH=%VSROOT%\Common7\IDE\CommonExtensions\Microsoft\CMake\CMake\bin;%VSROOT%\Common7\IDE\CommonExtensions\Microsoft\CMake\Ninja;%PATH%"

set "SRC=%~dp0..\external\deadly-dave"
if not exist "%SRC%\CMakeLists.txt" (
  echo error: %SRC% missing; clone it first, see docs/feasibility.md 1>&2
  exit /b 2
)
cmake -S "%SRC%" -B "%SRC%\build" -G Ninja -DCMAKE_BUILD_TYPE=Release || exit /b 1
cmake --build "%SRC%\build" || exit /b 1
echo built: %SRC%\deadly-dave.exe and deadly-dave-bridge.exe
