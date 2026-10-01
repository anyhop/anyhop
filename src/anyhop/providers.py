"""VPN provider registry.

A provider is described by its **kind**, which decides how channels get their
WireGuard parameters:

* ``token`` — the provider has an API. You add the provider once with a token/login
  (``anyhop providers add <name>``); thereafter ``anyhop channels add <name>
  --country …`` resolves a concrete server from the API. NordVPN is the token
  provider and is wired end-to-end. A token provider may also let the user
  **pin** one concrete server (``anyhop servers`` lists them; ``channels add
  <name> --server <host>`` pins one) instead of taking the recommended pick.
* ``config`` — portal-only providers (e.g. ProtonVPN) that hand out a WireGuard
  ``.conf``. There is no token; you add the provider so channels can be imported
  under it via ``channels add <name> --config <file>``. The ``customized``
  provider is the bring-your-own variant: any WireGuard server (self-hosted or
  a provider anyhop doesn't know), where the user names each channel
  (``--name``) since an arbitrary ``.conf`` file name carries no meaning.

Everything the engine needs for a functional provider — derive the account key,
list locations, resolve a location to a peer — comes straight from the provider's
own API, which is fresher than any bundled server database.
"""

from __future__ import annotations

import base64
import json
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Protocol

from anyhop import credentials, geo
from anyhop.constants import provider_order
from anyhop.credentials import mask

NORD_API = "https://api.nordvpn.com/v1"
WG_PORT = 51820  # NordLynx / standard WireGuard UDP port


class WireGuardResolver(Protocol):
    """``(country, city) -> WireGuard params``; ``server`` resolves one pinned
    server instead of a recommendation, ``avoid`` names a server the pick
    must not be (the one a reconnect is moving away from)."""

    def __call__(
        self, country: str, city: str = "", *, server: str = "", avoid: str = ""
    ) -> dict: ...


class ProviderError(Exception):
    """Raised when a provider rejects credentials or returns nothing usable."""


class ProviderAuthError(ProviderError):
    """A credential problem retrying can never fix (missing/rejected token).

    Kept as a distinct *type* so auto-reconnect can give up immediately on
    auth failures without pattern-matching words in error messages — which
    would misclassify transient errors that happen to contain the same words.
    """


class ProviderUnreachableError(ProviderError):
    """The provider API could not be reached at all — DNS failure, refused
    connection, timeout. Environmental and retryable; never a credential or
    payload problem."""


class ProviderServerUnavailableError(ProviderError):
    """The provider API answered, and a named server is not among the usable
    ones — retired, offline, or a kind the account cannot use. Retrying cannot
    fix it: a pinned channel needs a human to pick another server."""


class ProviderAPIError(ProviderError):
    """The provider API answered, but unusably — an HTTP error status, an
    oversized/undecodable body, or a payload whose shape doesn't match what
    the API is documented to return. ``status`` carries the HTTP code when
    one was involved (callers map auth codes to :class:`ProviderAuthError`
    where they know the context)."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


# Provider payloads anyhop reads are small (the NordVPN country list is a few
# hundred KB); anything past this bound is not a payload we should index.
MAX_RESPONSE_BYTES = 8 * 1024 * 1024


def _get_json(url: str, headers: dict | None = None, timeout: int = 30):
    """Fetch and decode one JSON payload, normalizing every transport failure.

    The single wrapping point for the provider boundary: network-level
    failures raise :class:`ProviderUnreachableError`; HTTP error statuses,
    oversized bodies, and undecodable JSON raise :class:`ProviderAPIError`
    (with ``status`` set for HTTP errors). Callers therefore only ever see
    typed ``ProviderError``\\ s — never a raw ``URLError``/``ValueError`` that
    would put reconnect backoff, bundle fallback, or CLI reporting on the
    wrong path.
    """
    req = urllib.request.Request(url, headers=headers or {"User-Agent": "anyhop/1"})  # noqa: S310
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310
            body = r.read(MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as e:
        raise ProviderAPIError(
            f"provider API failed (HTTP {e.code}) for {url}", status=e.code
        ) from e
    except OSError as e:
        reason = getattr(e, "reason", None) or e
        raise ProviderUnreachableError(
            f"could not reach the provider API: {reason}"
        ) from e
    if len(body) > MAX_RESPONSE_BYTES:
        raise ProviderAPIError(
            f"provider API response exceeds {MAX_RESPONSE_BYTES} bytes"
        )
    try:
        return json.loads(body)
    except ValueError as e:
        raise ProviderAPIError(f"provider API returned invalid JSON: {e}") from e


# ---- NordVPN ---------------------------------------------------------------

# Client interface address for NordLynx; the same for every server/account.
NORDVPN_WG_ADDRESS = ["10.5.0.2/32"]  # noqa: S1313

# In-process country/city cache as (fetched_at_monotonic, data). Time-bounded
# so a long-lived process (the daemon serving the API and reconnecting channels
# weeks later) doesn't pin the list it fetched at startup forever; the
# on-disk cache in locations.py has its own, longer expiry.
NORD_CACHE_TTL = 3600.0
_nord_countries_cache: tuple[float, list[dict]] | None = None


def nordvpn_derive_key(creds: dict) -> str:
    """Exchange a NordVPN access token for the account's NordLynx private key."""
    token = (creds.get("token") or "").strip()
    if not token:
        raise ProviderAuthError("nordvpn access token is missing.")
    auth = base64.b64encode(f"token:{token}".encode()).decode()
    try:
        data = _get_json(
            f"{NORD_API}/users/services/credentials",
            headers={"Authorization": f"Basic {auth}", "User-Agent": "anyhop/1"},
        )
    except ProviderAPIError as e:
        if e.status in (401, 403):
            raise ProviderAuthError(
                f"nordvpn token rejected by API (HTTP {e.status}). "
                "Generate a fresh token at "
                "https://my.nordaccount.com/dashboard/nordvpn/access-tokens."
            ) from e
        # e.g. 5xx / bad payload: the API is unhappy, not the credential — retryable
        raise
    key = data.get("nordlynx_private_key") if isinstance(data, dict) else None
    if not isinstance(key, str) or not key:
        raise ProviderAPIError("nordvpn API did not return nordlynx_private_key.")
    return key


def _check_nord_countries(data) -> list[dict]:
    """The country list, validated to the shape the lookups below index —
    ``[{id, name, cities: [{id, name}]}]`` — before anything touches it."""
    if not isinstance(data, list):
        raise ProviderAPIError("nordvpn country list is not a list.")
    for c in data:
        if not (
            isinstance(c, dict)
            and isinstance(c.get("name"), str)
            and c["name"]
            and isinstance(c.get("id"), int)
            and isinstance(c.get("cities", []), list)
            and all(
                isinstance(ci, dict)
                and isinstance(ci.get("name"), str)
                and ci["name"]
                and isinstance(ci.get("id"), int)
                for ci in c.get("cities", [])
            )
        ):
            raise ProviderAPIError(
                "nordvpn country list has an unexpected shape "
                f"(offending entry: {str(c)[:80]!r})."
            )
    return data


def _nord_countries() -> list[dict]:
    global _nord_countries_cache
    cached = _nord_countries_cache
    if cached is not None and time.monotonic() - cached[0] < NORD_CACHE_TTL:
        return cached[1]
    try:
        countries = _check_nord_countries(_get_json(f"{NORD_API}/servers/countries"))
    except ProviderError as e:
        if cached is not None:
            return cached[1]  # refresh failed: stale beats failing (reconnect path)
        # same typed class, with the what-was-being-fetched context added
        raise type(e)(f"could not fetch nordvpn country list: {e}") from e
    _nord_countries_cache = (time.monotonic(), countries)
    return countries


def nordvpn_locations() -> dict[str, list[str]]:
    """Country -> sorted cities, exactly as the NordVPN API reports them today."""
    out: dict[str, list[str]] = {}
    for c in _nord_countries():
        out[c["name"]] = sorted(ci["name"] for ci in c.get("cities", []))
    return out


def _nord_ids(country: str, city: str) -> tuple[int, int | None]:
    for c in _nord_countries():
        if c["name"].lower() == country.lower():
            if city:
                for ci in c.get("cities", []):
                    if ci["name"].lower() == city.lower():
                        return c["id"], ci["id"]
                raise ProviderError(
                    f"city {city!r} is not a nordvpn location in {country}."
                )
            return c["id"], None
    raise ProviderError(f"country {country!r} is not a nordvpn location.")


def _nord_pubkey(server: dict) -> str:
    for tech in server.get("technologies") or []:
        if isinstance(tech, dict) and tech.get("identifier") == "wireguard_udp":
            for m in tech.get("metadata") or []:
                if isinstance(m, dict) and m.get("name") == "public_key":
                    value = m.get("value")
                    if isinstance(value, str) and value:
                        return value
    raise ProviderAPIError(
        f"nordvpn server {server.get('hostname')} has no WireGuard public key."
    )


def _nord_server_host(server: dict) -> str | None:
    """The server's connect address (first IP, falling back to ``station``),
    tolerating shape drift in the nested ``ips`` structure."""
    ips = server.get("ips")
    if isinstance(ips, list) and ips and isinstance(ips[0], dict):
        ip = ips[0].get("ip")
        if isinstance(ip, dict) and isinstance(ip.get("ip"), str) and ip["ip"]:
            return ip["ip"]
    station = server.get("station")
    return station if isinstance(station, str) and station else None


def _nord_wg_filter() -> str:
    return "filters[servers_technologies][identifier]=wireguard_udp"


def _nord_location_filter(country_id: int, city_id: int | None) -> str:
    return (
        f"filters[country_city_id]={city_id}"
        if city_id
        else f"filters[country_id]={country_id}"
    )


def _check_nord_servers(data, what: str) -> list[dict]:
    if not isinstance(data, list) or not all(isinstance(s, dict) for s in data):
        raise ProviderAPIError(f"nordvpn {what} have an unexpected shape.")
    return data


def nordvpn_resolve(country: str, city: str, avoid: str = "") -> dict:
    """Pick the recommended WireGuard server for a location -> peer parameters.

    ``avoid`` (a hostname) skips that server — a reconnect moving off a dead
    server must not be handed the same one back by the API's shuffle.
    """
    country_id, city_id = _nord_ids(country, city)
    url = (
        f"{NORD_API}/servers/recommendations?"
        f"{_nord_location_filter(country_id, city_id)}&{_nord_wg_filter()}"
        f"&limit={5 if avoid else 1}"
    )
    try:
        servers = _check_nord_servers(_get_json(url), "server recommendations")
    except ProviderError as e:
        # same typed class, with the what-was-being-resolved context added
        raise type(e)(f"could not resolve a nordvpn server for {country}: {e}") from e
    candidates = [s for s in servers if s.get("hostname") != avoid] or servers
    if not candidates:
        where = f"{city}, {country}" if city else country
        raise ProviderError(f"nordvpn has no WireGuard server available in {where}.")
    s = candidates[0]
    host = _nord_server_host(s)
    if not host:
        raise ProviderAPIError(f"nordvpn server {s.get('hostname')} has no usable IP.")
    return {
        "host": host,
        "port": WG_PORT,
        "public_key": _nord_pubkey(s),
        "hostname": s.get("hostname") or "",
    }


def forget_nord_countries() -> None:
    global _nord_countries_cache
    _nord_countries_cache = None


# ---- NordVPN: listing and pinning concrete servers --------------------------
#
# The server list is ``/servers`` narrowed to the Standard group: unfiltered it
# also returns Dedicated IP servers (which need that add-on — and report the
# lowest loads, so a naive "least loaded" pick lands on them). It is not the
# recommendations endpoint, which at country level silently drops whole cities
# (Japan answers with Tokyo only, never Osaka). ``fields[...]`` trims each
# record to what is read here, which keeps even the whole-US list (~1600
# servers) near 1.5 MB.

NORD_SERVER_LIMIT = 10000
NORD_SERVER_FIELDS = "&".join(
    f"fields[servers.{name}]"
    for name in (
        "hostname",
        "load",
        "status",
        "station",
        "ips.ip.ip",
        "technologies.identifier",
        "technologies.metadata",
        "locations.country.name",
        "locations.country.city.name",
    )
)
# Loads move by the minute; this only spares a "list, then pin" round trip
# (and a bundle naming several servers in one country) a second fetch.
NORD_SERVERS_TTL = 60.0
_nord_servers_cache: dict[tuple[int, int | None], tuple[float, list[dict]]] = {}

# A standard server is ``<prefix><n>.nordvpn.com``, the prefix being the
# country's ISO 3166-1 alpha-2 code — except the UK, whose servers say ``uk``.
# Specialty servers (Double VPN ``us-ca12``, Onion ``ch-onion3``, SOCKS
# ``socks-nl5``) carry a dashed prefix and are never in the usable list.
_NORD_HOST_RE = re.compile(r"([a-z]{2})(\d+)(?:\.nordvpn\.com)?")
_NORD_SPECIALTY_RE = re.compile(r"[a-z]+-[a-z]+\d+(?:\.nordvpn\.com)?")
_NORD_PREFIX_TO_CODE = {"uk": "gb"}


def nordvpn_server_name(text: str) -> str:
    """Canonical hostname (``de1398.nordvpn.com``) for a typed server name.

    Accepts ``de1398`` or the full hostname, any case. Raises ProviderError
    for anything that is not a standard NordVPN server name.
    """
    name = (text or "").strip().lower()
    match = _NORD_HOST_RE.fullmatch(name)
    if match:
        return f"{match[1]}{match[2]}.nordvpn.com"
    if _NORD_SPECIALTY_RE.fullmatch(name):
        short = name.removesuffix(".nordvpn.com")
        raise ProviderError(
            f"{short} is a NordVPN specialty server (Double VPN, Onion or SOCKS); "
            "only standard servers can be pinned."
        )
    raise ProviderError(
        f"{text!r} is not a NordVPN server name (expected e.g. de1398 or "
        "de1398.nordvpn.com)."
    )


def _nord_server_country(hostname: str) -> dict:
    """The country record a canonical hostname's prefix belongs to."""
    prefix = hostname.split(".", 1)[0].rstrip("0123456789")
    code = _NORD_PREFIX_TO_CODE.get(prefix, prefix)
    for c in _nord_countries():
        if str(c.get("code") or "").lower() == code:
            return c
    raise ProviderError(f'no NordVPN country uses the server prefix "{prefix}".')


def _nord_server_entry(server: dict) -> dict | None:
    """One usable server record, or None when it lacks what a channel needs
    (a connect address or a WireGuard key) — such a server cannot be pinned,
    so it is left out of the list rather than failing the whole listing."""
    hostname = server.get("hostname")
    host = _nord_server_host(server)
    if not isinstance(hostname, str) or not hostname or not host:
        return None
    if server.get("status", "online") != "online":
        return None
    try:
        public_key = _nord_pubkey(server)
    except ProviderAPIError:
        return None
    country = city = ""
    locs = server.get("locations")
    if isinstance(locs, list) and locs and isinstance(locs[0], dict):
        c = locs[0].get("country")
        if isinstance(c, dict):
            if isinstance(c.get("name"), str):
                country = c["name"]
            ci = c.get("city")
            if isinstance(ci, dict) and isinstance(ci.get("name"), str):
                city = ci["name"]
    load = server.get("load")
    return {
        "hostname": hostname.lower(),
        "country": country,
        "city": city,
        "load": load if isinstance(load, int) and not isinstance(load, bool) else None,
        "host": host,
        "public_key": public_key,
    }


def _server_sort_key(entry: dict) -> tuple:
    """Least loaded first; ties in server-number order (de2 before de10)."""
    name = entry["hostname"].split(".", 1)[0]
    digits = name.lstrip("abcdefghijklmnopqrstuvwxyz-")
    load = entry["load"]
    return (
        load if load is not None else 101,
        name[: len(name) - len(digits)],
        int(digits) if digits.isdigit() else 0,
    )


def _nord_servers(country_id: int, city_id: int | None) -> list[dict]:
    key = (country_id, city_id)
    cached = _nord_servers_cache.get(key)
    if cached is not None and time.monotonic() - cached[0] < NORD_SERVERS_TTL:
        return cached[1]
    url = (
        f"{NORD_API}/servers?"
        f"{_nord_location_filter(country_id, city_id)}&{_nord_wg_filter()}"
        "&filters[servers_groups][identifier]=legacy_standard"
        f"&limit={NORD_SERVER_LIMIT}&{NORD_SERVER_FIELDS}"
    )
    raw = _check_nord_servers(_get_json(url), "server list")
    entries = [e for e in map(_nord_server_entry, raw) if e is not None]
    entries.sort(key=_server_sort_key)
    _nord_servers_cache[key] = (time.monotonic(), entries)
    return entries


def _public_server(entry: dict) -> dict:
    return {
        "server": entry["hostname"].split(".", 1)[0],
        "hostname": entry["hostname"],
        "country": entry["country"],
        "city": entry["city"],
        "load": entry["load"],
    }


def nordvpn_servers(country: str, city: str = "") -> list[dict]:
    """Every server a channel can pin in a location, least loaded first."""
    country_id, city_id = _nord_ids(country, city)
    try:
        entries = _nord_servers(country_id, city_id)
    except ProviderError as e:
        raise type(e)(f"could not list nordvpn servers for {country}: {e}") from e
    return [_public_server(e) for e in entries]


def _nord_dedicated_ip(hostname: str, country_id: int) -> bool:
    """Whether ``hostname`` is one of the country's Dedicated IP servers.

    Only refines the "not available" message, so any failure reads False.
    """
    url = (
        f"{NORD_API}/servers?filters[country_id]={country_id}&{_nord_wg_filter()}"
        "&limit=0&fields[servers.hostname]&fields[servers.groups.identifier]"
    )
    try:
        servers = _check_nord_servers(_get_json(url), "server list")
    except ProviderError:
        return False
    for s in servers:
        if s.get("hostname") == hostname:
            groups = s.get("groups")
            return isinstance(groups, list) and any(
                isinstance(g, dict) and g.get("identifier") == "legacy_dedicated_ip"
                for g in groups
            )
    return False


def _nord_lookup(server: str) -> dict:
    hostname = nordvpn_server_name(server)
    country = _nord_server_country(hostname)
    try:
        entries = _nord_servers(country["id"], None)
    except ProviderError as e:
        raise type(e)(f"could not look up nordvpn server {hostname}: {e}") from e
    for entry in entries:
        if entry["hostname"] == hostname:
            return entry
    short = hostname.split(".", 1)[0]
    if _nord_dedicated_ip(hostname, country["id"]):
        raise ProviderServerUnavailableError(
            f"{short} is a NordVPN Dedicated IP server, which needs that add-on; "
            "only standard servers can be pinned."
        )
    raise ProviderServerUnavailableError(
        f"{short} is not an available NordVPN WireGuard server in "
        f"{country['name']} (retired, offline, or never existed) — see: "
        f'anyhop servers nordvpn --country "{country["name"]}"'
    )


def nordvpn_lookup_server(server: str) -> dict:
    """Where a pinnable server is: ``{server, hostname, country, city, load}``."""
    return _public_server(_nord_lookup(server))


def nordvpn_resolve_server(server: str) -> dict:
    """Peer parameters for one pinned server (its address and key are re-read
    from the API every time, so a re-addressed server keeps working)."""
    entry = _nord_lookup(server)
    return {
        "host": entry["host"],
        "port": WG_PORT,
        "public_key": entry["public_key"],
        "hostname": entry["hostname"],
    }


def nordvpn_server_country_offline(hostname: str) -> str:
    """Best-effort country name from a canonical hostname with no network
    (ISO name, which may differ from NordVPN's own spelling) — only for
    labelling a pinned channel when the API cannot be asked."""
    prefix = hostname.split(".", 1)[0].rstrip("0123456789")
    return geo.from_filename(_NORD_PREFIX_TO_CODE.get(prefix, prefix))[0]


def forget_nord_servers() -> None:
    _nord_servers_cache.clear()


# ---- authentication --------------------------------------------------------
#
# Credentials are added explicitly with ``anyhop providers add <name>`` and
# stored locally (see credentials.py); anyhop never reads them from the
# environment. Each token provider declares *how* it authenticates so the CLI can
# drive the right prompt.


@dataclass(frozen=True)
class AuthField:
    """One credential a provider's login form asks for."""

    key: str  # storage key in credentials.yaml
    label: str  # prompt / form label shown to the user
    secret: bool = True  # hidden while typing and masked when displayed


# The registry. ``kind`` is "token" (API-backed) or "config" (portal .conf).
# ``functional`` marks *token* providers whose API resolver is wired up —
# config-kind providers are never gated by this flag (they're gated by
# ``kind`` instead, since there's no API to resolve). anyhop ships NordVPN
# (token, ``functional: True``), Proton VPN (config, resolved via
# ``kind == "config"`` regardless of this flag), and Customized — a config
# provider for arbitrary WireGuard servers. ``named_channels`` marks a config
# provider whose channel id is a user-chosen name (``--name``) instead of the
# ``.conf`` file name.
REGISTRY: dict[str, dict] = {
    "nordvpn": {
        "name": "NordVPN",
        "kind": "token",
        "functional": True,
        # NordVPN's WireGuard (NordLynx) does not fully support IPv6 —
        # configs are IPv4-only and v6 inside the tunnel is unsupported, so
        # anyhop explicitly disables v6 for its channels (see Engine._endpoint).
        "ipv6": False,
        "fields": [AuthField("token", "Access token")],
        "help": "https://my.nordaccount.com/dashboard/nordvpn/access-tokens → "
        "generate a new access token.",
        "url": "https://my.nordaccount.com/dashboard/nordvpn/access-tokens",
        "derive_key": nordvpn_derive_key,
        "wg_address": NORDVPN_WG_ADDRESS,
        "resolve": nordvpn_resolve,
        "locations": nordvpn_locations,
        # Drops the in-process country cache so the next "locations" call truly
        # hits the API — what a forced refresh must do even in a long-lived
        # daemon process, not just a fresh CLI run.
        "forget_locations": forget_nord_countries,
        # Server pinning: list a location's servers, canonicalize a typed
        # server name, and resolve one named server instead of a pick.
        "servers": nordvpn_servers,
        "server_name": nordvpn_server_name,
        "lookup_server": nordvpn_lookup_server,
        "resolve_server": nordvpn_resolve_server,
        "server_country_offline": nordvpn_server_country_offline,
    },
    "protonvpn": {
        "name": "Proton VPN",
        "kind": "config",
        "functional": False,
        # Proton VPN supports IPv6 inside the WireGuard tunnel (~80% of
        # servers; the server connection itself stays IPv4). A channel
        # actually carries v6 only when its own config has a global v6
        # interface address — per-server capability, detected locally.
        "ipv6": True,
        "config_help": "Proton VPN has no usable WireGuard API. Generate a WireGuard "
        "config in the Proton portal (Downloads → WireGuard configuration), "
        "then add it as a channel: "
        "anyhop channels add protonvpn --config /path/to/proton.conf",
        "url": "https://account.protonvpn.com/downloads",
    },
    "customized": {
        "name": "Customized",
        "kind": "config",
        "functional": False,
        # Any WireGuard server: v6 is allowed, and — as for Proton — a channel
        # only carries it when its own config has a global v6 interface
        # address (see Engine._endpoint).
        "ipv6": True,
        "named_channels": True,
        "config_help": "Bring any WireGuard server (self-hosted or another "
        "provider): export its WireGuard config, "
        "then add it as a channel under a name of your choice: "
        "anyhop channels add customized --name <name> --config /path/to/wg.conf",
        "url": "",
    },
}

# Functional providers only, in the shape locations.py / provider_wg expect.
PROVIDERS = {k: v for k, v in REGISTRY.items() if v.get("functional")}

# Human-facing names. Keys stay lowercase for config/CLI use.
PROVIDER_NAMES = {k: v["name"] for k, v in REGISTRY.items()}


def known() -> list[str]:
    """Every provider anyhop recognises, sorted (Customized last)."""
    return sorted(REGISTRY, key=provider_order)


def supported() -> list[str]:
    """Functional providers — those you can actually add channels under today."""
    return sorted(PROVIDERS)


def kind(provider: str) -> str:
    return REGISTRY.get(provider, {}).get("kind", "token")


def is_functional(provider: str) -> bool:
    return bool(REGISTRY.get(provider, {}).get("functional"))


def supports_ipv6(provider: str) -> bool:
    """Whether anyhop enables IPv6 for this provider's channels.

    An explicit per-provider decision (user policy, not autodetection):
    a provider that does not fully support v6 inside its tunnel gets v6
    stripped from its channels even if a config smuggles a v6 address in.
    Unknown providers default to False — v6 is opt-in per provider.
    """
    return bool(REGISTRY.get(provider, {}).get("ipv6"))


def names_channels(provider: str) -> bool:
    """Whether this provider's channels are named by the user (``--name``,
    required) rather than after the imported ``.conf`` file."""
    return bool(REGISTRY.get(provider, {}).get("named_channels"))


def display_name(key: str) -> str:
    return PROVIDER_NAMES.get(key, key)


def config_help(provider: str) -> str:
    return REGISTRY.get(provider, {}).get("config_help", "")


def auth_fields(provider: str) -> list[AuthField]:
    return REGISTRY.get(provider, {}).get("fields", [])


def auth_help(provider: str) -> tuple[str, str]:
    """``(instructions, url)`` for obtaining this provider's credential."""
    a = REGISTRY.get(provider, {})
    return a.get("help", ""), a.get("url", "")


def preview(provider: str, creds: dict) -> str:
    """Masked form of a provider's primary secret, for display (never the raw value)."""
    for f in auth_fields(provider):
        if f.secret:
            return mask(str(creds.get(f.key, "")))
    return ""


def supports_servers(provider: str) -> bool:
    """Whether channels under this provider can be pinned to one server."""
    return "servers" in PROVIDERS.get(provider, {})


def servers_unsupported(provider: str, channel: str = "") -> str:
    """Why ``provider`` (or one of its channels) cannot choose a server —
    naming the providers that can, so the message stays true as more do."""
    able = " and ".join(
        display_name(p) for p in sorted(PROVIDERS) if supports_servers(p)
    )
    brand = display_name(provider)
    if kind(provider) == "config":
        one, many = (
            "uses the server in its imported",
            "use the server in their imported",
        )
        source = " WireGuard .conf"
    else:
        one, many = "uses", "use"
        source = " the provider's recommended server"
    detail = (
        f"{channel} is a {brand} channel, which {one}{source}"
        if channel
        else f"{brand} channels {many}{source}"
    )
    return f"choosing a server is a {able}-only feature; {detail}."


def _server_spec(provider: str) -> dict:
    if not supports_servers(provider):
        raise ProviderError(servers_unsupported(provider))
    return PROVIDERS[provider]


def list_servers(provider: str, country: str, city: str = "") -> list[dict]:
    """The servers a channel can be pinned to in a location, least loaded
    first: ``[{server, hostname, country, city, load}]``."""
    return _server_spec(provider)["servers"](country, city)


def server_name(provider: str, text: str) -> str:
    """The canonical hostname for a typed server name (no network)."""
    return _server_spec(provider)["server_name"](text)


def lookup_server(provider: str, server: str) -> dict:
    """Find one pinnable server — ``{server, hostname, country, city, load}``.

    Raises :class:`ProviderServerUnavailableError` when the provider answered
    and the server is not among the usable ones.
    """
    return _server_spec(provider)["lookup_server"](server)


def server_country_offline(provider: str, hostname: str) -> str:
    """A pinned server's country from its name alone ("" when unknown)."""
    return _server_spec(provider)["server_country_offline"](hostname)


def match(name: str) -> str | None:
    """Resolve a user-typed provider (key or brand name, any case) to its key."""
    low = name.strip().lower()
    for p in REGISTRY:
        if low in (p, display_name(p).lower()):
            return p
    return None


def provider_wg(
    provider: str, country: str, city: str = "", *, server: str = ""
) -> dict:
    """Resolve a functional provider + location into WireGuard params for a channel.

    Uses the stored credential to derive the account's private key and the
    provider API to pick a server (or, with ``server``, to resolve that one
    pinned server), producing the ``wgconf.parse`` shape so API-derived and
    config-imported channels are identical at rest.
    """
    if provider not in PROVIDERS:
        raise ProviderError(
            f"{display_name(provider)} cannot resolve locations from an API."
        )
    creds = credentials.get(provider)
    if not creds:
        raise ProviderAuthError(
            f"{display_name(provider)} is not authenticated — run `anyhop providers add {provider}`."
        )
    resolve = provider_resolver(provider, creds)
    if server:
        return resolve(country, city, server=server)
    return resolve(country, city)


def provider_resolver(provider: str, creds: dict) -> WireGuardResolver:
    """A ``(country, city) -> WireGuard params`` resolver with the account key
    derived once up front — a bundle apply resolves many channels under one
    provider, and the key is per-account, not per-channel.

    The resolved server's hostname, when the provider names one, rides along
    as ``peer.hostname``: it is what ``channels ls`` shows and what a
    reconnect steers away from.
    """
    spec = PROVIDERS.get(provider)
    if spec is None:
        raise ProviderError(
            f"{display_name(provider)} cannot resolve locations from an API."
        )
    if not creds:
        raise ProviderAuthError(f"{display_name(provider)} has no credential.")
    private_key = spec["derive_key"](creds)

    def resolve(
        country: str, city: str = "", *, server: str = "", avoid: str = ""
    ) -> dict:
        if server:
            peer = _server_spec(provider)["resolve_server"](server)
        elif avoid:
            peer = spec["resolve"](country, city, avoid=avoid)
        else:
            peer = spec["resolve"](country, city)
        wg_peer = {
            "public_key": peer["public_key"],
            "endpoint_host": peer["host"],
            "endpoint_port": peer["port"],
            "preshared_key": None,
            "allowed_ips": ["0.0.0.0/0", "::/0"],
            "keepalive": 25,
        }
        if peer.get("hostname"):
            wg_peer["hostname"] = peer["hostname"]
        return {
            "private_key": private_key,
            "address": list(spec["wg_address"]),
            "peer": wg_peer,
        }

    return resolve
