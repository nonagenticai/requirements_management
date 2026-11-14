"""ISO 4217 alphabetic currency codes (``pm_projects.budget_currency``; fr cockpit R-CK3).

The active codes of the ISO 4217 list (Table A.1), plus the fund / precious-metal codes it
also carries. A code outside this set is a 422 at the request edge; the column's CHECK only
pins the shape (three upper-case letters), so a later ISO addition needs this set, not a
migration.
"""

from typing import Annotated

from pydantic import AfterValidator, BeforeValidator
from pydantic_core import PydanticCustomError

ISO_4217_CODES: frozenset[str] = frozenset(
    """
    AED AFN ALL AMD ANG AOA ARS AUD AWG AZN BAM BBD BDT BGN BHD BIF BMD BND BOB BOV BRL BSD
    BTN BWP BYN BZD CAD CDF CHE CHF CHW CLF CLP CNY COP COU CRC CUC CUP CVE CZK DJF DKK DOP
    DZD EGP ERN ETB EUR FJD FKP GBP GEL GHS GIP GMD GNF GTQ GYD HKD HNL HTG HUF IDR ILS INR
    IQD IRR ISK JMD JOD JPY KES KGS KHR KMF KPW KRW KWD KYD KZT LAK LBP LKR LRD LSL LYD MAD
    MDL MGA MKD MMK MNT MOP MRU MUR MVR MWK MXN MXV MYR MZN NAD NGN NIO NOK NPR NZD OMR PAB
    PEN PGK PHP PKR PLN PYG QAR RON RSD RUB RWF SAR SBD SCR SDG SEK SGD SHP SLE SLL SOS SRD
    SSP STN SVC SYP SZL THB TJS TMT TND TOP TRY TTD TWD TZS UAH UGX USD USN UYI UYU UYW UZS
    VED VES VND VUV WST XAF XAG XAU XBA XBB XBC XBD XCD XCG XDR XOF XPD XPF XPT XSU XTS XUA
    XXX YER ZAR ZMW ZWG ZWL
    """.split()
)


def _upper(v: object) -> object:
    return v.strip().upper() if isinstance(v, str) else v


def _iso_4217(v: str) -> str:
    if v not in ISO_4217_CODES:
        raise PydanticCustomError(
            "budget_currency_not_iso_4217",
            "budget_currency must be an ISO 4217 alphabetic code (e.g. EUR, USD)",
        )
    return v


# Accepts any case on input ("eur"), stores and returns the upper-case code.
Iso4217Code = Annotated[str, BeforeValidator(_upper), AfterValidator(_iso_4217)]
