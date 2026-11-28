"""BC-105: con el certificado montado, el gateway también atiende por HTTPS; sin él, sólo HTTP."""

import datetime
import socket
import ssl
import time
import urllib.request
from types import SimpleNamespace

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from comun import web
from gateway import app as gateway


def certificado(carpeta):
    """Un certificado autofirmado para localhost: (archivo del certificado, archivo de la clave)."""
    clave = ec.generate_private_key(ec.SECP256R1())
    nombre = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    ahora = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(nombre).issuer_name(nombre).public_key(clave.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(ahora)
            .not_valid_after(ahora + datetime.timedelta(days=1))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost")]), critical=False)
            .sign(clave, hashes.SHA256()))
    archivo_cert, archivo_clave = carpeta / "tls.crt", carpeta / "tls.key"
    archivo_cert.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    archivo_clave.write_bytes(clave.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                                  serialization.NoEncryption()))
    return archivo_cert, archivo_clave


def puerto_libre():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_con_certificado_atiende_por_https(tmp_path, monkeypatch):
    cert, clave = certificado(tmp_path)
    puerto = puerto_libre()
    monkeypatch.setenv("TLS_CERTIFICADO", str(cert))
    monkeypatch.setenv("TLS_CLAVE", str(clave))
    monkeypatch.setenv("PUERTO_TLS", str(puerto))
    servidor = gateway.servir_https(SimpleNamespace(app=web.crear_app("gateway", {})))
    try:
        contexto = ssl.create_default_context(cafile=str(cert))
        limite = time.monotonic() + 10
        while True:
            try:
                with urllib.request.urlopen(f"https://localhost:{puerto}/health", context=contexto, timeout=2) as r:
                    assert (r.status, r.read()) == (200, b'{"gateway":"ok"}')
                break
            except (ConnectionError, urllib.error.URLError):
                if time.monotonic() > limite:
                    raise
                time.sleep(0.1)
    finally:
        servidor.should_exit = True


def test_sin_certificado_no_abre_https(monkeypatch, tmp_path):
    monkeypatch.setenv("TLS_CERTIFICADO", str(tmp_path / "no-esta.crt"))
    monkeypatch.setenv("TLS_CLAVE", str(tmp_path / "no-esta.key"))
    assert gateway.servir_https(SimpleNamespace(app=web.crear_app("gateway", {}))) is None
