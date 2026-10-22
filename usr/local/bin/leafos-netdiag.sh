#!/bin/bash
export LC_ALL=C LANG=C
exec > /dev/console 2>&1
sleep 8
echo ""
echo "=============== LeafOS STATUS ==============="
echo "--- interfaces ---"; ip -br a 2>&1
echo "--- listening ---"; ss -lntp 2>&1 | head -14
echo "--- ssh unit ---"; systemctl is-active leafos-ssh 2>&1
journalctl -u leafos-ssh -b --no-pager 2>&1 | tail -8
echo "--- failed units ---"; systemctl --failed --no-pager --no-legend 2>&1 | head -6
echo "--- firmware ---"; dmesg 2>/dev/null | grep -iE 'rtw|firmware|regdb' | tail -6
echo "=============== END ==============="
