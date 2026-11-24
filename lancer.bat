@echo off
cd /d %~dp0
if not exist .env (
  copy .env.exemple .env >nul
  echo Le fichier .env a ete cree : ouvrez-le, collez vos cles, puis relancez lancer.bat
  notepad .env
  exit /b
)
start "" http://127.0.0.1:5050
python serveur.py
