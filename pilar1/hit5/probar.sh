#!/bin/bash
# Script de pruebas automatizadas y verificación cruzada para el Hit 5

set -e

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"

echo "============================================================"
echo "Compilando Hit 5 (pow.cu)..."
echo "============================================================"

# Detectar arquitectura GPU disponible o usar sm_75 por defecto (Colab T4)
ARCH="sm_75"
if command -v nvidia-smi &> /dev/null; then
    GPU_NAME=$(nvidia-smi --query-gpu=name --format=csv,noheader | head -n 1)
    if [[ "$GPU_NAME" =~ "40" ]]; then
        ARCH="sm_89"
    elif [[ "$GPU_NAME" =~ "T4" ]]; then
        ARCH="sm_75"
    fi
fi

echo "Compilando con arquitectura: -arch=$ARCH"
nvcc -O3 -arch=$ARCH pow.cu -o pow

echo ""
echo "============================================================"
echo "Iniciando Batería de Pruebas (Hit 5 - Proof of Work en GPU)"
echo "============================================================"

CASOS=(
    "bloque_genesis:0"
    "transaccion_alice_bob:ab"
    "unlu_sdypp_2026:123"
    "cripto_cuda:cafe"
    "minero_t4:00000"
)

EXITOSOS=0
TOTAL=${#CASOS[@]}

for CASO in "${CASOS[@]}"; do
    IFS=":" read -r CADENA PREFIJO <<< "$CASO"
    echo ""
    echo ">>> Testeando cadena='$CADENA' con prefijo='$PREFIJO'..."
    
    # Ejecutar minero en GPU y capturar salida
    SALIDA=$(./pow "$CADENA" "$PREFIJO")
    
    # Extraer nonce ganador y hash GPU usando grep y awk
    NONCE=$(echo "$SALIDA" | grep "Nonce ganador:" | awk '{print $NF}')
    HASH_GPU=$(echo "$SALIDA" | grep "Hash MD5 (GPU):" | awk '{print $NF}')
    
    echo "$SALIDA" | grep -E "Tiempo total:|Rendimiento:|VALIDACIÓN EXITOSA"
    
    # Verificación cruzada externa con herramienta de sistema (md5sum)
    HASH_MD5SUM=$(printf "%s%s" "$CADENA" "$NONCE" | md5sum | awk '{print $1}')
    
    if [[ "$HASH_GPU" == "$HASH_MD5SUM" && "$HASH_MD5SUM" =~ ^$PREFIJO ]]; then
        echo "  [AUDITORÍA EXTERNA md5sum]: OK ($HASH_MD5SUM coincide y empieza con '$PREFIJO')"
        EXITOSOS=$((EXITOSOS + 1))
    else
        echo "  [AUDITORÍA EXTERNA md5sum]: FALLÓ ($HASH_MD5SUM vs $HASH_GPU)"
        exit 1
    fi
done

echo ""
echo "============================================================"
echo "Resultado final: $EXITOSOS/$TOTAL pruebas verificadas con éxito en GPU, CPU y md5sum."
echo "============================================================"

