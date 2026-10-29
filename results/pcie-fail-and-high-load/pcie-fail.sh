#!/usr/bin/env bash

set -eou pipefail

sudo journalctl --list-boots --no-pager | tail -8
sudo journalctl -b -1 --no-pager -o short-iso-precise | tail -160
sudo journalctl -b -1 -k --no-pager -o short-iso-precise | tail -120
