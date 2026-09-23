"""Trim a real BORME XML document into a small, pseudonymised test fixture.

Usage::

    python scripts/make_fixture.py SOURCE.xml OUT.xml --numbers 423011 423013 ...

Keeps the document metadata and the selected announcements, and replaces names of
natural persons in ``Role: NAME;NAME.`` lists (directors, attorneys, sole shareholders,
judges...) with ``PERSONA N``. Company names are public data about legal persons and
are kept. Free text is not rewritten: review the printed output before committing.
"""

from __future__ import annotations

import argparse
import re
import xml.etree.ElementTree as ET
from pathlib import Path

from borme_radar.normalize import base_and_form, normalize_name

# "Role: VALUE." lists whose values are not people.
_NOT_PEOPLE = {
    "Artículo de los estatutos",
    "Capital",
    "Capital suscrito",
    "Comienzo de operaciones",
    "Denominación y forma adoptada",
    "Desembolsado",
    "Domicilio",
    "Expediente numero",
    "Fecha de mandamiento",
    "FIRME",
    "Importe reducción",
    "Juzgado",
    "Objeto social",
    "Resoluciones",
    "Resultante Desembolsado",
    "Resultante Suscrito",
    "Sociedad absorbente",
    "Sociedades absorbidas",
    "Sociedades beneficiarias de la escisión",
    "Suscrito",
}
_ENTITY_WORDS = (
    "SOCIEDAD",
    "AUDITOR",
    "FUNDACION",
    "ASOCIACION",
    "BANCO",
    "CAJA",
    "AYUNTAMIENTO",
    "UNIVERSIDAD",
    "LIMITED",
    "GMBH",
    "INC",
    "LLC",
    "LTD",
    "BV",
    "SARL",
    "SAS",
)
# A list ends at the full stop that precedes the next role or heading (a capitalised
# word such as "Presidente:" or "Nombramientos."), so "UNO CORP. LATINOAMERICA SA"
# stays one name.
_ROLE_LIST_RE = re.compile(
    r"(?P<role>[A-Z][\w.áéíóúñ ]{0,40}?): (?P<names>[A-ZÁÉÍÓÚÑÜÇ][A-ZÁÉÍÓÚÑÜÇ0-9 ,;'&.\-]*?)"
    r"\.(?=\s+[A-ZÁÉÍÓÚ][a-záéíóúñ]|\s*$)"
)


def _is_entity(name: str) -> bool:
    norm = normalize_name(name)
    if base_and_form(norm)[1] is not None:
        return True
    return any(word in norm.split() for word in _ENTITY_WORDS)


def pseudonymise(text: str, mapping: dict[str, str]) -> str:
    def replace(match: re.Match[str]) -> str:
        role = match.group("role").strip()
        if role in _NOT_PEOPLE:
            return match.group(0)
        out: list[str] = []
        for raw in match.group("names").split(";"):
            name = raw.strip()
            if not name or _is_entity(name):
                out.append(name)
            else:
                out.append(mapping.setdefault(name, f"PERSONA {len(mapping) + 1}"))
        return f"{match.group('role')}: {';'.join(out)}."

    return _ROLE_LIST_RE.sub(replace, text)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("source", type=Path)
    parser.add_argument("out", type=Path)
    parser.add_argument("--numbers", type=int, nargs="+", required=True)
    args = parser.parse_args()

    root = ET.parse(args.source).getroot()
    texto = root.find("texto")
    assert texto is not None
    paragraphs = list(texto)
    for child in paragraphs:
        texto.remove(child)

    wanted = set(args.numbers)
    keep = False
    mapping: dict[str, str] = {}
    for p in paragraphs:
        text = "".join(p.itertext())
        if p.get("class") == "articulo":
            keep = int(text.split("-", 1)[0]) in wanted
        if keep:
            if p.get("class") != "articulo":
                p.text = pseudonymise(text, mapping)
                for sub in list(p):
                    p.remove(sub)
            texto.append(p)
            print(p.text)

    ET.indent(root)
    xml = ET.tostring(root, encoding="unicode")
    notice = (
        "<!-- Source: Agencia Estatal Boletin Oficial del Estado (https://www.boe.es). "
        "MODIFIED copy: trimmed to a few announcements and natural-person names replaced "
        "by PERSONA N (scripts/make_fixture.py). fecha_actualizacion kept as published. -->"
    )
    args.out.write_text(
        f'<?xml version="1.0" encoding="UTF-8"?>\n{notice}\n{xml}\n', encoding="utf-8"
    )
    print(f"\nwrote {args.out} ({args.out.stat().st_size} bytes), {len(mapping)} names replaced")


if __name__ == "__main__":
    main()
