@echo off
REM ============================================================
REM  Roda tudo em sequencia: sobe o banco, instala dependencias
REM  e executa o script de extracao dos laudos.
REM  Basta dar 2 cliques neste arquivo.
REM ============================================================

cd /d "%~dp0"

echo ================================================
echo  1/3 - Subindo o banco de dados (Docker)...
echo ================================================
docker compose up -d
if errorlevel 1 (
    echo.
    echo [ERRO] Nao foi possivel subir o Docker. O Docker Desktop esta aberto?
    pause
    exit /b 1
)

echo.
echo ================================================
echo  2/3 - Instalando dependencias do Python...
echo ================================================
pip install -r requirements.txt
if errorlevel 1 (
    echo.
    echo [ERRO] Falha ao instalar dependencias do Python.
    pause
    exit /b 1
)

echo.
echo ================================================
echo  3/3 - Rodando o script de extracao...
echo ================================================
python banco_b_extractor.py

echo.
echo ================================================
echo  Concluido! Abra http://localhost:8080 no navegador
echo  para ver os dados na tabela "laudos" pelo Adminer.
echo ================================================
pause
