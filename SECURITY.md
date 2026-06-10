# Security & trust model

This agent runs on a **tenant-controlled box on an untrusted LAN** and connects to the
**shared multi-tenant makeros cloud**. It is built *certifiable* (SOC 2 / HITRUST controls
in from day one), even though certification itself is deferred. This document is the
auditable record of the trust model.

## Tenant isolation (enforced cloud-side)

- Each hub holds a **per-hub bearer credential** bound to exactly one workspace. The cloud
  resolves the workspace **from the DB-attested hub row, never from a request envelope** —
  a hub cannot claim to be another tenant.
- All hub data is **row-level-security scoped** to its workspace. A compromised or malicious
  hub can only ever read/write its **own** tenant's rows; it has no path to another tenant.
- **Immediate revocation:** an admin revokes a hub in the dashboard → its credential hash is
  nulled → the next request 401s. No re-deploy, no manual SQL.

## Credential handling

- The credential is minted **once** at enrollment and shown once. The cloud stores only its
  **SHA-256**; the plaintext lives only on the Pi at `/var/lib/makeros-hub/credential`,
  **mode 0600**, owned by the service user. It is **never logged** (agent or cloud).
- Authentication is **hash-lookup + constant-time hash compare**, never a plaintext `==`.
- The one-time enrollment token is single-use, 15-minute TTL, and likewise stored only as a hash.

## Printer credentials (config-down)

- A Bambu **LAN access code** is a low-stakes secret (it's printed on the printer's own screen
  and is rotatable there). The operator enters it in the cloud admin UI; the cloud stores it
  **encrypted at rest in Supabase Vault** — only its last-4 lands in a regular column.
- The agent **pulls** its printer list, access codes included, from `GET /api/print/hub/config`,
  authenticated with its per-hub bearer over TLS. This is the **only** direction a printer
  secret travels, and only to the one hub that owns the printer.
- On the Pi the access code is held **only in process memory** (the MQTT password) — never
  written to disk, never logged, and **never sent back up** on the heartbeat (which carries
  printer telemetry only). The wire status DTO has no `accessCode`/`serial`/`ip` fields by
  construction.

## Least privilege

- Runs as a dedicated **non-login system user** (`makeros-hub`), not root.
- **Outbound HTTPS only** — no inbound ports opened on the shop LAN.
- systemd hardening: `NoNewPrivileges`, `ProtectSystem=full`, `ProtectHome`, `PrivateTmp`,
  a scoped `StateDirectory`.
- **No secrets in this repository.**

## Supply-chain integrity

- **Install from a pinned, versioned tag** (`git clone --branch vX.Y.Z`), **never** a
  `curl | sudo bash` of mutable `main`. Pinning a reviewed release is the control that keeps
  a repo compromise from reaching tenant devices.
- The repository is **public** for transparency — tenants and auditors can review exactly what
  runs on their hardware and talks to the shared cloud.
- **Roadmap (before second-tenant onboarding):** signed release artifacts (minisign/Sigstore)
  + checksum verification in the installer, and a `--verify` step in the bootstrap one-liner.
  Until then, install only from a tag you (or your operator) have reviewed.

## OrcaSlicer ingest server (the one inbound listener)

The agent runs an OctoPrint-compatible HTTP server on the LAN (default `:8787`) so members'
OrcaSlicer can "Send" to the hub. Trust model:

- **Bound to the shop LAN only**, a non-privileged port; the hub→cloud link stays
  outbound-HTTPS. There is no inbound path from the internet (Phase 2's relay/NetBird adds
  encrypted remote send).
- **Every upload is authenticated by the member's print token** (OrcaSlicer's `X-Api-Key`),
  which the hub does **not** validate itself — it forwards the token to the cloud, which
  resolves it to a member + runs the eligibility gate. A bad/revoked token is rejected
  (`403`); the hub stores nothing attributable without a valid member.
- **Uploads are inert data**: the sliced file is written to a hub-local spool dir and
  forwarded; it is never executed, and the filename is sanitized before it touches the
  filesystem. Upload size is capped (default 256MB) to prevent a runaway upload exhausting
  the Pi.
- The member token rides in cleartext over the trusted LAN (like the Bambu access codes) —
  acceptable for on-LAN send; remote send (Phase 2) is the encrypted path.

## Over-the-air self-update

The agent can update itself when the cloud (in the heartbeat response) names a newer release —
the one remote-code-execution path in the system, so it is deliberately narrow:

- **Release tags only.** The agent acts only on a well-formed `vX.Y.Z` tag, validated by regex
  on both the agent and the root script. The cloud cannot point it at a branch, a commit,
  `main`, or any non-release ref — only a real tagged release of the one hardcoded repo URL.
- **Forward-only.** Never downgrades; a cooldown stops an update loop on a broken target.
- **Narrow sudoers.** The non-root `makeros-hub` user may run **only** `/opt/makeros-hub/update.sh`
  as root (an `/etc/sudoers.d/makeros-hub` drop-in, validated with `visudo` at install). The
  script is root-owned and not writable by the service user, so it can't be swapped. The update
  runs in an independent systemd transient unit so the service restart can't kill it mid-flight.
- **Operator-controlled.** Auto-update is **off by default**, per hub; the admin opts in (or
  triggers a one-shot "Update now") from the dashboard.
- **Roadmap:** signed release artifacts (minisign/Sigstore) + signature verification in the
  update path, so even a compromised repo can't push code to tenant hardware. Until then the
  control is pinned reviewed releases + the narrow sudoers surface above.

## Reporting

Report security issues privately to `security@overengineeredsolutions.org`. Do not open a
public issue for a vulnerability.
