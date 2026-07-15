# Instance lifecycle — verbs, states, jobs

Enumerated **before** the state machine was coded: retrofitting verbs (especially
resize) into a create/destroy-only state machine is painful. Every verb below gets a
job type and a state path on day one, even if the handler is a stub.

## States

| State | Meaning |
|---|---|
| `provisioning` | create job running (clone → cloud-init → ipfilter → boot) |
| `running` | up per Proxmox |
| `stopping` / `stopped` | graceful stop requested / done |
| `starting` | boot requested on a stopped instance |
| `rebooting` | reboot in flight (returns to `running`) |
| `rebuilding` | disk wiped, re-imaged from template (returns to `running`) |
| `resizing` | plan change in flight (stop → set cores/mem/disk-grow → start) |
| `suspended` | admin/billing hold — powered off, disk kept, user actions blocked |
| `destroying` / `destroyed` | teardown in flight / terminal. IP + vmid released, row kept for audit |
| `error` | a job exhausted retries — human/reconciler attention. Allowed exits: retry verb, destroy |

## Verbs → job types

| Verb | Job | From states | Through | To |
|---|---|---|---|---|
| create | `instance.create` | — (new row) | `provisioning` | `running` |
| start | `instance.start` | `stopped` | `starting` | `running` |
| stop | `instance.stop` | `running` | `stopping` | `stopped` |
| reboot | `instance.reboot` | `running` | `rebooting` | `running` |
| rebuild | `instance.rebuild` | `running`, `stopped`, `error` | `rebuilding` | `running` |
| resize | `instance.resize` | `running`, `stopped` | `resizing` | prior state |
| destroy | `instance.destroy` | any non-terminal | `destroying` | `destroyed` |
| suspend | `instance.suspend` | `running`, `stopped` | — | `suspended` (admin/billing only) |
| unsuspend | `instance.unsuspend` | `suspended` | — | `stopped` |
| snapshot | `instance.snapshot` | `running`, `stopped` | (no state change) | — (PBS-backed) |

Disk-grow is part of `instance.resize` (grow-only; shrink is never offered).

## Job rules

- Jobs are **idempotent**: every step checks current Proxmox state before acting, so a
  retried job resumes rather than double-applies.
- Retry with exponential backoff; after `max_attempts` the job goes `dead` and the
  instance goes `error` with the failure recorded on the row + an audit event.
- One in-flight mutating job per instance (enforced at enqueue).
- Failure states are explicit: `provision-timeout`, `rebuild-failed`, and
  `orphaned-in-proxmox` (reconciler finding) all land in `error`, never limbo.

## Reconciler policy

- **Auto-repair (safe):** DB says `running`, Proxmox says stopped (or vice versa) with
  no in-flight job → fix DB status + audit event.
- **Flag only (never auto-repair):** VM exists in the tenant pool with no DB row
  (orphan — never auto-delete), DB row with no VM, any drift that affects billing
  (plan specs vs actual cores/mem), any VM whose NIC is not on the tenant bridge or
  whose ipfilter is missing (security drift → alert immediately).
