@echo off
rem Локальный сервер модели для local_mimo (A9-02, LDEV-02): llama.cpp b12199 CUDA 13.5, только 127.0.0.1.
rem Рантайм и веса лежат в models\ (вне git). Архивы — официальные ggml-org/llama.cpp b12199, SHA-256 сверены:
rem   llama-b12199-bin-win-cuda-13.5-x64.zip  1cb9b561b0d62b3ecc48ecbff2003fa656bdbbf71bead919ae92110df7f961a5
rem   cudart-llama-bin-win-cuda-13.5-x64.zip  c2bf4cc202bfbaaa7a0b75f5082457c3811326947bb216059b72839d844e9cba
rem Порт и контекст должны совпадать с endpoint и context_limit зарегистрированной конфигурации.
setlocal
set ROOT=%~dp0..
set RT=%ROOT%\models\runtime\llama-b12199\llama-server.exe
set GGUF=%ROOT%\models\MiMo-V2.6-Distill-Qwen-9B-Q4_K_M.gguf
if not exist "%RT%" (echo Нет рантайма: %RT% & exit /b 2)
if not exist "%GGUF%" (echo Нет весов: %GGUF% & exit /b 2)
"%RT%" -m "%GGUF%" --host 127.0.0.1 --port 8089 -c 8192 -ngl 99 --jinja
