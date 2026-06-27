@echo off
pip install pyinstaller
pyinstaller --onefile --windowed --name rotor ^
    --collect-all sounddevice ^
    --hidden-import server ^
    --hidden-import client ^
    ui.py
echo.
echo Done. Executable: dist\rotor.exe
