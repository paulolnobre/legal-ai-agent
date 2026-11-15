"""Every route that ends web access removes the actual credentials.

The handler and its repository run together on Postgres. A mock recording a
call to revoke_devices would still pass with its DELETE statements broken.
"""
import pytest
from cryptography.fernet import Fernet
from psycopg import errors

from core import totp
from core.config import settings
from core.handlers import legal_message
from core.repositories import push_subscriptions, staff, web_access
from core.services.authorization import decide

pytestmark = pytest.mark.usefixtures('isolate_message_rate_limits', 'callback_store')
SECRET = 'JBSWY3DPEHPK3PXP'


async def _credentials(conn, staff_id):
    await web_access.enrol(conn, staff_id, secret=SECRET)
    await web_access.confirm_enrolment(conn, staff_id)
    key = await web_access.add_passkey(
        conn, staff_id, credential_id=bytes([staff_id]) * 32,
        public_key=b'public-key', sign_count=0,
        user_handle=await web_access.user_handle_for(conn, staff_id))
    assert key
    await web_access.open_session(conn, staff_id, idle_seconds=3600)
    await web_access.open_session(conn, staff_id, idle_seconds=3600,
                                  method='passkey', passkey_id=key, rule='app',
                                  absolute_seconds=18000)
    await push_subscriptions.subscribe(
        conn, staff_id, key, endpoint=f'https://web.push.apple.com/{staff_id}',
        p256dh=b'\x04' + bytes([staff_id]) * 64, auth=bytes([staff_id]) * 16)


async def _counts(conn, staff_id):
    async with conn.cursor() as cur:
        await cur.execute(
            'SELECT (SELECT count(*) FROM staff_passkeys WHERE staff_id = %s), '
            '(SELECT count(*) FROM web_sessions WHERE staff_id = %s), '
            '(SELECT count(*) FROM staff_totp WHERE staff_id = %s), '
            '(SELECT count(*) FROM staff_recovery_codes WHERE staff_id = %s), '
            '(SELECT count(*) FROM push_subscriptions WHERE staff_id = %s)',
            (staff_id,) * 5)
        return await cur.fetchone()


@pytest.mark.parametrize('path', ['end_all', 'reset', 'release', 'deactivate', 'erase'])
def test_every_revocation_path_removes_the_targets_keys_and_sessions(db, monkeypatch, path):
    monkeypatch.setattr(settings, 'web_totp_key', Fernet.generate_key().decode())
    # Recovery must remain possible even with the app and the web pages off.
    monkeypatch.setattr(settings, 'web_enabled', False)
    monkeypatch.setattr(settings, 'web_app', 'off')
    sent = []

    async def send(chat_id, message, **kwargs):
        sent.append((str(chat_id), message))
        return 1

    monkeypatch.setattr(legal_message, '_send', send)

    async def run(conn):
        for who in (4, 5):
            await _credentials(conn, who)
        other = await _counts(conn, 5)
        if path in ('deactivate', 'erase'):
            owner = await staff.get_staff_by_id(conn, 1)
            if path == 'deactivate':
                await legal_message._apply_deactivation(conn, '100', owner, 4)
            else:
                await staff.erase_person(conn, actor_id=1, target_id=4)
        else:
            code = totp.code_at(SECRET, totp.time.time())
            text, chat = {
                'end_all': ('/web encerrar', 400),
                'reset': (f'/mfa refazer {code}', 400),
                'release': ('/mfa liberar 4', 100),
            }[path]
            await legal_message.process_legal_message(conn, {'update_id': 1, 'message': {
                'chat': {'id': chat, 'type': 'private'}, 'from': {'id': chat}, 'text': text,
            }})
        remaining = await _counts(conn, 4)
        assert remaining[:2] == (0, 0) and remaining[4] == 0, (path, remaining)
        if path in ('reset', 'release', 'erase'):
            assert remaining[2:4] == (0, 0)
        else:
            assert remaining[2] == 1
        assert await _counts(conn, 5) == other == (1, 2, 1, web_access.RECOVERY_CODES, 1)
        if path == 'end_all':
            assert sent[-1] == ('400', 'Pronto: todas as chaves de Face ID foram removidas '
                                'e todas as sessões do painel foram encerradas. '
                                'Para voltar, peça um link com /web.')
    db.run(run)


def test_erasure_rolls_back_credential_removal_if_its_audit_cannot_be_written(db, monkeypatch):
    monkeypatch.setattr(settings, 'web_totp_key', Fernet.generate_key().decode())

    async def run(conn):
        await _credentials(conn, 4)
        async with conn.cursor() as cur:
            await cur.execute("CREATE FUNCTION refuse_erasure() RETURNS trigger LANGUAGE plpgsql "
                              "AS $$ BEGIN RAISE EXCEPTION 'audit unavailable'; END $$")
            await cur.execute('CREATE TRIGGER refuse_erasure BEFORE INSERT ON staff_management_log '
                              'FOR EACH ROW EXECUTE FUNCTION refuse_erasure()')
        await conn.commit()
        with pytest.raises(errors.RaiseException):
            await staff.erase_person(conn, actor_id=1, target_id=4)
        await conn.rollback()
        assert await _counts(conn, 4) == (1, 2, 1, web_access.RECOVERY_CODES, 1)
        person = await staff.get_staff_by_id(conn, 4)
        assert person and person.name == 'Rui'
    db.run(run)


def test_every_rank_can_end_only_its_own_web_access():
    from core.models import StaffMember
    for role in staff.ROLE_ORDER:
        assert decide(StaffMember(id=4, name='Rui', role=role), 'web.end_all').allowed
    assert not decide(None, 'web.end_all').allowed
