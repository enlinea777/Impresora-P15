# thermoprint (código vendorizado)

La carpeta `thermoprint_ref/` contiene una copia integrada del proyecto
[thermoprint](https://github.com/tomLadder/thermoprint), usada para hablar por
Bluetooth con la impresora Marklife P15.

- **Repositorio original:** https://github.com/tomLadder/thermoprint
- **Commit base:** `51967b7bdf6ddfe6e64db6456fad86bf39276e9a`
  (Merge pull request #25, 2026-07-08)
- **Cambios locales incluidos:** transporte Noble, tipos de dispositivo,
  comandos del protocolo L11 y `packages/cli/src/ble_server.ts`.

Se integró al repositorio principal (se eliminó su `.git` interno) para que todo
el código viva en un único repositorio. Las dependencias de Node **no** se
versionan: el script de compilación las instala con `bun install`.

Licencia original: MIT (ver `thermoprint_ref/packages/cli/README.md` y los
archivos `LICENSE` de cada paquete).
