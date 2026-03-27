import random

import pytest

from src.services.temp_mail_addressing import choose_domain, generate_local_part


def test_generate_local_part_defaults_to_no_tmp_prefix():
    rng = random.Random(123)
    name = generate_local_part(enable_prefix=False, rng=rng)

    assert name
    assert not name.startswith("tmp")
    assert name.isalnum()


def test_generate_local_part_keeps_legacy_prefix_when_enabled():
    rng = random.Random(123)
    name = generate_local_part(enable_prefix=True, rng=rng)

    assert name.startswith("tmp")
    assert name[3:].isalnum()


def test_choose_domain_uses_available_domains():
    rng = random.Random(1)
    selected = choose_domain(["a.example.com", "b.example.com"], rng=rng)

    assert selected in {"a.example.com", "b.example.com"}


def test_choose_domain_raises_for_empty_pool():
    with pytest.raises(ValueError):
        choose_domain([])
