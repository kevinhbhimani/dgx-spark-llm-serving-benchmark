#!/usr/bin/env python3
"""Validate and serialize a two-node cluster topology."""
from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import re
import stat
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TypeAlias

try:
    import yaml
except ModuleNotFoundError as error:
    raise SystemExit(
        f"PyYAML is required by {sys.executable}; install requirements.lock before startup"
    ) from error


JsonScalar: TypeAlias = str | int | bool | None
JsonValue: TypeAlias = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]
SLUG_PATTERN = re.compile(r"^[a-z0-9]+(?:[-_][a-z0-9]+)*$")
HOSTNAME_PATTERN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")
SSH_TARGET_PATTERN = re.compile(
    r"^(?:[A-Za-z0-9._-]+@)?[A-Za-z0-9][A-Za-z0-9._:-]*$"
)
INTERFACE_PATTERN = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,14}$")
ROOT_KEYS = {"schema_version", "head_node", "rendezvous_rail", "ray_port", "nodes"}
NODE_KEYS = {"id", "hostname", "ssh_target", "interfaces"}
INTERFACE_KEYS = {"rail", "name", "ipv4"}


class TopologyError(ValueError):
    """A topology document or local selection violates the schema."""


class UniqueKeySafeLoader(yaml.SafeLoader):
    """Safe YAML loader that rejects duplicate mapping keys."""

    def construct_mapping(
        self, node: yaml.MappingNode, deep: bool = False
    ) -> dict[object, object]:
        if not isinstance(node, yaml.MappingNode):
            raise TopologyError("expected a YAML mapping")
        self.flatten_mapping(node)
        mapping: dict[object, object] = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            try:
                duplicate = key in mapping
            except TypeError as error:
                raise TopologyError("YAML mapping keys must be scalar values") from error
            if duplicate:
                raise TopologyError(f"duplicate YAML key: {key!r}")
            mapping[key] = self.construct_object(value_node, deep=deep)
        return mapping


@dataclass(frozen=True)
class RailInterface:
    rail: str
    name: str
    address: ipaddress.IPv4Address
    prefix: int
    network: ipaddress.IPv4Network

    @property
    def cidr(self) -> str:
        return f"{self.address}/{self.prefix}"


@dataclass(frozen=True)
class ClusterNode:
    node_id: str
    hostname: str
    ssh_target: str
    interfaces: tuple[RailInterface, ...]

    def interface_for(self, rail: str) -> RailInterface:
        for interface in self.interfaces:
            if interface.rail == rail:
                return interface
        raise TopologyError(f"node {self.node_id} has no interface for rail {rail}")


@dataclass(frozen=True)
class ClusterTopology:
    schema_version: int
    head_node: str
    rendezvous_rail: str
    ray_port: int
    nodes: tuple[ClusterNode, ClusterNode]
    raw_sha256: str

    def node(self, node_id: str) -> ClusterNode:
        for node in self.nodes:
            if node.node_id == node_id:
                return node
        raise TopologyError(f"unknown node ID: {node_id}")

    def select_local(self, hostname: str) -> ClusterNode:
        normalized_hostname = normalize_hostname(hostname)
        matches = [node for node in self.nodes if node.hostname == normalized_hostname]
        if len(matches) != 1:
            raise TopologyError(
                f"hostname -s '{hostname}' matches {len(matches)} configured nodes"
            )
        return matches[0]

    @property
    def worker_node(self) -> ClusterNode:
        return next(node for node in self.nodes if node.node_id != self.head_node)

    @property
    def head_node_record(self) -> ClusterNode:
        return self.node(self.head_node)

    def canonical_object(self) -> dict[str, JsonValue]:
        canonical_nodes: list[JsonValue] = []
        for node in sorted(self.nodes, key=lambda current: current.node_id):
            canonical_nodes.append(
                {
                    "id": node.node_id,
                    "hostname": node.hostname,
                    "ssh_target": node.ssh_target,
                    "interfaces": [
                        {
                            "rail": interface.rail,
                            "name": interface.name,
                            "ipv4": interface.cidr,
                        }
                        for interface in sorted(node.interfaces, key=lambda item: item.rail)
                    ],
                }
            )
        return {
            "schema_version": self.schema_version,
            "head_node": self.head_node,
            "rendezvous_rail": self.rendezvous_rail,
            "ray_port": self.ray_port,
            "nodes": canonical_nodes,
        }

    def canonical_bytes(self) -> bytes:
        return json.dumps(
            self.canonical_object(), sort_keys=True, separators=(",", ":")
        ).encode("utf-8")

    @property
    def canonical_sha256(self) -> str:
        return hashlib.sha256(self.canonical_bytes()).hexdigest()


def require_mapping(value: object, location: str, allowed_keys: set[str]) -> dict[str, object]:
    if not isinstance(value, dict):
        raise TopologyError(f"{location} must be a mapping")
    mapping: dict[str, object] = {}
    for key, item in value.items():
        if not isinstance(key, str):
            raise TopologyError(f"{location} keys must be strings")
        if key not in allowed_keys:
            raise TopologyError(f"unknown key at {location}: {key}")
        mapping[key] = item
    return mapping


def require_string(value: object, location: str) -> str:
    if not isinstance(value, str) or not value:
        raise TopologyError(f"{location} must be a nonempty string")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise TopologyError(f"{location} contains a control character")
    return value


def require_slug(value: object, location: str) -> str:
    slug = require_string(value, location)
    if SLUG_PATTERN.fullmatch(slug) is None:
        raise TopologyError(f"{location} must be a lowercase stable slug")
    return slug


def normalize_hostname(value: str) -> str:
    hostname = value.rstrip(".").casefold()
    if HOSTNAME_PATTERN.fullmatch(hostname) is None:
        raise TopologyError(f"hostname must be a short DNS label: {value!r}")
    return hostname


def parse_interface(value: object, node_id: str, index: int) -> RailInterface:
    location = f"nodes[{node_id}].interfaces[{index}]"
    mapping = require_mapping(value, location, INTERFACE_KEYS)
    rail = require_slug(mapping.get("rail"), f"{location}.rail")
    name = require_string(mapping.get("name"), f"{location}.name")
    if INTERFACE_PATTERN.fullmatch(name) is None:
        raise TopologyError(f"{location}.name is not a valid Linux interface token")
    address_text = require_string(mapping.get("ipv4"), f"{location}.ipv4")
    try:
        parsed = ipaddress.IPv4Interface(address_text)
    except ValueError as error:
        raise TopologyError(f"{location}.ipv4 must be an IPv4 address with prefix") from error
    network = parsed.network
    if parsed.ip.is_loopback or parsed.ip.is_multicast or parsed.ip.is_unspecified:
        raise TopologyError(f"{location}.ipv4 must be a unicast host address")
    if network.prefixlen <= 30 and parsed.ip in {
        network.network_address,
        network.broadcast_address,
    }:
        raise TopologyError(f"{location}.ipv4 is a subnet endpoint, not a host")
    return RailInterface(rail, name, parsed.ip, network.prefixlen, network)


def parse_node(value: object, index: int) -> ClusterNode:
    location = f"nodes[{index}]"
    mapping = require_mapping(value, location, NODE_KEYS)
    node_id = require_slug(mapping.get("id"), f"{location}.id")
    hostname = normalize_hostname(require_string(mapping.get("hostname"), f"{location}.hostname"))
    ssh_target = require_string(mapping.get("ssh_target"), f"{location}.ssh_target")
    if SSH_TARGET_PATTERN.fullmatch(ssh_target) is None:
        raise TopologyError(f"{location}.ssh_target is not a single SSH destination")
    interface_values = mapping.get("interfaces")
    if not isinstance(interface_values, list) or not interface_values:
        raise TopologyError(f"{location}.interfaces must be a nonempty list")
    interfaces = tuple(
        parse_interface(interface_value, node_id, interface_index)
        for interface_index, interface_value in enumerate(interface_values)
    )
    rails = [interface.rail for interface in interfaces]
    names = [interface.name for interface in interfaces]
    if len(rails) != len(set(rails)):
        raise TopologyError(f"node {node_id} has duplicate rail IDs")
    if len(names) != len(set(names)):
        raise TopologyError(f"node {node_id} has duplicate interface names")
    return ClusterNode(node_id, hostname, ssh_target, interfaces)


def parse_topology(raw_bytes: bytes) -> ClusterTopology:
    try:
        document: object = yaml.load(raw_bytes, Loader=UniqueKeySafeLoader)
    except (yaml.YAMLError, UnicodeDecodeError, TopologyError) as error:
        raise TopologyError(f"invalid topology YAML: {error}") from error
    mapping = require_mapping(document, "root", ROOT_KEYS)
    schema_version = mapping.get("schema_version")
    if type(schema_version) is not int or schema_version != 1:
        raise TopologyError("schema_version must be integer 1")
    head_node = require_slug(mapping.get("head_node"), "head_node")
    rendezvous_rail = require_slug(mapping.get("rendezvous_rail"), "rendezvous_rail")
    ray_port = mapping.get("ray_port")
    if type(ray_port) is not int or not 1 <= ray_port <= 65535:
        raise TopologyError("ray_port must be an integer from 1 through 65535")
    node_values = mapping.get("nodes")
    if not isinstance(node_values, list) or len(node_values) != 2:
        raise TopologyError("Step 1 requires exactly two nodes")
    nodes = tuple(parse_node(node_value, index) for index, node_value in enumerate(node_values))
    node_ids = [node.node_id for node in nodes]
    hostnames = [node.hostname for node in nodes]
    if len(set(node_ids)) != 2:
        raise TopologyError("node IDs must be unique")
    if len(set(hostnames)) != 2:
        raise TopologyError("hostnames must be unique after normalization")
    if head_node not in node_ids:
        raise TopologyError(f"head_node '{head_node}' does not identify a configured node")
    rail_sets = [{interface.rail for interface in node.interfaces} for node in nodes]
    if rail_sets[0] != rail_sets[1]:
        raise TopologyError("both nodes must declare the same rail IDs")
    if rendezvous_rail not in rail_sets[0]:
        raise TopologyError("rendezvous_rail must be present on both nodes")
    networks: list[ipaddress.IPv4Network] = []
    addresses: set[ipaddress.IPv4Address] = set()
    first_node, second_node = nodes
    for rail in sorted(rail_sets[0]):
        first_interface = first_node.interface_for(rail)
        second_interface = second_node.interface_for(rail)
        if first_interface.network != second_interface.network:
            raise TopologyError(f"rail {rail} endpoints must use the same IPv4 network")
        if first_interface.prefix != second_interface.prefix:
            raise TopologyError(f"rail {rail} endpoints must use the same prefix")
        if first_interface.address == second_interface.address:
            raise TopologyError(f"rail {rail} endpoints must have different IPv4 addresses")
        networks.append(first_interface.network)
        for interface in (first_interface, second_interface):
            if interface.address in addresses:
                raise TopologyError(f"duplicate IPv4 address: {interface.address}")
            addresses.add(interface.address)
    for network_index, network in enumerate(networks):
        if any(network.overlaps(other) for other in networks[:network_index]):
            raise TopologyError(f"rail networks must not overlap: {network}")
    return ClusterTopology(
        schema_version=1,
        head_node=head_node,
        rendezvous_rail=rendezvous_rail,
        ray_port=ray_port,
        nodes=(nodes[0], nodes[1]),
        raw_sha256=hashlib.sha256(raw_bytes).hexdigest(),
    )


def read_topology(path: Path) -> ClusterTopology:
    try:
        file_status = path.stat()
        if not stat.S_ISREG(file_status.st_mode):
            raise TopologyError(f"topology path is not a regular file: {path}")
        raw_bytes = path.read_bytes()
    except OSError as error:
        raise TopologyError(f"cannot read topology file {path}: {error}") from error
    return parse_topology(raw_bytes)


def local_hostname() -> str:
    try:
        completed = subprocess.run(
            ["hostname", "-s"], check=True, capture_output=True, text=True, timeout=5
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise TopologyError(f"cannot determine local short hostname: {error}") from error
    hostname = completed.stdout.strip()
    if not hostname:
        raise TopologyError("hostname -s returned an empty value")
    return hostname


def shell_fields(topology: ClusterTopology, hostname: str) -> list[str]:
    local_node = topology.select_local(hostname)
    head_node = topology.head_node_record
    worker_node = topology.worker_node
    local_interface = local_node.interface_for(topology.rendezvous_rail)
    head_interface = head_node.interface_for(topology.rendezvous_rail)
    worker_interface = worker_node.interface_for(topology.rendezvous_rail)
    fields = [
        str(topology.schema_version),
        topology.canonical_sha256,
        topology.raw_sha256,
        head_node.node_id,
        worker_node.node_id,
        local_node.node_id,
        head_node.hostname,
        worker_node.hostname,
        str(head_interface.address),
        str(worker_interface.address),
        str(local_interface.address),
        str(topology.ray_port),
        local_node.ssh_target,
        worker_node.ssh_target,
        str(len(local_node.interfaces)),
    ]
    for rail in sorted(interface.rail for interface in local_node.interfaces):
        local_interface = local_node.interface_for(rail)
        peer_node = worker_node if local_node.node_id == head_node.node_id else head_node
        peer_interface = peer_node.interface_for(rail)
        fields.extend(
            [rail, local_interface.name, local_interface.cidr, peer_interface.name, peer_interface.cidr]
        )
    fields.extend(
        [
            ",".join(
                interface.name
                for interface in sorted(head_node.interfaces, key=lambda item: item.rail)
            ),
            ",".join(
                interface.name
                for interface in sorted(worker_node.interfaces, key=lambda item: item.rail)
            ),
        ]
    )
    return fields


def write_shell_fields(fields: list[str]) -> None:
    for field in fields:
        sys.stdout.buffer.write(field.encode("utf-8") + b"\0")


def validate_command(path: Path, hostname: str | None) -> int:
    topology = read_topology(path)
    print(f"schema_version: {topology.schema_version}")
    print(f"topology_sha256: {topology.canonical_sha256}")
    print(f"raw_file_sha256: {topology.raw_sha256}")
    print(f"head_node: {topology.head_node}")
    print(f"rendezvous_rail: {topology.rendezvous_rail}")
    print(f"ray_port: {topology.ray_port}")
    for node in sorted(topology.nodes, key=lambda current: current.node_id):
        print(f"node: {node.node_id} hostname={node.hostname} ssh_target={node.ssh_target}")
        for interface in sorted(node.interfaces, key=lambda current: current.rail):
            print(
                f"  rail: {interface.rail} interface={interface.name} ipv4={interface.cidr}"
            )
    if hostname is not None:
        local_node = topology.select_local(hostname)
        role = "head" if local_node.node_id == topology.head_node else "worker"
        print(f"local_node: {local_node.node_id} role={role}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    validate_parser = subparsers.add_parser("validate", help="validate and describe YAML")
    validate_parser.add_argument("path", type=Path)
    validate_parser.add_argument("--hostname")
    shell_parser = subparsers.add_parser("shell", help="write fixed-order NUL fields")
    shell_parser.add_argument("path", type=Path)
    shell_parser.add_argument("--hostname", default=None)
    return parser


def main(arguments: list[str] | None = None) -> int:
    parser = build_parser()
    parsed = parser.parse_args(arguments)
    try:
        if parsed.command == "validate":
            return validate_command(parsed.path, parsed.hostname)
        topology = read_topology(parsed.path)
        hostname = parsed.hostname or local_hostname()
        write_shell_fields(shell_fields(topology, hostname))
        return 0
    except TopologyError as error:
        print(f"[topology] {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
