"""Exposure-mode resolution tests."""

from server.tunnel import get_exposure_mode


def test_fresh_install_defaults_to_off():
    assert get_exposure_mode({}) == "off"


def test_explicit_modes_are_preserved():
    for mode in ("off", "temporary", "authenticated"):
        assert get_exposure_mode({"WOLTSPACE_EXPOSURE": mode}) == mode


def test_invalid_mode_fails_closed():
    assert get_exposure_mode({"WOLTSPACE_EXPOSURE": "public-ish"}) == "off"


def test_explicit_mode_wins_over_legacy_setting():
    env = {
        "WOLTSPACE_EXPOSURE": "off",
        "WOLTSPACE_PUBLIC_TUNNEL": "true",
        "CLOUDFLARE_TUNNEL_TOKEN": "synthetic-token",
        "CLOUDFLARE_TUNNEL_URL": "https://example.invalid",
    }
    assert get_exposure_mode(env) == "off"


def test_legacy_false_maps_to_off():
    assert get_exposure_mode({"WOLTSPACE_PUBLIC_TUNNEL": "false"}) == "off"


def test_legacy_true_without_named_tunnel_maps_to_temporary():
    assert get_exposure_mode({"WOLTSPACE_PUBLIC_TUNNEL": "true"}) == "temporary"


def test_legacy_true_with_named_tunnel_maps_to_authenticated():
    env = {
        "WOLTSPACE_PUBLIC_TUNNEL": "true",
        "CLOUDFLARE_TUNNEL_TOKEN": "synthetic-token",
        "CLOUDFLARE_TUNNEL_URL": "https://example.invalid",
    }
    assert get_exposure_mode(env) == "authenticated"
