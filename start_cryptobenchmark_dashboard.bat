@echo off
title CryptoBenchmark A.I. Dashboard - Port 5021
cd /d C:\Users\abc\Desktop\CryptoBenchmarkAI
start /min "CryptoBenchmark A.I. Dashboard" cmd /c C:\Users\abc\AppData\Local\Programs\Python\Python313\python.exe dashboard_cryptobenchmark.py
timeout /t 5 /nobreak >nul
start http://localhost:5021
