"""Offline unit tests for the Surfshark VPN manager, config builders and UK failover."""

import json
import stat

import pytest

from vpn import manager as vm

SAMPLE_WG_CONF = """\
[Interface]
PrivateKey = cHJpdmF0ZWtleWV4YW1wbGUxMjM0NTY3ODkwMTIzNDU2Nzg5MDEyMzQ=
Address = 10.14.0.2/16, fd00::2/64
DNS = 162.252.172.57, 149.154.159.92, 2606:4700:4700::1111
PostUp = rm -rf /should/be/stripped

[Peer]
PublicKey = cHVibGlja2V5ZXhhbXBsZTEyMzQ1Njc4OTAxMjM0NTY3ODkwMTIzNDU=
AllowedIPs = 0.0.0.0/0, ::/0
Endpoint = uk-lon.prod.surfshark.com:51820
"""

SAMPLE_OVPN_CONF = """\
client
dev tun
proto udp
remote uk-man.prod.surfshark.com 1194
resolv-retry infinite
nobind
persist-key
persist-tun
auth-user-pass
auth-nocache
cipher AES-256-CBC
redirect-gateway def1
<ca>
-----BEGIN CERTIFICATE-----
MIIB...
-----END CERTIFICATE-----
</ca>
"""


def test_normalize_profile_name():
    assert vm.normalize_profile_name("uk-lon.prod.surfshark.com.conf") == "uk-lon"
    assert vm.normalize_profile_name("uk-man.prod.surfshark.com_udp.ovpn") == "uk-man-udp"
    assert vm.normalize_profile_name("Surfshark_WireGuard_uk-gla.conf") == "uk-gla"
    assert vm.normalize_profile_name("GB-Edi.CONF") == "gb-edi"


def test_infer_country_tag():
    assert vm.infer_country_tag("uk-lon", "uk-lon.prod.surfshark.com") == "uk"
    assert vm.infer_country_tag("gb-man", "gb-man.prod.surfshark.com") == "uk"
    assert vm.infer_country_tag("us-nyc", "us-nyc.prod.surfshark.com") == "us"
    assert vm.infer_country_tag("customprofile", "198.51.100.10") == ""


def test_load_vpn_config_merges_local_override(tmp_path):
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps({"preferred_country": "uk", "hotspot": {"ssid": "DefaultSSID"}}), encoding="utf-8")
    vm.update_local_config(cfg_path, {"hotspot": {"ssid": "MyUKHotspot", "passphrase": "StrongPassword123"}})

    local_path = tmp_path / "config.local.json"
    assert local_path.is_file()
    assert stat.S_IMODE(local_path.stat().st_mode) == 0o600

    loaded = vm.load_vpn_config(cfg_path)
    assert loaded["hotspot"]["ssid"] == "MyUKHotspot"
    assert loaded["hotspot"]["passphrase"] == "StrongPassword123"
    assert loaded["route_table"] == 52000


def test_import_profiles_handles_wireguard_and_openvpn_collision(tmp_path):
    src_dir = tmp_path / "downloads"
    src_dir.mkdir()
    (src_dir / "uk-lon.prod.surfshark.com.conf").write_text(SAMPLE_WG_CONF, encoding="utf-8")
    (src_dir / "uk-lon.prod.surfshark.com.ovpn").write_text(SAMPLE_OVPN_CONF, encoding="utf-8")
    (src_dir / "us-nyc.prod.surfshark.com.conf").write_text(
        SAMPLE_WG_CONF.replace("uk-lon", "us-nyc"),
        encoding="utf-8",
    )

    profiles_dir = tmp_path / "profiles"
    imported = vm.import_profiles(src_dir, profiles_dir)
    names = {p.name: p for p in imported}

    assert "uk-lon" in names
    assert names["uk-lon"].protocol == "wireguard"
    assert names["uk-lon"].country == "uk"

    assert "uk-lon-ovpn" in names
    assert names["uk-lon-ovpn"].protocol == "openvpn"
    assert names["uk-lon-ovpn"].requires_auth is True

    assert "us-nyc" in names
    assert names["us-nyc"].country == "us"

    for item in imported:
        assert stat.S_IMODE(item.path.stat().st_mode) == 0o600


def test_select_failover_candidates_prioritizes_uk_wireguard(tmp_path):
    profiles = [
        vm.Profile(name="us-nyc", protocol="wireguard", path=tmp_path / "us-nyc.conf", country="us"),
        vm.Profile(name="uk-man-udp", protocol="openvpn", path=tmp_path / "uk-man-udp.ovpn", country="uk", transport="udp"),
        vm.Profile(name="uk-lon", protocol="wireguard", path=tmp_path / "uk-lon.conf", country="uk"),
        vm.Profile(name="uk-gla", protocol="wireguard", path=tmp_path / "uk-gla.conf", country="uk"),
        vm.Profile(name="uk-edi", protocol="wireguard", path=tmp_path / "uk-edi.conf", country="uk"),
    ]

    # Initial selection picks alphabetical UK WireGuard, never US
    initial = vm.select_failover_candidates(profiles, current_name=None, preferred_country="uk", prefer_protocol="wireguard")
    assert [p.name for p in initial] == ["uk-edi", "uk-gla", "uk-lon", "uk-man-udp"]
    assert all(p.country == "uk" for p in initial)

    # Failing over from uk-gla rotates to uk-lon -> uk-edi -> uk-man-udp (OpenVPN last)
    rotated = vm.select_failover_candidates(profiles, current_name="uk-gla", preferred_country="uk", prefer_protocol="wireguard")
    assert [p.name for p in rotated] == ["uk-lon", "uk-edi", "uk-man-udp"]


def test_build_wireguard_runtime_config_sanitizes_and_injects_tuning(tmp_path):
    cfg = vm.load_vpn_config(tmp_path / "config.json")
    rendered, ipv4_hosts, dns_servers = vm.build_wireguard_runtime_config(SAMPLE_WG_CONF, cfg)

    assert ipv4_hosts == ["10.14.0.2"]
    assert dns_servers == ["162.252.172.57", "149.154.159.92"]
    assert "Table = off" in rendered
    assert "MTU = 1420" in rendered
    assert "PersistentKeepalive = 25" in rendered
    # Host DNS and unsafe hooks must be stripped; IPv6 filtered when disable_ipv6=True
    assert "DNS =" not in rendered
    assert "PostUp" not in rendered
    assert "fd00::2" not in rendered
    assert "AllowedIPs = 0.0.0.0/0\n" in rendered


def test_build_wireguard_runtime_config_uses_fallback_key(tmp_path):
    cfg = vm.load_vpn_config(tmp_path / "config.json")
    cfg["wireguard"]["private_key"] = "fallbackKeyFromLocalConfig=="
    templated = SAMPLE_WG_CONF.replace(
        "cHJpdmF0ZWtleWV4YW1wbGUxMjM0NTY3ODkwMTIzNDU2Nzg5MDEyMzQ=",
        "<INSERT_PRIVATE_KEY>",
    )
    rendered, _, _ = vm.build_wireguard_runtime_config(templated, cfg)
    assert "PrivateKey = fallbackKeyFromLocalConfig==" in rendered


def test_build_openvpn_runtime_config_fixes_surfshark_quirks(tmp_path):
    cfg = vm.load_vpn_config(tmp_path / "config.json")
    auth_path = tmp_path / "auth.txt"
    rendered = vm.build_openvpn_runtime_config(
        SAMPLE_OVPN_CONF,
        cfg,
        auth_path=auth_path,
        route_up_command="/usr/bin/python3 /opt/rp-inky/vpn/manager.py internal-openvpn-up",
    )

    # Removes auth-nocache (fixes 1-hour renegotiation drop) and redirect-gateway
    assert "auth-nocache" not in rendered
    assert "redirect-gateway" not in rendered
    assert "dev tun-vpn" in rendered
    assert "route-nopull" in rendered
    assert "fast-io" in rendered
    assert f"auth-user-pass {auth_path}" in rendered
    assert "data-ciphers CHACHA20-POLY1305:AES-128-GCM:AES-256-GCM:AES-256-CBC" in rendered
    assert "-----BEGIN CERTIFICATE-----" in rendered


def test_build_hostapd_config_5ghz_vht80(tmp_path):
    cfg = vm.load_vpn_config(tmp_path / "config.json")
    cfg["hotspot"]["passphrase"] = "SuperSecretPass!"
    cfg["hotspot"]["country_code"] = "US"
    cfg["hotspot"]["band"] = "5g"
    cfg["hotspot"]["channel"] = 36

    text = vm.build_hostapd_config(cfg)
    assert "hw_mode=a" in text
    assert "channel=36" in text
    assert "ht_capab=[HT40+][SHORT-GI-20][SHORT-GI-40]" in text
    assert "ieee80211ac=1" in text
    assert "vht_oper_chwidth=1" in text
    assert "vht_oper_centr_freq_seg0_idx=42" in text


def test_build_hostapd_config_rejects_short_passphrase(tmp_path):
    cfg = vm.load_vpn_config(tmp_path / "config.json")
    cfg["hotspot"]["passphrase"] = "short"
    with pytest.raises(ValueError, match="8 to 63"):
        vm.build_hostapd_config(cfg)


def test_build_nftables_ruleset_enforces_kill_switch_and_mss_clamping(tmp_path):
    cfg = vm.load_vpn_config(tmp_path / "config.json")
    ruleset = vm.build_nftables_ruleset(cfg, dns_servers=["162.252.172.57"], wan_ipv4="192.168.1.200")

    assert "tcp flags syn / syn,rst tcp option maxseg size set rt mtu" in ruleset
    assert 'iifname "wlan0" meta l4proto { udp, tcp } th dport 53 dnat ip to 162.252.172.57:53' in ruleset
    assert 'iifname "eth0" ip daddr 192.168.1.200 meta l4proto { udp, tcp } th dport 53 dnat ip to 162.252.172.57:53' in ruleset
    assert 'iifname "wlan0" oifname { "wg-vpn", "tun-vpn" } accept' in ruleset
    assert 'iifname "wlan0" drop' in ruleset
    assert 'iifname "eth0" oifname "eth0" drop' in ruleset
    assert 'oifname { "wg-vpn", "tun-vpn" } masquerade' in ruleset


def test_build_dnsmasq_config_is_dhcp_only(tmp_path):
    cfg = vm.load_vpn_config(tmp_path / "config.json")
    text = vm.build_dnsmasq_config(cfg, dns_servers=["162.252.172.57", "149.154.159.92"])
    assert "port=0" in text
    assert "interface=wlan0" in text
    assert "dhcp-option=option:dns-server,162.252.172.57,149.154.159.92" in text


def test_watchdog_rotates_to_next_uk_profile_after_threshold(tmp_path, monkeypatch):
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps({"watchdog": {"failover_threshold": 2}}), encoding="utf-8")
    cfg = vm.load_vpn_config(cfg_path)
    profiles_dir = cfg["_profiles_dir"]
    profiles_dir.mkdir(parents=True, exist_ok=True)
    (profiles_dir / "uk-lon.conf").write_text(SAMPLE_WG_CONF, encoding="utf-8")
    (profiles_dir / "uk-man.conf").write_text(SAMPLE_WG_CONF.replace("uk-lon", "uk-man"), encoding="utf-8")
    vm.save_state(cfg, active_profile="uk-lon", desired_state="running")

    runtime_dir = tmp_path / "run"
    monkeypatch.setattr(vm, "RUNTIME_DIR", runtime_dir)
    monkeypatch.setattr(vm, "require_root", lambda: None)
    monkeypatch.setattr(vm, "probe_interface_connectivity", lambda *a, **kw: False)
    monkeypatch.setattr(vm, "probe_wan_connectivity", lambda *a, **kw: True)
    monkeypatch.setattr(vm, "interface_exists", lambda *a, **kw: False)
    restarted = []
    monkeypatch.setattr(vm, "restart_vpn_unit_or_direct", lambda c: restarted.append(vm.read_state(c)["active_profile"]))

    # First failure restarts current profile (uk-lon)
    assert vm.run_watchdog(cfg) == 0
    assert vm.read_state(cfg)["active_profile"] == "uk-lon"
    assert restarted == ["uk-lon"]

    # Second consecutive failure reaches threshold=2 and fails over to uk-man
    assert vm.run_watchdog(cfg) == 0
    assert vm.read_state(cfg)["active_profile"] == "uk-man"
    assert restarted == ["uk-lon", "uk-man"]


def test_resolv_conf_needs_repair_detects_surfshark_private_dns():
    stale = "# Generated by resolvconf\nnameserver 162.252.172.57\nnameserver 149.154.159.92\n"
    assert vm.resolv_conf_needs_repair(stale) is True
    assert vm.resolv_conf_needs_repair("") is True
    healthy = "# Generated by NetworkManager\nnameserver 192.168.86.1\n"
    assert vm.resolv_conf_needs_repair(healthy) is False


def test_dns_codec_roundtrip_and_endpoint_override(tmp_path):
    query = vm._encode_dns_query("uk-lon.prod.surfshark.com", 0x1234)
    assert query[:2] == b"\x12\x34"
    # Craft minimal DNS response with compressed pointer 0xC00C -> 185.108.105.10
    header = b"\x12\x34\x81\x80\x00\x01\x00\x01\x00\x00\x00\x00"
    question = query[12:]
    answer = b"\xc0\x0c\x00\x01\x00\x01\x00\x00\x00\x3c\x00\x04" + bytes([185, 108, 105, 10])
    assert vm._decode_dns_a_response(header + question + answer, 0x1234) == "185.108.105.10"

    cfg = vm.load_vpn_config(tmp_path / "config.json")
    rendered, _, _ = vm.build_wireguard_runtime_config(
        SAMPLE_WG_CONF,
        cfg,
        endpoint_override="185.108.105.10:51820",
    )
    assert "Endpoint = 185.108.105.10:51820" in rendered


