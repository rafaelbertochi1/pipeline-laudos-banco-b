"""
Calcula latitude/longitude pelo endereço dos laudos que NÃO trazem a
coordenada no PDF (o digital/AVM até meados de 2025 não imprime).

Uso: python banco_b_geocodificar.py   (etapa 4 do rodar_pipeline.py)

Estratégia (testada em 200 laudos que têm a coordenada certa no PDF:
erro típico de ~180 m, 2 em cada 3 a menos de 500 m, ~8% a mais de 5 km -
dá ideia da região, não do imóvel exato):
  1. OpenStreetMap (Nominatim) pelo número e rua;
  2. OpenStreetMap só pela rua;
  3. pelo CEP (AwesomeAPI, base brasileira de CEPs);
  4. OpenStreetMap só pela cidade.
A coluna `origem_coordenada` diz de onde veio cada coordenada: "laudo"
(impressa no PDF) ou "calculada_numero" / "calculada_rua" /
"calculada_cep" / "calculada_cidade" - pra filtrar só as precisas.

Só manda pros serviços endereço, número, cidade, UF e CEP (nunca nome,
CPF ou valores). O Nominatim exige no máximo 1 consulta por segundo e
cache dos resultados: por isso roda em sequência e guarda cada consulta
(inclusive as que não acharam nada) em data/laudos/_geocodificacao.json -
endereço já consultado nunca é consultado de novo.

Variáveis de ambiente:
  $env:GEOCODIFICAR_LIMITE="30"   só os primeiros N laudos (teste)
  $env:GEOCODIFICAR_SIMULAR="1"   não grava no banco, só mostra
"""

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from datetime import datetime

import psycopg2

PASTA_SCRIPT = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(PASTA_SCRIPT, "data", "laudos", "_geocodificacao.json")
LOG_DIR = os.path.join(PASTA_SCRIPT, "logs")
AGENTE = "CentralGestao-LaudosBancoB/1.0 (geocodificacao de laudos)"
INTERVALO_NOMINATIM = 1.1  # política do Nominatim: máx. 1 consulta/s
INTERVALO_CEP = 0.5
UFS = {"AC": "Acre", "AL": "Alagoas", "AP": "Amapá", "AM": "Amazonas", "BA": "Bahia", "CE": "Ceará",
       "DF": "Distrito Federal", "ES": "Espírito Santo", "GO": "Goiás", "MA": "Maranhão",
       "MT": "Mato Grosso", "MS": "Mato Grosso do Sul", "MG": "Minas Gerais", "PA": "Pará",
       "PB": "Paraíba", "PR": "Paraná", "PE": "Pernambuco", "PI": "Piauí", "RJ": "Rio de Janeiro",
       "RN": "Rio Grande do Norte", "RS": "Rio Grande do Sul", "RO": "Rondônia", "RR": "Roraima",
       "SC": "Santa Catarina", "SP": "São Paulo", "SE": "Sergipe", "TO": "Tocantins"}


class Tee:
    def __init__(self, *streams):
        self.streams = streams

    def write(self, dado):
        for s in self.streams:
            s.write(dado)

    def flush(self):
        for s in self.streams:
            s.flush()


class ServicoFora(Exception):
    """O serviço recusou ou não respondeu várias vezes - para a execução
    (sem gravar "não achado" no cache, pra tentar de novo na próxima)."""


class Geocodificador:
    def __init__(self):
        self.cache = self._carregar()
        self._ultima = {"osm": 0.0, "cep": 0.0}
        self.consultas = Counter()

    @staticmethod
    def _carregar():
        try:
            with open(CACHE, encoding="utf-8") as f:
                dados = json.load(f)
            return dados if isinstance(dados, dict) else {}
        except (OSError, ValueError):
            return {}

    def salvar(self):
        temporario = CACHE + ".tmp"
        with open(temporario, "w", encoding="utf-8") as f:
            json.dump(self.cache, f, ensure_ascii=False)
        os.replace(temporario, CACHE)

    def _get(self, servico, url, intervalo):
        for tentativa in range(4):
            espera = intervalo - (time.time() - self._ultima[servico])
            if espera > 0:
                time.sleep(espera)
            pedido = urllib.request.Request(url, headers={"User-Agent": AGENTE, "Accept-Language": "pt-BR"})
            try:
                with urllib.request.urlopen(pedido, timeout=30) as r:
                    dados = json.loads(r.read())
                self._ultima[servico] = time.time()
                self.consultas[servico] += 1
                return dados
            except urllib.error.HTTPError as e:
                self._ultima[servico] = time.time()
                if e.code in (400, 404):
                    return None  # CEP inexistente etc.: resposta de "não achei"
                motivo = f"HTTP {e.code}"
            except (urllib.error.URLError, OSError, ValueError) as e:
                self._ultima[servico] = time.time()
                motivo = type(e).__name__
            # 429 (excesso), 5xx ou rede: espera cada vez mais antes de insistir
            print(f"  [AVISO] {servico}: {motivo} - esperando {30 * (tentativa + 1)}s...")
            time.sleep(30 * (tentativa + 1))
        raise ServicoFora(f"{servico} não respondeu depois de 4 tentativas")

    def _consultar(self, chave, servico, url, intervalo, extrair):
        if chave in self.cache:
            return self.cache[chave]
        resposta = self._get(servico, url, intervalo)
        ponto = extrair(resposta) if resposta else None
        self.cache[chave] = ponto
        return ponto

    def _osm(self, **params):
        params.update(format="jsonv2", limit=1, countrycodes="br")
        url = "https://nominatim.openstreetmap.org/search?" + urllib.parse.urlencode(params)
        chave = "osm|" + json.dumps(params, sort_keys=True, ensure_ascii=False)
        return self._consultar(chave, "osm", url, INTERVALO_NOMINATIM,
                               lambda r: [float(r[0]["lat"]), float(r[0]["lon"])] if r else None)

    def _cep(self, cep):
        def extrair(r):
            if isinstance(r, dict) and r.get("lat") and r.get("lng"):
                return [float(r["lat"]), float(r["lng"])]
            return None
        return self._consultar("cep|" + cep, "cep", f"https://cep.awesomeapi.com.br/json/{cep}",
                               INTERVALO_CEP, extrair)

    def ponto(self, endereco, numero, municipio, uf, cep):
        """(nivel, lat, lon) ou (None, None, None)."""
        estado = UFS.get((uf or "").upper().strip(), uf or "")
        endereco, municipio = (endereco or "").strip(), (municipio or "").strip()
        numero = (numero or "").strip()
        cep = "".join(ch for ch in (cep or "") if ch.isdigit())
        cep = cep.zfill(8) if 7 <= len(cep) <= 8 else ""
        if numero and numero.upper() not in ("S/N", "SN", "0"):
            p = self._osm(street=f"{numero} {endereco}", city=municipio, state=estado)
            if p:
                return ("calculada_numero", *p)
        if endereco:
            p = self._osm(street=endereco, city=municipio, state=estado)
            if p:
                return ("calculada_rua", *p)
        if cep:
            p = self._cep(cep)
            if p:
                return ("calculada_cep", *p)
        if municipio:
            p = self._osm(city=municipio, state=estado)
            if p:
                return ("calculada_cidade", *p)
        return None, None, None


def conectar():
    return psycopg2.connect(
        host=os.getenv("PGURL", "127.0.0.1"), dbname=os.getenv("PGNAME", "testdb"),
        user=os.getenv("PGUSR", "postgres"), password=os.getenv("PGPASS", "postgres"),
        port=os.getenv("PGPORT", "5432"),
    )


TRAVA = os.path.join(PASTA_SCRIPT, "data", "laudos", "_geocodificacao.lock")


def pegar_trava():
    """Uma execução por vez: duas juntas passariam do limite de 1
    consulta/s do OpenStreetMap. O rodar_pipeline.py deixa esta etapa
    rodando em segundo plano, então o próximo ciclo pode chegar aqui com
    a anterior ainda rodando - espera ela terminar (a anterior já busca
    os laudos novos antes de sair, então esta normalmente sai logo).
    A trava some sozinha quando o processo termina, mesmo se cair."""
    arquivo = open(TRAVA, "a+")
    avisou = False
    while True:
        try:
            if sys.platform == "win32":
                import msvcrt
                arquivo.seek(0)
                msvcrt.locking(arquivo.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(arquivo, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return arquivo
        except OSError:
            if not avisou:
                print("Outra execução das coordenadas ainda está rodando - esperando ela terminar...", flush=True)
                avisou = True
            time.sleep(30)


def buscar_pendentes(conn, ja_vistos):
    with conn, conn.cursor() as cur:
        cur.execute("""
            SELECT codigo_laudo, endereco, numero, municipio, uf, cep FROM laudos
            WHERE latitude IS NULL AND codigo_laudo IS NOT NULL
              AND COALESCE(municipio, '') <> ''
            ORDER BY codigo_laudo
        """)
        return [linha for linha in cur.fetchall() if linha[0] not in ja_vistos]


def main():
    limite = int(os.getenv("GEOCODIFICAR_LIMITE") or 0)
    simular = os.getenv("GEOCODIFICAR_SIMULAR", "0") == "1"
    inicio = time.time()

    conn = conectar()
    if not simular:
        with conn, conn.cursor() as cur:
            cur.execute("ALTER TABLE laudos ADD COLUMN IF NOT EXISTS origem_coordenada TEXT;")
            # laudos gravados antes desta coluna existir
            cur.execute("UPDATE laudos SET origem_coordenada = 'laudo' "
                        "WHERE latitude IS NOT NULL AND origem_coordenada IS NULL;")
    ja_vistos = set()
    pendentes = buscar_pendentes(conn, ja_vistos)
    if limite:
        pendentes = pendentes[:limite]
    print(f"Laudos sem coordenada no PDF: {len(pendentes)}"
          + (" (SIMULAÇÃO - não grava no banco)" if simular else ""))

    geo = Geocodificador()
    niveis = Counter()
    lote = []

    def gravar():
        if simular or not lote:
            lote.clear()
            return
        with conn, conn.cursor() as cur:
            cur.executemany(
                "UPDATE laudos SET latitude = %s, longitude = %s, coordenadas = %s, origem_coordenada = %s "
                "WHERE codigo_laudo = %s AND latitude IS NULL;", lote)
        lote.clear()

    try:
        while pendentes:
            for i, (codigo, endereco, numero, municipio, uf, cep) in enumerate(pendentes, 1):
                ja_vistos.add(codigo)
                nivel, lat, lon = geo.ponto(endereco, numero, municipio, uf, cep)
                niveis[nivel or "não achado"] += 1
                if nivel:
                    lote.append((lat, lon, f"{lat}, {lon}", nivel, codigo))
                if simular and i <= 30:
                    print(f"  {codigo}: {nivel or 'não achado'}")
                if i % 50 == 0:
                    gravar()
                    geo.salvar()
                    print(f"  ... {i}/{len(pendentes)} ({dict(niveis)})", flush=True)
            gravar()
            if limite:
                break
            # rodando em segundo plano, o próximo ciclo pode ter gravado
            # laudos novos enquanto isto rodava - pega eles antes de sair
            pendentes = buscar_pendentes(conn, ja_vistos)
            if pendentes:
                print(f"Laudos novos gravados enquanto isto rodava: {len(pendentes)}", flush=True)
    except ServicoFora as e:
        print(f"[ERRO] {e} - parei aqui; o que já foi feito está gravado, rode de novo mais tarde.")
    finally:
        gravar()
        geo.salvar()
        conn.close()

    print("-" * 60)
    for nivel in ("calculada_numero", "calculada_rua", "calculada_cep", "calculada_cidade", "não achado"):
        print(f"  {nivel}:".ljust(22) + str(niveis.get(nivel, 0)))
    print(f"  Consultas feitas agora: OpenStreetMap {geo.consultas['osm']}, CEP {geo.consultas['cep']} "
          f"(o resto veio do cache)")
    print(f"  Tempo: {int(time.time() - inicio)}s")
    print("-" * 60)


if __name__ == "__main__":
    os.makedirs(LOG_DIR, exist_ok=True)
    log_path = os.path.join(LOG_DIR, f"geocodificacao_{datetime.now():%Y%m%d_%H%M%S}.txt")
    log_file = open(log_path, "w", encoding="utf-8")
    stdout_original = sys.stdout
    sys.stdout = Tee(stdout_original, log_file)
    print(f"Log desta execução: {log_path}")
    # a primeira execução leva horas (1 consulta/s): não deixa o Windows
    # suspender no meio - mesmo pedido que o extrator faz, só enquanto roda
    if sys.platform == "win32":
        import ctypes
        ctypes.windll.kernel32.SetThreadExecutionState(0x80000000 | 0x00000001)
    try:
        _trava = pegar_trava()  # segura até o processo sair
        main()
    finally:
        sys.stdout = stdout_original
        log_file.close()
