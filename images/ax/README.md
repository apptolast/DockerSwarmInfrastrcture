# AX: parche y manifiestos fijados

Entradas de AX que el laboratorio (`docs/AX.md`, «AX») fija por su sha256 y
que solo usa la CI: el job `reproduce-ax` de
`.github/workflows/ax-lab-reproducibility.yml`, que compila y compara, y el
job `ax` de `.github/workflows/ax-lab-images.yml`, que además sube lo que
compila (ver `docs/AX.md`, «Imágenes compiladas en la CI y ventana de
resiembra»). El host nunca compila AX: sus imágenes se siembran, la primera
vez desde el laboratorio manual y después desde los artefactos de la CI.

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

## `google-ax-modules.patch`

Segundo parche de AX, sobre el primero y en el mismo índice: solo `go.mod` y
`go.sum` (2418 bytes, sha256
`7e785a737ea02c3e134004224e4fe5470cef9daa4716759e752748990588de4f`, el de
`sources.ax.modules_patch` en `config/ax-lab.yml`). Sube `golang.org/x/net` a
v0.60.0 y con él `golang.org/x/sys` a v0.48.0 y `golang.org/x/text` a v0.42.0,
que es lo que corrige los hallazgos de `govulncheck` alcanzables en AX. Se
generó en la CI con `go get golang.org/x/net@v0.60.0` y `go mod tidy` sobre el
commit fijado con el primer parche aplicado (`git diff --cached --full-index
--binary`); compila y pasa los mismos ocho paquetes de tests que antes.

La CLI `ax` y las imágenes de ko de `ax-controller` y `ax-server` se compilan
con los dos parches. El binario `ax-task-runner` que llevan las imágenes del
runner y de agentes no: se compila solo con el primero y con
`images.runner_toolbox` (Go 1.27.1), porque esas dos imágenes se siembran tal
cual y su sha256 (`ax.task_runner_binary_sha256`) describe el binario que ya
tienen.

## `substrate-modules.patch`

Parche de Agent Substrate, sobre su commit fijado: solo `go.mod` y `go.sum`
(13580 bytes, sha256
`3625954af1aea377b7f5207c67d1139d4113b02b3a7c1dd3412a4bde095ab8c7`, el de
`sources.substrate.modules_patch` en `config/ax-lab.yml`). Sube los módulos
que corrigen los seis hallazgos alcanzables de `govulncheck` en dos módulos.
Substrate versiona `vendor/`, así que la CI aplica el parche, ejecuta
`go mod vendor` con la imagen `images.toolbox` y deja el árbol preparado sin
confirmarlo (Go sella `vcs.modified` desde el índice, y un commit añadiría una
revisión que ninguna recompilación reproduciría). Lo que se fija en
`modules_patch.tree` es el árbol de git de la raíz que resulta
(`git write-tree`), de modo que el resultado de `go mod vendor` queda
comprobado sin versionar los 339 ficheros (1,43 MB) que cambia. El host nunca
aplica este parche ni vendoriza: su copia de Substrate sigue siendo la
original.

El validador solo acepta parches que toquen exactamente `go.mod` y `go.sum`
(en ese orden, sin renombrados, cambios de modo ni binarios) y que no añadan
directivas `replace` ni `exclude`.

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
