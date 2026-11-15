"""Contagem de prazos processuais — cálculo puro, sem efeitos colaterais.

Errar um prazo é dos poucos erros que o escritório não consegue desfazer, então
este módulo é deliberadamente conservador:

- não adivinha o regime de contagem; quem chama declara qual é;
- não inventa feriado: o que não estiver no calendário informado é dia útil;
- devolve o rastro dia a dia do que foi pulado e por quê, para o advogado poder
  conferir a conta em vez de confiar nela.

Regras implementadas (CPC/2015):

- art. 219 — prazos em dias, fixados por lei ou pelo juiz, contam-se apenas em
  dias úteis. Vale para o processo civil; o processo penal (CPP art. 798) conta
  dias corridos, e por isso `Regime` é um parâmetro obrigatório e não um padrão
  escondido;
- art. 224 — exclui-se o dia do começo e inclui-se o do vencimento;
- art. 224 §1º — vencendo em dia sem expediente forense, prorroga-se para o
  primeiro dia útil seguinte;
- art. 220 — suspende-se o curso do prazo entre 20 de dezembro e 20 de janeiro.

O que este módulo NÃO sabe, e por isso recebe pronto:

feriados estaduais e municipais, feriados forenses locais, suspensões por
portaria do tribunal e pontos facultativos. Eles entram por um `Calendario`.
`CalendarioMicrosoft` é o ponto de entrada previsto para ler o calendário do
escritório no Microsoft 365 — a interface já existe, a implementação depende de
credenciais que ainda não temos.
"""
from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, timedelta
from enum import Enum
from typing import Protocol, runtime_checkable

logger = logging.getLogger(__name__)


class Regime(str, Enum):
    """Como os dias são contados. Sem padrão: quem chama precisa decidir."""

    #: CPC art. 219 — só dias úteis. Processo civil e trabalhista.
    DIAS_UTEIS = "dias_uteis"
    #: CPP art. 798 — dias corridos. Processo penal e boa parte dos prazos
    #: administrativos e de direito material.
    DIAS_CORRIDOS = "dias_corridos"


class Motivo(str, Enum):
    """Por que um dia não entrou na contagem."""

    FIM_DE_SEMANA = "fim de semana"
    FERIADO = "feriado"
    RECESSO = "recesso forense"
    SEM_EXPEDIENTE = "sem expediente forense"


# ── Calendário ───────────────────────────────────────────────────────────────

@runtime_checkable
class Calendario(Protocol):
    """De onde vêm os dias sem expediente.

    Uma única pergunta, respondida para um intervalo fechado: mantém o cálculo
    com uma consulta só, em vez de uma por dia — o que importa quando a fonte
    for uma API remota.
    """

    def dias_sem_expediente(self, inicio: date, fim: date) -> dict[date, str]:
        """Datas sem expediente no intervalo, mapeadas para o motivo legível."""
        ...


def domingo_de_pascoa(ano: int) -> date:
    """Algoritmo gregoriano anônimo (Meeus/Jones/Butcher).

    Carnaval, Sexta-feira Santa e Corpus Christi são móveis e derivam da Páscoa,
    e um calendário forense sem eles erra vários prazos por ano.
    """
    a = ano % 19
    b, c = divmod(ano, 100)
    d, e = divmod(b, 4)
    g = (8 * b + 13) // 25
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    lunar = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 19 * lunar) // 433
    mes = (h + lunar - 7 * m + 90) // 25
    dia = (h + lunar - 7 * m + 33 * mes + 19) % 32
    return date(ano, mes, dia)


def feriados_nacionais(ano: int) -> dict[date, str]:
    """Feriados nacionais fixos e móveis.

    Deliberadamente só os nacionais. Feriado estadual, municipal e forense varia
    por comarca e entra pelo calendário do escritório — inventar aqui produziria
    um prazo errado com aparência de certo.
    """
    pascoa = domingo_de_pascoa(ano)
    feriados = {
        date(ano, 1, 1): "Confraternização Universal",
        date(ano, 4, 21): "Tiradentes",
        date(ano, 5, 1): "Dia do Trabalho",
        date(ano, 9, 7): "Independência",
        date(ano, 10, 12): "Nossa Senhora Aparecida",
        date(ano, 11, 2): "Finados",
        date(ano, 11, 15): "Proclamação da República",
        # Lei 14.759/2023: nacional a partir de 2024.
        date(ano, 12, 25): "Natal",
        pascoa - timedelta(days=48): "Carnaval",
        pascoa - timedelta(days=47): "Carnaval",
        pascoa - timedelta(days=2): "Sexta-feira Santa",
        pascoa + timedelta(days=60): "Corpus Christi",
    }
    if ano >= 2024:
        feriados[date(ano, 11, 20)] = "Consciência Negra"
    return feriados


@dataclass(frozen=True)
class CalendarioNacional:
    """Feriados nacionais, mais o que o escritório acrescentar.

    `extras` é onde entram feriado estadual, municipal, forense e suspensão por
    portaria — sem isso o cálculo trata o dia como útil.
    """

    extras: dict[date, str] = field(default_factory=dict)

    def dias_sem_expediente(self, inicio: date, fim: date) -> dict[date, str]:
        dias: dict[date, str] = {}
        for ano in range(inicio.year, fim.year + 1):
            dias.update(feriados_nacionais(ano))
        dias.update(self.extras)
        return {dia: motivo for dia, motivo in dias.items() if inicio <= dia <= fim}


class CalendarioMicrosoft:
    """Calendário forense lido do Microsoft 365 — ainda não implementado.

    Existe para fixar o contrato antes das credenciais: quando o tenant estiver
    disponível, basta implementar `dias_sem_expediente` lendo os eventos de dia
    inteiro do calendário indicado, e nada mais no módulo muda.

    Nenhum processo depende desta classe hoje: ela não é importada por handler,
    worker ou grafo, e o cálculo funciona inteiro com `CalendarioNacional`.

    Implementação prevista (Microsoft Graph, somente leitura):
      GET /users/{user_id}/calendars/{calendar_id}/calendarView
          ?startDateTime=...&endDateTime=...
      escopo `Calendars.Read`, filtrando `isAllDay eq true`.
    """

    def __init__(
        self,
        *,
        tenant_id: str | None = None,
        client_id: str | None = None,
        calendar_id: str | None = None,
        base: Calendario | None = None,
    ) -> None:
        self.tenant_id = tenant_id
        self.client_id = client_id
        self.calendar_id = calendar_id
        #: Feriados nacionais continuam valendo; o Graph só acrescenta o local.
        self.base = base or CalendarioNacional()

    def configurado(self) -> bool:
        return all((self.tenant_id, self.client_id, self.calendar_id))

    def dias_sem_expediente(self, inicio: date, fim: date) -> dict[date, str]:
        raise NotImplementedError(
            "O calendário do Microsoft 365 ainda não está configurado. "
            "Use CalendarioNacional(extras=...) até haver tenant_id, client_id "
            "e calendar_id."
        )


# ── Recesso ──────────────────────────────────────────────────────────────────

def em_recesso(dia: date) -> bool:
    """CPC art. 220: 20/12 a 20/01, ambos incluídos."""
    return (dia.month == 12 and dia.day >= 20) or (dia.month == 1 and dia.day <= 20)


# ── Resultado ────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class DiaIgnorado:
    data: date
    motivo: Motivo
    detalhe: str = ""


@dataclass(frozen=True)
class Prazo:
    """O vencimento e a conta que levou até ele."""

    termo_inicial: date
    inicio_contagem: date
    vencimento: date
    dias: int
    regime: Regime
    ignorados: tuple[DiaIgnorado, ...]
    prorrogado: bool

    def descrever(self) -> str:
        """Explicação em português, para o advogado conferir a conta."""
        unidade = "dias úteis" if self.regime is Regime.DIAS_UTEIS else "dias corridos"
        linhas = [
            f"Termo inicial: {self.termo_inicial:%d/%m/%Y}",
            f"Início da contagem: {self.inicio_contagem:%d/%m/%Y} "
            "(exclui-se o dia do começo, CPC art. 224)",
            f"Prazo: {self.dias} {unidade}",
            f"Vencimento: {self.vencimento:%d/%m/%Y}",
        ]
        if self.prorrogado:
            linhas.append("Vencimento prorrogado para o primeiro dia útil (CPC art. 224 §1º).")
        recesso = [d for d in self.ignorados if d.motivo is Motivo.RECESSO]
        if recesso:
            linhas.append(
                f"Suspenso por recesso forense em {len(recesso)} dia(s) (CPC art. 220)."
            )
        feriados = [d for d in self.ignorados if d.motivo is Motivo.FERIADO]
        if feriados:
            nomes = ", ".join(f"{d.data:%d/%m} {d.detalhe}".strip() for d in feriados)
            linhas.append(f"Feriados no período: {nomes}.")
        linhas.append(
            "⚠️ Conferência obrigatória: feriados locais, suspensões por portaria e "
            "o regime aplicável ao caso são responsabilidade do advogado."
        )
        return "\n".join(linhas)


# ── Cálculo ──────────────────────────────────────────────────────────────────

_MARGEM = 400  # dias de folga para o calendário cobrir recesso e feriados


def calcular_prazo(
    termo_inicial: date,
    dias: int,
    *,
    regime: Regime,
    calendario: Calendario | None = None,
    suspender_no_recesso: bool | None = None,
) -> Prazo:
    """Vencimento de um prazo contado a partir de `termo_inicial`.

    `termo_inicial` é o dia da intimação/publicação: ele é excluído da contagem
    (CPC art. 224), então o primeiro dia contado é o seguinte.

    `suspender_no_recesso` segue o regime quando não informado — suspende em
    dias úteis (civil, art. 220) e não suspende em dias corridos, onde o prazo
    penal corre no recesso. Um prazo de direito material em dias corridos que
    também suspenda deve dizer isso explicitamente.
    """
    if dias < 1:
        raise ValueError("O prazo precisa ser de pelo menos um dia.")

    calendario = calendario or CalendarioNacional()
    if suspender_no_recesso is None:
        suspender_no_recesso = regime is Regime.DIAS_UTEIS

    janela_fim = termo_inicial + timedelta(days=dias * 3 + _MARGEM)
    sem_expediente = calendario.dias_sem_expediente(termo_inicial, janela_fim)

    def util(dia: date) -> bool:
        return dia.weekday() < 5 and dia not in sem_expediente

    def motivo_do_dia(dia: date) -> tuple[Motivo, str]:
        """Por que `dia` não é útil. Só é chamada quando já se sabe que não é."""
        if dia.weekday() >= 5:
            return Motivo.FIM_DE_SEMANA, ""
        return Motivo.FERIADO, sem_expediente.get(dia, "")

    ignorados: list[DiaIgnorado] = []
    contados = 0
    cursor = termo_inicial
    inicio_contagem: date | None = None
    limite = janela_fim

    while contados < dias:
        cursor += timedelta(days=1)
        if cursor > limite:  # pragma: no cover - defesa contra laço infinito
            raise RuntimeError("Não foi possível fechar o prazo dentro da janela prevista.")

        if suspender_no_recesso and em_recesso(cursor):
            ignorados.append(DiaIgnorado(cursor, Motivo.RECESSO))
            continue

        if regime is Regime.DIAS_UTEIS and not util(cursor):
            motivo, detalhe = motivo_do_dia(cursor)
            ignorados.append(DiaIgnorado(cursor, motivo, detalhe))
            continue

        if inicio_contagem is None:
            inicio_contagem = cursor
        contados += 1

    vencimento = cursor
    prorrogado = False
    # Art. 224 §1º: em dias corridos o vencimento pode cair em dia sem
    # expediente; em dias úteis isso não acontece por construção.
    while not util(vencimento) or (suspender_no_recesso and em_recesso(vencimento)):
        vencimento += timedelta(days=1)
        prorrogado = True

    return Prazo(
        termo_inicial=termo_inicial,
        inicio_contagem=inicio_contagem or vencimento,
        vencimento=vencimento,
        dias=dias,
        regime=regime,
        ignorados=tuple(ignorados),
        prorrogado=prorrogado,
    )


def dias_uteis_entre(inicio: date, fim: date, calendario: Calendario | None = None) -> int:
    """Quantos dias úteis existem em (inicio, fim], para conferir uma conta."""
    calendario = calendario or CalendarioNacional()
    sem_expediente = calendario.dias_sem_expediente(inicio, fim)
    total = 0
    cursor = inicio
    while cursor < fim:
        cursor += timedelta(days=1)
        if cursor.weekday() < 5 and cursor not in sem_expediente:
            total += 1
    return total


def proximo_dia_util(dia: date, calendario: Calendario | None = None) -> date:
    """Primeiro dia útil em ou após `dia`."""
    calendario = calendario or CalendarioNacional()
    sem_expediente = calendario.dias_sem_expediente(dia, dia + timedelta(days=_MARGEM))
    cursor = dia
    while cursor.weekday() >= 5 or cursor in sem_expediente:
        cursor += timedelta(days=1)
    return cursor


def calendario_do_escritorio(extras: Iterable[tuple[date, str]] = ()) -> CalendarioNacional:
    """Atalho para montar o calendário com os feriados locais do escritório."""
    return CalendarioNacional(extras=dict(extras))
