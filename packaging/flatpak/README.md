# Flatpak / Flathub

Este directorio contiene el manifiesto para empaquetar P15 Designer como
Flatpak. El flujo de CI genera un archivo `.flatpak` instalable.

## Compilar localmente

```bash
# 1. Runtime y SDK (una vez)
flatpak remote-add --if-not-exists flathub https://flathub.org/repo/flathub.flatpakrepo
flatpak install -y flathub org.freedesktop.Platform//25.08 org.freedesktop.Sdk//25.08

# 2. Bundle Linux autocontenido (Python + bun + deps)
python3 packaging/build_bundle.py --no-archive

# 3. Flatpak
P15_FLATPAK_INSTALL_DEPS=1 ./packaging/build_flatpak.sh

# 4. Instalar y probar
flatpak install --user dist/P15-Designer-*.flatpak
flatpak run io.github.enlinea777.ImpresoraP15
```

## Publicar en Flathub

Flathub no acepta un `.flatpak` ya compilado: hay que enviar un **manifiesto**
a un repositorio `flathub/io.github.enlinea777.ImpresoraP15` mediante un Pull
Request. El manifiesto de este directorio usa `type: dir`, que sirve para CI
local pero **no** para Flathub (el build de Flathub no tiene acceso a tu disco).

Para Flathub:

1. Crea un release en GitHub que incluya
   `P15-Designer-<version>-linux-x86_64.tar.gz`.
2. Copia `packaging/flatpak/` al repo de Flathub y cambia la fuente principal:

   ```yaml
   sources:
     - type: archive
       url: https://github.com/enlinea777/Impresora-P15/releases/download/v1.0.0/P15-Designer-1.0.0-linux-x86_64.tar.gz
       sha256: <sha256 del .tar.gz>
     - type: file
       path: flatpak-launcher.sh
     # ...
   ```

3. Ajusta los `build-commands` para extraer el tar.gz:

   ```yaml
   - tar -xzf P15-Designer-*.tar.gz
   - mkdir -p /app/lib/p15
   - cp -a P15-Designer/. /app/lib/p15/
   ```

4. Abre el PR en `flathub/flathub` solicitando el repositorio y luego el PR
   del manifiesto. Revisa el linter:

   ```bash
   flatpak-builder-lint manifest io.github.enlinea777.ImpresoraP15.yml
   ```

## Limitación conocida: Bluetooth

El sandbox de Flatpak permite sockets `AF_BLUETOOTH` (`--allow=bluetooth`),
pero **no** concede `CAP_NET_RAW`. La librería `noble` usa sockets HCI crudos
en Linux, por lo que la impresión Bluetooth puede no funcionar dentro de
Flatpak. El diseño, la previsualización y la exportación sí funcionan.

Para imprimir por BLE se recomienda el **AppImage** (con `--setup-bluetooth`)
o el **tar.gz portable** con permisos aplicados a bun:

```bash
sudo setcap cap_net_raw,cap_net_admin+eip \
  P15-Designer/thermoprint/bun
```

Una solución completa requeriría un transporte BLE por D-Bus (BlueZ), que
Flatpak sí permite.
