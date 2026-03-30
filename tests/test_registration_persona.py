from src.core.registration_engines.persona import build_persona


def test_build_persona_for_us_is_self_consistent():
    persona = build_persona(runtime_country="US", runtime_language="en-US")

    assert persona.country == "US"
    assert persona.locale == "en-US"
    assert persona.accept_language == "en-US,en;q=0.9"
    assert persona.timezone_id.startswith("America/")
    assert persona.platform == "Win32"
    assert persona.profile.impersonate.startswith("chrome")
    assert "Windows NT 10.0" in persona.profile.user_agent
    assert persona.screen.width > 0
    assert persona.screen.height > 0
    assert persona.device_pixel_ratio >= 1
    assert persona.hardware_concurrency in {4, 8, 12, 16}
    assert persona.device_memory in {4, 8, 16}
    assert persona.max_touch_points in {0, 1}


def test_persona_summary_matches_profile_and_runtime():
    persona = build_persona(runtime_country="US", runtime_language="en-US")
    summary = persona.summary()

    assert summary["country"] == "US"
    assert summary["locale"] == "en-US"
    assert summary["timezone_id"] == persona.timezone_id
    assert summary["user_agent"] == persona.profile.user_agent
    assert summary["impersonate"] == persona.profile.impersonate
    assert summary["screen"] == f"{persona.screen.width}x{persona.screen.height}"
