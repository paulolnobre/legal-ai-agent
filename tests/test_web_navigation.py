"""The pilot's shared navigation preserves escaping, CSRF and page policies."""
import html
import json
import re
import shutil
import subprocess
from html.parser import HTMLParser
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.config import settings
from core.models import StaffMember
from core.web import assistant, pages, staff_web

TOKEN = 'a-private-navigation-test-session'


class Elements(HTMLParser):
    def __init__(self, source):
        super().__init__()
        self.tags = []
        self.feed(source)

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))


@pytest.fixture
def pilot(monkeypatch):
    monkeypatch.setattr(settings, 'web_enabled', True)
    monkeypatch.setattr(settings, 'web_app', 'pilot')
    monkeypatch.setattr(settings, 'web_app_staff', '1')
    monkeypatch.setattr(settings, 'web_assistant', 'pilot')
    monkeypatch.setattr(settings, 'web_assistant_staff', '1')
    monkeypatch.setattr(settings, 'web_explorer', 'on')


def _render(*, who=1, name='Paulo', title='Assistente', policy=None):
    member = StaffMember(id=who, name=name, role='owner')
    return pages.page(title, '<h1>Conteúdo</h1>', wide=True,
                      headers={'Content-Security-Policy': policy} if policy else None,
                      navigation=staff_web.nav(member, TOKEN))


def test_pilot_navigation_adds_only_a_local_script_and_its_theme_save_without_changing_other_pages(pilot):
    original = dict(pages.HEADERS)
    response = _render()
    assert response.headers['content-security-policy'] == (
        original['Content-Security-Policy'] + "; script-src 'self'; connect-src 'self'")
    assert pages.HEADERS == original
    plain = pages.page('Entrar', '<h1>Entrar</h1>')
    assert 'script-src' not in plain.headers['content-security-policy']
    assert b'<script' not in plain.body
    assert b'<main class=app-content id=main-content>' in response.body


def test_outside_pilot_keeps_the_existing_navigation_and_policy(pilot, monkeypatch):
    for who in (4, 10, 11):
        response = _render(who=who)
        assert b'sidebar-nav' not in response.body
        assert b'<script' not in response.body
        assert 'script-src' not in response.headers['content-security-policy']
    monkeypatch.setattr(settings, 'web_app', 'off')
    assert b'sidebar-nav' not in _render().body


@pytest.mark.parametrize('path,label', [
    ('/web/integracoes/', 'Integrações'), ('/web/assistente/', 'Assistente'),
    ('/web/explorar/documento/2', 'Arquivos'), ('/web/arquivos/3/texto', 'Arquivos'),
    ('/web/aparelhos/', 'Aparelhos'),
])
def test_the_active_section_is_marked_for_assistive_technology(pilot, path, label):
    token = pages._here.set(path)
    try:
        response = _render()
    finally:
        pages._here.reset(token)
    marked = re.findall(r'aria-current=page>(.*?)</a>', bytes(response.body).decode())
    assert len(marked) == 1
    assert f'<span>{label}</span>' in marked[0]


def test_sidebar_escapes_the_person_and_title_and_preserves_logout_csrf(pilot):
    hostile = '\"><script>alert(1)</script>&'
    response = _render(name=hostile, title=hostile)
    source = bytes(response.body).decode()
    tags = Elements(source).tags
    scripts = [attrs for tag, attrs in tags if tag == 'script']
    assert scripts == [{'type': 'module', 'src': '/web/navigation.js'}]
    assert html.escape(hostile) in source
    assert TOKEN not in source
    assert any(tag == 'form' and attrs.get('method') == 'post'
               and attrs.get('action') == '/web/logout' for tag, attrs in tags)
    assert any(tag == 'input' and attrs.get('name') == 'csrf'
               and attrs.get('value') == staff_web.csrf_for(TOKEN) for tag, attrs in tags)
    assert all(not key.startswith('on') for _, attrs in tags for key in attrs)


def test_sidebar_keeps_the_assistant_policy_and_its_existing_script(pilot):
    response = _render(policy=assistant.POLICY)
    assert response.headers['content-security-policy'] == assistant.POLICY


def test_navigation_script_is_static_and_hidden_when_switched_off(pilot, monkeypatch):
    async def no_database():
        raise AssertionError('static navigation does not need a database')
    monkeypatch.setattr(staff_web, 'get_pool', no_database)
    app = FastAPI()
    app.include_router(staff_web.router)
    with TestClient(app) as client:
        response = client.get('/web/navigation.js')
        assert response.status_code == 200
        assert response.headers['content-type'].startswith('text/javascript')
        assert 'no-cache' in response.headers['cache-control']
        monkeypatch.setattr(settings, 'web_app', 'off')
        assert client.get('/web/navigation.js').status_code == 404
        monkeypatch.setattr(settings, 'web_app', 'pilot')
        monkeypatch.setattr(settings, 'web_enabled', False)
        assert client.get('/web/navigation.js').status_code == 404


# navigation.js runs under Node (tests/fixtures/navigation_harness.mjs) with a
# stand-in for the page. What is asserted is what a click on "Tema" does to
# the page, not the source text. The Chromium check (scroll kept, menu open,
# choice kept after a reload) is in the PR.

NODE = shutil.which('node')
HARNESS = Path(__file__).parent / 'fixtures' / 'navigation_harness.mjs'
SCRIPT = Path(staff_web.__file__).parent / 'static' / 'navigation.js'
needs_node = pytest.mark.skipif(
    NODE is None, reason='runs navigation.js under Node, which is not installed here')
BOTH = [['(prefers-color-scheme: light)', pages.DAY_GROUND],
        ['(prefers-color-scheme: dark)', pages.NIGHT_GROUND]]


def _clicks(**scenario) -> dict:
    assert NODE
    done = subprocess.run([NODE, str(HARNESS), str(SCRIPT), json.dumps(scenario)],
                          capture_output=True, text=True, timeout=30, check=True)
    state = json.loads(done.stdout)
    assert state['errors'] == []
    return state


@needs_node
@pytest.mark.parametrize('mobile', [False, True])
def test_the_theme_changes_in_place_and_the_menu_stays_open(mobile):
    """A reload closed the phone's menu and sent the assistant's conversation
    back to its first message (Paulo, 2026-10-27)."""
    state = _clicks(mobile=mobile, menuOpen=mobile, clicks=3, fetch='ok')

    assert [step['theme'] for step in state['steps']] == ['light', 'dark', '']
    assert [step['label'] for step in state['steps']] == [
        'Tema: claro', 'Tema: escuro', 'Tema: automático']
    assert [step['bar'] for step in state['steps']] == [
        [['', pages.DAY_GROUND]], [['', pages.NIGHT_GROUND]], BOTH]
    assert all(step['menuOpen'] is mobile for step in state['steps'])
    assert state['followed'] == [] and state['assigned'] == []
    assert [call['url'] for call in state['fetched']] == [
        '/web/tema?para=claro', '/web/tema?para=escuro', '/web/tema?para=auto']
    assert {call['credentials'] for call in state['fetched']} == {'same-origin'}


@needs_node
def test_the_next_theme_follows_the_one_on_screen():
    state = _clicks(theme='dark', clicks=1, fetch='ok')

    assert state['fetched'][0]['url'] == '/web/tema?para=auto'
    assert state['steps'][0]['theme'] == '' and state['steps'][0]['bar'] == BOTH


@needs_node
@pytest.mark.parametrize('failure', ['refused', 'offline'])
def test_when_saving_fails_the_link_reloads_as_before(failure):
    """Nothing is repainted that the server did not keep."""
    state = _clicks(theme='light', clicks=1, fetch=failure)

    assert state['assigned'] == ['/web/tema?volta=%2Fweb%2Fassistente%2F']
    assert state['steps'][0]['theme'] == 'light'
    assert state['steps'][0]['label'] == 'Tema: inicial'
