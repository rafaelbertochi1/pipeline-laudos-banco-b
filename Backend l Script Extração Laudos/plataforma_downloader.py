"""
Baixa em massa os "Laudo Completo" da Plataforma (cliente Banco B) pra
um período de datas, salvando em data/laudos/ pro banco_b_extractor.py
processar depois.

Uso: pip install playwright && playwright install chromium && python plataforma_downloader.py

Login: usa a sessão salva. Se ela expirar, loga sozinho com as
variáveis de ambiente PLATAFORMA_USUARIO / PLATAFORMA_SENHA e responde a
verificação em duas etapas (MFA) com o App Autenticador via
PLATAFORMA_CHAVE_AUTENTICADOR - mesmas variáveis e mesma lógica dos
robôs robo-cadastro-banco-b/Banco A, então quem já configurou lá não
precisa configurar de novo. Sem elas, cai no login manual (HEADLESS=0).

Primeiro coleta a lista de pendentes do período todo - dividido em
sub-períodos e coletado em paralelo com PARALELISMO_COLETA abas -, depois
baixa tudo: os PDFs vêm direto da API numa fila única, com
DOWNLOADS_SIMULTANEOS downloads ao mesmo tempo; as abas (uma por
sub-período, reaplicando o mesmo filtro de datas estreito usado na
coleta - filtrar pelo período inteiro na tela de download perdia
laudos que só apareciam com um filtro mais estreito) dão os cabeçalhos
de login pra API e baixam pela tela o que a API não resolver. Período
grande vai em lotes de até DIAS_POR_LOTE dias (padrão 31), um de cada
vez, conferindo a sessão antes de cada lote; PERIODO_INICIO/PERIODO_FIM
dispensam digitar as datas. Testa com período pequeno antes de rodar um
ano inteiro assim.
"""

import base64
import hashlib
import hmac
import json
import os
import queue
import re
import statistics
import struct
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta
from urllib.parse import parse_qs, urlparse
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

LOGIN_URL = "https://plataforma-laudos.example.com/sistema/index.html#/home"
PASTA_SCRIPT = os.path.dirname(os.path.abspath(__file__))
DOWNLOAD_DIR = os.path.join(PASTA_SCRIPT, "data", "laudos")
# cookie de sessão da Plataforma - o navegador descarta ao fechar, então
# salvamos/recarregamos na mão. Nunca sobe pro GitHub (.gitignore).
SESSION_FILE = os.path.join(PASTA_SCRIPT, "sessao_plataforma.json")
LOG_DIR = os.path.join(PASTA_SCRIPT, "logs")

# quantos laudos novos processar nesta execução (cada página tem só 6).
# 9999 = pega tudo do período.
LIMITE_TESTE = 9999

def _inteiro_env(nome, padrao, minimo, maximo):
    """Número de uma variável de ambiente, dentro de [minimo, maximo] -
    valor ausente ou inválido usa o padrão (com aviso, se inválido)."""
    valor = os.getenv(nome, "").strip()
    if not valor:
        return padrao
    try:
        return max(minimo, min(maximo, int(valor)))
    except ValueError:
        print(f"[AVISO] {nome}={valor!r} não é um número - usando {padrao}.")
        return padrao


# abas simultâneas coletando a lista de laudos (antes de baixar nada).
# 3 já rodou limpo (sem bloqueio/captcha) num teste de 12 e num mês
# inteiro - subindo pra 6 agora. Ajustável sem editar o arquivo (máx. 12):
#   $env:PARALELISMO="8"
# Se a Plataforma começar a devolver erro/lentidão com mais abas, volte.
PARALELISMO_COLETA = _inteiro_env("PARALELISMO", 6, 1, 12)

# Laudo "sem laudo publicado" (cancelado, ou ainda em andamento) custa ~2s
# por tentativa e era reaberto em TODA execução - numa rodada real de
# 24/09/2026, 59 deles tomaram 2min15s de 5min, pra baixar 0. Agora fica
# anotado com a data da checagem e só é aberto de novo depois de
# RECHECAR_SEM_LAUDO_DIAS dias (0 = checar sempre, como antes).
#   $env:RECHECAR_SEM_LAUDO_DIAS="0"
RECHECAR_SEM_LAUDO_DIAS = _inteiro_env("RECHECAR_SEM_LAUDO_DIAS", 7, 0, 3650)
CACHE_SEM_LAUDO = os.path.join(DOWNLOAD_DIR, "_sem_laudo_publicado.json")

# o download abre uma aba por sub-período (não por PARALELISMO fixo) - o
# número de abas sai igual a PARALELISMO_COLETA, porque reaproveita a
# mesma divisão de sub-períodos da coleta (ver comentário em main()).
# As abas só servem pra ter os cabeçalhos de login e pro caminho pela
# tela; os PDFs saem da API numa fila única, com vários downloads ao
# mesmo tempo repartidos entre as abas (antes: um por vez em cada aba,
# e a aba com o sub-período mais cheio segurava o fim da execução).
# Ajustável sem editar o arquivo (máx. 24):
#   $env:DOWNLOADS_SIMULTANEOS="6"
# Se a Plataforma começar a devolver erro com mais downloads, diminua.
DOWNLOADS_SIMULTANEOS = _inteiro_env("DOWNLOADS_SIMULTANEOS", 12, 1, 24)

MODO_RAPIDO = True  # remove a pausa artificial entre cliques

# login e seleção de cliente são automáticos, não precisa ver a janela.
# Pra ver a janela (ex.: logar na mão sem as variáveis de credencial):
#   $env:HEADLESS="0"   (ou "false", como nos robôs Cadastro_Gestao)
# Variável de ambiente e não edição do .py - editar trava o `git pull`.
HEADLESS = os.getenv("HEADLESS", "1").strip().lower() not in ("0", "false", "nao", "não")

# a Plataforma anda lenta pra desenhar o painel depois do login/MFA
TIMEOUT_LONGO = 45000

# cada linha da lista é uma div (não uma <table> de verdade), confirmado
# via Inspecionar elemento. A tela tem 5 abas escondidas com as mesmas
# linhas no HTML, por isso o :visible no final.
LINHA_SELECTOR = "div.insp360-mouse-link.insp360-tabela-relatorio.insp360-cor-tabela-rel:visible"


def carregar_cache_sem_laudo():
    """{laudo_id: "aaaa-mm-dd" da última checagem sem laudo publicado}.
    Arquivo ausente ou corrompido = cache vazio (só perde a economia)."""
    try:
        with open(CACHE_SEM_LAUDO, encoding="utf-8") as f:
            dados = json.load(f)
        return {k: v for k, v in dados.items() if isinstance(k, str) and isinstance(v, str)}
    except FileNotFoundError:
        return {}
    except Exception as e:
        print(f"[AVISO] Não consegui ler {CACHE_SEM_LAUDO} ({e}) - checando todos de novo.")
        return {}


def salvar_cache_sem_laudo(cache):
    """Grava num arquivo temporário e troca - um Ctrl+C no meio nunca
    deixa o cache pela metade."""
    try:
        os.makedirs(os.path.dirname(CACHE_SEM_LAUDO), exist_ok=True)
        temporario = CACHE_SEM_LAUDO + ".tmp"
        with open(temporario, "w", encoding="utf-8") as f:
            json.dump(dict(sorted(cache.items())), f, indent=0)
        os.replace(temporario, CACHE_SEM_LAUDO)
    except Exception as e:
        print(f"[AVISO] Não consegui salvar {CACHE_SEM_LAUDO} ({e}).")


def checado_recentemente(cache, laudo_id, hoje):
    """True se o laudo foi checado sem laudo publicado há menos de
    RECHECAR_SEM_LAUDO_DIAS dias."""
    if RECHECAR_SEM_LAUDO_DIAS <= 0 or laudo_id not in cache:
        return False
    try:
        return (hoje - date.fromisoformat(cache[laudo_id])).days < RECHECAR_SEM_LAUDO_DIAS
    except ValueError:
        return False


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


def formatar_duracao(segundos):
    minutos, segundos = divmod(int(segundos), 60)
    horas, minutos = divmod(minutos, 60)
    if horas:
        return f"{horas}h{minutos:02d}min{segundos:02d}s"
    return f"{minutos}min{segundos:02d}s"


def extrair_numero_proposta(texto_linha):
    """Tenta achar o N° de Proposta dentro do texto de uma linha da tabela."""
    match = re.search(r'\b(\d{6,10})\b', texto_linha)
    return match.group(1) if match else None


def pedir_dado(pergunta):
    """Pede um dado ao usuário com um aviso visual bem claro."""
    print("\n" + "-" * 60)
    print(">>> PRECISO DE UMA INFORMAÇÃO SUA <<<")
    # o BOM aparece quando a resposta vem por pipe do PowerShell
    # ("01/04/2024" | python ...) e quebrava a leitura da data
    return input(f"{pergunta}: ").strip().lstrip("﻿")


def pausar_para_usuario(*linhas_instrucao):
    """Pausa o robô e deixa bem claro que é a vez do usuário agir."""
    print("\n" + "#" * 60)
    print("#  A AÇÃO É SUA AGORA - O ROBÔ ESTÁ PAUSADO")
    print("#" * 60)
    for linha in linhas_instrucao:
        print(linha)
    input(">>> Quando terminar, clique aqui no terminal e pressione ENTER... ")
    print("#" * 60 + "\n")


def novo_contexto_pagina(browser, com_sessao):
    # viewport grande em ambos os modos - a tabela usa rolagem virtual e
    # só desenha as linhas que cabem na altura visível
    context = browser.new_context(
        accept_downloads=True,
        viewport={"width": 1920, "height": 1080} if HEADLESS else None,
        no_viewport=None if HEADLESS else True,
        storage_state=SESSION_FILE if com_sessao else None,
    )
    return context, context.new_page()


def chegou_no_painel(page, timeout=8000):
    try:
        page.wait_for_selector("text=GRID DE INSPEÇÃO", timeout=timeout)
        return True
    except PlaywrightTimeout:
        return False


def selecionar_cliente_banco_b(page, timeout=6000):
    # tela "escolha o cliente" aparece mesmo com sessão válida - o logo é
    # imagem, sem texto, então usamos a ordem dos cards (Banco A 1º, Banco B 2º)
    selecionar = page.locator("text=SELECIONAR")
    try:
        selecionar.first.wait_for(timeout=timeout)
    except PlaywrightTimeout:
        return False

    if selecionar.count() < 2:
        print("      [AVISO] Tela de cliente com layout inesperado - selecione manualmente.")
        return False

    selecionar.nth(1).click()
    page.wait_for_timeout(500)
    print("      Cliente Banco B selecionado automaticamente.")
    return True


# ---------------------------------------------------------------------------
# Login automático - portado de robo-cadastro-banco-b (exportar_status_
# banco_b.py + comum.py), onde foi validado em execução real. Mesmas
# variáveis de ambiente, pra configuração valer pros robôs todos.
# ---------------------------------------------------------------------------

def credenciais_configuradas(prefixo):
    """Lê usuário/senha de `<PREFIXO>_USUARIO` / `<PREFIXO>_SENHA` -
    nunca ficam gravados no .py. Devolve (None, None) se faltar algum."""
    usuario = os.environ.get(f"{prefixo}_USUARIO", "").strip()
    senha = os.environ.get(f"{prefixo}_SENHA", "")
    if usuario and senha:
        return usuario, senha
    return None, None


def normalizar_chave_totp(texto):
    """Aceita a chave do App Autenticador com espaços/hífens, minúscula, ou
    a URL inteira do QR ("otpauth://totp/...?secret=..."). Devolve só a
    chave base32, maiúscula e sem separadores."""
    texto = (texto or "").strip()
    if texto.lower().startswith("otpauth://"):
        texto = parse_qs(urlparse(texto).query).get("secret", [""])[0]
    return texto.replace(" ", "").replace("-", "").upper()


def gerar_codigo_totp(chave, instante=None, digitos=6, periodo=30):
    """Código de 6 dígitos do App Autenticador (TOTP, RFC 6238 - o mesmo
    cálculo que o Google/Microsoft Authenticator fazem no celular). Só
    biblioteca padrão."""
    chave = normalizar_chave_totp(chave)
    chave_bytes = base64.b32decode(chave + "=" * (-len(chave) % 8), casefold=True)
    contador = int((time.time() if instante is None else instante) // periodo)
    mac = hmac.new(chave_bytes, struct.pack(">Q", contador), hashlib.sha1).digest()
    offset = mac[-1] & 0x0F
    valor = struct.unpack(">I", mac[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(valor % (10 ** digitos)).zfill(digitos)


def chave_autenticador_configurada(prefixo):
    """Lê `<PREFIXO>_CHAVE_AUTENTICADOR`. Devolve a chave normalizada ou
    None (ausente ou inválida). Nunca imprime a chave."""
    chave = normalizar_chave_totp(os.environ.get(f"{prefixo}_CHAVE_AUTENTICADOR", ""))
    if not chave:
        return None
    try:
        gerar_codigo_totp(chave, instante=0)
    except Exception:
        print(f"      [AVISO] {prefixo}_CHAVE_AUTENTICADOR está definida mas não é uma chave")
        print("      válida (esperado: letras A-Z e números 2-7).")
        return None
    return chave


def login_automatico(page, usuario, senha, timeout=30000):
    """Preenche e envia o formulário de login a partir do campo de senha
    visível - não presume nomes/ids de campo. O usuário é o primeiro input
    de texto/e-mail do MESMO <form>. Nunca imprime a senha. True só se
    conseguiu preencher e submeter."""
    try:
        campo_senha = page.locator("input[type='password']:visible").first
        campo_senha.wait_for(timeout=timeout)
    except Exception:
        return False

    form = campo_senha.locator("xpath=ancestor::form[1]")
    campo_usuario = form.locator(
        "input[type='text']:visible, input[type='email']:visible, input:not([type]):visible"
    ).first
    try:
        campo_usuario.fill(usuario, timeout=timeout)
        campo_senha.fill(senha, timeout=timeout)
    except Exception:
        return False

    botao = form.locator("button[type='submit'], input[type='submit']").first
    try:
        if botao.count() > 0:
            botao.first.click(timeout=timeout)
        else:
            campo_senha.press("Enter")
    except Exception:
        return False
    return True


def codigo_pagina_de_erro(page):
    """Código da tela de erro da Plataforma ("401"...) ou None. Ela não
    redireciona pro login quando a sessão vence: manda pra
    .../sistema/http-status.html#/401 ("ACESSO NÃO AUTORIZADO"), uma tela
    sem formulário nenhum."""
    url = page.url or ""
    if "http-status.html" not in url:
        return None
    if "#/" in url:
        return url.split("#/")[-1].strip("/") or "?"
    return "?"


def descartar_sessao_salva():
    """Apaga o arquivo de sessão - insistir com um token que o servidor já
    recusou só repete o mesmo 401."""
    try:
        os.remove(SESSION_FILE)
        return True
    except OSError:
        return False


def pagina_pede_mfa(page):
    """True se a Plataforma está na verificação em duas etapas
    (index.html#/verificacao-mfa) - a senha já foi aceita, falta o código."""
    return "verificacao-mfa" in (page.url or "")


def aguardar_painel_ou_erro(page, timeout=10000):
    """Espera o painel (GRID DE INSPEÇÃO), a tela de erro ou o MFA - o que
    vier primeiro. Devolve ("painel", None), ("erro", "401"), ("mfa", None)
    ou ("nada", None). Polling em vez de uma espera única: o 401 só vem
    depois de uma chamada de API, e o painel às vezes demora - uma sessão
    válida já foi tratada como morta por isso no robô irmão."""
    prazo = time.time() + timeout / 1000
    while time.time() < prazo:
        codigo = codigo_pagina_de_erro(page)
        if codigo:
            return "erro", codigo
        if pagina_pede_mfa(page):
            return "mfa", None
        if page.locator("text=GRID DE INSPEÇÃO").count() > 0:
            return "painel", None
        page.wait_for_timeout(500)
    return "nada", None


def _salvar_evidencia_falha_login(page, motivo):
    """Print da tela quando o login falha de verdade - evidência real em
    vez de adivinhar. Vai pra logs/ (ignorado pelo git)."""
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        caminho = os.path.join(LOG_DIR, f"falha_login_plataforma_{motivo}_{datetime.now():%Y%m%d_%H%M%S}.png")
        page.screenshot(path=caminho, full_page=True)
        print(f"      Print da tela no momento da falha salvo em: {caminho}")
    except Exception as e:
        print(f"      [AVISO] Não consegui nem salvar o print da falha ({e}).")


JS_DESCREVER_TELA = """
() => {
  const vis = (el) => {
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  };
  const texto = (document.body.innerText || "").replace(/\\n{2,}/g, "\\n").trim();
  const campos = [];
  document.querySelectorAll("input, select, textarea, button, a.btn, [role='button']").forEach((el) => {
    if (!vis(el)) return;
    campos.push({
      tag: el.tagName.toLowerCase(),
      type: el.getAttribute("type") || "",
      id: el.id || "",
      name: el.getAttribute("name") || "",
      texto: el.type === "checkbox" ? "" : (el.innerText || el.value || "").trim().slice(0, 60),
    });
  });
  return { texto: texto.slice(0, 1500), campos };
}
"""


def _descrever_tela_mfa(page):
    """Escreve no log o que a tela de MFA mostra (texto e campos) - o passo 2
    da verificação ainda não foi visto de verdade nos robôs irmãos, então
    isso é o que permite ajustar em cima da tela real. Só lê, nunca digita."""
    try:
        dados = page.evaluate(JS_DESCREVER_TELA)
    except Exception as e:
        print(f"      (não consegui ler a tela de MFA pra descrever: {e})")
        return
    print("      ---- o que a tela de MFA mostra ----")
    for linha in (dados.get("texto") or "").splitlines():
        if linha.strip():
            print(f"      | {linha.strip()}")
    for c in dados.get("campos") or []:
        detalhes = " ".join(f"{k}={c[k]!r}" for k in ("type", "id", "name", "texto") if c.get(k))
        print(f"      | <{c['tag']}> {detalhes}")
    print("      -----------------------------------")


# Tela real (robô irmão, 23/09/2026): "VERIFICAÇÃO DE SEGURANÇA - Passo 1 de
# 2", com os cards E-MAIL / SMS / App Autenticador e o botão "Avançar". Não
# existe "lembrar este dispositivo" - toda sessão nova passa por aqui.
SELETOR_CAMPO_CODIGO = (
    "input:not([type=hidden]):not([type=checkbox]):not([type=radio])"
    ":not([type=submit]):not([type=button]):visible"
)
PADRAO_BOTAO_CONFIRMAR_CODIGO = (
    r"^\s*(validar|verificar|confirmar|avan[çc]ar|entrar|continuar|concluir|enviar)\b"
)


def _clicar_botao_por_texto(page, padrao):
    rx = re.compile(padrao, re.IGNORECASE)
    botoes = page.locator(
        "button:visible, input[type=submit]:visible, a.btn:visible, [role=button]:visible"
    )
    for botao in botoes.all():
        try:
            texto = (botao.inner_text() or botao.get_attribute("value") or "").strip()
        except Exception:
            continue
        if rx.search(texto):
            botao.click()
            return True
    return False


def _esperar_campos_codigo(page, timeout):
    prazo = time.time() + timeout / 1000
    while True:
        campos = page.locator(SELETOR_CAMPO_CODIGO).all()
        if campos or time.time() >= prazo:
            return campos
        page.wait_for_timeout(300)


def _responder_mfa_autenticador(page, chave):
    """Uma tentativa de passar pela verificação com o App Autenticador.
    Devolve (True, None) se saiu da tela de MFA, ou (False, motivo)."""
    campos = _esperar_campos_codigo(page, 3000)
    if not campos:
        opcao = page.get_by_text("App Autenticador", exact=True)
        if opcao.count() == 0:
            return False, "sem_opcao_app_autenticador"
        opcao.first.click()
        page.wait_for_timeout(500)
        if not _clicar_botao_por_texto(page, r"^\s*Avan[çc]ar\s*$"):
            return False, "sem_botao_avancar"
        campos = _esperar_campos_codigo(page, 15000)
        if not campos:
            return False, "sem_campo_codigo"
        print("      Passo 2 da verificação - o que a tela mostra:")
        _descrever_tela_mfa(page)

    codigo = gerar_codigo_totp(chave)
    if len(campos) == 1:
        campos[0].fill(codigo)
    elif len(campos) == len(codigo):
        for campo, digito in zip(campos, codigo):  # uma caixinha por dígito
            campo.fill(digito)
    else:
        return False, f"campos_inesperados_{len(campos)}"
    if not _clicar_botao_por_texto(page, PADRAO_BOTAO_CONFIRMAR_CODIGO):
        campos[-1].press("Enter")

    try:
        page.wait_for_url(lambda url: "verificacao-mfa" not in url, timeout=15000)
    except PlaywrightTimeout:
        return False, "codigo_recusado"
    return True, None


def _resolver_mfa(page):
    """Com PLATAFORMA_CHAVE_AUTENTICADOR, gera o código e conclui a
    verificação sozinho; sem ela, explica o que falta. True só se o painel
    aparecer depois."""
    chave = chave_autenticador_configurada("PLATAFORMA")
    if not chave:
        print(f"      [MFA] A Plataforma pediu verificação em duas etapas (URL: {page.url}).")
        print("      A senha foi aceita - falta o código, e sem PLATAFORMA_CHAVE_AUTENTICADOR")
        print("      o robô não tem como gerá-lo.")
        _salvar_evidencia_falha_login(page, "mfa")
        _descrever_tela_mfa(page)
        return False
    print("      [MFA] Verificação em duas etapas - respondendo com o App Autenticador...")
    motivo = None
    for tentativa in (1, 2):
        ok, motivo = _responder_mfa_autenticador(page, chave)
        if ok:
            print("      [MFA] Verificação concluída.")
            selecionar_cliente_banco_b(page, timeout=8000)
            estado, codigo = aguardar_painel_ou_erro(page, timeout=TIMEOUT_LONGO)
            if estado == "painel":
                return True
            motivo = f"erro_{codigo}" if estado == "erro" else f"sem_painel_depois_do_mfa_{estado}"
            break
        if motivo != "codigo_recusado" or tentativa == 2:
            break
        # o código vale por janelas de 30s - com o relógio do Windows um
        # pouco fora, o próximo costuma passar
        espera = 30 - (time.time() % 30) + 1
        print(f"      Código recusado - esperando o próximo ({espera:.0f}s) e tentando de novo...")
        page.wait_for_timeout(espera * 1000)
    print(f"      [MFA] Não consegui concluir a verificação com o App Autenticador ({motivo}).")
    if motivo == "codigo_recusado":
        print("      Confira se a chave é a mesma cadastrada na Plataforma e se o relógio")
        print("      do Windows está certo (Configurações > Hora > Sincronizar agora).")
    _salvar_evidencia_falha_login(page, f"mfa_{motivo}")
    _descrever_tela_mfa(page)
    return False


def _tentar_login_automatico(page):
    """Loga sozinho com PLATAFORMA_USUARIO / PLATAFORMA_SENHA. True só se o
    painel realmente aparecer (a Plataforma ainda seleciona cliente e pode
    pedir MFA, então confirmar de verdade é essencial)."""
    usuario, senha = credenciais_configuradas("PLATAFORMA")
    if not usuario:
        print("      [AVISO] PLATAFORMA_USUARIO / PLATAFORMA_SENHA não estão definidas NESTE terminal -")
        print("      sem elas não dá pra logar sozinho. Se você já rodou o")
        print("      SetEnvironmentVariable, feche e reabra o VS Code inteiro.")
        return False
    print("      Sessão expirada - tentando login automático...")
    if pagina_pede_mfa(page):
        return _resolver_mfa(page)
    if codigo_pagina_de_erro(page):
        # a tela de erro não tem formulário - volta pro login, o mesmo
        # clique que uma pessoa daria no link "plataforma-laudos.example.com" dela
        try:
            page.goto(LOGIN_URL)
            page.wait_for_timeout(2000)
        except Exception:
            pass
    if not login_automatico(page, usuario, senha):
        # sem formulário e sem erro: pode ser só o painel demorando (a
        # sessão ainda valia) - mais um tempo antes de desistir
        estado, _codigo = aguardar_painel_ou_erro(page, timeout=30000)
        if estado == "painel":
            print("      O painel apareceu (só estava lento) - a sessão salva ainda vale.")
            return True
        if estado == "mfa":
            return _resolver_mfa(page)
        print(f"      [AVISO] Não achei o formulário de login pra preencher sozinho (URL: {page.url}).")
        _salvar_evidencia_falha_login(page, "sem_formulario")
        return False
    page.wait_for_timeout(3000)
    selecionar_cliente_banco_b(page)
    estado, codigo = aguardar_painel_ou_erro(page, timeout=TIMEOUT_LONGO)
    if estado == "erro":
        print(f"      [AVISO] Preenchi o login e a Plataforma devolveu a tela de erro {codigo}.")
        _salvar_evidencia_falha_login(page, f"erro_{codigo}")
        return False
    if estado == "mfa":
        return _resolver_mfa(page)
    if estado != "painel":
        print("      [AVISO] Preenchi o login mas não cheguei no painel "
              "(credencial errada, ou a página não respondeu a tempo).")
        _salvar_evidencia_falha_login(page, "sem_painel")
        return False
    return True


def preparar_sessao(browser):
    """Garante uma sessão válida (painel GRID DE INSPEÇÃO) e salva em
    SESSION_FILE, antes da coleta/download em paralelo - cada aba depois
    abre o próprio navegador com a sessão salva aqui. Ordem: sessão salva
    -> login automático (senha + MFA) -> login manual (só com janela).
    Devolve (True, context) se deu certo, (False, context) se não."""
    sessao_existente = os.path.exists(SESSION_FILE)
    context, page = novo_contexto_pagina(browser, com_sessao=sessao_existente)

    page.goto(LOGIN_URL)
    selecionar_cliente_banco_b(page)
    estado, codigo_erro = aguardar_painel_ou_erro(page, timeout=10000)

    if estado == "erro":
        print(f"      A Plataforma respondeu com a tela de erro {codigo_erro} "
              f"(ACESSO NÃO AUTORIZADO) - a sessão salva não vale mais.")
        print("      Descartando a sessão salva e recomeçando do zero...")
        descartar_sessao_salva()
        # contexto NOVO, sem storage_state: a Plataforma guarda o token no
        # localStorage, não só em cookie - limpar cookies não bastava
        try:
            context.close()
        except Exception:
            pass
        context, page = novo_contexto_pagina(browser, com_sessao=False)
        sessao_existente = False
        page.goto(LOGIN_URL)
        selecionar_cliente_banco_b(page)
        estado, codigo_erro = aguardar_painel_ou_erro(page, timeout=10000)
        if estado == "erro":
            print(f"\n[ERRO] Mesmo começando do zero a Plataforma devolveu a tela de erro {codigo_erro}.")
            print("Isso é do lado da Plataforma: normalmente é o usuário sem permissão no")
            print("cliente Banco B, ou o acesso suspenso. Confira entrando em plataforma-laudos.example.com")
            print("no seu navegador normal.")
            return False, context

    if sessao_existente and estado == "painel":
        print("      Sessão válida, painel carregado direto.")
    elif _tentar_login_automatico(page):
        print("      Login automático OK - sessão renovada.")
    elif HEADLESS:
        if pagina_pede_mfa(page):
            print("\n[ERRO] A Plataforma pediu verificação em duas etapas (MFA) e o robô não")
            print("conseguiu responder sozinho (veja as linhas [MFA] acima). Solução definitiva:")
            print("configurar PLATAFORMA_CHAVE_AUTENTICADOR (README, seção Login). Pra destravar")
            print("hoje, rode com a janela visível e digite o código na mão:")
        else:
            print("\n[ERRO] A sessão salva expirou e não consegui logar sozinho.")
            print("Pra rodar sem ninguém na frente, configure as credenciais uma vez")
            print("(README, seção Login). Pra destravar hoje, rode com a janela visível e")
            print("logue na mão:")
        print(f'  $env:HEADLESS="0"; python {os.path.basename(__file__)}; $env:HEADLESS=""')
        return False, context
    else:
        if pagina_pede_mfa(page):
            print("      A Plataforma está pedindo verificação em duas etapas (MFA) - a senha")
            print("      já foi aceita, só falta digitar o código na janela do Chrome.")
        if codigo_pagina_de_erro(page):
            print(f"      O Chrome está na tela de erro {codigo_pagina_de_erro(page)} da Plataforma (sem")
            print("      formulário). Clique no link 'plataforma-laudos.example.com' embaixo do erro pra ir pro login.")
        pausar_para_usuario(
            "A sessão salva não está mais válida (ou pediu login/",
            "verificação de novo).",
            "1. Faça o login manualmente na janela do Chrome (e-mail e senha).",
            "2. Selecione o cliente Banco B, se for pedido (o robô já",
            "   tenta fazer isso sozinho, mas confirme se ele conseguiu).",
            "3. Complete a verificação de segurança, se pedir.",
            "4. Espere até ver a tela com 'GRID DE INSPEÇÃO'.",
        )
        estado, codigo_erro = aguardar_painel_ou_erro(page, timeout=10000)
        # ENTER apertado com o Chrome ainda no MFA (caso real no robô
        # irmão) não pode jogar a execução fora - dá mais chances
        tentativas_restantes = 5
        while estado in ("mfa", "nada") and tentativas_restantes > 0:
            tentativas_restantes -= 1
            if estado == "mfa":
                pausar_para_usuario(
                    "O Chrome AINDA está na verificação em duas etapas (MFA).",
                    "Termine por lá (digite o código e confirme) e só aperte",
                    "ENTER aqui quando aparecer o 'GRID DE INSPEÇÃO'.",
                )
            else:
                pausar_para_usuario(
                    "Ainda não vi o 'GRID DE INSPEÇÃO' no Chrome.",
                    "Espere ele aparecer e aperte ENTER de novo.",
                )
            estado, codigo_erro = aguardar_painel_ou_erro(page, timeout=10000)
        if estado != "painel":
            if estado == "erro":
                print(f"\n[ERRO] Depois do login o Chrome continua na tela de erro {codigo_erro}.")
            elif estado == "mfa":
                print("\n[ERRO] A verificação em duas etapas (MFA) não foi concluída.")
            else:
                print("\n[ERRO] Ainda não consegui ver o painel principal (GRID DE INSPEÇÃO).")
            print("Encerrando esta execução.")
            return False, context

    try:
        context.storage_state(path=SESSION_FILE)
        print("      Sessão salva para as próximas execuções.")
    except Exception as e:
        # não derruba a execução: a sessão está válida neste navegador, só
        # as abas paralelas é que podem precisar logar de novo
        print(f"      [AVISO] Não consegui salvar a sessão ({e}).")
    return True, context


def preencher_periodo_e_exibir(page, data_inicio, data_fim):
    page.wait_for_selector("text=GRID DE INSPEÇÃO", timeout=20000)
    page.locator("button.hamburger").click()
    page.click("text=Administrativo")
    page.click("text=Relatórios")
    page.click("text=Inspeções")
    page.click("text=Analítico")

    page.wait_for_selector("text=PERÍODO DE SOLICITAÇÃO DA INSPEÇÃO", timeout=15000)
    # busca os campos de data DEPOIS desse título - o painel FILTROS tem
    # outros campos com a mesma classe CSS
    titulo_periodo = page.locator("text=PERÍODO DE SOLICITAÇÃO DA INSPEÇÃO")
    campos_data = titulo_periodo.locator(
        "xpath=following::input[contains(@class,'insp360-filtro-data')]"
    )

    def preencher_data(campo, valor):
        # .fill() não funciona com a máscara desse campo
        campo.click()
        campo.press("Control+A")
        campo.type(valor, delay=50)
        page.keyboard.press("Escape")
        page.wait_for_timeout(300)

    preencher_data(campos_data.nth(0), data_inicio)
    preencher_data(campos_data.nth(1), data_fim)
    # os botões "30/60/90" ao lado são atalhos de período ("últimos N
    # dias"), não itens por página - não mexer, sobrescrevem as datas

    # a resposta da API da grade diz se o período tem alguma inspeção -
    # sem ela, período vazio (fim de semana, feriado) ficava esperando
    # linha que nunca aparece e saía como "[ERRO] Timeout"
    try:
        with page.expect_response(lambda r: TRECHO_GRADE_API in r.url, timeout=30000) as info:
            page.get_by_role("button", name="Exibir").click()
        dados = info.value.json()
        conteudo = dados.get("content") if isinstance(dados, dict) else dados
        if isinstance(conteudo, list) and not conteudo:
            return False
    except PlaywrightTimeout:
        pass  # não viu a resposta: segue esperando as linhas, como antes
    except Exception:
        pass
    page.wait_for_selector(LINHA_SELECTOR, timeout=20000)
    return True


def fechar_modal_preso(page):
    """Tenta fechar um modal que ficou preso na tela, bloqueando cliques
    seguintes ("... intercepts pointer events" nos logs do Playwright).
    Nunca levanta exceção - só diz se conseguiu ou não."""
    try:
        if page.locator(".modal.in").count() > 0:
            page.locator("i.insp360-icone-fechar").first.click(timeout=5000)
            page.wait_for_selector(".modal.in", state="hidden", timeout=10000)
            return True
    except Exception:
        pass
    return False


# ---------------------------------------------------------------------------
# Download direto pela API da Plataforma
# ---------------------------------------------------------------------------
# O site busca o PDF numa API (confirmado com um script de diagnóstico
# numa rodada real):
#   GET .../inspecao-360-api/v1/laudos?itemInspecao=<item>   -> lista de laudos
#   GET .../inspecao-360-api/v1/laudos/<id>/pdf?tipoLaudo=1  -> o PDF ("Laudo Completo")
# Só com os cookies a API responde 401 - ela exige os cabeçalhos que o
# próprio site manda (authorization, cod-cliente, plataforma-ts...). Por
# isso não montamos esses cabeçalhos: copiamos os da última chamada que o
# site fez, sem nunca gravar nem imprimir os valores.
# Chamar a API direto dispensa abrir a linha, o modal, a aba Laudos, a
# engrenagem etc. pra cada laudo. Qualquer problema (código do laudo não
# achado nos dados da grade, erro na API, resposta que não é PDF) cai no
# caminho antigo pela tela - no pior caso fica igual era antes.
# Pra desligar e usar só a tela: $env:DOWNLOAD_API="0"
DOWNLOAD_API = os.getenv("DOWNLOAD_API", "1").strip().lower() not in ("0", "false", "nao", "não")
TRECHO_API = "/inspecao-360-api/v1/"
TRECHO_GRADE_API = "/relatorios/inspecoes/analitico"
# cabeçalhos que o próprio Playwright monta (ou que não fazem sentido
# repetir numa chamada nova); os ":..." são pseudo-cabeçalhos do HTTP/2
_CABECALHOS_NAO_COPIAR = {"host", "content-length", "content-type", "cookie", "accept-encoding", "connection"}
# depois de tantas falhas seguidas a aba desiste da API e vai só pela tela
MAX_FALHAS_API_SEGUIDAS = 3


class ErroApi(Exception):
    pass


class SoPelaTela(ErroApi):
    """Não é falha da API - é um caso que a tela decide melhor."""


def _atualizar_carimbo(valor):
    """Se o valor for um carimbo de hora (milissegundos ou segundos desde
    1970, perto de agora), devolve o de agora - é o que o site mandaria
    numa chamada nova. Qualquer outro formato volta como estava."""
    if not valor.isdigit():
        return valor
    agora = time.time()
    if len(valor) == 13 and abs(int(valor) / 1000 - agora) < 86400:
        return str(int(agora * 1000))
    if len(valor) == 10 and abs(int(valor) - agora) < 86400:
        return str(int(agora))
    return valor


class ApiPlataforma:
    """Chama a API da Plataforma com os cabeçalhos da última chamada que o
    site fez nesta mesma aba (o site continua fazendo chamadas enquanto a
    aba é usada, então os cabeçalhos se renovam sozinhos)."""

    def __init__(self, contexto):
        self.contexto = contexto
        self.base = None
        self._ultima = None
        self._lida = None
        self._cabecalhos = None
        contexto.on("request", self._observar)

    def _observar(self, req):
        # só guarda a referência - ler os cabeçalhos completos é uma ida ao
        # navegador, feita depois, fora do evento
        i = req.url.find(TRECHO_API)
        if i >= 0 and req.method == "GET":
            self.base = req.url[: i + len(TRECHO_API)]
            self._ultima = req

    def _cabecalhos_atuais(self):
        req = self._ultima
        if req is not None and req is not self._lida:
            try:
                todos = req.all_headers()
            except Exception:
                todos = {}
            cabecalhos = {
                nome: valor for nome, valor in todos.items()
                if not nome.startswith(":") and nome.lower() not in _CABECALHOS_NAO_COPIAR
            }
            if any(nome.lower() == "authorization" for nome in cabecalhos):
                self._cabecalhos = cabecalhos
            self._lida = req
        if not self._cabecalhos:
            raise ErroApi("ainda não vi nenhuma chamada autenticada do site à API")
        return self._cabecalhos

    def sessao_http(self):
        """Cópia dos cabeçalhos e cookies desta aba pra usar fora do
        Playwright (ver SessaoHttp). Chamar na thread da aba."""
        cabecalhos = self._cabecalhos_atuais()
        cookies = "; ".join(f"{c['name']}={c['value']}" for c in self.contexto.cookies(self.base))
        return SessaoHttp(self.base, dict(cabecalhos), cookies)


class SessaoHttp:
    """Chama a API com os cabeçalhos copiados de uma aba, pelo urllib. O
    Playwright síncrono só pode ser usado na thread que o abriu (um
    download por vez por aba); assim vários downloads rodam ao mesmo
    tempo. Nunca grava nem imprime os valores dos cabeçalhos."""

    def __init__(self, base, cabecalhos, cookies):
        self.base = base
        self._cabecalhos = cabecalhos
        self._cookies = cookies

    def _get(self, caminho):
        cabecalhos = {
            nome: (_atualizar_carimbo(valor) if nome.lower() == "plataforma-ts" else valor)
            for nome, valor in self._cabecalhos.items()
        }
        if self._cookies:
            cabecalhos["Cookie"] = self._cookies
        pedido = urllib.request.Request(self.base + caminho, headers=cabecalhos)
        try:
            with urllib.request.urlopen(pedido, timeout=60) as resposta:
                return resposta.read()
        except urllib.error.HTTPError as e:
            raise ErroApi(f"a API respondeu {e.code}") from None
        except (urllib.error.URLError, OSError) as e:
            raise ErroApi(f"falha de rede ({getattr(e, 'reason', e)})") from None

    def _lista(self, caminho):
        try:
            dados = json.loads(self._get(caminho))
        except ValueError:
            raise ErroApi("a resposta da API não é JSON") from None
        if isinstance(dados, dict):
            dados = dados.get("content") or []
        return dados if isinstance(dados, list) else []

    def baixar(self, item_id, destino, tempos):
        """Retorna 'baixado' ou 'sem_laudo'; levanta exceção em qualquer
        outro caso (quem chama cai no caminho pela tela). Anota em
        `tempos` quanto levou cada chamada ("lista" e "pdf")."""
        inicio = time.time()
        laudos = self._lista(f"laudos?itemInspecao={item_id}")
        tempos["lista"] = time.time() - inicio
        if not laudos:
            # a aba Laudos da tela também pode listar laudo "frustro"
            # (vistoria que não aconteceu); nesse caso raro deixa a tela
            # decidir, pra não marcar como "sem laudo" algo que a tela baixaria
            if self._lista(f"laudosFrustro?itemInspecao={item_id}"):
                raise SoPelaTela("só tem laudo frustro")
            return "sem_laudo"
        # o primeiro da lista é o mesmo da primeira engrenagem na tela,
        # que é o que o caminho pela tela sempre baixou
        laudo_id_api = laudos[0].get("id")
        if not laudo_id_api:
            raise ErroApi("laudo sem id na resposta da API")
        inicio = time.time()
        corpo = self._get(f"laudos/{laudo_id_api}/pdf?tipoLaudo=1")
        tempos["pdf"] = time.time() - inicio
        if not corpo.startswith(b"%PDF"):
            raise ErroApi("a resposta não é um PDF")
        # grava com outro nome e renomeia no fim: um PDF pela metade (queda
        # no meio) nunca fica com o nome final, que faria ele ser pulado
        # como "já existia" na próxima rodada
        temporario = destino + ".tmp"
        with open(temporario, "wb") as f:
            f.write(corpo)
        os.replace(temporario, destino)
        return "baixado"


def _textos(valor, saida=None):
    """Todos os textos de um JSON (e cada pedaço alfanumérico deles)."""
    if saida is None:
        saida = set()
    if isinstance(valor, dict):
        for v in valor.values():
            _textos(v, saida)
    elif isinstance(valor, list):
        for v in valor:
            _textos(v, saida)
    elif isinstance(valor, str):
        texto = valor.strip()
        if texto:
            saida.add(texto)
            saida.update(re.findall(r"[A-Za-z0-9]+", texto))
    return saida


class MapaItensGrade:
    """Liga o código de cada linha da grade (ex.: TAT9324) ao id do item
    de inspeção que a API usa, lendo as respostas que o próprio site
    recebe pra desenhar a grade. Não depende do nome do campo que guarda
    o código (não deu pra ver no diagnóstico): procura o código entre os
    textos de cada inspeção, e só aceita se achar numa inspeção só."""

    def __init__(self, pagina):
        self._respostas = []
        self._entradas = []
        pagina.on("response", self._guardar)

    def _guardar(self, resposta):
        if TRECHO_GRADE_API in resposta.url:
            self._respostas.append(resposta)

    def _processar(self):
        respostas, self._respostas = self._respostas, []
        for resposta in respostas:
            try:
                dados = resposta.json()
            except Exception:
                continue
            conteudo = dados.get("content") if isinstance(dados, dict) else dados
            for inspecao in conteudo if isinstance(conteudo, list) else []:
                if not isinstance(inspecao, dict):
                    continue
                itens = [it for it in inspecao.get("itens") or [] if isinstance(it, dict) and it.get("id")]
                textos_inspecao = _textos({k: v for k, v in inspecao.items() if k != "itens"})
                for item in itens:
                    self._entradas.append((_textos(item), textos_inspecao, item["id"], len(itens)))
        # só as páginas mais recentes interessam (as linhas lidas são as
        # da página que acabou de ser desenhada)
        self._entradas = self._entradas[-120:]

    def item_de(self, codigo):
        self._processar()
        achados = {
            item_id for textos_item, textos_inspecao, item_id, qtd_itens in self._entradas
            if codigo in textos_item or (qtd_itens == 1 and codigo in textos_inspecao)
        }
        # ambíguo ou não achado: None, e esse laudo vai pela tela
        return achados.pop() if len(achados) == 1 else None


class FilaApi:
    """Fila única dos downloads pela API, compartilhada por todas as abas:
    cada download simultâneo pega o próximo laudo livre, seja de que
    sub-período for. O que não der pela API vai pra lista "pela tela" do
    sub-período do laudo - só a aba desse sub-período acha a linha dele."""

    def __init__(self, pendentes, num_abas):
        self.fila = queue.Queue()
        self.pela_tela = defaultdict(dict)  # sub-período -> {laudo_id: (proposta, destino, item_id)}
        for grupo, numero_proposta, laudo_id, destino, item_id in pendentes:
            if item_id and DOWNLOAD_API:
                self.fila.put((grupo, numero_proposta, laudo_id, destino, item_id))
            else:
                self.pela_tela[grupo][laudo_id] = (numero_proposta, destino, item_id)
        self.ativa = DOWNLOAD_API and not self.fila.empty()
        self.lock = threading.Lock()
        self._abas_na_fase_api = num_abas
        self._fim_fase_api = threading.Condition(self.lock)
        # com vários downloads ao mesmo tempo, uma queda de rede derruba
        # alguns juntos - só desiste da API depois de mais falhas seguidas
        self.limite_falhas = max(MAX_FALHAS_API_SEGUIDAS, DOWNLOADS_SIMULTANEOS)
        self.falhas_seguidas = 0
        self.baixados = self.sem_laudo = 0
        self.ids_sem_laudo = []
        self.tempos_lista = []
        self.tempos_pdf = []

    def proximo(self):
        if not self.ativa:
            return None
        try:
            return self.fila.get_nowait()
        except queue.Empty:
            return None

    def deu_certo(self, laudo_id, resultado, tempos):
        with self.lock:
            self.falhas_seguidas = 0
            if "lista" in tempos:
                self.tempos_lista.append(tempos["lista"])
            if "pdf" in tempos:
                self.tempos_pdf.append(tempos["pdf"])
            if resultado == "baixado":
                self.baixados += 1
            else:
                self.sem_laudo += 1
                self.ids_sem_laudo.append(laudo_id)

    def falhou(self, item, conta_como_falha):
        """Manda o laudo pra tela. Devolve True se foi esta falha que
        desligou a API (pra avisar no log uma vez só)."""
        grupo, numero_proposta, laudo_id, destino, item_id = item
        with self.lock:
            self.pela_tela[grupo][laudo_id] = (numero_proposta, destino, item_id)
            if not conta_como_falha:
                return False
            self.falhas_seguidas += 1
            if self.ativa and self.falhas_seguidas >= self.limite_falhas:
                self.ativa = False
                return True
            return False

    def terminar_fase_api(self):
        """Cada aba chama uma vez, quando os downloads dela acabaram (ou
        quando ela nem conseguiu começar). Espera todas as abas: um laudo
        do sub-período desta aba pode ter falhado no download de outra.
        A última a chegar manda pra tela o que sobrou na fila (API
        desligada no meio, ou nenhuma aba conseguiu os cabeçalhos)."""
        with self.lock:
            self._abas_na_fase_api -= 1
            if self._abas_na_fase_api <= 0:
                while True:
                    try:
                        grupo, numero_proposta, laudo_id, destino, item_id = self.fila.get_nowait()
                    except queue.Empty:
                        break
                    self.pela_tela[grupo][laudo_id] = (numero_proposta, destino, item_id)
                self._fim_fase_api.notify_all()
            else:
                while self._abas_na_fase_api > 0:
                    self._fim_fase_api.wait()


def _baixar_da_fila(fila, sessao, log):
    """Um download simultâneo: vai pegando laudos da fila até ela acabar."""
    while True:
        item = fila.proximo()
        if item is None:
            return
        _grupo, numero_proposta, laudo_id, destino, item_id = item
        tempos = {}
        try:
            resultado = sessao.baixar(item_id, destino, tempos)
        except Exception as e:
            motivo = str(e).splitlines()[0] if str(e) else type(e).__name__
            log(f"[API] {laudo_id}: {motivo} - vai pela tela.")
            if fila.falhou(item, not isinstance(e, SoPelaTela)):
                log(f"[API] {fila.limite_falhas} falhas seguidas - o resto vai pela tela.")
            continue
        fila.deu_certo(laudo_id, resultado, tempos)
        if resultado == "baixado":
            log(f"{laudo_id} (proposta {numero_proposta}) salvo direto pela API "
                f"({tempos.get('lista', 0) + tempos.get('pdf', 0):.1f}s) em: {destino}")
        else:
            log(f"   [PULADO] Ainda não há laudo publicado para {laudo_id}.")


def baixar_um_laudo(page, laudo_id, destino):
    """Baixa o laudo completo da linha já visível. Retorna 'baixado' ou
    'sem_laudo'; levanta exceção em caso de erro."""
    linha = page.locator(LINHA_SELECTOR, has_text=laudo_id)
    if linha.count() == 0:
        raise RuntimeError(f"Não achei mais a linha de {laudo_id} na tela.")
    linha.first.click()
    page.wait_for_selector("text=DETALHAR INSPEÇÃO", timeout=15000)
    page.click("text=Laudos")

    # pode não ter laudo publicado ainda - espera qualquer um dos dois sinais
    icone_engrenagem = page.locator("i.fa-cog, i.fa-gear, .fa-cog")
    aviso_sem_laudo = page.locator("text=Nenhum laudo encontrado")
    icone_engrenagem.or_(aviso_sem_laudo).first.wait_for(timeout=15000)

    try:
        if aviso_sem_laudo.count() > 0:
            return "sem_laudo"

        icone_engrenagem.first.click()
        page.click("text=Download")

        page.wait_for_selector("text=DOWNLOAD DE LAUDO", timeout=15000)
        page.click("text=Laudo Completo")

        with page.expect_download(timeout=30000) as download_info:
            page.get_by_role("button", name="Download").click()
        download = download_info.value
        download.save_as(destino)
        return "baixado"
    finally:
        fechar_modal_preso(page)
        page.wait_for_selector(LINHA_SELECTOR, timeout=15000)


def baixar_lote_paralelo(indice, grupo, fila, downloads_por_aba, log_lock):
    """Uma aba de download, em paralelo com as outras: loga, filtra a
    grade pelo sub-período `grupo` (é isso que faz o site chamar a API e
    dá os cabeçalhos), abre `downloads_por_aba` downloads simultâneos na
    fila única da API e, quando todas as abas terminam a API, baixa pela
    tela o que sobrou do seu sub-período. Cada aba abre seu próprio
    navegador Playwright (não compartilha `browser` com as outras) - é a
    forma segura de paralelizar com a API síncrona.
    Devolve (baixados, sem_laudo, erros, ids_sem_laudo) do caminho pela tela."""
    prefixo = f"  [Aba {indice + 1}]"

    def log(msg):
        with log_lock:
            print(f"{prefixo} {msg}")

    fase_api_encerrada = []

    def encerrar_fase_api():
        # exatamente uma vez por aba, aconteça o que acontecer - as outras
        # abas ficam esperando todas passarem por aqui
        if not fase_api_encerrada:
            fase_api_encerrada.append(True)
            fila.terminar_fase_api()

    try:
        return _baixar_lote(prefixo, log, grupo, fila, downloads_por_aba, encerrar_fase_api)
    except Exception as e:
        # rede de segurança: navegador que nem abriu, por exemplo
        msg = str(e).splitlines()[0] if str(e) else type(e).__name__
        log(f"[ERRO INESPERADO NESTA ABA] {msg}")
        encerrar_fase_api()
        with fila.lock:
            return (0, 0, len(fila.pela_tela.get(grupo, {})), [])
    finally:
        encerrar_fase_api()


def _baixar_lote(prefixo, log, grupo, fila, downloads_por_aba, encerrar_fase_api):
    data_inicio, data_fim = grupo
    baixados = sem_laudo = erros = 0
    ids_sem_laudo = []

    with sync_playwright() as p:
        navegador = p.chromium.launch(
            headless=HEADLESS,
            args=[] if HEADLESS else ["--start-maximized"],
        )
        contexto, pagina = novo_contexto_pagina(navegador, com_sessao=True)
        api = ApiPlataforma(contexto)
        pronta = False
        pendentes_meus = {}
        try:
            # 1) direto pela API, na fila única; o que não der vai pela
            #    tela logo abaixo
            try:
                pagina.goto(LOGIN_URL)
                selecionar_cliente_banco_b(pagina)
                if not chegou_no_painel(pagina, timeout=15000):
                    log("[ERRO] Não consegui entrar no painel com a sessão salva - abortando esta aba.")
                else:
                    preencher_periodo_e_exibir(pagina, data_inicio, data_fim)
                    pronta = True
                    if fila.ativa:
                        try:
                            sessao = api.sessao_http()
                        except ErroApi as e:
                            log(f"[API] {e} - esta aba não baixa pela API.")
                        else:
                            downloads = [
                                threading.Thread(target=_baixar_da_fila, args=(fila, sessao, log), daemon=True)
                                for _ in range(downloads_por_aba)
                            ]
                            for t in downloads:
                                t.start()
                            for t in downloads:
                                t.join()
            except Exception as e:
                msg = str(e).splitlines()[0] if str(e) else type(e).__name__
                log(f"[ERRO INESPERADO NESTA ABA] {msg}")
            finally:
                encerrar_fase_api()

            with fila.lock:
                pendentes_meus = dict(fila.pela_tela.get(grupo, {}))
            if not pronta:
                erros += len(pendentes_meus)
                pendentes_meus = {}

            # 2) pela tela, como sempre foi
            if pendentes_meus:
                log(f"{len(pendentes_meus)} laudo(s) pela tela...")
            while pendentes_meus:
                linhas = pagina.locator(LINHA_SELECTOR)
                for i in range(linhas.count()):
                    if not pendentes_meus:
                        break
                    laudo_id = linhas.nth(i).inner_text().split("\n")[0].strip()
                    if laudo_id not in pendentes_meus:
                        continue
                    numero_proposta, destino, _item_id = pendentes_meus.pop(laudo_id)
                    log(f"Abrindo laudo {laudo_id} (proposta {numero_proposta})...")
                    try:
                        resultado = baixar_um_laudo(pagina, laudo_id, destino)
                        if resultado == "baixado":
                            baixados += 1
                            log(f"   salvo em: {destino}")
                        else:
                            sem_laudo += 1
                            ids_sem_laudo.append(laudo_id)
                            log(f"   [PULADO] Ainda não há laudo publicado para {laudo_id}.")
                    except PlaywrightTimeout as e:
                        erros += 1
                        log(f"   [PULADO - ERRO] {str(e).splitlines()[0]}")
                    except Exception as e:
                        erros += 1
                        log(f"   [PULADO - ERRO INESPERADO] {str(e)}")

                if not pendentes_meus:
                    break

                botao_proxima = pagina.locator("a:visible", has_text="próxima")
                if botao_proxima.count() == 0 or botao_proxima.get_attribute("disabled") is not None:
                    log(
                        f"Cheguei ao fim das páginas com {len(pendentes_meus)} laudo(s) da minha "
                        "lista não encontrados (raro - confira manualmente)."
                    )
                    erros += len(pendentes_meus)
                    break

                try:
                    botao_proxima.click()
                    pagina.wait_for_selector(LINHA_SELECTOR, timeout=15000)
                except Exception as e:
                    # já aconteceu de verdade: um modal preso bloqueando o
                    # clique ("... intercepts pointer events") derrubava o
                    # processo inteiro com código de saída 1, perdendo o
                    # trabalho todo. Tenta fechar o modal e continuar; se
                    # não conseguir, desiste só desta aba.
                    log(f"[AVISO] Erro ao paginar: {str(e).splitlines()[0]}")
                    if fechar_modal_preso(pagina):
                        log("      Modal preso fechado, tentando continuar...")
                        continue
                    log(
                        f"[ERRO] Não consegui paginar - desistindo desta aba com "
                        f"{len(pendentes_meus)} laudo(s) restante(s)."
                    )
                    erros += len(pendentes_meus)
                    break
        except Exception as e:
            # rede de segurança final - nenhum erro não previsto nesta aba
            # pode derrubar o processo inteiro (já aconteceu antes).
            msg = str(e).splitlines()[0] if str(e) else type(e).__name__
            log(f"[ERRO INESPERADO NESTA ABA] {msg}")
            erros += len(pendentes_meus)
        finally:
            navegador.close()

    return (baixados, sem_laudo, erros, ids_sem_laudo)


def dividir_periodo(data_inicio, data_fim, num_partes):
    """Divide o período dd/mm/aaaa em até num_partes sub-períodos contíguos
    (um por aba de coleta) - mesma ideia de dividir trabalho em abas que já
    existe pro download, só que aqui a divisão é por data em vez de por
    lista de laudos (a lista ainda não existe nesse ponto)."""
    inicio = datetime.strptime(data_inicio, "%d/%m/%Y")
    fim = datetime.strptime(data_fim, "%d/%m/%Y")
    total_dias = (fim - inicio).days + 1
    if total_dias <= 1:
        return [(data_inicio, data_fim)]

    num_partes = max(1, min(num_partes, total_dias))
    dias_por_parte = total_dias / num_partes

    periodos = []
    cursor = inicio
    for i in range(num_partes):
        fim_da_parte = inicio + timedelta(days=round((i + 1) * dias_por_parte) - 1)
        if i == num_partes - 1:
            fim_da_parte = fim
        if fim_da_parte < cursor:
            fim_da_parte = cursor
        periodos.append((cursor.strftime("%d/%m/%Y"), fim_da_parte.strftime("%d/%m/%Y")))
        cursor = fim_da_parte + timedelta(days=1)
        if cursor > fim:
            break
    return periodos


def coletar_periodo_paralelo(indice, data_inicio, data_fim, limite, log_lock):
    """Coleta a lista de laudos de um sub-período numa aba própria, em
    paralelo com as outras - mesmo padrão do baixar_lote_paralelo (cada
    aba abre seu próprio navegador Playwright e loga com a sessão salva,
    já validada antes pelo login principal em main())."""
    prefixo = f"  [Coleta {indice + 1}]"

    def log(msg):
        with log_lock:
            print(f"{prefixo} {msg}")

    coletados = []
    with sync_playwright() as p:
        navegador = p.chromium.launch(
            headless=HEADLESS,
            args=[] if HEADLESS else ["--start-maximized"],
        )
        _contexto, pagina = novo_contexto_pagina(navegador, com_sessao=True)
        mapa_itens = MapaItensGrade(pagina)
        try:
            pagina.goto(LOGIN_URL)
            selecionar_cliente_banco_b(pagina)
            if not chegou_no_painel(pagina, timeout=15000):
                log("[ERRO] Não consegui entrar no painel com a sessão salva - abortando esta aba.")
                return coletados

            log(f"Período {data_inicio} a {data_fim}...")
            if not preencher_periodo_e_exibir(pagina, data_inicio, data_fim):
                log("Nenhuma inspeção neste sub-período.")
                return coletados

            pagina_num = 1
            while len(coletados) < limite:
                linhas = pagina.locator(LINHA_SELECTOR)
                textos_linhas = linhas.all_inner_texts()

                novos_da_pagina = 0
                codigos_vistos_pagina = set()
                for texto_linha in textos_linhas:
                    laudo_id = texto_linha.split("\n")[0].strip()
                    if not laudo_id or laudo_id in codigos_vistos_pagina:
                        continue
                    codigos_vistos_pagina.add(laudo_id)
                    numero_proposta = extrair_numero_proposta(texto_linha) or ""
                    # guarda o sub-período junto - o download precisa
                    # refiltrar por ESSE MESMO sub-período estreito depois,
                    # não pelo período inteiro (ver comentário em main())
                    # id do item na API, pro download direto (None = vai pela tela)
                    item_id = mapa_itens.item_de(laudo_id) if DOWNLOAD_API else None
                    coletados.append((numero_proposta, laudo_id, data_inicio, data_fim, item_id))
                    novos_da_pagina += 1

                log(f"Página {pagina_num}: {novos_da_pagina} laudo(s) ({len(coletados)} no total desta aba).")

                if len(coletados) >= limite:
                    break

                botao_proxima = pagina.locator("a:visible", has_text="próxima")
                if botao_proxima.count() == 0 or botao_proxima.get_attribute("disabled") is not None:
                    log("Fim das páginas deste sub-período.")
                    break

                botao_proxima.click()
                pagina_num += 1
                pagina.wait_for_selector(LINHA_SELECTOR, timeout=15000)
        except PlaywrightTimeout as e:
            log(f"[ERRO] Timeout coletando este sub-período: {str(e).splitlines()[0]}")
        except Exception as e:
            log(f"[ERRO INESPERADO] {str(e)}")
        finally:
            navegador.close()

    return coletados


# ---------------------------------------------------------------------------
# Período e lotes
# ---------------------------------------------------------------------------
# O período pedido é quebrado em lotes de no máximo DIAS_POR_LOTE dias, e
# cada lote passa pela coleta + download completos antes do próximo. Por
# quê: a coleta divide o período em PARALELISMO_COLETA sub-períodos, e um
# período de anos daria sub-períodos de meses - e foi com filtro largo
# que a Plataforma perdeu laudos aqui (~29% "não encontrados").
# Com lotes de um mês, cada aba filtra ~5 dias, o mesmo tamanho validado
# em produção. Entre um lote e outro a sessão é conferida de novo (uma
# rodada de anos leva horas; se o login vencer no meio, renova sozinho).
#   $env:DIAS_POR_LOTE="31"   (0 = período inteiro de uma vez, como antes)
# Pra não digitar o período no terminal (ex.: deixar agendado):
#   $env:PERIODO_INICIO="01/01/2023"; $env:PERIODO_FIM="31/12/2023"
DIAS_POR_LOTE = _inteiro_env("DIAS_POR_LOTE", 31, 0, 3650)
FORMATO_DATA = "%d/%m/%Y"


def ler_periodo():
    """(data_inicio, data_fim) em dd/mm/aaaa, das variáveis de ambiente
    PERIODO_INICIO/PERIODO_FIM ou perguntando no terminal. Devolve None
    (com a mensagem já impressa) se a data for inválida ou invertida."""
    data_inicio = os.getenv("PERIODO_INICIO", "").strip()
    data_fim = os.getenv("PERIODO_FIM", "").strip()
    if data_inicio and data_fim:
        print(f"Período das variáveis PERIODO_INICIO/PERIODO_FIM: {data_inicio} até {data_fim}")
    else:
        data_inicio = pedir_dado("Data inicial (dd/mm/aaaa)")
        data_fim = pedir_dado("Data final (dd/mm/aaaa)")
        print(f"Período usado: {data_inicio} até {data_fim}")  # input() não vai pro log sozinho
    try:
        inicio = datetime.strptime(data_inicio, FORMATO_DATA)
        fim = datetime.strptime(data_fim, FORMATO_DATA)
    except ValueError:
        print("\n[ERRO] Data inválida - use o formato dd/mm/aaaa (ex.: 01/03/2024).")
        return None
    if fim < inicio:
        print("\n[ERRO] A data final vem antes da inicial.")
        return None
    if fim > datetime.now() + timedelta(days=1):
        print("\n[ERRO] A data final está no futuro.")
        return None
    return inicio.strftime(FORMATO_DATA), fim.strftime(FORMATO_DATA)


def dividir_em_lotes(data_inicio, data_fim, dias_por_lote):
    """[(ini, fim)] contíguos, cada um com no máximo dias_por_lote dias.
    dias_por_lote <= 0 devolve o período inteiro num lote só."""
    inicio = datetime.strptime(data_inicio, FORMATO_DATA)
    fim = datetime.strptime(data_fim, FORMATO_DATA)
    if dias_por_lote <= 0:
        return [(data_inicio, data_fim)]
    lotes = []
    cursor = inicio
    while cursor <= fim:
        fim_lote = min(cursor + timedelta(days=dias_por_lote - 1), fim)
        lotes.append((cursor.strftime(FORMATO_DATA), fim_lote.strftime(FORMATO_DATA)))
        cursor = fim_lote + timedelta(days=1)
    return lotes


def garantir_sessao(pausar_no_fim):
    """Abre um navegador, garante sessão válida (preparar_sessao) e fecha.
    `pausar_no_fim`: com janela visível, espera ENTER antes de fechar (só
    faz sentido na primeira vez, pra pessoa ver o que aconteceu)."""
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=HEADLESS,
            slow_mo=0 if MODO_RAPIDO else 300,
            args=[] if HEADLESS else ["--start-maximized"],
        )
        sessao_ok = False
        try:
            sessao_ok, _context = preparar_sessao(browser)
        except Exception as e:
            print("\n[ERRO] Falha verificando a sessão: " + str(e))
        finally:
            if not HEADLESS and pausar_no_fim:
                print("\n" + "#" * 60)
                print("#  A AÇÃO É SUA AGORA - O ROBÔ ESTÁ PAUSADO")
                print("#" * 60)
                input(">>> Pressione ENTER aqui para fechar o navegador... ")
            browser.close()
    return sessao_ok


def processar_lote(data_inicio, data_fim, cache_sem_laudo, hoje, rotulo):
    """Coleta + download de um lote de datas. Devolve um dict com os
    totais (e salva o cache de "sem laudo" no fim, pra um Ctrl+C no lote
    seguinte não perder o que este já descobriu)."""
    t = {"baixados": 0, "via_api": 0, "sem_laudo": 0, "erros": 0,
         "ja_existiam": 0, "sem_laudo_recente": 0, "pendentes": 0,
         "tempos_lista": [], "tempos_pdf": [], "duracao_coleta": 0.0, "duracao_download": 0.0}
    pendentes = []

    # coleta a lista de laudos do período em paralelo - divide o
    # intervalo de datas em sub-períodos e cada um roda numa aba própria
    # (mesmo princípio do download em paralelo mais abaixo), em vez de
    # paginar o período inteiro numa única aba sequencial
    inicio_coleta = time.time()
    try:
        sub_periodos = dividir_periodo(data_inicio, data_fim, PARALELISMO_COLETA)
        print(f"\n{rotulo} Coletando lista de laudos de {data_inicio} a {data_fim} em paralelo "
              f"({len(sub_periodos)} sub-período(s), sem baixar ainda)...")
        for ini, fim_sub in sub_periodos:
            print(f"  {ini} a {fim_sub}")

        log_lock_coleta = threading.Lock()
        todos_coletados = []
        with ThreadPoolExecutor(max_workers=len(sub_periodos)) as executor:
            futuros = [
                executor.submit(coletar_periodo_paralelo, i, ini, fim_sub, LIMITE_TESTE, log_lock_coleta)
                for i, (ini, fim_sub) in enumerate(sub_periodos)
            ]
            for futuro in as_completed(futuros):
                todos_coletados.extend(futuro.result())

        # dedup pelo código do laudo - os sub-períodos são contíguos e
        # não deveriam se sobrepor, mas não custa garantir
        codigos_vistos = set()
        for numero_proposta, laudo_id, di_sub, df_sub, item_id in todos_coletados:
            if laudo_id in codigos_vistos:
                continue
            codigos_vistos.add(laudo_id)
            destino = os.path.join(DOWNLOAD_DIR, f"laudo_{laudo_id}.pdf")
            if os.path.exists(destino):
                t["ja_existiam"] += 1
                continue
            if checado_recentemente(cache_sem_laudo, laudo_id, hoje):
                t["sem_laudo_recente"] += 1
                continue
            pendentes.append((numero_proposta, laudo_id, destino, di_sub, df_sub, item_id))

        pendentes = pendentes[:LIMITE_TESTE]
        t["pendentes"] = len(pendentes)
        print(f"\n  {len(pendentes)} laudo(s) pendente(s) pra baixar, {t['ja_existiam']} já existiam.")
        if pendentes and DOWNLOAD_API:
            com_item = sum(1 for p_ in pendentes if p_[5])
            print(f"  {com_item} de {len(pendentes)} com o item da API identificado (download direto); "
                  f"o resto vai pela tela.")
            if com_item == 0:
                print("  [AVISO] Não achei o código dos laudos nos dados da grade - vai tudo pela "
                      "tela, como antes (mais lento). Vale avisar pra investigar.")
        if t["sem_laudo_recente"]:
            print(f"  {t['sem_laudo_recente']} pulado(s) por já terem sido checados sem laudo publicado "
                  f"nos últimos {RECHECAR_SEM_LAUDO_DIAS} dia(s) "
                  f'($env:RECHECAR_SEM_LAUDO_DIAS="0" pra checar de novo).')

    except Exception as e:
        print("\n[ERRO INESPERADO NA COLETA]")
        print(str(e))
        pendentes = []

    t["duracao_coleta"] = time.time() - inicio_coleta
    print(f"\n  Tempo da coleta: {formatar_duracao(t['duracao_coleta'])}")

    inicio_download = time.time()
    if pendentes:
        # agrupa por sub-período de origem (o mesmo em que a coleta
        # achou cada laudo) e abre uma aba de download por grupo,
        # refiltrando por ESSE sub-período estreito em vez do período
        # inteiro. Filtrar pelos 6 meses inteiros na tela de download
        # perdia laudos que só apareciam com um filtro mais estreito -
        # confirmado numa rodada real (~29% "não encontrados" - o site
        # parece truncar/paginar diferente pra um filtro largo).
        grupos = sorted({(di_sub, df_sub) for _n, _l, _d, di_sub, df_sub, _i in pendentes})
        fila = FilaApi(
            [((di_sub, df_sub), numero_proposta, laudo_id, destino, item_id)
             for numero_proposta, laudo_id, destino, di_sub, df_sub, item_id in pendentes],
            len(grupos),
        )
        downloads_por_aba = max(1, -(-DOWNLOADS_SIMULTANEOS // len(grupos)))  # arredonda pra cima

        print(f"\nBaixando {len(pendentes)} laudo(s) em {len(grupos)} aba(s) em paralelo "
              f"(uma por sub-período, mesmo filtro usado na coleta), "
              f"{downloads_por_aba * len(grupos)} download(s) simultâneo(s) pela API...")
        log_lock = threading.Lock()

        try:
            with ThreadPoolExecutor(max_workers=len(grupos)) as executor:
                futuros = [
                    executor.submit(baixar_lote_paralelo, i, grupo, fila, downloads_por_aba, log_lock)
                    for i, grupo in enumerate(grupos)
                ]
                for futuro in as_completed(futuros):
                    baixados, sem_laudo, erros, ids_sem_laudo = futuro.result()
                    t["baixados"] += baixados
                    t["sem_laudo"] += sem_laudo
                    t["erros"] += erros
                    for laudo_id in ids_sem_laudo:
                        cache_sem_laudo[laudo_id] = hoje.isoformat()
            t["via_api"] = fila.baixados
            t["baixados"] += fila.baixados
            t["sem_laudo"] += fila.sem_laudo
            for laudo_id in fila.ids_sem_laudo:
                cache_sem_laudo[laudo_id] = hoje.isoformat()
            t["tempos_lista"], t["tempos_pdf"] = fila.tempos_lista, fila.tempos_pdf
        except Exception as e:
            # rede de segurança final: mesmo com as abas já protegidas
            # individualmente, nada aqui pode matar o processo com
            # código de saída != 0 (já aconteceu - o rodar_pipeline.py
            # aborta o extractor quando isso acontece)
            print("\n[ERRO INESPERADO NO DOWNLOAD]")
            print(str(e))

    salvar_cache_sem_laudo(cache_sem_laudo)
    t["duracao_download"] = time.time() - inicio_download
    return t


def main():
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    os.makedirs(LOG_DIR, exist_ok=True)

    log_path = os.path.join(LOG_DIR, f"execucao_{datetime.now():%Y%m%d_%H%M%S}.txt")
    log_file = open(log_path, "w", encoding="utf-8")
    stdout_original = sys.stdout
    sys.stdout = Tee(stdout_original, log_file)

    inicio = time.time()
    try:
        print("=" * 60)
        print(" ROBÔ DE DOWNLOAD - PLATAFORMA")
        print("=" * 60)
        print(f"Log desta execução: {log_path}")
        periodo = ler_periodo()
        if not periodo:
            return
        data_inicio, data_fim = periodo

        hoje = date.today()
        # entrada de laudo que já foi baixado não serve mais pra nada
        cache_sem_laudo = {
            laudo_id: data for laudo_id, data in carregar_cache_sem_laudo().items()
            if not os.path.exists(os.path.join(DOWNLOAD_DIR, f"laudo_{laudo_id}.pdf"))
        }

        lotes = dividir_em_lotes(data_inicio, data_fim, DIAS_POR_LOTE)
        if len(lotes) > 1:
            print(f"\nPeríodo grande: vai em {len(lotes)} lotes de até {DIAS_POR_LOTE} dias, um de cada vez "
                  f"({lotes[0][0]} a {lotes[0][1]}, depois {lotes[1][0]} a {lotes[1][1]}, ...). "
                  f'Pra mudar o tamanho: $env:DIAS_POR_LOTE="{DIAS_POR_LOTE}".')

        totais = {"baixados": 0, "via_api": 0, "sem_laudo": 0, "erros": 0,
                  "ja_existiam": 0, "sem_laudo_recente": 0,
                  "duracao_coleta": 0.0, "duracao_download": 0.0}
        tempos_lista, tempos_pdf = [], []
        lotes_feitos = 0
        for n, (ini_lote, fim_lote) in enumerate(lotes, 1):
            rotulo = f"[Lote {n}/{len(lotes)}]" if len(lotes) > 1 else "[2/2]"
            if n == 1:
                print("\n[1/2] Verificando sessão salva...")
            else:
                print(f"\n{rotulo} Conferindo a sessão antes do lote {ini_lote} a {fim_lote}...")
            if not garantir_sessao(pausar_no_fim=(n == 1)):
                if n > 1:
                    print(f"\n[ERRO] A sessão venceu no meio e não consegui renovar. Lotes já feitos: {n - 1} "
                          f"de {len(lotes)}. Pra continuar de onde parou:")
                    print(f'  $env:PERIODO_INICIO="{ini_lote}"; $env:PERIODO_FIM="{data_fim}"; '
                          f"python {os.path.basename(__file__)}")
                break

            t = processar_lote(ini_lote, fim_lote, cache_sem_laudo, hoje, rotulo)
            lotes_feitos += 1
            for chave in totais:
                totais[chave] += t[chave]
            tempos_lista += t["tempos_lista"]
            tempos_pdf += t["tempos_pdf"]
            if len(lotes) > 1:
                print(f"\n{rotulo} {ini_lote} a {fim_lote}: {t['baixados']} baixado(s) "
                      f"({t['via_api']} pela API), {t['ja_existiam']} já existiam, "
                      f"{t['sem_laudo']} sem laudo, {t['erros']} erro(s) - "
                      f"{formatar_duracao(t['duracao_coleta'] + t['duracao_download'])}. "
                      f"Acumulado: {totais['baixados']} baixado(s) em {lotes_feitos} lote(s).")

        decorrido = time.time() - inicio
        print("\nConcluído!" if lotes_feitos == len(lotes) else f"\nParado em {lotes_feitos} de {len(lotes)} lote(s).")
        print("-" * 60)
        if len(lotes) > 1:
            print(f"  Lotes processados:         {lotes_feitos} de {len(lotes)}")
        print(f"  Baixados agora:            {totais['baixados']}")
        print(f"    direto pela API:         {totais['via_api']}")
        print(f"    pela tela:               {totais['baixados'] - totais['via_api']}")
        print(f"  Já existiam (pulados):     {totais['ja_existiam']}")
        print(f"  Sem laudo publicado ainda: {totais['sem_laudo']}")
        print(f"  Sem laudo, checado há pouco (pulados): {totais['sem_laudo_recente']}")
        print(f"  Com erro (pulados):        {totais['erros']}")
        print(f"  Tempo da coleta:           {formatar_duracao(totais['duracao_coleta'])}")
        print(f"  Tempo do download:         {formatar_duracao(totais['duracao_download'])}")
        print(f"  Tempo total:               {formatar_duracao(decorrido)}")
        if totais["baixados"]:
            print(f"  Média por laudo baixado:   {totais['duracao_download'] / totais['baixados']:.1f}s")
        if tempos_pdf:
            # quanto cada chamada leva na Plataforma (não o total da execução,
            # que depende de quantos downloads rodam ao mesmo tempo)
            print(f"  Chamada da API (mediana):  lista {statistics.median(tempos_lista):.1f}s, "
                  f"PDF {statistics.median(tempos_pdf):.1f}s "
                  f"(PDF mais lento: {max(tempos_pdf):.1f}s)")
        print(f"  Pasta: {DOWNLOAD_DIR}")
        print("-" * 60)

    finally:
        sys.stdout = stdout_original
        log_file.close()


if __name__ == "__main__":
    main()
