"""Contagem de prazo — o cálculo que o escritório não pode errar.

Os vencimentos abaixo foram conferidos dia a dia contra o calendário real.
"""
from datetime import date

import pytest

from core.services.deadlines import (
    CalendarioMicrosoft,
    CalendarioNacional,
    Motivo,
    Regime,
    calcular_prazo,
    dias_uteis_entre,
    domingo_de_pascoa,
    em_recesso,
    feriados_nacionais,
    proximo_dia_util,
)


# ── Calendário ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "ano, esperado",
    [(2024, date(2024, 3, 31)), (2025, date(2025, 4, 20)), (2026, date(2026, 4, 5))],
)
def test_a_pascoa_ancora_os_feriados_moveis(ano, esperado):
    assert domingo_de_pascoa(ano) == esperado


def test_os_feriados_moveis_derivam_da_pascoa():
    feriados = feriados_nacionais(2026)
    assert feriados[date(2026, 2, 16)] == "Carnaval"
    assert feriados[date(2026, 2, 17)] == "Carnaval"
    assert feriados[date(2026, 4, 3)] == "Sexta-feira Santa"
    assert feriados[date(2026, 6, 4)] == "Corpus Christi"


def test_consciencia_negra_so_vale_como_nacional_a_partir_de_2024():
    assert date(2024, 11, 20) in feriados_nacionais(2024)
    assert date(2023, 11, 20) not in feriados_nacionais(2023)


def test_o_calendario_recorta_o_intervalo_pedido():
    calendario = CalendarioNacional()
    dias = calendario.dias_sem_expediente(date(2026, 1, 1), date(2026, 1, 31))
    assert dias == {date(2026, 1, 1): "Confraternização Universal"}


def test_feriado_local_entra_por_extras():
    # Sem isso o cálculo trata 09/07 como dia útil, e o prazo sai errado em SP.
    calendario = CalendarioNacional(extras={date(2026, 7, 9): "Revolução Constitucionalista"})
    dias = calendario.dias_sem_expediente(date(2026, 7, 1), date(2026, 7, 31))
    assert dias[date(2026, 7, 9)] == "Revolução Constitucionalista"


# ── Contagem em dias úteis (CPC art. 219) ────────────────────────────────────

def test_exclui_o_dia_do_comeco_e_inclui_o_do_vencimento():
    # Segunda 02/03/2026 + 5 dias úteis: conta ter 03, qua 04, qui 05, sex 06,
    # seg 09 — o dia da intimação não conta (art. 224).
    prazo = calcular_prazo(date(2026, 3, 2), 5, regime=Regime.DIAS_UTEIS)
    assert prazo.inicio_contagem == date(2026, 3, 3)
    assert prazo.vencimento == date(2026, 3, 9)


def test_o_fim_de_semana_nao_conta():
    # Sexta 06/03/2026 + 3 dias úteis: seg 09, ter 10, qua 11.
    prazo = calcular_prazo(date(2026, 3, 6), 3, regime=Regime.DIAS_UTEIS)
    assert prazo.vencimento == date(2026, 3, 11)
    assert {d.motivo for d in prazo.ignorados} == {Motivo.FIM_DE_SEMANA}


def test_o_feriado_nacional_nao_conta():
    # Quarta 15/04/2026 + 5 dias úteis. 21/04 é Tiradentes (terça), então a
    # contagem passa por qui 16, sex 17, seg 20, qua 22, qui 23.
    prazo = calcular_prazo(date(2026, 4, 15), 5, regime=Regime.DIAS_UTEIS)
    assert prazo.vencimento == date(2026, 4, 23)
    assert any(d.detalhe == "Tiradentes" for d in prazo.ignorados)


def test_um_prazo_de_quinze_dias_uteis_atravessa_dois_fins_de_semana():
    # Intimação segunda 02/03/2026, 15 dias úteis sem feriado no meio.
    prazo = calcular_prazo(date(2026, 3, 2), 15, regime=Regime.DIAS_UTEIS)
    assert prazo.vencimento == date(2026, 3, 23)


# ── Recesso forense (CPC art. 220) ───────────────────────────────────────────

@pytest.mark.parametrize(
    "dia, dentro",
    [
        (date(2025, 12, 19), False),
        (date(2025, 12, 20), True),
        (date(2026, 1, 20), True),
        (date(2026, 1, 21), False),
    ],
)
def test_o_recesso_vai_de_20_12_a_20_01_inclusive(dia, dentro):
    assert em_recesso(dia) is dentro


def test_o_prazo_civil_fica_suspenso_no_recesso():
    # Intimação em 15/12/2025 (segunda), 10 dias úteis. Corre ter 16, qua 17,
    # qui 18, sex 19 (4 dias) e o recesso suspende de 20/12 a 20/01. Retoma em
    # qua 21/01 e completa em qua 28/01: 21, 22, 23, 26, 27, 28.
    prazo = calcular_prazo(date(2025, 12, 15), 10, regime=Regime.DIAS_UTEIS)
    assert prazo.vencimento == date(2026, 1, 28)
    assert any(d.motivo is Motivo.RECESSO for d in prazo.ignorados)


def test_o_prazo_penal_corre_no_recesso():
    # Dias corridos e sem suspensão: 22/12/2025 + 10 = 01/01/2026, que é
    # feriado, então prorroga para o primeiro dia útil.
    prazo = calcular_prazo(
        date(2025, 12, 22), 10, regime=Regime.DIAS_CORRIDOS, suspender_no_recesso=False,
    )
    assert prazo.vencimento == date(2026, 1, 2)
    assert prazo.prorrogado is True


# ── Dias corridos (CPP art. 798) ─────────────────────────────────────────────

def test_dias_corridos_contam_fim_de_semana():
    # Sexta 06/03/2026 + 3 dias corridos = segunda 09/03 (sáb e dom contam).
    prazo = calcular_prazo(date(2026, 3, 6), 3, regime=Regime.DIAS_CORRIDOS)
    assert prazo.vencimento == date(2026, 3, 9)
    assert prazo.ignorados == ()


def test_o_vencimento_em_dia_sem_expediente_e_prorrogado():
    # Quinta 05/03/2026 + 2 dias corridos = sábado 07/03 → prorroga p/ 09/03.
    prazo = calcular_prazo(date(2026, 3, 5), 2, regime=Regime.DIAS_CORRIDOS)
    assert prazo.vencimento == date(2026, 3, 9)
    assert prazo.prorrogado is True


# ── Regras de uso ────────────────────────────────────────────────────────────

def test_o_regime_e_obrigatorio():
    """Sem padrão escondido: contar penal como civil vence o prazo cedo demais.

    A chamada abaixo é inválida de propósito — o editor apontar isso é o
    comportamento desejado, e é justamente o que o teste garante em execução.
    """
    with pytest.raises(TypeError):
        calcular_prazo(date(2026, 3, 2), 5)  # type: ignore[call-arg]


def test_prazo_de_zero_dias_e_recusado():
    with pytest.raises(ValueError):
        calcular_prazo(date(2026, 3, 2), 0, regime=Regime.DIAS_UTEIS)


def test_a_explicacao_mostra_a_conta_e_a_ressalva():
    prazo = calcular_prazo(date(2026, 4, 15), 5, regime=Regime.DIAS_UTEIS)
    texto = prazo.descrever()
    assert "15/04/2026" in texto and "23/04/2026" in texto
    assert "art. 224" in texto
    assert "Tiradentes" in texto
    assert "responsabilidade do advogado" in texto


# ── Auxiliares ───────────────────────────────────────────────────────────────

def test_dias_uteis_entre_ignora_fim_de_semana_e_feriado():
    # 15/04 a 24/04/2026: qui 16, sex 17, seg 20, (21 Tiradentes), qua 22,
    # qui 23, sex 24 = 6 dias úteis.
    assert dias_uteis_entre(date(2026, 4, 15), date(2026, 4, 24)) == 6


def test_proximo_dia_util_pula_o_feriado_prolongado():
    # Sábado 21/11/2026; 20/11 (sexta) é feriado, então cai na segunda 23.
    assert proximo_dia_util(date(2026, 11, 21)) == date(2026, 11, 23)


def test_um_dia_util_e_ele_mesmo():
    assert proximo_dia_util(date(2026, 3, 4)) == date(2026, 3, 4)


# ── Microsoft 365 ────────────────────────────────────────────────────────────

def test_o_calendario_da_microsoft_falha_claramente_enquanto_nao_ha_credencial():
    calendario = CalendarioMicrosoft()
    assert calendario.configurado() is False
    with pytest.raises(NotImplementedError, match="ainda não está configurado"):
        calendario.dias_sem_expediente(date(2026, 1, 1), date(2026, 12, 31))


def test_o_calculo_nao_depende_da_microsoft():
    """Nada no caminho de cálculo importa ou instancia o adaptador do Graph."""
    import inspect

    from core.services import deadlines

    fonte = inspect.getsource(deadlines.calcular_prazo)
    assert "Microsoft" not in fonte
    prazo = calcular_prazo(date(2026, 3, 2), 5, regime=Regime.DIAS_UTEIS)
    assert prazo.vencimento == date(2026, 3, 9)
