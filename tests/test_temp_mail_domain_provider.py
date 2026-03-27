from src.services.temp_mail_domain_provider import resolve_temp_mail_domains, summarize_temp_mail_domains
from src.core.email_service_catalog import build_temp_mail_service_entry


def test_resolve_prefers_config_domains_and_skips_worker_fetch():
    called = {"value": False}

    def _fetcher():
        called["value"] = True
        return {"domains": ["ignored.example.com"]}

    domains = resolve_temp_mail_domains(
        {
            "domains": ["a.example.com", "a.example.com", "  b.example.com  ", ""],
            "domain": "fallback.example.com",
        },
        fetch_domains=_fetcher,
    )

    assert domains == ["a.example.com", "b.example.com"]
    assert called["value"] is False


def test_resolve_uses_worker_domains_when_config_pool_missing():
    domains = resolve_temp_mail_domains(
        {"domain": "fallback.example.com"},
        fetch_domains=lambda: {"domains": ["w1.example.com", "w2.example.com", "w1.example.com"]},
    )

    assert domains == ["w1.example.com", "w2.example.com"]


def test_resolve_falls_back_to_single_domain_when_worker_unavailable():
    def _boom():
        raise RuntimeError("worker down")

    domains = resolve_temp_mail_domains(
        {"domain": "fallback.example.com"},
        fetch_domains=_boom,
    )

    assert domains == ["fallback.example.com"]


def test_summarize_returns_preview_and_source():
    summary = summarize_temp_mail_domains(
        {"domain": "fallback.example.com"},
        fetch_domains=lambda: {"domains": ["d1.example.com", "d2.example.com", "d3.example.com", "d4.example.com"]},
        preview_limit=3,
    )

    assert summary["domain_source"] == "worker_api"
    assert summary["domain_count"] == 4
    assert summary["domains_preview"] == ["d1.example.com", "d2.example.com", "d3.example.com"]
    assert summary["domain"] == "d1.example.com"


def test_catalog_entry_exposes_temp_mail_domain_summary_without_secrets():
    class Service:
        id = 12
        name = "mail-worker"
        priority = 2
        config = {
            "domain": "fallback.example.com",
            "admin_password": "admin888",
        }

    entry = build_temp_mail_service_entry(
        Service(),
        fetch_domains=lambda: {"domains": ["d1.example.com", "d2.example.com"]},
        preview_limit=1,
    )

    assert entry["id"] == 12
    assert entry["type"] == "temp_mail"
    assert entry["domain_count"] == 2
    assert entry["domains_preview"] == ["d1.example.com"]
    assert entry["domain_source"] == "worker_api"
    assert "admin_password" not in entry
