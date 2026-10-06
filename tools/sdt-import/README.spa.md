# Escáner de importación SDT

`scan_sdt.py` analiza flujos MPEG-TS existentes y extrae la información
de su **Service Description Table (SDT Actual)**.

El archivo JSON generado está diseñado para ser importado por el
**Generador SDT de ISDB-Tb EPG**, evitando tener que ingresar
manualmente los identificadores de servicio, nombres de canales,
proveedores, tipos de servicio, TSID y ONID.

El escáner funciona en modo de solo lectura. No modifica los transport
streams de entrada.

## Información extraída

Para cada transport stream:

-   Transport Stream ID (TSID)
-   Original Network ID (ONID)

Para cada servicio:

-   Service ID
-   Tipo de servicio
-   Nombre del servicio
-   Nombre del proveedor
-   Estado de ejecución (`running_status`)
-   Modo de acceso condicional (`free_ca_mode`)
-   Indicador EIT Schedule
-   Indicador EIT Present/Following

La información se obtiene de:

-   PID `0x0011`
-   SDT Actual
-   Table ID `0x42`

## Requisitos

-   Python 3
-   TSDuck
-   Acceso de red a los flujos MPEG-TS UDP/RTP que se desean analizar

Verifique que TSDuck esté disponible:

``` bash
tsp --version
```

## Configuración

Copie el archivo de configuración de ejemplo:

``` bash
cp inputs.example.json inputs.json
```

Edite `inputs.json`:

``` json
{
  "interface": "192.168.1.10",
  "timeout": 5,
  "inputs": [
    {
      "name": "MUX1",
      "address": "239.1.1.1",
      "port": 2001
    },
    {
      "name": "MUX2",
      "address": "239.1.1.1",
      "port": 2002
    }
  ]
}
```

### `interface`

Dirección IP local utilizada para recibir los flujos multicast.

Si no es necesario seleccionar explícitamente una interfaz local, el
campo `interface` puede omitirse.

### `timeout`

Cantidad máxima de segundos que el escáner espera para recibir una tabla SDT.

El escáner indica a TSDuck que finalice normalmente después de recibir una
tabla SDT Actual completa (`--max-tables 1`). Por lo tanto, el timeout funciona
solamente como límite de seguridad cuando no se recibe una SDT, el flujo no está
disponible o la entrada está configurada incorrectamente.

Para una SDT transmitida continuamente, 5 segundos normalmente son suficientes.

### `inputs`

Cada entrada define un flujo MPEG-TS:

-   `name`: Nombre descriptivo del multiplex.
-   `address`: Dirección UDP multicast.
-   `port`: Puerto UDP.

No existe un límite fijo en la cantidad de multiplexes que pueden
incluirse en la configuración.

## Ejecutar el escaneo

``` bash
python3 scan_sdt.py \
  --config inputs.json \
  --output sdt-import.json
```

Ejemplo de salida:

``` text
SDT Import Scanner
============================================================

Scanning MUX1 (239.1.1.1:2001)...
  TSID: 14  ONID: 14  Services: 7
  OK

Scanning MUX2 (239.1.1.1:2002)...
  TSID: 15  ONID: 15  Services: 8
  OK

============================================================
MUX scanned:     2
Services found: 15
Errors:          0
Output:          sdt-import.json
============================================================
```

## Formato de salida

El escáner convierte la representación utilizada por TSDuck a un formato
de importación estable utilizado por este proyecto.

Ejemplo:

``` json
{
  "format": "isdb-tb-epg-sdt-import",
  "version": 1,
  "source": {
    "tool": "TSDuck",
    "table": "SDT Actual",
    "table_id": 66,
    "pid": 17
  },
  "muxes": [
    {
      "name": "MUX1",
      "transport_stream_id": 14,
      "original_network_id": 14,
      "services": [
        {
          "service_id": 32,
          "service_type": 1,
          "service_name": "Canal de ejemplo HD",
          "provider_name": "Proveedor de ejemplo",
          "running_status": "running",
          "free_ca_mode": false,
          "eit_schedule": false,
          "eit_present_following": false
        }
      ]
    }
  ]
}
```

## ¿Por qué utilizar un formato intermedio?

TSDuck representa internamente las tablas PSI/SI mediante su propio
modelo XML/JSON.

Este escáner convierte esa representación a un formato pequeño y estable
específico del proyecto ISDB-Tb EPG.

De esta forma, el Generador SDT no depende directamente de la estructura
JSON interna producida por una versión específica de TSDuck.

Si el formato interno de TSDuck cambia en el futuro, solamente será
necesario adaptar `scan_sdt.py`, manteniendo estable el formato de
importación utilizado por el Generador SDT.

## Indicadores EIT

El escáner conserva exactamente los valores encontrados en la SDT
original para:

-   `eit_schedule`
-   `eit_present_following`

Importar una SDT no modifica automáticamente estos valores.

Posteriormente, el Generador SDT podrá configurar los valores apropiados
de acuerdo con los servicios para los cuales el sistema genera EIT.

## Validación y errores

El escáner valida que cada servicio importado contenga:

-   Service ID
-   Service descriptor
-   Tipo de servicio
-   Nombre del servicio
-   Nombre del proveedor

Si falta información obligatoria, el multiplex afectado se informa como
error en lugar de inventar valores silenciosamente.

Si algunos multiplexes pueden analizarse correctamente y otros fallan,
los multiplexes válidos igualmente se escriben en el archivo de salida.

### Códigos de salida

-   `0`: Todos los flujos fueron analizados correctamente.
-   `1`: Error fatal o no se encontró ninguna SDT utilizable.
-   `2`: Escaneo parcial; uno o más flujos no pudieron analizarse.

## Cómo funciona la captura

Para cada entrada configurada, el escáner utiliza TSDuck para:

1. Abrir la entrada MPEG-TS UDP/RTP.
2. Escuchar el PID `0x0011`.
3. Filtrar la SDT Actual (`table_id 0x42`).
4. Recibir una tabla SDT completa.
5. Finalizar TSDuck normalmente mediante `--max-tables 1`.
6. Leer la representación JSON generada por TSDuck.
7. Validarla y convertirla al formato estable de importación de ISDB-Tb EPG.

El timeout configurado se utiliza únicamente como mecanismo de seguridad si
TSDuck no puede recibir una SDT completa.

## Importar en la aplicación

Antes de modificar la base de datos, valide el archivo generado:

```bash
python3 import_sdt.py --check sdt-import.json
```

Si la validación es correcta, importe la configuración SDT:

```bash
python3 import_sdt.py sdt-import.json
```

El importador identifica cada multiplex mediante su Transport Stream ID
(TSID) y Original Network ID (ONID).

El archivo representa un snapshot SDT completo de cada multiplex incluido.
Los servicios SDT existentes de esos multiplexes son reemplazados por los
servicios contenidos en el archivo de importación.

Si todavía no existe una configuración SDT para un multiplex, se crea
deshabilitada. El generador debe habilitarse explícitamente después de
revisar la configuración importada.

## Notas

El escáner lee específicamente la **SDT Actual** (`table_id 0x42`) desde
el PID `0x0011`.

El transport stream de origen permanece sin modificaciones. La
herramienta solamente recibe el flujo y extrae sus metadatos SDT.

El archivo generado está diseñado para proporcionar la configuración
inicial del Generador SDT. Una vez importada esta información en la base
de datos de la aplicación, el Generador SDT no necesita mantener acceso
al transport stream original.

Esto permite utilizar la herramienta como mecanismo de migración inicial
para instalaciones que ya poseen uno o varios multiplexes configurados.

## Seguridad y privacidad

El archivo de importación generado contiene metadatos SDT, como nombres
de servicios y nombres de proveedores.

Las direcciones multicast de origen y la dirección de la interfaz local
**no se almacenan** en el archivo de importación generado.

Revise el JSON generado antes de publicarlo si los nombres de servicios
o proveedores contienen información que no desea hacer pública.

## Proyecto

Esta herramienta forma parte del proyecto **ISDB-Tb EPG**.

Su objetivo es simplificar la migración desde multiplexes existentes
mediante la importación automática de los metadatos SDT, evitando tener
que configurar manualmente cada servicio.

## Licencia

Esta herramienta se distribuye bajo la misma licencia que el proyecto
principal ISDB-Tb EPG.
