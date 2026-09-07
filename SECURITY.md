# Shop-box security and trust model

This agent connects one Procrastination Station shop box to the sovereign pstation app. It is not a multi-tenant cloud and makes no SOC 2/HITRUST certification claim. The app uses Auth.js and Postgres with app-layer member ownership, not Supabase Vault or RLS. Read the app's AGENTS.md for its isolation contract.

## Boundaries actually implemented

- The app resolves a hub from its bearer, checks hub ownership on printer/job operations, and checks the member's print eligibility at submission. Revoking the hub rejects its next HTTP request. A running physical print is not a remotely revocable permission grant.
- Enrollment exchanges a short-lived one-use token for a per-hub bearer. The app stores the hash; the box stores the bare credential in `/var/lib/makeros-hub/credential`, private mode 0600. Empty credentials are refused; replacement uses a private temporary file and atomic rename. Never share config-down responses, state backups or member setup downloads.
- Cloud URLs must be HTTPS origins (HTTP is allowed only for localhost tests). All four credentialed HTTP transports refuse redirects. Parsed acknowledgements, not just HTTP 200, release report/submission buffers. Invalid printer-list responses preserve the current physical adapters. Virtual-printer authorization still fails closed when no valid members remain.
- The app encrypts physical printer access codes with AES-256-GCM under a key derived from its AUTH_SECRET. Config-down delivers them only to the owning hub. The box necessarily has printer credentials and member code hashes in memory; possession of that process is a compromise of the shop's printer access.
- New member codes from pstation use ten random characters, the current OrcaSlicer input limit. Earlier eight-character hashes remain valid until explicitly revoked. Online attempts are limited, but hash disclosure permits offline guessing; code hashes are sensitive, not public identifiers.
- MQTT reports contain the destination session's own transfer state and filename. FTPS upload notifications select that member's sessions. Tests drive two members and inspect both packets. FTPS does not implement RETR; LIST is empty and SIZE refuses. Unique spool filenames and `(member_id, filename)` capture pairing prevent one member's same-named upload from replacing another's.
- Passive FTPS binds to the virtual printer's configured address, and accepts a data connection only from the known control peer. Unknown control peers are refused.

## Root access and updates

The service runs as `makeros-hub`. The installed code, venv and three sudo helpers are root-owned. Sudo permits only `update.sh`, `tailscale-setup.sh`, and `vp-network.py`; it no longer permits arbitrary `ip` or `arping` argument patterns. These are script entrypoints, not a claim that the whole service is sandboxed.

`update.sh` fixes the canonical sovereign repository in root-owned code, requires a release tag and exact commit, rejects same-version/downgrade calls at the root boundary, checks the cloned package version against its tag, and serializes updates. Service-writable `repo` configuration cannot select executable root code. A manual operator rollback is deliberately separate from service-triggered OTA. Invalid/unwritable cooldown state refuses an update rather than causing a restart loop.

`install.sh` stages code and helpers before stopping an active service. Copy/swap/start failures restore their previous versions and attempt restart. The old files remain under `/opt/makeros-hub/previous`. Required printer dependencies must import successfully. **This is not a complete dependency rollback:** pip changes to the venv happen earlier; take a full image/venv backup for operational rollback.

`bootstrap.sh` also requires a reviewed tag and commit before executing the clone's installer. The bootstrap script itself must come from the operator's reviewed checkout, not an unauthenticated mutable pipe. The sovereign publisher and operator's release selection remain trusted. A SHA supplied by the app/service is not an independent publisher signature, and a compromised authorized release is not safe. Signed release attestation needs an operator-provisioned verification key before it can provide a stronger boundary.

`tailscale-setup.sh` only operates an installed client, validates hostname/key inputs, and passes the temporary private key file to Tailscale. It no longer downloads and executes a mutable root installation script. Provision Tailscale once from its official signed OS repository as an operator. The helper does not grant an exit route or SSH access.

`vp-network.py` validates exactly add/del/probe, individual IPv4 `/32` aliases and fixed probe flags. LAN addresses must belong to the primary interface's subnet and cannot be its own address, gateway, broadcast or network. Loopback aliases are limited to `198.18.0.0/15`. Failed ARP probes mean unavailable, never free. This does not reserve DHCP addresses or implement a firewall; the operator must reserve the alias range.

## File intake and durability

Manual HTTP intake has one buffered upload at a time, at most four connection threads, a 64 MiB effective cap, a read deadline and a free-space floor. Multipart parsing respects delimiter lines and preserves binary content. A rejection returns an error, not an apparent successful upload. An unknown cloud outcome retains the spool file and tells the member to check the queue before resending. Staff must resolve these files; there is no automatic speculative resubmission.

FTPS streams uploads. File and directory flushes precede success acknowledgements. The durable submission outbox and dispatch/progress state survive process restarts; failed flushes are not reported as success. The app holds legacy manual jobs without a printer model for staff, and auto-dispatch requires matching model, readable applicable filament requirements and the plate safety gates. The box rechecks printer/tray state and compares the actual sliced model with the physical printer before uploading. Missing or mismatched model metadata refuses the start. This is an accidental mismatch guard, not a full validator of arbitrary malicious G-code. Existing running/assigned jobs are not silently moved to a new printer by the audit.

## Required operational boundaries

The software does **not** prove that the installed router, host firewall or Headscale ACL matches this design. Virtual-printer ports and manual HTTP must be reachable only on the member/shop networks, management only on the operator network, and physical printers isolated from member clients. Do not expose these listeners publicly. HTTP print-host tokens are plaintext on their local hop unless carried through the encrypted network; prefer the website or VP TLS path.

Physical Bambu links currently use self-signed TLS without verifying the server certificate. This encrypts traffic but does not authenticate the printer against an active LAN attacker. A segregated printer LAN is therefore a required current trust boundary; certificate enrollment/pinning would need observed printer certificates and an operator-controlled replacement workflow. None was invented from unavailable hardware.

Systemd uses a non-login account, private temporary storage, ProtectHome and memory limits. NoNewPrivileges/ProtectSystem are deliberately absent because the root maintenance helpers need elevation/writes. These tradeoffs, installed ownership, controller ACLs, disk/power-loss recovery and a real print must be checked on the box before production acceptance. Never infer physical readiness from a green heartbeat alone.
