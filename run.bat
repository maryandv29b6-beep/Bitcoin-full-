@echo off
rem FlowLens for Windows: run.bat  (live)   or   run.bat --demo   (synthetic data)
cd /d "%~dp0backend"
if not exist .venv (
  py -3 -m venv .venv || python -m venv .venv
)
call .venv\Scripts\activate.bat
pip install -q -r requirements.txt
if exist ..\config\flowlens.config.json set FLOWLENS_CONFIG=..\config\flowlens.config.json
python -m flowlens serve %*
