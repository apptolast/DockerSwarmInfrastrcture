# Stack de observabilidad

Este directorio implementa la plataforma interna definida en
`config/services.yml`. No contiene ni migra Dozzle, Kubernetes o monitoring
legacy.

- `stack.yml.j2`: doce servicios Swarm, cero puertos publicados.
- `secrets.yml`: nombres y consumidores de cinco secretos externos v1.
- `config/`: Prometheus, reglas, Alertmanager, Blackbox, Loki, Alloy y
  provisioning Grafana.
- `dashboards/`: dashboards Grafana versionados.

Render y validación:

```bash
./scripts/validate-observability.sh
```

La operación, el acceso SSH por stdio sin forwarding TCP, el proxy Docker
restringido, la rotación de secretos, las rutas de backup y las fuentes
primarias están documentadas en
[`docs/OBSERVABILITY.md`](../../docs/OBSERVABILITY.md).
