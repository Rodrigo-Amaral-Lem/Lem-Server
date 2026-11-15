# LEM Server

The FastAPI service for contracts, charger catalogues, maintenance reports and evidence. The current
API/source version is **0.2.0** and the shared checklist is **6**.

| Guide | Use it for |
| --- | --- |
| [Architecture and API](docs/architecture-and-api.md) | Modules, persistent data, permissions, request flow and endpoint groups |
| [Deployment](docs/deployment.md) | Local development, Ubuntu installation, SSH updates and configuration |
| [Administration](docs/administration.md) | Accounts, imports, station identity, ownership, backup and removal |
| [Testing](docs/testing.md) | API/deployment tests, prerequisites and interpretation of results |

`server/` contains the service. `deploy/` contains installation and operational tools. `shared/` is
a versioned snapshot of `../Common/shared/`, so this repository can be deployed independently.
`examples/` contains shipped demonstration output, not live customer records.

Run local commands from this repository root. Use `.venv/` for Python and ignored `build/`
subdirectories for development databases, test reports and deployment payloads. See the [workspace guide](../Common/docs/workspace.md) and
[report contract](../Common/docs/report-contract.md) for cross-repository rules. Do not version
production configuration, credentials, backups or build outputs.

For an installed server, [deploy/README.md](deploy/README.md) is the short tool index that travels
with a deployment payload. [deploy/UNINSTALL.md](deploy/UNINSTALL.md) explains the removal workflow
available on the host.

## License

First-party LEM source code, tests, build/deployment tools, configuration, schemas and accompanying
project documentation are licensed under the **GNU General Public License, version 3 only**
(`GPL-3.0-only`). See [LICENSE](LICENSE) for the complete terms and warranty disclaimer.

Source files carry SPDX notices. JSON data keeps its original strict format and has adjacent
`.json.license` notices. Existing third-party license notices remain applicable; this declaration
does not relicense Qt, Python dependencies, bundled fonts or third-party customer artwork/trademarks.
Generated reports and customer data are not relicensed by this source-code notice.

A complete license copy at [server/LICENSE](server/LICENSE) also travels with server deployment
payloads and the API container. Bundled fonts retain [their original license](server/fonts/LICENSE.txt).
