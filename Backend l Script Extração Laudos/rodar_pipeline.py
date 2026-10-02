"""
Roda o pipeline completo (download + extração + imagens + coordenadas) numa execução só e
mostra um mini relatório de tempo de cada etapa no final.

Uso: python rodar_pipeline.py

Continua pedindo a data inicial/final no terminal (é o
plataforma_downloader.py rodando por baixo) - só não precisa mais rodar
os dois comandos separados nem calcular o tempo de cada um na mão.
Pra não digitar as datas:
    $env:PERIODO_INICIO="01/01/2024"; $env:PERIODO_FIM="31/12/2024"
Período grande vai em lotes de até DIAS_POR_LOTE dias (padrão 31), um de
cada vez, e a extração só começa depois que o download inteiro termina.
"""

import os
import subprocess
import sys
import time

def formatar_duracao(segundos):
    minutos, segundos = divmod(int(segundos), 60)
    horas, minutos = divmod(minutos, 60)
    if horas:
        return f"{horas}h{minutos:02d}min{segundos:02d}s"
    return f"{minutos}min{segundos:02d}s"

def rodar_etapa(titulo, script):
    print("=" * 60)
    print(f" {titulo}")
    print("=" * 60)
    inicio = time.time()
    # usa o mesmo interpretador Python que está rodando este script
    # (respeita venv/ambiente ativo, em vez de assumir "python" no PATH)
    resultado = subprocess.run([sys.executable, script])
    duracao = time.time() - inicio

    if resultado.returncode != 0:
        print(f"\n[ERRO] {script} terminou com código {resultado.returncode} "
              f"após {formatar_duracao(duracao)}.")
        sys.exit(resultado.returncode)

    return duracao

def main():
    inicio_total = time.time()

    tempo_downloader = rodar_etapa(
        "1/4 - BAIXANDO LAUDOS (Plataforma)", "plataforma_downloader.py"
    )
    tempo_extractor = rodar_etapa(
        "2/4 - EXTRAINDO E GRAVANDO NO BANCO", "banco_b_extractor.py"
    )
    # o extrator já salva as imagens dos laudos novos; esta etapa passa
    # pela pasta toda (tira foto errada, completa o que faltar). Só abre
    # PDF ainda não verificado, então depois da primeira vez é rápida.
    tempo_imagens = rodar_etapa(
        "3/4 - IMAGENS (fachada e cômodos)", "banco_b_imagens.py"
    )
    # coordenada pelo endereço só pros laudos que não trazem no PDF. É a
    # etapa mais demorada (o OpenStreetMap aceita 1 consulta/s) e não
    # depende de nada depois dela, então roda em segundo plano: o pipeline
    # termina e dá pra começar o próximo ciclo. Pra esperar ela terminar
    # aqui, como antes: $env:COORDENADAS_ESPERAR="1"
    if os.getenv("COORDENADAS_ESPERAR", "0") == "1":
        tempo_coordenadas = rodar_etapa(
            "4/4 - COORDENADAS (laudos sem coordenada no PDF)", "banco_b_geocodificar.py"
        )
    else:
        print("=" * 60)
        print(" 4/4 - COORDENADAS (em segundo plano)")
        print("=" * 60)
        opcoes = {}
        if sys.platform == "win32":
            # processo solto, sem janela: continua depois que este terminar
            opcoes["creationflags"] = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
        else:
            opcoes["start_new_session"] = True
        subprocess.Popen([sys.executable, "banco_b_geocodificar.py"], stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True, **opcoes)
        print("  Rodando por trás - acompanhe em logs\\geocodificacao_<data>.txt. Pode fechar")
        print("  este terminal e começar o próximo ciclo; se ela ainda estiver rodando,")
        print("  a próxima espera ela terminar em vez de rodar junto.")
        tempo_coordenadas = None

    tempo_total = time.time() - inicio_total

    print("\n" + "#" * 60)
    print("#  MINI RELATÓRIO - PIPELINE COMPLETO")
    print("#" * 60)
    print(f"  Download (Plataforma):      {formatar_duracao(tempo_downloader)}")
    print(f"  Extração (banco de dados): {formatar_duracao(tempo_extractor)}")
    print(f"  Imagens:                   {formatar_duracao(tempo_imagens)}")
    print("  Coordenadas:               "
          + (formatar_duracao(tempo_coordenadas) if tempo_coordenadas is not None else "em segundo plano"))
    print(f"  Tempo total:               {formatar_duracao(tempo_total)}")
    print("#" * 60)

if __name__ == "__main__":
    main()
