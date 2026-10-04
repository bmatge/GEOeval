"""
Vendorisation des ressources front (ADR-088 lot 1.6) — DSFR, Chart.js, dsfr-chart, dsfr-data, Swagger UI.

Les fichiers sont versionnés dans le dépôt sous geoeval/web/static/vendor/ : aucune
dépendance à un CDN à l'exécution (réseau fermé, CSP stricte), aucune chaîne Node.
Ce script sert à (re)produire exactement ce contenu depuis le registre npm et à le
vérifier.

    python -m scripts.vendor_assets --verify     # empreintes SHA-256 des fichiers = MANIFEST.json (CI)
    python -m scripts.vendor_assets --refresh    # re-télécharge les versions épinglées, réécrit MANIFEST.json
    python -m scripts.vendor_assets --refresh --registry https://nexus.interne/repository/npm-proxy/

Pour monter de version : modifier PACKAGES, lancer --refresh, mettre à jour les
gabarits (le chemin contient la version), relire le diff.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import shutil
import sys
import tarfile
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VENDOR_DIR = ROOT / "geoeval" / "web" / "static" / "vendor"
MANIFEST = VENDOR_DIR / "MANIFEST.json"
DEFAULT_REGISTRY = "https://registry.npmjs.org/"


@dataclass(frozen=True)
class Package:
    name: str
    version: str
    license: str
    dest: str                       # dossier sous vendor/
    files: tuple[str, ...]          # fichiers à copier depuis package/
    dirs: tuple[str, ...] = field(default_factory=tuple)  # dossiers entiers à copier
    note: str = ""


PACKAGES: tuple[Package, ...] = (
    Package(
        "@gouvfr/dsfr", "1.13.0", "MIT (sauf fonte Marianne : usage réservé à l'État — CGU DSFR)",
        "dsfr-1.13.0",
        ("dist/dsfr.min.css", "dist/utility/utility.min.css", "dist/dsfr.module.min.js", "dist/dsfr.nomodule.min.js",
         "LICENSE.md"),
        ("dist/fonts", "dist/icons"),
        "dsfr.min.css référence fonts/ et icons/ en relatif ; utility.min.css référence ../icons/.",
    ),
    Package(
        "@gouvfr/dsfr-chart", "2.1.1", "MIT", "dsfr-chart-2.1.1",
        ("dist/DSFRChart/DSFRChart.css", "dist/DSFRChart/DSFRChart.js", "LICENSE"),
    ),
    Package(
        "chart.js", "4.4.1", "MIT", "chartjs-4.4.1",
        ("dist/chart.umd.js", "LICENSE.md"),
        note="dist/chart.umd.js est déjà minifié (jsDelivr servait ce fichier sous le nom chart.umd.min.js).",
    ),
    Package(
        "dsfr-data", "0.46.0", "MIT", "dsfr-data-0.46.0",
        ("dist/dsfr-data.core.umd.js", "dist/fzstd-w50XBdvj.js", "dist/hyparquet-BlvNCtjd.js", "LICENSE"),
        note="Le cœur UMD charge les deux chunks (zstd, parquet) en relatif si une source le demande.",
    ),
    Package(
        "swagger-ui-dist", "5.33.1", "Apache-2.0", "swagger-ui-5.33.1",
        ("swagger-ui-bundle.js", "swagger-ui.css", "favicon-32x32.png", "LICENSE", "NOTICE"),
        note="Documentation interactive de l'API v1 (/api/v1/docs) : servie localement, jamais depuis un CDN.",
    ),
)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _fetch_tarball(pkg: Package, registry: str) -> tuple[bytes, str]:
    meta_url = registry.rstrip("/") + "/" + pkg.name.replace("/", "%2F")
    with urllib.request.urlopen(meta_url, timeout=60) as r:
        meta = json.load(r)
    dist = meta["versions"][pkg.version]["dist"]
    with urllib.request.urlopen(dist["tarball"], timeout=120) as r:
        data = r.read()
    integrity = dist.get("integrity", "")
    if integrity.startswith("sha512-"):
        import base64

        expected = base64.b64decode(integrity[len("sha512-"):])
        if hashlib.sha512(data).digest() != expected:
            raise RuntimeError(f"{pkg.name}@{pkg.version} : intégrité du tarball invalide")
    return data, integrity


def refresh(registry: str) -> dict:
    if VENDOR_DIR.exists():
        shutil.rmtree(VENDOR_DIR)
    VENDOR_DIR.mkdir(parents=True)
    manifest: dict = {"registry": registry, "packages": {}, "files": {}}
    for pkg in PACKAGES:
        data, integrity = _fetch_tarball(pkg, registry)
        dest = VENDOR_DIR / pkg.dest
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
            members = {m.name: m for m in tar.getmembers()}

            def extract(rel: str) -> None:
                name = f"package/{rel}"
                if name not in members:
                    raise FileNotFoundError(f"{pkg.name}@{pkg.version} : {rel} absent du tarball")
                target = dest / rel.removeprefix("dist/")
                target.parent.mkdir(parents=True, exist_ok=True)
                with tar.extractfile(members[name]) as src, target.open("wb") as dst:
                    shutil.copyfileobj(src, dst)

            for rel in pkg.files:
                extract(rel)
            for d in pkg.dirs:
                prefix = f"package/{d}/"
                found = [n for n in members if n.startswith(prefix) and members[n].isfile()]
                if not found:
                    raise FileNotFoundError(f"{pkg.name}@{pkg.version} : dossier {d} absent du tarball")
                for n in found:
                    extract(n.removeprefix("package/"))
        manifest["packages"][pkg.name] = dict(version=pkg.version, license=pkg.license, dest=pkg.dest,
                                              tarball_integrity=integrity, note=pkg.note)
        print(f"{pkg.name}@{pkg.version} → vendor/{pkg.dest}")
    for path in sorted(p for p in VENDOR_DIR.rglob("*") if p.is_file() and p.name != MANIFEST.name):
        manifest["files"][path.relative_to(VENDOR_DIR).as_posix()] = sha256_file(path)
    MANIFEST.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    total = sum((VENDOR_DIR / f).stat().st_size for f in manifest["files"])
    print(f"{len(manifest['files'])} fichiers, {total / 1_048_576:.1f} MiB — MANIFEST.json écrit")
    return manifest


def verify() -> list[str]:
    """Renvoie la liste des anomalies (vide = conforme)."""
    if not MANIFEST.exists():
        return ["MANIFEST.json absent"]
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    problems: list[str] = []
    expected = manifest.get("files", {})
    for rel, digest in expected.items():
        path = VENDOR_DIR / rel
        if not path.exists():
            problems.append(f"manquant : {rel}")
        elif sha256_file(path) != digest:
            problems.append(f"empreinte différente : {rel}")
    for path in VENDOR_DIR.rglob("*"):
        if path.is_file() and path.name != MANIFEST.name:
            rel = path.relative_to(VENDOR_DIR).as_posix()
            if rel not in expected:
                problems.append(f"non déclaré : {rel}")
    for pkg in PACKAGES:
        entry = manifest.get("packages", {}).get(pkg.name)
        if entry is None or entry.get("version") != pkg.version:
            problems.append(f"version manifeste ≠ PACKAGES : {pkg.name}")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Vendorisation des ressources front")
    parser.add_argument("--refresh", action="store_true", help="re-télécharger et réécrire le manifeste")
    parser.add_argument("--verify", action="store_true", help="vérifier les empreintes (défaut)")
    parser.add_argument("--registry", default=DEFAULT_REGISTRY, help="registre npm (miroir Nexus possible)")
    args = parser.parse_args(argv)
    if args.refresh:
        refresh(args.registry)
        return 0
    problems = verify()
    for p in problems:
        print("✗", p)
    print("✓ ressources vendorisées conformes au manifeste" if not problems else f"{len(problems)} anomalie(s)")
    return 0 if not problems else 1


if __name__ == "__main__":
    sys.exit(main())
