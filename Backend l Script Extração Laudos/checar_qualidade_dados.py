"""Roda TODAS as consultas de checar_qualidade_dados.sql de uma vez e
salva um relatório único em logs/qualidade_<data>.txt.

Existe porque conferir a qualidade dos dados no Adminer significa colar
~30 consultas uma por uma e printar cada resultado. Aqui é um comando só:

    python checar_qualidade_dados.py

As consultas NÃO ficam duplicadas aqui - são lidas do .sql, que continua
sendo a referência pra quem preferir rodar no Adminer. O .sql separa as
consultas por linhas "-- @@ <título>".

O relatório sai em logs/ (que é ignorado pelo git) porque traz endereço
de imóvel de cliente, igual aos PDFs.
"""

import os
import re
import sys
from datetime import datetime

import psycopg2

ARQUIVO_SQL = "checar_qualidade_dados.sql"
LOG_DIR = "logs"
LARGURA_MAXIMA_COLUNA = 42

def carregar_consultas(caminho):
    """Lê o .sql e devolve [(título, nota, sql)] - um por bloco "-- @@"."""
    with open(caminho, encoding="utf-8") as f:
        texto = f.read()

    consultas = []
    for bloco in re.split(r'^-- @@ ', texto, flags=re.MULTILINE)[1:]:
        linhas = bloco.split('\n')
        titulo = linhas[0].strip()

        # os "--" logo depois do título são a nota explicativa; o resto é SQL
        nota = []
        i = 1
        while i < len(linhas) and linhas[i].startswith('--'):
            nota.append(linhas[i].lstrip('-').strip())
            i += 1

        sql = "\n".join(linhas[i:]).strip()
        if sql:
            consultas.append((titulo, " ".join(nota), sql))
    return consultas

def formatar_tabela(colunas, linhas):
    """Resultado como tabela de texto alinhada, no estilo do psql."""
    if not linhas:
        return "  (nenhuma linha)"

    def celula(valor):
        if valor is None:
            return "NULL"
        texto = str(valor).replace('\n', ' ')
        return texto[:LARGURA_MAXIMA_COLUNA - 1] + "…" if len(texto) > LARGURA_MAXIMA_COLUNA else texto

    tabela = [list(colunas)] + [[celula(v) for v in linha] for linha in linhas]
    larguras = [max(len(linha[c]) for linha in tabela) for c in range(len(colunas))]

    saida = ["  " + " | ".join(t.ljust(l) for t, l in zip(tabela[0], larguras)).rstrip(),
             "  " + "-+-".join("-" * l for l in larguras)]
    for linha in tabela[1:]:
        saida.append("  " + " | ".join(t.ljust(l) for t, l in zip(linha, larguras)).rstrip())
    return "\n".join(saida)

def main():
    if not os.path.exists(ARQUIVO_SQL):
        print(f"[ERRO] {ARQUIVO_SQL} não encontrado - rode este script de dentro "
              f"da pasta 'Backend l Script Extração Laudos'.")
        return 1

    consultas = carregar_consultas(ARQUIVO_SQL)

    os.makedirs(LOG_DIR, exist_ok=True)
    caminho_relatorio = os.path.join(
        LOG_DIR, f"qualidade_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt")

    try:
        conn = psycopg2.connect(
            host=os.getenv("PGURL", "127.0.0.1"),
            dbname=os.getenv("PGNAME", "testdb"),
            user=os.getenv("PGUSR", "postgres"),
            password=os.getenv("PGPASS", "postgres"),
            port=os.getenv("PGPORT", "5432"),
        )
    except Exception as e:
        print(f"[ERRO] não conseguiu conectar no banco: {e}")
        return 1

    partes = [
        f"RELATÓRIO DE QUALIDADE DOS DADOS - {datetime.now():%d/%m/%Y %H:%M}",
        f"{len(consultas)} consultas de {ARQUIVO_SQL}",
    ]

    with conn:
        for numero, (titulo, nota, sql) in enumerate(consultas, start=1):
            print(f"  [{numero}/{len(consultas)}] {titulo}", flush=True)
            partes.append("")
            partes.append("=" * 78)
            partes.append(f"{numero}. {titulo}")
            if nota:
                partes.append(f"   ({nota})")
            partes.append("=" * 78)
            try:
                with conn.cursor() as cursor:
                    cursor.execute(sql)
                    colunas = [d[0] for d in cursor.description]
                    partes.append(formatar_tabela(colunas, cursor.fetchall()))
            except Exception as e:
                # uma consulta que falha (coluna que ainda não existe, por
                # exemplo) não pode derrubar o relatório inteiro
                conn.rollback()
                partes.append(f"  [ERRO NESTA CONSULTA] {str(e).splitlines()[0]}")

    conn.close()

    relatorio = "\n".join(partes) + "\n"
    with open(caminho_relatorio, "w", encoding="utf-8") as f:
        f.write(relatorio)

    print()
    print(relatorio)
    print(f"Relatório salvo em: {os.path.abspath(caminho_relatorio)}")
    return 0

if __name__ == "__main__":
    sys.exit(main())
