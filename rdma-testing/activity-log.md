# Activity Log
## Tracking the things I am doing in order to setup and test RDMA

### Update to latest firmware:
- run `fwupdmgr get-upgrades` to query for available firmware
- run `fwupdmgr update <DEVICE (OPTIONAL)>` to update firmware. If you only want to update a certain device, pass in the device name as the optional argument

### Use DGX Dashboard to get newest software
- navigate to `localhost:11000/updates` and "Update"

### Setup users on both machines (same username)
- Add user to sudo group

### Clone Nvidia's guide:
- git clone git@github.com:NVIDIA/dgx-spark-playbooks
- run 
```bash
discover-sparks
```
Now the machines can ssh into eachother without passwords

### Following [NVIDIA Connect Two Sparks](https://github.com/NVIDIA/dgx-spark-playbooks/tree/main/nvidia/connect-two-sparks)

### Netplan Setup
- NOTE: I plugged the single QSFP cable into the leftmost port on both machines. If you plug it into the rightmost port, the you will use the ...1np1 interfaces instead of the 0np0 interfaces

ON DIYA
```bash
sudo tee /etc/netplan/40-cx7.yaml > /dev/null <<EOF
network:
  version: 2
  ethernets:
    enp1s0f0np0:
      addresses:
        - 10.10.10.1/24
      dhcp4: no
    enP2p1s0f0np0:
      addresses:
        - 10.10.11.1/24
      dhcp4: no
EOF

sudo chmod 600 /etc/netplan/40-cx7.yaml
sudo netplan apply
```
ON HOEDUS
```bash
## Create the netplan configuration file
sudo tee /etc/netplan/40-cx7.yaml > /dev/null <<EOF
network:
  version: 2
  ethernets:
    enp1s0f0np0:
      addresses:
        - 10.10.10.2/24
      dhcp4: no
    enP2p1s0f0np0:
      addresses:
        - 10.10.11.2/24
      dhcp4: no
EOF

## Set appropriate permissions
sudo chmod 600 /etc/netplan/40-cx7.yaml

## Apply the configuration
sudo netplan apply
```
