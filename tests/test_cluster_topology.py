from __future__ import annotations

import copy
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import cluster_topology


def valid_document(rail_count: int = 2, head_node: str = "spark_a") -> dict[str, object]:
    first_interfaces: list[dict[str, str]] = []
    second_interfaces: list[dict[str, str]] = []
    for rail_index in range(rail_count):
        subnet = 2 * rail_index
        rail = f"rail_{rail_index}"
        first_interfaces.append(
            {
                "rail": rail,
                "name": f"enp1s0f{rail_index}np0",
                "ipv4": f"192.0.{subnet}.1/30",
            }
        )
        second_interfaces.append(
            {
                "rail": rail,
                "name": f"cx_primary_{rail_index}",
                "ipv4": f"192.0.{subnet}.2/30",
            }
        )
    return {
        "schema_version": 1,
        "head_node": head_node,
        "rendezvous_rail": "rail_0",
        "ray_port": 6379,
        "nodes": [
            {
                "id": "spark_a",
                "hostname": "lab-alpha",
                "ssh_target": "lab-alpha",
                "interfaces": first_interfaces,
            },
            {
                "id": "spark_b",
                "hostname": "lab-beta",
                "ssh_target": "lab-beta",
                "interfaces": second_interfaces,
            },
        ],
    }


def encode_document(document: object) -> bytes:
    return yaml.safe_dump(document, sort_keys=False).encode("utf-8")


class ClusterTopologyTests(unittest.TestCase):
    def test_selects_either_local_node_and_uses_local_interface_names(self) -> None:
        topology = cluster_topology.parse_topology(encode_document(valid_document()))

        head_fields = cluster_topology.shell_fields(topology, "LAB-ALPHA")
        worker_fields = cluster_topology.shell_fields(topology, "lab-beta")

        self.assertEqual(head_fields[5], "spark_a")
        self.assertEqual(head_fields[9], "192.0.0.2")
        self.assertEqual(head_fields[15:20], [
            "rail_0", "enp1s0f0np0", "192.0.0.1/30",
            "cx_primary_0", "192.0.0.2/30",
        ])
        self.assertEqual(worker_fields[5], "spark_b")
        self.assertEqual(worker_fields[10], "192.0.0.2")
        self.assertEqual(worker_fields[15:20], [
            "rail_0", "cx_primary_0", "192.0.0.2/30",
            "enp1s0f0np0", "192.0.0.1/30",
        ])

    def test_canonical_hash_ignores_yaml_order_and_changes_with_role(self) -> None:
        document = valid_document()
        reordered = copy.deepcopy(document)
        reordered_nodes = reordered["nodes"]
        assert isinstance(reordered_nodes, list)
        reordered["nodes"] = list(reversed(reordered_nodes))
        original = cluster_topology.parse_topology(encode_document(document))
        same_topology = cluster_topology.parse_topology(encode_document(reordered))
        changed_document = valid_document(head_node="spark_b")
        changed_topology = cluster_topology.parse_topology(encode_document(changed_document))

        self.assertEqual(original.canonical_sha256, same_topology.canonical_sha256)
        self.assertNotEqual(original.canonical_sha256, changed_topology.canonical_sha256)
        self.assertNotEqual(original.raw_sha256, same_topology.raw_sha256)

    def test_accepts_one_two_and_four_rails(self) -> None:
        for rail_count in (1, 2, 4):
            with self.subTest(rail_count=rail_count):
                topology = cluster_topology.parse_topology(
                    encode_document(valid_document(rail_count=rail_count))
                )
                self.assertEqual(len(topology.nodes[0].interfaces), rail_count)

    def test_accepts_shared_and_overlapping_rail_networks(self) -> None:
        node_addresses = (
            ("10.65.0.10", "10.65.0.20", "10.65.0.30", "10.65.0.40"),
            ("10.65.0.11", "10.65.0.21", "10.65.0.31", "10.65.0.41"),
        )
        for rail_prefixes in ((26, 26, 26, 26), (24, 26, 26, 26)):
            with self.subTest(rail_prefixes=rail_prefixes):
                document = valid_document(rail_count=len(rail_prefixes))
                node_values = document["nodes"]
                assert isinstance(node_values, list)
                for node_value, addresses in zip(node_values, node_addresses, strict=True):
                    assert isinstance(node_value, dict)
                    interface_values = node_value["interfaces"]
                    assert isinstance(interface_values, list)
                    for interface_value, address, prefix in zip(
                        interface_values, addresses, rail_prefixes, strict=True
                    ):
                        assert isinstance(interface_value, dict)
                        interface_value["ipv4"] = f"{address}/{prefix}"

                topology = cluster_topology.parse_topology(encode_document(document))

                self.assertEqual(len(topology.nodes[0].interfaces), len(rail_prefixes))

    def test_rejects_duplicate_keys_unknown_fields_bad_types_and_node_counts(self) -> None:
        document = valid_document()
        invalid_documents = [
            b"schema_version: 1\nschema_version: 1\n",
            encode_document({**document, "unknown": "value"}),
            encode_document({**document, "schema_version": True}),
            encode_document({**document, "ray_port": True}),
        ]
        three_nodes = copy.deepcopy(document)
        nodes = three_nodes["nodes"]
        assert isinstance(nodes, list)
        three_nodes["nodes"] = [*nodes, copy.deepcopy(nodes[0])]
        invalid_documents.append(encode_document(three_nodes))

        for invalid_document in invalid_documents:
            with self.subTest(invalid_document=invalid_document):
                with self.assertRaises(cluster_topology.TopologyError):
                    cluster_topology.parse_topology(invalid_document)

    def test_rejects_duplicate_addresses_mismatched_networks_and_duplicate_hosts(self) -> None:
        invalid_documents: list[dict[str, object]] = []

        duplicate_address = valid_document()
        duplicate_nodes = duplicate_address["nodes"]
        assert isinstance(duplicate_nodes, list)
        duplicate_worker = duplicate_nodes[1]
        assert isinstance(duplicate_worker, dict)
        duplicate_interfaces = duplicate_worker["interfaces"]
        assert isinstance(duplicate_interfaces, list)
        duplicate_interfaces[0]["ipv4"] = "192.0.0.1/30"
        invalid_documents.append(duplicate_address)

        mismatched_network = valid_document()
        mismatched_nodes = mismatched_network["nodes"]
        assert isinstance(mismatched_nodes, list)
        mismatched_nodes[1]["interfaces"][0]["ipv4"] = "192.0.1.2/30"
        invalid_documents.append(mismatched_network)

        duplicate_hostname = valid_document()
        hostname_nodes = duplicate_hostname["nodes"]
        assert isinstance(hostname_nodes, list)
        hostname_nodes[1]["hostname"] = "lab-alpha"
        invalid_documents.append(duplicate_hostname)

        for invalid_document in invalid_documents:
            with self.subTest(invalid_document=invalid_document):
                with self.assertRaises(cluster_topology.TopologyError):
                    cluster_topology.parse_topology(encode_document(invalid_document))

    def test_rejects_invalid_role_rail_interface_prefix_and_ssh_values(self) -> None:
        invalid_documents: list[dict[str, object]] = []

        duplicate_ids = valid_document()
        duplicate_id_nodes = duplicate_ids["nodes"]
        assert isinstance(duplicate_id_nodes, list)
        duplicate_id_nodes[1]["id"] = "spark_a"
        invalid_documents.append(duplicate_ids)

        unknown_head = valid_document(head_node="spark_c")
        invalid_documents.append(unknown_head)

        missing_rendezvous = valid_document()
        missing_rendezvous["rendezvous_rail"] = "rail_9"
        invalid_documents.append(missing_rendezvous)

        wrong_prefix = valid_document()
        prefix_nodes = wrong_prefix["nodes"]
        assert isinstance(prefix_nodes, list)
        prefix_nodes[1]["interfaces"][0]["ipv4"] = "192.0.0.2/29"
        invalid_documents.append(wrong_prefix)

        invalid_interface = valid_document()
        interface_nodes = invalid_interface["nodes"]
        assert isinstance(interface_nodes, list)
        interface_nodes[0]["interfaces"][0]["name"] = "invalid interface"
        invalid_documents.append(invalid_interface)

        invalid_ssh_target = valid_document()
        ssh_nodes = invalid_ssh_target["nodes"]
        assert isinstance(ssh_nodes, list)
        ssh_nodes[1]["ssh_target"] = "lab-beta; rm -rf /"
        invalid_documents.append(invalid_ssh_target)

        for invalid_document in invalid_documents:
            with self.subTest(invalid_document=invalid_document):
                with self.assertRaises(cluster_topology.TopologyError):
                    cluster_topology.parse_topology(encode_document(invalid_document))

    def test_canonical_hash_tracks_interface_names_and_addresses(self) -> None:
        original = cluster_topology.parse_topology(
            encode_document(valid_document())
        )
        changed_interface = valid_document()
        changed_nodes = changed_interface["nodes"]
        assert isinstance(changed_nodes, list)
        changed_nodes[0]["interfaces"][0]["name"] = "fabric0"
        changed_address = valid_document()
        address_nodes = changed_address["nodes"]
        assert isinstance(address_nodes, list)
        address_nodes[0]["interfaces"][0]["ipv4"] = "192.0.0.5/30"
        address_nodes[1]["interfaces"][0]["ipv4"] = "192.0.0.6/30"

        self.assertNotEqual(
            original.canonical_sha256,
            cluster_topology.parse_topology(encode_document(changed_interface)).canonical_sha256,
        )
        self.assertNotEqual(
            original.canonical_sha256,
            cluster_topology.parse_topology(encode_document(changed_address)).canonical_sha256,
        )

    def test_unknown_hostname_fails_local_selection(self) -> None:
        topology = cluster_topology.parse_topology(encode_document(valid_document()))
        with self.assertRaisesRegex(cluster_topology.TopologyError, "matches 0"):
            topology.select_local("lab-gamma")

    def test_reads_a_regular_file_and_tracks_raw_and_canonical_hashes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cluster.yaml"
            path.write_bytes(encode_document(valid_document()))
            topology = cluster_topology.read_topology(path)

        self.assertEqual(len(topology.raw_sha256), 64)
        self.assertEqual(len(topology.canonical_sha256), 64)


if __name__ == "__main__":
    unittest.main()
