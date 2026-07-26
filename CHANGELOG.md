# Changelog

Los cambios relevantes de la plataforma se documentan aquí. El formato sigue
[Keep a Changelog](https://keepachangelog.com/es-ES/1.1.0/) y las versiones
siguen [Semantic Versioning](https://semver.org/lang/es/).

## [Unreleased]

### Added

- Roots Terraform separados para bootstrap R2, DNS Cloudflare y perímetro
  Netcup.
- Roles Ansible para plataforma, baseline adoptado del host, edge Traefik y
  trazabilidad del despliegue.
- Contrato compartido, tests de seguridad, CI, escaneo de secretos y runbooks
  de migración, estado y recuperación.
- Wrappers seguros para planes Terraform y despliegues Ansible versionados.

### Changed

- El daemon Docker activa `no-new-privileges` y conserva logging `local`
  acotado.
- La política pública queda limitada de forma contractual a `80/TCP` y
  `443/TCP`.

## [0.1.0] - Pendiente de despliegue

- Primera versión declarativa de la plataforma; no se etiqueta ni se considera
  desplegada hasta completar las compuertas externas y verificar producción.

[Unreleased]: https://github.com/apptolast/DockerSwarmInfrastrcture/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/apptolast/DockerSwarmInfrastrcture/releases/tag/v0.1.0
