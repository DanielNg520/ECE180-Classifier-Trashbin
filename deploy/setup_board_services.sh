#!/bin/bash
# One-shot setup for the UNO Q: installs two systemd services so the board is
# fully autonomous after this — no Mac cable needed again.
#
#   trashbin-camera.service  — runs camera_loop.py on boot (auto-detects the
#                              webcam, posts results to the dashboard)
#   trashbin-tunnel.service  — holds a reverse SSH tunnel open to the droplet;
#                              from anywhere: ssh to the droplet, then
#                              `ssh -p 2222 arduino@localhost` to reach the board
#
# Run ON THE BOARD as the arduino user:
#   bash setup_board_services.sh <droplet_ssh e.g. root@1.2.3.4> <webapp_url e.g. https://trashbin.example.com>
set -euo pipefail

DROPLET_SSH="${1:?usage: setup_board_services.sh <user@droplet> <webapp_url>}"
WEBAPP_URL="${2:?usage: setup_board_services.sh <user@droplet> <webapp_url>}"
HOME_DIR="$HOME"
USER_NAME="$(whoami)"

# --- SSH key for the tunnel (no passphrase: a service can't type one) -------
if [ ! -f "$HOME_DIR/.ssh/id_ed25519" ]; then
  ssh-keygen -t ed25519 -N "" -f "$HOME_DIR/.ssh/id_ed25519" -C "uno-q-trashbin"
fi
echo
echo "=== ADD THIS PUBLIC KEY to the droplet's ~/.ssh/authorized_keys: ==="
cat "$HOME_DIR/.ssh/id_ed25519.pub"
echo "====================================================================="
echo

# --- Camera/classifier service ----------------------------------------------
sudo tee /etc/systemd/system/trashbin-camera.service >/dev/null <<EOF
[Unit]
Description=Trashbin camera classification loop
After=network-online.target
Wants=network-online.target

[Service]
User=$USER_NAME
WorkingDirectory=$HOME_DIR/trashbin
Environment=WEBAPP_URL=$WEBAPP_URL
Environment=CONFIDENCE_THRESHOLD=0.8
Environment=TEMPERATURE=0.55
Environment=DEVICE_ID=trashbin-1
ExecStart=$HOME_DIR/trashbin/.venv/bin/python3 camera_loop.py mobilenet_v3_large_waste30_static_int8.tflite labels.txt
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

# --- Reverse SSH tunnel service ---------------------------------------------
# -R 2222:localhost:22  = droplet's localhost:2222 forwards to the board's sshd.
# ServerAlive* keeps NAT mappings fresh; Restart=always redials after drops.
sudo tee /etc/systemd/system/trashbin-tunnel.service >/dev/null <<EOF
[Unit]
Description=Reverse SSH tunnel to droplet
After=network-online.target
Wants=network-online.target

[Service]
User=$USER_NAME
ExecStart=/usr/bin/ssh -N -R 2222:localhost:22 \\
  -o ExitOnForwardFailure=yes \\
  -o ServerAliveInterval=30 -o ServerAliveCountMax=3 \\
  -o StrictHostKeyChecking=accept-new \\
  $DROPLET_SSH
Restart=always
RestartSec=15

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable trashbin-camera trashbin-tunnel
echo "Services installed and enabled. After the droplet has the key, start with:"
echo "  sudo systemctl start trashbin-tunnel trashbin-camera"
echo "Or just reboot with the camera hub attached."
