from sync_domain_blacklist import merge_blacklists, normalize_blacklist


def test_normalize_blacklist_lowercases_deduplicates_and_sorts():
    result = normalize_blacklist([
        " VB.cloudvxz.com ",
        "vb.cloudvxz.com",
        "",
        "  ",
        None,
        "Aa.Example.com",
    ])

    assert result == ["aa.example.com", "vb.cloudvxz.com"]


def test_merge_blacklists_returns_union_of_all_items():
    result = merge_blacklists(
        ["a.com", "b.com", "VB.cloudvxz.com"],
        ["b.com", "c.com", " vb.cloudvxz.com "],
        ["D.com"],
    )

    assert result == ["a.com", "b.com", "c.com", "d.com", "vb.cloudvxz.com"]
