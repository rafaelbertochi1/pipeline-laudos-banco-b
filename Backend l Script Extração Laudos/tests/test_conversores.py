"""Conversões de texto do PDF para os tipos do banco (banco_b_extractor.py).

Cada caso vem de um formato que aparece de verdade nos laudos."""
from decimal import Decimal

import pytest

from banco_b_extractor import (
    _dms_para_decimal,
    calcular_valor_unitario,
    converter_area_plataforma,
    converter_float_coordenada,
    converter_float_seguro,
    converter_int_seguro,
    extrair_coordenadas_generico,
    extrair_data_avaliacao,
    extrair_endereco_numero,
    extrair_numero_proposta,
    limpar_txt,
)


@pytest.mark.parametrize("entrada, esperado", [
    ("1.234,56", "1234.56"),          # formato brasileiro
    ("R$ 450.000,00", "450000.00"),   # com símbolo de moeda
    ("1.234.567", "1234567.00"),      # só separador de milhar
    ("8666.3", "8666.30"),
    (8666.299999999999, "8666.30"),   # float com sobra binária vira Decimal limpo
    ("95,30", "95.30"),
    (None, "0.00"),
    ("-", "0.00"),
    ("NULL", "0.00"),
    ("sem número", "0.00"),
])
def test_converter_float_seguro(entrada, esperado):
    assert converter_float_seguro(entrada) == Decimal(esperado)


@pytest.mark.parametrize("entrada, esperado", [
    ("61.800", "61.80"),     # laudo antigo: ponto decimal (antes virava 61800 m²)
    ("216.04", "216.04"),
    ("95,30", "95.30"),      # laudo novo: vírgula decimal
    ("1.234,50", "1234.50"),
])
def test_converter_area_plataforma(entrada, esperado):
    assert converter_area_plataforma(entrada) == Decimal(esperado)


@pytest.mark.parametrize("entrada, esperado", [
    ("3", 3),
    ("2 quartos", 2),
    ("123.456.789-09", 0),   # CPF pego por engano não estoura o INTEGER do banco
    ("abc", 0),
    (None, 0),
])
def test_converter_int_seguro(entrada, esperado):
    assert converter_int_seguro(entrada) == esperado


def test_calcular_valor_unitario():
    assert calcular_valor_unitario(Decimal("500000"), Decimal("100")) == Decimal("5000.00")
    assert calcular_valor_unitario(Decimal("500000"), Decimal("0")) == Decimal("0.00")
    assert calcular_valor_unitario(Decimal("500000"), None) == Decimal("0.00")


@pytest.mark.parametrize("entrada, esperado", [
    ("-23.5505", -23.5505),
    ("- 46.6333", -46.6333),   # sinal separado do número no PDF
    (12, 12.0),
    ("NULL", None),
    ("abc", None),
    (None, None),
])
def test_converter_float_coordenada(entrada, esperado):
    assert converter_float_coordenada(entrada) == esperado


@pytest.mark.parametrize("entrada, padrao, esperado", [
    ("Bairro Centro", "", "Centro"),
    ("  Número 123 ", "", "123"),
    ("NULL", "", ""),
    (None, "S/N", "S/N"),
])
def test_limpar_txt(entrada, padrao, esperado):
    assert limpar_txt(entrada, padrao) == esperado


def test_numero_proposta_ancorado_na_data():
    texto = "Nº da Proposta  Data Solicitação\n2.714.283 01/07/2026\nCREA 123456789"
    assert extrair_numero_proposta(texto) == "2714283"


def test_numero_proposta_com_rotulo_simples():
    assert extrair_numero_proposta("Proposta: 12345678") == "12345678"


def test_numero_proposta_ausente():
    assert extrair_numero_proposta("sem nada aqui") == ""


def test_data_da_vistoria_vem_do_relatorio_fotografico():
    texto = "Data Solicitação 01/01/2026\n...\nRELATÓRIO FOTOGRÁFICO\n10/01/2026 Fachada"
    assert extrair_data_avaliacao(texto) == "10/01/2026"


def test_data_da_vistoria_por_frase():
    assert extrair_data_avaliacao("Vistoria realizada em 05/02/2026") == "05/02/2026"


def test_dms_para_decimal():
    assert _dms_para_decimal("23", "30", "0", "S") == pytest.approx(-23.5)
    assert _dms_para_decimal("10", "15", "0", "N") == pytest.approx(10.25)


def test_coordenadas_com_rotulo_ficam_negativas():
    texto, lat, lon = extrair_coordenadas_generico("Coordenadas: 23.550520, 46.633308")
    assert (lat, lon) == (-23.55052, -46.633308)
    assert texto == "-23.55052, -46.633308"


def test_coordenadas_pulam_titulo_sem_numeros():
    texto = "Localização\nSem dados aqui\nGeolocalização: -22.906847, -43.172897"
    _, lat, lon = extrair_coordenadas_generico(texto)
    assert (lat, lon) == (-22.906847, -43.172897)


def test_coordenadas_do_carimbo_gps_das_fotos():
    _, lat, lon = extrair_coordenadas_generico("Foto 1 9°25'58\"S / 40°28'13\"W")
    assert lat == pytest.approx(-9.43277778)
    assert lon == pytest.approx(-40.47027778)


def test_sem_coordenadas():
    assert extrair_coordenadas_generico("documento sem localização") == (None, None, None)


@pytest.mark.parametrize("texto, esperado", [
    ("RUA DAS FLORES 123 APTO 4", ("RUA DAS FLORES", "123")),
    ("AVENIDA BRASIL S/N", ("AVENIDA BRASIL", "S/N")),
    ("Rua sem numero", ("Rua sem numero", "S/N")),
    ("texto qualquer", ("", "S/N")),
])
def test_extrair_endereco_numero(texto, esperado):
    assert extrair_endereco_numero(texto) == esperado
