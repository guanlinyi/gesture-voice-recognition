"""Local HTTPS identity: append new identities; reuse matching valid certificates."""
from __future__ import annotations

from datetime import datetime
import ipaddress
import json
from pathlib import Path
import subprocess
import shutil
import sys
import uuid

from ..paths import DATA
HERE = DATA
OPENSSL = "/usr/bin/openssl"


def _command_output(command):
    try:
        return subprocess.run(command, capture_output=True, text=True, timeout=5, check=True).stdout
    except (OSError, subprocess.SubprocessError):
        return ""


def route_addresses(text):
    """Prefer the source IP of the default physical interface, never the gateway."""
    routes, defaults = [], {}
    for line in text.splitlines():
        words = line.split()
        if not words or "dev" not in words:
            continue
        def field(name):
            index = words.index(name) if name in words else -1
            return words[index + 1] if 0 <= index < len(words) - 1 else None
        device = field("dev") or ""
        if device == "lo" or device.startswith(("docker", "br-", "virbr", "veth", "tun", "tap", "wg")) or "linkdown" in words:
            continue
        try:
            metric = int(field("metric") or 0)
        except ValueError:
            metric = 0
        if words[0] == "default":
            defaults[device] = min(metric, defaults.get(device, metric))
        sources = normalize_addresses([field("src") or ""])
        if sources:
            routes.append((device, sources[0]))
    routes.sort(key=lambda item: (item[0] not in defaults, defaults.get(item[0], 0), item[0], item[1]))
    return list(dict.fromkeys(address for _, address in routes))


def resolve_addresses(requested=None):
    routes = route_addresses(_command_output(["ip", "-4", "route"]))
    if not routes:
        routes = route_addresses(_command_output(["ip", "-4", "route", "get", "1.1.1.1"]))
    hosts = normalize_addresses(_command_output(["hostname", "-I"]).split())
    current = set(routes + hosts)
    if requested:
        supplied = normalize_addresses(requested)
        valid = [address for address in supplied if address in current]
        if len(valid) == len(supplied) and valid:
            return valid
        if current:
            print("指定的电脑 IP 已失效，已根据 ip route / 本机网卡重新获取地址。", file=sys.stderr, flush=True)
        else:
            raise ValueError("无法校验电脑 IP：请检查网络，并在桌面终端运行 ip route")
    return routes or hosts


def lan_addresses() -> list[str]:
    return resolve_addresses()


def normalize_addresses(addresses) -> list[str]:
    result = set()
    for value in addresses:
        try:
            address = ipaddress.ip_address(value)
        except ValueError:
            continue
        if address.version == 4 and not (address.is_loopback or address.is_unspecified or address.is_multicast):
            result.add(str(address))
    return sorted(result)


def certificate_for(addresses, root=HERE / "https"):
    addresses = normalize_addresses(addresses)
    if not addresses:
        raise ValueError("无法获取电脑局域网 IPv4 地址。请检查网络，在桌面终端运行 ip route 查看 src 地址")
    root = Path(root)
    for manifest in sorted(root.glob("*/output_json/identity.json"), reverse=True):
        data = json.loads(manifest.read_text(encoding="utf-8"))
        if data.get("addresses") != addresses:
            continue
        base = manifest.parent.parent
        cert, key, ca = base / "output_crt/server.crt", base / "output_pem/server-key.pem", base / "output_crt/phone-ca.crt"
        if all(path.is_file() for path in (cert, key, ca)):
            valid = subprocess.run([OPENSSL, "x509", "-checkend", "86400", "-noout", "-in", str(cert)], capture_output=True)
            if valid.returncode == 0:
                return cert, key, ca

    # Every write below is in a newly claimed identity directory.
    base = root / (datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8])
    base.mkdir(parents=True, mode=0o700, exist_ok=False)
    for folder in ("output_pem", "output_crt", "output_csr", "input_cnf", "output_json"):
        (base / folder).mkdir()
    cert, key, ca = base / "output_crt/server.crt", base / "output_pem/server-key.pem", base / "output_crt/phone-ca.crt"
    ca_key, csr = base / "output_pem/ca-key.pem", base / "output_csr/server.csr"
    extensions = base / "input_cnf/server.cnf"
    san = ",".join(["DNS:localhost", "IP:127.0.0.1"] + [f"IP:{ip}" for ip in addresses])
    with extensions.open("x", encoding="ascii") as output:
        output.write("basicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature,keyEncipherment\nextendedKeyUsage=serverAuth\nsubjectAltName=" + san + "\n")

    def openssl(*args):
        result = subprocess.run([OPENSSL, *map(str, args)], capture_output=True, text=True, timeout=45)
        if result.returncode:
            raise RuntimeError(f"本地 HTTPS 证书生成失败：{result.stderr[-1000:]}")

    reused_ca = False
    for manifest in sorted(root.glob("*/output_json/identity.json"), reverse=True):
        previous = manifest.parent.parent
        previous_ca, previous_key = previous / "output_crt/phone-ca.crt", previous / "output_pem/ca-key.pem"
        if not previous_ca.is_file() or not previous_key.is_file():
            continue
        valid = subprocess.run([OPENSSL, "x509", "-checkend", "86400", "-noout", "-in", str(previous_ca)], capture_output=True)
        cert_public = subprocess.run([OPENSSL, "x509", "-pubkey", "-noout", "-in", str(previous_ca)], capture_output=True)
        key_public = subprocess.run([OPENSSL, "pkey", "-in", str(previous_key), "-pubout"], capture_output=True)
        if valid.returncode == cert_public.returncode == key_public.returncode == 0 and cert_public.stdout == key_public.stdout:
            with previous_ca.open("rb") as source, ca.open("xb") as target:
                shutil.copyfileobj(source, target)
            with previous_key.open("rb") as source, ca_key.open("xb") as target:
                shutil.copyfileobj(source, target)
            reused_ca = True
            break
    if not reused_ca:
        openssl("req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "3650", "-sha256",
                "-subj", "/CN=Orin Phone Microphone Local CA", "-addext", "basicConstraints=critical,CA:TRUE",
                "-addext", "keyUsage=critical,keyCertSign,cRLSign", "-keyout", ca_key, "-out", ca)
    ca_key.chmod(0o600)
    openssl("req", "-new", "-newkey", "rsa:2048", "-nodes", "-sha256", "-subj", "/CN=Orin Phone Microphone",
            "-keyout", key, "-out", csr)
    key.chmod(0o600)
    openssl("x509", "-req", "-in", csr, "-CA", ca, "-CAkey", ca_key,
            "-set_serial", str(uuid.uuid4().int), "-days", "365", "-sha256", "-extfile", extensions, "-out", cert)
    with (base / "output_json/identity.json").open("x", encoding="utf-8") as output:
        json.dump({"addresses": addresses, "created_at": datetime.now().isoformat()}, output, indent=2)
    return cert, key, ca
