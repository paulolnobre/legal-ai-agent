"""From a CNJ number to the page where the lawyer can actually open the case.

DataJud answers what a process is and what moved in it. It never gives the
documents: those live in the court's own system (PJe, e-SAJ, eproc, Projudi),
behind a query form and usually a captcha or a certificate. So the honest help
is a direct link to the right consultation page plus the number to paste — the
lawyer opens it, downloads the piece, and sends the file back to be ingested.

The map is deliberately small and explicit. An unknown court falls back to the
CNJ's national consultation, which resolves any process to its court.
"""
from __future__ import annotations

from dataclasses import dataclass

from core.integrations.datajud import court_alias, normalize_process_number

# The national fallback: it accepts any CNJ number and points to the court.
CNJ_CONSULTATION = "https://www.cnj.jus.br/consultas-processuais/"

# Court alias -> (system name, consultation URL). Only entry points that a
# person can actually use; nothing here is scraped.
_CONSULTATION = {
    "stj": ("STJ (Consulta Processual)", "https://processo.stj.jus.br/processo/pesquisa/"),
    "tst": ("TST (Consulta Unificada)", "https://consultaprocessual.tst.jus.br/"),
    "tse": ("TSE (Consulta Pública)", "https://consultaunificadapje.tse.jus.br/"),
    "stm": ("STM (Consulta Processual)", "https://www.stm.jus.br/servicos-stm/processual"),
    "tjsp": ("TJSP (e-SAJ)", "https://esaj.tjsp.jus.br/cpopg/open.do"),
    "tjrj": ("TJRJ (Consulta Processual)", "https://www3.tjrj.jus.br/consultaprocessual/"),
    "tjmg": ("TJMG (Consulta Processual)", "https://www4.tjmg.jus.br/juridico/sf/"),
    "tjrs": ("TJRS (Consulta Processual)", "https://www.tjrs.jus.br/novo/busca/?return=proc"),
    "tjpr": ("TJPR (Projudi/PJe)", "https://projudi.tjpr.jus.br/projudi/"),
    "tjsc": ("TJSC (eproc)", "https://eproc1g.tjsc.jus.br/eproc/"),
    "tjba": ("TJBA (PJe)", "https://pje.tjba.jus.br/pje/ConsultaPublica/listView.seam"),
    "tjpe": ("TJPE (PJe)", "https://pje.tjpe.jus.br/1g/ConsultaPublica/listView.seam"),
    "tjce": ("TJCE (PJe)", "https://pje.tjce.jus.br/pje1grau/ConsultaPublica/listView.seam"),
    "tjgo": ("TJGO (Projudi)", "https://projudi.tjgo.jus.br/"),
    "tjdft": ("TJDFT (Consulta Processual)", "https://cnj.tjdft.jus.br/"),
    "tjes": ("TJES (Consulta Processual)", "https://sistemas.tjes.jus.br/pje/"),
    "tjma": ("TJMA (PJe)", "https://pje.tjma.jus.br/pje/ConsultaPublica/listView.seam"),
    "tjpa": ("TJPA (PJe)", "https://pje.tjpa.jus.br/pje/ConsultaPublica/listView.seam"),
    "tjpb": ("TJPB (PJe)", "https://pje.tjpb.jus.br/pje/ConsultaPublica/listView.seam"),
    "tjrn": ("TJRN (PJe)", "https://pje.tjrn.jus.br/pje/ConsultaPublica/listView.seam"),
    "tjms": ("TJMS (e-SAJ)", "https://esaj.tjms.jus.br/cpopg5/open.do"),
    "tjmt": ("TJMT (PJe)", "https://pje.tjmt.jus.br/pje/ConsultaPublica/listView.seam"),
    "tjam": ("TJAM (PJe)", "https://pje.tjam.jus.br/consultapublica/ConsultaPublica/listView.seam"),
    "tjpi": ("TJPI (PJe)", "https://pje.tjpi.jus.br/1g/ConsultaPublica/listView.seam"),
    "tjal": ("TJAL (e-SAJ)", "https://www2.tjal.jus.br/cpopg/open.do"),
    "tjse": ("TJSE (Consulta Processual)", "https://www.tjse.jus.br/portal/consultas/consulta-processual"),
    "tjro": ("TJRO (PJe)", "https://pjepg.tjro.jus.br/consulta/ConsultaPublica/listView.seam"),
    "tjac": ("TJAC (PJe)", "https://pje.tjac.jus.br/pje/ConsultaPublica/listView.seam"),
    "tjap": ("TJAP (PJe)", "https://pje.tjap.jus.br/pje/ConsultaPublica/listView.seam"),
    "tjrr": ("TJRR (PJe)", "https://pje.tjrr.jus.br/pje/ConsultaPublica/listView.seam"),
    "tjto": ("TJTO (eproc)", "https://eproc1.tjto.jus.br/eprocV2_prod_1grau/"),
}

# Court alias -> (diary name, where a person reads its editions). The DJe is
# where intimations and decisions are published, so it is the second place a
# lawyer looks after the case itself.
_PUBLICATIONS = {
    "tjpa": ("DJe TJPA", "https://dje.tjpa.jus.br/ClientDJEletronico/app/home.html"),
}

# Federal and labour courts follow one pattern each.
_TRF = "https://pje1g.trf{0}.jus.br/pje/ConsultaPublica/listView.seam"
_TRT = "https://pje.trt{0}.jus.br/consultaprocessual/"


@dataclass(frozen=True)
class ProcessLink:
    court: str
    system: str
    url: str
    formatted_number: str


def format_process_number(number: str) -> str:
    """0000000-00.0000.0.00.0000 — the shape the court's form expects."""
    digits = normalize_process_number(number)
    return (
        f"{digits[0:7]}-{digits[7:9]}.{digits[9:13]}."
        f"{digits[13]}.{digits[14:16]}.{digits[16:20]}"
    )


def consultation_link(process_number: str) -> ProcessLink:
    """The page where this specific process can be opened by a human."""
    digits = normalize_process_number(process_number)
    formatted = format_process_number(digits)
    try:
        alias = court_alias(digits)
    except ValueError:
        return ProcessLink("tribunal não identificado", "CNJ (Consultas Processuais)",
                           CNJ_CONSULTATION, formatted)

    if alias in _CONSULTATION:
        system, url = _CONSULTATION[alias]
        return ProcessLink(alias.upper(), system, url, formatted)
    if alias.startswith("trf"):
        return ProcessLink(alias.upper(), f"{alias.upper()} (PJe)",
                           _TRF.format(alias[3:]), formatted)
    if alias.startswith("trt"):
        return ProcessLink(alias.upper(), f"{alias.upper()} (PJe)",
                           _TRT.format(alias[3:]), formatted)
    return ProcessLink(alias.upper(), "CNJ (Consultas Processuais)",
                       CNJ_CONSULTATION, formatted)


def publications_link(process_number: str) -> tuple[str, str] | None:
    """The court's Diário da Justiça, when there is one to point at."""
    try:
        return _PUBLICATIONS.get(court_alias(normalize_process_number(process_number)))
    except ValueError:
        return None


def describe(process_number: str) -> str:
    """The lines sent to the lawyer: where to open it, and with which number."""
    link = consultation_link(process_number)
    lines = [
        f"🔗 Onde abrir o processo {link.formatted_number}",
        f"{link.system}: {link.url}",
    ]
    diary = publications_link(process_number)
    if diary:
        lines.append(f"{diary[0]}, para ler as publicações: {diary[1]}")
    lines.append(
        "Cole o número acima na consulta pública. Baixe a peça que interessar e "
        "me envie o arquivo aqui (eu ingiro no acervo e passo a responder sobre ele.)"
    )
    return "\n".join(lines)
