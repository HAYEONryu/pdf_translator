@echo off
call .venv\Scripts\activate
start http://127.0.0.1:8000
uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 1
