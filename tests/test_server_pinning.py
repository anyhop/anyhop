"""Choosing a concrete provider server: listing a location's servers, pinning a
channel to one (add / setserver / auto), and the pin holding everywhere a
server is re-resolved — token replacement, auto-reconnect, bundles — plus the
CLI and REST surfaces. The NordVPN API is faked at the HTTP boundary
(``providers._get_json``), so every layer above it runs for real."""

from __future__ import annotations

import base64
import json
import re
from typing import cast

import pytest
import yaml

from anyhop import bundle, cli, credentials, providers, reconnect, service, singbox
from anyhop.api import server
from anyhop.state import Store
from conftest import start_test_server, stop_test_server

PRIV = base64.b64encode(bytes([7] * 32)).decode()
PUB = base64.b64encode(bytes([8] * 32)).decode()

COUNTRIES = [
    {
        "id": 81,
        "name": "Germany",
        "code": "DE",
        "cities": [{"id": 100, "name": "Frankfurt"}, {"id": 101, "name": "Berlin"}],
    },
    {
        "id": 227,
        "name": "United Kingdom",
        "code": "GB",
        "cities": [{"id": 200, "name": "London"}],
    },
]


def _srv(name: str, city: str, load: int, country: str = "Germany", ip: str = ""):
    return {
        "hostname": f"{name}.nordvpn.com",
        "load": load,
        "station": ip or f"10.0.0.{len(name)}{load}",
        "technologies": [
            {
                "identifier": "wireguard_udp",
                "metadata": [{"name": "public_key", "value": PUB}],
            }
        ],
        "locations": [{"country": {"name": country, "city": {"name": city}}}],
    }


class FakeNord:
    """The slice of api.nordvpn.com anyhop reads, in the API's own order.

    Like the real API, country-level *recommendations* silently drop whole
    cities (here: Berlin) — only ``/servers`` lists a country completely.
    """

    def __init__(self):
        self.de = [
            _srv("de1", "Frankfurt", 20, ip="10.0.1.1"),
            _srv("de10", "Frankfurt", 5, ip="10.0.1.10"),
            _srv("de2", "Berlin", 5, ip="10.0.1.2"),
            {"hostname": "de3.nordvpn.com", "load": 1, "technologies": []},  # no key
        ]
        self.uk = [_srv("uk7", "London", 9, country="United Kingdom")]
        self.dedicated = [
            {
                "hostname": "de99.nordvpn.com",
                "groups": [{"identifier": "legacy_dedicated_ip"}],
            }
        ]
        self.down = False
        self.urls: list[str] = []

    def __call__(self, url, headers=None, timeout=30):
        self.urls.append(url)
        if url.endswith("/servers/countries"):
            return COUNTRIES
        if "/users/services/credentials" in url:
            return {"nordlynx_private_key": PRIV}
        if self.down:
            raise providers.ProviderUnreachableError("could not reach the API")
        recommend = "/servers/recommendations" in url
        if recommend or "legacy_standard" in url:
            if "country_city_id]=100" in url:
                rows = [s for s in self.de if "Frankfurt" in json.dumps(s)]
            elif "country_city_id]=101" in url:
                rows = [s for s in self.de if "Berlin" in json.dumps(s)]
            elif "country_id]=81" in url:
                rows = [s for s in self.de if not recommend or "Berlin" not in str(s)]
            elif "country_id]=227" in url:
                rows = list(self.uk)
            else:
                rows = []
            limit = re.search(r"limit=(\d+)", url)
            return rows[: int(limit[1])] if limit else rows
        if "/servers?" in url:
            return self.dedicated
        raise AssertionError(f"unexpected NordVPN API call: {url}")


@pytest.fixture
def nord(monkeypatch):
    fake = FakeNord()
    monkeypatch.setattr(providers, "_get_json", fake)
    providers.forget_nord_countries()
    providers.forget_nord_servers()
    Store.load().add_provider("nordvpn")
    credentials.set_("nordvpn", {"token": "tok"})
    yield fake
    providers.forget_nord_countries()
    providers.forget_nord_servers()


def _channel(cid: str):
    ch = Store.load().get_channel("nordvpn", cid)
    assert ch is not None
    return ch


# ---- provider layer --------------------------------------------------------


def test_server_names_are_canonicalized():
    assert providers.nordvpn_server_name("de1398") == "de1398.nordvpn.com"
    assert providers.nordvpn_server_name(" DE1398.NordVPN.com ") == (
        "de1398.nordvpn.com"
    )
    with pytest.raises(providers.ProviderError, match="specialty server"):
        providers.nordvpn_server_name("us-ca12")
    with pytest.raises(providers.ProviderError, match="specialty server"):
        providers.nordvpn_server_name("ch-onion3.nordvpn.com")
    for bad in ("xyz", "de", "de1398.example.com", "1398"):
        with pytest.raises(providers.ProviderError, match="not a NordVPN server name"):
            providers.nordvpn_server_name(bad)


def test_listing_is_least_loaded_first_and_skips_unusable_servers(nord):
    country = providers.list_servers("nordvpn", "germany")
    # de2/de10 tie on load 5 -> server-number order; keyless de3 is left out
    assert [s["server"] for s in country] == ["de2", "de10", "de1"]
    assert country[0] == {
        "server": "de2",
        "hostname": "de2.nordvpn.com",
        "country": "Germany",
        "city": "Berlin",
        "load": 5,
    }
    city = providers.list_servers("nordvpn", "Germany", "Frankfurt")
    assert [s["server"] for s in city] == ["de10", "de1"]
    # the listing asks for Standard servers only (never Dedicated IP ones),
    # with trimmed fields
    url = nord.urls[-1]
    assert "/servers?" in url
    assert "legacy_standard" in url
    assert "fields[servers.hostname]" in url
    with pytest.raises(providers.ProviderError, match="city"):
        providers.list_servers("nordvpn", "Germany", "Atlantis")


def test_lookup_finds_servers_in_cities_recommendations_omit(nord):
    # Berlin is missing from Germany's country-level recommendations
    assert providers.lookup_server("nordvpn", "de2")["city"] == "Berlin"


def test_offline_servers_are_not_listed(nord):
    nord.de[1] = {**nord.de[1], "status": "maintenance"}
    assert "de10" not in [
        s["server"] for s in providers.list_servers("nordvpn", "Germany")
    ]


def test_lookup_maps_prefixes_and_explains_unavailable_servers(nord):
    assert providers.lookup_server("nordvpn", "UK7")["country"] == "United Kingdom"
    with pytest.raises(providers.ProviderError, match='prefix "zz"'):
        providers.lookup_server("nordvpn", "zz1")
    with pytest.raises(providers.ProviderServerUnavailableError, match="Dedicated IP"):
        providers.lookup_server("nordvpn", "de99")
    with pytest.raises(
        providers.ProviderServerUnavailableError, match="not an available"
    ):
        providers.lookup_server("nordvpn", "de404")
    with pytest.raises(providers.ProviderError, match="NordVPN-only feature"):
        providers.list_servers("protonvpn", "Germany")


def test_recommendation_can_avoid_a_server(nord):
    assert providers.nordvpn_resolve("Germany", "")["hostname"] == "de1.nordvpn.com"
    picked = providers.nordvpn_resolve("Germany", "", avoid="de1.nordvpn.com")
    assert picked["hostname"] == "de10.nordvpn.com"


# ---- service: add / setserver ----------------------------------------------


def test_add_pinned_channel_takes_location_from_the_server(nord):
    result = service.channel_add("nordvpn", None, None, server="DE10")
    channel = result["channel"]
    assert channel["name"] == "wg_de_frankfurt_1"
    assert (channel["country"], channel["city"]) == ("Germany", "Frankfurt")
    assert channel["server"] == "de10.nordvpn.com"
    assert channel["pinned"] is True
    stored = _channel("wg_de_frankfurt_1")
    assert stored.server == "de10.nordvpn.com"
    assert stored.wg["peer"]["endpoint_host"] == "10.0.1.10"
    assert stored.wg["peer"]["hostname"] == "de10.nordvpn.com"
    assert stored.wg["private_key"] == PRIV


@pytest.mark.parametrize(
    "extra",
    [{"country": "Germany"}, {"city": "Frankfurt"}, {"config": "x.conf"}],
)
def test_pinned_add_refuses_location_flags(nord, extra):
    with pytest.raises(service.ServiceError, match="cannot be combined with"):
        service.channel_add(
            "nordvpn",
            extra.get("country"),
            extra.get("city"),
            extra.get("config"),
            server="de10",
        )
    assert Store.load().provider_channels("nordvpn") == []


def test_pinned_add_fails_explicitly_for_unavailable_servers(nord):
    with pytest.raises(service.ServiceError, match="Dedicated IP"):
        service.channel_add("nordvpn", None, None, server="de99")
    with pytest.raises(service.ServiceError, match="not an available"):
        service.channel_add("nordvpn", None, None, server="de404")
    assert Store.load().provider_channels("nordvpn") == []


def test_two_channels_cannot_share_a_server(nord):
    service.channel_add("nordvpn", "Germany", None)  # recommended: de1
    with pytest.raises(service.ServiceError, match="already in use by nordvpn/wg_de_1"):
        service.channel_add("nordvpn", None, None, server="de1")
    service.channel_add("nordvpn", None, None, server="de10")
    with pytest.raises(service.ServiceError, match="already in use"):
        service.channel_set_server("wg_de_1", "de10")


def test_setserver_pins_and_auto_unpins_without_moving(nord):
    service.channel_add("nordvpn", "Germany", None)
    auto = service.channel_list()["channels"][0]
    assert (auto["server"], auto["pinned"]) == ("de1.nordvpn.com", False)

    pinned = service.channel_set_server("wg_de_1", "de2")
    assert pinned["changed"] is True
    ch = pinned["channel"]
    assert ch["name"] == "wg_de_1"  # the id is the handle; it never changes
    # a country-wide (any city) channel stays country-wide when pinned
    assert (ch["city"], ch["server"], ch["pinned"]) == (
        "(Any City)",
        "de2.nordvpn.com",
        True,
    )
    assert _channel("wg_de_1").wg["peer"]["endpoint_host"] == "10.0.1.2"

    again = service.channel_set_server("nordvpn/wg_de_1", "de2.nordvpn.com")
    assert again["changed"] is False

    wg_before = _channel("wg_de_1").wg
    unpinned = service.channel_set_server("wg_de_1", "AUTO")
    assert unpinned["changed"] is True
    assert unpinned["channel"]["pinned"] is False
    assert unpinned["channel"]["server"] == "de2.nordvpn.com"  # stays put
    after = _channel("wg_de_1")
    assert after.server == ""
    assert after.wg == wg_before
    assert after.city == ""
    assert service.channel_set_server("wg_de_1", "auto")["changed"] is False


def test_setserver_rejects_globs_and_a_missing_country(nord):
    with pytest.raises(service.ServiceError, match="glob"):
        service.channel_set_server("wg_*", "de1")
    with pytest.raises(service.ServiceError, match="--country"):
        service.servers_list("nordvpn", None)


NORD_ONLY = "choosing a server is a NordVPN-only feature"


@pytest.fixture
def config_channels(nord):
    """A Proton VPN and a Customized channel, imported from a .conf."""
    key = base64.b64encode(b"k" * 32).decode()
    conf = (
        f"[Interface]\nPrivateKey = {key}\nAddress = 10.0.0.2/32\n"
        f"[Peer]\nPublicKey = {key}\nEndpoint = 1.2.3.4:51820\n"
    )
    store = Store.load()
    store.add_provider("protonvpn")
    store.add_provider("customized")
    service.channel_add_conf_text("protonvpn", "wg-US-CA-1.conf", conf, "")
    service.channel_add_conf_text("customized", "home.conf", conf, "", "home")


@pytest.mark.parametrize(
    ("ref", "server", "brand"),
    [
        ("wg_us_ca_1", "de10", "Proton VPN"),
        ("protonvpn/wg_us_ca_1", "auto", "Proton VPN"),
        ("customized/home", "de10", "Customized"),
    ],
)
def test_setserver_on_a_non_nordvpn_channel_says_it_is_nordvpn_only(
    config_channels, ref, server, brand
):
    before = Store.load().data
    with pytest.raises(service.ServiceError) as err:
        service.channel_set_server(ref, server)
    message = str(err.value)
    assert message.startswith(NORD_ONLY)
    assert f"is a {brand} channel, which uses the server" in message
    assert "imported WireGuard .conf" in message
    assert Store.load().data == before  # nothing touched


def test_every_server_entry_point_refuses_non_nordvpn_providers(config_channels):
    for call in (
        lambda: service.servers_list("protonvpn", "Germany"),
        lambda: service.channel_add("protonvpn", None, None, server="de10"),
        # the provider is the real problem, not the flag combination
        lambda: service.channel_add("protonvpn", None, None, "x.conf", server="de1"),
    ):
        with pytest.raises(service.ServiceError, match=NORD_ONLY):
            call()
    with pytest.raises(bundle.BundleError, match=NORD_ONLY):
        bundle.validate(
            bundle.loads(
                yaml.safe_dump(
                    {
                        "kind": "anyhop-bundle",
                        "bundle_version": 1,
                        "providers": {
                            "protonvpn": {"channels": {"a_1": {"server": "de10"}}}
                        },
                    }
                )
            )
        )


def test_token_replacement_keeps_pinned_channels_on_their_server(nord, monkeypatch):
    monkeypatch.setattr(service, "validate_provider_credentials", lambda p, c: None)
    service.channel_add("nordvpn", None, None, server="de2")
    service.channel_add("nordvpn", "Germany", None)
    result = service.provider_update_token("nordvpn", {"token": "new"})
    assert sorted(result["channels"]["resolved"]) == ["wg_de_1", "wg_de_berlin_1"]
    assert _channel("wg_de_berlin_1").current_server == "de2.nordvpn.com"
    assert _channel("wg_de_berlin_1").server == "de2.nordvpn.com"


# ---- auto-reconnect --------------------------------------------------------

FAIL = {"ok": False, "at": 1, "latency_ms": None, "ip": None, "error": "timeout"}


class _Runner:
    def restart(self):
        raise AssertionError("token channels never restart sing-box")


def _fail_until_attempt(cid: str) -> dict:
    for i in range(reconnect.FAIL_THRESHOLD):
        store = Store.load()
        store.set_probe("nordvpn", cid, dict(FAIL))
        reconnect.run_pass(store, cast(singbox.Runner, _Runner()), now=1000 + i)
    return _channel(cid).reconnect


def test_reconnect_of_a_retired_pinned_server_fails_explicitly(nord):
    service.channel_add("nordvpn", None, None, server="de10")
    nord.de = [s for s in nord.de if s["hostname"] != "de10.nordvpn.com"]
    providers.forget_nord_servers()
    wg_before = _channel("wg_de_frankfurt_1").wg

    rc = _fail_until_attempt("wg_de_frankfurt_1")

    assert rc["failed"] is True  # at once: no retries onto another server
    assert "pinned server de10.nordvpn.com is no longer available" in rc["error"]
    assert "setserver nordvpn/wg_de_frankfurt_1 auto" in rc["error"]
    assert _channel("wg_de_frankfurt_1").wg == wg_before


def test_reconnect_of_a_pinned_server_rereads_that_same_server(nord):
    service.channel_add("nordvpn", None, None, server="de10")
    nord.de[1] = _srv("de10", "Frankfurt", 5, ip="10.9.9.9")  # re-addressed
    providers.forget_nord_servers()

    rc = _fail_until_attempt("wg_de_frankfurt_1")

    assert not rc.get("failed")
    peer = _channel("wg_de_frankfurt_1").wg["peer"]
    assert (peer["hostname"], peer["endpoint_host"]) == ("de10.nordvpn.com", "10.9.9.9")


def test_reconnect_api_outage_stays_retryable_for_pinned_channels(nord):
    service.channel_add("nordvpn", None, None, server="de10")
    providers.forget_nord_servers()
    nord.down = True

    rc = _fail_until_attempt("wg_de_frankfurt_1")

    assert not rc.get("failed")
    assert "could not reach" in rc["error"]


def test_reconnect_of_an_auto_channel_moves_off_the_dead_server(nord):
    service.channel_add("nordvpn", "Germany", None)
    assert _channel("wg_de_1").current_server == "de1.nordvpn.com"

    _fail_until_attempt("wg_de_1")

    assert _channel("wg_de_1").current_server == "de10.nordvpn.com"


# ---- bundles ---------------------------------------------------------------


def _bundle(channels: dict) -> str:
    return yaml.safe_dump(
        {
            "kind": "anyhop-bundle",
            "bundle_version": 1,
            "providers": {
                "nordvpn": {"credential": {"token": "tok"}, "channels": channels}
            },
        }
    )


def test_export_writes_a_pinned_channel_as_server_only(nord):
    service.channel_add("nordvpn", None, None, server="de10")
    service.channel_add("nordvpn", "Germany", None)
    chans = bundle.export_bundle()["providers"]["nordvpn"]["channels"]
    pinned = chans["wg_de_frankfurt_1"]
    assert pinned["server"] == "de10.nordvpn.com"
    assert "country" not in pinned
    assert "city" not in pinned
    assert pinned["wg"]["peer"]["hostname"] == "de10.nordvpn.com"
    assert chans["wg_de_1"]["country"] == "Germany"
    assert "server" not in chans["wg_de_1"]


def test_bundle_validation_refuses_server_with_location_and_shared_servers(nord):
    with pytest.raises(bundle.BundleError) as err:
        bundle.validate(
            bundle.loads(
                _bundle(
                    {
                        "a_1": {"server": "de10", "country": "Germany"},
                        "b_1": {"server": "de10.nordvpn.com"},
                        "c_1": {"server": "us-ca12"},
                    }
                )
            )
        )
    reasons = dict(err.value.entries)
    path = "providers.nordvpn.channels"
    assert "cannot be combined with server" in reasons[f"{path}.a_1.country"]
    assert "also pinned by channel a_1" in reasons[f"{path}.b_1.server"]
    assert "specialty server" in reasons[f"{path}.c_1.server"]


def test_bundle_import_resolves_the_pinned_server(nord):
    summary = bundle.apply_import(_bundle({"fra_1": {"server": "DE10"}}))
    assert "nordvpn/fra_1" in summary["wg_resolved"]
    ch = _channel("fra_1")
    assert ch.server == "de10.nordvpn.com"
    assert (ch.country, ch.city) == ("Germany", "Frankfurt")
    assert ch.wg["peer"]["endpoint_host"] == "10.0.1.10"


def test_bundle_import_refuses_an_unavailable_pinned_server(nord):
    with pytest.raises(bundle.BundleError, match="not an available"):
        bundle.apply_import(_bundle({"fra_1": {"server": "de404"}}))
    assert Store.load().provider_channels("nordvpn") == []


def test_bundle_roundtrip_keeps_the_pin_offline(nord):
    service.channel_add("nordvpn", None, None, server="de10")
    text = bundle.dumps(bundle.export_bundle())
    Store.load().remove_channels([("nordvpn", "wg_de_frankfurt_1")])
    providers.forget_nord_servers()
    nord.down = True  # the snapshot stands in; the label comes from the name

    summary = bundle.apply_import(text)

    assert "nordvpn/wg_de_frankfurt_1" in summary["wg_fallback"]
    ch = _channel("wg_de_frankfurt_1")
    assert ch.server == "de10.nordvpn.com"
    assert ch.country == "Germany"
    assert ch.wg["peer"]["hostname"] == "de10.nordvpn.com"


# ---- CLI -------------------------------------------------------------------


def _cli(args, capsys) -> str:
    cli.main(args)
    return capsys.readouterr().out.rstrip("\n")


def test_cli_lists_servers_pins_and_shows_the_server(nord, capsys):
    listing = _cli(["servers", "nordvpn", "--country", "Germany"], capsys)
    lines = listing.splitlines()
    assert lines[0] == "NordVPN servers in Germany (3), least loaded first:"
    assert lines[1].split() == ["SERVER", "CITY", "LOAD"]
    assert lines[3].split() == ["de2", "Berlin", "5%"]
    assert lines[-1] == "Pin one:  anyhop channels add nordvpn --server de2"

    as_json = json.loads(
        _cli(["servers", "nordvpn", "--country", "Germany", "--json"], capsys)
    )
    assert [s["server"] for s in as_json["servers"]] == ["de2", "de10", "de1"]

    added = _cli(["channels", "add", "nordvpn", "--server", "de10"], capsys)
    assert "pinned to de10.nordvpn.com" in added

    table = _cli(["channels", "ls"], capsys).splitlines()
    assert "SERVER" in table[0].split()
    assert "de10 (pinned)" in table[2]

    unpinned = _cli(["channels", "setserver", "wg_de_frankfurt_1", "auto"], capsys)
    assert "now follows the recommended server" in unpinned


def test_cli_refuses_server_with_country(nord, capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["channels", "add", "nordvpn", "--server", "de10", "--country", "X"])
    assert "server cannot be combined with country" in str(exc.value.code)


# ---- REST ------------------------------------------------------------------


@pytest.fixture
def live(nord):
    httpd = server.build_server()
    thread = start_test_server(httpd)
    api = server.control_api()
    try:
        yield f"http://{api['address']}", api["secret"]
    finally:
        stop_test_server(httpd, thread)


def _call(base, secret, path, *, method="GET", data=None, headers=None):
    import urllib.error
    import urllib.request

    body = json.dumps(data).encode() if data is not None else None
    hdrs = {"Authorization": f"Bearer {secret}", "Origin": base, **(headers or {})}
    if body is not None:
        hdrs["Content-Type"] = "application/json"
    req = urllib.request.Request(base + path, method=method, data=body, headers=hdrs)
    try:
        with urllib.request.urlopen(req) as resp:  # noqa: S310 (loopback test)
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_rest_lists_pins_and_unpins(live):
    base, secret = live
    st, listing = _call(
        base, secret, "/api/v1/servers?provider=nordvpn&country=Germany&city=Frankfurt"
    )
    assert st == 200
    assert [s["server"] for s in listing["servers"]] == ["de10", "de1"]

    st, catalog = _call(base, secret, "/api/v1/providers/catalog")
    flags = {p["provider"]: p["servers"] for p in catalog["providers"]}
    assert flags["nordvpn"] is True
    assert flags["protonvpn"] is False

    st, added = _call(
        base,
        secret,
        "/api/v1/channels",
        method="POST",
        data={"provider": "nordvpn", "server": "de10"},
    )
    assert st == 200
    assert added["channel"]["pinned"] is True
    assert "wg" not in added["channel"]

    st, err = _call(
        base,
        secret,
        "/api/v1/channels",
        method="POST",
        data={"provider": "nordvpn", "server": "de1", "country": "Germany"},
    )
    assert st == 400
    assert "cannot be combined" in err["error"]

    path = "/api/v1/channels/nordvpn/wg_de_frankfurt_1/server"
    st, stale = _call(
        base,
        secret,
        path,
        method="POST",
        data={"server": "de1"},
        headers={"If-Match": '"' + "0" * 64 + '"'},
    )
    assert st == 409
    st, moved = _call(base, secret, path, method="POST", data={"server": "de1"})
    assert st == 200
    assert moved["channel"]["server"] == "de1.nordvpn.com"
    st, auto = _call(base, secret, path, method="POST", data={"server": "auto"})
    assert st == 200
    assert auto["channel"]["pinned"] is False

    st, err = _call(base, secret, "/api/v1/servers?provider=protonvpn&country=X")
    assert st == 400
    assert "NordVPN-only feature" in err["error"]


# ---- "any city": a pin that keeps a country-wide scope ---------------------


def test_any_city_pin_records_the_country_only_and_stays_country_wide(nord):
    channel = service.channel_add("nordvpn", None, None, server="de10", any_city=True)[
        "channel"
    ]
    assert channel["name"] == "wg_de_1"  # not wg_de_frankfurt_1
    assert (channel["country"], channel["city"]) == ("Germany", "(Any City)")
    assert channel["server"] == "de10.nordvpn.com"
    assert channel["pinned"] is True

    # re-pinning to a server in another city keeps the country-wide scope
    moved = service.channel_set_server("wg_de_1", "de2")["channel"]
    assert (moved["city"], moved["server"]) == ("(Any City)", "de2.nordvpn.com")
    assert _channel("wg_de_1").city == ""
    # ...and so does unpinning: the recommendation is for the whole country
    service.channel_set_server("wg_de_1", "auto")
    assert (_channel("wg_de_1").city, _channel("wg_de_1").server) == ("", "")


def test_a_city_scoped_pin_still_follows_its_server(nord):
    service.channel_add("nordvpn", None, None, server="de10")
    assert service.channel_set_server("wg_de_frankfurt_1", "de2")["channel"][
        "city"
    ] == ("Berlin")


def test_any_city_needs_a_pinned_server(nord):
    with pytest.raises(service.ServiceError, match="only applies to a pinned server"):
        service.channel_add("nordvpn", "Germany", None, any_city=True)


def test_any_city_pin_round_trips_through_a_bundle(nord):
    service.channel_add("nordvpn", None, None, server="de10", any_city=True)
    exported = bundle.export_bundle()
    spec = exported["providers"]["nordvpn"]["channels"]["wg_de_1"]
    assert spec["server"] == "de10.nordvpn.com"
    assert spec["any_city"] is True
    Store.load().remove_channels([("nordvpn", "wg_de_1")])

    bundle.apply_import(bundle.dumps(exported))

    ch = _channel("wg_de_1")
    assert (ch.server, ch.country, ch.city) == ("de10.nordvpn.com", "Germany", "")


def test_bundle_any_city_without_server_is_rejected(nord):
    with pytest.raises(bundle.BundleError, match="only applies to a channel pinned"):
        bundle.validate(
            bundle.loads(_bundle({"a_1": {"country": "Germany", "any_city": True}}))
        )


def test_cli_and_rest_accept_any_city(live, capsys):
    out = _cli(["channels", "add", "nordvpn", "--server", "de10", "--any-city"], capsys)
    assert "Added channel wg_de_1" in out
    assert "pinned to de10.nordvpn.com" in out
    repinned = _cli(["channels", "setserver", "wg_de_1", "de2"], capsys)
    assert repinned.startswith("Pinned nordvpn/wg_de_1 to de2.nordvpn.com (Germany).")

    base, secret = live
    st, added = _call(
        base,
        secret,
        "/api/v1/channels",
        method="POST",
        data={"provider": "nordvpn", "server": "de1", "any_city": True},
    )
    assert st == 200
    assert added["channel"]["name"] == "wg_de_2"
    assert added["channel"]["city"] == "(Any City)"
