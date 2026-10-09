# Three.js recortado para la vista 3D

`internal/web/static/3d/00-three.js` es un paquete de [Three.js](https://threejs.org)
0.186.1 (licencia MIT, ver `LICENSE`) con solo las clases que usa la escena de
la Oficina, más `OrbitControls`. Se sirve como un paquete aparte que la interfaz
carga cuando se abre la vista 3D.

Conserva los avisos de licencia de Three.js al final del fichero
(`--legal-comments=eof`). No lleva `eval`, `new Function`, `importScripts` ni
`document.write`, así que
cumple la CSP de la interfaz y el contrato de `tests/test_ax_web_contract.py`.

## Cómo se genera

El resultado es determinista con estas versiones exactas:

```bash
mkdir /tmp/three-vendor && cd /tmp/three-vendor && npm init -y
npm install --save-exact three@0.186.1 esbuild@0.28.2
cp <repo>/images/ax-web/third_party/three/entry.js .
./node_modules/.bin/esbuild entry.js --bundle --format=iife \
  --global-name=THREE --minify --legal-comments=eof --target=es2020 \
  --outfile=three.min.js
sha256sum three.min.js
```

Las sumas sha256 de lo que entra y de lo que sale:

- `three/build/three.module.js` 0.186.1:
  `9052042d676cb0fdc1ddfefe193053f34b7ac0513a616fdac4535d49987812ea`
- `three/build/three.core.js` 0.186.1:
  `9edde002b066a9a05676a6127f67735b62baf399bdea529f2f7e31657da769e6`
- `static/3d/00-three.js` (el resultado):
  `22eb60869d1c0dd93c19dce255e0081fbc88c0417ad8f30bb18bcf3a19b34add`

Para añadir una clase, se añade a `entry.js` y se vuelve a generar. Una versión
nueva de Three.js se revisa y se fija en este fichero.
