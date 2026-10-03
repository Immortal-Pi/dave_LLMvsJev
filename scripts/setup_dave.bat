@echo off
rem Clone deadly-dave at the pinned commit, apply the bridge patch, and build.
rem Usage: scripts\setup_dave.bat   (run from the repo root; needs git + VS Build Tools 2022)
setlocal
set "COMMIT=950d39da2912a166e067062bc06461245cc897ab"
set "DEST=%~dp0..\external\deadly-dave"
set "PATCH=%~dp0..\bridge\deadly-dave-bridge.patch"

if not exist "%DEST%\.git" (
  git -c core.autocrlf=false clone https://github.com/skoperst/deadly-dave "%DEST%" || exit /b 1
)
pushd "%DEST%"
git -c core.autocrlf=false checkout -q %COMMIT% || (popd & exit /b 1)
git apply --reverse --check "%PATCH%" >nul 2>&1
if errorlevel 1 (
  git apply "%PATCH%" || (echo error: bridge patch does not apply cleanly 1>&2 & popd & exit /b 1)
  echo applied bridge patch
) else (
  echo bridge patch already applied
)
popd
call "%~dp0build_dave.bat"
