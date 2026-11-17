#!/usr/bin/env bash
# Shared topology and container settings for the two node DGX Spark benchmark.
# Source from other scripts: source scripts/connectx_env.sh

CONNECTX_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONNECTX_REPO_ROOT="$(cd "$CONNECTX_SCRIPT_DIR/.." && pwd)"
source "$CONNECTX_SCRIPT_DIR/load_env.sh"
resolve_single_node_mode || return $? 2>/dev/null || exit $?

if [[ "$SINGLE_NODE_ONLY" == "1" ]]; then
  LOCAL_HOSTNAME="$(hostname -s 2>/dev/null | tr '[:upper:]' '[:lower:]' | sed 's/\\.$//')"
  if [[ ! "$LOCAL_HOSTNAME" =~ ^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$ ]]; then
    echo "[execution] cannot determine a valid local hostname" >&2
    return 1 2>/dev/null || exit 1
  fi
  SOLO_LOOPBACK_ADDRESS=127.0.0.1
  LOCAL_IP="$SOLO_LOOPBACK_ADDRESS"
  export LOCAL_HOSTNAME SOLO_LOOPBACK_ADDRESS LOCAL_IP
  unset CLUSTER_CONFIG CONNECTX_DERIVED_CONFIG CONNECTX_DERIVED_VALUES_SHA256
  unset SCHEMA_VERSION TOPOLOGY_SHA256 TOPOLOGY_RAW_SHA256
  unset HEAD_NODE_ID WORKER_NODE_ID LOCAL_NODE_ID HEAD_HOST WORKER_HOST
  unset HEAD_IP WORKER_IP RAY_PORT LOCAL_SSH_TARGET WORKER_SSH_TARGET
  unset TOPOLOGY_RAIL_COUNT TOPOLOGY_RAILS TOPOLOGY_LOCAL_INTERFACES
  unset TOPOLOGY_LOCAL_CIDRS TOPOLOGY_PEER_INTERFACES TOPOLOGY_PEER_CIDRS
  unset HEAD_CX_IFACE WORKER_CX_IFACE CX_IFACE VLLM_HOST_IP
  unset NCCL_SOCKET_IFNAME GLOO_SOCKET_IFNAME NCCL_IB_DISABLE NCCL_IB_HCA
  unset NCCL_IB_GID_INDEX NCCL_NET_PLUGIN NCCL_DEBUG NCCL_DEBUG_SUBSYS
else
  CLUSTER_CONFIG="${CLUSTER_CONFIG:-$CONNECTX_REPO_ROOT/configs/cluster.yaml}"
  if [[ "$CLUSTER_CONFIG" != /* ]]; then
    CLUSTER_CONFIG="$CONNECTX_REPO_ROOT/$CLUSTER_CONFIG"
  fi
  export CLUSTER_CONFIG

connectx_topology_values_signature() {
  printf '%s\0' \
    "${SCHEMA_VERSION-}" "${TOPOLOGY_SHA256-}" \
    "${HEAD_NODE_ID-}" "${WORKER_NODE_ID-}" "${LOCAL_NODE_ID-}" \
    "${HEAD_HOST-}" "${WORKER_HOST-}" "${HEAD_IP-}" \
    "${WORKER_IP-}" "${LOCAL_IP-}" "${RAY_PORT-}" \
    "${CX_IFACE-}" "${VLLM_HOST_IP-}" \
    "${NCCL_SOCKET_IFNAME-}" "${GLOO_SOCKET_IFNAME-}" \
    "${HEAD_CX_IFACE-}" "${WORKER_CX_IFACE-}" \
    | sha256sum | awk '{print $1}'
}

_previous_derived_config="${CONNECTX_DERIVED_CONFIG-}"
_previous_derived_signature="${CONNECTX_DERIVED_VALUES_SHA256-}"
_current_derived_signature="$(connectx_topology_values_signature)"
_ignore_previous_derived_values=0
if [[ -n "$_previous_derived_config" \
      && "$_previous_derived_config" != "$CLUSTER_CONFIG" \
      && "$_previous_derived_signature" =~ ^[0-9a-f]{64}$ \
      && "$_current_derived_signature" == "$_previous_derived_signature" ]]; then
  _ignore_previous_derived_values=1
fi

_provided_schema_version="${SCHEMA_VERSION-}"
_provided_topology_sha256="${TOPOLOGY_SHA256-}"
_provided_head_node_id="${HEAD_NODE_ID-}"
_provided_worker_node_id="${WORKER_NODE_ID-}"
_provided_local_node_id="${LOCAL_NODE_ID-}"
_provided_head_host="${HEAD_HOST-}"
_provided_worker_host="${WORKER_HOST-}"
_provided_head_ip="${HEAD_IP-}"
_provided_worker_ip="${WORKER_IP-}"
_provided_local_ip="${LOCAL_IP-}"
_provided_ray_port="${RAY_PORT-}"
_provided_cx_iface="${CX_IFACE-}"
_provided_vllm_host_ip="${VLLM_HOST_IP-}"
_provided_nccl_socket_ifname="${NCCL_SOCKET_IFNAME-}"
_provided_gloo_socket_ifname="${GLOO_SOCKET_IFNAME-}"
_provided_head_cx_iface="${HEAD_CX_IFACE-}"
_provided_worker_cx_iface="${WORKER_CX_IFACE-}"
if (( _ignore_previous_derived_values )); then
  for _provided_name in \
      _provided_schema_version _provided_topology_sha256 \
      _provided_head_node_id _provided_worker_node_id \
      _provided_local_node_id _provided_head_host _provided_worker_host \
      _provided_head_ip _provided_worker_ip _provided_local_ip \
      _provided_ray_port _provided_cx_iface _provided_vllm_host_ip \
      _provided_nccl_socket_ifname _provided_gloo_socket_ifname \
      _provided_head_cx_iface _provided_worker_cx_iface; do
    printf -v "$_provided_name" '%s' ""
  done
fi
_topology_python="${TOPOLOGY_PYTHON:-${PYTHON:-}}"
if [[ -z "$_topology_python" ]]; then
  if [[ -x "$CONNECTX_REPO_ROOT/.venv/bin/python" ]]; then
    _topology_python="$CONNECTX_REPO_ROOT/.venv/bin/python"
  else
    _topology_python=python3
  fi
fi
_topology_record="$(mktemp "${TMPDIR:-/tmp}/cluster-topology.XXXXXX")" || {
  echo "[topology] cannot create a temporary topology transfer file" >&2
  return 1 2>/dev/null || exit 1
}
if ! "$_topology_python" "$CONNECTX_SCRIPT_DIR/cluster_topology.py" shell "$CLUSTER_CONFIG" >"$_topology_record"; then
  rm -f "$_topology_record"
  echo "[topology] validation failed with interpreter $_topology_python" >&2
  return 1 2>/dev/null || exit 1
fi

_topology_field_index=0
_topology_rail_index=0
_topology_rail_count=""
_topology_interface=""
_topology_cidr=""
_topology_peer_interface=""
_topology_peer_cidr=""
TOPOLOGY_RAILS=""
TOPOLOGY_LOCAL_INTERFACES=""
TOPOLOGY_LOCAL_CIDRS=""
TOPOLOGY_PEER_INTERFACES=""
TOPOLOGY_PEER_CIDRS=""
while IFS= read -r -d '' _topology_field; do
  case "$_topology_field_index" in
    0) SCHEMA_VERSION="$_topology_field" ;;
    1) TOPOLOGY_SHA256="$_topology_field" ;;
    2) TOPOLOGY_RAW_SHA256="$_topology_field" ;;
    3) HEAD_NODE_ID="$_topology_field" ;;
    4) WORKER_NODE_ID="$_topology_field" ;;
    5) LOCAL_NODE_ID="$_topology_field" ;;
    6) HEAD_HOST="$_topology_field" ;;
    7) WORKER_HOST="$_topology_field" ;;
    8) HEAD_IP="$_topology_field" ;;
    9) WORKER_IP="$_topology_field" ;;
    10) LOCAL_IP="$_topology_field" ;;
    11) RAY_PORT="$_topology_field" ;;
    12) LOCAL_SSH_TARGET="$_topology_field" ;;
    13) WORKER_SSH_TARGET="$_topology_field" ;;
    14) _topology_rail_count="$_topology_field" ;;
    *)
      if (( _topology_field_index >= 15 + 5 * _topology_rail_count )); then
        case $((_topology_field_index - 15 - 5 * _topology_rail_count)) in
          0) HEAD_CX_IFACE="$_topology_field" ;;
          1) WORKER_CX_IFACE="$_topology_field" ;;
        esac
      else
        case $(((_topology_field_index - 15) % 5)) in
          0) _topology_rail="$_topology_field" ;;
          1) _topology_interface="$_topology_field" ;;
          2) _topology_cidr="$_topology_field" ;;
          3) _topology_peer_interface="$_topology_field" ;;
          4)
            _topology_peer_cidr="$_topology_field"
            if [[ -n "$TOPOLOGY_RAILS" ]]; then
              TOPOLOGY_RAILS+=","
              TOPOLOGY_LOCAL_INTERFACES+=","
              TOPOLOGY_LOCAL_CIDRS+=","
              TOPOLOGY_PEER_INTERFACES+=","
              TOPOLOGY_PEER_CIDRS+=","
            fi
            TOPOLOGY_RAILS+="$_topology_rail"
            TOPOLOGY_LOCAL_INTERFACES+="$_topology_interface"
            TOPOLOGY_LOCAL_CIDRS+="$_topology_cidr"
            TOPOLOGY_PEER_INTERFACES+="$_topology_peer_interface"
            TOPOLOGY_PEER_CIDRS+="$_topology_peer_cidr"
            _topology_rail_index=$((_topology_rail_index + 1))
            ;;
        esac
      fi
      ;;
  esac
  _topology_field_index=$((_topology_field_index + 1))
done <"$_topology_record"
rm -f "$_topology_record"
if [[ ! "$_topology_rail_count" =~ ^[1-9][0-9]*$ ]] || (( _topology_rail_index != _topology_rail_count )) || (( _topology_field_index != 17 + 5 * _topology_rail_count )) || [[ -z "$HEAD_CX_IFACE" || -z "$WORKER_CX_IFACE" ]]; then
  echo "[topology] loader returned an invalid field record" >&2
  return 1 2>/dev/null || exit 1
fi
TOPOLOGY_RAIL_COUNT="$_topology_rail_count"
CX_IFACE="$TOPOLOGY_LOCAL_INTERFACES"

connectx_check_topology_override() {
  local variable_name="$1" current_value="$2" expected_value="$3"
  if [[ -n "$current_value" && "$current_value" != "$expected_value" ]]; then
    echo "[topology] inherited $variable_name='$current_value' conflicts with CLUSTER_CONFIG value '$expected_value'" >&2
    return 1
  fi
}
connectx_check_topology_override SCHEMA_VERSION "$_provided_schema_version" "$SCHEMA_VERSION" || return 1 2>/dev/null || exit 1
connectx_check_topology_override TOPOLOGY_SHA256 "$_provided_topology_sha256" "$TOPOLOGY_SHA256" || return 1 2>/dev/null || exit 1
connectx_check_topology_override HEAD_NODE_ID "$_provided_head_node_id" "$HEAD_NODE_ID" || return 1 2>/dev/null || exit 1
connectx_check_topology_override WORKER_NODE_ID "$_provided_worker_node_id" "$WORKER_NODE_ID" || return 1 2>/dev/null || exit 1
connectx_check_topology_override LOCAL_NODE_ID "$_provided_local_node_id" "$LOCAL_NODE_ID" || return 1 2>/dev/null || exit 1
connectx_check_topology_override HEAD_HOST "$_provided_head_host" "$HEAD_HOST" || return 1 2>/dev/null || exit 1
connectx_check_topology_override WORKER_HOST "$_provided_worker_host" "$WORKER_HOST" || return 1 2>/dev/null || exit 1
connectx_check_topology_override HEAD_IP "$_provided_head_ip" "$HEAD_IP" || return 1 2>/dev/null || exit 1
connectx_check_topology_override WORKER_IP "$_provided_worker_ip" "$WORKER_IP" || return 1 2>/dev/null || exit 1
connectx_check_topology_override LOCAL_IP "$_provided_local_ip" "$LOCAL_IP" || return 1 2>/dev/null || exit 1
connectx_check_topology_override RAY_PORT "$_provided_ray_port" "$RAY_PORT" || return 1 2>/dev/null || exit 1
connectx_check_topology_override HEAD_CX_IFACE "$_provided_head_cx_iface" "$HEAD_CX_IFACE" || return 1 2>/dev/null || exit 1
connectx_check_topology_override WORKER_CX_IFACE "$_provided_worker_cx_iface" "$WORKER_CX_IFACE" || return 1 2>/dev/null || exit 1
connectx_check_topology_override CX_IFACE "$_provided_cx_iface" "$CX_IFACE" || return 1 2>/dev/null || exit 1
connectx_check_topology_override VLLM_HOST_IP "$_provided_vllm_host_ip" "$LOCAL_IP" || return 1 2>/dev/null || exit 1
connectx_check_topology_override NCCL_SOCKET_IFNAME "$_provided_nccl_socket_ifname" "$CX_IFACE" || return 1 2>/dev/null || exit 1
connectx_check_topology_override GLOO_SOCKET_IFNAME "$_provided_gloo_socket_ifname" "$CX_IFACE" || return 1 2>/dev/null || exit 1

export SCHEMA_VERSION TOPOLOGY_SHA256 TOPOLOGY_RAW_SHA256 HEAD_NODE_ID WORKER_NODE_ID LOCAL_NODE_ID
export HEAD_HOST WORKER_HOST HEAD_IP WORKER_IP LOCAL_IP RAY_PORT
export LOCAL_SSH_TARGET WORKER_SSH_TARGET TOPOLOGY_RAIL_COUNT
export HEAD_CX_IFACE WORKER_CX_IFACE
export CX_IFACE
export NCCL_SOCKET_IFNAME="$CX_IFACE"
export GLOO_SOCKET_IFNAME="$CX_IFACE"
export VLLM_HOST_IP="$LOCAL_IP"
CONNECTX_DERIVED_CONFIG="$CLUSTER_CONFIG"
CONNECTX_DERIVED_VALUES_SHA256="$(connectx_topology_values_signature)"
export CONNECTX_DERIVED_CONFIG CONNECTX_DERIVED_VALUES_SHA256
unset _provided_schema_version _provided_topology_sha256 _provided_head_node_id
unset _provided_worker_node_id _provided_local_node_id _provided_head_host
unset _provided_worker_host _provided_head_ip _provided_worker_ip _provided_local_ip
unset _provided_ray_port _provided_cx_iface _provided_vllm_host_ip
unset _provided_nccl_socket_ifname _provided_gloo_socket_ifname
unset _provided_head_cx_iface _provided_worker_cx_iface
unset _previous_derived_config _previous_derived_signature
unset _current_derived_signature _ignore_previous_derived_values
unset _provided_name
unset _topology_python _topology_record _topology_field_index _topology_rail_index
unset _topology_rail_count _topology_interface _topology_cidr
unset _topology_peer_interface _topology_peer_cidr _topology_field

# NCCL data plane uses RDMA over ConnectX by default. Set NCCL_IB_DISABLE=1
# to fall back to TCP sockets, for example to diagnose a RoCE fault.
# GLOO and the Ray rendezvous use TCP sockets in both modes.
export NCCL_IB_DISABLE="${NCCL_IB_DISABLE:-0}"
# NCCL_DEBUG=INFO with NCCL_DEBUG_SUBSYS=INIT,NET logs one "via NET/IB" or
# "via NET/Socket" line per channel, at communicator setup only, so it never
# touches a measurement window. run_models.sh fails a dual launch whose log
# shows the wrong transport; audit_nccl_transport.py reports every launch.
export NCCL_DEBUG="${NCCL_DEBUG:-INFO}"
export NCCL_DEBUG_SUBSYS="${NCCL_DEBUG_SUBSYS:-INIT,NET}"
# NCCL RDMA device allowlist. Leave empty to use every device. Set a
# per-host device list when required, for example NCCL_IB_HCA="mlx5_0:1,mlx5_1:1".
export NCCL_IB_HCA="${NCCL_IB_HCA:-}"
# Empty loads the library NCCL discovers. Set to "none" to use the built-in
# IB transport instead of the HPC-X RDMA plugin bundled in the vLLM image.
export NCCL_NET_PLUGIN="${NCCL_NET_PLUGIN:-}"
# RoCEv2 GID index used by the built-in IB transport. Empty defers to NCCL.
export NCCL_IB_GID_INDEX="${NCCL_IB_GID_INDEX:-}"
fi

# Container settings. A mutable monthly tag alone is not sufficient provenance
# for a paper rerun, so the image is pinned by digest. This is NGC 26.09-py3
# (vLLM 0.29.0, CUDA 13.4.1, NCCL 2.31.2, HPC-X 2.51), required by the newer
# checkpoints in configs/models.yaml. The baselines/ campaigns used 26.04-py3
# (vLLM 0.19.0): nvcr.io/nvidia/vllm@sha256:98494fdaea68ad892c107180b0be41c99caa3c77683175becc4e202b2cb75b27
export VLLM_IMAGE="${VLLM_IMAGE:-nvcr.io/nvidia/vllm@sha256:557747337846eecf34486866f3d992383c6f8af869fdb0e365b5093fcaee02c2}"
export VLLM_CONTAINER="${VLLM_CONTAINER:-vllm-spark}"
if [[ "$SINGLE_NODE_ONLY" == "1" && "${CONNECTX_SOLO_CONTAINER_INITIALIZED:-0}" != "1" ]]; then
  if [[ "$VLLM_CONTAINER" != *-solo ]]; then
    VLLM_CONTAINER="${VLLM_CONTAINER}-solo"
  fi
  export VLLM_CONTAINER
  export CONNECTX_SOLO_CONTAINER_INITIALIZED=1
fi
export HF_CACHE="${HF_CACHE:-$HOME/.cache/huggingface-$UID}"
# HF_CACHE is the host Hugging Face home. Every host and container process uses
# its repository cache below that root.
export HF_HOME="$HF_CACHE"
export HF_HUB_CACHE="${HF_CACHE%/}/hub"
export HF_MODULES_CACHE="${HF_CACHE%/}/modules"
export HF_CACHE_UID="$(id -u)"
export HF_CACHE_GID="$(id -g)"
# Keep Hugging Face downloads in repo-local blobs. The cache-wide Xet blob
# store creates an extra symlink level that complicates mirroring and cleanup.
export HF_HUB_DISABLE_SHARED_BLOBS=1
# Use Docker's host-derived resolver by default. An explicit public resolver can
# be unreachable on managed networks even while the host resolver works, which
# would make the pinned Ray install fail before the cluster starts. Override
# this only when the local Docker resolver is known to be broken.
export CONTAINER_DNS="${CONTAINER_DNS-}"

# The image digest pins the platform stack. start_ray.sh installs the pinned
# Ray runtime using scripts/ray-runtime-constraints.txt, which also fixes its
# Python dependency versions to the latest full runtime recorded in this repo.
# Update the image, Ray version, and constraints together.
export RAY_VERSION="${RAY_VERSION:-2.56.1}"

check_hf_cache_access() {
  local script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  local python="${PYTHON:-python3}"
  mkdir -p "$HF_HUB_CACHE" || {
    echo "[cache] cannot create HF_HUB_CACHE=$HF_HUB_CACHE as UID:GID $HF_CACHE_UID:$HF_CACHE_GID" >&2
    return 1
  }
  "$python" "$script_dir/cache_access_probe.py" "$HF_HUB_CACHE"
}

check_hf_cache_access_in_image() {
  local script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  docker run --rm --user "$HF_CACHE_UID:$HF_CACHE_GID" \
    --entrypoint python3 \
    -v "$HF_CACHE:/hf-cache" \
    -v "$script_dir/cache_access_probe.py:/tmp/cache_access_probe.py:ro" \
    -e HOME=/tmp -e HF_HOME=/hf-cache -e HF_HUB_CACHE=/hf-cache/hub \
    "$VLLM_IMAGE" /tmp/cache_access_probe.py /hf-cache/hub
}

# Safety helpers shared by every launcher/orchestrator. Paper measurements must
# originate on the configured head because local GPU power is part of each row.
#
# CX_IFACE may be a comma-separated list. These functions iterate over every
# interface in the list.
require_local_connectx_ip() {
  local expected="$1" role="${2:-requested role}"
  if [[ "$expected" != "$LOCAL_IP" ]]; then
    echo "[cluster] local node $LOCAL_NODE_ID has $LOCAL_IP, expected $expected for $role" >&2
    return 1
  fi
}

validate_execution_environment() {
  local expected_head="${1:-}" role="${2:-requested role}"
  local validate_links="${3:-1}"
  if [[ "$SINGLE_NODE_ONLY" == "1" ]]; then
    [[ -n "$LOCAL_HOSTNAME" ]] || {
      echo "[execution] local hostname is unavailable for $role" >&2
      return 1
    }
    echo "[execution] solo mode on $LOCAL_HOSTNAME; dual-node checks disabled"
    return 0
  fi
  require_local_connectx_ip "$expected_head" "$role" || return 1
  if [[ "$validate_links" == "1" ]]; then
    validate_local_topology_links || return 1
  fi
}

reject_solo_execution() {
  local operation="${1:-this operation}"
  if [[ "$SINGLE_NODE_ONLY" == "1" ]]; then
    echo "[execution] $operation requires dual-node execution and cannot run with SINGLE_NODE_ONLY=1" >&2
    return 1
  fi
}

solo_resource_scope() {
  if [[ "$SINGLE_NODE_ONLY" == "1" ]]; then
    printf '%s\n' none
  else
    printf '%s\n' required
  fi
}

refuse_solo_ray_container_collision() {
  [[ "$SINGLE_NODE_ONLY" == "1" ]] || return 0
  if docker inspect -f '{{.Id}}' "$VLLM_CONTAINER" >/dev/null 2>&1; then
    if docker exec "$VLLM_CONTAINER" ray status 2>/dev/null | grep -q '^Active:'; then
      echo "[execution] refusing solo container '$VLLM_CONTAINER': it already runs a Ray runtime" >&2
      return 1
    fi
    echo "[execution] refusing solo container '$VLLM_CONTAINER': a pre-existing container is not owned by this command" >&2
    return 1
  fi
  return 0
}

validate_local_topology_links() {
  local index route route_interface route_source local_ip peer_ip address_output link_output
  local -a rails interfaces local_cidrs peer_cidrs
  IFS=',' read -ra rails <<<"$TOPOLOGY_RAILS"
  IFS=',' read -ra interfaces <<<"$TOPOLOGY_LOCAL_INTERFACES"
  IFS=',' read -ra local_cidrs <<<"$TOPOLOGY_LOCAL_CIDRS"
  IFS=',' read -ra peer_cidrs <<<"$TOPOLOGY_PEER_CIDRS"
  for index in "${!rails[@]}"; do
    local_ip="${local_cidrs[$index]%/*}"
    peer_ip="${peer_cidrs[$index]%/*}"
    address_output="$(ip -4 -o addr show dev "${interfaces[$index]}" 2>/dev/null | awk '$3 == "inet" {print $4}')"
    if ! grep -Fxq -- "${local_cidrs[$index]}" <<<"$address_output"; then
      echo "[cluster] rail ${rails[$index]} local ${interfaces[$index]} expected ${local_cidrs[$index]}, observed '${address_output:-none}'" >&2
      return 1
    fi
    if ! link_output="$(ip -o link show dev "${interfaces[$index]}" 2>/dev/null)" || [[ "$link_output" != *"state UP"* ]]; then
      echo "[cluster] rail ${rails[$index]} local ${interfaces[$index]} to peer ${peer_cidrs[$index]} is not link-up" >&2
      return 1
    fi
    if ! route="$(ip -4 route get "$peer_ip" from "$local_ip" 2>/dev/null)"; then
      echo "[cluster] rail ${rails[$index]} route to peer ${peer_cidrs[$index]} from local ${local_cidrs[$index]} failed" >&2
      return 1
    fi
    route_interface="$(awk '{for (i = 1; i < NF; i++) if ($i == "dev") {print $(i + 1); exit}}' <<<"$route")"
    route_source="$(awk '
      {
        for (field_index = 1; field_index < NF; field_index++) {
          if ($field_index == "src") route_source = $(field_index + 1)
          else if ($field_index == "from" && route_source == "") route_source = $(field_index + 1)
        }
      }
      END {print route_source}
    ' <<<"$route")"
    if [[ "$route_interface" != "${interfaces[$index]}" || "$route_source" != "$local_ip" ]]; then
      echo "[cluster] rail ${rails[$index]} route to peer ${peer_cidrs[$index]} expected dev ${interfaces[$index]} src $local_ip, observed dev ${route_interface:-unknown} src ${route_source:-unknown}" >&2
      return 1
    fi
    echo "[cluster] rail ${rails[$index]} local ${interfaces[$index]} $local_ip to peer $peer_ip"
    if ! ping -I "${interfaces[$index]}" -W 2 -c 3 "$peer_ip"; then
      echo "[cluster] rail ${rails[$index]} ping failed from ${local_cidrs[$index]} to ${peer_cidrs[$index]}" >&2
      return 1
    fi
  done
  require_connectx_rdma
}

# Link reachability through TCP does not prove that verbs are available. Check
# each requested rail before a launcher removes/recreates the serving container.
require_connectx_rdma() {
  case "$NCCL_IB_DISABLE" in
    1) return 0 ;;
    0) ;;
    *) echo "[cluster] NCCL_IB_DISABLE must be 0 (RDMA) or 1 (TCP)" >&2; return 1 ;;
  esac
  local iface links index line token link_name rdma_device rdma_port netdev
  local state physical_state selector selector_device selector_port selector_exact
  local -a ifaces rails local_cidrs peer_cidrs fields
  local -a hca_selectors
  local -A expected_interfaces selected_interfaces matched_selectors
  local topology_endpoints="rails $TOPOLOGY_RAILS local $TOPOLOGY_LOCAL_CIDRS to peers $TOPOLOGY_PEER_CIDRS"
  command -v rdma >/dev/null || {
    echo "[cluster] rdma command missing for $topology_endpoints; install iproute2 RDMA tooling" >&2
    return 1
  }
  if ! links="$(rdma link show)"; then
    echo "[cluster] cannot read RDMA link state for $topology_endpoints" >&2
    return 1
  fi
  IFS=',' read -ra ifaces <<< "$TOPOLOGY_LOCAL_INTERFACES"
  IFS=',' read -ra rails <<< "$TOPOLOGY_RAILS"
  IFS=',' read -ra local_cidrs <<< "$TOPOLOGY_LOCAL_CIDRS"
  IFS=',' read -ra peer_cidrs <<< "$TOPOLOGY_PEER_CIDRS"
  for iface in "${ifaces[@]}"; do
    expected_interfaces["$iface"]=1
  done
  if [[ -n "$NCCL_IB_HCA" ]]; then
    IFS=',' read -ra hca_selectors <<< "$NCCL_IB_HCA"
    for selector in "${hca_selectors[@]}"; do
      if [[ ! "$selector" =~ ^=?[A-Za-z0-9_.-]+(:[0-9]+)?$ ]]; then
        echo "[cluster] unsupported NCCL_IB_HCA selector '$selector'; use device names with optional :port; configured $topology_endpoints" >&2
        return 1
      fi
    done
  fi
  for index in "${!ifaces[@]}"; do
    iface="${ifaces[$index]}"
    if [[ -z "$NCCL_IB_HCA" ]]; then
      if awk -v iface="$iface" '
        {active=0; linked=0; physical=0;
         for(i=1;i<NF;i++) {
           if($i=="state" && $(i+1)=="ACTIVE") active=1;
           if($i=="physical_state" && $(i+1)=="LINK_UP") physical=1;
           if($i=="netdev" && $(i+1)==iface) linked=1;
         }
         if(active && physical && linked) found=1}
        END {exit !found}' <<< "$links"; then
        continue
      fi
      echo "[cluster] rail ${rails[$index]} local $iface ${local_cidrs[$index]} to peer ${peer_cidrs[$index]} has no ACTIVE/LINK_UP RDMA port" >&2
      return 1
    fi
  done
  if [[ -n "$NCCL_IB_HCA" ]]; then
    while IFS= read -r line; do
      read -r -a fields <<< "$line"
      (( ${#fields[@]} >= 2 )) || continue
      [[ "${fields[0]}" == "link" ]] || continue
      link_name="${fields[1]}"
      [[ "$link_name" == */* ]] || continue
      rdma_device="${link_name%/*}"
      rdma_port="${link_name##*/}"
      netdev=""
      state=""
      physical_state=""
      for ((index = 2; index + 1 < ${#fields[@]}; index += 2)); do
        case "${fields[index]}" in
          netdev) netdev="${fields[index + 1]}" ;;
          state) state="${fields[index + 1]}" ;;
          physical_state) physical_state="${fields[index + 1]}" ;;
        esac
      done
      for selector in "${hca_selectors[@]}"; do
        selector_exact=0
        [[ "$selector" == =* ]] && selector_exact=1
        selector="${selector#=}"
        selector_device="${selector%%:*}"
        selector_port=""
        [[ "$selector" == *:* ]] && selector_port="${selector##*:}"
        if (( selector_exact )); then
          [[ "$rdma_device" == "$selector_device" ]] || continue
        else
          [[ "$rdma_device" == "$selector_device"* ]] || continue
        fi
        [[ -z "$selector_port" || "$rdma_port" == "$selector_port" ]] || continue
        matched_selectors["$selector"]=1
        if [[ -z "$netdev" ]]; then
          echo "[cluster] NCCL_IB_HCA selector '$selector' maps $rdma_device/$rdma_port without a netdev; configured rails $TOPOLOGY_RAILS map $TOPOLOGY_LOCAL_CIDRS to peers $TOPOLOGY_PEER_CIDRS" >&2
          return 1
        fi
        if [[ -z "${expected_interfaces[$netdev]+x}" ]]; then
          echo "[cluster] NCCL_IB_HCA selector '$selector' maps $rdma_device/$rdma_port to unconfigured netdev '$netdev'; configured rails $TOPOLOGY_RAILS map $TOPOLOGY_LOCAL_CIDRS to peers $TOPOLOGY_PEER_CIDRS" >&2
          return 1
        fi
        if [[ "$state" != "ACTIVE" || "$physical_state" != "LINK_UP" ]]; then
          for index in "${!ifaces[@]}"; do
            if [[ "${ifaces[$index]}" == "$netdev" ]]; then
              echo "[cluster] rail ${rails[$index]} local $netdev ${local_cidrs[$index]} to peer ${peer_cidrs[$index]} maps to NCCL_IB_HCA device $rdma_device/$rdma_port without ACTIVE/LINK_UP state" >&2
              return 1
            fi
          done
        fi
        selected_interfaces["$netdev"]=1
      done
    done <<< "$links"
    for selector in "${hca_selectors[@]}"; do
      selector="${selector#=}"
      if [[ -z "${matched_selectors[$selector]+x}" ]]; then
        echo "[cluster] NCCL_IB_HCA selector '$selector' has no RDMA port mapping; configured rails $TOPOLOGY_RAILS map $TOPOLOGY_LOCAL_CIDRS to peers $TOPOLOGY_PEER_CIDRS" >&2
        return 1
      fi
    done
    for index in "${!ifaces[@]}"; do
      iface="${ifaces[$index]}"
      if [[ -z "${selected_interfaces[$iface]+x}" ]]; then
        echo "[cluster] rail ${rails[$index]} local $iface ${local_cidrs[$index]} to peer ${peer_cidrs[$index]} is not covered by NCCL_IB_HCA='$NCCL_IB_HCA'" >&2
        return 1
      fi
    done
  fi
}

validate_cluster_runtime() {
  local runtime_facts runtime_sha256
  if ! runtime_facts="$(docker exec -i "$VLLM_CONTAINER" python3 - \
      "$HEAD_IP" "$WORKER_IP" "$VLLM_IMAGE" "$RAY_VERSION" \
      "$TOPOLOGY_SHA256" "$HEAD_NODE_ID" "$WORKER_NODE_ID" \
      "$HEAD_CX_IFACE" "$WORKER_CX_IFACE" \
      < "$CONNECTX_SCRIPT_DIR/cluster_runtime_fingerprint.py")"; then
    echo "[cluster] per-node Ray runtime or topology validation failed" >&2
    return 1
  fi
  runtime_sha256="$(printf '%s' "$runtime_facts" | sha256sum | awk '{print $1}')"
  echo "[cluster] Ray runtime verified for topology $TOPOLOGY_SHA256 ($runtime_sha256)"
}

validate_local_ray_container() {
  docker exec -i "$VLLM_CONTAINER" python3 - \
      "$TOPOLOGY_SHA256" "$LOCAL_NODE_ID" "$LOCAL_IP" "$CX_IFACE" \
      "$NCCL_IB_DISABLE" "$NCCL_IB_HCA" "$NCCL_IB_GID_INDEX" \
      "$NCCL_NET_PLUGIN" <<'PY'
import os
import sys

(
    expected_topology_sha256,
    expected_node_id,
    expected_ip,
    expected_interfaces,
    expected_ib_disable,
    expected_ib_hca,
    expected_gid_index,
    expected_net_plugin,
) = sys.argv[1:]
expected_values = {
    "TOPOLOGY_SHA256": expected_topology_sha256,
    "LOCAL_NODE_ID": expected_node_id,
    "VLLM_HOST_IP": expected_ip,
    "NCCL_SOCKET_IFNAME": expected_interfaces,
    "GLOO_SOCKET_IFNAME": expected_interfaces,
    "NCCL_IB_DISABLE": expected_ib_disable,
    "NCCL_IB_HCA": expected_ib_hca,
    "NCCL_IB_GID_INDEX": expected_gid_index,
    "NCCL_NET_PLUGIN": expected_net_plugin,
}
mismatches = [
    f"{name}={os.environ.get(name, '')!r}, expected {expected!r}"
    for name, expected in expected_values.items()
    if os.environ.get(name, "") != expected
]
if mismatches:
    raise SystemExit(
        "[cluster] local Ray container topology/runtime mismatch: "
        + "; ".join(mismatches)
    )
print(
    f"[cluster] local Ray container matches node {expected_node_id} "
    f"at {expected_ip} for topology {expected_topology_sha256}"
)
PY
}

# One physical head GPU and its local cache are shared by every campaign,
# regardless of output directory or serving-container name. Every repository
# command that changes or measures these resources takes this same lock.
_resource_lock_id="$(hostname -s 2>/dev/null || printf 'unknown-host')"
export CAMPAIGN_LOCK_FILE="${CAMPAIGN_LOCK_FILE:-${RESOURCE_LOCK_FILE:-/tmp/dgx-spark-resources_${_resource_lock_id}.lock}}"
export RESOURCE_LOCK_FILE="$CAMPAIGN_LOCK_FILE"
export RESOURCE_LOCK_WAIT_SECONDS="${RESOURCE_LOCK_WAIT_SECONDS:-3600}"
export RESOURCE_WAIT_TIMEOUT_SECONDS="${RESOURCE_WAIT_TIMEOUT_SECONDS:-1800}"
export RESOURCE_WAIT_INTERVAL_SECONDS="${RESOURCE_WAIT_INTERVAL_SECONDS:-5}"
unset _resource_lock_id

acquire_resource_lock() {
  if [[ "${CAMPAIGN_LOCK_HELD:-0}" == "1" && -n "${CAMPAIGN_LOCK_FD:-}" \
        && -e "/proc/$$/fd/$CAMPAIGN_LOCK_FD" ]]; then
    local inherited_path expected_path
    inherited_path="$(readlink "/proc/$$/fd/$CAMPAIGN_LOCK_FD" 2>/dev/null || true)"
    expected_path="$(readlink -f "$CAMPAIGN_LOCK_FILE" 2>/dev/null || true)"
    if [[ "$inherited_path" == "$expected_path" ]] \
        && flock -n "$CAMPAIGN_LOCK_FD"; then
      export RESOURCE_LOCK_INHERITED=1
      return 0
    fi
  fi
  unset CAMPAIGN_LOCK_HELD CAMPAIGN_LOCK_FD

  if [[ ! "$RESOURCE_LOCK_WAIT_SECONDS" =~ ^[0-9]+$ ]]; then
    echo "[resources] RESOURCE_LOCK_WAIT_SECONDS must be a non-negative integer" >&2
    return 2
  fi
  # flock(2) locks an inode through any descriptor and never needs write
  # permission, so open an EXISTING lock file read-only. That matters on a
  # shared head node: /tmp is sticky and world-writable, and under
  # fs.protected_regular=2 the kernel refuses the O_CREAT open that `>` always
  # performs whenever the file belongs to another user -- whatever its mode is,
  # so chmod cannot fix it. Only creating the file for the first time needs
  # write access, and that is a plain create in a directory we can write.
  # The open is also checked rather than redirected: `exec` without a command
  # applies its redirections to the shell permanently, so a `2>/dev/null` here
  # would survive the success path and silently discard every later error.
  if [[ -e "$CAMPAIGN_LOCK_FILE" ]]; then
    if ! exec {CAMPAIGN_LOCK_FD}<"$CAMPAIGN_LOCK_FILE"; then
      echo "[resources] resource lock exists but is not readable:" >&2
      ls -l "$CAMPAIGN_LOCK_FILE" >&2 2>/dev/null || true
      echo "[resources] make it readable (chmod a+r) so every user on this host" \
           "can take the same lock." >&2
      return 1
    fi
  else
    # Create a read-only lock inode so another account can open it to take the
    # same flock. Noclobber makes simultaneous first use safe.
    if ! (umask 0222; set -o noclobber; : >"$CAMPAIGN_LOCK_FILE") 2>/dev/null \
        && [[ ! -e "$CAMPAIGN_LOCK_FILE" ]]; then
      echo "[resources] cannot create the resource lock $CAMPAIGN_LOCK_FILE" >&2
      echo "[resources] its directory must be writable by this user" >&2
      return 1
    fi
    if ! exec {CAMPAIGN_LOCK_FD}<"$CAMPAIGN_LOCK_FILE"; then
      echo "[resources] cannot open the resource lock $CAMPAIGN_LOCK_FILE" >&2
      return 1
    fi
  fi
  if ! flock -n "$CAMPAIGN_LOCK_FD"; then
    echo "[resources] another repository operation holds $CAMPAIGN_LOCK_FILE; waiting up to ${RESOURCE_LOCK_WAIT_SECONDS}s" >&2
    if ! flock -w "$RESOURCE_LOCK_WAIT_SECONDS" "$CAMPAIGN_LOCK_FD"; then
      echo "[resources] timed out waiting for $CAMPAIGN_LOCK_FILE" >&2
      return 1
    fi
  fi
  export CAMPAIGN_LOCK_FD
  export CAMPAIGN_LOCK_HELD=1
  export RESOURCE_LOCK_INHERITED=0
}

# Existing callers use this name; it now takes the shared resource lock.
acquire_campaign_lock() { acquire_resource_lock; }

resource_check_once() {
  local worker_scope="${1:-auto}" mode="${2:-all}"
  local local_output="" worker_output="" local_status=0 worker_status=0
  local container_running=0 check_worker=0 ray_status=""
  local manager="$CONNECTX_SCRIPT_DIR/resource_manager.py"
  local python="${RESOURCE_MANAGER_PYTHON:-python3}"

  case "$worker_scope" in auto|required|none) ;; *)
    echo "[resources] invalid worker scope '$worker_scope'" >&2; return 2 ;; esac
  if [[ "$SINGLE_NODE_ONLY" == "1" ]]; then
    case "$worker_scope" in
      required)
        echo "[resources] worker scope required is invalid with SINGLE_NODE_ONLY=1" >&2
        return 2
        ;;
      auto) worker_scope=none ;;
    esac
  fi
  case "$mode" in all|serving) ;; *)
    echo "[resources] invalid resource-check mode '$mode'" >&2; return 2 ;; esac

  if local_output="$("$python" "$manager" --mode "$mode" 2>&1)"; then
    local_status=0
  else
    local_status=$?
  fi
  if (( local_status > 1 )); then
    printf '%s\n' "$local_output" >&2
    return 2
  fi

  if [[ "$worker_scope" != "none" ]]; then
    if docker inspect -f '{{.State.Running}}' "$VLLM_CONTAINER" 2>/dev/null \
        | grep -q '^true$'; then
      container_running=1
    fi
  fi
  if [[ "$worker_scope" == "required" ]]; then
    check_worker=1
  elif [[ "$worker_scope" == "auto" && "$container_running" == "1" ]]; then
    ray_status="$(docker exec "$VLLM_CONTAINER" ray status 2>/dev/null || true)"
    [[ "$ray_status" == *"Active:"* ]] && check_worker=1
  fi
  if [[ "$check_worker" == "1" ]]; then
    if [[ "$container_running" != "1" ]]; then
      echo "[resources] UNKNOWN: required Ray container $VLLM_CONTAINER is not running" >&2
      return 2
    fi
    local -a worker_check_args=(--mode "$mode" --worker --head-ip "$HEAD_IP" --worker-ip "$WORKER_IP")
    [[ "$worker_scope" == "auto" ]] && worker_check_args+=(--allow-missing-worker)
    if worker_output="$(docker exec -i "$VLLM_CONTAINER" python3 - "${worker_check_args[@]}" \
        < "$manager" 2>&1)"; then
      worker_status=0
    else
      worker_status=$?
    fi
    if (( worker_status > 1 )) || \
        { (( worker_status == 1 )) && [[ "$worker_output" != *"[resources] BUSY"* ]]; }; then
      printf '%s\n' "$worker_output" >&2
      return 2
    fi
  fi

  [[ -n "$local_output" ]] && printf '%s\n' "$local_output"
  [[ -n "$worker_output" ]] && printf '%s\n' "$worker_output"
  if (( local_status == 1 || worker_status == 1 )); then
    return 1
  fi
}

wait_for_resources_idle() {
  local worker_scope="${1:-auto}" mode="${2:-all}"
  local elapsed=0 status=0 output=""
  if [[ ! "$RESOURCE_WAIT_TIMEOUT_SECONDS" =~ ^[0-9]+$ \
        || ! "$RESOURCE_WAIT_INTERVAL_SECONDS" =~ ^[1-9][0-9]*$ ]]; then
    echo "[resources] wait timeout must be non-negative and interval must be positive" >&2
    return 2
  fi
  while :; do
    if output="$(resource_check_once "$worker_scope" "$mode" 2>&1)"; then
      echo "$output"
      return 0
    else
      status=$?
    fi
    if (( status != 1 )); then
      [[ -n "$output" ]] && printf '%s\n' "$output" >&2
      echo "[resources] availability could not be confirmed; refusing to proceed" >&2
      return 2
    fi
    if (( elapsed >= RESOURCE_WAIT_TIMEOUT_SECONDS )); then
      printf '%s\n' "$output" >&2
      echo "[resources] timed out after ${elapsed}s waiting for idle resources" >&2
      return 1
    fi
    echo "[resources] resources are occupied; waiting (${elapsed}/${RESOURCE_WAIT_TIMEOUT_SECONDS}s)" >&2
    [[ -n "$output" ]] && printf '%s\n' "$output" >&2
    sleep "$RESOURCE_WAIT_INTERVAL_SECONDS"
    elapsed=$((elapsed + RESOURCE_WAIT_INTERVAL_SECONDS))
  done
}

