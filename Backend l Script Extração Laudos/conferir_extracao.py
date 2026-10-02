"""Mede a taxa de acerto da extração comparando o banco com uma SEGUNDA
fonte dentro do próprio laudo.

O laudo repete os números das amostras nas tabelas de cálculo, e os do
imóvel avaliado na "AVALIAÇÃO FINAL", na linha "Avaliando", no formulário
da página 3 (digital) e em campos duplicados da capa/questionário
(físico). O banco_b_extractor.py não lê nenhuma dessas partes, então
elas servem de testemunha.

Não têm segunda fonte no PDF, então ficam de fora: endereço, tipo,
quartos e banheiros das amostras; e no físico, endereço, proposta,
quartos, banheiros e vagas do laudo. Esses são vigiados só pelo
checar_qualidade_dados.py (vazio, zerado, fora da realidade). Este script lê as testemunhas com regras próprias (não
importa nada do extrator - senão estaria conferindo o código contra ele
mesmo) e compara campo a campo com o que está gravado.

Quando o banco não bate com a testemunha, o script ainda separa de quem é
a culpa, olhando a linha do laudo de onde a extração leu o campo:

    bate                  banco = testemunha
    laudo inconsistente   banco != testemunha, mas o valor do banco está
                          impresso exatamente no campo de origem - a
                          extração leu certo, o laudo é que se contradiz
    erro de extração      banco != testemunha e o valor do banco não está
                          impresso no campo de origem

Uso (dentro da pasta "Backend l Script Extração Laudos"):

    python conferir_extracao.py

Por padrão sorteia 500 laudos (alguns minutos). Pra conferir outra
quantidade: $env:CONFERIR_QTD="2000"  (ou "todos"). Pra repetir o mesmo
sorteio: $env:CONFERIR_SEMENTE="<número impresso no relatório>".

O relatório sai em logs/conferencia_<data>.txt (ignorado pelo git, traz
endereço e valores de cliente).
"""

import multiprocessing
import os
import random
import re
import sys
import unicodedata
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime
from decimal import Decimal, InvalidOperation

import pdfplumber
import psycopg2

PASTA_LAUDOS = os.path.join("data", "laudos")
LOG_DIR = "logs"
QTD_PADRAO = 500
# o laudo arredonda em lugares diferentes (tabela com 2 casas, capa com 0)
TOLERANCIA = Decimal("0.005")
MAX_EXEMPLOS = 40

BATE, INCONSISTENTE, ERRO = "bate", "laudo inconsistente", "erro de extração"


# ---------------------------------------------------------------------------
# Leitura das testemunhas
# ---------------------------------------------------------------------------

def _num(texto):
    """Número de testemunha. Vírgula presente -> pt-BR (ponto é milhar);
    sem vírgula -> ponto é decimal (formato antigo do Plataforma)."""
    texto = (texto or "").replace("R$", "").replace("m²", "").strip()
    if "," in texto:
        texto = texto.replace(".", "").replace(",", ".")
    try:
        return Decimal(texto)
    except InvalidOperation:
        return None

def _secao(texto, inicio, fins):
    """Trecho entre o título `inicio` e o primeiro dos padrões `fins`."""
    m = re.search(inicio, texto)
    if not m:
        return ""
    resto = texto[m.end():]
    corte = len(resto)
    for fim in fins:
        mf = re.search(fim, resto)
        if mf:
            corte = min(corte, mf.start())
    return resto[:corte]

def ler_testemunhas(texto):
    """Devolve {"laudo": {campo: valor}, "amostras": {n: {campo: valor}}}
    só com o que achou - laudo sem uma das tabelas simplesmente não entra
    na conferência daquele campo."""
    laudo, amostras = {}, {}

    def amostra(n):
        return amostras.setdefault(n, {})

    # --- laudo: AVALIAÇÃO FINAL (valor na MESMA linha, em caixa normal;
    # a capa usa caixa alta com o valor na linha de baixo)
    m = re.search(r'Valor de avalia[çc][ãa]o para efeito de garantia\s+R\$\s*([\d\.,]+)', texto)
    if m:
        laudo["valor_mercado"] = _num(m.group(1))
    m = re.search(r'Valor de venda for[çc]ada final\s+R\$\s*([\d\.,]+)', texto)
    if m:
        laudo["valor_venda_forcada"] = _num(m.group(1))

    # --- físico, comparativo direto: CÁLCULOS AVALIATÓRIOS
    #   1 250,00 R$ 4.800,00 0,90 ...
    sec = _secao(texto, r'C[ÁA]LCULOS AVALIAT[ÓO]RIOS\n', [r'\nF1 \(', r'\nHOMOGENEIZA'])
    for linha in sec.split("\n"):
        m = re.match(r'^(\d{1,2})\s+([\d\.,]+)\s+R\$\s*([\d\.,]+)\s', linha)
        if m:
            amostra(int(m.group(1))).update(
                area_construida=_num(m.group(2)), valor_unitario_m2=_num(m.group(3)))

    # --- físico, método evolutivo: depreciação (idade e estado)
    #   Avaliando 4 Regular 60 6,67 0,056 20 0,045
    #   3 1 Nova(até 5 anos) 60 1,67 20
    #   5 10                      <- estado em branco no laudo
    sec = _secao(texto, r'C[ÁA]LCULO DO COEFICIENTE DE DEPRECIA[ÇC][ÃA]O\n',
                 [r'C[ÁA]LCULO DE VALOR DA [ÁA]REA CONSTRU'])
    for linha in sec.split("\n"):
        m = re.match(r'^(Avaliando|\d{1,2})\s+(\d+)(?:\s+(.+?)\s+\d+\s+[\d,]+)?(?:\s|$)', linha)
        if not m:
            continue
        valores = {"idade_anos": int(m.group(2)), "estado_conservacao": (m.group(3) or "").strip()}
        (laudo if m.group(1) == "Avaliando" else amostra(int(m.group(1)))).update(valores)

    # --- físico, método evolutivo: área construída
    #   Avaliando 216,04 R$ 2.039,61 ...
    #   5 270,00
    sec = _secao(texto, r'C[ÁA]LCULO DE VALOR DA [ÁA]REA CONSTRU[ÍI]DA\n',
                 [r'C[ÁA]LCULO DE VALOR DO TERRENO'])
    for linha in sec.split("\n"):
        m = re.match(r'^(Avaliando|\d{1,2})\s+([\d\.,]+)(?:\s|$)', linha)
        if not m:
            continue
        if m.group(1) == "Avaliando":
            laudo["area_privativa_m2"] = _num(m.group(2))
        else:
            amostra(int(m.group(1)))["area_construida"] = _num(m.group(2))

    # --- físico, método evolutivo: terreno
    #   1 150,00 R$ 31.234,73 R$ 208,23 ...
    sec = _secao(texto, r'C[ÁA]LCULO DE VALOR DO TERRENO\n', [r'\nF1 \(', r'\nHOMOGENEIZA'])
    for linha in sec.split("\n"):
        m = re.match(r'^(\d{1,2})\s+([\d\.,]+)\s+R\$', linha)
        if m:
            amostra(int(m.group(1)))["area_terreno_m2"] = _num(m.group(2))

    # --- digital: linha do imóvel avaliado no quadro comparativo
    #   BAIRRO:VILAPREL Médio 54,40 Sim Bom 38
    m = re.search(r'BAIRRO:\S*\s+(\S+)\s+([\d\.,]+)\s+(?:Sim|N[ãa]o)\s+\S+\s+(\d+)', texto)
    if m:
        laudo.update(padrao_acabamento=m.group(1), area_privativa_m2=_num(m.group(2)),
                     idade_anos=int(m.group(3)))

    # --- digital: DADOS COMPARATIVOS, uma linha por amostra, na ordem
    #   11900000000 Vila Exemplo 1 280.000,00 54 38 100 0,95 ...
    #   (vagas, valor, área, idade)
    sec = _secao(texto, r'DADOS COMPARATIVOS\n', [r'\nM[ÉE]DIA\b'])
    n = 0
    for linha in sec.split("\n"):
        m = re.search(r'\s(\d+)\s+([\d\.]+,\d{2})\s+([\d\.,]+)\s+(\d+)\s+(\d+)\s+[\d\.,]+\s', " " + linha + " ")
        if m:
            n += 1
            amostra(n).update(vagas=int(m.group(1)), valor=_num(m.group(2)),
                              area_construida=_num(m.group(3)), idade_anos=int(m.group(4)))

    # --- digital: a página 3 ("Laudo de Avaliação" / FACHADA CROQUI) repete
    # quase toda a capa num formulário "Rótulo: valor"
    #   Endereço: Rua Exemplo No.: 309 Complemento: APTO 402
    #   Bairro: São José UF: RS Área Comum: 21,45 m² Fração Ideal (%): 0
    #   Municipio: Caxias do Sul Área Privativa: 70,01 m² Vaga: 1
    #   Dormitório: 2 Suíte: Banheiro Social: 2 Área Total: 91.46 m²
    #   Tipo do Imóvel: Apartamento Tipo No. da Matrícula: 150463 No. do Cart.:
    m = re.search(r'Laudo de Avalia[çc][ãa]o\n(\d{6,})\n', texto)
    if m:
        laudo["numero_proposta"] = m.group(1)
    m = re.search(r'^Endere[çc]o:\s*(.*?)\s+No\.:\s*(\S*)\s+Complemento:\s*(.*)$', texto, re.MULTILINE)
    if m:
        laudo.update(endereco=m.group(1), numero=m.group(2), complemento=m.group(3))
    m = re.search(r'^Bairro:\s*(.*?)\s+UF:\s*(\S+)\s+[ÁA]rea Comum:\s*([\d\.,]+)', texto, re.MULTILINE)
    if m:
        laudo.update(bairro=m.group(1), uf=m.group(2), area_comum_m2=_num(m.group(3)))
    m = re.search(r'^Municipio:\s*(.*?)\s+[ÁA]rea Privativa:.*?Vaga:\s*(\d+)', texto, re.MULTILINE)
    if m:
        laudo.update(municipio=m.group(1), vagas=int(m.group(2)))
    m = re.search(r'^Dormit[óo]rio:\s*(\d+).*?Banheiro Social:\s*(\d+)', texto, re.MULTILINE)
    if m:
        laudo.update(quartos=int(m.group(1)), banheiros=int(m.group(2)))
    m = re.search(r'^Tipo do Im[óo]vel:\s*(.+?)\s+(?:Tipo\s+)?No\.\s*da\s*Matr[íi]cula:\s*(\d*)',
                  texto, re.MULTILINE)
    if m:
        laudo.update(tipo_imovel=m.group(1), matricula=m.group(2))

    # --- físico: o tipo aparece na capa e de novo no questionário (a
    # extração usa o do questionário)
    #   Tipo do imóvel Matrícula Núm. Registro de Imóveis [IPTU]
    #   Loja Comercial/Agencia 50960 09.338-5
    if re.search(r'01\s*-\s*Tipo\s+do\s+Im[óo]vel\s+Avaliado', texto):
        m = re.search(r'Tipo do im[óo]vel Matr[íi]cula[^\n]*\n(.+?)\s+\d', texto)
        if m:
            laudo["tipo_imovel"] = m.group(1)

    # --- físico: área total = averbada + não averbada (campos 21 e 22; a
    # extração lê o 20)
    m = re.search(r'21 - [ÁA]rea Averbada[^\n]*\n\s*([\d\.,]+)\s+([\d\.,]+)', texto)
    if m and _num(m.group(1)) is not None and _num(m.group(2)) is not None:
        laudo["area_total_m2"] = _num(m.group(1)) + _num(m.group(2))

    # --- área na capa (RESUMO) - só quando não veio da linha "Avaliando"
    # ou do quadro comparativo, que são mais confiáveis
    m = re.search(r'\n[ÁA]REA (?:PRIVATIVA|CONSTRU[ÍI]DA)\n([\d\.,]+)\s*m²', texto)
    if m:
        laudo.setdefault("area_privativa_m2", _num(m.group(1)))

    return {"laudo": laudo, "amostras": amostras}


# ---------------------------------------------------------------------------
# Linha de origem - de onde a EXTRAÇÃO leu cada campo
# ---------------------------------------------------------------------------

def _linha_apos(bloco, rotulo):
    linhas = bloco.split("\n")
    for i, linha in enumerate(linhas):
        if re.search(rotulo, linha, re.IGNORECASE) and i + 1 < len(linhas):
            return linhas[i + 1]
    return ""

def _linha_com(bloco, rotulo):
    for linha in bloco.split("\n"):
        if re.search(rotulo, linha, re.IGNORECASE):
            return linha
    return ""

def blocos_de_amostra(texto):
    """{n: texto do bloco} - físico ("AMOSTRA 1") ou digital ("Amostra n.0",
    renumerado a partir de 1 como o extrator faz)."""
    blocos = {}
    m_ini = re.search(r'\nAMOSTRAS\n', texto)
    if m_ini:
        trecho = texto[m_ini.end():]
        m_fim = re.search(r'\nAVALIA[ÇC][ÃA]O DO IM[ÓO]VEL\n', trecho)
        if m_fim:
            trecho = trecho[:m_fim.start()]
        for bloco in re.split(r'\n(?=AMOSTRA\s+\d+\b)', "\n" + trecho):
            m = re.match(r'AMOSTRA\s+(\d+)\b', bloco)
            if m:
                blocos[int(m.group(1))] = bloco
        return blocos

    achados = {}
    for bloco in re.split(r'\n(?=Amostra\s+n\.\s*\d+\b)', texto):
        m = re.match(r'Amostra\s+n\.\s*(\d+)\b', bloco)
        if m:
            achados[int(m.group(1))] = bloco
    deslocamento = 1 if achados and min(achados) == 0 else 0
    return {n + deslocamento: b for n, b in achados.items()}

def linha_origem_amostra(bloco, campo, digital):
    if digital:
        rotulo = {"valor": r'Valor total', "area_privativa_m2": r'A\.\s*privativa',
                  "idade_anos": r'Idade Aparente', "vagas": r'N\.\s*vagas'}.get(campo)
        return _linha_com(bloco, rotulo) if rotulo else ""
    rotulo = {"area_privativa_m2": r'^[ÁA]rea privativa', "valor_unitario_m2": r'^[ÁA]rea privativa',
              "area_terreno_m2": r'^[ÁA]rea do terreno',
              "idade_anos": r'^Idade aparente', "estado_conservacao": r'^Idade aparente'}.get(campo)
    return _linha_apos(bloco, rotulo) if rotulo else ""

def linha_origem_laudo(texto, campo, digital):
    capa = texto[:3000]
    grade_capa = {
        "numero_proposta": r'^Solicitante N',
        "endereco": r'^Endere[çc]o N[úu]mero', "numero": r'^Endere[çc]o N[úu]mero',
        "complemento": r'^Endere[çc]o N[úu]mero',
        "bairro": r'^Bairro Munic', "municipio": r'^Bairro Munic', "uf": r'^Bairro Munic',
        "matricula": r'^Tipo do im[óo]vel Matr',
        "area_comum_m2": r'^Estado de Conserva[çc][ãa]o Condom',
        "vagas": r'^Estado de Conserva[çc][ãa]o Im[óo]vel Quantidade',
        "quartos": r'^Estado de Conserva[çc][ãa]o Im[óo]vel Quantidade',
        "banheiros": r'^Estado de Conserva[çc][ãa]o Im[óo]vel Quantidade',
    }
    if campo in grade_capa:
        return _linha_apos(capa, grade_capa[campo])
    if campo == "tipo_imovel":
        return (_linha_apos(capa, r'^Tipo do im[óo]vel Matr') if digital
                else _linha_apos(texto, r'01 - Tipo do Im[óo]vel Avaliado'))
    if campo == "area_total_m2":
        return _linha_apos(texto, r'19 - [ÁA]rea Comum')
    if campo == "valor_mercado":
        return _linha_apos(capa, r'^VALOR DE (AVALIA[ÇC][ÃA]O PARA EFEITO DE GARANTIA|MERCADO)$')
    if campo == "valor_venda_forcada":
        return _linha_apos(capa, r'^VALOR DE VENDA FOR[ÇC]ADA$')
    if digital:
        return _linha_apos(capa, r'^Tipo de Implanta[çc][ãa]o')
    rotulo = {"area_privativa_m2": r'18 - [ÁA]rea Privativa', "idade_anos": r'04 - Idade Aparente',
              "estado_conservacao": r'06 - Estado de Conserva'}.get(campo)
    return _linha_apos(texto, rotulo) if rotulo else ""


# ---------------------------------------------------------------------------
# Comparação
# ---------------------------------------------------------------------------

def _norm(texto):
    """Minúsculo, sem acento e sem espaço repetido - a capa do digital vem
    "RUA EXEMPLO" e a página 3 "Rua Exemplo"."""
    texto = unicodedata.normalize("NFKD", str(texto or ""))
    texto = "".join(c for c in texto if not unicodedata.combining(c))
    return re.sub(r'\s+', ' ', texto).strip().lower()

def _iguais(banco, testemunha):
    if isinstance(testemunha, Decimal):
        banco = Decimal(str(banco or 0))
        return abs(banco - testemunha) <= max(Decimal("0.01"), abs(testemunha) * TOLERANCIA)
    if isinstance(testemunha, int):
        return int(banco or 0) == testemunha
    return _norm(banco) == _norm(testemunha)

def _formas_impressas(valor):
    """Jeitos como um número do banco pode estar impresso no laudo."""
    v = Decimal(str(valor))
    duas = f"{v:.2f}"
    inteiro, dec = duas.split(".")
    milhar = f"{int(inteiro):,}".replace(",", ".")
    formas = {f"{milhar},{dec}", f"{inteiro},{dec}", duas, f"{v:.3f}"}
    if v == v.to_integral_value():
        formas.add(str(int(v)))
    return formas

def _esta_impresso(valor, linha):
    """O valor que foi pro banco aparece literalmente na linha de origem?
    Zero/vazio nunca conta como impresso: zero no banco onde a testemunha
    tem número é leitura que falhou."""
    if not linha or valor in (None, "", 0) or valor == Decimal("0"):
        return False
    if isinstance(valor, str):
        return _norm(valor) in _norm(linha)
    if isinstance(valor, int):
        return re.search(rf'(?<![\d.,/]){valor}(?![\d.,/])', linha) is not None
    return any(re.search(rf'(?<![\d.,]){re.escape(f)}(?![\d])', linha) for f in _formas_impressas(valor))

def classificar(banco, testemunha, linha_origem):
    if _iguais(banco, testemunha):
        return BATE
    return INCONSISTENTE if _esta_impresso(banco, linha_origem) else ERRO


# ---------------------------------------------------------------------------
# Execução
# ---------------------------------------------------------------------------

def _ler_pdf(caminho):
    try:
        with pdfplumber.open(caminho) as pdf:
            return os.path.basename(caminho), "".join((p.extract_text() or "") + "\n" for p in pdf.pages)
    except Exception:
        return os.path.basename(caminho), ""

def _tabela(cabecalho, linhas, largura_max=48):
    def celula(v):
        t = str(v).replace("\n", " ")
        return t[:largura_max - 1] + "…" if len(t) > largura_max else t
    tabela = [cabecalho] + [[celula(v) for v in l] for l in linhas]
    larg = [max(len(l[c]) for l in tabela) for c in range(len(cabecalho))]
    saida = ["  " + " | ".join(t.ljust(w) for t, w in zip(tabela[0], larg)).rstrip(),
             "  " + "-+-".join("-" * w for w in larg)]
    saida += ["  " + " | ".join(t.ljust(w) for t, w in zip(l, larg)).rstrip() for l in tabela[1:]]
    return "\n".join(saida)

def main():
    if not os.path.isdir(PASTA_LAUDOS):
        print(f"[ERRO] pasta {PASTA_LAUDOS} não encontrada - rode de dentro de "
              f"'Backend l Script Extração Laudos'.")
        return 1

    try:
        conn = psycopg2.connect(
            host=os.getenv("PGURL", "127.0.0.1"), dbname=os.getenv("PGNAME", "testdb"),
            user=os.getenv("PGUSR", "postgres"), password=os.getenv("PGPASS", "postgres"),
            port=os.getenv("PGPORT", "5432"))
    except Exception as e:
        print(f"[ERRO] não conseguiu conectar no banco: {e}")
        return 1

    with conn, conn.cursor() as cur:
        colunas = ["path", "codigo_laudo", "modelo_usado", "numero_proposta", "tipo_imovel",
                   "endereco", "numero", "complemento", "bairro", "municipio", "uf", "matricula",
                   "valor_mercado", "valor_venda_forcada", "area_privativa_m2", "area_comum_m2",
                   "area_total_m2", "quartos", "banheiros", "vagas", "idade_anos",
                   "estado_conservacao", "padrao_acabamento"]
        cur.execute(f"SELECT {', '.join(colunas)} FROM laudos WHERE path IS NOT NULL")
        laudos = {r[0]: dict(zip(colunas, r)) for r in cur.fetchall()}

    no_disco = set(os.listdir(PASTA_LAUDOS))
    candidatos = sorted(p for p in laudos if p in no_disco)

    qtd_env = os.getenv("CONFERIR_QTD", str(QTD_PADRAO)).strip().lower()
    qtd = len(candidatos) if qtd_env == "todos" else min(int(qtd_env), len(candidatos))
    semente = int(os.getenv("CONFERIR_SEMENTE") or random.randrange(1, 10**6))
    sorteados = random.Random(semente).sample(candidatos, qtd)

    print(f"Conferindo {qtd} laudos (semente {semente})...", flush=True)

    codigos = [laudos[p]["codigo_laudo"] for p in sorteados]
    with conn, conn.cursor() as cur:
        cur.execute("""SELECT codigo_laudo, numero_amostra, area_privativa_m2, area_terreno_m2,
                              valor, valor_unitario_m2, idade_anos, estado_conservacao, vagas
                       FROM laudos_amostras WHERE codigo_laudo = ANY(%s)""", (codigos,))
        amostras_banco = {}
        for r in cur.fetchall():
            amostras_banco.setdefault(r[0], {})[r[1]] = dict(zip(
                ["area_privativa_m2", "area_terreno_m2", "valor", "valor_unitario_m2",
                 "idade_anos", "estado_conservacao", "vagas"], r[2:]))
    conn.close()

    resultados = []  # (tabela, campo, classe, path, amostra, banco, testemunha, linha)
    sem_texto = 0
    num_workers = min(multiprocessing.cpu_count(), 8)
    with ProcessPoolExecutor(max_workers=num_workers) as ex:
        for feitos, (path, texto) in enumerate(
                ex.map(_ler_pdf, [os.path.join(PASTA_LAUDOS, p) for p in sorteados], chunksize=4), 1):
            if feitos % 100 == 0 or feitos == qtd:
                print(f"  ... {feitos}/{qtd}", flush=True)
            if not texto.strip():
                sem_texto += 1
                continue

            banco = laudos[path]
            digital = banco["modelo_usado"] == "digital"
            test = ler_testemunhas(texto)

            for campo, t in test["laudo"].items():
                if t is None or campo not in banco:
                    continue
                valor_banco = banco[campo]
                if campo == "numero_proposta":
                    # a pág. 3 do digital imprime a proposta sem o "_1" do pedido
                    valor_banco = str(valor_banco or "").split("_")[0]
                linha = linha_origem_laudo(texto, campo, digital)
                resultados.append(("laudos", campo, classificar(valor_banco, t, linha),
                                   path, "", valor_banco, t, linha))

            blocos = blocos_de_amostra(texto)
            do_banco = amostras_banco.get(banco["codigo_laudo"], {})
            for n, campos in test["amostras"].items():
                a = do_banco.get(n)
                if a is None:
                    resultados.append(("laudos_amostras", "(amostra inteira)", ERRO, path, n,
                                       "não gravada", "existe no laudo", ""))
                    continue
                for campo, t in campos.items():
                    if t is None:
                        continue
                    if campo == "area_construida":
                        # a tabela de cálculo não diz se a área é privativa
                        # ou de terreno (lote avaliado por comparativo usa a
                        # do terreno) - compara com a que o banco tiver
                        campo = "area_privativa_m2" if a["area_privativa_m2"] else "area_terreno_m2"
                    linha = linha_origem_amostra(blocos.get(n, ""), campo, digital)
                    resultados.append(("laudos_amostras", campo, classificar(a[campo], t, linha),
                                       path, n, a[campo], t, linha))

    # --- relatório
    total = len(resultados)
    por_classe = {c: sum(1 for r in resultados if r[2] == c) for c in (BATE, INCONSISTENTE, ERRO)}
    pct = lambda n, d: f"{100 * n / d:.1f}%" if d else "-"

    partes = [
        f"CONFERÊNCIA DA EXTRAÇÃO - {datetime.now():%d/%m/%Y %H:%M}",
        f"{qtd} laudos sorteados de {len(candidatos)} (semente {semente})"
        + (f" - {sem_texto} sem texto extraível, ignorados" if sem_texto else ""),
        "",
        "Cada campo do banco foi comparado com uma segunda fonte dentro do próprio",
        "laudo (tabelas de cálculo, AVALIAÇÃO FINAL, linha 'Avaliando'), lida por",
        "um código independente do extrator.",
        "",
        "=" * 78,
        "RESUMO",
        "=" * 78,
        f"  Conferências feitas:            {total}",
        f"  Batem:                          {por_classe[BATE]:>6}  ({pct(por_classe[BATE], total)})",
        f"  Laudo inconsistente:            {por_classe[INCONSISTENTE]:>6}  ({pct(por_classe[INCONSISTENTE], total)})"
        "   <- a extração leu certo; o laudo imprime valores diferentes em lugares diferentes",
        f"  Erro de extração:               {por_classe[ERRO]:>6}  ({pct(por_classe[ERRO], total)})",
        "",
        f"  TAXA DE ACERTO DA EXTRAÇÃO:     {pct(por_classe[BATE] + por_classe[INCONSISTENTE], total)}",
        "",
        "=" * 78,
        "POR CAMPO",
        "=" * 78,
    ]
    chaves = sorted({(r[0], r[1]) for r in resultados})
    linhas = []
    for tab, campo in chaves:
        rs = [r for r in resultados if r[0] == tab and r[1] == campo]
        b = sum(1 for r in rs if r[2] == BATE)
        i = sum(1 for r in rs if r[2] == INCONSISTENTE)
        e = sum(1 for r in rs if r[2] == ERRO)
        linhas.append([tab, campo, len(rs), b, i, e, pct(b + i, len(rs))])
    partes.append(_tabela(["tabela", "campo", "conferidos", "batem", "laudo_inconsist.",
                           "erro_extração", "acerto"], linhas))

    for classe, titulo in ((ERRO, "ERROS DE EXTRAÇÃO"), (INCONSISTENTE, "INCONSISTÊNCIAS DOS PRÓPRIOS LAUDOS")):
        rs = [r for r in resultados if r[2] == classe]
        partes += ["", "=" * 78, f"{titulo} ({len(rs)}; mostrando até {MAX_EXEMPLOS})", "=" * 78]
        if not rs:
            partes.append("  (nenhum)")
            continue
        partes.append(_tabela(["path", "amostra", "campo", "banco", "segunda fonte", "linha de origem no laudo"],
                              [[r[3], r[4], r[1], r[5], r[6], r[7]] for r in rs[:MAX_EXEMPLOS]]))

    relatorio = "\n".join(partes) + "\n"
    os.makedirs(LOG_DIR, exist_ok=True)
    caminho = os.path.join(LOG_DIR, f"conferencia_{datetime.now():%Y%m%d_%H%M%S}.txt")
    with open(caminho, "w", encoding="utf-8") as f:
        f.write(relatorio)
    print()
    print(relatorio)
    print(f"Relatório salvo em: {os.path.abspath(caminho)}")
    return 0

if __name__ == "__main__":
    sys.exit(main())
