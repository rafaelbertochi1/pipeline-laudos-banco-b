"""Checagens dos dados extraídos de um laudo - usadas pelo testar_amostra.py
(numa amostra, antes de rodar na base) e pelo banco_b_extractor.py (em
todo lote, antes de gravar: se alguma passar do limite, o lote não é
gravado). Todas são de problema que já aconteceu de verdade."""

import re
from decimal import Decimal

# Checagem -> fração tolerável do lote. 0 = bug conhecido. Os limites
# acima de zero vêm do que a base real já tem de legítimo (laudo que
# realmente não traz o campo): ~0,1-0,5% em 10 mil laudos.
LIMITES_DADOS = {
    # laudo
    "erro ao processar o PDF": 0,
    "sem código de laudo": 0,
    "modelo não bate com a metodologia": 0,
    "padrão/estado com pedaço de outra coluna": 0,
    "sem número de proposta": 0.05,
    "sem tipo de imóvel": 0.05,
    "valor de mercado zerado": 0.05,
    "sem área (privativa e terreno zeradas)": 0.05,
    "sem município/UF": 0.05,
    "sem endereço": 0.05,
    "venda forçada fora de 50-90% da avaliação": 0.05,
    # amostras
    "laudo sem nenhuma amostra": 0.05,
    "amostra com texto de outra coluna": 0,
    "amostra sem tipo, valor ou área": 0.05,
    "amostra com quartos/banheiros acima de 15": 0.05,
}

# no extrator, um lote só é barrado se a checagem passar do limite E
# aparecer em mais que isso de laudos: laudo isolado esquisito (PDF
# quebrado, formato único) não trava a carga inteira - bug de layout
# aparece em dezenas ou centenas
FOLGA_LAUDOS = 3


def texto_contaminado(valor, eh_estado=False):
    """Padrão/estado com número ou comprido demais = pedaço de outra coluna
    do PDF (ex.: "4 Regular 60 6,67 0,056 20", da tabela de depreciação).
    "Nova(até 5 anos)" é o único valor legítimo com número."""
    texto = str(valor or "")
    if eh_estado:
        texto = texto.replace("(até 5 anos)", "").replace("(ate 5 anos)", "")
    return bool(re.search(r"\d", texto)) or len(texto) > 40


def problemas_dos_dados(d, amostras):
    """[(checagem, detalhe)] dos dados de um laudo já extraído."""
    problemas = []

    def p(checagem, detalhe=""):
        problemas.append((checagem, detalhe))

    modelo = d.get("modelo_usado", "?")

    # --- dados do laudo
    if not d.get("codigo_laudo"):
        p("sem código de laudo")
    if not d.get("numero_proposta"):
        p("sem número de proposta")
    if not d.get("tipo_imovel"):
        p("sem tipo de imóvel")
    if not d.get("valor_mercado"):
        p("valor de mercado zerado")
    if not d.get("area_privativa_m2") and not d.get("area_terreno_m2"):
        p("sem área (privativa e terreno zeradas)")
    if not d.get("municipio") or not d.get("uf"):
        p("sem município/UF")
    if not d.get("endereco"):
        p("sem endereço")
    metodologia = d.get("metodologia") or ""
    if metodologia and (modelo == "digital") != (metodologia == "AVM"):
        p("modelo não bate com a metodologia", f"modelo {modelo}, metodologia {metodologia!r}")
    for campo, eh_estado in (("padrao_acabamento", False), ("estado_conservacao", True)):
        if texto_contaminado(d.get(campo), eh_estado):
            p("padrão/estado com pedaço de outra coluna", f"{campo}={d.get(campo)!r}")
    valor, venda = d.get("valor_mercado") or 0, d.get("valor_venda_forcada") or 0
    if valor and venda and not (Decimal("0.5") <= Decimal(venda) / Decimal(valor) <= Decimal("0.9")):
        p("venda forçada fora de 50-90% da avaliação", f"{venda} / {valor}")

    # --- amostras
    if not amostras:
        p("laudo sem nenhuma amostra")
    for a in amostras:
        n = a.get("numero_amostra")
        if not a.get("tipo_imovel") or not a.get("valor") or \
                (not a.get("area_privativa_m2") and not a.get("area_terreno_m2")):
            p("amostra sem tipo, valor ou área", f"amostra {n}")
        for campo, eh_estado in (("padrao_acabamento", False), ("estado_conservacao", True)):
            if texto_contaminado(a.get(campo), eh_estado):
                p("amostra com texto de outra coluna", f"amostra {n}: {campo}={a.get(campo)!r}")
        if (a.get("quartos") or 0) > 15 or (a.get("banheiros") or 0) > 15:
            p("amostra com quartos/banheiros acima de 15",
              f"amostra {n}: {a.get('quartos')} quartos, {a.get('banheiros')} banheiros")
    return problemas


def checagens_estouradas(contagem_laudos, total):
    """{checagem: laudos} das que passaram do limite no lote.
    `contagem_laudos`: {checagem: nº de laudos com ela}."""
    return {
        checagem: n for checagem, n in contagem_laudos.items()
        if n > LIMITES_DADOS.get(checagem, 0) * total + FOLGA_LAUDOS
    }
