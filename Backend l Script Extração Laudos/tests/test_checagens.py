"""Validação do lote antes de gravar (checagens.py e validar_lote).

Se uma checagem passa do limite, o lote inteiro não é gravado: é o que
impede um layout novo de PDF de encher o banco de dados errados."""
from decimal import Decimal

import pytest

import checagens
from banco_b_extractor import validar_lote


def laudo(**mudancas):
    base = {
        "path": "laudo_1.pdf",
        "codigo_laudo": "L1",
        "numero_proposta": "123456",
        "tipo_imovel": "Apartamento",
        "valor_mercado": Decimal("500000"),
        "valor_venda_forcada": Decimal("400000"),
        "area_privativa_m2": Decimal("80"),
        "area_terreno_m2": Decimal("0"),
        "municipio": "São Paulo",
        "uf": "SP",
        "endereco": "Rua A",
        "metodologia": "Comparativo",
        "modelo_usado": "fisico",
        "padrao_acabamento": "Normal",
        "estado_conservacao": "Nova(até 5 anos)",
    }
    base.update(mudancas)
    return base


def amostra(**mudancas):
    base = {
        "codigo_laudo": "L1",
        "numero_amostra": 1,
        "tipo_imovel": "Apartamento",
        "valor": Decimal("450000"),
        "area_privativa_m2": Decimal("75"),
        "quartos": 2,
        "banheiros": 1,
        "padrao_acabamento": "Normal",
        "estado_conservacao": "Regular",
    }
    base.update(mudancas)
    return base


def nomes(problemas):
    return {checagem for checagem, _ in problemas}


@pytest.mark.parametrize("valor, eh_estado, esperado", [
    ("Normal", False, False),
    ("4 Regular 60 6,67 0,056 20", False, True),   # pedaço da tabela de depreciação
    ("Nova(até 5 anos)", True, False),             # único estado legítimo com número
    ("Nova(até 5 anos)", False, True),
    ("x" * 41, False, True),
    (None, False, False),
])
def test_texto_contaminado(valor, eh_estado, esperado):
    assert checagens.texto_contaminado(valor, eh_estado) is esperado


def test_laudo_correto_nao_tem_problemas():
    assert checagens.problemas_dos_dados(laudo(), [amostra()]) == []


@pytest.mark.parametrize("mudanca, checagem", [
    ({"endereco": ""}, "sem endereço"),
    ({"codigo_laudo": None}, "sem código de laudo"),
    ({"area_privativa_m2": 0, "area_terreno_m2": 0}, "sem área (privativa e terreno zeradas)"),
    ({"valor_venda_forcada": Decimal("475000")}, "venda forçada fora de 50-90% da avaliação"),
    ({"modelo_usado": "digital"}, "modelo não bate com a metodologia"),
    ({"padrao_acabamento": "4 Regular 60"}, "padrão/estado com pedaço de outra coluna"),
])
def test_cada_problema_e_detectado(mudanca, checagem):
    assert checagem in nomes(checagens.problemas_dos_dados(laudo(**mudanca), [amostra()]))


def test_amostras_com_problema():
    problemas = nomes(checagens.problemas_dos_dados(laudo(), [amostra(quartos=20, valor=0)]))
    assert "amostra com quartos/banheiros acima de 15" in problemas
    assert "amostra sem tipo, valor ou área" in problemas
    assert "laudo sem nenhuma amostra" in nomes(checagens.problemas_dos_dados(laudo(), []))


@pytest.mark.parametrize("contagem, estourou", [
    ({"sem endereço": 9}, True),             # limite 5% de 100 + folga de 3 = 8
    ({"sem endereço": 8}, False),
    ({"erro ao processar o PDF": 4}, True),  # bug conhecido: limite 0 + folga 3
    ({"erro ao processar o PDF": 3}, False),
    ({"checagem nova": 4}, True),            # checagem sem limite cadastrado conta como 0
])
def test_checagens_estouradas(contagem, estourou):
    assert bool(checagens.checagens_estouradas(contagem, 100)) is estourou


def test_lote_bom_e_gravado():
    dados = [laudo(path=f"l{i}.pdf", codigo_laudo=f"L{i}") for i in range(20)]
    amostras = [amostra(codigo_laudo=f"L{i}") for i in range(20)]
    assert validar_lote(dados, amostras) is True


def test_layout_novo_bloqueia_o_lote(monkeypatch):
    monkeypatch.delenv("VALIDACAO_IGNORAR", raising=False)
    dados = [laudo(path=f"l{i}.pdf", codigo_laudo=f"L{i}", endereco="") for i in range(20)]
    amostras = [amostra(codigo_laudo=f"L{i}") for i in range(20)]
    assert validar_lote(dados, amostras) is False


def test_variavel_de_ambiente_permite_gravar_mesmo_assim(monkeypatch):
    monkeypatch.setenv("VALIDACAO_IGNORAR", "1")
    dados = [laudo(path=f"l{i}.pdf", codigo_laudo=f"L{i}", endereco="") for i in range(20)]
    assert validar_lote(dados, [amostra(codigo_laudo=f"L{i}") for i in range(20)]) is True
