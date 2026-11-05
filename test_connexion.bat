@echo off
setlocal
set "PYTHON=C:/ISAE Eurobot/Projet recherche/PythonProject/.venv/Scripts/python.exe"
set "SCRIPT=%~dp0shimmers\shimmer_emg_lsl.py"
echo Shimmer1 Right COM44...
start "Shimmer EMG Left - COM3" cmd /k ""%PYTHON%" "%SCRIPT%" --port COM3 --rate 512.0 --name Shimmer_EMG_Left --channels 2"
 
 
echo.
echo READY
endlocal