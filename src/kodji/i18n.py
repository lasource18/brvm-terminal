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

    # ---- sign-in (PR-X2) ----
    "Sign in": "Connexion",
    "Sign out": "Déconnexion",
    "Email address": "Adresse e-mail",
    "Send me a link": "Envoyez-moi un lien",
    "Check your email": "Consultez votre messagerie",
    "6-digit code": "Code à 6 chiffres",
    "Send another link": "Envoyer un nouveau lien",
    "Continue as": "Continuer en tant que",
    "We sent a sign-in link and a code to":
        "Nous avons envoyé un lien de connexion et un code à",
    "No password. We email you a link and a 6-digit code — either one "
    "signs you in.":
        "Sans mot de passe. Nous vous envoyons un lien et un code à "
        "6 chiffres : l'un ou l'autre vous connecte.",
    "Open the link on this device, or type the code below if you're "
    "reading your mail somewhere else.":
        "Ouvrez le lien sur cet appareil, ou saisissez le code ci-dessous "
        "si vous consultez votre messagerie ailleurs.",
    "That doesn't look like an email address.":
        "Cette adresse e-mail ne semble pas valide.",
    "We couldn't send that email just now. Try again in a moment.":
        "L'envoi de l'e-mail a échoué. Réessayez dans un instant.",
    "That link has expired or has already been used. Ask for a new one.":
        "Ce lien a expiré ou a déjà été utilisé. Demandez-en un nouveau.",
    "That code isn't right, or it has expired. Ask for a new one.":
        "Ce code est incorrect ou a expiré. Demandez-en un nouveau.",

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

    # ---- security page tab labels ----
    "Chart": "Graphique",
    "Cash flow": "Flux de trésorerie",
    "Yield & Duration": "Rendement & duration",
    "Related bonds": "Obligations liées",
    "Description": "Description",
    "Peers": "Pairs",
    "Corporate actions": "Événements sur titre",
    "Financials": "États financiers",
    "Ownership": "Actionnariat",
    "Segments": "Segments",
    "Analyst view": "Vue analyste",

    # ---- overview (homepage) ----
    "Turnover leaders": "Volumes d'échange",
    "Gainers": "Hausses",
    "Losers": "Baisses",
    "Calendar · next 30d": "Calendrier · 30 prochains jours",
    "TICKER": "TICKER",
    "LAST": "DERNIER",
    "CHG%": "VAR%",
    "DAY%": "JOUR%",
    "YTD%": "YTD%",
    "VOLUME": "VOLUME",
    "TURNOVER": "MONTANT",
    "DATE": "DATE",
    "KIND": "TYPE",
    "AMOUNT": "MONTANT",
    "NAME": "NOM",
    "TBD": "À définir",
    "No data. Run": "Aucune donnée. Lancez",
    "No upcoming actions.": "Aucun événement à venir.",
    "generated": "généré",
    "last snapshot": "dernier snapshot",
    "auto-refresh every": "actualisation auto toutes les",
    "STALE": "OBSOLÈTE",
    "more": "de plus",
    "no data": "aucune donnée",

    # ---- brief page ----
    "machine-generated": "généré par IA",
    "Archive": "Archive",
    "Empty.": "Vide.",
    "No brief has been generated yet. Run":
        "Aucun résumé n'a encore été généré. Lancez",
    "after market close (or wait for the scheduled 15:30 Africa/Abidjan job on a weekday).":
        "après la clôture (ou attendez le job planifié à 15h30 Africa/Abidjan en semaine).",

    # ---- security header + kind badges ----
    "source": "source",
    "Vol": "Vol",
    "Turnover": "Montant",
    # Rendered from `{{ sec.kind|capitalize }}` — translate the capitalised form
    "Equity": "Action",
    "Bond": "Obligation",
    "Index": "Indice",

    # ---- description tab ----
    "Profile": "Profil",
    "Snapshot": "Aperçu",
    "Main shareholders": "Principaux actionnaires",
    "Sector": "Secteur",
    "Industry": "Industrie",
    "Address": "Adresse",
    "Phone": "Téléphone",
    "Fax": "Fax",
    "Email": "Courriel",
    "Website": "Site web",
    "Shares outstanding": "Actions en circulation",
    "Float": "Flottant",
    "Market cap": "Capitalisation",
    "Leadership": "Direction",
    "No description available from this source.":
        "Aucune description disponible depuis cette source.",
    "No profile data available for this security yet.":
        "Aucune donnée de profil disponible pour ce titre.",

    # ---- chart tab ----
    "Loading chart…": "Chargement du graphique…",

    # ---- news tab ----
    "Open in full news view →": "Ouvrir dans la vue Actualités complète →",

    # ---- bond overview tab ----
    "Issuer": "Émetteur",
    "Category": "Catégorie",
    "Country": "Pays",
    "Coupon": "Coupon",
    "Issue date": "Date d'émission",
    "Maturity": "Échéance",
    "Nominal": "Nominal",
    "assumed": "supposé",
    "Latest price": "Dernier cours",
    "Last payment": "Dernier paiement",
    "Next coupon": "Prochain coupon",
    "left": "restants",
    "Related": "Liens",
    "Issuer equity": "Action de l'émetteur",
    "Prospectus": "Prospectus",
    "View": "Consulter",
    "Prospectus / admission news": "Actualités prospectus / admission",
    "No bond data available for this ticker.":
        "Aucune donnée d'obligation disponible pour ce ticker.",
    "annual": "annuel",
    "semi-annual": "semi-annuel",
    "quarterly": "trimestriel",
    "bullet, annual": "in fine, annuel",
    "residual (inferred from last coupon":
        "résidu (inféré du dernier coupon",
    "vs. 10 000 XOF issuance": "vs. émission à 10 000 XOF",

    # ---- bond cash flow tab ----
    "COUPON": "COUPON",
    "PRINCIPAL": "PRINCIPAL",
    "TOTAL": "TOTAL",
    "Cash-flow schedule unavailable — this bond is missing coupon rate, maturity year, or a coupon-date anchor.":
        "Échéancier indisponible — coupon, année d'échéance ou date d'ancrage manquants.",
    "Schedule assumes": "L'échéancier suppose",
    "bullet redemption": "un remboursement in fine",
    "coupons": "coupons",
    "at a nominal of": "à un nominal de",
    "annual coupon": "coupon annuel",
    "Coupon cadence is inferred from the exchange's last-coupon amount vs. price — verify against the prospectus for atypical issues.":
        "La cadence de coupon est inférée à partir du dernier coupon publié par la bourse et du cours — à vérifier avec le prospectus pour les émissions atypiques.",
    "Coupon anchor:": "Ancrage du coupon :",
    "last-payment anniversary": "anniversaire du dernier paiement",
    "issue-date anniversary": "anniversaire de la date d'émission",

    # ---- bond yield tab (panels + footer) ----
    "Price": "Cours",
    "Yield": "Rendement",
    "Duration & convexity": "Duration & convexité",
    "yrs": "ans",
    "Yield analytics unavailable — missing price or coupon information.":
        "Analytique de rendement indisponible — cours ou coupon manquants.",
    "YTM solved via bisection on the bullet +":
        "TRA résolu par bissection sur l'échéancier in fine +",
    "cash-flow schedule": "flux de trésorerie",
    "coupons remaining": "coupons restants",
    "Duration is": "La duration est",
    "in years — scales price sensitivity to a small parallel shift in the yield.":
        "en années — mesure la sensibilité du cours à un petit déplacement parallèle du rendement.",

    # ---- bond related tab ----
    "Related bonds (same issuer)": "Obligations liées (même émetteur)",
    "matured": "arrivée à échéance",
    "MATURITY": "ÉCHÉANCE",
    "COUNTRY": "PAYS",
    "All bonds from": "Toutes les obligations de",
    "this issuer": "cet émetteur",
    "sorted by maturity. Matured issues appear dimmed for context.":
        "triées par échéance. Les obligations arrivées à échéance apparaissent estompées.",
    "No other bonds from": "Aucune autre obligation de",
    "are currently listed.": "n'est actuellement cotée.",

    # ---- financials / ownership / segments empty states ----
    "No extracted financials on file yet.":
        "Aucun état financier extrait pour le moment.",
    "No ownership data extracted": "Aucune donnée d'actionnariat extraite",
    "No segment breakdown": "Aucune ventilation par segment",

    # ---- financials tab (extended coverage) ----
    "Annual · amounts as reported · currency varies by period (see column headers) · extracted from filings":
        "Annuel · montants tels que rapportés · devise variable par période "
        "(voir les en-têtes) · extrait des dépôts",
    "Annual · amounts in": "Annuel · montants en",
    "extracted from filings": "extrait des dépôts",
    "METRIC": "MÉTRIQUE",
    "No extracted financials yet for":
        "Aucun état financier extrait pour le moment pour",
    "Runs after the daily fundamentals-extract job processes an annual filing.":
        "Généré après l'exécution du job quotidien fundamentals-extract sur un "
        "rapport annuel.",
    "Latest interim ·": "Dernier intermédiaire ·",
    "amounts in": "montants en",
    "period-to-date, not annualised": "cumul de période, non annualisé",
    "Interim ratios (period-to-date)": "Ratios intermédiaires (cumul de période)",
    "Revenue": "Chiffre d'affaires",
    "Operating income": "Résultat d'exploitation",
    "Net income": "Résultat net",
    "Total assets": "Total de l'actif",
    "Total equity": "Capitaux propres",
    "EPS": "BPA",
    "Dividend / share": "Dividende / action",
    "Cash flow from ops": "Flux de trésorerie d'exploitation",
    "Capex": "Investissements (Capex)",
    "Free cash flow": "Flux de trésorerie disponible",
    "Ratios · annual · price from latest snapshot":
        "Ratios · annuels · cours du dernier snapshot",
    "last": "dernier",
    "currency mismatch — price-based ratios suppressed":
        "incohérence de devise — ratios de cours masqués",
    "P/E": "P/E",
    "P/B": "P/B",
    "P/S": "P/S",
    "P/FCF": "P/FCF",
    "FCF yield": "Rendement FCF",
    "EV/EBITDA*": "EV/EBITDA*",
    "Dividend yield": "Rendement du dividende",
    "Payout ratio": "Taux de distribution",
    "Earnings yield": "Rendement bénéficiaire",
    "ROE": "ROE",
    "ROA": "ROA",
    "Net margin": "Marge nette",
    "Operating margin": "Marge opérationnelle",
    "Revenue growth (YoY)": "Croissance CA (YoY)",
    "Net income growth (YoY)": "Croissance résultat net (YoY)",
    "EPS growth (YoY)": "Croissance BPA (YoY)",
    "Financial leverage": "Levier financier",
    "Equity ratio": "Ratio d'autonomie financière",
    "* EV/EBITDA uses market cap as an EV proxy and operating income (RBE) as an EBITDA proxy — we don't yet ingest net debt or D&A, so the multiple is directionally informative but not comparable to a full EV/EBITDA screen.":
        "* EV/EBITDA utilise la capitalisation comme approximation de EV et le "
        "résultat d'exploitation (RBE) comme approximation de l'EBITDA — la "
        "dette nette et les amortissements ne sont pas encore intégrés, donc le "
        "multiple est indicatif mais non comparable à un screen EV/EBITDA complet.",
    "References · filings used to populate the rows above · click to open the source PDF":
        "Références · dépôts utilisés pour renseigner les lignes ci-dessus · "
        "cliquez pour ouvrir le PDF source",
    "PERIOD": "PÉRIODE",
    "DOC TYPE": "TYPE DE DOCUMENT",
    "PUBLISHED": "PUBLIÉ",
    "SOURCE": "SOURCE",
    "LINK": "LIEN",
    "open": "ouvrir",

    # ---- corporate actions tab ----
    "EX-DATE": "DATE-EX",
    "YIELD%": "RENDEMENT%",
    "PAY DATE": "DATE PAIEMENT",
    "NOTE": "NOTE",
    "No upcoming corporate actions for":
        "Aucun événement sur titre à venir pour",
    "in the next 90 days.": "dans les 90 prochains jours.",

    # ---- ownership tab ----
    "HOLDER": "ACTIONNAIRE",
    "SHARES": "TITRES",
    "As of": "Au",
    "No ownership data extracted yet for":
        "Aucune donnée d'actionnariat extraite pour le moment pour",

    # ---- segments tab ----
    "REVENUE": "CHIFFRE D'AFFAIRES",
    "SHARE": "PART",
    "By business": "Par activité",
    "By geography": "Par géographie",
    # "Segments" already covered in the tab-labels section above; no override needed
    "No segment breakdown extracted yet for":
        "Aucune ventilation par segment extraite pour le moment pour",
    "Not every issuer publishes a business or geographic split.":
        "Certains émetteurs ne publient pas de ventilation par activité ou "
        "géographie.",

    # ---- analyst tab ----
    "Week of": "Semaine du",
    "Week": "Semaine",
    "No analyst note has been generated yet for":
        "Aucune note d'analyste n'a encore été générée pour",
    "Notes are written weekly on Saturday at 20:00 Africa/Abidjan; use":
        "Les notes sont rédigées le samedi à 20h00 Africa/Abidjan ; utilisez",
    "to trigger a one-off pass.": "pour déclencher un passage ponctuel.",
    "machine-generated when available":
        "généré par IA lorsque disponible",

    # ---- watchlists page ----
    "New list name": "Nom de la nouvelle liste",
    "Create list": "Créer la liste",
    "No watchlists yet.": "Aucune liste pour le moment.",

    # ---- PR-I: bilingual brief + analyst notes ----
    "translation pending": "traduction en cours",
    "Showing English source — French translation not yet available.":
        "Version anglaise affichée — la traduction française n'est pas encore "
        "disponible.",
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
