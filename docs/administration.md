# Administration and recovery

Use an account authorized for the target installation. Administrative commands mutate the database
selected by `DATABASE_URL`; verify the target before editing or deleting data. Run local examples
from the **Server repository root**, and keep local logs/test data in ignored `build/`.

## Account and data console

On the installed host, from the installation root:

```bash
bash deploy/manage-server.sh help
bash deploy/manage-server.sh users list
```

[manage-server.sh](../deploy/manage-server.sh) runs `python -m app.admin_cli` inside the Compose API
container using that installation's `.env`. It requires Docker access as the invoking account; use
sudo when required. With no arguments it starts the interactive `lem-admin>` console. Use `help` for
examples and `exit` to leave.

For a local development database, activate the `.venv/` described in
[Deployment](deployment.md#local-development), keep the same `DATABASE_URL`, then run from the
Server repository root:

```powershell
$env:PYTHONPATH = (Resolve-Path server).Path
python -m app.admin_cli help
```

Do not use the bootstrap module merely to change an existing account.

| Commands | Purpose |
| --- | --- |
| `users list [--include-deleted]`, `users show EMAIL_OR_ID` | Inspect accounts |
| `users create --email EMAIL --name "Name" --role tech` | Create a technician; `admin` is the other active role |
| `users update EMAIL_OR_ID --name "Name" --email NEW_EMAIL --role tech` | Change selected account properties |
| `users assign EMAIL_OR_ID CONTRACT_ID`, `users unassign EMAIL_OR_ID CONTRACT_ID` | Manage contract access |
| `users password EMAIL_OR_ID` | Prompt for a replacement password and revoke sessions |
| `users password-hash EMAIL_OR_ID` | Show the stored one-way hash, not the original password |
| `users delete EMAIL_OR_ID` | Remove login identity while retaining required historical references |
| `contracts list [--deleted]`, `contracts trash ID`, `contracts restore ID`, `contracts purge ID` | Inspect, retain, restore or permanently purge a contract |
| `reports list [--contract ID] [--unowned]`, `reports show ID` | Inspect reports and owners |
| `reports owner REPORT_ID EMAIL_OR_ID [--assign-access]` | Transfer ownership, optionally assigning needed access |
| `reports delete ID` | Permanently delete a report and its evidence |
| `chargers list CONTRACT_ID` | Inspect contract membership/station data |
| `chargers add CONTRACT_ID --external-id ID --set model=MODEL` | Add a membership/station |
| `chargers update CONTRACT_ID CHARGER_UUID --details-json FILE` | Merge a JSON patch into charger details |

Account creation supports `--if-missing`. Creation/password commands accept `--generate`; printing
generated credentials requires `--show-password`. Account/role changes protect the last active
administrator. Destructive commands have confirmation prompts; `APAGAR` is not a preview mode. Do
not automate a confirmation merely to make an operation unattended.

[server/manage.py](../server/manage.py) is a smaller local bootstrap/recovery CLI:

```powershell
python server/manage.py create-admin --email admin@example.test
python server/manage.py reset-password --email admin@example.test
```

It prompts twice for a password and commits directly to the selected database. It does not recover
old plaintext passwords. Prefer the full administrator console for routine account/data management.

## Excel import and physical station identity

Import is analysis → mapping/preview → commit. Preview does not apply catalogue changes. A commit is
atomic and includes a retry/batch identity. Keep source identifiers as text so leading zeroes in
serial numbers and similar fields survive.

The built-in `lem-stations-v1` profile recognizes the **Station ID** layout. Required columns
include Station ID, Morada, CCDR, Fornecimento de Energia, Nº(s) Série, Fabricante, Modelo, Potência
(kVA), Limitação de Potência (kVA), and Tomadas / Fichas. Código Postal, Latitude, Longitude and CPE
Associado are optional in that profile. The exact field/header mapping is maintained in
[import_profiles.py](../server/app/import_profiles.py), not in a separate versioned document.

Generic mappings and saved company profiles remain supported. The parser validates workbook
structure, required cells, duplicates, coordinate/scalar forms and connector configuration before
committing. Reusing a verified station must not let a stale workbook overwrite fields already
confirmed by a technician.

Physical `Station` identity is shared across contracts; a `Charger` UUID is the contract membership.
Closing a report can confirm manufacturer, model and serial via EST.02–EST.04. Concurrent
inconsistent corrections return a conflict. Historical snapshots are preserved. Rename through the
console's `--external-id`, not a shadow `external_id` inside `details`.

## Ownership, trash and deletion

Ownership transfer records history and checks revision/access. Removing a contract assignment does
not silently grant another technician ownership. Protect pending work on devices before making
administrative changes that remove their access.

Contract trash retains dependent records for **30 days** unless restored or explicitly purged. A
lifespan sweep checks expiry approximately every minute. Purging is irreversible and differs from
hiding/trashing. Report deletion is permanent and records a tombstone; a client receiving 410
discards the corresponding deleted local report rather than restoring it from cache.

## Backup and restore — on the server

From the installation root:

```bash
bash deploy/backup.sh
```

The script writes a private timestamped PostgreSQL custom-format dump under `deploy/backups/` using
`pg_dump`. Report evidence is in the database and is included. `.env` and HTTPS certificate volumes
are **not** part of that dump; preserve configuration/certificates through their own controlled
backup process. Keep an off-host copy if the backup must survive host loss.

Restoration is an operator-controlled procedure, not a feature of `backup.sh`. Stop API writes,
identify the intended database/volume, preserve the current state, restore the selected custom dump
with PostgreSQL `pg_restore`, then start the API and verify catalogue, reports, evidence and login.
Do not generate a fresh database password against an existing initialized volume. No restore of a
production database was performed as part of this documentation update.

## Stop, uninstall or remove a prototype

The maintained host-side removal procedure is [deploy/UNINSTALL.md](../deploy/UNINSTALL.md). It
covers preview, `--stop`, `--purge`, backup handling, confirmation and missing-configuration
recovery. `--stop` keeps files/data/certificates; `--purge` removes the selected installation and
owned data, while retaining a private sibling backup of configuration/prior backups. Removal is not
transactional.

[remove-prototype.py](../deploy/remove-prototype.py) is a separate first-install cleanup tool. Run
it from the new Server checkout/package root with `--installation PATH` identifying the old
prototype. The new checkout must not be inside the old installation being removed. Its default is a
plan; only `--delete` performs irreversible removal. It is not an update mechanism and does not
replace preserving a working installation's configuration.
