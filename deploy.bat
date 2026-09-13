@echo off
setlocal

rem ============================================================
rem  scan-ocr push (GitHub only, source distribution)
rem  Double-click after changes to push updates to GitHub.
rem
rem  First-time setup (already done for this folder):
rem    git init
rem    git branch -M main
rem    git remote add origin https://github.com/minnanosaiban/scan-ocr.git
rem
rem  jobs/ ・ __pycache__/ は .gitignore で除外済み
rem  (アップロード・出力の一時ファイルが入るため)。
rem ============================================================

echo === Push scan-ocr to GitHub ===
cd /d "%~dp0"
echo Current: %CD%

echo === Commit ^& Push to GitHub (main) ===
git add .
git commit -m "Update scan-ocr" || echo No changes to commit
git push -u origin main
if %errorlevel% neq 0 (
    echo [ERROR] Git push failed. Check remote/auth settings.
    pause
    exit /b 1
)

echo === Done ===
pause
