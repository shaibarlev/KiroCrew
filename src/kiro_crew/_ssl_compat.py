"""Ensure the system CA bundle is visible to OpenSSL before any HTTPS call.

Must run before any library (aiohttp, slack_sdk, requests) caches its
default SSL context, otherwise every HTTPS call fails with
CERTIFICATE_VERIFY_FAILED.

Two distinct gaps, one fix: on Amazon Linux dev-desktops, mise-installed
Python looks for certs at ``/etc/ssl/`` but Amazon Linux stores them at
``/etc/pki/tls/`` (the ``_CA_CANDIDATES`` file paths below). On macOS, a
python.org/Homebrew/pyenv-built CPython ships no bundled CA store at all and
does not read the OS Keychain via any of these file paths — the standard
fix there is ``certifi``'s bundled Mozilla root store, already present as a
transitive dependency (``pip``, ``requests``, ``slack-sdk`` all pull it) but
never installed as a direct one, so it is used only as a soft fallback: if
it is not importable, this function is a no-op on that platform, matching
prior behavior.

On Windows behind a corporate proxy that performs SSL inspection, the Rust
TLS library in kiro-cli uses webpki-roots (Mozilla's root store) rather than
the native Windows certificate store, so connections fail with
``InvalidCertificate(UnknownIssuer)``. This module generates a combined CA
bundle that includes both the Mozilla roots (via certifi) and any extra
certificates from the Windows system store that are not already in the
Mozilla set, then points ``SSL_CERT_FILE`` at it so both Python libraries
and spawned kiro-cli processes trust the corporate CA chain.
"""

from __future__ import annotations

import hashlib
import logging
import os
import sys
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)

_CA_CANDIDATES = (
    "/etc/pki/tls/cert.pem",
    "/etc/pki/tls/certs/ca-bundle.crt",
    "/etc/ssl/certs/ca-certificates.crt",
)


def _windows_extra_ca_certs() -> list[str]:
    """Export non-Mozilla CA certificates from the Windows system store as PEM.

    Reads from both the Root and CA stores, returning PEM-encoded certificates
    that are NOT already in the certifi bundle (to avoid duplicates). Returns
    an empty list on failure or when no extra certs are found.
    """
    try:
        import ssl

        import certifi
    except ImportError:
        return []

    # Load the set of DER fingerprints already in certifi so we only export
    # certificates the Mozilla bundle does not contain.
    certifi_fingerprints: set[str] = set()
    try:
        ctx = ssl.create_default_context(cafile=certifi.where())
        for cert_der in ctx.get_ca_certs(binary_form=True):
            certifi_fingerprints.add(hashlib.sha256(cert_der).hexdigest())
    except Exception:
        pass

    extra_pems: list[str] = []
    try:
        # ssl.enum_certificates is Windows-only and returns
        # (cert_der_bytes, encoding_type, trust_oids)
        for store_name in ("ROOT", "CA"):
            for cert_der, encoding, _trust in ssl.enum_certificates(store_name):
                if encoding != "x509_asn":
                    continue
                fp = hashlib.sha256(cert_der).hexdigest()
                if fp in certifi_fingerprints:
                    continue
                # Convert DER to PEM
                import base64

                b64 = base64.b64encode(cert_der).decode("ascii")
                lines = [b64[i : i + 64] for i in range(0, len(b64), 64)]
                pem = (
                    "-----BEGIN CERTIFICATE-----\n"
                    + "\n".join(lines)
                    + "\n-----END CERTIFICATE-----\n"
                )
                extra_pems.append(pem)
                certifi_fingerprints.add(fp)
    except (AttributeError, OSError):
        # ssl.enum_certificates not available (non-Windows) or store inaccessible
        pass

    return extra_pems


def _build_combined_bundle(bundle_path: Path) -> bool:
    """Create a combined CA bundle at ``bundle_path`` (certifi + Windows store).

    Returns True if the bundle was created or already exists and is fresh,
    False on failure.
    """
    try:
        import certifi
    except ImportError:
        return False

    certifi_path = Path(certifi.where())
    if not certifi_path.exists():
        return False

    # Check if the bundle already exists and is newer than certifi's bundle.
    # Regenerate when certifi is updated (e.g. after a pip upgrade).
    if bundle_path.exists():
        try:
            if bundle_path.stat().st_mtime >= certifi_path.stat().st_mtime:
                return True
        except OSError:
            pass

    extra_certs = _windows_extra_ca_certs()
    if not extra_certs:
        # No extra certs needed — just point at certifi directly
        return False

    try:
        bundle_path.parent.mkdir(parents=True, exist_ok=True)
        # Write atomically: write to a temp file then rename
        fd, tmp_path = tempfile.mkstemp(
            dir=str(bundle_path.parent), suffix=".pem.tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                # Write the certifi base bundle
                f.write(certifi_path.read_text(encoding="utf-8"))
                # Append the Windows store extras
                f.write("\n# === Windows System Store CA Certificates ===\n\n")
                for pem in extra_certs:
                    f.write(pem)
                    f.write("\n")
            # Atomic rename (on Windows, remove target first if it exists)
            tmp = Path(tmp_path)
            if bundle_path.exists():
                bundle_path.unlink()
            tmp.rename(bundle_path)
        except BaseException:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
    except Exception:
        logger.debug("failed to build combined CA bundle", exc_info=True)
        return False

    return True


def _ensure_ssl_certs() -> None:
    """Point OpenSSL at a working CA bundle before any library caches it.

    Sets both ``SSL_CERT_FILE`` (used by OpenSSL / aiohttp / rustls in
    kiro-cli) and ``REQUESTS_CA_BUNDLE`` (used by the ``requests`` library /
    slack_sdk).

    On Windows, also generates a combined bundle that merges certifi's Mozilla
    roots with certificates from the Windows system store (Root + CA), so that
    corporate proxy CAs installed in the system store are trusted by both
    Python libraries and spawned Rust binaries (kiro-cli) that read
    ``SSL_CERT_FILE``.
    """
    if os.environ.get("SSL_CERT_FILE"):
        return

    import ssl

    defaults = ssl.get_default_verify_paths()
    if defaults.cafile and Path(defaults.cafile).exists():
        return

    for candidate in _CA_CANDIDATES:
        if Path(candidate).exists():
            os.environ["SSL_CERT_FILE"] = candidate
            os.environ.setdefault("REQUESTS_CA_BUNDLE", candidate)
            return

    # Windows: build a combined bundle from certifi + system store CAs.
    # Corporate proxies performing SSL inspection install their CA in the
    # Windows certificate store, but kiro-cli's Rust TLS (rustls/webpki)
    # reads SSL_CERT_FILE rather than the native store, causing
    # InvalidCertificate(UnknownIssuer) failures without this.
    if sys.platform == "win32":
        from kiro_crew.config.paths import config_dir

        bundle_path = config_dir() / "ca-bundle.pem"
        if _build_combined_bundle(bundle_path):
            os.environ["SSL_CERT_FILE"] = str(bundle_path)
            os.environ.setdefault("REQUESTS_CA_BUNDLE", str(bundle_path))
            return

    # Fall back to certifi's bundle if it happens to be installed transitively.
    try:
        import certifi

        bundle = certifi.where()
    except ImportError:
        return
    if Path(bundle).exists():
        os.environ["SSL_CERT_FILE"] = bundle
        os.environ.setdefault("REQUESTS_CA_BUNDLE", bundle)
