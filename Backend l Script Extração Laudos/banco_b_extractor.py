import json
import os
import re
import sys
import multiprocessing
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime
from decimal import Decimal, InvalidOperation
import pdfplumber
import psycopg2
from psycopg2.extras import execute_values

from banco_b_imagens import extrair_imagens_do_laudo, extrair_opcoes_marcadas, marcar_verificados
import checagens

ZERO = Decimal('0.00')
PASTA_SCRIPT = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(PASTA_SCRIPT, "logs")


class Tee:
    """Escreve simultaneamente no terminal e num arquivo de log."""

    def __init__(self, *streams):
        self.streams = streams

    def write(self, dado):
        for s in self.streams:
            s.write(dado)

    def flush(self):
        for s in self.streams:
            s.flush()

def converter_float_seguro(val):
    # Decimal em vez de float pra não gerar sobra de binário
    # (8666.299999999999 em vez de 8666.30) nas colunas NUMERIC.
    if val is None:
        return ZERO
    if isinstance(val, Decimal):
        return val.quantize(Decimal('0.01'))
    if isinstance(val, (int, float)):
        try:
            return Decimal(str(val)).quantize(Decimal('0.01'))
        except InvalidOperation:
            return ZERO

    s_val = str(val).strip()
    if not s_val or s_val.upper() in ['-', '--', '—', 'NONE', 'NULL']:
        return ZERO

    match = re.search(r'[-+]?[\d\.,]+', s_val)
    if not match:
        return ZERO

    num_str = match.group(0)

    if '.' in num_str and ',' in num_str:
        num_str = num_str.replace('.', '').replace(',', '.')
    elif '.' in num_str and ',' not in num_str:
        partes = num_str.split('.')
        if len(partes) == 2 and len(partes[1]) == 3:
            num_str = num_str.replace('.', '')
        elif len(partes) > 2:
            num_str = num_str.replace('.', '')
    elif ',' in num_str:
        num_str = num_str.replace(',', '.')

    try:
        return Decimal(num_str).quantize(Decimal('0.01'))
    except InvalidOperation:
        return ZERO

def converter_area_plataforma(val):
    """Área impressa pelo Plataforma. O laudo mudou de formato com o tempo:
    os antigos usam ponto decimal ("216.04", "61.800" = 61,8 m²) e os novos
    vírgula ("95,30"). Nenhum dos dois usa separador de milhar sem decimal,
    então a regra é: tem vírgula -> pt-BR (ponto é milhar); não tem -> o
    ponto é decimal.

    Sem isso, "61.800" caía na regra de milhar do converter_float_seguro e
    virava 61800 m² - foi assim que 28 laudos ficaram com área inflada.
    """
    texto = str(val or "").strip()
    if texto and ',' not in texto and texto.count('.') == 1:
        try:
            return Decimal(texto.replace(' ', '')).quantize(Decimal('0.01'))
        except InvalidOperation:
            pass
    return converter_float_seguro(val)

def converter_int_seguro(val):
    try:
        resultado = int(converter_float_seguro(val))
    except Exception:
        return 0
    # nenhum desses campos (quartos, banheiros, vagas, suítes, idade) é
    # plausivelmente >= 1000 - evita "integer out of range" no banco
    # quando o regex pega um CPF, CEP ou ID de anúncio por engano
    return resultado if 0 <= resultado < 1000 else 0

def calcular_valor_unitario(valor_mercado, area_base):
    # valor_unitario_m2 não vem mais de regex no texto - é sempre
    # valor_mercado / área (privativa, ou terreno pra lotes), calculado
    # depois que os dois já foram extraídos.
    if not area_base or area_base <= 0:
        return ZERO
    return (valor_mercado / area_base).quantize(Decimal('0.01'))

def converter_float_coordenada(val):
    if val is None:
        return None
    if isinstance(val, (int, float)):
        return float(val)

    s_val = str(val).strip()
    if not s_val or s_val.upper() in ['-', '--', '—', 'NONE', 'NULL']:
        return None

    s_val = re.sub(r'-\s+', '-', s_val)
    match = re.search(r'[-+]?\d+\.\d+', s_val)
    if not match:
        return None

    try:
        return float(match.group(0))
    except Exception:
        return None

def limpar_txt(val, valor_padrao=""):
    if not val:
        return valor_padrao
    txt = str(val).strip()
    txt = re.sub(r'^(Número|Complemento|Matrícula|Núm|Bairro|Municipio|UF|Endere[çc]o)\s*', '', txt, flags=re.IGNORECASE)
    txt = str(txt).strip()
    return txt if txt and txt.upper() != "NULL" else valor_padrao

def extrair_numero_proposta(text):
    # Ancora na data que vem logo depois do número (ex: "2.714.283 01/07/2026")
    # pra não cair no CREA do avaliador ou outro número solto do documento.
    m = re.search(
        r'N[º°]?\s*da\s*Proposta[^\n]*\n(?:\S+\s+)?([\d][\d\.]{4,14}\d)\s+\d{2}/\d{2}/\d{4}',
        text, re.IGNORECASE
    )
    if m:
        return m.group(1).replace('.', '')

    m = re.search(r'(?:Proposta|N[º°]?\s*da\s*Proposta)\s*[:\n]?\s*(\d{6,12})', text, re.IGNORECASE)
    if m:
        return m.group(1)

    m = re.search(r'\b(\d{7,10})\b', text)
    return m.group(1) if m else ""

def extrair_data_avaliacao(text):
    # A data da vistoria de verdade, não a Data Solicitação do cabeçalho
    # (que costuma vir antes e pode ficar semanas fora do dia real da
    # visita). A seção RELATÓRIO FOTOGRÁFICO carimba a data de cada foto
    # tirada na visita, é a âncora mais confiável.
    m = re.search(r'RELAT[ÓO]RIO\s+FOTOGR[ÁA]FICO[^\n]*\n+\s*(\d{2}/\d{2}/\d{4})', text, re.IGNORECASE)
    if m:
        return m.group(1)

    m = re.search(r'Vistoria\s+realizada\s+em\s+(\d{2}/\d{2}/\d{4})', text, re.IGNORECASE)
    if m:
        return m.group(1)

    m = re.search(r'(\d{2}/\d{2}/\d{4})', text)
    return m.group(1) if m else None

def _dms_para_decimal(graus, minutos, segundos, hemisferio):
    valor = float(graus) + float(minutos) / 60 + float(segundos) / 3600
    return -valor if hemisferio in ('S', 'W') else valor

def extrair_coordenadas_generico(text):
    texto_limpo = re.sub(r'-\s+([\d\.]+)', r'-\1', text)
    # "Localização" também aparece como título de seção sem coordenada
    # nenhuma perto - percorre todas as ocorrências até achar uma com
    # números de verdade, em vez de parar na primeira (que pode ser essa).
    for match_rotulo in re.finditer(
        r'(?:Coordenadas|Localização|Geolocalização)[^\n:]*[:\n]?\s*([-\d\.\s,\n]+)',
        texto_limpo,
        re.IGNORECASE
    ):
        candidatos = re.findall(r'(-?\d{1,3}\.\d{4,16})', match_rotulo.group(1))
        if len(candidatos) >= 2:
            lat_raw = converter_float_coordenada(candidatos[0])
            lon_raw = converter_float_coordenada(candidatos[1])
            if lat_raw is not None and lon_raw is not None:
                lat_final = -abs(lat_raw)
                lon_final = -abs(lon_raw)
                return f"{lat_final}, {lon_final}", lat_final, lon_final

    # sem rótulo "Coordenadas:" explícito - tenta o carimbo de GPS das
    # fotos (grau/min/seg, ex: 9°25'58"S / 40°28'13"W), que se repete no
    # RELATÓRIO FOTOGRÁFICO. Sem isso, varrer o documento inteiro atrás de
    # qualquer número parecido com coordenada pega lixo de outras tabelas
    # (já vimos pegar desvio padrão de uma análise estatística por engano).
    m = re.search(
        r"(\d{1,3})°(\d{1,2})'(\d{1,2})\"([NS])\s*/\s*(\d{1,3})°(\d{1,2})'(\d{1,2})\"([EW])",
        texto_limpo
    )
    if m:
        lat_g, lat_m, lat_s, lat_h, lon_g, lon_m, lon_s, lon_h = m.groups()
        lat_final = round(_dms_para_decimal(lat_g, lat_m, lat_s, lat_h), 8)
        lon_final = round(_dms_para_decimal(lon_g, lon_m, lon_s, lon_h), 8)
        return f"{lat_final}, {lon_final}", lat_final, lon_final

    return None, None, None

_TIPOS_LOGRADOURO = (
    r'RUA|AVENIDA|AV\.|AL\.|ALAMEDA|ESTRADA|TRAVESSA|ROD\.|RODOVIA|'
    r'LARGO|PRA[ÇC]A|VIA|QUADRA'
)

def extrair_endereco_numero(text):
    direto = re.search(rf'(?:{_TIPOS_LOGRADOURO})\s+[^\n\r]+', text, re.IGNORECASE)
    if direto:
        trecho = direto.group(0).strip()
        m = re.match(
            rf'^((?:{_TIPOS_LOGRADOURO})\s+[^\d\n]+?)\s+(\d{{1,5}}|S/N)\b',
            trecho, re.IGNORECASE
        )
        if m:
            return m.group(1).strip(), m.group(2)
        return trecho, "S/N"
    return "", "S/N"

def extrair_complemento_generico(text):
    if not text:
        return ""
    
    match_rotulo = re.search(r'Complemento\s*[\n\r:]+\s*([^\n]+)', text, re.IGNORECASE)
    txt_busca = match_rotulo.group(1).strip() if match_rotulo else text

    pat_complemento = (
        r'(?:AP|APTO|APARTAMENTO|FLAT|BL|BL-?\d*|BLOCO|TORRE|CASA|SOBRADO|'
        r'LOTE|QUADRA|UNIDADE|SL|SALA|CONDOM[ÍI]NIO).*$'
    )

    match_compl = re.search(pat_complemento, txt_busca, re.IGNORECASE)
    if match_compl:
        return limpar_txt(match_compl.group(0))

    pat_corte_logradouro = (
        rf'^(?:{_TIPOS_LOGRADOURO})\s+.+?\s+(\d{{1,5}}|S/N)\b\s*[:,-]?\s*'
    )
    txt_sem_logradouro = re.sub(pat_corte_logradouro, '', txt_busca, flags=re.IGNORECASE).strip()

    if txt_sem_logradouro and txt_sem_logradouro != txt_busca:
        return limpar_txt(txt_sem_logradouro)

    return ""

# Opções fixas do Plataforma pros dois blocos da página "Região". Servem pra
# (a) separar o valor da coluna da esquerda do primeiro item da direita, que
# o pdfplumber junta na mesma linha, e (b) classificar as opções marcadas
# do modelo digital, onde os dois blocos vêm numa lista só.
OPCOES_INFRA_URBANA = [
    "Água", "Pavimentação", "Esgoto Sanitário", "Esgoto Pluvial",
    "Energia Elétrica", "Telefone", "Iluminação Pública", "Gás Canalizado",
    "Fossa", "Cisterna/Poço",
]
OPCOES_SERVICOS_PUBLICOS = [
    "Metrô", "Escola", "Ônibus", "Rede Bancária", "Lazer", "Centro Comercial",
    "Aeroporto", "Parque", "Coleta de Lixo", "Shopping Center", "Shopping",
    "Clínicas/Hospitais", "Segurança", "Vista",
]

def _lista_da_secao(text, rotulo, opcoes, lista_a_direita):
    """Lê a lista de itens de um bloco da página "Região" do modelo físico.

    A página é em duas colunas e o pdfplumber junta as duas na mesma linha,
    então a primeira linha depois do cabeçalho mistura o valor do bloco
    vizinho com o primeiro item da lista:

        01 - Região 02 - Infraestrutura Urbana
        Residencial Multifamiliar Água        <- valor da esq. + 1º item
        Energia Elétrica
        ...
        03 - Tipo de Pavimentação 04 - Restritivos

    `lista_a_direita` diz de que lado a lista está: True pra Infraestrutura
    Urbana (o item é o FIM da primeira linha), False pra Serviços Públicos
    (o item é o COMEÇO da linha, e o vizinho "06 - Localização" vem depois).
    Usa as opções conhecidas pra fazer esse corte.
    """
    m = re.search(r'\d{2}\s*-\s*' + rotulo + r'[^\n]*\n', text, re.IGNORECASE)
    if not m:
        return ""
    itens = []
    primeira = True
    for linha in text[m.end():].split('\n'):
        linha = linha.strip()
        if not linha:
            continue
        if re.match(r'^\d{2}\s*-\s', linha) or (len(linha) > 5 and linha == linha.upper()):
            break
        if primeira:
            primeira = False
            # opção mais longa primeiro, senão "Shopping" ganha de "Shopping Center"
            for opcao in sorted(opcoes, key=len, reverse=True):
                if lista_a_direita and linha.endswith(opcao):
                    itens.append(opcao)
                    break
                if not lista_a_direita and linha.startswith(opcao):
                    itens.append(opcao)
                    break
            continue
        itens.append(linha)
    return "; ".join(itens)

def extrair_infraestrutura_urbana(text):
    """Bloco "02 - Infraestrutura Urbana" (Água, Energia Elétrica, Esgoto...)
    da página "Região" do modelo físico. No digital vem de checkbox - ver
    extrair_melhoramentos_digital."""
    return _lista_da_secao(text, r'Infraestrutura\s+Urbana', OPCOES_INFRA_URBANA, True)

def extrair_servicos_publicos(text):
    """Bloco "05 - Serviços Públicos e Comunitários" (Ônibus, Escola, Coleta
    de Lixo...) da página "Região" do modelo físico."""
    return _lista_da_secao(text, r'Servi[çc]os\s+P[úu]blicos', OPCOES_SERVICOS_PUBLICOS, False)

def extrair_melhoramentos_digital(pdf_path):
    """No modelo digital os dois blocos viram uma lista única de checkboxes
    ("Melhoramentos Públicos e Infra-estrutura da Região"). Lê as marcadas
    e separa pelas listas de opções. Devolve (infraestrutura_urbana,
    servicos_publicos) já no mesmo formato "a; b; c" do físico."""
    marcadas = extrair_opcoes_marcadas(pdf_path, "Melhoramentos")
    infra = [o for o in marcadas if o in OPCOES_INFRA_URBANA]
    servicos = [o for o in marcadas if o in OPCOES_SERVICOS_PUBLICOS]
    return "; ".join(infra), "; ".join(servicos)

def extrair_infraestrutura(text):
    """Itens de infraestrutura/área comum do prédio (Playground, Churrasqueira,
    Quadra Esportiva, Piscina...). No laudo vêm empilhados um por linha embaixo
    do rótulo "11 - Infraestrutura", até a próxima seção.

    Não confundir com "02 - Infraestrutura Urbana" (água, energia, esgoto), que
    é da região e não do prédio - daí o lookahead negativo no rótulo.
    """
    m = re.search(r'\d{2}\s*-\s*Infraestrutura(?!\s+Urbana)[^\n]*\n', text, re.IGNORECASE)
    if not m:
        return ""

    itens = []
    for linha in text[m.end():].split('\n'):
        linha = linha.strip()
        if not linha:
            continue
        # para no próximo campo numerado ("12 - ...") ou no título da próxima
        # seção. Os valores vêm de uma lista fixa do Plataforma, sempre em
        # Maiúscula/minúscula ("Quadra Esportiva"), então linha inteira em
        # CAIXA ALTA é cabeçalho de seção, não item.
        if re.match(r'^\d{2}\s*-\s', linha) or (len(linha) > 5 and linha == linha.upper()):
            break
        itens.append(linha)

    return "; ".join(itens)

# Rótulos que aparecem na linha de cabeçalho de cada bloco da amostra e o
# campo/tipo do valor correspondente na linha de baixo. A ordem importa:
# "Valor unitário" tem que vir antes de "Valor", senão "Valor" casa primeiro.
#   int   -> só dígitos           (quartos, idade)
#   num   -> número com vírgula   (áreas)
#   money -> "R$ 1.234,56"        (valores)
#   word  -> uma palavra          (padrão)
#   text  -> o que sobrar         (tipo, estado)
# "Padrão terreno" e "Topografia" não vão pro banco, mas o rótulo precisa
# continuar reconhecido: sem ele a linha "Área do terreno | Padrão terreno
# | Topografia" deixa de ser lida e a área do terreno se perde.
ROTULOS_AMOSTRA = [
    (r'Tipo\s+de\s+Im[óo]vel',          "tipo_imovel",        "text"),
    (r'Qtd\.?\s*Quartos',               "quartos",            "int"),
    (r'Qtd\.?\s*Banheiros',             "banheiros",          "int"),
    (r'Qtd\.?\s*Vagas',                 "vagas",              "int"),
    (r'[ÁA]rea\s+privativa',            "area_privativa_m2",  "num"),
    (r'[ÁA]rea\s+do\s+terreno',         "area_terreno_m2",    "num"),
    (r'Valor\s+unit[áa]rio[^\s]*(?:\s*\([^)]*\))?', "valor_unitario_m2", "money"),
    (r'Valor',                          "valor",              "money"),
    (r'Idade\s+aparente',               "idade_anos",         "int"),
    (r'Padr[ãa]o\s+de\s+acabamento',    "padrao_acabamento",  "word"),
    (r'Padr[ãa]o\s+terreno',            "padrao_terreno",     "word"),
    (r'Estado\s+de\s+conserva[çc][ãa]o', "estado_conservacao", "text"),
    (r'Topografia',                     "topografia",         "text"),
]
_RE_ROTULOS_AMOSTRA = [(re.compile(p, re.IGNORECASE), campo, tipo)
                       for p, campo, tipo in ROTULOS_AMOSTRA]

# grupo de captura por tipo de valor - versão estrita e versão relaxada
# (número pode faltar, ex.: idade em branco em terreno)
_GRUPO_ESTRITO = {
    "int": r'(\d+)', "num": r'([\d\.,]+)', "money": r'R\$\s*([\d\.,]+)',
    "word": r'(\S+)', "text": r'(.+?)',
}
# no relaxado TODO grupo é opcional. Antes "word"/"text" eram obrigatórios,
# e numa linha de terreno que só traz a idade ("10") o padrão fatiava o
# número: idade vazia, padrão "1", estado "0".
_GRUPO_RELAXADO = {
    "int": r'(\d+)?', "num": r'([\d\.,]+)?', "money": r'(?:R\$\s*([\d\.,]+))?',
    "word": r'(\S+)?', "text": r'(.+?)?',
}

# linhas de rodapé/cabeçalho de página que o pdfplumber joga no meio de uma
# amostra quando ela atravessa a quebra de página
_RE_RODAPE_PAGINA = re.compile(
    r'^(powered by|\d+\.\d+\.\d+\s+\d+\s*\|\s*\d+|plataforma\.com|'
    r'LAUDO DE AVALIA[ÇC][ÃA]O\s*\|.*|CR[ÉE]DITO IMOBILI[ÁA]RIO.*)$',
    re.IGNORECASE
)

def _ler_cabecalho_amostra(linha):
    """Identifica os rótulos de uma linha de cabeçalho, na ordem em que
    aparecem. Devolve [] se a linha não é um cabeçalho reconhecido (se
    sobrar qualquer texto que não seja rótulo, também não é)."""
    resto = linha
    achados = []
    while resto.strip():
        resto = resto.strip()
        for padrao, campo, tipo in _RE_ROTULOS_AMOSTRA:
            m = padrao.match(resto)
            if m:
                achados.append((campo, tipo))
                resto = resto[m.end():]
                break
        else:
            return []
    return achados

def _ler_valores_amostra(linha, rotulos):
    """Aplica na linha de valores o padrão montado a partir dos rótulos do
    cabeçalho. Tenta a versão estrita primeiro; se não casar (algum número
    em branco), tenta a relaxada."""
    for grupos in (_GRUPO_ESTRITO, _GRUPO_RELAXADO):
        sep = r'\s+' if grupos is _GRUPO_ESTRITO else r'\s*'
        padrao = '^' + sep.join(grupos[tipo] for _, tipo in rotulos) + '$'
        m = re.match(padrao, linha.strip(), re.IGNORECASE)
        if m:
            return {campo: m.group(i + 1) for i, (campo, _) in enumerate(rotulos)}
    return {}

# campos que só o modelo digital (AVM) traz - ficam vazios no físico
_CAMPOS_AMOSTRA_DIGITAL = {"cidade": "", "uf": ""}

def _campo_linha(bloco, padrao):
    """Procura `padrao` linha a linha no bloco; devolve os grupos (com ""
    no lugar de grupo que não casou) ou None se nenhuma linha casar."""
    for linha in bloco.split('\n'):
        m = re.search(padrao, linha, re.IGNORECASE)
        if m:
            return tuple(g or "" for g in m.groups())
    return None

def extrair_amostras_digital(text):
    """Amostras do modelo digital (AVM). Layout bem diferente do físico:
    blocos "Amostra n.0", "n.1"... numerados a partir de ZERO, com dois
    pares "chave: valor" por linha (confirmado em PDF real):

        Amostra n.0 Data03/09/2024
        Empreendimento: Distânçia até o avaliando (km)
        Edereço: Estrada de Itapecerica, 2736 IF 100
        Bairro: Vila Exemplo Cidade: São Paulo UF: SP
        Tipo: Apartamento Padrão de construção: Médio
        Estado de conservação: Bom Idade Aparente(anos): 38
        A. privativa/construida (m): 54 Elevador: Sim
        N.dormitórios/suites: 2/0 N.vagas: 1
        Valor total (R$): 280.000,00 Valor unitário (R$/m): 5.185,19
        Fonte: IMOBILIÁRIA EXEMPLO Tel: 11900000000 Oferta/Transação Oferta
        Apartamento com 2 Quartos e 1 banheiro à Venda, 54 m² por R$ 280.000

    ("Edereço" e "Distânçia" são erros de digitação do próprio laudo.)
    Não tem URL nem banheiros como campo próprio - banheiros sai da linha
    de descrição do anúncio, quando ela menciona.
    """
    amostras = []
    blocos = re.split(r'\n(?=Amostra\s+n\.\s*\d+\b)', text)
    for bloco in blocos:
        m_num = re.match(r'Amostra\s+n\.\s*(\d+)\b', bloco)
        if not m_num:
            continue
        # rodapé da página ("Página 2/3", "Controle Interno") não é amostra
        m_fim = re.search(r'\n\s*P[áa]gina\s+\d+\s*/\s*\d+', bloco)
        if m_fim:
            bloco = bloco[:m_fim.start()]

        dados = {
            "numero_amostra": converter_int_seguro(m_num.group(1)),
            "endereco": "",
            "tipo_imovel": "",
            "quartos": 0,
            "banheiros": 0,
            "vagas": 0,
            "area_privativa_m2": ZERO,
            "valor": ZERO,
            "valor_unitario_m2": ZERO,
            "idade_anos": 0,
            "padrao_acabamento": "",
            "estado_conservacao": "",
            "area_terreno_m2": ZERO,
            "url": "",
            **_CAMPOS_AMOSTRA_DIGITAL,
        }

        g = _campo_linha(bloco, r'Edere[çc]o:\s*(.*?)\s*(?:\bIF\b.*)?$')
        endereco = limpar_txt(g[0]) if g else ""
        g = _campo_linha(bloco, r'Bairro:\s*(.*?)\s*Cidade:\s*(.*?)\s*UF:\s*(\S*)')
        if g:
            bairro, dados["cidade"], dados["uf"] = (limpar_txt(x) for x in g)
            # mesmo formato do modelo físico: "Rua X, 42 , Bairro"
            if bairro:
                endereco = f"{endereco} , {bairro}" if endereco else bairro
        dados["endereco"] = endereco

        g = _campo_linha(bloco, r'^Tipo:\s*(.*?)\s*Padr[ãa]o\s+de\s+constru[çc][ãa]o:\s*(.*)$')
        if g:
            dados["tipo_imovel"], dados["padrao_acabamento"] = limpar_txt(g[0]), limpar_txt(g[1])

        g = _campo_linha(bloco, r'Estado\s+de\s+conserva[çc][ãa]o:\s*(.*?)\s*Idade\s+Aparente[^:]*:\s*(\S*)')
        if g:
            dados["estado_conservacao"] = limpar_txt(g[0])
            dados["idade_anos"] = converter_int_seguro(g[1])

        g = _campo_linha(bloco, r'A\.\s*privativa[^:]*:\s*(\S*)\s*Elevador:')
        if g:
            dados["area_privativa_m2"] = converter_float_seguro(g[0])

        # "N.dormitórios/suites: 2/0 N.vagas: 1" - o laudo de 2023 traz só
        # "N.dormitórios/suites: 1 N.vagas: 1", sem a barra; sem aceitar as
        # duas, quartos e vagas dessas amostras saíam 0
        g = _campo_linha(bloco, r'N\.\s*dormit[óo]rios/suites:\s*(\d*)\s*(?:/\s*\d*)?\s*N\.\s*vagas:\s*(\d*)')
        if g:
            dados["quartos"] = converter_int_seguro(g[0])
            dados["vagas"] = converter_int_seguro(g[1])

        g = _campo_linha(bloco, r'Valor\s+total[^:]*:\s*([\d\.,]*)\s*Valor\s+unit[áa]rio[^:]*:\s*([\d\.,]*)')
        if g:
            dados["valor"] = converter_float_seguro(g[0])
            dados["valor_unitario_m2"] = converter_float_seguro(g[1])

        # o digital não tem campo de banheiros - sai da descrição do anúncio
        # (a linha logo depois de "Fonte:"), quando ela menciona
        linhas = [l.strip() for l in bloco.split('\n') if l.strip()]
        for i, linha in enumerate(linhas):
            if re.match(r'^Fonte:', linha, re.IGNORECASE) and i + 1 < len(linhas):
                m_ban = re.search(r'(\d+)\s+banheiro', linhas[i + 1], re.IGNORECASE)
                if m_ban:
                    dados["banheiros"] = converter_int_seguro(m_ban.group(1))
                break

        if dados["valor_unitario_m2"] == ZERO and dados["valor"] > ZERO:
            dados["valor_unitario_m2"] = calcular_valor_unitario(dados["valor"], dados["area_privativa_m2"])

        amostras.append(dados)

    # o digital numera a partir de 0; renumera pra começar em 1 como o físico
    if amostras and min(a["numero_amostra"] for a in amostras) == 0:
        for a in amostras:
            a["numero_amostra"] += 1

    return amostras

def extrair_amostras(text):
    """Amostras (imóveis comparativos) usadas no cálculo do valor, uma por
    bloco "AMOSTRA N" no laudo. Devolve uma lista de dicts, um por amostra.

    Cada bloco é uma sequência de pares "linha de rótulos / linha de
    valores", e QUAIS rótulos aparecem muda com o tipo do imóvel:

        Apartamento:  Tipo de Imóvel | Qtd. Quartos | Qtd. Banheiros | Qtd. Vagas
                      Área privativa | Valor | Valor unitário (R$/m²)
                      Idade aparente | Padrão de acabamento | Estado de conservação
        Casa:         (igual, mas sem "Valor unitário") +
                      Área do terreno | Padrão terreno | Topografia
        Terreno:      Tipo de Imóvel | Valor
                      Área do terreno | Padrão terreno | Topografia

    Por isso a leitura não assume layout fixo: lê os rótulos da linha de
    cima (ROTULOS_AMOSTRA) e monta o padrão da linha de baixo a partir deles.

    Laudo do modelo digital (AVM) não tem esses blocos - cai em
    extrair_amostras_digital, que lê o formato "Amostra n.0".
    """
    if not re.search(r'\nAMOSTRA\s+\d+\b', text):
        return extrair_amostras_digital(text)

    # o que vem depois das amostras (avaliação, cálculo de depreciação...)
    # tem rótulos parecidos ("Idade", "Padrão") e contaminaria a última
    m_fim = re.search(r'\n\s*AVALIA[ÇC][ÃA]O DO IM[ÓO]VEL\b', text)
    if m_fim:
        text = text[:m_fim.start()]

    amostras = []
    blocos = re.split(r'\n(?=AMOSTRA\s+\d+\b)', text)
    for bloco in blocos:
        m_num = re.match(r'AMOSTRA\s+(\d+)\b', bloco)
        if not m_num:
            continue

        dados = {
            "numero_amostra": converter_int_seguro(m_num.group(1)),
            "endereco": "",
            "tipo_imovel": "",
            "quartos": 0,
            "banheiros": 0,
            "vagas": 0,
            "area_privativa_m2": ZERO,
            "valor": ZERO,
            "valor_unitario_m2": ZERO,
            "idade_anos": 0,
            "padrao_acabamento": "",
            "estado_conservacao": "",
            "area_terreno_m2": ZERO,
            "url": "",
            **_CAMPOS_AMOSTRA_DIGITAL,
        }

        linhas = [l.strip() for l in bloco.split('\n')]
        linhas = [l for l in linhas if l and not _RE_RODAPE_PAGINA.match(l)]

        i = 1  # linha 0 é o "AMOSTRA N R$ ..."
        while i < len(linhas):
            linha = linhas[i]
            proxima = linhas[i + 1] if i + 1 < len(linhas) else ""

            if re.match(r'^Endere[çc]o$', linha, re.IGNORECASE):
                dados["endereco"] = limpar_txt(proxima)
                i += 2
                continue

            if re.match(r'^URL$', linha, re.IGNORECASE):
                # a URL do anúncio costuma quebrar no meio, em duas linhas -
                # o PDF corta no hífen, então emenda as linhas seguintes (sem
                # espaço) enquanto o que já foi juntado terminar em "-".
                url = ""
                j = i + 1
                while j < len(linhas) and j <= i + 3:
                    if url and not url.endswith("-"):
                        break
                    url += linhas[j]
                    j += 1
                dados["url"] = url
                i = j
                continue

            rotulos = _ler_cabecalho_amostra(linha)
            if rotulos and proxima and not _ler_cabecalho_amostra(proxima):
                # (a linha de baixo só é de valores se ela mesma não for
                # outro cabeçalho - campo em branco deixa isso acontecer)
                valores = _ler_valores_amostra(proxima, rotulos)
                for campo, tipo in rotulos:
                    bruto = valores.get(campo)
                    # campo reconhecido só pra ler a linha (padrão do
                    # terreno, topografia) não tem lugar no dict
                    if bruto is None or campo not in dados:
                        continue
                    if tipo == "int":
                        dados[campo] = converter_int_seguro(bruto)
                    elif tipo in ("num", "money"):
                        dados[campo] = converter_float_seguro(bruto)
                    else:
                        dados[campo] = limpar_txt(bruto)
                # contagem de cômodo acima de 30 não existe em imóvel de
                # amostra - é número de outra coluna que entrou aqui
                for campo in ("quartos", "banheiros"):
                    if dados[campo] > 30:
                        dados[campo] = 0
                i += 2 if valores else 1
                continue

            i += 1

        # casa/terreno não trazem o valor por m² impresso - calcula pela
        # área privativa (ou do terreno, pra lote)
        if dados["valor_unitario_m2"] == ZERO and dados["valor"] > ZERO:
            base = dados["area_privativa_m2"] or dados["area_terreno_m2"]
            dados["valor_unitario_m2"] = calcular_valor_unitario(dados["valor"], base)

        amostras.append(dados)

    return amostras

def extrair_modelo_digital(text):
    cod_laudo = re.search(r'#(TA[NOP]\d+|\w+\d+)', text)
    num_proposta_val = extrair_numero_proposta(text)

    data_aval_val = extrair_data_avaliacao(text)

    endereco_val, num_val_fallback = extrair_endereco_numero(text)
    
    num_busca = re.search(r'N[úu]mero\s*[\n\r]+\s*([0-9a-zA-Z/]+)', text, re.IGNORECASE)
    num_val = num_busca.group(1).strip() if num_busca else num_val_fallback

    compl_val = extrair_complemento_generico(text)

    tipo_imovel = re.search(r'\b(M[úu]ltiplas\s+Unidades|Apartamento\s*Tipo|Apartamento|Casa|Sobrado|Terreno(?:\s*-\s*Lote)?)\b', text, re.IGNORECASE)
    tipo_imovel_val = tipo_imovel.group(1).strip() if tipo_imovel else "Apartamento"

    area_priv_match = re.search(r'(?:[Áá]rea\s+privativa[^\d]*|privativa[^\d]*)(\d{1,5}[,\.]?\d{0,2})', text, re.IGNORECASE)
    area_com_match = re.search(r'(?:[Áá]rea\s+comum[^\d]*|comum[^\d]*)(\d{1,5}[,\.]?\d{0,2})', text, re.IGNORECASE)

    area_priv = converter_float_seguro(area_priv_match.group(1) if area_priv_match else None)
    area_comum = converter_float_seguro(area_com_match.group(1) if area_com_match else None)

    # terreno não tem área privativa/comum de verdade - mas a regex de
    # "privativa" logo acima costuma achar a área do terreno mesmo assim
    # (o relatório reaproveita esse campo pra terrenos). Então aproveita
    # o que já foi extraído em area_priv como area_terreno, em vez de
    # jogar fora e depender só da busca alternativa (que quase nunca
    # bate - foi assim que 356 de 366 terrenos ficaram com área zerada).
    area_terreno = ZERO
    eh_terreno = "terreno" in tipo_imovel_val.lower()
    if eh_terreno:
        if area_priv > 0:
            area_terreno = area_priv
        else:
            match_terreno_area = re.search(
                r'(?:[Áá]rea\s+do\s+terreno|[Áá]rea\s+constru[íi]da)[^\d]*([\d\.,]+)',
                text, re.IGNORECASE
            )
            if match_terreno_area:
                area_terreno = converter_float_seguro(match_terreno_area.group(1))
        area_priv = ZERO
        area_comum = ZERO
        area_total = area_terreno
    else:
        area_total = (area_priv + area_comum).quantize(Decimal('0.01')) if area_priv > 0 else ZERO

    banheiros = re.search(r'Banheiro\s*Social:\s*(\d+)', text, re.IGNORECASE) or re.search(r'(\d+)\s*(?=banheiro)', text, re.IGNORECASE)
    quartos = re.search(r'Dormitóri[oa]s?:\s*(\d+)', text, re.IGNORECASE) or re.search(r'(\d+)\s*(?=quarto|dormit)', text, re.IGNORECASE)
    suites_match = re.search(r'(?:su[íi]te|semi\s*su[íi]te)[^\d]*(\d+)', text, re.IGNORECASE)
    
    vagas_match = re.search(r'Vagas?:\s*(\d+)', text, re.IGNORECASE) or re.search(r'(\d+)\s*(?=vaga)', text, re.IGNORECASE)
    vagas_val = converter_int_seguro(vagas_match.group(1) if vagas_match else 0)

    idade = re.search(r'(\d+)\s*anos', text, re.IGNORECASE)

    padrao_val = None
    termos_invalidos = ['imóvel', 'imovel', 'condomínio', 'condominio', 'de', 'do', 'da']
    matches_padrao = re.findall(r'Padr[ãa]o\s+(?:de\s+)?Acabamento\s*[\n\r]+\s*([A-Za-zÀ-ÿ\s/]+)', text, re.IGNORECASE)
    for m in matches_padrao:
        candidato = m.strip()
        if candidato.lower() not in termos_invalidos and len(candidato) > 1:
            padrao_val = candidato
            break

    if not padrao_val:
        busca_termo = re.search(r'\b(M[ée]dio|Alto|Baixo|Simples|Normal|Superior|Luxo|Standard)\b', text, re.IGNORECASE)
        if busca_termo:
            padrao_val = busca_termo.group(1).strip()

    estado_val = None
    estado_match = re.search(r'Estado\s+de\s+Conserva[çc][ãa]o[^\n:]*[:\n]?\s*([^\n]+)', text, re.IGNORECASE)
    if estado_match:
        bruto = estado_match.group(1).strip()
        limpo = re.sub(r'\s+\d+(\s+\d+)*$', '', bruto).strip()
        estado_val = limpo if limpo else bruto
    if not estado_val:
        busca_est = re.search(r'\b(Bom|Nova\s*\(até\s*5\s*anos\)|Nova\|Regular|Regular|Ótimo|Ruim)\b', text, re.IGNORECASE)
        if busca_est:
            estado_val = busca_est.group(1).strip()

    val_mercado_val = None
    val_venda_f_val = None

    val_mercado_match = re.search(r'VALOR\s+DE\s+MERCADO[^\d]*?R\$\s*([\d\.,]+)', text, re.IGNORECASE) or re.search(r'R\$\s*([\d\.,]+)', text)
    if val_mercado_match:
        val_mercado_val = val_mercado_match.group(1)

    val_venda_f_match = re.search(r'VENDA\s+FOR[ÇC]ADA[^\d]*?R\$\s*([\d\.,]+)', text, re.IGNORECASE)
    if val_venda_f_match:
        val_venda_f_val = val_venda_f_match.group(1)

    valor_mercado_dec = converter_float_seguro(val_mercado_val)
    area_para_unitario = area_terreno if eh_terreno else area_priv
    val_unit_dec = calcular_valor_unitario(valor_mercado_dec, area_para_unitario)

    coords_str, lat, lon = extrair_coordenadas_generico(text)

    return {
        "numero_proposta": num_proposta_val,
        "codigo_laudo": cod_laudo.group(1) if cod_laudo else None,
        "data_avaliacao": data_aval_val,
        "endereco": limpar_txt(endereco_val),
        "numero": limpar_txt(num_val, valor_padrao="S/N"),
        "complemento": compl_val,
        "tipo_imovel": tipo_imovel_val,
        "area_privativa_m2": area_priv,
        "area_comum_m2": area_comum,
        "area_total_m2": area_total,
        "area_terreno_m2": area_terreno,
        "quartos": converter_int_seguro(quartos.group(1) if quartos else 0),
        "suites": converter_int_seguro(suites_match.group(1) if suites_match else 0),
        "banheiros": converter_int_seguro(banheiros.group(1) if banheiros else 0),
        "vagas": vagas_val,
        "idade_anos": converter_int_seguro(idade.group(1) if idade else 0),
        "padrao_acabamento": padrao_val or "Normal",
        "estado_conservacao": estado_val or "Bom",
        "infraestrutura": extrair_infraestrutura(text),
        "infraestrutura_urbana": extrair_infraestrutura_urbana(text),
        "servicos_publicos": extrair_servicos_publicos(text),
        "valor_mercado": valor_mercado_dec,
        "valor_venda_forcada": converter_float_seguro(val_venda_f_val),
        "valor_unitario_m2": val_unit_dec,
        "coordenadas": coords_str,
        "latitude": lat,
        "longitude": lon
    }

def extrair_tipo_imovel_fisico(text):
    # ancora no campo real "01 - Tipo do Imóvel Avaliado" do questionário,
    # em vez de buscar a palavra solta no texto inteiro. Busca solta pega
    # "Terreno"/"Casa" de um cabeçalho de resumo (ex: "TERRENO ÁREA
    # CONSTRUÍDA", que é só título de coluna) ou de um imóvel comparativo
    # mais abaixo no PDF, classificando errado o imóvel avaliado de
    # verdade - foi assim que um imóvel "Misto" virou "Terreno" por engano
    # e teve a área apagada.
    secao = re.search(
        r'01\s*-\s*Tipo\s+do\s+Im[óo]vel\s+Avaliado[^\n]*\n+([^\n]+)',
        text, re.IGNORECASE
    )
    texto_busca = secao.group(1) if secao else text
    tipo_imovel = re.search(
        r'\b(M[úu]ltiplas\s+Unidades|Misto(?:\s*\([^)]*\))?|Apartamento|Casa|Sobrado|Terreno(?:\s*-\s*Lote)?)\b',
        texto_busca, re.IGNORECASE
    )
    return tipo_imovel.group(1).strip() if tipo_imovel else "Apartamento"

def extrair_unidades_multiplas(text):
    # laudos "Múltiplas Unidades" (avaliação de empreendimento inteiro,
    # crédito PJ) não têm um campo simples de área - a área e o valor de
    # cada unidade ficam numa tabela "UNIDADES IMOBILIÁRIAS", uma linha
    # por apartamento/sala. Soma a coluna "Área Cálculo" (área privativa
    # de cada unidade) e "Vl. Avaliação" pra ter a área e o valor do
    # empreendimento todo - validado em 3 laudos reais, a soma bate com
    # "N° Total de Unidades" e com o VALOR DE MERCADO do topo do laudo.
    m_header = re.search(r'Tipo Unid\.\s+N[ºo]\s+Unidade.*', text)
    if not m_header:
        return ZERO, ZERO
    resto = text[m_header.end():]

    # não tenta separar tipo/nº unidade/andar/bloco (larguras variáveis,
    # ex: "TORRE A" vs "A" vs "1") - ancora só nas duas colunas de área
    # (sempre decimal com vírgula) e no valor de avaliação da unidade
    padrao_linha = re.compile(
        r'^(.+?)\s+(\d+,\d+)\s+(\d+,\d+)\s+(\d+)\s+(\d+)\s+([\d.,]+)\s+R\$\s*([\d.,]+)\s+([\d.,]+)\s+R\$\s*([\d.,]+)\s*$'
    )
    area_total = ZERO
    valor_total = ZERO
    qtd = 0
    for linha in resto.split('\n'):
        m = padrao_linha.match(linha.strip())
        if not m:
            if qtd > 0:
                break  # tabela acabou
            continue  # ainda não chegou na primeira linha de dado
        area_total += converter_float_seguro(m.group(3))
        valor_total += converter_float_seguro(m.group(7))
        qtd += 1
    return area_total, valor_total

def extrair_modelo_fisico(text):
    cod_laudo = re.search(r'#(TAP\d+|\w+\d+)', text)
    num_proposta_val = extrair_numero_proposta(text)
    data_aval_val = extrair_data_avaliacao(text)

    endereco_bruto, num_val = extrair_endereco_numero(text)
    compl_val = extrair_complemento_generico(text)

    tipo_imovel_val = extrair_tipo_imovel_fisico(text)

    area_priv = ZERO
    match_18 = re.search(r'18\s*-\s*[ÁA]rea\s+Privativa[^\n]*\n+([^\n]+)', text, re.IGNORECASE)
    if match_18:
        # pdfplumber às vezes solta espaço no meio do número ("131. 7")
        linha_18 = match_18.group(1).strip()
        val_m = re.search(r'([\d\.,]+)$', re.sub(r'\s+', '', linha_18))
        if val_m:
            area_priv = converter_float_seguro(val_m.group(1))

    area_comum = ZERO
    match_19 = re.search(r'19\s*-\s*[ÁA]rea\s+Comum[^\n]*\n+([^\n]+)', text, re.IGNORECASE)
    if match_19:
        linha_19 = match_19.group(1).strip()
        val_m = re.search(r'^\s*([\d\.,]+)', linha_19)
        if val_m and not re.search(r'20\s*-', linha_19):
            area_comum = converter_float_seguro(val_m.group(1))

    area_terreno = ZERO
    eh_terreno = "terreno" in tipo_imovel_val.lower()
    if eh_terreno:
        # o questionário reaproveita o campo "18 - Área Privativa" pra
        # guardar a área do terreno em imóveis desse tipo (confirmado nos
        # PDFs de amostra) - então o valor que já foi extraído em
        # area_priv acima É a área do terreno, só precisa mudar de coluna.
        # Sem esse reaproveitamento, a área ficava zerada quase sempre:
        # a busca alternativa abaixo só rodava se area_priv já estivesse
        # zerado, o que raramente era o caso.
        if area_priv > 0:
            area_terreno = area_priv
        else:
            match_terreno = re.search(
                r'(?:[Áá]rea\s+do\s+terreno|[Áá]rea\s+constru[íi]da)[^\d]*([\d\.,]+)',
                text, re.IGNORECASE
            )
            if match_terreno:
                area_terreno = converter_float_seguro(match_terreno.group(1))
    elif area_priv == 0 and "casa" in tipo_imovel_val.lower():
        match_terreno = re.search(
            r'(?:[Áá]rea\s+do\s+terreno|[Áá]rea\s+constru[íi]da)[^\d]*([\d\.,]+)',
            text, re.IGNORECASE
        )
        if match_terreno:
            area_priv = converter_float_seguro(match_terreno.group(1))
    elif area_priv == 0 and "ltiplas" in tipo_imovel_val.lower():
        # "múltiplas"/"multiplas" - evita depender do acento vindo certo
        # do PDF
        area_priv, _valor_unidades_tabela = extrair_unidades_multiplas(text)

    if eh_terreno:
        area_priv = ZERO
        area_comum = ZERO
        area_total = area_terreno
    else:
        area_total = (area_priv + area_comum).quantize(Decimal('0.01'))

    # banheiros (11) e dormitórios (12) ficam na mesma linha - precisa
    # ler os dois juntos, senão os dois pegam o primeiro número
    banheiros_val = 0
    quartos = None
    match_11_12 = re.search(
        r'11\s*-\s*N[°º]?\s*de\s*Banheiros[^\n]*\n+(\d+)\s+(\d+)',
        text, re.IGNORECASE
    )
    if match_11_12:
        banheiros_val = converter_int_seguro(match_11_12.group(1))
        quartos = match_11_12.group(2)

    # mesma coisa em vagas cobertas (13) / descobertas (14)
    vagas_val = 0
    v13 = 0
    v14 = 0
    m_vagas_13_14 = re.search(
        r'13\s*-\s*N[°º]?\s*de\s*Vagas\s+Cobertas[^\n]*\n+(\d+)\s+(\d+)',
        text, re.IGNORECASE
    )
    if m_vagas_13_14:
        v13 = converter_int_seguro(m_vagas_13_14.group(1))
        v14 = converter_int_seguro(m_vagas_13_14.group(2))

    m_vagas_15 = re.search(r'15\s*-\s*N[°º]?\s*de\s*Vagas\s+Privativas[^\n]*\n+([0-9]+)', text, re.IGNORECASE)
    v15 = converter_int_seguro(m_vagas_15.group(1)) if m_vagas_15 else 0

    if v13 > 0:
        vagas_val = v13
    elif v14 > 0:
        vagas_val = v14
    elif v15 > 0:
        vagas_val = v15

    idade_val = 0
    match_idade = re.search(r'04\s*-\s*Idade\s+Aparente[^\n]*.*?\b([0-9]+)\b', text, re.IGNORECASE | re.DOTALL)
    if match_idade:
        idade_val = converter_int_seguro(match_idade.group(1))

    # limitado a 1-2 dígitos (nenhum imóvel tem 10+ suítes) - sem isso, uma
    # URL de anúncio comparativo tipo ".../1-suite-1234567890.html" fazia
    # pegar o ID do anúncio como se fosse a contagem de suítes
    suite_exata = re.search(r'^\s*suite\s+(\d{1,2})\b', text, re.IGNORECASE | re.MULTILINE) or re.search(r'\bsuite\b.*?\b(\d{1,2})\b', text, re.IGNORECASE)
    total_suites = converter_int_seguro(suite_exata.group(1) if suite_exata else 0)

    estado_cons = None
    match_est = re.search(r'06\s*-\s*Estado\s+de\s+Conserva[çc][ãa]o[^\n]*\n+([A-Za-zÀ-ÿ\s]+)', text, re.IGNORECASE)
    if match_est:
        val = match_est.group(1).strip()
        if val.lower() not in ['do', 'imóvel', 'imovel', 'de']:
            estado_cons = re.sub(r'\s+\d+(\s+\d+)*$', '', val).strip()

    padrao_acab = None
    match_pad = re.search(r'07\s*-\s*Padr[ãa]o\s+de\s+Acabamento[^\n]*\n+([A-Za-zÀ-ÿ\s/]+)', text, re.IGNORECASE)
    if match_pad:
        val = match_pad.group(1).strip()
        val_limpo = re.sub(r'\b(Residencial|Comercial|Industrial)\b', '', val, flags=re.IGNORECASE).strip()
        palavras = [p for p in val_limpo.split() if p.lower() not in ['do', 'imóvel', 'imovel', 'de']]
        padrao_acab = palavras[0] if palavras else None

    val_mercado_val = None
    val_venda_f_val = None

    val_mercado_match = re.search(r'VALOR\s+DE\s+MERCADO.*?R\$\s*([\d\.,]+)', text, re.IGNORECASE | re.DOTALL)
    if val_mercado_match:
        val_mercado_val = val_mercado_match.group(1)

    val_venda_f_match = re.search(r'VALOR\s+DE\s+VENDA\s+FOR[ÇC]ADA.*?R\$\s*([\d\.,]+)', text, re.IGNORECASE | re.DOTALL)
    if val_venda_f_match:
        val_venda_f_val = val_venda_f_match.group(1)

    valor_mercado_dec = converter_float_seguro(val_mercado_val)
    area_para_unitario = area_terreno if eh_terreno else area_priv
    val_unit_dec = calcular_valor_unitario(valor_mercado_dec, area_para_unitario)

    coords_str, lat, lon = extrair_coordenadas_generico(text)

    return {
        "numero_proposta": num_proposta_val,
        "codigo_laudo": cod_laudo.group(1) if cod_laudo else None,
        "data_avaliacao": data_aval_val,
        "endereco": limpar_txt(endereco_bruto),
        "numero": num_val,
        "complemento": compl_val,
        "tipo_imovel": tipo_imovel_val,
        "area_privativa_m2": area_priv,
        "area_comum_m2": area_comum,
        "area_total_m2": area_total,
        "area_terreno_m2": area_terreno,
        "quartos": converter_int_seguro(quartos if quartos else 0),
        "suites": total_suites,
        "banheiros": banheiros_val,
        "vagas": vagas_val,
        "idade_anos": idade_val,
        "padrao_acabamento": padrao_acab or "Normal",
        "estado_conservacao": estado_cons or "Bom",
        "infraestrutura": extrair_infraestrutura(text),
        "infraestrutura_urbana": extrair_infraestrutura_urbana(text),
        "servicos_publicos": extrair_servicos_publicos(text),
        "valor_mercado": valor_mercado_dec,
        "valor_venda_forcada": converter_float_seguro(val_venda_f_val),
        "valor_unitario_m2": val_unit_dec,
        "coordenadas": coords_str,
        "latitude": lat,
        "longitude": lon
    }

# ---------------------------------------------------------------------------
# Layout Plataforma (laudos atuais, físico e digital)
# ---------------------------------------------------------------------------
# As páginas do Plataforma são grades de "rótulo em cima / valor embaixo",
# em 2 a 4 colunas. Lidas como texto corrido as colunas se misturam
# ("RUA CASTANHAL 232 LOTE 164 QUADRA 13" é endereço + número + complemento
# na mesma linha), e um regex solto no texto inteiro acha o número errado
# (a proposta virava o CREA do avaliador, o valor de mercado virava o R$ de
# uma amostra). Então aqui a leitura é por posição: cada valor pertence à
# coluna cujo rótulo começa no mesmo x.

MESES_EXTENSO = {
    "janeiro": 1, "fevereiro": 2, "março": 3, "marco": 3, "abril": 4, "maio": 5,
    "junho": 6, "julho": 7, "agosto": 8, "setembro": 9, "outubro": 10,
    "novembro": 11, "dezembro": 12,
}

def _linhas_por_posicao(pdf):
    """Todas as linhas do PDF como listas de palavras (com x0), na ordem das
    páginas. Uma linha = palavras com o mesmo `top` (arredondado)."""
    linhas = []
    for page in pdf.pages:
        por_top = {}
        for w in page.extract_words():
            por_top.setdefault(round(w["top"]), []).append(w)
        for top in sorted(por_top):
            linhas.append(sorted(por_top[top], key=lambda w: w["x0"]))
    return linhas

def _texto_linha(palavras):
    return re.sub(r'\s+', ' ', " ".join(w["text"] for w in palavras)).strip()

def _ler_grade(linhas, rotulos, a_partir=0):
    """Acha a linha de cabeçalho cujo texto é exatamente os `rotulos` (em
    sequência) e devolve os valores da linha de baixo, um por rótulo, pela
    posição x de cada coluna. Também devolve o índice da linha achada, pra
    leituras seguintes começarem dali (rótulos como "03 - Área" se repetem
    em seções diferentes)."""
    alvo = re.sub(r'\s+', ' ', " ".join(rotulos)).strip().lower()
    for i in range(a_partir, len(linhas)):
        if _texto_linha(linhas[i]).lower() != alvo:
            continue
        # x0 de cada coluna = x0 da primeira palavra de cada rótulo
        colunas_x = []
        k = 0
        for rotulo in rotulos:
            colunas_x.append(linhas[i][k]["x0"])
            k += len(rotulo.split())
        valores = [[] for _ in rotulos]
        if i + 1 < len(linhas):
            proxima = linhas[i + 1]
            # linha seguinte já é outro cabeçalho -> todos os valores em branco
            if not re.match(r'^\d{2} - ', _texto_linha(proxima)):
                for w in proxima:
                    idx = 0
                    for c, x in enumerate(colunas_x):
                        if w["x0"] + 2 >= x:
                            idx = c
                    valores[idx].append(w["text"])
        return [" ".join(v).strip() for v in valores], i
    return None, None

def _grade(linhas, *alternativas, a_partir=0):
    """Tenta cada conjunto de rótulos na ordem e devolve o primeiro que
    casar. O laudo mudou de layout ao longo do tempo - "N° do Pedido"
    virou "N° da Proposta", a coluna IPTU sumiu - então cada grade tem
    mais de uma versão."""
    for rotulos in alternativas:
        valores, _ = _ler_grade(linhas, rotulos, a_partir)
        if valores is not None:
            return valores
    return [""] * len(alternativas[0])

def _valor_apos_titulo(text, *titulos):
    """Valor que vem na linha logo abaixo de um título do RESUMO
    ("VALOR DE VENDA FORÇADA\\nR$ 87.000,00")."""
    # o título tem que ser a linha inteira - "Valor unitário (R$/m²)" também
    # aparece no fim do cabeçalho das amostras, e não é esse
    for titulo in titulos:
        m = re.search(r'^' + titulo + r'\s*\n\s*([^\n]+)', text,
                      re.IGNORECASE | re.MULTILINE)
        if m:
            return m.group(1).strip()
    return ""

def _data_extenso(text):
    """"São Paulo, Sexta-feira, 6 de Setembro de 2024" -> "06/09/2024"."""
    m = re.search(r'(\d{1,2})\s+de\s+([A-Za-zçÇ]+)\s+de\s+(\d{4})', text)
    if not m:
        return None
    mes = MESES_EXTENSO.get(m.group(2).lower())
    return f"{int(m.group(1)):02d}/{mes:02d}/{m.group(3)}" if mes else None

def _laudo_vazio():
    """Todos os campos da tabela laudos (o INSERT usa as chaves do dict,
    então todo modelo tem que devolver exatamente estas)."""
    return {
        "numero_proposta": "", "codigo_laudo": None, "data_avaliacao": None,
        "endereco": "", "numero": "S/N", "complemento": "",
        "bairro": "", "municipio": "", "uf": "", "cep": "", "matricula": "",
        "tipo_imovel": "", "metodologia": "",
        "area_privativa_m2": ZERO, "area_comum_m2": ZERO, "area_total_m2": ZERO,
        "area_terreno_m2": ZERO,
        "quartos": 0, "suites": 0, "banheiros": 0, "vagas": 0, "idade_anos": 0,
        "padrao_acabamento": "", "estado_conservacao": "",
        "infraestrutura": "", "infraestrutura_urbana": "", "servicos_publicos": "",
        "valor_mercado": ZERO, "valor_venda_forcada": ZERO, "valor_unitario_m2": ZERO,
        "coordenadas": "", "latitude": None, "longitude": None,
    }

def extrair_cabecalho_plataforma(linhas, text):
    """Página 1 do Plataforma, igual nos dois modelos: DADOS DO PEDIDO,
    DADOS DO IMÓVEL e RESUMO (metodologia, áreas, valores)."""
    d = _laudo_vazio()

    m = re.search(r'#\s*([A-Z]{2,4}\d+)', text)
    d["codigo_laudo"] = m.group(1) if m else None

    # laudo novo chama de "N° da Proposta" o que o antigo chamava de
    # "N° do Pedido" - sem a alternativa, 6.571 laudos ficaram sem proposta
    _, pedido, data_sol = _grade(
        linhas,
        ["Solicitante", "N° do Pedido", "Data Solicitação"],
        ["Solicitante", "N° da Proposta", "Data Solicitação"])
    d["numero_proposta"] = pedido

    # o laudo de 2023 traz o CEP na linha do endereço e só Bairro/
    # Municipio/UF na de baixo; de 2024 em diante o CEP foi pra linha do
    # bairro. Sem a versão de 2023, endereço, bairro, município, UF e CEP
    # saíam todos vazios (99,8% dos laudos de out-dez/2023)
    valores, _ = _ler_grade(linhas, ["Endereço", "Número", "Complemento", "CEP"])
    if valores is not None:
        endereco, numero, compl, cep = valores
        bairro, municipio, uf = _grade(linhas, ["Bairro", "Municipio", "UF"])
    else:
        endereco, numero, compl = _grade(linhas, ["Endereço", "Número", "Complemento"])
        bairro, municipio, uf, cep = _grade(linhas, ["Bairro", "Municipio", "UF", "CEP"])
    d["endereco"] = limpar_txt(endereco)
    d["numero"] = limpar_txt(numero, valor_padrao="S/N")
    d["complemento"] = limpar_txt(compl)

    d["bairro"], d["municipio"], d["uf"], d["cep"] = (limpar_txt(x) for x in (bairro, municipio, uf, cep))

    # o laudo novo não tem mais a coluna IPTU nessa linha
    valores = _grade(
        linhas,
        ["Tipo do imóvel", "Matrícula", "Núm. Registro de Imóveis", "IPTU"],
        ["Tipo do imóvel", "Matrícula", "Núm. Registro de Imóveis"])
    d["tipo_imovel"] = limpar_txt(valores[0])
    d["matricula"] = limpar_txt(valores[1])

    d["metodologia"] = limpar_txt(_valor_apos_titulo(text, r'METODOLOGIA APLICADA'))
    # o laudo novo voltou a imprimir "VALOR DE MERCADO" na capa
    d["valor_mercado"] = converter_float_seguro(_valor_apos_titulo(
        text, r'VALOR DE AVALIA[ÇC][ÃA]O PARA EFEITO DE GARANTIA', r'VALOR DE MERCADO'))
    d["valor_venda_forcada"] = converter_float_seguro(_valor_apos_titulo(text, r'VALOR DE VENDA FOR[ÇC]ADA'))
    d["valor_unitario_m2"] = converter_float_seguro(_valor_apos_titulo(text, r'VALOR UNIT[ÁA]RIO \(R\$/m²\)'))

    # áreas do RESUMO: uma linha de títulos ("TERRENO ÁREA CONSTRUÍDA") e a
    # de baixo com os m² na mesma ordem
    m = re.search(r'\n((?:(?:TERRENO|[ÁA]REA PRIVATIVA|[ÁA]REA CONSTRU[ÍI]DA)\s*)+)\n([^\n]*m²[^\n]*)', text)
    if m:
        titulos = re.findall(r'TERRENO|[ÁA]REA PRIVATIVA|[ÁA]REA CONSTRU[ÍI]DA', m.group(1))
        valores = re.findall(r'[\d\.,]+\s*m²', m.group(2))
        for titulo, valor in zip(titulos, valores):
            if titulo == "TERRENO":
                d["area_terreno_m2"] = converter_area_plataforma(valor)
            elif "PRIVATIVA" in titulo or d["area_privativa_m2"] == ZERO:
                # "ÁREA CONSTRUÍDA" (casa, comercial) faz as vezes da
                # privativa; o questionário do físico sobrescreve depois
                d["area_privativa_m2"] = converter_area_plataforma(valor)

    # data: a da vistoria (carimbo das fotos), senão a de emissão do laudo,
    # senão a da solicitação
    m = re.search(r'RELAT[ÓO]RIO\s+FOTOGR[ÁA]FICO[^\n]*\n+\s*(\d{2}/\d{2}/\d{4})', text, re.IGNORECASE)
    d["data_avaliacao"] = (m.group(1) if m else None) or _data_extenso(text) or (data_sol or None)

    d["coordenadas"], d["latitude"], d["longitude"] = extrair_coordenadas_generico(text)
    d["infraestrutura"] = extrair_infraestrutura(text)
    return d

def extrair_plataforma_fisico(linhas, text):
    """Modelo físico: vistoria presencial, campos numerados na página
    "VISTORIA DO IMÓVEL" (01 a 24) e seção TERRENO pra casa/lote."""
    d = extrair_cabecalho_plataforma(linhas, text)

    tipo, _ = _grade(linhas, ["01 - Tipo do Imóvel Avaliado", "02 - Tipo de Implantação"])
    if tipo:
        d["tipo_imovel"] = limpar_txt(tipo)  # mais específico que o da capa
    _, idade = _grade(linhas, ["03 - Indício de Ocupação do Imóvel", "04 - Idade Aparente do Imóvel (em anos)"])
    d["idade_anos"] = converter_int_seguro(idade)
    _, estado = _grade(linhas, ["05 - Ano Construção", "06 - Estado de Conservação do Imóvel"])
    d["estado_conservacao"] = limpar_txt(estado)
    padrao, _ = _grade(linhas, ["07 - Padrão de Acabamento do Imóvel", "08 - Uso do Imóvel"])
    d["padrao_acabamento"] = limpar_txt(padrao)
    banheiros, quartos = _grade(linhas, ["11 - N° de Banheiros", "12 - N° de Dormitórios"])
    d["banheiros"] = converter_int_seguro(banheiros)
    d["quartos"] = converter_int_seguro(quartos)
    cobertas, descobertas = _grade(linhas, ["13 - N° de Vagas Cobertas", "14 - N° de Vagas Descobertas"])
    privativas, _ = _grade(linhas, ["15 - N° de Vagas Privativas", "16 - Fachada Principal"])
    d["vagas"] = converter_int_seguro(cobertas) + converter_int_seguro(descobertas)
    if d["vagas"] == 0:
        d["vagas"] = converter_int_seguro(privativas)
    _, area_priv = _grade(linhas, ["17 - Esquadrias", "18 - Área Privativa (em m²)"])
    area_comum, area_total = _grade(linhas, ["19 - Área Comum (em m²)", "20 - Área Total (em m²)"])
    if area_priv:
        d["area_privativa_m2"] = converter_area_plataforma(area_priv)
    d["area_comum_m2"] = converter_area_plataforma(area_comum)
    d["area_total_m2"] = converter_area_plataforma(area_total) or \
        (d["area_privativa_m2"] + d["area_comum_m2"]).quantize(Decimal('0.01'))

    # suítes: linha "Suíte N ..." da tabela "24 - Cômodos"
    m = re.search(r'24 - C[ôo]modos\n[^\n]*\n((?:[^\n]+\n)+?)(?=[A-ZÇÃÕÉÍ ]{6,}\n|\Z)', text)
    if m:
        m_suite = re.search(r'^Su[íi]tes?\s+(\d+)', m.group(1), re.IGNORECASE | re.MULTILINE)
        d["suites"] = converter_int_seguro(m_suite.group(1)) if m_suite else 0

    # área do terreno: RESUMO da capa já traz; se não, a seção TERRENO
    if d["area_terreno_m2"] == ZERO:
        _, i_terreno = _ler_grade(linhas, ["01 - Topografia", "02 - Formato"])
        if i_terreno is not None:
            area_terreno, _ = _grade(linhas, ["03 - Área (em m²)", "04 - Testada/Frente (em metros)"],
                                     a_partir=i_terreno)
            d["area_terreno_m2"] = converter_area_plataforma(area_terreno)

    if "terreno" in d["tipo_imovel"].lower() and d["area_privativa_m2"] == ZERO:
        d["area_total_m2"] = d["area_terreno_m2"]

    d["infraestrutura_urbana"] = extrair_infraestrutura_urbana(text)
    d["servicos_publicos"] = extrair_servicos_publicos(text)
    _completar_valor_unitario(d)
    return d

def _completar_valor_unitario(d):
    """Capa sem "VALOR UNITÁRIO" (comparativo direto de apartamento, AVM
    com mais de uma matrícula...): calcula valor / área privativa, ou / área
    do terreno em lote."""
    if d["valor_unitario_m2"] == ZERO and d["valor_mercado"] > ZERO:
        base = d["area_privativa_m2"] or d["area_terreno_m2"]
        d["valor_unitario_m2"] = calcular_valor_unitario(d["valor_mercado"], base)

def extrair_plataforma_digital(linhas, text):
    """Modelo digital (AVM): sem vistoria - os dados do imóvel vêm em três
    linhas extras da própria capa."""
    d = extrair_cabecalho_plataforma(linhas, text)

    _, area_priv, idade, padrao = _grade(
        linhas, ["Tipo de Implantação", "área privativa", "Idade do imóvel", "Padrão Acabamento"])
    estado, banheiros, quartos, vagas = _grade(
        linhas, ["Estado de Conservação Imóvel", "Quantidade de banheiros",
                 "Quantidade de quartos", "Quantidade de vagas"])
    _, area_comum = _grade(linhas, ["Estado de Conservação Condomínio", "Área Comum"])

    if area_priv:
        d["area_privativa_m2"] = converter_area_plataforma(area_priv)
    d["area_comum_m2"] = converter_area_plataforma(area_comum)
    d["area_total_m2"] = (d["area_privativa_m2"] + d["area_comum_m2"]).quantize(Decimal('0.01'))
    d["idade_anos"] = converter_int_seguro(idade)
    d["padrao_acabamento"] = limpar_txt(padrao)
    d["estado_conservacao"] = limpar_txt(estado)
    d["banheiros"] = converter_int_seguro(banheiros)
    d["quartos"] = converter_int_seguro(quartos)
    d["vagas"] = converter_int_seguro(vagas)
    m = re.search(r'Su[íi]te:\s*(\d+)', text)
    d["suites"] = converter_int_seguro(m.group(1)) if m else 0
    _completar_valor_unitario(d)
    return d

def _completar_campos(dados):
    """Laudo do layout antigo (extrair_modelo_*): garante as mesmas chaves
    dos novos, senão o INSERT quebra."""
    base = _laudo_vazio()
    base.update(dados)
    return base

def extrair_dados_pdf(pdf_path):
    # Roda dentro de um worker separado (ProcessPoolExecutor) - devolve o
    # resultado pro processo principal em vez de imprimir aqui, porque um
    # print() dentro do worker não passa pelo log em arquivo do processo
    # principal.
    file_name = os.path.basename(pdf_path)
    try:
        # salva a foto da fachada e uma de cada cômodo junto (data/imagens/)
        # - num try próprio pra que problema em imagem nunca derrube a
        # extração dos dados.
        imagens_verificadas = False
        try:
            imagens_verificadas = extrair_imagens_do_laudo(pdf_path)
        except Exception as e:
            print(f"[ERRO IMAGEM] {file_name}: {str(e)}")

        with pdfplumber.open(pdf_path) as pdf:
            full_text = ""
            for page in pdf.pages:
                full_text += (page.extract_text() or "") + "\n"

            if not full_text.strip():
                return {"status": "vazio", "path": file_name}

            # Layout Plataforma (todos os laudos atuais): físico tem o
            # questionário da vistoria, digital (AVM) não. Ancora no campo
            # "01 - Tipo do Imóvel Avaliado" e não no título da seção, que
            # já se chamou "VISTORIA DO IMÓVEL" e hoje é "QUESTIONÁRIO" -
            # olhar o título classificou 3.856 laudos físicos como digitais.
            # Laudo que não é Plataforma cai na leitura antiga por texto.
            if "DADOS DO PEDIDO" in full_text and re.search(
                    r'01\s*-\s*Tipo\s+do\s+Im[óo]vel\s+Avaliado', full_text, re.IGNORECASE):
                linhas = _linhas_por_posicao(pdf)
                dados = extrair_plataforma_fisico(linhas, full_text)
                dados["modelo_usado"] = "fisico"
            elif "DADOS DO PEDIDO" in full_text:
                linhas = _linhas_por_posicao(pdf)
                dados = extrair_plataforma_digital(linhas, full_text)
                dados["modelo_usado"] = "digital"
                # no digital esses dois blocos são checkboxes em imagem, não texto
                try:
                    dados["infraestrutura_urbana"], dados["servicos_publicos"] = \
                        extrair_melhoramentos_digital(pdf_path)
                except Exception as e:
                    print(f"[ERRO CHECKBOX] {file_name}: {e}")
            elif "Comparativo direto" in full_text or "QUESTIONARIO" in full_text or "QUESTIONÁRIO" in full_text:
                dados = _completar_campos(extrair_modelo_fisico(full_text))
                dados["modelo_usado"] = "fisico"
            else:
                dados = _completar_campos(extrair_modelo_digital(full_text))
                dados["modelo_usado"] = "digital"

            dados["path"] = file_name
            # "LAUDO DE INSPEÇÃO" (crédito PJ, "Laudão" do empreendimento, 60+
            # páginas, sem valor nem amostras) não é laudo de avaliação de
            # unidade: fica fora do banco como o "Múltiplas Unidades". A chave
            # só existe nesses, que são tirados antes de montar o INSERT
            if re.match(r"\s*LAUDO DE INSPE[ÇC][ÃA]O", full_text, re.IGNORECASE):
                dados["_documento"] = "inspecao"
            # "laudo" = coordenada impressa no PDF. Sem ela fica vazio, e o
            # banco_b_geocodificar.py calcula pelo endereço depois
            # ("calculada_numero", "calculada_rua"... - ver lá)
            dados["origem_coordenada"] = "laudo" if dados.get("latitude") is not None else None

            # amostras vão pra outra tabela (laudos_amostras), uma linha por
            # imóvel comparativo - por isso saem separadas de `dados`.
            amostras = extrair_amostras(full_text)
            for amostra in amostras:
                amostra["codigo_laudo"] = dados["codigo_laudo"]
                amostra["path"] = file_name

            return {"status": "ok", "dados": dados, "amostras": amostras,
                    "imagens_verificadas": imagens_verificadas}
    except Exception as e:
        return {"status": "erro", "path": file_name, "mensagem": str(e)}

def gravar_amostras(cursor, amostras):
    """Grava as amostras em laudos_amostras. Upsert por (codigo_laudo,
    numero_amostra), então reprocessar o mesmo laudo atualiza em vez de
    duplicar - mesma lógica já usada na tabela laudos."""
    if not amostras:
        return

    colunas = [
        "codigo_laudo", "numero_amostra", "endereco", "tipo_imovel",
        "quartos", "banheiros", "vagas", "area_privativa_m2", "valor",
        "valor_unitario_m2", "idade_anos", "padrao_acabamento",
        "estado_conservacao", "area_terreno_m2", "cidade", "uf", "url", "path",
    ]

    # amostra sem codigo_laudo não tem como ser deduplicada depois (o índice
    # único ignora NULL), então seria regravada a cada execução - fica de fora.
    com_codigo = [a for a in amostras if a.get("codigo_laudo")]
    descartadas = len(amostras) - len(com_codigo)
    if descartadas:
        print(f"[AVISO] {descartadas} amostra(s) sem código de laudo - não gravadas.")
    if not com_codigo:
        return

    # um único INSERT não pode atualizar a mesma linha duas vezes: dedup por
    # (codigo_laudo, numero_amostra) antes, mantendo a última
    por_chave = {(a["codigo_laudo"], a["numero_amostra"]): a for a in com_codigo}

    set_clause = ",\n            ".join(
        f"{col} = EXCLUDED.{col}" for col in colunas
        if col not in ("codigo_laudo", "numero_amostra")
    )
    query = f"""
        INSERT INTO laudos_amostras ({', '.join(colunas)})
        VALUES %s
        ON CONFLICT (codigo_laudo, numero_amostra) WHERE codigo_laudo IS NOT NULL
        DO UPDATE SET
            {set_clause};
    """
    valores = [[a[col] for col in colunas] for a in por_chave.values()]
    execute_values(cursor, query, valores, page_size=500)
    print(f"[SUCESSO] {len(valores)} amostra(s) gravada(s) na tabela laudos_amostras.")

# Laudo de empreendimento inteiro (crédito PJ, tipo "Múltiplas Unidades" -
# ex.: TAM6785, 3 torres, R$ 712 milhões) não é avaliação de uma unidade:
# sem área nem amostras, e o valor distorce qualquer média. Não é gravado
# no banco (pedido do usuário, 24/09/2026). Crédito PJ de UMA unidade entra
# normal - o critério é o tipo, não o PJ.
ARQUIVO_FORA_DO_BANCO = os.path.join("data", "laudos", "_fora_do_banco.json")
_PADRAO_FORA_DO_BANCO = re.compile(r"m[úu]ltiplas\s+unidades", re.IGNORECASE)


def fica_fora_do_banco(dados):
    return dados.get("_documento") == "inspecao" or         bool(_PADRAO_FORA_DO_BANCO.search(str(dados.get("tipo_imovel") or "")))


def _carregar_fora_do_banco():
    try:
        with open(ARQUIVO_FORA_DO_BANCO, encoding="utf-8") as f:
            dados = json.load(f)
        return dados if isinstance(dados, dict) else {}
    except (OSError, ValueError):
        return {}


def _salvar_fora_do_banco(fora):
    os.makedirs(os.path.dirname(ARQUIVO_FORA_DO_BANCO), exist_ok=True)
    temporario = ARQUIVO_FORA_DO_BANCO + ".tmp"
    with open(temporario, "w", encoding="utf-8") as f:
        json.dump(fora, f, ensure_ascii=False, indent=0)
    os.replace(temporario, ARQUIVO_FORA_DO_BANCO)


def processar_em_lote():
    folder_path = r"data/laudos"
    if not os.path.exists(folder_path):
        folder_path = "."

    todos_pdfs = [f for f in os.listdir(folder_path) if f.endswith('.pdf')]
    print(f"Total de PDFs encontrados: {len(todos_pdfs)}")

    if not todos_pdfs:
        return

    host_pg = os.getenv("PGURL", "127.0.0.1")
    dbname = os.getenv("PGNAME", "testdb")
    user = os.getenv("PGUSR", "postgres")
    password = os.getenv("PGPASS", "postgres")
    port = os.getenv("PGPORT", "5432")

    def conectar():
        return psycopg2.connect(host=host_pg, dbname=dbname, user=user, password=password, port=port)

    try:
        conn = conectar()
    except Exception as e:
        print(f"[ERRO CARGA BANCO]: {str(e)}")
        return

    with conn:
        with conn.cursor() as cursor:
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS laudos (
                    id SERIAL PRIMARY KEY,
                    numero_proposta TEXT,
                    codigo_laudo TEXT,
                    data_avaliacao TEXT,
                    endereco TEXT,
                    numero TEXT,
                    complemento TEXT,
                    tipo_imovel TEXT,
                    area_privativa_m2 NUMERIC,
                    area_comum_m2 NUMERIC,
                    area_total_m2 NUMERIC,
                    area_terreno_m2 NUMERIC,
                    quartos INTEGER,
                    suites INTEGER,
                    banheiros INTEGER,
                    vagas INTEGER,
                    idade_anos INTEGER,
                    padrao_acabamento TEXT,
                    estado_conservacao TEXT,
                    valor_mercado NUMERIC,
                    valor_venda_forcada NUMERIC,
                    valor_unitario_m2 NUMERIC,
                    coordenadas TEXT,
                    origem_coordenada TEXT,
                    latitude DOUBLE PRECISION,
                    longitude DOUBLE PRECISION,
                    path TEXT,
                    modelo_usado TEXT
                );
            """)
            cursor.execute("ALTER TABLE laudos ADD COLUMN IF NOT EXISTS modelo_usado TEXT;")
            cursor.execute("ALTER TABLE laudos ADD COLUMN IF NOT EXISTS area_terreno_m2 NUMERIC;")
            cursor.execute("ALTER TABLE laudos ADD COLUMN IF NOT EXISTS infraestrutura TEXT;")
            cursor.execute("ALTER TABLE laudos ADD COLUMN IF NOT EXISTS infraestrutura_urbana TEXT;")
            for coluna in ("bairro", "municipio", "uf", "cep", "matricula", "metodologia"):
                cursor.execute(f"ALTER TABLE laudos ADD COLUMN IF NOT EXISTS {coluna} TEXT;")
            cursor.execute("ALTER TABLE laudos ADD COLUMN IF NOT EXISTS servicos_publicos TEXT;")
            cursor.execute("ALTER TABLE laudos ADD COLUMN IF NOT EXISTS origem_coordenada TEXT;")
            cursor.execute("ALTER TABLE laudos DROP CONSTRAINT IF EXISTS laudos_path_key;")
            cursor.execute("DROP INDEX IF EXISTS laudos_numero_proposta_key;")
            cursor.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS laudos_codigo_laudo_key
                ON laudos (codigo_laudo) WHERE codigo_laudo IS NOT NULL;
            """)

            # uma linha por imóvel comparativo usado no cálculo do valor.
            # Ligada a laudos.codigo_laudo (sem FK formal porque o laudo pode
            # ser regravado/atualizado sem que as amostras mudem).
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS laudos_amostras (
                    id SERIAL PRIMARY KEY,
                    codigo_laudo TEXT,
                    numero_amostra INTEGER,
                    endereco TEXT,
                    tipo_imovel TEXT,
                    quartos INTEGER,
                    banheiros INTEGER,
                    vagas INTEGER,
                    area_privativa_m2 NUMERIC,
                    valor NUMERIC,
                    valor_unitario_m2 NUMERIC,
                    idade_anos INTEGER,
                    padrao_acabamento TEXT,
                    estado_conservacao TEXT,
                    area_terreno_m2 NUMERIC,
                    cidade TEXT,
                    uf TEXT,
                    url TEXT,
                    path TEXT
                );
            """)
            # bancos criados antes dessas colunas existirem (casa/terreno e
            # campos do modelo digital)
            for coluna, tipo in (("area_terreno_m2", "NUMERIC"),
                                 ("cidade", "TEXT"), ("uf", "TEXT")):
                cursor.execute(
                    f"ALTER TABLE laudos_amostras ADD COLUMN IF NOT EXISTS {coluna} {tipo};"
                )
            # colunas que existiram e foram tiradas por não serem usadas
            for coluna in ("padrao_terreno", "topografia", "suites", "elevador",
                           "fonte", "oferta_transacao", "descricao"):
                cursor.execute(f"ALTER TABLE laudos_amostras DROP COLUMN IF EXISTS {coluna};")
            cursor.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS laudos_amostras_codigo_numero_key
                ON laudos_amostras (codigo_laudo, numero_amostra)
                WHERE codigo_laudo IS NOT NULL;
            """)

            # bancos antigos ficaram com essas colunas como double
            # precision em vez de NUMERIC, o que reintroduz sobra de
            # binário mesmo já mandando Decimal - migra se ainda estiver assim
            cursor.execute("""
                DO $$
                BEGIN
                    IF EXISTS (
                        SELECT 1 FROM information_schema.columns
                        WHERE table_name = 'laudos'
                          AND column_name = 'valor_unitario_m2'
                          AND data_type = 'double precision'
                    ) THEN
                        ALTER TABLE laudos
                            ALTER COLUMN valor_unitario_m2   TYPE NUMERIC USING ROUND(valor_unitario_m2::numeric, 2),
                            ALTER COLUMN valor_mercado       TYPE NUMERIC USING ROUND(valor_mercado::numeric, 2),
                            ALTER COLUMN valor_venda_forcada TYPE NUMERIC USING ROUND(valor_venda_forcada::numeric, 2),
                            ALTER COLUMN area_privativa_m2   TYPE NUMERIC USING ROUND(area_privativa_m2::numeric, 2),
                            ALTER COLUMN area_comum_m2       TYPE NUMERIC USING ROUND(area_comum_m2::numeric, 2),
                            ALTER COLUMN area_total_m2       TYPE NUMERIC USING ROUND(area_total_m2::numeric, 2);
                    END IF;
                END $$;
            """)

            # pula PDF que já está no banco - evita reextrair à toa.
            # FORCAR_REPROCESSAR=1 ignora esse filtro (ex: depois de mudar
            # a lógica de extração e precisar atualizar laudos antigos -
            # o upsert é por codigo_laudo, então isso atualiza em vez de duplicar).
            forcar_reprocessar = os.getenv("FORCAR_REPROCESSAR", "0") == "1"
            # REPROCESSAR_SEM_ENDERECO=1 relê só os laudos gravados sem
            # endereço/município (layout de capa de 2023 e começo de 2024,
            # antes lido errado) - ~2,4 mil em vez da base inteira
            sem_endereco = os.getenv("REPROCESSAR_SEM_ENDERECO", "0") == "1"
            # REPROCESSAR_LISTA=<arquivo .txt> relê só os laudos listados (um
            # código ou nome de PDF por linha) - pra corrigir os afetados por
            # um bug sem reprocessar a base inteira
            lista_reprocessar = os.getenv("REPROCESSAR_LISTA", "").strip()
            if lista_reprocessar:
                with open(lista_reprocessar, encoding="utf-8-sig") as f:
                    pedidos = {linha.strip() for linha in f if linha.strip()}
                pedidos |= {f"laudo_{c}.pdf" for c in list(pedidos)}
                cursor.execute("SELECT path FROM laudos WHERE path IS NOT NULL;")
                ja_processados = {row[0] for row in cursor.fetchall()} - pedidos
                print(f"[INFO] REPROCESSAR_LISTA - relendo os laudos listados em {lista_reprocessar}.")
            elif forcar_reprocessar:
                ja_processados = set()
                print("[INFO] FORCAR_REPROCESSAR=1 - reprocessando todos os PDFs da pasta, mesmo os já gravados.")
            elif sem_endereco:
                cursor.execute("SELECT path FROM laudos WHERE path IS NOT NULL "
                               "AND COALESCE(municipio, '') <> '' AND COALESCE(endereco, '') <> '';")
                ja_processados = {row[0] for row in cursor.fetchall()}
                print("[INFO] REPROCESSAR_SEM_ENDERECO=1 - relendo os laudos gravados sem endereço/município.")
            else:
                cursor.execute("SELECT path FROM laudos WHERE path IS NOT NULL;")
                ja_processados = {row[0] for row in cursor.fetchall()}

    # a extração de 10 mil PDFs leva mais de uma hora; uma conexão aberta
    # parada esse tempo todo cai ("connection already closed") e a carga
    # inteira se perde. Fecha aqui e abre outra na hora de gravar.
    conn.close()

    # PDF que já ficou fora do banco não é relido a cada execução
    # (FORCAR_REPROCESSAR relê tudo, e ele fica fora de novo)
    fora_do_banco = _carregar_fora_do_banco()
    pulados_fora = 0 if forcar_reprocessar else sum(
        1 for f in todos_pdfs if f in fora_do_banco and f not in ja_processados)
    pdf_files = [os.path.join(folder_path, f) for f in todos_pdfs
                 if f not in ja_processados and (forcar_reprocessar or f not in fora_do_banco)]
    pulados_ja_no_banco = len(todos_pdfs) - len(pdf_files) - pulados_fora
    print(f"  {pulados_ja_no_banco} já estavam no banco (extração pulada), {len(pdf_files)} novo(s) pra processar.")
    if pulados_fora:
        print(f"  {pulados_fora} fora do banco de propósito (empreendimento inteiro - {ARQUIVO_FORA_DO_BANCO}).")

    if not pdf_files:
        print("Nada novo pra extrair.")
        return

    num_workers = min(multiprocessing.cpu_count(), 8)
    dados_extraidos = []
    amostras_extraidas = []
    vazios = []
    erros_parser = []
    imagens_verificadas = []

    print(f"Iniciando extração paralela em {num_workers} workers...")
    with ProcessPoolExecutor(max_workers=num_workers) as executor:
        futures = {executor.submit(extrair_dados_pdf, f): f for f in pdf_files}
        for concluidos, future in enumerate(as_completed(futures), start=1):
            # com 10 mil PDFs a extração leva bem mais de uma hora - sem
            # isso o terminal fica mudo e parece que travou
            if concluidos % 500 == 0 or concluidos == len(futures):
                print(f"  ... {concluidos}/{len(futures)} PDFs lidos", flush=True)
            resultado = future.result()
            if resultado.get("imagens_verificadas"):
                imagens_verificadas.append(futures[future])
            if resultado["status"] == "ok":
                dados_extraidos.append(resultado["dados"])
                amostras_extraidas.extend(resultado.get("amostras", []))
            elif resultado["status"] == "vazio":
                vazios.append(resultado["path"])
            else:
                erros_parser.append((resultado["path"], resultado["mensagem"]))

    # PDFs com as imagens tiradas agora do zero entram na lista de
    # verificados da etapa de imagens - senão ela reabre todos de novo
    marcar_verificados(imagens_verificadas)

    fora = [d for d in dados_extraidos if fica_fora_do_banco(d)]
    if fora:
        codigos_fora = {d["codigo_laudo"] for d in fora}
        dados_extraidos = [d for d in dados_extraidos if not fica_fora_do_banco(d)]
        amostras_extraidas = [a for a in amostras_extraidas if a.get("codigo_laudo") not in codigos_fora]
        for d in fora:
            fora_do_banco[d["path"]] = d.get("tipo_imovel")
            motivo = "laudo de inspeção (Laudão PJ)" if d.get("_documento") == "inspecao" else d.get("tipo_imovel")
            print(f"[INFO] {d['codigo_laudo']} ({motivo}) não vai pro banco - "
                  f"empreendimento inteiro, não é laudo de unidade.")
        _salvar_fora_do_banco(fora_do_banco)

    print(f"Extração concluída. Total laudos: {len(dados_extraidos)} | amostras: {len(amostras_extraidas)}")
    if vazios:
        print(f"[AVISO] {len(vazios)} PDF(s) sem texto extraível (provavelmente escaneado como imagem):")
        for p in vazios:
            print(f"  {p}")
    if erros_parser:
        print(f"[AVISO] {len(erros_parser)} PDF(s) com erro na extração:")
        for p, msg in erros_parser:
            print(f"  {p}: {msg}")
    if not dados_extraidos:
        return

    if not validar_lote(dados_extraidos, amostras_extraidas, erros_parser):
        # código 2: o rodar_pipeline.py para aqui, sem imagens/coordenadas
        sys.exit(2)

    colunas = list(dados_extraidos[0].keys())

    # Avisa se dois ou mais arquivos extraíram o MESMO código de laudo
    # (TAT/TAN/TAP...) - isso NUNCA deve acontecer de verdade (cada
    # inspeção tem um código único), então é sinal de erro de extração
    # nesses arquivos. Repetir N° de Proposta é normal e esperado (uma
    # mesma proposta pode ter mais de um laudo/inspeção).
    arquivos_por_codigo = defaultdict(list)
    for d in dados_extraidos:
        if d["codigo_laudo"]:
            arquivos_por_codigo[d["codigo_laudo"]].append(d["path"])
    duplicados = {k: v for k, v in arquivos_por_codigo.items() if len(v) > 1}
    if duplicados:
        print("\n[AVISO] Mais de um arquivo extraiu o mesmo código de laudo (isso não deveria acontecer):")
        for codigo, arquivos in duplicados.items():
            print(f"  Código {codigo}: {', '.join(arquivos)}")
        print("  (o último desses arquivos processado vai prevalecer no banco -")
        print("  vale conferir manualmente a extração desses PDFs)\n")

    # codigo_laudo é a chave de verdade - numero_proposta pode repetir
    set_clause = ",\n            ".join(
        f"{col} = EXCLUDED.{col}" for col in colunas if col != "codigo_laudo"
    )
    placeholders = ", ".join(["%s"] * len(colunas))
    query_upsert_lote = f"""
        INSERT INTO laudos ({', '.join(colunas)})
        VALUES %s
        ON CONFLICT (codigo_laudo) WHERE codigo_laudo IS NOT NULL DO UPDATE SET
            {set_clause};
    """
    query_upsert_linha = f"""
        INSERT INTO laudos ({', '.join(colunas)})
        VALUES ({placeholders})
        ON CONFLICT (codigo_laudo) WHERE codigo_laudo IS NOT NULL DO UPDATE SET
            {set_clause};
    """

    # um único INSERT não pode atualizar a mesma linha duas vezes, então
    # dedup por codigo_laudo antes (mantém o último) pra poder gravar tudo
    # num lote só
    por_codigo = {}
    sem_codigo = []
    for d in dados_extraidos:
        if d["codigo_laudo"]:
            por_codigo[d["codigo_laudo"]] = d
        else:
            sem_codigo.append(d)
    dados_para_gravar = list(por_codigo.values()) + sem_codigo

    try:
        conn = conectar()
    except Exception as e:
        print(f"[ERRO CARGA BANCO] não conseguiu reconectar pra gravar: {str(e)}")
        return

    try:
        with conn:
            with conn.cursor() as cursor:
                valores = [[dados[col] for col in colunas] for dados in dados_para_gravar]
                try:
                    execute_values(cursor, query_upsert_lote, valores, page_size=500)
                    print(f"[SUCESSO] {len(valores)} laudo(s) gravado(s) no banco (carga em lote). 0 com erro.")
                except Exception as e:
                    # se o lote falhar, cai pro linha-a-linha pra isolar o arquivo com problema
                    conn.rollback()
                    print(f"[AVISO] Carga em lote falhou ({str(e).splitlines()[0]}) - tentando linha por linha...")
                    gravados = 0
                    falhas = 0
                    for dados in dados_para_gravar:
                        linha_valores = [dados[col] for col in colunas]
                        try:
                            cursor.execute(query_upsert_linha, linha_valores)
                            gravados += 1
                        except Exception as e2:
                            conn.rollback()
                            falhas += 1
                            print(f"[ERRO - PULADO] {dados.get('path')}: {str(e2).splitlines()[0]}")
                    print(f"[SUCESSO] {gravados} laudo(s) gravado(s) no banco. {falhas} com erro (pulados).")

                gravar_amostras(cursor, amostras_extraidas)
        atualizar_estatisticas(conn)
    except Exception as e:
        print(f"[ERRO CARGA BANCO]: {str(e)}")
    finally:
        conn.close()


def validar_lote(dados_extraidos, amostras_extraidas, erros_parser=()):
    """Checa o lote inteiro antes de gravar (checagens.py, as mesmas do
    testar_amostra.py) - já leu os PDFs, então sai de graça, em vez de
    uma leitura a mais só pra validar. Se alguma checagem passar do
    limite, nada é gravado: foi assim que os laudos de 2023 teriam
    entrado sem endereço. VALIDACAO_IGNORAR=1 grava mesmo assim."""
    amostras_por_laudo = defaultdict(list)
    for a in amostras_extraidas:
        amostras_por_laudo[a.get("codigo_laudo")].append(a)
    laudos_por_checagem = defaultdict(set)
    exemplos = defaultdict(list)
    for d in dados_extraidos:
        for checagem, detalhe in checagens.problemas_dos_dados(d, amostras_por_laudo[d.get("codigo_laudo")]):
            laudos_por_checagem[checagem].add(d.get("path"))
            if len(exemplos[checagem]) < 3:
                exemplos[checagem].append(f"{d.get('path')} {detalhe}".strip())
    contagem = {c: len(p) for c, p in laudos_por_checagem.items()}
    if erros_parser:
        contagem["erro ao processar o PDF"] = len(erros_parser)
        exemplos["erro ao processar o PDF"] = [f"{p} {m}" for p, m in list(erros_parser)[:3]]
    total = len(dados_extraidos) + len(erros_parser)
    estouradas = checagens.checagens_estouradas(contagem, total)

    print(f"Validação do lote ({total} laudos):")
    if not contagem:
        print("  nenhum problema encontrado.")
    for checagem, n in sorted(contagem.items(), key=lambda x: -x[1]):
        marca = "ACIMA" if checagem in estouradas else "ok   "
        limite = checagens.LIMITES_DADOS.get(checagem, 0)
        print(f"  {marca} {checagem}: {n} ({n / total:.1%}, limite {limite:.0%}) - ex.: {exemplos[checagem][:2]}")
    if not estouradas:
        return True
    if os.getenv("VALIDACAO_IGNORAR", "0") == "1":
        print("[AVISO] Checagem acima do limite, mas VALIDACAO_IGNORAR=1 - gravando mesmo assim.")
        return True
    print("[BLOQUEADO] Nada foi gravado no banco: as checagens marcadas ACIMA passaram do limite "
          "(provável layout novo de laudo). Mande este log pra investigar. Pra gravar mesmo assim: "
          '$env:VALIDACAO_IGNORAR="1"')
    return False


def atualizar_estatisticas(conn):
    """ANALYZE depois da carga: a contagem "~ N registros" do Adminer (e o
    plano das consultas) vem da estatística do Postgres, que o autovacuum
    só refaz de vez em quando - depois de uma carga ela ficava com o
    número antigo. Não mexe em dado nenhum; se falhar, só avisa."""
    try:
        with conn:
            with conn.cursor() as cursor:
                cursor.execute("ANALYZE laudos, laudos_amostras;")
        print("[INFO] Estatísticas do banco atualizadas (ANALYZE) - o Adminer já mostra a contagem nova.")
    except Exception as e:
        print(f"[AVISO] ANALYZE falhou ({str(e).splitlines()[0]}) - os dados foram gravados, "
              "só a contagem aproximada do Adminer fica desatualizada.")

def manter_pc_acordado(ligar):
    """Impede o Windows de entrar em suspensão enquanto a extração roda (ela
    leva mais de uma hora com a pasta toda, e a suspensão mata o processo).
    Só segura a suspensão - a tela ainda apaga e bloqueia normalmente, o
    que não atrapalha o script. O Windows libera sozinho se o processo
    morrer; fora do Windows não faz nada."""
    if sys.platform != "win32":
        return
    import ctypes
    ES_CONTINUOUS = 0x80000000
    ES_SYSTEM_REQUIRED = 0x00000001
    ctypes.windll.kernel32.SetThreadExecutionState(
        ES_CONTINUOUS | (ES_SYSTEM_REQUIRED if ligar else 0))

if __name__ == "__main__":
    os.makedirs(LOG_DIR, exist_ok=True)
    log_path = os.path.join(LOG_DIR, f"extracao_{datetime.now():%Y%m%d_%H%M%S}.txt")
    log_file = open(log_path, "w", encoding="utf-8")
    stdout_original = sys.stdout
    sys.stdout = Tee(stdout_original, log_file)
    print(f"Log desta execução: {log_path}")
    manter_pc_acordado(True)
    if sys.platform == "win32":
        print("[INFO] Suspensão do PC bloqueada até a extração terminar.")
    try:
        processar_em_lote()
    finally:
        manter_pc_acordado(False)
        sys.stdout = stdout_original
        log_file.close()