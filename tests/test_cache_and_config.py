"""Etapa 0 — a resposta em cache carrega as fontes, e a configuração falha cedo.

Dois problemas que o roteiro previa e que existiam de fato: uma resposta servida
do cache voltava sem as fontes que a sustentavam, e um par provedor/modelo
incompatível era trocado em silêncio em vez de recusado na inicialização.
"""
import asyncio
import os
from typing import Any

import pytest

from core.config import (
    Settings,
    missing_provider_credentials,
    provider_model_problems,
    validate_startup_config,
)
from core.integrations import cache
from core.services.legal_assistant import internal_research
from core.services.legal_assistant.sources import Source
from core.services.legal_assistant.state import LegalState


# ── Identidade do cache ──────────────────────────────────────────────────────

def test_a_mesma_pergunta_em_tarefas_diferentes_nao_colide():
    """Pesquisa interna e externa respondem coisas diferentes à mesma pergunta."""
    interna = cache._key("answer", "prazo recursal", cache._answer_identity(
        "internal_research", None, "gpt-4o"))
    externa = cache._key("answer", "prazo recursal", cache._answer_identity(
        "external_research", None, "gpt-4o"))
    assert interna != externa


def test_o_modelo_faz_parte_da_identidade():
    """Resposta escrita por outro modelo é outra resposta."""
    a = cache._answer_identity("internal_research", None, "gpt-4o")
    b = cache._answer_identity("internal_research", None, "gpt-4o-mini")
    assert a != b


def test_o_cliente_separa_as_entradas():
    """A mesma pergunta sobre clientes diferentes tem respostas diferentes."""
    a = cache._answer_identity("internal_research", "Acme", "gpt-4o")
    b = cache._answer_identity("internal_research", "Beta", "gpt-4o")
    assert a != b


def test_a_versao_do_pipeline_invalida_o_que_ficou_para_tras():
    assert cache._answer_identity("internal_research", None, "gpt-4o").startswith(
        f"v{cache.ANSWER_SCHEMA_VERSION}:"
    )


def test_politica_de_fontes_faz_parte_da_identidade_externa(monkeypatch):
    original = cache._answer_identity("external_research", None, "gpt-4o")
    monkeypatch.setattr(
        cache.settings,
        "serper_allowed_domains",
        cache.settings.serper_allowed_domains + ",novo-tribunal.jus.br",
    )
    changed = cache._answer_identity("external_research", None, "gpt-4o")

    assert original != changed
    assert f":external-{cache.EXTERNAL_SOURCE_POLICY_VERSION}-" in original


def test_politica_externa_nao_invalida_resposta_do_acervo(monkeypatch):
    original = cache._answer_identity("internal_research", None, "gpt-4o")
    monkeypatch.setattr(cache.settings, "fetch_max_pages", 99)

    assert cache._answer_identity("internal_research", None, "gpt-4o") == original


# ── O cache hit devolve as fontes ────────────────────────────────────────────

def _lookup(monkeypatch, record):
    async def get_answer(question, **kwargs):
        return record

    async def log(*args, **kwargs):
        return None

    monkeypatch.setattr(cache, "get_answer", get_answer)
    monkeypatch.setattr(internal_research, "log_access", log)
    state = LegalState(conn=None, question="prazo", query="prazo")
    return asyncio.run(internal_research.cache_lookup(state))


def test_uma_resposta_em_cache_volta_com_as_fontes(monkeypatch):
    """Sem isso, quem cai no cache recebe resposta sem lastro e /fontes vazio."""
    out = _lookup(monkeypatch, {
        "answer": "O prazo é de 15 dias úteis.",
        "sources": [{"kind": "corpus", "title": "Contrato Acme", "reference": "12"}],
        "limitations": ["Documento sem atualização posterior a 2024."],
    })
    assert out["answer"] == "O prazo é de 15 dias úteis."
    assert out["cached"] is True
    assert [s.title for s in out["sources"]] == ["Contrato Acme"]
    assert all(isinstance(s, Source) for s in out["sources"])
    assert out["limitations"] == ["Documento sem atualização posterior a 2024."]


def test_uma_fonte_corrompida_no_cache_nao_derruba_a_resposta(monkeypatch):
    out = _lookup(monkeypatch, {
        "answer": "Resposta.",
        "sources": [{"kind": "invalido"}, {"kind": "corpus", "title": "Válida"}],
    })
    assert [s.title for s in out["sources"]] == ["Válida"]


def test_limitacao_corrompida_no_cache_e_ignorada(monkeypatch):
    out = _lookup(monkeypatch, {
        "answer": "Resposta.",
        "limitations": ["  Fonte indisponível.  ", None, 42, ""],
    })
    assert out["limitations"] == ["Fonte indisponível."]


def test_cache_vazio_segue_o_pipeline(monkeypatch):
    assert _lookup(monkeypatch, None) == {}


def test_uma_entrada_antiga_em_texto_puro_e_ignorada():
    """O formato antigo guardava só a string; servi-lo perderia as fontes."""
    async def check():
        original = cache._get
        try:
            async def fake(namespace, text, identity=""):
                return "resposta antiga em texto puro"

            cache._get = fake
            return await cache.get_answer("prazo", task="internal_research")
        finally:
            cache._get = original

    assert asyncio.run(check()) is None


# ── Frescor e TTL por classe de pesquisa ────────────────────────────────────

@pytest.mark.parametrize(
    "question",
    [
        "Qual é o entendimento atual do STJ?",
        "Há decisões recentes sobre o tema?",
        "Consulte o processo 0001234-56.2024.8.26.0100",
        "Qual foi o último andamento?",
    ],
)
def test_consulta_externa_que_exige_frescor_ignora_o_cache(question):
    assert cache.bypass_answer_cache(question, "external_research") is True
    assert cache.bypass_answer_cache(question, "combined_research") is True


def test_marcador_de_tempo_nao_impede_cache_do_acervo_interno():
    assert cache.bypass_answer_cache(
        "Qual é a versão atual do contrato?", "internal_research"
    ) is False


def test_consulta_externa_estavel_pode_usar_cache():
    assert cache.bypass_answer_cache(
        "Quais são os requisitos do agravo de instrumento?", "external_research"
    ) is False


def test_bypass_nao_le_nem_grava_uma_resposta(monkeypatch):
    calls: list[str] = []

    async def fake_get(*args, **kwargs):
        calls.append("get")

    async def fake_set(*args, **kwargs):
        calls.append("set")

    monkeypatch.setattr(cache, "_get", fake_get)
    monkeypatch.setattr(cache, "_set", fake_set)

    async def check():
        found = await cache.get_answer(
            "entendimento STJ", task="external_research", freshness_text="atual hoje"
        )
        await cache.set_answer(
            "entendimento STJ",
            "resposta",
            task="external_research",
            freshness_text="atual hoje",
        )
        return found

    assert asyncio.run(check()) is None
    assert calls == []


@pytest.mark.parametrize(
    ("task", "expected_ttl"),
    [
        ("internal_research", 86_400),
        ("external_research", 1_800),
        ("combined_research", 1_800),
    ],
)
def test_resposta_usa_ttl_da_classe_de_pesquisa(monkeypatch, task, expected_ttl):
    captured: dict = {}

    async def fake_set(namespace, text, value, ttl, identity=""):
        captured.update(namespace=namespace, ttl=ttl, identity=identity)

    monkeypatch.setattr(cache, "_set", fake_set)
    monkeypatch.setattr(cache.settings, "cache_answer_ttl", 86_400)
    monkeypatch.setattr(cache.settings, "cache_external_answer_ttl", 1_800)

    asyncio.run(cache.set_answer("pergunta estável", "resposta", task=task))

    assert captured["namespace"] == "answer"
    assert captured["ttl"] == expected_ttl
    assert f":{task}:" in captured["identity"]


def test_registro_cacheado_preserva_limitacoes(monkeypatch):
    captured: dict = {}

    async def fake_set(namespace, text, value, ttl, identity=""):
        captured.update(value=value)

    monkeypatch.setattr(cache, "_set", fake_set)
    asyncio.run(cache.set_answer(
        "pergunta estável",
        "resposta",
        task="external_research",
        limitations=["Uma página oficial não pôde ser aberta."],
    ))

    assert captured["value"]["limitations"] == [
        "Uma página oficial não pôde ser aberta."
    ]
    assert captured["value"]["source_policy"].startswith(
        f"external-{cache.EXTERNAL_SOURCE_POLICY_VERSION}-"
    )


# ── Configuração provedor × modelo ───────────────────────────────────────────

class _Settings(Settings):
    """Settings montada só com o que o teste passa.

    A Settings real lê o .env da máquina — e estes testes existem justamente
    para policiar configuração, então não podem depender da configuração local.
    """

    @classmethod
    def settings_customise_sources(
        cls, settings_cls, init_settings, env_settings, dotenv_settings,
        file_secret_settings,
    ):
        return (init_settings,)


_BASE: dict[str, Any] = {
    "app_env": "development",
    "llm_simple_provider": "openai",
    "llm_complex_provider": "openai",
    "llm_simple_model": "gpt-4o-mini",
    "llm_complex_model": "gpt-4o",
    "llm_fallback_model": "gpt-4o-mini",
}


def test_uma_configuracao_coerente_nao_tem_problema():
    config = _Settings(**_BASE)
    assert config.legal_model == "gpt-4o"
    assert config.router_model == "gpt-4o-mini"
    assert provider_model_problems(config) == []
    validate_startup_config(config)  # não levanta


def test_modelo_gemini_com_provedor_openai_impede_a_inicializacao():
    """Antes disso, llm.py trocava para o fallback sem avisar ninguém.

    O escritório lia gpt-4o no .env e recebia respostas de gpt-4o-mini.
    """
    config = _Settings(**_BASE, legal_model="gemini-1.5-flash")
    with pytest.raises(RuntimeError, match="LEGAL_MODEL"):
        validate_startup_config(config)


def test_modelo_openai_com_provedor_gemini_impede_a_inicializacao():
    config = _Settings(
        **{**_BASE, "llm_complex_provider": "gemini", "llm_complex_model": "gpt-4o"}
    )
    assert any("LLM_COMPLEX_MODEL" in p for p in provider_model_problems(config))


def test_o_roteador_segue_o_provedor_simples():
    config = _Settings(**_BASE, router_model="gemini-1.5-flash")
    assert any("ROUTER_MODEL" in p for p in provider_model_problems(config))


def test_o_fallback_precisa_ser_openai():
    """O fallback só é usado no caminho OpenAI; um nome Gemini ali é letra morta."""
    config = _Settings(**{**_BASE, "llm_fallback_model": "gemini-1.5-flash"})
    assert any("LLM_FALLBACK_MODEL" in p for p in provider_model_problems(config))


def test_uma_configuracao_gemini_coerente_e_aceita():
    config = _Settings(
        **{
            **_BASE,
            "llm_simple_provider": "gemini",
            "llm_complex_provider": "gemini",
            "llm_simple_model": "gemini-1.5-flash",
            "llm_complex_model": "gemini-1.5-pro",
        }
    )
    assert config.legal_model == "gemini-1.5-pro"
    assert provider_model_problems(config) == []


def test_o_erro_nomeia_todos_os_campos_errados_de_uma_vez():
    """Corrigir um .env de cada vez, a cada reinício, é hostil."""
    config = _Settings(
        **_BASE, legal_model="gemini-1.5-flash", research_model="gemini-1.5-flash",
    )
    with pytest.raises(RuntimeError) as erro:
        validate_startup_config(config)
    assert "LEGAL_MODEL" in str(erro.value) and "RESEARCH_MODEL" in str(erro.value)


def test_importar_a_configuracao_nunca_e_fatal():
    """Uma linha errada no .env não pode impedir a suíte de ser coletada.

    A checagem pertence à inicialização da aplicação. Colocá-la na Settings
    fazia `import core.config` explodir, e com isso todo arquivo de teste que
    importa qualquer coisa de core.* falhava na coleta, escondendo a mensagem
    que explicava o problema.
    """
    import subprocess
    import sys

    resultado = subprocess.run(
        [sys.executable, "-c", "import core.config; print('ok')"],
        env={**os.environ, "LEGAL_MODEL": "gemini-1.5-flash"},
        capture_output=True,
        text=True,
    )
    assert resultado.returncode == 0, resultado.stderr
    assert "ok" in resultado.stdout


def test_a_aplicacao_se_recusa_a_subir_com_configuracao_incompativel():
    """O critério de aceite da Etapa 0: falhar cedo, não trocar em silêncio."""
    import subprocess
    import sys

    resultado = subprocess.run(
        [sys.executable, "-c",
         "from core.config import validate_startup_config; validate_startup_config()"],
        env={**os.environ, "LEGAL_MODEL": "gemini-1.5-flash"},
        capture_output=True,
        text=True,
    )
    assert resultado.returncode != 0, "uma configuração incompatível não pode subir"
    assert "Incompatible provider/model configuration" in resultado.stderr
    assert "LEGAL_MODEL" in resultado.stderr


def test_o_lifespan_valida_antes_de_tocar_o_banco():
    """A validação tem que vir antes da conexão, senão o erro sai trocado."""
    import inspect

    from core.web import app as web_app

    fonte = inspect.getsource(web_app.lifespan)
    assert fonte.index("validate_startup_config") < fonte.index("get_pool")


# ── MGC_* loads where OBJECT_STORE_* used to ─────────────────────────────────
# The field stayed named object_store_* in Python (it is a storage
# abstraction, and Magalu is just today's backend); only the env var it reads
# changed, through validation_alias. Every other test in this file sets these
# fields with monkeypatch.setattr, which would still pass if the alias wiring
# were broken — this is the one that actually reads the environment.

_MGC_KEYS = (
    "MGC_ENDPOINT", "MGC_BUCKET", "MGC_REGION",
    "MGC_ACCESS_KEY_ID", "MGC_SECRET_ACCESS_KEY",
)


def test_mgc_env_vars_populate_the_object_store_settings(monkeypatch):
    for key in _MGC_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("MGC_ENDPOINT", "https://br-se1.magaluobjects.com")
    monkeypatch.setenv("MGC_BUCKET", "a-bucket")
    monkeypatch.setenv("MGC_REGION", "br-ne1")
    monkeypatch.setenv("MGC_ACCESS_KEY_ID", "an-id")
    monkeypatch.setenv("MGC_SECRET_ACCESS_KEY", "a-secret")

    config = Settings(_env_file=None)  # pyright: ignore[reportCallIssue]

    assert config.object_store_endpoint == "https://br-se1.magaluobjects.com"
    assert config.object_store_bucket == "a-bucket"
    assert config.object_store_region == "br-ne1"
    assert config.object_store_access_key_id == "an-id"
    assert config.object_store_secret_access_key == "a-secret"


def test_the_old_object_store_env_vars_are_dead(monkeypatch):
    """The rename has to actually retire the old name, not just add the new
    one next to it: a key still answering to OBJECT_STORE_BUCKET is a key
    that silently ignores the explicit naming the rename exists to give."""
    for key in _MGC_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("OBJECT_STORE_BUCKET", "should-not-be-read")
    monkeypatch.setenv("OBJECT_STORE_ACCESS_KEY_ID", "should-not-be-read")

    config = Settings(_env_file=None)  # pyright: ignore[reportCallIssue]

    assert config.object_store_bucket is None
    assert config.object_store_access_key_id is None


# ── Um provedor escolhido tem que ser o provedor usado ───────────────────────

_GEMINI: dict[str, Any] = {
    "app_env": "development",
    "llm_simple_provider": "gemini",
    "llm_complex_provider": "gemini",
    "llm_simple_model": "gemini-1.5-flash",
    "llm_complex_model": "gemini-1.5-pro",
    "llm_fallback_model": "gpt-4o-mini",
}


def test_escolher_gemini_sem_a_chave_impede_a_inicializacao():
    """llm._provider_for responde a chave ausente servindo OpenAI em silêncio.

    Escolher um provedor e receber outro é a mesma classe de erro do modelo
    trocado: o escritório acredita estar rodando o que configurou.
    """
    config = _Settings(**_GEMINI, gemini_api_key=None)
    assert missing_provider_credentials(config)
    with pytest.raises(RuntimeError, match="GEMINI_API_KEY"):
        validate_startup_config(config)


def test_escolher_gemini_com_a_chave_esta_correto():
    config = _Settings(**_GEMINI, gemini_api_key="uma-chave")
    assert missing_provider_credentials(config) == []
    validate_startup_config(config)  # não levanta


def test_sem_a_chave_do_gemini_e_sem_escolher_gemini_nao_ha_problema():
    """É a configuração atual do escritório: só OpenAI, sem credencial parada."""
    config = _Settings(**_BASE, gemini_api_key=None)
    assert missing_provider_credentials(config) == []
    validate_startup_config(config)  # não levanta


def test_o_erro_diz_qual_variavel_escolheu_o_gemini():
    config = _Settings(
        **{**_BASE, "llm_complex_provider": "gemini", "llm_complex_model": "gemini-1.5-pro"},
        gemini_api_key=None,
    )
    problema = missing_provider_credentials(config)[0]
    assert "LLM_COMPLEX_PROVIDER" in problema
    assert "LLM_SIMPLE_PROVIDER" not in problema


# ── O número do processo não vai para o buscador ─────────────────────────────

def test_o_numero_cnj_e_removido_da_consulta_web():
    """DataJud é a fonte do andamento; o Google não precisa saber o processo."""
    from core.integrations import datajud

    texto = "andamento do processo 0001234-56.2024.8.26.0100 por favor"
    assert datajud.extract_process_numbers(texto)
    limpo = datajud.redact_process_numbers(texto)
    assert "0001234" not in limpo
    assert "[processo]" in limpo


def _research(monkeypatch, question):
    """Roda a pesquisa externa capturando o que chegaria ao buscador web."""
    from core.services.legal_assistant import external_research

    enviado: list[str] = []

    async def fake_search(query, _provider=None):
        enviado.append(query)
        return []

    async def fake_datajud(text):
        return [], [], [], []

    async def fake_pages(_sources):
        return [], [], []

    async def fake_complete(*args, **kwargs):
        return "resposta"

    async def fake_log(*args, **kwargs):
        return None

    monkeypatch.setattr(external_research, "log_access", fake_log)
    monkeypatch.setattr(external_research.websearch, "search", fake_search)
    monkeypatch.setattr(external_research, "_datajud_context", fake_datajud)
    monkeypatch.setattr(external_research, "_read_official_pages", fake_pages)
    monkeypatch.setattr(external_research, "complete", fake_complete)
    state = LegalState(conn=None, question=question, query=question)
    asyncio.run(external_research.research(state))
    return enviado


def test_uma_consulta_de_processo_nao_dispara_busca_web(monkeypatch):
    enviado = _research(monkeypatch, "andamento do processo 0001234-56.2024.8.26.0100")
    assert enviado == [], "o número do cliente não pode sair para um terceiro"


def test_uma_pergunta_juridica_com_processo_ainda_pesquisa_sem_o_numero(monkeypatch):
    enviado = _research(
        monkeypatch,
        "qual o prazo para agravo de instrumento no processo "
        "0001234-56.2024.8.26.0100 do TJSP",
    )
    assert len(enviado) == 1
    assert "0001234" not in enviado[0]
    assert "agravo" in enviado[0]


def test_uma_pergunta_sem_processo_pesquisa_normalmente(monkeypatch):
    enviado = _research(monkeypatch, "prazo para embargos de declaração no CPC")
    assert enviado == ["prazo para embargos de declaração no CPC"]
