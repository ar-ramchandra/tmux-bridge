# Always-on gateway and sleeping nodes

A small wired Linux host can run the gateway while larger nodes sleep. The gateway must stay awake: its Cloudflare connector, mTLS relay, notification database and wake sender all run there. Keep recovery SSH access independent of a node that might sleep.

## Wake from the browser

Create `wake.json` in the gateway configuration directory, readable only by its service account:

```json
{
  "workstation": {
    "network": "192.168.40.0/24",
    "macs": ["08:bf:b8:16:66:71"]
  }
}
```

Use the actual node name, local subnet and physical adapter MAC. The gateway must be on that broadcast domain. The offline device card then offers **Wake**, including on phones. `POST /api/workstation/wake` sends three magic packets per configured adapter and rate limits requests to one per device every 15 seconds. Browser admission and same-origin checks apply. Clients cannot supply a different destination. A successful request means packets were sent; it does not prove the computer woke. Offline can also mean a network or agent failure.

Enable the device's firmware/OS wake support and test suspend/resume before relying on it. Ethernet is preferable. Wi-Fi requires adapter and driver support for Wake-on-WLAN; cold-boot wake is a separate capability. A gateway on one subnet cannot wake a machine on another subnet without an appropriately secured local wake relay.

## Sleep policy

Sleep pauses every local job, including tmux, agents, containers and scheduled tasks. Tmux sessions normally remain in RAM during suspend, but they do not keep working. A restart does not preserve those processes. This release intentionally does not infer idleness from terminal silence or automatically suspend nodes. Enable automatic sleep only after defining work-aware inhibitors and validating a recovery wake path. Do not remove existing keep-awake services until their workloads are accounted for.

For Macs, enable Wake for network access and test on the actual network. Keep FileVault and normal login security enabled. Do not promise unattended recovery after a full shutdown or power failure without testing the boot/login requirements.

## Moving the gateway

Stage an identical release with a private, consistent database backup and the same node identities, transport CA trust, allowed fingerprints and push key. Use a dedicated service account, loopback listeners and boot-enabled services. Test the staged gateway before moving traffic; retain a rollback copy. Never run independent gateways behind replicas of the same Cloudflare tunnel: their WebSocket routing state is held in memory. Repoint every agent and private UI forward to the new gateway. Verify existing session identities, notification subscriptions and browser authentication after cutover.

Store administrative API credentials separately from the runtime tunnel token. Enrollment and certificate revocation must update the authoritative gateway state after migration; an old configuration copy on a sleeping workstation is not authoritative.
