# LEM 0.1.46 — local Docker deployment on Ubuntu 26.04 LTS

This package contains the Dockerfile, server code and Compose stack. It is NOT a
prebuilt Docker image or a published registry image. Docker is not available in
the authoring environment, so a built image is not supplied or runtime-validated
here. Build the image on your Docker host (Ubuntu 26.04.1 reports VERSION_ID=26.04).
The installer also retains Ubuntu 24.04 support.

## Fresh server: local installation, no SSH required

Use an ordinary account with sudo permissions. Extract the ZIP into a dedicated
folder and open a terminal in the folder containing server/, shared/ and deploy/.
Set your domain's A/AAAA records to the server and allow TCP 80/443 to reach it.
Then run, as that ordinary user (not sudo bash):

    bash deploy/install-ubuntu.sh

The installer checks for an existing .env and refuses to overwrite one. On a fresh
host it installs Docker Engine/Compose from the correct official Ubuntu repository
(resolute for 26.04; noble for 24.04), creates private credentials, enables the
services, creates the certificate volumes, builds the API and checks HTTPS.
It prints the first administrator login locally; do not share deploy/.env.
No Python/Qt/PostgreSQL/Caddy runtime install on the host is required: those runtime
services live in containers. Python3, sudo and a terminal are prerequisites for the
supplied deployment tools; unzip is needed only if you extract from the command line.
The installer opens the SSH port in UFW but does not install or require an SSH daemon.
It enables UFW: on an existing multipurpose server, review its firewall needs first.

The domain defaults to lem.rodrod.pt. To use another domain:

    LEM_DOMAIN=maintenance.example.org bash deploy/install-ubuntu.sh

## Images in this stack

- lem-api:0.1.46 — built locally from deploy/Dockerfile (Python 3.12 slim).
- postgres:17 — pulled from Docker Hub.
- caddy:2 — pulled from Docker Hub.

No docker pull lem-api command is available: no registry image has been published.
Port 8000 (API) and 5432 (PostgreSQL) stay on Docker's internal network; only the
proxy publishes TCP 80/443. Data and certificates persist in named Docker volumes.
The API image contains no database, photographs, .env or TLS private keys.

## Build/export just the API image

From the extracted project root, with Docker Engine installed and running:

    sudo docker build -t lem-api:0.1.46 -f deploy/Dockerfile .
    sudo docker save -o lem-api-0.1.46.tar lem-api:0.1.46

Transfer the .tar to a host with the SAME CPU architecture and import it there:

    sudo docker load -i lem-api-0.1.46.tar

This API-only tar does not include PostgreSQL/Caddy, configuration or data. For an
air-gapped stack, also pull/export postgres:17 and caddy:2 on a matching-architecture
host, copy the deployment configuration, prepare .env and the certificate volumes,
and provide the network conditions needed for certificate issuance. Initial local
installation normally requires internet access for apt, pip, images and certificates.

## Existing installation

Do not run the fresh installer over an existing deploy/.env. Continue to use the
established update/backup process. The source archive includes the usual SSH
updates, but this R1 does not introduce a new standalone local-update wrapper.
Do not use docker compose down -v: that removes persistent data.

## Validation

Shell syntax, Ubuntu release-to-repository selection and deployment packaging are
checked locally, together with existing mocked deployment tests. Actual Docker
build/startup and Ubuntu 26.04.1 execution must be validated on the target host.
The app version, server policy and app binaries remain 0.1.46; no rebuild of the
Qt app is required for these deployment-only changes.
