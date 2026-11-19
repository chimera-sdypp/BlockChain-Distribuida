#!/bin/bash
# Los tests del Pilar 2 contra el Redis y el RabbitMQ de prueba (6390/5690). Uso: billetera/tests/correr_tests.sh [args de pytest]
cd "$(dirname "$0")/../.." || exit 1
PYTHON=${PYTHON:-python3}
INTEGRACION=1 REDIS_HOST=127.0.0.1 REDIS_PUERTO=${REDIS_PUERTO:-6390} RABBIT_HOST=127.0.0.1 RABBIT_PUERTO=${RABBIT_PUERTO:-5690} \
    exec "$PYTHON" -m pytest -q "$@"
