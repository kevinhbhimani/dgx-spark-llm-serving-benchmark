# Tracking the steps I take to diagnose the issue

Installing nvidia field diagnosis suite. This is an older version, but reports show that the dependency (DOCA OFED) the newest version requires frequently breaks connectx-7. The only downside is this version of the suite does not contain the stress test. You will need ssh access to the node you wish to test. This will take ~3 hours
```bash
sudo apt install dgx-spark-fieldiag=1.0.9-1
```

requires secure boot to be off
```bash
sudo mokutil --sb-state
```

save any important work first, then:
```bash
sudo init 3
```

You will now need to SSH into the node from another
```bash
ssh user@hostname
```

start the diagnostics
```bash
cd /opt/nvidia/dgx-spark-fieldiag
sudo ./partnerdiag --field
```
