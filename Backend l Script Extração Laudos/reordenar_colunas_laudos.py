"""
Muda a posição de uma coluna da tabela `laudos` (só a ordem em que ela
aparece no Adminer; nenhum script depende da ordem - todos gravam pelo
nome da coluna).

Uso:
    python reordenar_colunas_laudos.py <coluna> <depois_desta>
    ex.: python reordenar_colunas_laudos.py origem_coordenada coordenadas

O Postgres não tem "ALTER TABLE ... MOVE COLUMN": a única forma é recriar a
tabela. Este script lê as colunas ATUAIS do banco (tipos, padrões,
obrigatórias) e recria a tabela com a mesma lista, só trocando a posição
pedida - diferente do antigo reordenar_area_terreno.sql, que tinha a
lista de colunas fixa e hoje apagaria as colunas criadas depois dele.

Segurança:
- tudo numa transação só, com a tabela travada (quem tentar gravar
  espera); se qualquer passo falhar, o Postgres desfaz tudo;
- confere antes de confirmar: mesmo número de linhas e o mesmo conteúdo
  linha a linha (hash de todas as colunas, por id) - se não bater, desfaz;
- recria a chave (id), o índice único de codigo_laudo e mantém o
  contador do id;
- faça um backup antes (o comando está no README, seção desta ferramenta).
"""

import os
import sys

import psycopg2
from psycopg2 import sql


def conectar():
    return psycopg2.connect(
        host=os.getenv("PGURL", "127.0.0.1"), dbname=os.getenv("PGNAME", "testdb"),
        user=os.getenv("PGUSR", "postgres"), password=os.getenv("PGPASS", "postgres"),
        port=os.getenv("PGPORT", "5432"),
    )


def colunas_atuais(cur):
    """[(nome, tipo, not_null, padrao)] na ordem atual."""
    cur.execute("""
        SELECT a.attname, format_type(a.atttypid, a.atttypmod), a.attnotnull,
               pg_get_expr(d.adbin, d.adrelid)
        FROM pg_attribute a
        LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum
        WHERE a.attrelid = 'laudos'::regclass AND a.attnum > 0 AND NOT a.attisdropped
        ORDER BY a.attnum
    """)
    return cur.fetchall()


def assinatura_conteudo(cur, nomes):
    """Hash de todas as linhas (colunas em ordem alfabética, por id) - não
    depende da posição das colunas, então tem que ser igual antes e depois."""
    campos = sql.SQL(", ").join(sql.Identifier(n) for n in sorted(nomes))
    cur.execute(sql.SQL("SELECT count(*), md5(string_agg(md5(row({})::text), '' ORDER BY id)) FROM laudos")
                .format(campos))
    return cur.fetchone()


def main():
    if len(sys.argv) != 3:
        print(__doc__)
        sys.exit(1)
    coluna, depois_de = sys.argv[1], sys.argv[2]

    conn = conectar()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("LOCK TABLE laudos IN ACCESS EXCLUSIVE MODE;")
                cols = colunas_atuais(cur)
                nomes = [c[0] for c in cols]
                if coluna not in nomes or depois_de not in nomes:
                    raise SystemExit(f"Coluna não encontrada: {coluna if coluna not in nomes else depois_de}")
                if nomes.index(coluna) == nomes.index(depois_de) + 1:
                    print(f"{coluna} já está logo depois de {depois_de} - nada a fazer.")
                    return
                cur.execute("SELECT indexname, indexdef FROM pg_indexes WHERE tablename = 'laudos' "
                            "AND indexname <> 'laudos_pkey'")
                indices = cur.fetchall()
                antes = assinatura_conteudo(cur, nomes)

                nova_ordem = [c for c in cols if c[0] != coluna]
                pos = [c[0] for c in nova_ordem].index(depois_de) + 1
                nova_ordem.insert(pos, next(c for c in cols if c[0] == coluna))

                definicoes = []
                for nome, tipo, not_null, padrao in nova_ordem:
                    d = f"{sql.Identifier(nome).as_string(cur)} {tipo}"
                    if padrao:
                        d += f" DEFAULT {padrao}"
                    if not_null:
                        d += " NOT NULL"
                    definicoes.append(d)
                lista = sql.SQL(", ").join(sql.Identifier(c[0]) for c in nova_ordem)

                cur.execute(f"CREATE TABLE laudos_reordenada ({', '.join(definicoes)});")
                cur.execute(sql.SQL("INSERT INTO laudos_reordenada ({0}) SELECT {0} FROM laudos").format(lista))
                # o contador do id passa pra tabela nova antes de apagar a antiga
                cur.execute("SELECT pg_get_serial_sequence('laudos', 'id')")
                sequencia = cur.fetchone()[0]
                if sequencia:
                    cur.execute(f"ALTER SEQUENCE {sequencia} OWNED BY laudos_reordenada.id;")
                cur.execute("DROP TABLE laudos;")
                cur.execute("ALTER TABLE laudos_reordenada RENAME TO laudos;")
                cur.execute("ALTER TABLE laudos ADD CONSTRAINT laudos_pkey PRIMARY KEY (id);")
                for _nome, definicao in indices:
                    cur.execute(definicao + ";")

                depois = assinatura_conteudo(cur, nomes)
                if antes != depois:
                    raise RuntimeError(f"conteúdo diferente depois de recriar ({antes} x {depois}) - desfazendo")
                cur.execute("ANALYZE laudos;")
                print(f"OK: {coluna} agora vem logo depois de {depois_de}. "
                      f"{depois[0]} linhas, conteúdo idêntico (hash {depois[1][:12]}...).")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
