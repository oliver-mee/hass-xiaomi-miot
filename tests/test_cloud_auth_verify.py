"""Tests for verify_ticket — preserves challenge and raises typed outcomes."""
import pytest

from custom_components.xiaomi_miot import init_integration_data
from custom_components.xiaomi_miot.core.xiaomi_cloud import (
    MiCloudException,
    MiCloudVerificationError,
    MiotCloud,
)


def _cloud(hass):
    return MiotCloud(hass, "u", "p", "cn", "xiaomiio")


async def test_verify_ticket_missing_url_raises_micloud(hass):
    init_integration_data(hass)
    c = _cloud(hass)
    with pytest.raises(MiCloudException):
        await hass.async_add_executor_job(c.verify_ticket, "TICKET")


async def test_verify_ticket_missing_identity_session_raises_micloud(hass):
    init_integration_data(hass)
    c = _cloud(hass)
    c.attrs["verify_url"] = "https://account.xiaomi.com/identity/authStart"
    c.check_identity_list = lambda url, path="fe/service/identity/authStart": (_ for _ in ()).throw(MiCloudException("missing"))
    # The above hack won't propagate cleanly; use a real exception-raising stub:
    def _raise(*a, **k):
        raise MiCloudException("Xiaomi identity session missing")
    c.check_identity_list = _raise
    with pytest.raises(MiCloudException):
        await hass.async_add_executor_job(c.verify_ticket, "TICKET")


async def test_verify_ticket_non_zero_each_method_raises_verification(hass):
    init_integration_data(hass)
    c = _cloud(hass)
    c.attrs["verify_url"] = "https://account.xiaomi.com/identity/authStart"
    c.check_identity_list = lambda url, path="fe/service/identity/authStart": [4]
    c.account_post = lambda *a, **k: {"code": 87001}
    with pytest.raises(MiCloudVerificationError):
        await hass.async_add_executor_job(c.verify_ticket, "TICKET")
    # verify_url preserved so reauth form can retry
    assert c.attrs.get("verify_url") == "https://account.xiaomi.com/identity/authStart"


async def test_verify_ticket_success_returns_data(hass):
    init_integration_data(hass)
    c = _cloud(hass)
    c.attrs["verify_url"] = "https://account.xiaomi.com/identity/authStart"
    c.attrs["identity_session"] = "IS"
    c.check_identity_list = lambda url, path="fe/service/identity/authStart": [4]
    c.account_post = lambda *a, **k: {"code": 0, "location": "/x?userId=1"}
    ret = await hass.async_add_executor_job(c.verify_ticket, "TICKET")
    assert ret.get("code") == 0
    # identity_session cleared on success
    assert "identity_session" not in c.attrs


async def test_verify_ticket_no_supported_method_raises_micloud(hass):
    init_integration_data(hass)
    c = _cloud(hass)
    c.attrs["verify_url"] = "https://account.xiaomi.com/identity/authStart"
    # Return a flag that has no api mapping (not 4 or 8)
    c.check_identity_list = lambda url, path="fe/service/identity/authStart": [99]
    with pytest.raises(MiCloudException):
        await hass.async_add_executor_job(c.verify_ticket, "TICKET")


async def test_verify_ticket_pins_trust_false_and_payload(hass):
    """Pin trust=false (browser-confirmed) and the surrounding payload fields.

    Flipping any of these silently breaks the embedded-callback path that
    7f454747 locked down."""
    init_integration_data(hass)
    c = _cloud(hass)
    c.attrs["verify_url"] = "https://account.xiaomi.com/identity/authStart"
    c.attrs["identity_session"] = "IS"
    c.check_identity_list = lambda url, path="fe/service/identity/authStart": [4]
    captured = {}

    def _capture(*args, **kwargs):
        captured["args"] = args
        captured.update(kwargs)
        return {"code": 0}

    c.account_post = _capture
    await hass.async_add_executor_job(c.verify_ticket, "TICKET")

    assert captured["args"][0] == "/identity/auth/verifyPhone"
    assert captured["data"]["trust"] == "false"
    assert captured["data"]["ticket"] == "TICKET"
    assert captured["data"]["_flag"] == 4
    assert captured["data"]["_json"] == "true"
    assert captured["cookies"]["identity_session"] == "IS"

async def test_email_challenge_reuses_session_and_sends_once(hass):
    from types import SimpleNamespace
    from unittest.mock import Mock
    c = _cloud(hass)
    c.attrs['verify_url'] = 'https://account.xiaomi.com/fe/service/identity/authStart?sid=xiaomiio'
    c.account_get = Mock(return_value=SimpleNamespace(
        cookies={'identity_session': 'same-session'}, text='{"options":[8]}'))
    c.account_post = Mock(side_effect=[{'code': 0}, {'code': 70014}, {'code': 0}])
    assert await hass.async_add_executor_job(c.prepare_email_verification)
    assert await hass.async_add_executor_job(c.prepare_email_verification)
    with pytest.raises(MiCloudVerificationError):
        await hass.async_add_executor_job(c.verify_ticket, 'wrong')
    await hass.async_add_executor_job(c.verify_ticket, 'right')
    c.account_get.assert_called_once()
    assert [call.args[0] for call in c.account_post.call_args_list] == [
        '/identity/auth/sendEmailTicket', '/identity/auth/verifyEmail', '/identity/auth/verifyEmail',
    ]
    assert all(call.kwargs['cookies']['identity_session'] == 'same-session'
               for call in c.account_post.call_args_list)
    assert 'identity_session' not in c.attrs


async def test_phone_delivery_is_not_inferred(hass):
    from unittest.mock import Mock
    c = _cloud(hass)
    c.attrs['verify_url'] = 'https://account.xiaomi.com/fe/service/identity/authStart'
    c.check_identity_list = Mock(return_value=[4])
    c.account_post = Mock()
    assert not await hass.async_add_executor_job(c.prepare_email_verification)
    c.account_post.assert_not_called()


async def test_new_challenge_url_gets_new_session(hass):
    from types import SimpleNamespace
    from unittest.mock import Mock
    c = _cloud(hass)
    c.account_get = Mock(side_effect=[
        SimpleNamespace(cookies={'identity_session': 'first'}, text='{"options":[8]}'),
        SimpleNamespace(cookies={'identity_session': 'second'}, text='{"options":[8]}'),
    ])
    c.account_post = Mock(return_value={'code': 0})
    for suffix in ('one', 'two'):
        c.attrs['verify_url'] = f'https://account.xiaomi.com/fe/service/identity/authStart?challenge={suffix}'
        await hass.async_add_executor_job(c.prepare_email_verification)
    assert c.account_get.call_count == 2
    assert c.account_post.call_count == 2
    assert c.attrs['identity_session'] == 'second'
