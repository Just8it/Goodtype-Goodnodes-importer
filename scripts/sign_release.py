"""Sign the built release descriptor in the protected publisher job, using OpenSSL Ed25519."""
import json
import os
from pathlib import Path
import subprocess
import tempfile


def sign(directory, public_key, private_key):
    descriptor, = Path(directory).glob("goodtype-goodnotes-importer-*-windows-x64.json")
    data = descriptor.read_bytes()
    manifest = json.loads(data)
    if manifest["sourceDirty"] or manifest["sourceCommit"] != os.environ["GITHUB_SHA"]:
        raise ValueError("only the verified clean publishing commit may be signed")
    update = Path(directory)/"importer-update.json"
    signature = Path(directory)/"importer-update.json.sig"
    if update.exists() or signature.exists():
        raise FileExistsError("signed release metadata already exists")
    with tempfile.TemporaryDirectory(prefix="goodtype-release-sign-") as temporary:
        secret = Path(temporary)/"key.pem"
        descriptor_fd = os.open(secret, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor_fd, "w", encoding="ascii") as stream:
            stream.write(private_key)
        update.write_bytes(data)
        try:
            subprocess.run(["openssl", "pkeyutl", "-sign", "-rawin", "-inkey", str(secret),
                            "-in", str(update), "-out", str(signature)], check=True)
            subprocess.run(["openssl", "pkeyutl", "-verify", "-rawin", "-pubin", "-inkey", str(public_key),
                            "-in", str(update), "-sigfile", str(signature)], check=True, stdout=subprocess.DEVNULL)
            if signature.stat().st_size != 64:
                raise ValueError("unexpected Ed25519 signature length")
        except Exception:
            update.unlink(missing_ok=True)
            signature.unlink(missing_ok=True)
            raise


if __name__ == "__main__":
    sign(Path("dist"), Path("release-public-key.pem"), os.environ["IMPORTER_SIGNING_KEY"])
