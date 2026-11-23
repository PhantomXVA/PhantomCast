@echo off
rem LanAudio sender: first run creates .venv and installs deps
cd /d "%~dp0"
if not exist .venv\Scripts\pythonw.exe (
  echo First run: installing dependencies...
  py -3 -m venv .venv || python -m venv .venv
  .venv\Scripts\python -m pip install --disable-pip-version-check -r requirements.txt || pause
)
start "" .venv\Scripts\pythonw.exe lanaudio_sender.py
