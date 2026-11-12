#!/bin/sh
# Prepara lo que no puede salir directo del Secret:
# - la cookie de Erlang, que tiene que tener permiso 600 (fsGroup le suma el del grupo al montar);
# - el usuario de los servicios: con definiciones, RabbitMQ no crea el usuario por defecto.
set -eu
printf %s "$RABBIT_COOKIE" > /var/lib/rabbitmq/.erlang.cookie
chmod 600 /var/lib/rabbitmq/.erlang.cookie
cp /config/colas.json /definiciones/colas.json
cat > /definiciones/usuario.json <<FIN
{"users": [{"name": "${RABBIT_USUARIO}", "password": "${RABBIT_CLAVE}", "tags": ["administrator"]}],
 "permissions": [{"user": "${RABBIT_USUARIO}", "vhost": "/", "configure": ".*", "write": ".*", "read": ".*"}]}
FIN
