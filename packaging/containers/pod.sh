#!/bin/bash
# This file is part of Xpra.
# Copyright (C) 2025 Antoine Martin <antoine@xpra.org>
# Xpra is released under the terms of the GNU GPL v2, or, at your option, any
# later version. See the file COPYING for details.

set -e

PORT=10000

# when building and configuring the containers,
# SEAMLESS switches between seamless mode (preferred) and desktop mode (slower):
export SEAMLESS=1

# ensure that the containers we need exist:
if ! buildah inspect -t image xvfb &> /dev/null; then
  sh ./xvfb.sh
fi
if ! buildah inspect -t image xpra &> /dev/null; then
  sh ./xpra.sh
fi
if ! buildah inspect -t image apps &> /dev/null; then
  sh ./desktop.sh
fi

# Create public network (standard podman bridge with internet access)
PUBLIC_NET="publicnet"
if ! podman network exists "$PUBLIC_NET"; then
  podman network create "$PUBLIC_NET"
fi

RUN_VOLUME="run"
if ! podman volume exists "$RUN_VOLUME"; then
  # rootless containers can't use ro,nodev,noexec
  # or "--opt device=tmpfs"
  podman volume create "$RUN_VOLUME"
fi

POD_NAME="xpra"
if ! podman pod exists "$POD_NAME"; then
  podman pod create \
    --name ${POD_NAME} \
    --memory 4g \
    --shm-size=1g \
    --uts=private
#    --network "${PUBLIC_NET}" \
#    -p 10000:10000/tcp \
#    -p 10000:10000/udp
#    --infra-image=xvfb --infra-name=xvfb
#    --infra-command=??
#    --share=ipc,net,uts
#    --share=ipc,net,uts,cgroup
fi

# Start xvfb, exposes ipc to other containers for XShm
podman run -dt \
  --pod ${POD_NAME} \
  --replace \
  --name xvfb \
  --hostname xpra \
  --uts private \
  --ipc shareable \
  --cgroupns private \
  --network "$PUBLIC_NET" \
  -p ${PORT}:${PORT}/tcp \
  -p ${PORT}:${PORT}/udp \
  --read-only --read-only-tmpfs=true \
  xvfb

# Start xpra
podman run -dt \
  --pod ${POD_NAME} \
  --replace \
  --name xpra \
  --uts container:xvfb \
  --ipc container:xvfb \
  --cgroupns container:xvfb \
  --network container:xvfb \
  --volume ${RUN_VOLUME}:/run:rw \
  --security-opt label=type:container_runtime_t \
  xpra
#  --read-only --read-only-tmpfs=true \

# Start app container running the desktop environment applications:
podman run -dt \
  --pod ${POD_NAME} \
  --replace \
  --name apps \
  --uts container:xvfb \
  --ipc container:xvfb \
  --cgroupns container:xvfb \
  --network container:xvfb \
  -v /tmp/.X11-unix:/tmp/.X11-unix \
  --security-opt label=type:container_runtime_t \
  -v /dev/dri:/dev/dri \
  --volumes-from xpra:rw \
  apps

# Output status
echo "Containers running:"
podman ps --format "table {{.Names}}\t{{.Status}}\t{{.Networks}}\t{{.Ports}}"

echo
echo "Network info:"
echo "- $PUBLIC_NET:"
podman network inspect "$PUBLIC_NET" | grep -iE '"Name":|"Subnet":|"Gateway":'
echo

sleep 5
xdg-open "http://localhost:${PORT}/"
