# ISDB-Tb EPG Generator

Sistema de generación y distribución de EPG para redes de Televisión
Digital Terrestre **ISDB-Tb**.

Permite importar programación desde fuentes XMLTV, generar programación
manual, asociar canales EPG con servicios ISDB-Tb y generar tablas
**SDT, EIT, TDT y TOT** listas para ser enviadas como MPEG Transport Stream
a un modulador ISDB-T.

También permite agrupar MUX mediante **EPG/EIT Groups** para distribuir
EIT Other entre varios Transport Streams, de modo que un receptor pueda
recibir la programación de otros MUX del mismo grupo.

La aplicación incluye una interfaz web para administrar fuentes EPG,
redes, MUX, servicios, EPG/EIT Groups, SDT y salidas UDP.

## Características

-   Importación de múltiples fuentes XMLTV.
-   Generación de EPG manual.
-   Base de datos SQLite.
-   Interfaz de administración web.
-   Múltiples redes ISDB-Tb y múltiples MUX / Transport Streams.
-   Asociación de canales XMLTV con servicios ISDB-Tb.
-   EIT Present/Following y EIT Schedule.
-   EIT Actual y EIT Other.
-   EPG/EIT Groups para compartir programación entre múltiples MUX.
-   Generación de SDT Actual configurable por MUX.
-   Configuración de servicios SDT y flags EIT por servicio.
-   Herramientas para escanear e importar SDT existentes mediante TSDuck.
-   TDT y TOT.
-   Salida MPEG-TS por UDP multicast configurable por MUX.
-   TTL e interfaz IPv4 de salida configurables.
-   Intervalos independientes para P/F y Schedule.
-   Un broadcaster independiente por MUX.
-   Salida opcional a archivo TS para diagnóstico.
-   Endpoint `/health`.
-   Ejecución mediante systemd.
-   Reverse proxy mediante Nginx.

## Arquitectura

``` text
Fuentes XMLTV / EPG manual
            |
            v
          SQLite
            |
            v
     Mapeo canal EPG
            |
            v
     Servicio ISDB-Tb
            |
    +-------+-------+
    |               |
    v               v
EPG/EIT Groups     SDT
    |           PID 0x0011
    v               |
EIT Actual/Other    |
 PID 0x0012         |
    |               |
    +-------+-------+
            |
         TDT/TOT
        PID 0x0014
            |
            v
   MPEG Transport Stream
            |
            v
      UDP / Multicast
            |
            v
      Modulador ISDB-T
            |
            v
        Receptor / TV
```

Una única instancia puede administrar múltiples MUX. Cada MUX puede
tener su propia dirección UDP, puerto, TTL e interfaz de salida.

Los PID generados son:

``` text
0x0011  SDT
0x0012  EIT Present/Following + EIT Schedule
0x0014  TDT + TOT
```

Los MUX pueden organizarse en EPG/EIT Groups. Dentro de un grupo, cada
salida transmite su propia EIT Actual y además EIT Other correspondiente
a los demás MUX habilitados del mismo grupo.

## Plataforma validada

La instalación de referencia ha sido probada con:

-   Ubuntu 26.04.1 LTS
-   Python 3.14.4
-   pip 26.2.1
-   FastAPI 0.142.0
-   Uvicorn 0.54.0
-   SQLAlchemy 2.1.1
-   SQLite
-   Nginx
-   TSDuck 3.45-4798 para diagnóstico

TSDuck no es necesario para ejecutar el generador. Se utiliza como
herramienta de análisis y validación de Transport Streams.

## Instalación

### 1. Actualizar Ubuntu

``` bash
apt update
apt upgrade -y
```

### 2. Instalar dependencias

``` bash
apt install -y git python3 python3-venv python3-pip nginx curl ca-certificates
```

### 3. Clonar el repositorio

``` bash
cd /opt
git clone <URL-DEL-REPOSITORIO> isdb-epg
cd /opt/isdb-epg
```

Reemplazar `<URL-DEL-REPOSITORIO>` por la URL real del repositorio
GitHub.

### 4. Crear el entorno virtual

``` bash
python3 -m venv venv
source venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt
```

### 5. Preparar directorios

``` bash
mkdir -p /opt/isdb-epg/data/generated
mkdir -p /opt/isdb-epg/logs
```

La base de datos se almacena en `/opt/isdb-epg/data/epg.db`. No es
necesario crear las tablas manualmente: SQLAlchemy crea automáticamente
las que no existan durante el arranque.

## Primera prueba

``` bash
cd /opt/isdb-epg
source venv/bin/activate
uvicorn main:app --host 127.0.0.1 --port 8090
```

Desde otra terminal:

``` bash
curl http://127.0.0.1:8090/health
```

Una respuesta correcta contiene:

``` json
{
  "status": "ok",
  "version": "0.3.0"
}
```

## Servicio systemd

El repositorio incluye `deploy/isdb-epg.service`.

``` bash
cp /opt/isdb-epg/deploy/isdb-epg.service /etc/systemd/system/isdb-epg.service
systemctl daemon-reload
systemctl enable --now isdb-epg
systemctl status isdb-epg
```

El servicio ejecuta Uvicorn en `127.0.0.1:8090`.

Logs:

``` bash
journalctl -u isdb-epg -f
```

## Nginx

El repositorio incluye `deploy/nginx.conf.example`.

``` bash
cp /opt/isdb-epg/deploy/nginx.conf.example /etc/nginx/sites-available/isdb-epg
```

Editar el archivo y reemplazar `epg.example.com` por el dominio real.

La interfaz web es administrativa. Se recomienda restringir el acceso
mediante ACL IPv4/IPv6 y firewall.

Ejemplo:

``` nginx
location / {
    allow 192.168.1.0/24;
    allow 2001:db8::/32;
    deny all;

    proxy_pass http://127.0.0.1:8090;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto https;

    proxy_connect_timeout 10s;
    proxy_send_timeout 60s;
    proxy_read_timeout 60s;
}
```

Habilitar el sitio:

``` bash
ln -s /etc/nginx/sites-available/isdb-epg /etc/nginx/sites-enabled/isdb-epg
nginx -t
systemctl reload nginx
```

Si el sitio `default` entra en conflicto, eliminar su enlace de
`sites-enabled`.

## HTTPS

Se recomienda HTTPS. El ejemplo espera certificados en:

``` text
/etc/letsencrypt/live/epg.example.com/fullchain.pem
/etc/letsencrypt/live/epg.example.com/privkey.pem
```

Adaptar dominio y rutas a la instalación real. La configuración de
ejemplo habilita TLS 1.2 y TLS 1.3.

## Configuración inicial

``` text
Sources
   |
   v
XMLTV / EPG manual
   |
   v
ISDB-Tb Network
   |
   v
MUX / Transport Stream
   |
   +--> Servicios + Mapeo EPG
   |
   +--> EPG/EIT Group
   |
   +--> SDT
   |
   v
EIT Actual / Other + SDT + TDT/TOT
   |
   v
UDP / Multicast
   |
   v
Modulador
```

## Fuentes EPG

### XMLTV

Desde `Sources` pueden agregarse fuentes XMLTV. Una fuente puede
proporcionar múltiples canales y programas. Después de importarla, sus
canales quedan disponibles para asociarlos a servicios ISDB-Tb.

### EPG manual

También pueden crearse fuentes EPG manuales para señales sin XMLTV
público. La programación manual se almacena en la misma base SQLite.

## Network ISDB-Tb

Desde `ISDB-Tb -> Network` pueden crearse redes.

Parámetros principales:

-   Network name
-   Network ID
-   Original Network ID
-   Country code
-   Timezone

Valores predeterminados:

``` text
Country code: ARG
Timezone: America/Argentina/Buenos_Aires
```

## MUX / Transport Stream

Cada Network puede contener múltiples MUX.

Parámetros disponibles:

-   Transport Stream ID
-   Physical channel
-   Frequency
-   EIT PID
-   EIT version
-   Schedule days
-   Language
-   Running status
-   Free CA mode
-   Extended descriptions
-   P/F interval
-   Schedule prime interval
-   Schedule later interval
-   Prime days
-   Output mode
-   Output address
-   Output port
-   Multicast TTL
-   Output interface

PID EIT predeterminado: `0x0012`.

Salida predeterminada:

``` text
Address: 239.255.0.1
Port:    5000
TTL:     1
```

## Servicios ISDB-Tb

Cada MUX puede contener múltiples servicios. Para cada servicio pueden
configurarse:

-   Service ID / SID
-   Service name
-   Service type
-   Fuente EPG
-   Canal EPG
-   EIT Present/Following
-   EIT Schedule
-   Estado enabled/disabled

El SID debe coincidir con el Service ID real utilizado en el Transport
Stream.

## EPG/EIT Groups

Los **EPG/EIT Groups** permiten relacionar varios MUX para distribuir la
programación de todos ellos en cada carrier del grupo.

Cada MUX puede pertenecer como máximo a un grupo. Un grupo puede estar
habilitado o deshabilitado. Si un MUX no pertenece a ningún grupo, el
comportamiento permanece limitado a sus propias tablas EIT Actual.

Dentro de un grupo habilitado:

-   El MUX local genera EIT Present/Following Actual (`table_id 0x4E`).
-   Los demás MUX del grupo se anuncian mediante EIT Present/Following
    Other (`table_id 0x4F`).
-   El Schedule del MUX local utiliza EIT Schedule Actual
    (`table_id 0x50` a `0x5F`).
-   El Schedule de los demás MUX utiliza EIT Schedule Other
    (`table_id 0x60` a `0x6F`).
-   Las tablas Other conservan el TSID, ONID y Service ID del MUX al que
    realmente pertenece cada servicio.
-   Actual y Other comparten el PID EIT `0x0012` de la salida del MUX.

Esto permite que receptores compatibles aprendan la programación de
otros Transport Streams sin necesidad de sintonizarlos previamente.

## EIT Present/Following

-   PID: `0x0012`
-   EIT Present/Following Actual: `table_id 0x4E`
-   EIT Present/Following Other: `table_id 0x4F`
-   Intervalo predeterminado Actual: 2 segundos
-   Intervalo predeterminado Other: 20 segundos

## EIT Schedule

El sistema mantiene un carousel continuo de EIT Schedule sobre PID
`0x0012`.

Valores predeterminados:

``` text
Schedule Actual prime: 10 segundos
Schedule Actual later: 30 segundos
Schedule Other prime:  60 segundos
Schedule Other later:  300 segundos
```

El Schedule Actual utiliza `table_id 0x50` a `0x5F` y el Schedule Other
utiliza `table_id 0x60` a `0x6F`.

La caché interna se regenera periódicamente para incorporar cambios de
programación. La generación compartida evita repetir innecesariamente el
trabajo pesado cuando varios MUX pertenecen al mismo EPG/EIT Group.

## SDT Generator

El sistema puede generar **SDT Actual** para cada MUX sobre PID `0x0011`.

La configuración SDT permite definir los servicios anunciados en cada
Transport Stream y sus parámetros principales:

-   Service ID.
-   Service type.
-   Service name.
-   Provider name.
-   `eit_schedule`.
-   `eit_present_following`.
-   Running status.
-   Free CA mode.

La interfaz web permite crear, editar, habilitar o deshabilitar la
configuración SDT de cada MUX y administrar sus servicios. También existe
una configuración global para valores predeterminados al crear nuevos
servicios.

La SDT se transmite por la misma salida UDP configurada para el MUX y
mantiene un continuity counter independiente.

### Importación de una SDT existente

El directorio `tools/sdt-import/` incluye herramientas para migrar la SDT
de Transport Streams existentes. El scanner utiliza TSDuck para leer SDT
Actual (`table_id 0x42`) desde PID `0x0011`.

``` bash
cd /opt/isdb-epg/tools/sdt-import
/opt/isdb-epg/venv/bin/python scan_sdt.py --config inputs.json --output sdt-import.json
```

Validación sin modificar la base:

``` bash
/opt/isdb-epg/venv/bin/python import_sdt.py --check sdt-import.json
```

Importación:

``` bash
/opt/isdb-epg/venv/bin/python import_sdt.py sdt-import.json
```

Consulte `tools/sdt-import/README.md` o
`tools/sdt-import/README.spa.md` para la documentación completa.

## TDT / TOT

El broadcaster genera TDT y TOT sobre PID `0x0014`. EIT y las tablas de
tiempo mantienen continuity counters independientes.

## Salida UDP

Cada MUX puede tener su propia salida:

``` text
Output mode:       udp
Output address:    239.255.0.1
Output port:       5000
TTL:               1
Output interface:  <vacío o IPv4 local>
```

Si `Output interface` queda vacío, el sistema operativo selecciona la
interfaz. En servidores con múltiples interfaces puede indicarse la IPv4
unicast local de salida multicast.

El broadcaster genera paquetes MPEG-TS de 188 bytes y agrupa hasta
`7 x 188 = 1316 bytes` por datagrama UDP.

## Múltiples MUX

No es necesario ejecutar un servicio systemd por MUX. El EIT Manager
crea un broadcaster independiente por cada MUX habilitado con salida
activa.

``` text
ISDB-Tb EPG
    |
    +-- MUX 1 -> 239.255.0.1:5000
    +-- MUX 2 -> 239.255.0.2:5000
    +-- MUX 3 -> 239.255.0.3:5000
```

Todos funcionan dentro del mismo proceso.

## Integración con moduladores ISDB-T

``` text
ISDB-Tb EPG Generator
          |
          | UDP
          v
      Modulador
          |
          | PID Bypass / SI insertion
          v
     Transport Stream
          |
          v
       ISDB-T RF
```

Los mecanismos exactos de inserción dependen del fabricante. En equipos
con `PID Bypass` pueden incorporarse los PID generados al TS final. Debe
verificarse que no existan conflictos de PID.

Para utilizar todas las tablas generadas deben permitirse los PID
`0x0011` (SDT), `0x0012` (EIT) y `0x0014` (TDT/TOT), evitando que el
modulador genere tablas incompatibles sobre esos mismos PID.

## Verificación de multicast

``` bash
tcpdump -ni any udp port 5000
```

Para un multicast específico:

``` bash
tcpdump -ni any host 239.255.0.1 and udp port 5000
```

## TSDuck

TSDuck es opcional y se utiliza para diagnóstico. Durante el desarrollo
se utilizó TSDuck 3.45-4798.

Puede comprobar:

``` text
PID 0x0011
    SDT Actual

PID 0x0012
    EIT Present/Following Actual / Other
    EIT Schedule Actual / Other

PID 0x0014
    TDT
    TOT
```

También es útil para analizar continuity counters, CRC, secciones,
table_id, repetición de tablas, servicios, TSID y ONID.

## Health check

``` bash
curl http://127.0.0.1:8090/health
```

El endpoint informa el estado general y los broadcasters activos.

## Backup

La configuración y programación se almacenan en
`/opt/isdb-epg/data/epg.db`

``` bash
systemctl stop isdb-epg
cp /opt/isdb-epg/data/epg.db /opt/isdb-epg/data/epg-backup-$(date +%Y%m%d-%H%M%S).db
systemctl start isdb-epg
```

## Restauración

``` bash
systemctl stop isdb-epg
cp /ruta/del/backup.db /opt/isdb-epg/data/epg.db
systemctl start isdb-epg
```

## Actualización

Realizar primero un backup de `data/epg.db`.

``` bash
cd /opt/isdb-epg
git pull
source venv/bin/activate
pip install -r requirements.txt
systemctl restart isdb-epg
systemctl status isdb-epg
curl http://127.0.0.1:8090/health
```

## Troubleshooting

### Uvicorn no responde

``` bash
systemctl status isdb-epg
journalctl -u isdb-epg -n 100 --no-pager
ss -lntp | grep 8090
curl http://127.0.0.1:8090/health
```

### Nginx devuelve 502

``` bash
curl http://127.0.0.1:8090/health
nginx -t
journalctl -u isdb-epg -n 100 --no-pager
```

### No aparece tráfico multicast

``` bash
tcpdump -ni any udp port 5000
```

Comprobar Output mode, address, port, TTL, interface y estado del MUX.

### Hay multicast pero el receptor no muestra EPG

Comprobar:

1.  SID del servicio.
2.  TSID del MUX.
3.  ONID de la Network.
4.  Asociación servicio/canal EPG.
5.  EIT Present/Following habilitado.
6.  EIT Schedule habilitado.
7.  Pertenencia al EPG/EIT Group correcto si se utiliza EIT Other.
8.  PID `0x0011` presente si se utiliza el SDT Generator.
9.  PID `0x0012` presente en el TS final.
10. PID `0x0014` presente para TDT/TOT.
11. PID Bypass/SI insertion del modulador.

## Estructura del proyecto

``` text
isdb-epg/
├── app/
│   ├── database.py
│   ├── models.py
│   ├── epg/
│   │   ├── manual.py
│   │   ├── scheduler.py
│   │   └── xmltv_importer.py
│   ├── isdb/
│   │   ├── broadcaster.py
│   │   ├── crc.py
│   │   ├── datetime.py
│   │   ├── descriptors.py
│   │   ├── eit.py
│   │   ├── generator.py
│   │   ├── manager.py
│   │   ├── packetizer.py
│   │   ├── sdt.py
│   │   ├── text.py
│   │   └── time_tables.py
│   └── web/
│       ├── routes.py
│       ├── static/
│       └── templates/
├── data/
│   └── generated/
├── deploy/
│   ├── isdb-epg.service
│   └── nginx.conf.example
├── logs/
├── tools/
│   └── sdt-import/
├── eit_streamer.py
├── main.py
├── requirements.txt
├── LICENSE
└── README.md
```

## Seguridad

La interfaz web debe considerarse administrativa. No se recomienda
exponer Uvicorn directamente a Internet.

``` text
Cliente
   |
 HTTPS
   |
 Nginx
   |
 ACL / Firewall / VPN
   |
127.0.0.1:8090
   |
 Uvicorn
```

## Licencia

El proyecto puede utilizarse gratuitamente, incluyendo uso interno en
empresas, ISP, cableoperadores, broadcasters y otras organizaciones.

Se permite estudiar, modificar y redistribuir gratuitamente el software.
También se permite cobrar por instalación, configuración, integración,
personalización, capacitación, mantenimiento y soporte.

No está permitido vender, alquilar, sublicenciar por un cargo ni ofrecer
el software como producto o servicio alojado pago.

Consultar `LICENSE` para los términos completos. Debido a la restricción
de comercialización del software, esta es una licencia
**source-available**, no una licencia OSI open-source.

## Versión

Versión actual: **0.3.0**

### Novedades de v0.3.0

-   EPG/EIT Groups para relacionar múltiples MUX.
-   EIT Present/Following Other (`0x4F`).
-   EIT Schedule Other (`0x60` a `0x6F`).
-   Generación y caché compartida de EIT por grupo.
-   Cadencias independientes para EIT Actual y Other.
-   SDT Generator sobre PID `0x0011`.
-   Administración web de SDT y sus servicios.
-   Configuración global/defaults para SDT.
-   Herramientas TSDuck para escanear, validar e importar SDT existentes.
-   Scheduler automático para actualización de fuentes EPG.
-   Configuración de actualización automática para fuentes EPG manuales.
-   Preservación estable de IDs de canales XMLTV durante actualizaciones.

El proyecto está siendo desarrollado y probado en un entorno real
ISDB-Tb.

## Fuentes EPG

Las fuentes XMLTV pueden ser de pago como ReporTV o las versiones free dispobles en internet como FreeEPG o IPTV-EPG 
