# Administration and recovery

Use an account authorized for the target installation. Administrative commands mutate the database
selected by `DATABASE_URL`; verify the target before editing or deleting data. Run local examples
from the **Server repository root**, and keep local logs/test data in ignored `build/`.

## Technician password reset in the app

An authenticated administrator can open **Gestão → Contas e acessos**, select a technician and
choose **Repor password do técnico…**. Enter the new password twice (12–200 characters).
The API independently rejects non-administrator callers and every target role except `tech`,
including an account promoted since the app loaded its list. It changes the salted password hash,
revokes all sessions and records `user.password.reset` with actor/target in one transaction.
Reports, ownership and assignments remain intact; devices must log in with the new password.
Use the console below for administrator password recovery. Install the updated API before using
this new app action; it adds no migration or incompatibility for existing clients.

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
| `users revoke-sessions EMAIL_OR_ID`, `users revoke-sessions --all` | Require a fresh login for one account or every account |
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

To require a fresh login from the installed server, run one of these commands:

```bash
bash deploy/manage-server.sh users revoke-sessions tecnico@example.test
bash deploy/manage-server.sh users revoke-sessions --all
```

Use the user's email or UUID for one account. An explicit `--all` also revokes administrators'
sessions. The command reports the number of deleted session records and records a
`console.sessions.revoke` audit entry. Devices are asked to sign in on their next authenticated
request. Users can enter with their existing password and keep their reports and assignments.

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

The built-in `lem-stations-v1` profile recognizes the **Station ID** layout. Required cell values
are Station ID, Morada, CCDR, Fabricante and Tomadas / Fichas. Nº(s) Série, Modelo, Fornecimento de
Energia, Potência (kVA) and Limitação de Potência (kVA) may be empty, as may Código Postal, Latitude,
Longitude and CPE Associado. Keep all template headers in their expected columns, including those
whose values are optional. Model (EST.03) and serial number (EST.04) must be filled in the report
before closure; drafts can retain these answers empty. The exact field/header mapping is maintained in
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

### Move a backup to a new server

This procedure **replaces the destination database**; it does not merge installations.
It assumes a custom `.dump` made by `deploy/backup.sh`, the PostgreSQL 17 Compose layout,
and a fresh destination with matching or newer compatible LEM source. Do not restore an
unknown newer schema into older server code. Keep the old server and backup until checks pass.
The Linux login can be `rodrigo_lem` while PostgreSQL's role/database remain `lem`.

1. Prepare the new Ubuntu host using [First installation](deployment.md#first-installation--on-the-server).
   Log in as `rodrigo_lem`, put the Server source in `/home/rodrigo_lem/apps/Lem`, and run
   `LEM_DOMAIN=lem.example.com bash deploy/install-ubuntu.sh --user rodrigo_lem` there.
   Use a temporary hostname resolving to the new host if the production hostname still serves
   the old host; the installer checks HTTPS. Keep the new `.env` and its database password.
   Do not overwrite it with the old host's configuration after the database volume is initialized.
2. For a final cutover, stop writes on the **old host**, then create the backup from its
   installation root. This takes the old application offline; do not resume writes there
   while users begin working on the new host. An existing backup restores only its snapshot.

   ```bash
   sudo docker compose --env-file deploy/.env -f deploy/compose.yml stop proxy api
   bash deploy/backup.sh
   ```

3. Transfer the exact timestamped dump printed by the backup command over SSH. For example,
   from a workstation (replace hostnames and the timestamp):

   ```bash
   scp lem@OLD_SERVER:/home/lem/apps/Lem/deploy/backups/lem-TIMESTAMP.dump ./lem-transfer.dump
   scp ./lem-transfer.dump rodrigo_lem@NEW_SERVER:~/lem-transfer.dump
   ```

   Compare `sha256sum` on both Linux hosts (or `Get-FileHash` on Windows). Restrict access to
   every transferred copy: `chmod 600 ~/lem-transfer.dump` on the new host. The dump contains
   accounts, reports, signatures and database-stored evidence; treat it as private data.

4. On the **new host**, logged in as `rodrigo_lem`, run the following block from the installation
   root. The subshell stops on any error. It preserves the destination snapshot before replacement,
   validates the archive, and restores in one transaction; failed restoration leaves services
   stopped for diagnosis. Never use `down -v` or `--reset-database` for this procedure.

   ```bash
   cd /home/rodrigo_lem/apps/Lem
   (
     set -e
     compose=(sudo docker compose --env-file deploy/.env -f deploy/compose.yml)
     test -s "$HOME/lem-transfer.dump"
     sudo -v
     "${compose[@]}" exec -T db pg_restore --list < "$HOME/lem-transfer.dump" > /dev/null
     "${compose[@]}" stop proxy api
     bash deploy/backup.sh
     "${compose[@]}" exec -T db pg_restore \
       -U lem -d lem --clean --if-exists --no-owner --no-privileges \
       --exit-on-error --single-transaction < "$HOME/lem-transfer.dump"
     "${compose[@]}" up -d api proxy
   )
   ```

   `--clean` replaces objects from the dump, and `--single-transaction` rolls back those
   changes if restoration fails. If schema differences cause an error, keep the API stopped
   and investigate compatibility; do not remove error checks or delete volumes to force a restore.
   `--no-owner --no-privileges` avoids importing old database ownership/grants. The new database
   role password remains the one in the new `.env`; the Linux account name is irrelevant here.

5. Inspect `sudo docker compose --env-file deploy/.env -f deploy/compose.yml ps` and
   `logs --tail=80 api proxy` using the same Compose options. Check
   `curl --fail https://lem.example.com/api/health`, log in with an **account from the backup**,
   and verify contracts, draft/closed reports, photographs, signatures and PDF/ZIP exports.
   The fresh install's bootstrap administrator is replaced by backed-up accounts; changing
   `ADMIN_PASSWORD` does not reset their passwords. Use the account console above if needed.
6. Point clients at the new HTTPS URL, or move production DNS after verification. If changing
   from a temporary hostname, update `LEM_DOMAIN` in the new `deploy/.env`, point DNS at the new
   host and run `sudo docker compose --env-file deploy/.env -f deploy/compose.yml up -d api proxy`;
   then verify HTTPS again. The deployment `--domain` option asserts a hostname and does not
   change it. Fresh TLS volumes obtain new certificates; the database dump contains no certificates.
   Retain the old host offline for recovery. Returning to it after new writes requires reconciling
   those writes; simply restarting it would lose the new server's changes.

These are operator-run steps; repository checks do not constitute a live database restore test.

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
