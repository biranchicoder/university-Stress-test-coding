@echo off
rem Initialize Git repository if not already initialized
if not exist .git (git init)

rem Ensure any stray lock file is removed
if exist .git\index.lock del /f .git\index.lock

rem Stage all files
git add .

rem Commit with a descriptive message
git commit -m "Finalize 75‑proposal JSON, VS Code suite, CI workflow, README updates"

rem Create a zip of the entire project (excluding .git folder)
rem Adjust the output path as needed
powershell -Command "Compress-Archive -Path * -Exclude .git -DestinationPath %USERPROFILE%\\Desktop\\UniversityCoding_Final.zip"

echo All steps completed. You may now add a remote and push if desired.
pause