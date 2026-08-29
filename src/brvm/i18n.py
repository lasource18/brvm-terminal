"""UI translation catalog + resolver.

Deliberately simple: a nested dict `TRANSLATIONS[locale][source_string]`
looked up at render time by the `t` Jinja filter. Missing keys fall back
to the source string — that's how new templates ship in English by
default and get French coverage incrementally without ever crashing
render. Source strings ARE the keys (as in `gettext`) so adding a new
label doesn't need a separate id registry.

Kept as a plain Python module (not `.po` catalogs) because:
- single-user, ~two locales, and coverage is small enough that a full
  gettext toolchain adds more friction than it removes
- catalog changes ride the same PR review that added the string
- no compile step, no runtime file I/O, no cache invalidation

Storage layer (news bodies, filings text) stays in the source language
per CLAUDE.md — this catalog only translates UI chrome + operator-authored
copy (glossaries, footnotes).
"""

from __future__ import annotations

from typing import Literal

Locale = Literal["en", "fr"]
SUPPORTED_LOCALES: tuple[Locale, ...] = ("en", "fr")
DEFAULT_LOCALE: Locale = "en"


# French translations. Source string → French copy. Add entries as
# templates get their FR coverage pass; anything absent falls through to
# the English source string.
_FR: dict[str, str] = {
    # ---- topbar / global chrome ----
    "Overview": "Aperçu",
    "Directory": "Répertoire",
    "News": "Actualités",
    "Watchlists": "Listes de suivi",
    "Alerts": "Alertes",
    "Brief": "Résumé",
    "Search ticker or name…": "Rechercher un ticker ou nom…",
    "MARKET OPEN": "MARCHÉ OUVERT",
    "MARKET CLOSED": "MARCHÉ FERMÉ",

    # ---- Peers ratio glossary ----
    "Ratio glossary": "Glossaire des ratios",
    "formulas + reading guide": "formules + guide de lecture",
    "Price / earnings per share.": "Cours / bénéfice par action.",
    "How many years of current EPS the market is paying for a share.":
        "Nombre d'années de BPA courant que le marché paie pour une action.",
    "Lower = cheaper on earnings; negative EPS → suppressed "
    "(a negative multiple is a trap for a skim-reader).":
        "Plus bas = moins cher sur les bénéfices ; BPA négatif → masqué "
        "(un multiple négatif induit en erreur à la lecture rapide).",
    "Net income / total equity.": "Résultat net / capitaux propres.",
    "Return the company generates on shareholder capital.":
        "Rendement dégagé par la société sur les capitaux des actionnaires.",
    "Sector-relative — banks/insurers structurally sit higher than industrials.":
        "À évaluer par secteur — banques et assureurs affichent structurellement "
        "des niveaux plus élevés que l'industrie.",
    "Net income / revenue.": "Résultat net / chiffre d'affaires.",
    "How many CFA of profit per CFA of revenue.":
        "Combien de CFA de bénéfice par CFA de revenu.",
    "Reflects operating efficiency + tax + non-op items combined.":
        "Reflète l'efficacité opérationnelle + fiscalité + éléments non "
        "opérationnels combinés.",
    "Market cap / free cash flow.":
        "Capitalisation boursière / flux de trésorerie disponible.",
    "Cash-flow analog of P/E.": "Équivalent en trésorerie du P/E.",
    "Suppressed when FCF is negative (a negative multiple is meaningless).":
        "Masqué lorsque le FCF est négatif (un multiple négatif n'a pas de sens).",
    "Complements P/E because accounting earnings and cash generation can diverge.":
        "Complète le P/E car les bénéfices comptables et la génération de "
        "trésorerie peuvent diverger.",
    "Free cash flow / market cap":
        "Flux de trésorerie disponible / capitalisation boursière",
    "Cash-flow yield to the equity holder.":
        "Rendement en trésorerie pour l'actionnaire.",
    "Negative values are shown (informative: the company is burning cash).":
        "Les valeurs négatives sont affichées (informatif : la société brûle "
        "de la trésorerie).",
    "Compare against local risk-free rates + coupon yields on the Bonds tab.":
        "À comparer aux taux sans risque locaux et aux rendements de coupons "
        "sur l'onglet Obligations.",
    "Enterprise value / EBITDA.":
        "Valeur d'entreprise / EBITDA.",
    "Capital-structure-neutral valuation multiple.":
        "Multiple de valorisation neutre vis-à-vis de la structure financière.",
    "Central tendency across non-self peers. Both stay blank when fewer than 2 peers "
    "report a field (a single sample isn't a sector reference).":
        "Tendance centrale sur les pairs hors ligne courante. Restent vides "
        "quand moins de 2 pairs renseignent le champ (un seul échantillon n'est "
        "pas une référence sectorielle).",

    # ---- Bond yield glossary ----
    "Yield & risk glossary": "Glossaire rendement & risque",
    "Clean price": "Cours pied de coupon",
    "Accrued coupon": "Coupon couru",
    "Dirty price": "Cours plein coupon",
    "Current yield": "Rendement courant",
    "Yield to maturity": "Rendement à l'échéance",
    "Macaulay duration": "Duration de Macaulay",
    "Modified duration": "Duration modifiée",
    "Convexity": "Convexité",
    "All figures assume bullet redemption and the inferred coupon cadence — "
    "see the Cash flow tab for the exact schedule and the anchor date.":
        "Toutes les valeurs supposent un remboursement in fine et la cadence "
        "de coupon inférée — voir l'onglet Flux de trésorerie pour l'échéancier "
        "exact et la date d'ancrage.",
}


_CATALOGS: dict[Locale, dict[str, str]] = {
    "en": {},   # source == translation; entries only if we ever need overrides
    "fr": _FR,
}


def normalize(candidate: str | None) -> Locale:
    """Coerce a user-supplied locale code to a supported one.

    Accepts common variants (case, `fr-FR`, `fr_FR`) and falls back to
    `DEFAULT_LOCALE` for anything unknown. Never raises — a bad cookie
    shouldn't 500 a page render."""
    if not candidate:
        return DEFAULT_LOCALE
    code = candidate.strip().lower().replace("_", "-").split("-", 1)[0]
    if code in SUPPORTED_LOCALES:
        return code  # type: ignore[return-value]
    return DEFAULT_LOCALE


def translate(source: str, locale: Locale = DEFAULT_LOCALE) -> str:
    """Return the translation for `source` under `locale`, or `source`
    itself when no translation is registered. The fallback is what lets
    templates ship English strings incrementally without breaking any
    active locale."""
    return _CATALOGS.get(locale, {}).get(source, source)
