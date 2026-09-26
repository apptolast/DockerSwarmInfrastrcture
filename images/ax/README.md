# AX: parche y manifiestos fijados

Entradas de AX que el laboratorio (`docs/AX.md`, «AX») fija por su sha256 y
que solo usa el job `reproduce-ax` de
`.github/workflows/ax-lab-reproducibility.yml`. El host nunca compila AX:
sus imágenes se siembran desde el laboratorio manual.

## `google-ax-375.patch`

El diff, byte a byte, que el laboratorio manual dejó preparado con
`git apply --index` sobre google/ax `f009cc81c9a571073bc1dd58cd2ed934bf2d5b1c`
(`/opt/ax-lab/patches/ax-issue-375.patch`), y con el que compiló las cuatro
imágenes de AX y la CLI: sha256
`7b8bff50ffc90f06970352f4576ee1dc0d3d9c2fcfb520fea86249beeeae9d72`, el de
`sources.ax.patch` en `config/ax-lab.yml`.

Procedencia: la comparación `google/ax main...arkady-emelyanov/ax:local-models`
propuesta en [google/ax#375](https://github.com/google/ax/issues/375), commits
`c7fd10b` (proveedor compatible con OpenAI) y `89fa366` (los `goal` corren
cuando el sandbox ya está listo), sobre `d8ed0fe` (v0.3.0), aplicada sobre el
commit fijado y sin el `__pycache__/*.pyc` que esa rama versiona. Como AX, es
Apache-2.0. Toca doce ficheros:

- `.dockerignore`
- `cmd/ax-task-runner/antigravity_bootstrap.py`
- `docs/manifests.md`, `docs/runner.md` y `docs/sandbox.md`
- `internal/metadata/server.go` y `internal/metadata/server_test.go`
- `internal/model/client.go` y `internal/model/client_test.go`
- `internal/substrate/client.go`
- `internal/workspace/setup.go`
- `runner/runner.go`

No se le añade cabecera ni se corrige nada dentro, para no cambiar su
sha256: por eso el comentario de `internal/substrate/client.go` que dice que
el runner mantiene `/readyz` en 503 queda desfasado.

## `manifests/`

Los manifiestos OCI exactos que ko subió al registro del laboratorio manual
para `ax-controller` y `ax-server`: sus bytes dan los digests de
`ax.images`, que el validador comprueba. El job de reproducibilidad compara
con ellos lo que recompila.

## `../ax-task-runner/` y `../ax-agents/`

Las entradas exactas con las que el laboratorio manual compiló con buildx
las imágenes del runner y de agentes (`/opt/ax-lab/images`). Esas imágenes
no se pueden recompilar byte a byte (apt de espejos vivos, dependencias de
pip sin hash, fechas y atestación de buildx): solo se siembran, y estos
ficheros quedan como registro de su procedencia. El `Dockerfile` del runner
copia el binario `ax-task-runner` compilado antes con el `Makefile` de AX
(línea 49) y `cmd/ax-task-runner/antigravity_bootstrap.py` del árbol con el
parche; el de agentes se construye con
`--build-arg RUNNER_IMAGE=localhost:5001/ax-task-runner@sha256:5d536baa31bc6ba7efc805d60fd964f1929713a805a731adcb119aac2031d1f2`,
el índice del runner. Sus sha256 están en `tests/test_ax_lab_ax_contract.py`.
