# LEM Server

The FastAPI service for contracts, charger catalogues, maintenance reports and evidence. The current
API/source version is **0.1.57** and the shared checklist is **6**.

| Guide | Use it for |
| --- | --- |
| [Architecture and API](docs/architecture-and-api.md) | Modules, persistent data, permissions, request flow and endpoint groups |
| [Deployment](docs/deployment.md) | Local development, Ubuntu installation, SSH updates and configuration |
| [Administration](docs/administration.md) | Accounts, imports, station identity, ownership, backup and removal |
| [Testing](docs/testing.md) | API/deployment tests, prerequisites and interpretation of results |

`server/` contains the service. `deploy/` contains installation and operational tools. `shared/` is
a versioned snapshot of `../Common/shared/`, so this repository can be deployed independently.
`examples/` contains shipped demonstration output, not live customer records.

Run local commands from `../ongoing_work/` and keep environments, test databases, logs and packages
there. See the [workspace guide](../Common/docs/workspace.md) and
[report contract](../Common/docs/report-contract.md) for cross-repository rules. Do not version
production configuration, credentials, backups or build outputs.

For an installed server, [deploy/README.md](deploy/README.md) is the short tool index that travels
with a deployment payload. [deploy/UNINSTALL.md](deploy/UNINSTALL.md) explains the removal workflow
available on the host.
