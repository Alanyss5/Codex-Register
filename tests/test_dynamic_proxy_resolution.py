from types import SimpleNamespace

from src.core import dynamic_proxy


def test_infer_expected_country_from_dynamic_proxy_url():
    expected = dynamic_proxy.infer_expected_country(
        "https://white.1024proxy.com/white/api?region=US&num=1&format=1&type=txt"
    )

    assert expected == "US"


def test_resolve_proxy_for_task_retries_until_expected_country(monkeypatch):
    fetches = iter(["http://10.0.0.1:7000", "http://10.0.0.2:7000"])
    probes = iter(
        [
            dynamic_proxy.ProxyExitInfo(ip="1.1.1.1", country="HN"),
            dynamic_proxy.ProxyExitInfo(ip="2.2.2.2", country="US"),
        ]
    )

    monkeypatch.setattr(dynamic_proxy, "fetch_dynamic_proxy", lambda **kwargs: next(fetches))
    monkeypatch.setattr(dynamic_proxy, "probe_proxy_exit", lambda *args, **kwargs: next(probes))

    settings = SimpleNamespace(
        proxy_dynamic_enabled=True,
        proxy_dynamic_api_url="https://white.1024proxy.com/white/api?region=US&num=1&format=1&type=txt",
        proxy_dynamic_api_key="",
        proxy_dynamic_api_key_header="X-API-Key",
        proxy_dynamic_result_field="",
        proxy_url=None,
    )

    result = dynamic_proxy.resolve_proxy_for_task(settings=settings, previous_proxy_url=None)

    assert result.proxy_url == "http://10.0.0.2:7000"
    assert result.exit_country == "US"
    assert result.attempts == 2
    assert result.source == "dynamic"
    assert result.matches_expected_country is True


def test_resolve_proxy_for_task_flags_reused_proxy(monkeypatch):
    monkeypatch.setattr(dynamic_proxy, "fetch_dynamic_proxy", lambda **kwargs: "http://10.0.0.1:7000")
    monkeypatch.setattr(
        dynamic_proxy,
        "probe_proxy_exit",
        lambda *args, **kwargs: dynamic_proxy.ProxyExitInfo(ip="2.2.2.2", country="US"),
    )

    settings = SimpleNamespace(
        proxy_dynamic_enabled=True,
        proxy_dynamic_api_url="https://white.1024proxy.com/white/api?region=US&num=1&format=1&type=txt",
        proxy_dynamic_api_key="",
        proxy_dynamic_api_key_header="X-API-Key",
        proxy_dynamic_result_field="",
        proxy_url=None,
    )

    result = dynamic_proxy.resolve_proxy_for_task(settings=settings, previous_proxy_url="http://10.0.0.1:7000")

    assert result.proxy_url == "http://10.0.0.1:7000"
    assert result.reused_proxy is True
