"""Teste rápido: roda a extração (dados + imagens) numa amostra de laudos
e aponta problema ANTES de rodar na base inteira.

Não grava nada no banco nem em data/imagens/ - as imagens vão pra uma
pasta temporária que é apagada no final. Pode rodar quantas vezes quiser.

Uso (dentro da pasta "Backend l Script Extração Laudos"):

    python testar_amostra.py

Quando rodar: depois de um `git pull` que mudou a extração (antes de
reprocessar tudo com FORCAR_REPROCESSAR) e depois de baixar meses novos
(antes de extrair) - laudo novo é onde layout novo aparece.

A amostra junta laudos sorteados da pasta com os baixados mais
recentemente (1/3 da amostra), porque foi em laudo novo que o layout
mudou da última vez. Padrão: 60 laudos, 1-2 minutos.
    $env:TESTE_QTD="150"      amostra maior
    $env:TESTE_SEMENTE="123"  repete o mesmo sorteio (o nº sai no relatório)

Três tipos de checagem, todas de problema que já aconteceu de verdade:
1. dados absurdos ou vazios (proposta, tipo, valor, área, texto com
   pedaço de outra coluna, modelo digital com metodologia de físico...);
2. conferência contra a segunda fonte dentro do próprio PDF - as mesmas
   regras do conferir_extracao.py, só que sem precisar de banco;
3. imagens: cômodo igual à fachada, cômodo em laudo digital, sem fachada,
   foto do relatório sem legenda, e quais legendas viraram "outro" (pra
   crescer a lista de labels do banco_b_imagens.py com dado real).

Relatório em logs/teste_rapido_<data>.txt, com veredito no final.
"""

import hashlib
import multiprocessing
import os
import random
import shutil
import sys
import tempfile
import time
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime

import pdfplumber

import checagens
import conferir_extracao as conf
import banco_b_extractor as se
import banco_b_imagens as si

PASTA_LAUDOS = os.path.join("data", "laudos")
LOG_DIR = "logs"
QTD_PADRAO = 60
MAX_EXEMPLOS_POR_CHECAGEM = 8

# Checagem -> quanto dela é tolerável na amostra antes do veredito virar
# "não rode ainda". 0 = bug conhecido, não pode aparecer nenhuma vez. Os
# limites acima de zero vêm do que a base real já tem de legítimo (laudo
# que realmente não traz o campo): ~0,1-0,5% em 10 mil laudos.
LIMITES = {
    # imagens
    "cômodo com a mesma foto da fachada": 0,
    "sala vinda de área comum ou vizinha (salão de festas...)": 0,
    "cômodo já salvo em data/imagens igual à fachada salva": 0,
    "foto de cômodo em laudo digital (não tem vistoria)": 0,
    "laudo não digital tratado como digital nas imagens (perderia os cômodos)": 0,
    "sem foto de fachada": 0.10,
    # foto do relatório sem legenda embaixo (layout diferente do esperado?)
    "laudo com foto do relatório sem legenda": 0.10,
    # legenda que não caiu em nenhum label de CATEGORIAS_FOTO - não é erro,
    # é lista pra crescer: o relatório mostra as legendas mais comuns
    "laudo com foto classificada como 'outro'": 1.0,
    # dados do laudo e das amostras (mesmas do extrator - ver checagens.py)
    **checagens.LIMITES_DADOS,
}
# erro de extração na conferência contra a segunda fonte do PDF: a base
# real ficou em ~0,7% (quase tudo falso positivo do próprio conferidor)
LIMITE_ERRO_CONFERENCIA = 0.02


# legendas de área comum do condomínio/unidade vizinha que já viraram
# "sala" - fixo aqui (e não lido de banco_b_imagens) pra pegar o bug
# mesmo se a lista de lá for apagada
AREA_COMUM = ("salao de festa", "salao festa", "sala de jogo", "sala jogo", "sala vizinha")


def _hash(caminho):
    with open(caminho, "rb") as f:
        return hashlib.md5(f.read()).hexdigest()


def analisar_pdf(caminho, pasta_imagens):
    """Roda a extração de um PDF e devolve (nome, modelo, problemas,
    conferências). Roda num processo separado - por isso anula aqui (e não
    no processo principal) a gravação de imagem que o extrator faz em
    data/imagens/, e extrai as imagens de novo numa pasta temporária."""
    se.extrair_imagens_do_laudo = lambda *a, **k: None

    nome = os.path.basename(caminho)
    problemas = []
    conferencias = []

    resultado = se.extrair_dados_pdf(caminho)

    # antes de tudo (vale também pra PDF que o extrator não lê): a limpeza
    # de imagens só pode tratar como digital o que o extrator também trata
    # (o extrator chama de "digital" também o layout antigo que não
    # reconhece, então confere que é laudo Plataforma - "DADOS DO PEDIDO")
    try:
        with si.pymupdf.open(caminho) as documento:
            _paginas, digital_nas_imagens = si._paginas_de_comodo(documento)
            texto_norm = si._normalizar(" ".join(pg.get_text() for pg in documento))
            eh_plataforma = "dados do pedido" in texto_norm
            tem_relatorio_fotografico = "relatorio fotografico" in texto_norm
    except Exception:
        digital_nas_imagens, eh_plataforma, tem_relatorio_fotografico = False, True, False
    modelo_extrator = (resultado.get("dados") or {}).get("modelo_usado", "?") \
        if resultado["status"] == "ok" else "?"
    if digital_nas_imagens and (modelo_extrator != "digital" or not eh_plataforma):
        problemas.append(("laudo não digital tratado como digital nas imagens (perderia os cômodos)",
                          f"extrator: {resultado['status']}, modelo {modelo_extrator}"))

    if resultado["status"] != "ok":
        motivo = resultado.get("mensagem") or "PDF sem texto (escaneado?)"
        return nome, "?", problemas + [("erro ao processar o PDF", motivo)], []

    d = resultado["dados"]
    amostras = resultado["amostras"]
    modelo = d.get("modelo_usado", "?")
    if se.fica_fora_do_banco(d):
        # empreendimento inteiro: não vai pro banco, nada a checar nos dados
        return nome, "fora do banco", problemas, []

    def p(checagem, detalhe=""):
        problemas.append((checagem, detalhe))

    # --- dados do laudo e das amostras
    problemas.extend(checagens.problemas_dos_dados(d, amostras))

    # --- imagens (pasta temporária)
    try:
        fachada, _ = si.extrair_imagem_fachada(caminho, pasta_imagens)
        fotos = si.extrair_fotos_relatorio(caminho, pasta_destino=pasta_imagens, fachada_salva=fachada)
        if not fachada:
            p("sem foto de fachada")
        hash_fachada = _hash(fachada) if fachada else None
        sem_legenda = [f for f in fotos if f["label"] == si.LABEL_SEM_LEGENDA]
        outros = [f["legenda"] for f in fotos if f["label"] == si.LABEL_OUTRO]
        if sem_legenda:
            p("laudo com foto do relatório sem legenda", f"{len(sem_legenda)} de {len(fotos)} foto(s)")
        if outros:
            p("laudo com foto classificada como 'outro'", "; ".join(repr(l) for l in outros[:5]))
        for f in fotos:
            categoria, arquivo, legenda = f["label"], f["caminho"], f["legenda"]
            # laudo com relatório fotográfico tem foto de cômodo de verdade,
            # mesmo que o extrator o leia como digital (ex.: crédito PJ de
            # empreendimento inteiro, sem o questionário da unidade)
            if modelo == "digital" and not tem_relatorio_fotografico:
                p("foto de cômodo em laudo digital (não tem vistoria)", f"{categoria} (legenda {legenda!r})")
            if hash_fachada and categoria != "fachada" and _hash(arquivo) == hash_fachada:
                p("cômodo com a mesma foto da fachada", f"{categoria} (legenda {legenda!r})")
            if categoria == "sala" and si._normalizar(legenda or "").startswith(AREA_COMUM):
                p("sala vinda de área comum ou vizinha (salão de festas...)", f"legenda {legenda!r}")
    except Exception as e:
        p("erro ao processar o PDF", f"imagens: {e}")

    # --- imagens já salvas na base (só lê): versão antiga que salvou a
    # fachada como cômodo e nunca foi revista
    nome_base = os.path.splitext(nome)[0]
    fachada_salva = si._imagem_existente(si.PASTA_IMAGENS, nome_base, "img")
    if fachada_salva:
        hash_salva = _hash(fachada_salva)
        for categoria in si.CATEGORIAS_COMODO:
            salvo = si._imagem_existente(si.PASTA_IMAGENS, nome_base, categoria)
            if salvo and _hash(salvo) == hash_salva:
                p("cômodo já salvo em data/imagens igual à fachada salva", categoria)

    # --- conferência contra a segunda fonte do PDF (mesmas regras do
    # conferir_extracao.py, comparando com o que acabou de ser extraído
    # em vez do que está no banco)
    try:
        with pdfplumber.open(caminho) as pdf:
            texto = "".join((pg.extract_text() or "") + "\n" for pg in pdf.pages)
        digital = modelo == "digital"
        test = conf.ler_testemunhas(texto)
        for campo, t in test["laudo"].items():
            if t is None or campo not in d:
                continue
            valor_extraido = d[campo]
            if campo == "numero_proposta":
                valor_extraido = str(valor_extraido or "").split("_")[0]
            linha = conf.linha_origem_laudo(texto, campo, digital)
            conferencias.append(("laudo", campo, conf.classificar(valor_extraido, t, linha), valor_extraido, t))
        blocos = conf.blocos_de_amostra(texto)
        por_numero = {a["numero_amostra"]: a for a in amostras}
        for n, campos in test["amostras"].items():
            a = por_numero.get(n)
            if a is None:
                conferencias.append(("amostra", f"amostra {n} inteira", conf.ERRO, "não extraída", "existe no laudo"))
                continue
            for campo, t in campos.items():
                if t is None:
                    continue
                if campo == "area_construida":
                    campo = "area_privativa_m2" if a.get("area_privativa_m2") else "area_terreno_m2"
                linha = conf.linha_origem_amostra(blocos.get(n, ""), campo, digital)
                conferencias.append((f"amostra {n}", campo, conf.classificar(a.get(campo), t, linha), a.get(campo), t))
    except Exception as e:
        p("erro ao processar o PDF", f"conferência: {e}")

    return nome, modelo, problemas, conferencias


def escolher_amostra(qtd, semente):
    """1/3 dos laudos baixados mais recentemente + o resto sorteado."""
    todos = [f for f in os.listdir(PASTA_LAUDOS) if f.lower().endswith(".pdf")]
    if not todos:
        return []
    qtd = min(qtd, len(todos))
    por_data = sorted(todos, key=lambda f: os.path.getmtime(os.path.join(PASTA_LAUDOS, f)), reverse=True)
    recentes = por_data[: qtd // 3]
    resto = [f for f in todos if f not in set(recentes)]
    sorteados = random.Random(semente).sample(resto, min(qtd - len(recentes), len(resto)))
    return [os.path.join(PASTA_LAUDOS, f) for f in recentes + sorteados]


def main():
    if not os.path.isdir(PASTA_LAUDOS):
        print(f"[ERRO] pasta {PASTA_LAUDOS} não encontrada - rode de dentro de "
              f"'Backend l Script Extração Laudos'.")
        return 1

    qtd = int(os.getenv("TESTE_QTD") or QTD_PADRAO)
    semente = int(os.getenv("TESTE_SEMENTE") or random.randrange(1, 10**6))
    pdfs = escolher_amostra(qtd, semente)
    if not pdfs:
        print("[ERRO] nenhum PDF em data/laudos.")
        return 1

    print(f"Testando {len(pdfs)} laudos (semente {semente}) - não grava no banco nem em data/imagens...")
    pasta_imagens = tempfile.mkdtemp(prefix="teste_rapido_imagens_")
    inicio = time.time()
    resultados = []
    try:
        with ProcessPoolExecutor(max_workers=min(multiprocessing.cpu_count(), 8)) as ex:
            futuros = [ex.submit(analisar_pdf, p, pasta_imagens) for p in pdfs]
            for i, futuro in enumerate(as_completed(futuros), 1):
                resultados.append(futuro.result())
                if i % 20 == 0 or i == len(futuros):
                    print(f"  ... {i}/{len(futuros)}", flush=True)
    finally:
        shutil.rmtree(pasta_imagens, ignore_errors=True)
    duracao = time.time() - inicio

    total = len(resultados)
    modelos = Counter(r[1] for r in resultados)
    ocorrencias = defaultdict(list)  # checagem -> [(pdf, detalhe)]
    laudos_por_checagem = defaultdict(set)
    for nome, _modelo, problemas, _conf in resultados:
        for checagem, detalhe in problemas:
            ocorrencias[checagem].append((nome, detalhe))
            laudos_por_checagem[checagem].add(nome)

    confs = [(nome, c) for nome, _m, _p, cs in resultados for c in cs]
    c_bate = sum(1 for _, c in confs if c[2] == conf.BATE)
    c_inc = sum(1 for _, c in confs if c[2] == conf.INCONSISTENTE)
    c_erro = [(nome, c) for nome, c in confs if c[2] == conf.ERRO]
    pct = lambda n, d: f"{100 * n / d:.1f}%" if d else "-"

    reprovadas = []
    linhas = [
        f"TESTE RÁPIDO DA EXTRAÇÃO - {datetime.now():%d/%m/%Y %H:%M}",
        f"{total} laudos (semente {semente}): " + ", ".join(f"{n} {m}" for m, n in modelos.most_common()),
        f"Tempo: {duracao:.0f}s ({duracao / max(total, 1):.1f}s por laudo, contando imagens e conferência)",
        "",
        "=" * 78,
        "CHECAGENS (laudos afetados / tolerado)",
        "=" * 78,
    ]
    for checagem, limite in LIMITES.items():
        afetados = len(laudos_por_checagem.get(checagem, ()))
        tolerado = int(limite * total)
        estourou = afetados > tolerado
        if estourou:
            reprovadas.append(checagem)
        marca = "PROBLEMA" if estourou else ("ok" if afetados == 0 else "ok (dentro do normal)")
        linhas.append(f"  [{marca:^21}] {checagem}: {afetados} / {tolerado}")

    linhas += [
        "",
        "=" * 78,
        "CONFERÊNCIA CONTRA A SEGUNDA FONTE DO PDF",
        "=" * 78,
        f"  Conferências:         {len(confs)}",
        f"  Batem:                {c_bate} ({pct(c_bate, len(confs))})",
        f"  Laudo inconsistente:  {c_inc} ({pct(c_inc, len(confs))})  <- o PDF se contradiz, não é erro nosso",
        f"  Erro de extração:     {len(c_erro)} ({pct(len(c_erro), len(confs))})",
        f"  Acerto da extração:   {pct(c_bate + c_inc, len(confs))}",
    ]
    if confs and len(c_erro) / len(confs) > LIMITE_ERRO_CONFERENCIA:
        reprovadas.append("erro de extração na conferência")

    linhas += ["", "=" * 78, "DETALHES", "=" * 78]
    for checagem in LIMITES:
        if checagem not in ocorrencias:
            continue
        linhas.append(f"  {checagem}:")
        for nome, detalhe in ocorrencias[checagem][:MAX_EXEMPLOS_POR_CHECAGEM]:
            linhas.append(f"    {nome}  {detalhe}")
        if len(ocorrencias[checagem]) > MAX_EXEMPLOS_POR_CHECAGEM:
            linhas.append(f"    ... e mais {len(ocorrencias[checagem]) - MAX_EXEMPLOS_POR_CHECAGEM}")
    if c_erro:
        linhas.append("  erro de extração na conferência (campo: extraído x segunda fonte):")
        for nome, c in c_erro[:MAX_EXEMPLOS_POR_CHECAGEM]:
            linhas.append(f"    {nome}  {c[0]} {c[1]}: {c[3]} x {c[4]}")

    linhas += ["", "=" * 78]
    if reprovadas:
        linhas += [
            "VEREDITO: NÃO RODE NA BASE INTEIRA AINDA.",
            "Problema em: " + "; ".join(reprovadas) + ".",
            "Mande este relatório (e 1 ou 2 dos PDFs citados em DETALHES) antes.",
        ]
    else:
        linhas += ["VEREDITO: OK - pode rodar na base inteira."]
    linhas.append("=" * 78)

    relatorio = "\n".join(linhas) + "\n"
    os.makedirs(LOG_DIR, exist_ok=True)
    caminho = os.path.join(LOG_DIR, f"teste_rapido_{datetime.now():%Y%m%d_%H%M%S}.txt")
    with open(caminho, "w", encoding="utf-8") as f:
        f.write(relatorio)
    print()
    print(relatorio)
    print(f"Relatório salvo em: {os.path.abspath(caminho)}")
    return 1 if reprovadas else 0


if __name__ == "__main__":
    sys.exit(main())
