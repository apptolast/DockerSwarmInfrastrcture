# Diagnósticos conocidos de Docker 29.6.2

La restauración de este Swarm sano genera dos registros de arranque reproducibles.
No se ocultan ni se rebaja globalmente el nivel de logging. El validador solo los
acepta mediante `--allow-known-swarm-startup` y después de comprobar que el nodo
está `active`, `Ready`, `Active` y `Leader`.

## `error creating cluster object`

Registro:

```text
level=error msg="error creating cluster object"
```

El código oficial de Moby 29.6.2 explica que crear el objeto por defecto debe
fallar cuando el clúster ya existe. La condición que decide registrarlo utiliza
`err != ErrExist || err != ErrNameConflict`; esa disyunción también registra los
dos resultados esperados. El mismo código continúa presente en la rama principal.

- [Comentario y condición en Moby 29.6.2](https://github.com/moby/moby/blob/3d80467678f6e36325fa9ae3dd486fe91e5652e3/vendor/github.com/moby/swarmkit/v2/manager/manager.go#L953-L985)

## `MAC address changed`

Registro:

```text
level=warning msg="MAC address changed" iface=br0
```

Durante la restauración de la red ingress, Linux recalcula la MAC del bridge al
incorporar sus interfaces. Moby detecta el cambio mientras prepara anuncios
ARP/NA, lo registra y detiene ese envío con la MAC antigua. En este host aparece
una sola vez por arranque; la red, el nodo y el scheduler convergen correctamente.

- [Detección en Moby 29.6.2](https://github.com/moby/moby/blob/3d80467678f6e36325fa9ae3dd486fe91e5652e3/daemon/libnetwork/osl/interface_linux.go#L723-L733)

## Política de validación

Toda otra entrada de prioridad warning o superior, o con
`level=warning|error|fatal|panic`, hace fallar la validación. También falla si
alguno de estos dos textos aparece más de una vez o si el manager no está sano.

La aceptación es específica de la versión y debe revisarse al actualizar Docker.
