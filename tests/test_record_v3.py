"""Record fields from the gazette: subscribed capital, purpose extension, renamed streets,
the comparison of places and the activity description kept apart.

The renamed-street texts follow wordings read in the gazette, with invented streets.
"""

from __future__ import annotations

import sqlite3
from datetime import date

import pytest

from borme_radar import record, store
from borme_radar.record import observations_of_act, place, renamed_street, same_address, same_city
from conftest import watched

FILLER = frozenset({"CALLE", "AVDA", "AVENIDA", "CTRA", "NUMERO", "PISO", "BAJO", "ZONA", "DEL"})


@pytest.fixture(autouse=True)
def _filler(monkeypatch: pytest.MonkeyPatch) -> None:
    # The comparison is tested with a known list; the shipped one is measured (formats).
    monkeypatch.setattr(record, "STREET_FILLER", FILLER)


def obs(act_type: str, detail: str) -> list[tuple[str, str]]:
    return [(o.field, o.value) for o in observations_of_act(act_type, detail)]


def test_incorporation_with_capital_not_paid_up_in_full() -> None:
    detail = (
        "Comienzo de operaciones: 1.10.25. Objeto social: La tenencia de valores. "
        "Domicilio: C/ EJEMPLO 19 (MARBELLA). Capital suscrito: 10.000.000,00 Euros. "
        "Desembolsado: 4.000.000,00 Euros"
    )
    assert obs("incorporation", detail) == [
        ("purpose", "La tenencia de valores"),
        ("address", "C/ EJEMPLO 19"),
        ("city", "MARBELLA"),
        ("capital", "10000000.00"),
    ]


def test_an_extension_of_the_purpose_is_not_the_purpose() -> None:
    assert obs("business_purpose_change", "La venta de bicicletas") == [
        ("purpose", "La venta de bicicletas")
    ]
    assert obs("business_purpose_extension", "Intermediación financiera") == []


@pytest.mark.parametrize(
    ("detail", "street"),
    [
        (
            "Cambiada la denominación de la calle del domicilio social, siendo la actual "
            '"Calle Ejemplo Uno, número 4"',
            "Calle Ejemplo Uno, número 4",
        ),
        (
            "CONSTANCIA DEL CAMBIO DE NOMBRE DE LA VIA DEL DOMICILIO SOCIAL, QUEDANDO "
            "ESTABLECIDO EN: CL EJEMPLO DOS NUM.4 BAJOS DE CIUDAD",
            "CL EJEMPLO DOS NUM.4 BAJOS DE CIUDAD",
        ),
        (
            "Por cambio de numeración, efectuado por el Ayuntamiento, de la calle del "
            "domicilio social, éste pasa a estar en la Zona Industrial Norte, calle Ejemplo "
            "Tres, número 1 B, de Ciudad",
            "calle Ejemplo Tres, número 1 B, de Ciudad",
        ),
        (
            "Se hace constar el cambio de nombre de la calle donde se halla ubicado el "
            "domicilio social, que de calle Vieja, 7 ha pasado a denominarse calle Nueva "
            "Ejemplo, número 7",
            "calle Nueva Ejemplo, número 7",
        ),
        (
            "Nuevo domicilio social en Avda Ejemplo Cuatro, 54, bajo por cambio de "
            "denominación de la calle",
            "Avda Ejemplo Cuatro, 54, bajo",
        ),
        (
            'El nombre de la calle del domicilio social es ahora "Avenida Parque Ejemplo '
            "(PE) 1, planta 4, 04131 por modificación en el Nomenclátor Municipal",
            "Avenida Parque Ejemplo (PE) 1, planta 4, 04131",
        ),
        (
            "Ha cambiado la denominación del domicilio social, antes Carretera de Ejemplo, "
            "km. 12,500, ahora Avenida Ejemplo Norte, 45",
            "Avenida Ejemplo Norte, 45",
        ),
        (
            "CAMBIO DE NOMENCLATURA DEL DOMICILIO SOCIAL. En lo sucesivo: Avenida Ejemplo "
            "Sur, nº 7",
            "Avenida Ejemplo Sur, nº 7",
        ),
        (
            "Por renumeración de los inmuebles del municipio, el inmueble donde la sociedad "
            "tiene su domicilio queda establecido como Parque Empresarial Ejemplo número 16",
            "Parque Empresarial Ejemplo número 16",
        ),
        (
            "Se modifica la numeración de la calle del domicilio social, pasando a ser "
            "CALLE EJEMPLO CINCO, 37, DE CIUDAD. Ciudad, a 25 de enero de 2017",
            "CALLE EJEMPLO CINCO, 37, DE CIUDAD",
        ),
    ],
)
def test_street_renamed_by_the_city_council(detail: str, street: str) -> None:
    assert renamed_street(detail) == street
    assert obs("other", detail) == [("address", street)]


@pytest.mark.parametrize(
    "detail",
    [
        "HA CAMBIADO LA DENOMINACION DE LA SOCIEDAD MATRIZ",
        "Se da nueva redacción al artículo 4º del domicilio social",
        # Only the new name, or only the new number: not an address by itself.
        "Cambiado el nombre de la calle del domicilio social, siendo la actual Calle Ejemplo",
        "CAMBIADA LA NUMERACION DE LA CALLE DEL DOMICILIO SOCIAL, ANTES ERA EL NUMERO 26 DE "
        "LA CALLE EJEMPLO Y EN LA ACTUALIDAD ES EL NUMERO 52",
        # A correction of an earlier inscription, not a council renaming the street.
        "Se rectifica el nombre de la calle del domicilio social que ha de ser: CALLE "
        "EJEMPLO, Nº 12",
        # Someone else's domicile; articles and shares renumbered.
        "CONSTANCIA DEL CAMBIO EN LA NUMERACION DEL DOMICILIO DEL ADMINISTRADOR DON PERSONA UNO",
        "SE SUBSANA LA RENUMERACION DE ARTICULOS DE LOS ESTATUTOS, CORRESPONDIENDO EL "
        'ARTICULO 2 AL "DOMICILIO"',
        "Lugar del acuerdo: el domicilio social. Renumeración de las participaciones de la 1 "
        "a la 3.006",
    ],
)
def test_other_free_text_is_not_a_renamed_street(detail: str) -> None:
    assert renamed_street(detail) is None


@pytest.mark.parametrize(
    ("record_value", "registry", "same"),
    [
        ("CALLE EJEMPLO, 11 - PISO 2 A", "C/ EJEMPLO 11 2º A", True),
        ("CALLE ROMERO 38", "C/ ROMERO, 38 06240", True),
        ("AVENIDA EJEMPLO 21", "AV EJEMPLO 21-23", True),
        ("CARRETERA N-340 KM 28,400", "CTRA N-340 KM 28.4", True),
        ("CALLE EJEMPLO 9", "C/ EJEMPLO NUMERO 9", True),
        ("CALLE EJEMPLO 5, 2", "C/ EJEMPLO 5", True),
        ("CALLE ROMERO 38", "C/ ROMERO 40", False),
        ("CALLE ROMERO 38", "AVDA DEL PUERTO 38", False),
        ("CALLE EJEMPLO S/N", "C/ EJEMPLO 5", False),
        ("CALLE EJEMPLO 5 PISO 2", "C/ EJEMPLO 5 PISO 3", False),
        ("", "C/ EJEMPLO 5", False),
    ],
)
def test_same_address_tolerates_spelling_not_moves(
    record_value: str, registry: str, same: bool
) -> None:
    assert same_address(record_value, registry) is same
    if record_value:
        assert record.differs("address", record_value, registry) is not same


def test_the_parts_compared() -> None:
    assert place("CTRA N-340 KM 28,400 NAVE 3") == (
        "28.4",
        frozenset({"3"}),
        frozenset({"N340", "NAVE"}),
        None,
        True,
    )
    # On a road the kilometre point is the number, even after the road's own number.
    assert place("CTRA NACIONAL 340 KM 12").portal == "12"


def test_a_street_cut_by_the_registro_keeps_matching() -> None:
    cut = "AVDA EJEMPLO DE LAS PALMERAS, S/N - ZONA INDUSTRIA"  # 50 characters, cut
    assert len(cut) == 50
    assert same_address("AVENIDA EJEMPLO DE LAS PALMERAS S/N ZONA INDUSTRIAL", cut)
    assert not same_address("AVENIDA EJEMPLO DE LAS PALMERAS S/N ZONA PORTUARIA", cut)


def test_same_city_in_any_official_form() -> None:
    assert same_city("DONOSTIA/SAN SEBASTIAN", "San Sebastian")
    assert same_city("San Sebastián", "DONOSTIA/SAN SEBASTIAN")
    assert not same_city("VALENCIA", "San Sebastian")
    assert not record.differs("city", "Toledo", "TOLEDO")


def test_sync_leaves_out_the_same_place_spelled_otherwise(conn: sqlite3.Connection) -> None:
    store.import_watchlist(
        conn,
        [watched("G", "ACME, S.A.", address="CALLE EJEMPLO, 11 - PISO 2", city="MADRID")],
        date(2026, 1, 1),
    )
    for field, value in (("address", "C/ EJEMPLO 11"), ("city", "Madrid")):
        conn.execute(
            "INSERT INTO seen_values (company_id, field, value, day, source, added_at)"
            " VALUES (1, ?, ?, '2026-02-01', 'registry', 'x')",
            (field, value),
        )
    assert record.sync(conn) == []
    assert record.spelled_otherwise(conn) == 1  # the city is equal as plain text


def test_the_shipped_filler_lists_street_types_and_entrance_words() -> None:
    from borme_radar.normalize import word_list

    shipped = word_list("street_filler.txt")
    assert {"CALLE", "AVDA", "PISO", "PLANTA", "LOCAL", "PUERTA"} <= shipped
    assert not {"MAYOR", "GOYA", "MADRID"} & shipped  # names of streets and towns
