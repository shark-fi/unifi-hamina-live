"""Login failures, and saying which kind they are.

Only the response body explains a 429, and we reported the bare status code —
so "login failed: HTTP 429" was all anyone had to work with.

The body is not the whole answer either. AUTHENTICATION_FAILED_LIMIT_REACHED
reads like a rejected password and is not: UniFi OS returns it for the
login-attempt limiter generally, so valid credentials used too often trip it.
Reading it as "wrong password" led to a working admin account nearly being
recreated. The message has to carry both causes.
"""
import httpx

from unifi_hamina_live.unifi.client import UniFiClient, UniFiError


def _client(handler) -> UniFiClient:
    c = UniFiClient("https://console", "user", "pw", verify_tls=False)
    c._client = httpx.AsyncClient(
        base_url="https://console",
        transport=httpx.MockTransport(handler),
    )
    return c


LOCKED_OUT = {
    "message": "You've reached the login attempt limit",
    "code": "AUTHENTICATION_FAILED_LIMIT_REACHED",
    "level": "debug",
}


async def test_a_lockout_does_not_claim_the_password_is_wrong():
    """The code names authentication, but the cause is often just volume.

    This bridge tripped it with correct credentials by authenticating ~120
    times an hour. A message asserting "the credentials are REFUSED" would be
    confidently wrong in exactly the case that produced it.
    """
    async def handler(request):
        return httpx.Response(429, json=LOCKED_OUT)

    try:
        await _client(handler).login()
    except UniFiError as exc:
        msg = str(exc)
    else:
        raise AssertionError("a 429 must not look like a successful login")

    assert "AUTHENTICATION_FAILED_LIMIT_REACHED" in msg
    assert "NOT proof the password is wrong" in msg
    assert "wait" in msg, "volume is the likelier cause and waiting clears it"
    assert "LOCAL admin" in msg, "but still name the credentials case"
    assert "role" in msg, (
        "the cause that actually produced this: the right password on an "
        "account whose role does not reach the Network application"
    )


async def test_a_lockout_does_not_trigger_a_second_failed_login():
    """The classic-controller fallback was doubling the damage.

    A 429 used to fall through to /api/login — a second failed login against a
    console that is already counting them, so the compatibility path for old
    controllers dug the hole twice as fast.
    """
    paths = []

    async def handler(request):
        paths.append(request.url.path)
        return httpx.Response(429, json=LOCKED_OUT)

    try:
        await _client(handler).login()
    except UniFiError:
        pass

    assert paths == ["/api/auth/login"], f"attempted {paths}"


async def test_a_plain_429_still_reports_the_console_wording():
    async def handler(request):
        return httpx.Response(429, json={"message": "Too many requests"})

    try:
        await _client(handler).login()
    except UniFiError as exc:
        assert "Too many requests" in str(exc)
        assert "LOCAL admin" not in str(exc), "no credentials advice here"


async def test_a_non_json_body_falls_back_to_the_status_code():
    """Consoles behind a proxy can answer with HTML; that must not crash us."""
    async def handler(request):
        return httpx.Response(429, text="<html>nginx</html>")

    try:
        await _client(handler).login()
    except UniFiError as exc:
        assert "429" in str(exc)


async def test_other_failures_still_reach_the_classic_controller():
    """Old controllers 404 the UniFi OS path — that fallback must survive."""
    paths = []

    async def handler(request):
        paths.append(request.url.path)
        if request.url.path == "/api/auth/login":
            return httpx.Response(404)
        return httpx.Response(200, json={"meta": {"rc": "ok"}})

    await _client(handler).login()
    assert paths == ["/api/auth/login", "/api/login"]
