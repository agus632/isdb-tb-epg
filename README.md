# ISDB-Tb EPG Generator

Sistema de generación y distribución de EPG para redes de Televisión
Digital Terrestre **ISDB-Tb**.

Permite importar programación desde fuentes XMLTV, generar programación
manual, asociar canales EPG con servicios ISDB-Tb y generar tablas
**EIT, TDT y TOT** listas para ser enviadas como MPEG Transport Stream a
un modulador ISDB-T.

La aplicación incluye una interfaz web para administrar fuentes EPG,
redes, MUX, servicios y salidas UDP.

## Características

-   Importación de múltiples fuentes XMLTV.
-   Generación de EPG manual.
-   Base de datos SQLite.
-   Interfaz de administración web.
-   Múltiples redes ISDB-Tb y múltiples MUX / Transport Streams.
-   Asociación de canales XMLTV con servicios ISDB-Tb.
-   EIT Present/Following y EIT Schedule.
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
    |       |       |
    v       v       v
 EIT P/F Schedule TDT/TOT
    |       |       |
    +-------+-------+
            |
            v
   MPEG Transport Stream
     PID 0x0012 / 0x0014
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
  "version": "0.2.0"
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
   v
Servicios
   |
   v
Mapeo Servicio <-> Canal EPG
   |
   v
EIT P/F + Schedule
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

## EIT Present/Following

-   PID: `0x0012`
-   EIT Present/Following Actual: `table_id 0x4E`
-   Intervalo predeterminado: 2 segundos

## EIT Schedule

El sistema mantiene un carousel continuo de EIT Schedule sobre PID
`0x0012`.

Valores predeterminados:

``` text
Schedule prime interval: 10 segundos
Schedule later interval: 30 segundos
```

La caché interna se regenera periódicamente para incorporar cambios de
programación.

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
PID 0x0012
    EIT Present/Following
    EIT Schedule

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
7.  PID `0x0012` presente en el TS final.
8.  PID `0x0014` presente para TDT/TOT.
9.  PID Bypass/SI insertion del modulador.

## Estructura del proyecto

``` text
isdb-epg/
├── app/
│   ├── database.py
│   ├── models.py
│   ├── epg/
│   │   ├── manual.py
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

Versión actual: **0.2.0**

El proyecto está siendo desarrollado y probado en un entorno real
ISDB-Tb.

## Fuentes EPG

Las fuentes XMLTV pueden ser de pago como ReporTV o las versiones free dispobles en internet como FreeEPG o IPTV-EPG 
